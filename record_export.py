"""The whole record as one workbook, emailed after every 6-hourly batch.

For LTA staff who are not in the WhatsApp groups and use Copilot rather than
the dashboard: a Power Automate flow on the receiving mailbox saves the
attachment into OneDrive, replacing the last one, and people open that file or
point Copilot at it.

Two choices follow from that:

  - The filename and the subject prefix never change. The flow overwrites by
    name and filters on the subject, so a date in either would leave a pile of
    files and break the filter.
  - Each table is a real Excel table (Insert > Table), with real dates and
    numbers. Copilot in Excel only works on formatted tables, and text that
    looks like a date cannot be filtered or summed.

The columns are the dashboard's (dashboard.TABLES): the same view, the same
scope to CR106 and the tunnel groups, and no phone numbers or raw WhatsApp text.
"""

import io
import logging
import re
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from psycopg import sql

import database as db
import mailer
from config import settings
from sitetime import SITE_TZ, site_now

logger = logging.getLogger("site_bot")

FILENAME = "Site_Record.xlsx"
SUBJECT_PREFIX = "[Site Record]"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Excel's own limit is 1,048,576 rows; this is a guard against a runaway query,
# far above the ~42,000 site logs today.
_MAX_ROWS = 500_000

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

# Sheet name, Excel table name. Table names may not contain spaces.
_SHEETS = {
    "daily_logs": ("Site logs", "SiteLogs"),
    "dwall_panels": ("D-wall panels", "DwallPanels"),
    "tunnel_progress": ("Tunnel progress", "TunnelProgress"),
    "tunnel_flags": ("Flagged items", "FlaggedItems"),
}


def _header(column: str) -> str:
    # The dashboard's column labels, so the file and the page read the same.
    return {
        "log_date": "Date", "logged_at": "Logged", "main_location": "Location",
        "sub_location": "Sub-location", "sender_name": "Sent by", "sent_at": "Sent",
        "sent_date": "Date", "report_date": "Report date", "pct_complete": "% complete",
        "tbm_location": "TBM location", "flag_text": "Flagged item",
        "overbreak_pct": "Overbreak %", "created_at": "Recorded",
    }.get(column, column.replace("_", " ").capitalize())


def _cell(value):
    """A value Excel can filter and sort: dates as dates, site-time stamps as
    datetimes, JSON as text, and no control characters (openpyxl refuses them)."""
    if isinstance(value, str):
        if _DATE.match(value):
            return date.fromisoformat(value)
        if _DATETIME.match(value):
            try:
                stamp = datetime.fromisoformat(value)
            except ValueError:
                return value
            if stamp.tzinfo:
                stamp = stamp.astimezone(SITE_TZ).replace(tzinfo=None)
            return stamp
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    if isinstance(value, (list, dict)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return value


def build_workbook(slot: datetime | None = None) -> tuple[bytes, dict[str, int]]:
    """(the .xlsx, rows per sheet)."""
    from dashboard import TABLES, build_query

    slot = slot or site_now()
    wb = Workbook()
    about = wb.active
    about.title = "About"
    counts: dict[str, int] = {}

    for name, (sheet_name, table_name) in _SHEETS.items():
        cols = TABLES[name]["columns"]
        select, _, params = build_query(name)
        rows = db._fetch(sql.SQL("{} LIMIT %s").format(select),
                         tuple(params + [_MAX_ROWS]))
        counts[sheet_name] = len(rows)

        ws = wb.create_sheet(sheet_name)
        ws.append([_header(c) for c in cols])
        for row in rows:
            ws.append([_cell(row.get(c)) for c in cols])

        for i, col in enumerate(cols, start=1):
            letter = get_column_letter(i)
            sample = [str(r.get(col) or "") for r in rows[:300]]
            ws.column_dimensions[letter].width = min(
                max([len(_header(col))] + [len(s) for s in sample]) + 2, 60)
            for cell in ws[letter][1:]:
                if isinstance(cell.value, datetime):
                    cell.number_format = "d mmm yyyy h:mm AM/PM"
                elif isinstance(cell.value, date):
                    cell.number_format = "d mmm yyyy"
        ws.freeze_panes = "A2"

        # An Excel table needs at least one data row under its header.
        if rows:
            table = Table(displayName=table_name,
                          ref=f"A1:{get_column_letter(len(cols))}{len(rows) + 1}")
            table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2",
                                                  showRowStripes=True)
            ws.add_table(table)

    about.append(["Site Record"])
    about["A1"].font = Font(bold=True, size=14)
    about.append([])
    about.append(["Updated", slot.astimezone(SITE_TZ).replace(tzinfo=None)])
    about["B3"].number_format = "d mmm yyyy h:mm AM/PM"
    about.append(["Source", "The WhatsApp site bot — CR106 site group and the tunnel groups"])
    about.append(["Refresh", "Rebuilt after every batch: 12am, 6am, 12pm and 6pm (Singapore). "
                             "This file is replaced each time — edits to it will be lost."])
    about.append([])
    about.append(["Sheet", "Rows"])
    about["A7"].font = about["B7"].font = Font(bold=True)
    for sheet_name, n in counts.items():
        about.append([sheet_name, n])
    about.column_dimensions["A"].width = 18
    about.column_dimensions["B"].width = 90

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), counts


def send_record(slot: datetime | None = None, recipients: list[str] | None = None) -> int:
    """Build the workbook and email it. Blocking. Returns its size in bytes."""
    recipients = recipients or settings.record_email_to
    slot = slot or site_now()
    data, counts = build_workbook(slot)
    when = slot.astimezone(SITE_TZ)
    stamp = f"{when.day} {when:%b %Y}, {when:%I:%M %p}".replace(" 0", " ")
    summary = ", ".join(f"{n:,} {name.lower()}" for name, n in counts.items())

    mailer.send_email(
        recipients,
        f"{SUBJECT_PREFIX} Updated {stamp}",
        f"The site record as of {stamp} (Singapore time) is attached as {FILENAME}: "
        f"{summary}.\n\n"
        "This email comes after every 6-hourly batch. A Power Automate flow saves "
        "the attachment to OneDrive, replacing the previous copy.\n",
        attachments=[(FILENAME, data, XLSX)],
    )
    logger.info("record workbook: %d bytes to %d recipient(s) — %s",
                len(data), len(recipients), summary)
    return len(data)
