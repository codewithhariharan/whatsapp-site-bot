"""Backfill Supabase from messages the bot never processed.

Covers outages like 11–26 July 2026, when the Baileys bridge was down. The
bridge keeps no raw archive (it forwards live `notify` messages only), so the
messages have to be recovered from WhatsApp itself. Two sources, same pipeline:

  1. A WhatsApp "Export chat" .txt — export WITH media, because the site logs
     here are photo captions and a without-media export discards them.
  2. history.json from history-harvester/ — reads captions straight out of the
     message payload, so it also recovers photos the phone no longer holds.

This deliberately does NOT go through message_handler.handle_message, for two
reasons: that path replies into the group (a backfill would spray hundreds of
"✅ Logged" messages at everyone) and it stamps every row with date.today(),
which would pile three weeks of work onto a single day. Here the log_date comes
from the message's own timestamp.

Usage
-----
    # Preview — writes no rows, prints what it *would* do:
    python backfill.py chat.txt --group-id "1203...@g.us" \
        --from 2026-07-11 --to 2026-07-26

    # Commit, once the preview looks right:
    python backfill.py chat.txt --group-id "1203...@g.us" \
        --from 2026-07-11 --to 2026-07-26 --apply

    # Same, from a harvested history.json:
    python backfill.py --from-history history.json \
        --group-id "1203...@g.us" --from 2026-07-13 --to 2026-07-25

    # Discover group_ids the bot already knows about:
    python backfill.py --list-groups
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import database as db
from message_parser import classify_and_parse
from whatsapp_export import ExportedMessage, parse_export

# Below this, a message can't hold a location + description + manpower. Skipping
# them keeps a few hundred "ok" / "👍" lines from costing an API call each.
MIN_LENGTH = 15

# The bot posts into the same group from the work phone, so its own replies come
# back in the export. Re-classifying them would turn the bot's /ask answers —
# which quote locations and panel numbers — into brand-new fictional log rows.
BOT_REPLY_PREFIXES = ("✅", "🔍", "🤖", "📊", "❌", "⚠️")

# Classification is network-bound, so overlap the calls. Writes stay sequential
# and in chronological order, so a later edit to a panel still wins.
CLASSIFY_WORKERS = 6


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def _sender_number(sender: str) -> str:
    """Export files name saved contacts, but show unsaved ones as a number."""
    digits = "".join(c for c in sender if c.isdigit())
    looks_like_number = sender.lstrip("+").replace(" ", "").replace("-", "").isdigit()
    return digits if looks_like_number and len(digits) >= 7 else ""


def load_history(path: Path, group_id: str) -> list[ExportedMessage]:
    """Read history-harvester output into the same shape as a parsed export.

    The harvester records unix seconds; everything downstream works in local
    dates, matching how the live bot stamps rows.
    """
    rows = json.loads(path.read_text(encoding="utf-8"))
    messages = []
    for row in rows:
        if row.get("group_id") and row["group_id"] != group_id:
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        messages.append(ExportedMessage(
            timestamp=datetime.fromtimestamp(row["timestamp"]),
            sender=row.get("sender_name") or row.get("sender_number", ""),
            text=text,
            sender_number=row.get("sender_number", ""),
        ))
    messages.sort(key=lambda m: m.timestamp)
    return messages


def select_messages(
    messages: list[ExportedMessage], start: date, end: date
) -> list[ExportedMessage]:
    """Messages in [start, end] worth spending a classification call on."""
    selected = []
    for m in messages:
        if not (start <= m.log_date <= end):
            continue
        if m.text.startswith("/"):
            continue  # a command; replaying it would re-trigger exports
        if m.text.startswith(BOT_REPLY_PREFIXES):
            continue
        if len(m.text) < MIN_LENGTH:
            continue
        selected.append(m)
    return selected


def _classify(message: ExportedMessage) -> tuple[ExportedMessage, dict | None]:
    try:
        return message, classify_and_parse(message.text)
    except Exception as exc:  # noqa: BLE001 — one bad message must not abort the run
        print(f"  ! parse failed at {message.timestamp}: {exc}", file=sys.stderr)
        return message, None


def dedupe_key(log_date: str, text: str) -> tuple[str, str]:
    """Identity of a log row for duplicate detection.

    Whitespace and case are normalised because the same caption does not arrive
    byte-identically by both routes: what the bridge received live and what an
    export renders differ in trailing spaces, blank lines and capitalisation.
    Comparing raw text finds only ~46% of the rows that are actually already
    stored, so a rerun would quietly re-insert the rest.
    """
    return (log_date, " ".join(text.split()).lower())


def _existing_log_keys(group_id: str, start: date, end: date) -> set[tuple[str, str]]:
    """Keys already stored, so re-runs don't duplicate.

    daily_logs has no unique constraint and no message id, so this is the only
    thing standing between a second run and a doubled table.
    """
    # Paged: PostgREST truncates at 1000 rows without saying so, and a silently
    # short list here means every row past the cap gets inserted a second time.
    rows = db._fetch_all(lambda: (
        db.db.table("daily_logs")
        .select("log_date, raw_message")
        .eq("group_id", group_id)
        .gte("log_date", start.isoformat())
        .lte("log_date", end.isoformat())
        .order("log_date")
    ))
    return {dedupe_key(r["log_date"], r["raw_message"] or "") for r in rows}


def _existing_panels(group_id: str) -> set[str]:
    return {p["panel_number"] for p in db.get_all_panels(group_id) if p["panel_number"]}


def run(
    source_path: Path,
    group_id: str,
    start: date,
    end: date,
    apply: bool,
    date_order: str | None,
    overwrite_dwall: bool,
    from_history: bool = False,
    logs_only: bool = False,
) -> int:
    if from_history:
        all_messages = load_history(source_path, group_id)
        empty_hint = f"No messages for {group_id} in {source_path.name}."
    else:
        content = source_path.read_text(encoding="utf-8", errors="replace")
        all_messages = parse_export(content, date_order=date_order)
        empty_hint = "No messages parsed — is this a WhatsApp chat export?"

    if not all_messages:
        print(empty_hint, file=sys.stderr)
        return 1

    span = f"{all_messages[0].log_date} to {all_messages[-1].log_date}"
    print(f"Loaded {len(all_messages)} messages from {source_path.name} ({span})")

    candidates = select_messages(all_messages, start, end)
    print(f"{len(candidates)} candidates in {start} to {end} after filtering\n")
    if not candidates:
        return 0

    seen_logs = _existing_log_keys(group_id, start, end)
    seen_panels = set() if overwrite_dwall else _existing_panels(group_id)
    if seen_logs:
        print(f"{len(seen_logs)} log rows already present in this range — will skip matches")
    if seen_panels:
        print(f"{len(seen_panels)} panels already present — will skip (use --overwrite-dwall to replace)")

    # Drop duplicates BEFORE classifying, not at write time: an album repeats one
    # caption across every photo, so a full-range run would otherwise spend
    # hundreds of API calls re-deciding text it already has an answer for.
    skipped_dupe = 0
    todo, batch_seen = [], set()
    for message in candidates:
        key = dedupe_key(message.log_date.isoformat(), message.text)
        if key in seen_logs or key in batch_seen:
            skipped_dupe += 1
            continue
        batch_seen.add(key)
        todo.append(message)
    if skipped_dupe:
        print(f"{skipped_dupe} duplicates skipped before classifying; {len(todo)} to classify")

    with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        classified = list(pool.map(_classify, todo))

    logs, panels, preview = 0, 0, []
    skipped_panel = 0

    if apply:
        db.upsert_group(group_id)

    for message, parsed in classified:
        if not parsed:
            continue
        kind = parsed.get("type")
        data = parsed.get("data", {})

        if kind == "log":
            key = dedupe_key(message.log_date.isoformat(), message.text)
            if key in seen_logs:
                skipped_dupe += 1
                continue
            seen_logs.add(key)
            preview.append({
                "type": "log",
                "log_date": message.log_date.isoformat(),
                "sender": message.sender,
                "main_location": data.get("main_location", "Unknown"),
                "sub_location": data.get("sub_location", ""),
                "description": data.get("description", ""),
                "manpower": data.get("manpower", ""),
            })
            if apply:
                db.insert_log(
                    group_id=group_id,
                    log_date=message.log_date,
                    sender_name=message.sender,
                    sender_number=message.sender_number or _sender_number(message.sender),
                    main_location=data.get("main_location", "Unknown"),
                    sub_location=data.get("sub_location", ""),
                    description=data.get("description", ""),
                    manpower=data.get("manpower", ""),
                    raw_message=message.text,
                )
            logs += 1

        elif kind == "dwall":
            if logs_only:
                skipped_panel += 1
                continue
            panel = data.get("panel_number")
            # An upsert would overwrite a panel updated *after* the outage with
            # this older entry, so existing panels are left alone by default.
            if panel in seen_panels:
                skipped_panel += 1
                continue
            seen_panels.add(panel)
            preview.append({
                "type": "dwall",
                "log_date": message.log_date.isoformat(),
                "sender": message.sender,
                "panel_number": panel,
                "entry_number": data.get("entry_number", ""),
            })
            if apply:
                data.setdefault("raw_message", message.text)
                db.upsert_dwall_panel(group_id, data)
            panels += 1

    preview_path = source_path.with_suffix(".backfill-preview.json")
    preview_path.write_text(json.dumps(preview, indent=2), encoding="utf-8")

    verb = "Inserted" if apply else "Would insert"
    print(f"\n{verb}: {logs} daily_logs, {panels} dwall_panels")
    if skipped_dupe or skipped_panel:
        print(f"Skipped: {skipped_dupe} duplicate logs, {skipped_panel} existing panels")
    print(f"Preview written to {preview_path.name}")
    if not apply:
        print("\nNothing was written. Re-run with --apply to commit.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("export", nargs="?", type=Path,
                        help="WhatsApp chat export .txt (export WITH media)")
    parser.add_argument("--from-history", type=Path, metavar="FILE",
                        help="history.json from history-harvester/ instead of an export")
    parser.add_argument("--group-id", help='group JID, e.g. "1203...@g.us"')
    parser.add_argument("--from", dest="start", type=_parse_date, help="first date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end", type=_parse_date, help="last date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="write to Supabase (default: dry run)")
    parser.add_argument("--date-order", choices=["DMY", "MDY"], help="override autodetection")
    parser.add_argument("--overwrite-dwall", action="store_true",
                        help="replace panels that already exist")
    parser.add_argument("--logs-only", action="store_true",
                        help="skip dwall entries — use on groups that aren't D-Wall "
                             "groups, where a stray 'dwall' classification is a "
                             "misread site log and upserts junk keyed on panel_number")
    parser.add_argument("--list-groups", action="store_true",
                        help="print known group_ids and exit")
    args = parser.parse_args()

    if args.list_groups:
        for row in db.db.table("groups").select("group_id, group_name").execute().data:
            print(f"  {row['group_id']}\t{row.get('group_name') or ''}")
        return 0

    if args.export and args.from_history:
        parser.error("give either an export file or --from-history, not both")
    source = args.from_history or args.export

    missing = [n for n, v in
               (("export or --from-history", source), ("--group-id", args.group_id),
                ("--from", args.start), ("--to", args.end)) if not v]
    if missing:
        parser.error(f"missing required argument(s): {', '.join(missing)}")
    if not source.exists():
        parser.error(f"no such file: {source}")
    if args.start > args.end:
        parser.error("--from must be on or before --to")

    return run(source, args.group_id, args.start, args.end,
               args.apply, args.date_order, args.overwrite_dwall,
               from_history=bool(args.from_history), logs_only=args.logs_only)


if __name__ == "__main__":
    raise SystemExit(main())
