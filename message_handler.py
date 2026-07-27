from datetime import date
import database as db
from message_parser import classify_and_parse
from whatsapp_client import send_message
import commands as cmd


async def handle_message(group_id: str, sender_name: str, sender_number: str, text: str):
    """Route an incoming message to the correct handler."""

    text = text.strip()
    lower = text.lower()

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

    # Must precede /excel: startswith("/excel") also matches "/excel2", which
    # would otherwise be read as /excel with the argument "2".
    if lower.startswith("/excel2"):
        await cmd.handle_excel2(group_id)
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

    # ── Classify and parse non-command messages ───────────────────────────────
    try:
        parsed = classify_and_parse(text)
    except Exception:
        return  # Silently ignore parse errors for unrelated chat

    msg_type = parsed.get("type")

    if msg_type == "log":
        data = parsed.get("data", {})
        db.upsert_group(group_id)
        db.insert_log(
            group_id=group_id,
            log_date=date.today(),
            sender_name=sender_name,
            sender_number=sender_number,
            main_location=data.get("main_location", "Unknown"),
            sub_location=data.get("sub_location", ""),
            description=data.get("description", ""),
            manpower=data.get("manpower", ""),
            raw_message=text,
        )
        await send_message(group_id, "✅ Logged")

    elif msg_type == "dwall":
        data = parsed.get("data", {})
        db.upsert_group(group_id)
        db.upsert_dwall_panel(group_id, data)
        panel_num = data.get("panel_number", "Panel")
        await send_message(group_id, f"✅ {panel_num} logged in D-Wall tracker.")

    elif msg_type == "query":
        question = parsed.get("query", text)
        await cmd.handle_ask(group_id, question)

    # "ignore" → do nothing
