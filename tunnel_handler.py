"""Routing for a tunnel group.

A group is a tunnel group or it is not — the decision is made once, on the JID,
in handle_message(). Everything below assumes it already went that way, so the
site-work commands (/daily, /reorder, /setorder, /dwall) are simply absent here
rather than guarded one by one. A tunnel group has three jobs: log the update,
answer questions, hand out the spreadsheet.
"""

import asyncio
import logging

import database as db
from tunnel_parser import is_tunnel_update
from whatsapp_client import send_message, send_document

logger = logging.getLogger("site_bot")


HELP_TEXT = (
    "🚇 *Tunnel Bot — Commands*\n\n"
    "*/ask* _your question_\n"
    "Ask anything about the tunnel updates.\n"
    "Examples:\n"
    "• /ask what are the critical activities?\n"
    "• /ask what is P103's completion now?\n"
    "• /ask how many rings were built last week?\n\n"
    "*/critical*\n"
    "List every ‼ flagged item, newest first.\n"
    "*/critical* _P103_ — one contract only.\n\n"
    "*/excel*\n"
    "Export every update as a spreadsheet.\n\n"
    "📌 Engineers: send your update in the usual format —\n"
    "contract and date on the first two lines, then\n"
    "*Main Drive*, *TBM Progress* and *Delays*.\n"
    "Put anything critical between ‼ marks.\n"
    "Updates are logged at 12am, 6am, 12pm and 6pm —\n"
    "the bot only replies if one couldn't be logged."
)


async def handle_tunnel_message(group_id: str, sender_name: str,
                                sender_number: str, text: str):
    """Route one message in a tunnel group."""
    lower = text.lower()

    if lower.startswith("/help"):
        await send_message(group_id, HELP_TEXT)
        return

    if lower.startswith("/critical"):
        await handle_critical(group_id, text[len("/critical"):].strip())
        return

    if lower.startswith("/excel") or lower.startswith("/tunnel"):
        await handle_tunnel_export(group_id)
        return

    if lower.startswith("/ask"):
        await handle_tunnel_ask(group_id, text[len("/ask"):].strip())
        return

    # A structural test, not a model call: the group carries ordinary chat too,
    # and classifying every "noted" would cost a request each time.
    # It is held for the next batch run (00/06/12/18), not logged now, and
    # gets no reply — ingest_batch.py reports only the ones it could not log.
    if is_tunnel_update(text):
        await asyncio.to_thread(
            db.enqueue_message, group_id, sender_name, sender_number, text
        )
        return

    # Anything ending in a question mark, or opening with a question word, is
    # treated as a question. This is what lets the director just ask, without
    # having to know there is a /ask command.
    if _looks_like_question(text):
        await handle_tunnel_ask(group_id, text)
        return

    # Ordinary chat — say nothing.


_QUESTION_OPENERS = (
    "what", "when", "where", "which", "who", "why", "how", "is there",
    "are there", "any ", "show me", "list ", "give me", "tell me",
    "can you", "do we", "did we", "has ", "have we",
)


def _looks_like_question(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 4 or len(stripped) > 400:
        return False
    if stripped.endswith("?"):
        return True
    return stripped.lower().startswith(_QUESTION_OPENERS)


async def handle_tunnel_ask(group_id: str, question: str):
    from tunnel_ai import answer_tunnel_query

    if not question.strip():
        await send_message(group_id, "⚠️ Usage: /ask your question here")
        return
    await send_message(group_id, "🔍 Searching...")

    try:
        answer = await asyncio.to_thread(answer_tunnel_query, group_id, question)
    except Exception:
        logger.exception("tunnel /ask failed for %s: %r", group_id, question)
        await send_message(
            group_id,
            "⚠️ Couldn't answer that one — the search failed.\n"
            "Try rephrasing, or narrow it to a date or a contract."
        )
        return

    await send_message(group_id, f"🤖 {answer}")


async def handle_critical(group_id: str, args: str = ""):
    """List the flagged items straight from the exclamation column.

    No model in the path. The director's standing question has one exact
    answer — the contents of that column — and reading it directly means the
    reply cannot drift, cannot summarise away a flag, and comes back instantly.
    /ask handles the same question conversationally when it is phrased less
    directly.
    """
    contract = args.strip() or None
    rows = await asyncio.to_thread(
        db.get_tunnel_exclamations, group_id, None, 50
    )
    if contract:
        rows = [r for r in rows if r["contract"].lower() == contract.lower()]

    if not rows:
        scope = f" for {contract}" if contract else ""
        await send_message(
            group_id,
            f"✅ No critical items flagged{scope}.\n"
            "(Nothing has been sent between ‼ marks.)"
        )
        return

    header = f"‼️ *Critical items* ({len(rows)})"
    if contract:
        header += f" — {contract}"
    parts = [header, ""]
    for row in rows:
        body = "\n   ".join(row["exclamation"].splitlines())
        parts.append(f"*{row['update_date']}* — {row['contract']}\n   {body}")

    await send_message(group_id, "\n\n".join(parts))


async def handle_tunnel_export(group_id: str):
    import excel_generator as xls

    updates = await asyncio.to_thread(db.get_tunnel_updates, group_id)
    if not updates:
        await send_message(group_id, "📭 No tunnel updates recorded yet.")
        return

    await send_message(group_id, "📊 Generating tunnel report...")
    try:
        data = await asyncio.to_thread(xls.generate_tunnel_excel, updates)
    except Exception:
        logger.exception("tunnel export failed for %s", group_id)
        await send_message(group_id, "⚠️ Couldn't build the spreadsheet.")
        return

    await send_document(
        group_id, data, "Tunnel_Updates.xlsx",
        caption=f"🚇 Tunnel updates — {len(updates)} entries",
    )
