"""Routing for the tunnel groups: each contract's group, and the master group.

A group's kind is decided once, on its JID, in handle_message(). Everything
below assumes it already went that way, so the site-work commands (/daily,
/reorder, /setorder, /dwall) are simply absent here.

A contract group sends the standardised TUNNEL PROGRESS UPDATE, and any line
with a "!" in it — inside an update or in a separate message — is a flag. Both
are held and filed at the 00/06/12/18 runs, or straight away when someone asks.

The master group holds the senior group director and the bot. It sends nothing
to file. /!! lists the day's flags from every contract; anything else is a
question answered across every contract.

/tunnel emails a day's summary on request, and daily_email_scheduler() emails
every contract's summary for the day at TUNNEL_DAILY_EMAIL_AT.
"""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta

import database as db
from config import settings
from sitetime import site_now, site_today
from tunnel_parser import extract_flags, is_progress_update, parse_date
from whatsapp_client import send_message, send_document

logger = logging.getLogger("site_bot")


HELP_TEXT = (
    "🚇 *Tunnel Bot — Commands*\n\n"
    "*/ask* _your question_\n"
    "Ask anything about this contract's tunnel updates.\n"
    "• /ask how many rings were built this week?\n"
    "• /ask any instruments breaching AL?\n\n"
    "*/!!* — today's items marked with \"!\"\n"
    "*/!! yesterday*, */!! 28 Sep* — another day\n\n"
    "*/tunnel* — email today's summary\n"
    "*/tunnel yesterday*, */tunnel 15 Aug* — another day\n\n"
    "*/excel* — every update as a spreadsheet\n\n"
    "📌 Engineers: send the standard update, once per shift —\n"
    "TUNNEL PROGRESS UPDATE\n"
    "CONTRACT: CRxxx\n"
    "DATE: DD-MMM-YYYY\n"
    "SHIFT: Day / Night\n"
    "DRIVE: …\n"
    "PROGRESS: built / current / total (%)\n"
    "- Follow-on works: …\n"
    "- Soil disposal: …\n"
    "- On site storage: …\n"
    "TBM LOCATION: …\n"
    "INSTRUMENTATION: …\n"
    "ISSUES: [None / …]\n\n"
    "Put a \"!\" on any line that is critical.\n"
    "Updates are filed at 12am, 6am, 12pm and 6pm —\n"
    "the bot only replies if one couldn't be filed."
)

MASTER_HELP_TEXT = (
    "🚇 *Tunnel Master — Commands*\n\n"
    "*/!!* — today's items marked with \"!\", every contract\n"
    "*/!! yesterday*, */!! 28 Sep*, */!! monday* — another day\n\n"
    "*/tunnel* — email today's summary, every contract\n"
    "*/tunnel yesterday*, */tunnel 15 Aug* — another day\n\n"
    "Ask anything else in plain words, e.g.\n"
    "• where is every TBM now?\n"
    "• how many rings did CR146 build this week?\n"
    "• which contracts have instruments breaching AL?\n\n"
    "*/excel* — every contract's updates as a spreadsheet\n\n"
    "📧 Every contract's summary is also emailed daily at 6pm."
)


# ── Contract groups ───────────────────────────────────────────────────────────

async def handle_tunnel_message(group_id: str, sender_name: str,
                                sender_number: str, text: str):
    """Route one message in a contract's tunnel group."""
    lower = text.lower().strip()

    if lower.startswith("/help"):
        await send_message(group_id, HELP_TEXT)
        return

    if _is_flags_command(lower):
        await handle_flags(group_id, _flags_args(text), all_contracts=False)
        return

    if lower.startswith("/tunnel"):
        await handle_tunnel_email(group_id, text.strip()[len("/tunnel"):], all_contracts=False)
        return

    if lower.startswith("/excel"):
        await handle_tunnel_export(group_id, all_contracts=False)
        return

    if lower.startswith("/ask"):
        await handle_tunnel_ask(group_id, text[len("/ask"):].strip())
        return

    # Held for the next run (00/06/12/18), not filed now, and no reply. An
    # update, or anything carrying a "!" — the director's /!! must see a
    # flag whether or not it came inside an update.
    if is_progress_update(text) or extract_flags(text):
        await asyncio.to_thread(
            db.enqueue_message, group_id, sender_name, sender_number, text
        )
        return

    # Anything ending in a question mark, or opening with a question word, is
    # treated as a question, so nobody has to know there is a /ask command.
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


# ── Master group ──────────────────────────────────────────────────────────────

async def handle_master_message(group_id: str, sender_name: str,
                                sender_number: str, text: str):
    """Route one message in the master group. Nothing here is ever filed."""
    lower = text.lower().strip()

    if lower.startswith("/help"):
        await send_message(group_id, MASTER_HELP_TEXT)
        return

    if _is_flags_command(lower, allow_bare=True):
        await handle_flags(group_id, _flags_args(text), all_contracts=True)
        return

    if lower.startswith("/tunnel"):
        await handle_tunnel_email(group_id, text.strip()[len("/tunnel"):], all_contracts=True)
        return

    if lower.startswith("/excel"):
        await handle_tunnel_export(group_id, all_contracts=True)
        return

    question = text[len("/ask"):].strip() if lower.startswith("/ask") else text.strip()
    if len(question) < 3:
        return      # an emoji or "ok" is not a question
    await handle_tunnel_ask(group_id, question, all_contracts=True)


# ── /!! ───────────────────────────────────────────────────────────────────────

# "/!!", "/critical", and the emoji a phone offers for "!!".
_FLAGS_COMMAND = re.compile(r"^/\s*(!!|‼️?|critical)", re.I)
# The master group also takes a bare "!!" — only the director writes there. In
# a contract group a line starting "!!" is an engineer's flag, not a command.
_BARE_FLAGS_COMMAND = re.compile(r"^(!!|‼️?)", re.I)


def _is_flags_command(lower: str, allow_bare: bool = False) -> bool:
    if _FLAGS_COMMAND.match(lower):
        return True
    return allow_bare and bool(_BARE_FLAGS_COMMAND.match(lower))


def _flags_args(text: str) -> str:
    text = text.strip()
    text = _FLAGS_COMMAND.sub("", text, count=1)
    return _BARE_FLAGS_COMMAND.sub("", text.strip(), count=1).strip()


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def parse_day(args: str, today: date) -> date | None:
    """The day /!! asks about. None if the words are not a day.

    '' / today, yesterday, a weekday (the most recent one), '29', '29th',
    '28 Sep', 'Sep 28', '28-Sep-2026', '28/09', '2026-09-28'. A date with no
    year that would be in the future means last year's.
    """
    words = re.sub(r"[,.]", " ", args.lower()).split()
    words = [w for w in words if w not in ("of", "for", "on", "from", "the", "items")]
    phrase = " ".join(words)

    if phrase in ("", "today", "tdy"):
        return today
    if phrase in ("yesterday", "ytd", "yday"):
        return today - timedelta(days=1)
    weekday = [i for i, d in enumerate(_WEEKDAYS)
               if len(phrase) >= 3 and d.startswith(phrase)]
    if weekday:                                                 # "mon", "monday"
        return today - timedelta(days=(today.weekday() - weekday[0]) % 7)

    phrase = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", phrase)

    if re.fullmatch(r"\d{1,2}", phrase):                       # "29"
        day = int(phrase)
        for back in range(0, 3):
            month_start = (today.replace(day=1) - timedelta(days=31 * back)).replace(day=1)
            try:
                candidate = month_start.replace(day=day)
            except ValueError:
                continue
            if candidate <= today:
                return candidate
        return None

    # "sep 28" -> "28 sep"
    swapped = re.sub(r"^([a-z]{3,9}) (\d{1,2})\b", r"\2 \1", phrase)
    for candidate in {phrase, swapped}:
        parsed = parse_date(candidate)
        if parsed:
            return parsed
        for sep in (" ", "-", "/"):
            try_with_year = f"{candidate}{sep}{today.year}"
            parsed = parse_date(try_with_year)
            if parsed:
                return parsed if parsed <= today else parsed.replace(year=today.year - 1)
    return None


def format_flags(day: date, rows: list[dict], all_contracts: bool) -> str:
    """The /!! reply: the engineers' own words, grouped by contract."""
    when = day.strftime("%d %b %Y").lstrip("0")
    if not rows:
        scope = "any contract" if all_contracts else "this group"
        return f"✅ No \"!\" items were posted in {scope} on {when}."

    by_contract: dict[str, list[str]] = {}
    for row in rows:
        contract = row.get("contract") or "Contract not known"
        text = row["flag_text"]
        line = f"{row['drive']}: {text}" if row.get("drive") else text
        lines = by_contract.setdefault(contract, [])
        # A resent message can repeat a line; the director needs it once.
        if line not in lines:
            lines.append(line)

    parts = [f"‼️ *Critical items – {when}*"]
    for contract in sorted(by_contract, key=lambda c: (c == "Contract not known", c)):
        parts.append("")
        parts.append(f"*{contract}*")
        parts.extend(f"• {line}" for line in by_contract[contract])
    return "\n".join(parts)


async def handle_flags(group_id: str, args: str, all_contracts: bool):
    """/!! [day] — every "!" line posted that day, word for word.

    No model in the path: the reply is the engineers' own text, so nothing can
    be softened, merged or left out.
    """
    from ingest_batch import catch_up

    day = parse_day(args, site_today())
    if day is None:
        await send_message(
            group_id,
            "⚠️ I couldn't read that day. Try /!!, /!! yesterday, "
            "/!! 28 Sep or /!! monday."
        )
        return

    # File anything waiting first, so today's flags are complete.
    await catch_up(_groups_in_scope(group_id, all_contracts))

    try:
        rows = await asyncio.to_thread(
            db.get_flags_for_day, day, None if all_contracts else group_id
        )
    except Exception:
        logger.exception("/!! failed for %s", group_id)
        await send_message(group_id, "⚠️ Couldn't read the critical items. Please try again.")
        return

    await send_message(group_id, format_flags(day, rows, all_contracts))


def _groups_in_scope(group_id: str, all_contracts: bool) -> list[str]:
    return sorted(settings.tunnel_group_ids) if all_contracts else [group_id]


# ── /ask ──────────────────────────────────────────────────────────────────────

async def handle_tunnel_ask(group_id: str, question: str, all_contracts: bool = False):
    from tunnel_ai import answer_tunnel_query
    from ingest_batch import catch_up

    if not question.strip():
        await send_message(group_id, "⚠️ Usage: /ask your question here")
        return
    await send_message(group_id, "🔍 Searching...")
    await catch_up(_groups_in_scope(group_id, all_contracts))

    try:
        answer = await asyncio.to_thread(
            answer_tunnel_query, group_id, question, all_contracts
        )
    except Exception:
        logger.exception("tunnel /ask failed for %s: %r", group_id, question)
        await send_message(
            group_id,
            "⚠️ Couldn't answer that one — the search failed.\n"
            "Try rephrasing, or narrow it to a date or a contract."
        )
        return

    await send_message(group_id, f"🤖 {answer}")


# ── /tunnel ───────────────────────────────────────────────────────────────────

# "/tunnel updates", "/tunnel summary for yesterday" — the words around the day.
_TUNNEL_FILLER = {"update", "updates", "summary", "summarise", "summarize",
                  "email", "mail", "report", "progress", "please", "pls", "send"}


def _mask(address: str) -> str:
    """h…@gmail.com — the group sees where it went, not the whole address."""
    name, _, domain = address.partition("@")
    return f"{name[:1]}…@{domain}" if domain else address


async def handle_tunnel_email(group_id: str, args: str, all_contracts: bool):
    """/tunnel [day] — summarise a day's updates and email them.

    The day defaults to today, site time.
    """
    import mailer
    import tunnel_summary as ts
    from ingest_batch import catch_up

    recipients = settings.tunnel_email_to
    if not mailer.is_configured(recipients):
        await send_message(group_id, "⚠️ Email isn't set up for /tunnel yet.")
        return

    words = [w for w in re.sub(r"[,.]", " ", args).split()
             if w.lower() not in _TUNNEL_FILLER]
    day = parse_day(" ".join(words), site_today())
    if day is None:
        await send_message(
            group_id,
            "⚠️ I couldn't read that day. Try /tunnel, /tunnel yesterday "
            "or /tunnel 15 Aug."
        )
        return
    scope = None if all_contracts else group_id

    # File anything waiting first, so the latest shift is in the summary.
    await catch_up(_groups_in_scope(group_id, all_contracts))

    try:
        rows = await asyncio.to_thread(db.get_tunnel_progress_for_day, day, scope)
        flags = await asyncio.to_thread(db.get_flags_for_report, day, scope)
    except Exception:
        logger.exception("/tunnel failed reading %s", group_id)
        await send_message(group_id, "⚠️ Couldn't read the tunnel updates. Please try again.")
        return

    if not rows:
        await send_message(group_id, f"📭 No tunnel updates report on {ts.when(day)}.")
        return

    await send_message(group_id, f"⏳ Summarising the tunnel updates for {ts.when(day)}...")

    contracts = sorted({r["contract"] for r in rows})
    label = "All contracts" if all_contracts else ", ".join(contracts)
    try:
        await asyncio.to_thread(_send_summary, day, rows, flags, label, recipients)
    except Exception:
        logger.exception("/tunnel email failed for %s", group_id)
        await send_message(group_id, "⚠️ The summary was ready but the email didn't send.")
        return

    await send_message(
        group_id,
        f"📧 Tunnel summary for {ts.when(day)} — {len(rows)} update"
        f"{'s' if len(rows) != 1 else ''}, {len(contracts)} contract"
        f"{'s' if len(contracts) != 1 else ''} — emailed to "
        + ", ".join(_mask(r) for r in recipients) + "."
    )


def _send_summary(day: date, rows: list[dict], flags: list[dict], label: str,
                  recipients: list[str]) -> None:
    """Build one day's summary and email it. Blocking; raises if the send fails."""
    import mailer
    import tunnel_summary as ts

    try:
        ai_text = ts.highlights(day, rows, flags)
    except Exception:
        # The figures are the point; the email goes without the model's read.
        logger.exception("tunnel highlights failed for %s", day)
        ai_text = None
    subject, text, page = ts.build_email(day, rows, flags, ai_text, label)
    mailer.send_email(recipients, subject, text, page)


# ── The daily email ───────────────────────────────────────────────────────────

async def send_daily_email(day: date) -> None:
    """Every contract's summary for `day`, emailed. No WhatsApp message.

    A day with no updates still sends a short note, so a missing email always
    means something went wrong rather than a quiet day.
    """
    import mailer
    import tunnel_summary as ts
    from ingest_batch import catch_up

    recipients = settings.tunnel_email_to
    if not mailer.is_configured(recipients):
        logger.warning("daily tunnel email skipped: email is not set up")
        return

    await catch_up(sorted(settings.tunnel_group_ids))
    rows = await asyncio.to_thread(db.get_tunnel_progress_for_day, day, None)
    flags = await asyncio.to_thread(db.get_flags_for_report, day, None)

    if rows:
        await asyncio.to_thread(_send_summary, day, rows, flags, "All contracts", recipients)
    else:
        await asyncio.to_thread(
            mailer.send_email, recipients,
            f"Tunnel Progress Summary – All contracts – {ts.when(day)}",
            f"No tunnel progress updates were reported for {ts.when(day)}.\n",
        )
    logger.info("daily tunnel email sent for %s (%d updates)", day, len(rows))


def next_daily_email(now: datetime) -> datetime | None:
    """The next TUNNEL_DAILY_EMAIL_AT after `now`, site time. None if it is off."""
    at = settings.TUNNEL_DAILY_EMAIL_AT.strip()
    if not at:
        return None
    hour, minute = (int(p) for p in at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


async def daily_email_scheduler():
    """Run forever: email every contract's summary at TUNNEL_DAILY_EMAIL_AT.

    A restart after the time does not resend: that day's email is the one
    already sent, or none if the container was down across it.
    """
    from ingest_batch import _sleep_until

    while (target := next_daily_email(site_now())) is not None:
        await _sleep_until(target)
        try:
            await send_daily_email(target.date())
        except Exception:
            logger.exception("daily tunnel email for %s failed", target.date())


# ── /excel ────────────────────────────────────────────────────────────────────

async def handle_tunnel_export(group_id: str, all_contracts: bool = False):
    import excel_generator as xls

    scope = None if all_contracts else group_id
    await send_message(group_id, "⏳ Generating the tunnel report...")
    try:
        updates = await asyncio.to_thread(db.get_tunnel_progress, scope)
        if not updates:
            await send_message(group_id, "📭 No tunnel updates recorded yet.")
            return
        flags = await asyncio.to_thread(db.get_all_flags, scope)
        data = await asyncio.to_thread(xls.generate_tunnel_excel, updates, flags)
    except Exception:
        logger.exception("tunnel export failed for %s", group_id)
        await send_message(group_id, "⚠️ Couldn't build the spreadsheet.")
        return

    contracts = len({u["contract"] for u in updates})
    await send_document(
        group_id, data, f"Tunnel_Progress_{site_today():%Y%m%d}.xlsx",
        caption=(f"🚇 Tunnel progress — {len(updates)} updates across "
                 f"{contracts} contract{'s' if contracts != 1 else ''}"),
    )
