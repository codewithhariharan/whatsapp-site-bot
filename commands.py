from datetime import date
import database as db
import excel_generator as xls
from whatsapp_client import send_message, send_document


# ── /help ─────────────────────────────────────────────────────────────────────

async def handle_help(group_id: str):
    text = (
        "📋 *Site Bot — Commands*\n\n"
        "*/setorder* _Zone1, Zone2, Zone3_\n"
        "Set the fixed location order for daily reports.\n\n"
        "*/daily*\n"
        "Generate today's progress summary.\n\n"
        "*/reorder* _3 1 2 4 5_\n"
        "Reorder the daily summary by position number (after /daily).\n\n"
        "*/delete* _3 5 7_\n"
        "Remove one or more entries from the preview (session only, not database).\n\n"
        "*/confirm*\n"
        "Send the final daily report after reviewing.\n\n"
        "*/excel*\n"
        "Export this month's logs as an Excel file.\n\n"
        "*/excel* _Jan 2026_\n"
        "Export a specific month's logs.\n\n"
        "*/dwall*\n"
        "Export all D-Wall / Barrette panel records as Excel.\n\n"
        "*/ask* _your question_\n"
        "Ask anything about site history.\n"
        "Example: /ask when was Panel 39 cast?\n\n"
        "📌 Engineers: just send your caption normally —\n"
        "Main Location, Sub Location, Description, Manpower.\n"
        "The bot will reply ✅ Logged."
    )
    await send_message(group_id, text)


# ── /setorder ─────────────────────────────────────────────────────────────────

async def handle_setorder(group_id: str, args: str):
    if not args.strip():
        await send_message(group_id, "⚠️ Usage: /setorder Zone1, Zone2, Zone3, ...")
        return

    locations = [loc.strip() for loc in args.split(",") if loc.strip()]
    if not locations:
        await send_message(group_id, "⚠️ No locations found. Separate them with commas.")
        return

    db.set_location_order(group_id, locations)

    order_text = "\n".join(f"{i+1}. {loc}" for i, loc in enumerate(locations))
    await send_message(
        group_id,
        f"✅ Location order saved:\n\n{order_text}\n\n"
        "All /daily reports will follow this order."
    )


# ── /daily ────────────────────────────────────────────────────────────────────

def _resolve_location(loc: str, ordered: list[str]) -> str:
    """Map a log's location to its best match in the ordered list.
    Handles prefix mismatches like 'CCW' stored in DB vs 'CCW1' in setorder.
    """
    if loc in ordered:
        return loc
    for candidate in ordered:
        if candidate.startswith(loc) or loc.startswith(candidate):
            return candidate
    return loc


def _format_daily_report(logs: list[dict], locations: list[str], report_date: date) -> str:
    """Build the formatted daily report string."""
    date_str = f"{report_date.day} {report_date.strftime('%B %Y')}"
    lines = [f"📋 *Project Report ({date_str})*\n"]

    # Group logs by resolved location, mapping e.g. "CCW" → "CCW1"
    by_location: dict[str, list[dict]] = {}
    for log in logs:
        resolved = _resolve_location(log["main_location"], locations)
        by_location.setdefault(resolved, []).append(log)

    # Follow fixed order, then append any locations not in the order
    ordered_locations = list(locations)
    for loc in by_location:
        if loc not in ordered_locations:
            ordered_locations.append(loc)

    for loc in ordered_locations:
        loc_logs = by_location.get(loc)
        if not loc_logs:
            continue

        # Group by sub_location; deduplicate descriptions within each group
        by_sub: dict[str, list[str]] = {}
        seen: dict[str, set[str]] = {}
        for log in loc_logs:
            sub = log.get("sub_location") or ""
            key = f"{loc} › {sub}" if sub else loc
            desc = (log.get("description") or "").strip()
            if key not in by_sub:
                by_sub[key] = []
                seen[key] = set()
            if desc and desc not in seen[key]:
                by_sub[key].append(desc)
                seen[key].add(desc)

        for header, descriptions in by_sub.items():
            lines.append(f"*{header}:*")
            for desc in descriptions:
                lines.append(f"• {desc}")
            lines.append("")

    return "\n".join(lines).strip()


async def handle_daily(group_id: str, for_date: date = None):
    today = for_date or date.today()
    logs = db.get_logs_for_date(group_id, today)
    locations = db.get_location_order(group_id)

    if not logs:
        await send_message(group_id, f"⚠️ No logs found for {today.strftime('%d %b %Y')}.")
        return

    # Save session so /reorder and /confirm can access it
    db.save_reorder_session(group_id, today, logs)

    report = _format_daily_report(logs, locations, today)

    # Show numbered preview for potential reordering
    numbered = "\n".join(
        f"{i+1}. {log['main_location']} › {log.get('sub_location','')}"
        for i, log in enumerate(logs)
    )

    await send_message(
        group_id,
        f"👀 *Preview — {today.strftime('%d %b %Y')}*\n\n"
        f"{report}\n\n"
        "─────────────────\n"
        "Send */confirm* to post this report.\n"
        f"Or */reorder* with new positions (e.g. /reorder 3 1 2) to change order:\n\n"
        f"{numbered}"
    )


# ── /reorder ──────────────────────────────────────────────────────────────────

async def handle_reorder(group_id: str, args: str):
    session = db.get_reorder_session(group_id)
    if not session:
        await send_message(group_id, "⚠️ No active /daily session. Run /daily first.")
        return

    try:
        positions = [int(x.strip()) - 1 for x in args.split()]
    except ValueError:
        await send_message(group_id, "⚠️ Use numbers separated by spaces. E.g. /reorder 3 1 2 4")
        return

    logs = session["ordered_logs"]
    if any(p < 0 or p >= len(logs) for p in positions):
        await send_message(group_id, f"⚠️ Numbers must be between 1 and {len(logs)}.")
        return

    reordered = [logs[p] for p in positions]
    # Append any logs not mentioned
    mentioned_ids = {logs[p]["id"] for p in positions}
    for log in logs:
        if log["id"] not in mentioned_ids:
            reordered.append(log)

    report_date = date.fromisoformat(session["session_date"])
    db.save_reorder_session(group_id, report_date, reordered)
    locations = db.get_location_order(group_id)
    report = _format_daily_report(reordered, locations, report_date)

    await send_message(
        group_id,
        f"🔄 *Updated Preview*\n\n{report}\n\n"
        "Send */confirm* to post, or */reorder* again to adjust."
    )


# ── /confirm ──────────────────────────────────────────────────────────────────

async def handle_confirm(group_id: str):
    session = db.get_reorder_session(group_id)
    if not session:
        await send_message(group_id, "⚠️ No active /daily session. Run /daily first.")
        return

    logs = session["ordered_logs"]
    report_date = date.fromisoformat(session["session_date"])
    locations = db.get_location_order(group_id)
    report = _format_daily_report(logs, locations, report_date)

    db.clear_reorder_session(group_id)
    await send_message(group_id, report)


# ── /excel ────────────────────────────────────────────────────────────────────

async def handle_excel(group_id: str, args: str = ""):
    from datetime import datetime
    import calendar

    today = date.today()
    year, month = today.year, today.month

    if args.strip():
        try:
            parsed = datetime.strptime(args.strip(), "%b %Y")
            year, month = parsed.year, parsed.month
        except ValueError:
            await send_message(group_id, "⚠️ Format: /excel or /excel Jan 2026")
            return

    logs = db.get_logs_for_month(group_id, year, month)
    locations = db.get_location_order(group_id)
    month_name = calendar.month_name[month]

    if not logs:
        await send_message(group_id, f"⚠️ No logs found for {month_name} {year}.")
        return

    file_bytes = xls.generate_monthly_excel(group_id, year, month, logs, locations)
    filename = f"Site_Report_{month_name}_{year}.xlsx"

    await send_document(
        group_id,
        file_bytes,
        filename,
        caption=f"📊 {month_name} {year} — {len(logs)} log entries across {len(locations)} locations.",
    )


# ── /dwall ────────────────────────────────────────────────────────────────────

async def handle_dwall_export(group_id: str):
    panels = db.get_all_panels(group_id)
    if not panels:
        await send_message(group_id, "⚠️ No D-Wall / Barrette panel records found.")
        return

    file_bytes = xls.generate_dwall_excel(panels)
    filename = "DWall_Panel_Tracker.xlsx"

    await send_document(
        group_id,
        file_bytes,
        filename,
        caption=f"🏗️ D-Wall / Barrette Panel Tracker — {len(panels)} panels.",
    )


# ── /delete ───────────────────────────────────────────────────────────────────

async def handle_delete(group_id: str, args: str):
    session = db.get_reorder_session(group_id)
    if not session:
        await send_message(group_id, "⚠️ No active /daily session. Run /daily first.")
        return

    try:
        positions = {int(p) - 1 for p in args.split()}  # 1-based → 0-based, deduplicated
        if not positions:
            raise ValueError
    except ValueError:
        await send_message(group_id, "⚠️ Usage: /delete 3 5 7 — use numbers shown in the preview list.")
        return

    logs = session["ordered_logs"]
    invalid = [p + 1 for p in positions if p < 0 or p >= len(logs)]
    if invalid:
        nums = " ".join(str(n) for n in sorted(invalid))
        await send_message(group_id, f"⚠️ Invalid number(s): {nums}. Must be between 1 and {len(logs)}.")
        return

    deleted = [logs[p] for p in sorted(positions)]
    remaining = [log for i, log in enumerate(logs) if i not in positions]

    report_date = date.fromisoformat(session["session_date"])
    db.save_reorder_session(group_id, report_date, remaining)

    locations = db.get_location_order(group_id)

    removed_lines = "\n".join(
        f"  • {log.get('main_location', '')} › {log.get('sub_location', '')} — {log.get('description', '')}"
        for log in deleted
    )

    if not remaining:
        await send_message(
            group_id,
            f"🗑️ Removed {len(deleted)} entr{'y' if len(deleted) == 1 else 'ies'}:\n{removed_lines}\n\n"
            "⚠️ No entries left. Run /daily again to start over."
        )
        return

    report = _format_daily_report(remaining, locations, report_date)
    numbered = "\n".join(
        f"{i+1}. {log['main_location']} › {log.get('sub_location', '')}"
        for i, log in enumerate(remaining)
    )

    await send_message(
        group_id,
        f"🗑️ Removed {len(deleted)} entr{'y' if len(deleted) == 1 else 'ies'}:\n{removed_lines}\n\n"
        f"*Updated Preview*\n\n{report}\n\n"
        "─────────────────\n"
        "Send */confirm* to post, */delete N* to remove more, or */reorder* to adjust order.\n\n"
        f"{numbered}"
    )


# ── /ask ──────────────────────────────────────────────────────────────────────

async def handle_ask(group_id: str, question: str):
    from ai_handler import answer_query
    if not question.strip():
        await send_message(group_id, "⚠️ Usage: /ask your question here")
        return
    await send_message(group_id, "🔍 Searching...")
    answer = answer_query(group_id, question)
    await send_message(group_id, f"🤖 {answer}")
