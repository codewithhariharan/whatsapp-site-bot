"""Batch logging: posts are logged four times a day, not as they arrive.

The groups asked for no per-post replies. So message_handler only stores each
post in pending_messages, and this module drains that inbox at 00:00, 06:00,
12:00 and 18:00 Singapore time — parsing every post, writing it to daily_logs,
dwall_panels or tunnel_updates, and then posting ONE message per group listing
the posts it could not log. A run where everything logged says nothing.

Commands and questions are not batched; they still answer immediately. An /ask
first runs `catch_up()` for its group, so the answer includes posts sent since
the last slot.

Two things this has to get right that real-time logging got for free:

  - The date. A site log is filed under the day the post ARRIVED, not the day
    the batch runs, or everything sent between 18:00 and midnight would land on
    the next day. The same arrival date is the tunnel parser's fallback when a
    message has no date line.
  - Order. Posts are applied oldest first, so a tunnel resend still replaces
    the earlier update for that day instead of being replaced by it.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import database as db
from config import settings
from llm import is_transient
from message_parser import classify_and_parse
from sitetime import SITE_TZ, site_now
from tunnel_parser import parse_tunnel_update
from whatsapp_client import send_message

logger = logging.getLogger("site_bot")

SLOT_HOURS = (0, 6, 12, 18)

# A model outage at 06:00 would otherwise fail a whole run's worth of posts.
# llm.is_transient() picks the errors worth waiting out; anything else fails
# the post at once.
_RETRY_DELAYS = (5, 30)

# Posts parsed at once. The on-demand catch-up keeps someone waiting on /ask,
# and six hours of posts parsed one by one would take minutes.
_PARSE_CONCURRENCY = 6

# One run at a time: the startup catch-up and a scheduled slot must not both
# pick up the same pending rows.
_run_lock = asyncio.Lock()


# ── Slots ─────────────────────────────────────────────────────────────────────

def previous_slot(now: datetime) -> datetime:
    """The most recent slot at or before `now`, in site time."""
    now = now.astimezone(SITE_TZ)
    hour = max(h for h in SLOT_HOURS if h <= now.hour)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def next_slot(now: datetime) -> datetime:
    """The first slot strictly after `now`, in site time."""
    now = now.astimezone(SITE_TZ)
    for h in SLOT_HOURS:
        slot = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=SLOT_HOURS[0], minute=0, second=0, microsecond=0)


def _received(row: dict) -> datetime:
    value = row["received_at"]
    if isinstance(value, str):      # database._coerce hands datetimes back as ISO
        value = datetime.fromisoformat(value)
    return value.astimezone(SITE_TZ)


# ── Processing one post ───────────────────────────────────────────────────────
#
# Split in two. Parsing is a model call and takes seconds, so a run parses
# several posts at once; writing is applied one post at a time, oldest first,
# so a later post still lands after — and wins over — an earlier one.

async def _call_with_retry(fn, *args):
    for delay in (*_RETRY_DELAYS, None):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:
            if delay is None or not is_transient(exc):
                raise
            logger.warning("batch: transient model error, retrying in %ss", delay)
            await asyncio.sleep(delay)


def _is_tunnel(row: dict) -> bool:
    return row["group_id"] in settings.tunnel_group_ids


async def _parse(row: dict):
    if _is_tunnel(row):
        received_on = _received(row).date()
        return await _call_with_retry(parse_tunnel_update, row["text"], received_on)
    return await _call_with_retry(classify_and_parse, row["text"])


async def _write_site_post(row: dict, parsed: dict) -> str:
    msg_type = parsed.get("type")
    group_id = row["group_id"]

    if msg_type == "log":
        data = parsed.get("data", {})
        received = _received(row)
        await asyncio.to_thread(db.upsert_group, group_id)
        await asyncio.to_thread(
            db.insert_log,
            group_id=group_id,
            log_date=received.date(),
            # The time the post was SENT, not the time this run filed it.
            # "What happened between 12 and 6?" is answered from this, and it
            # puts a morning and an afternoon entry for one place in order.
            logged_at=received,
            sender_name=row["sender_name"],
            sender_number=row["sender_number"],
            main_location=data.get("main_location", "Unknown"),
            sub_location=data.get("sub_location", ""),
            description=data.get("description", ""),
            manpower=data.get("manpower", ""),
            raw_message=row["text"],
        )
        return "logged"

    if msg_type == "dwall":
        await asyncio.to_thread(db.upsert_group, group_id)
        await asyncio.to_thread(db.upsert_dwall_panel, group_id, parsed.get("data", {}))
        return "logged"

    # "query" and "ignore". A question is not answered hours after it was
    # asked — by then the answer is stale and the asker has moved on; /ask is
    # still immediate.
    return "skipped"


async def _write_tunnel_post(row: dict, update: dict | None) -> str:
    if not update:
        return "skipped"  # looked like an update, but isn't one

    update["sender_name"] = row["sender_name"]
    update["sender_number"] = row["sender_number"]
    update.pop("date_was_stated", None)

    await asyncio.to_thread(db.upsert_group, row["group_id"])
    await asyncio.to_thread(db.upsert_tunnel_update, row["group_id"], update)
    return "logged"


async def _write(row: dict, parsed) -> str:
    if _is_tunnel(row):
        return await _write_tunnel_post(row, parsed)
    return await _write_site_post(row, parsed)


# ── A run ─────────────────────────────────────────────────────────────────────

async def run_batch(cutoff: datetime | None = None, label: str | None = None,
                    group_id: str | None = None) -> dict:
    """Log every post received before `cutoff`, then report the failures.

    `group_id` limits the run to one group's posts — that is the /ask catch-up,
    which only needs the asker's group to be current.

    Returns {group_id: [failed rows]} — mostly for the tests.
    """
    async with _run_lock:
        rows = await asyncio.to_thread(db.get_pending_messages, cutoff, group_id)
        if not rows:
            return {}

        gate = asyncio.Semaphore(_PARSE_CONCURRENCY)

        async def parse(row):
            async with gate:
                try:
                    return await _parse(row), None
                except Exception as exc:
                    return None, exc

        parsed_rows = await asyncio.gather(*(parse(row) for row in rows))

        failed = defaultdict(list)
        counts = defaultdict(int)
        for row, (parsed, parse_error) in zip(rows, parsed_rows):
            try:
                if parse_error is not None:
                    raise parse_error
                status = await _write(row, parsed)
                error = None
            except Exception as exc:
                logger.error("batch: could not log message %s", row["id"],
                             exc_info=exc)
                status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                failed[row["group_id"]].append(row)
            counts[status] += 1
            await asyncio.to_thread(db.mark_message, row["id"], status, error)

        logger.info("batch %s: %s", label or "run", dict(counts))

        for gid, bad in failed.items():
            try:
                await send_message(gid, failure_report(bad, label))
            except Exception:
                logger.exception("batch: could not post failure report to %s", gid)

        return dict(failed)


async def catch_up(group_id: str) -> None:
    """Log a group's waiting posts now, so /ask answers from them.

    Posts otherwise wait for the next slot, so a question at 16:00 about "this
    afternoon" would be answered from a record that stops at 12:00. The
    scheduled runs carry on as before; they just find less waiting.

    Never raises: if the catch-up fails, /ask answers from what is already
    filed — a slightly stale answer beats none, and the posts stay pending for
    the next run.
    """
    try:
        await run_batch(label="on-demand", group_id=group_id)
    except Exception:
        logger.exception("catch-up for %s failed; answering from filed data", group_id)


def failure_report(rows: list[dict], label: str | None = None) -> str:
    """The one message a run posts, and only when something failed."""
    n = len(rows)
    heading = f"⚠️ *{n} post{'s' if n != 1 else ''} couldn't be logged*"
    if label:
        heading += f" ({label} run)"

    lines = [heading, ""]
    for i, row in enumerate(rows, start=1):
        when = _received(row).strftime("%d %b %H:%M")
        first = next((l.strip() for l in row["text"].splitlines() if l.strip()), "")
        if len(first) > 60:
            first = first[:57] + "..."
        lines.append(f"{i}. {row['sender_name'] or row['sender_number']}, {when}")
        lines.append(f"   _{first}_")
    lines += ["", "Please check these and resend."]
    return "\n".join(lines)


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _sleep_until(target: datetime):
    # Short hops rather than one long sleep, so a suspended host or a clock
    # step cannot carry the loop far past its slot.
    while (remaining := (target - site_now()).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, 300))


async def scheduler():
    """Run forever: catch up on a missed slot, then run at each slot."""
    # If the container was down across a slot, the posts from before that slot
    # are logged now rather than waiting up to six more hours. Posts since the
    # slot still wait for the next one.
    missed = previous_slot(site_now())
    try:
        await run_batch(cutoff=missed, label="catch-up")
    except Exception:
        logger.exception("batch: catch-up run failed")

    while True:
        slot = next_slot(site_now())
        await _sleep_until(slot)
        try:
            await run_batch(cutoff=slot, label=slot.strftime("%H:%M"))
        except Exception:
            logger.exception("batch: %s run failed", slot.strftime("%H:%M"))
