import asyncio
from config import settings
import database as db
import commands as cmd
from tunnel_handler import handle_tunnel_message


async def handle_message(group_id: str, sender_name: str, sender_number: str, text: str):
    """Route an incoming message to the correct handler."""

    text = text.strip()
    lower = text.lower()

    # ── Tunnel groups ─────────────────────────────────────────────────────────
    # Decided once, on the JID, before anything else runs. A tunnel group's
    # messages must never reach classify_and_parse — that would file a TBM
    # update into daily_logs as a site log, and the two records are meant to
    # stay separate.
    if group_id in settings.tunnel_group_ids:
        await handle_tunnel_message(group_id, sender_name, sender_number, text)
        return

    # ── Commands ──────────────────────────────────────────────────────────────
    if lower.startswith("/help"):
        await cmd.handle_help(group_id)
        return

    if lower.startswith("/setorder"):
        args = text[len("/setorder"):].strip()
        await cmd.handle_setorder(group_id, args)
        return

    if lower.startswith("/daily"):
        await cmd.handle_daily(group_id)
        return

    if lower.startswith("/reorder"):
        args = text[len("/reorder"):].strip()
        await cmd.handle_reorder(group_id, args)
        return

    if lower.startswith("/confirm"):
        await cmd.handle_confirm(group_id)
        return

    if lower.startswith("/delete"):
        args = text[len("/delete"):].strip()
        await cmd.handle_delete(group_id, args)
        return

    if lower.startswith("/excel"):
        args = text[len("/excel"):].strip()
        await cmd.handle_excel(group_id, args)
        return

    if lower.startswith("/dwall"):
        # /dwall with no args = export; with args = handled by parser below
        args = text[len("/dwall"):].strip()
        if not args:
            await cmd.handle_dwall_export(group_id)
            return
        # Fall through to parser for dwall data entry

    if lower.startswith("/ask"):
        question = text[len("/ask"):].strip()
        await cmd.handle_ask(group_id, question)
        return

    # ── Everything else waits for the next batch run ──────────────────────────
    # Logs, D-Wall entries and chat alike: no reply now. ingest_batch.py parses
    # and logs them at 00/06/12/18 and reports only the posts it could not log.
    await asyncio.to_thread(db.enqueue_message, group_id, sender_name, sender_number, text)
