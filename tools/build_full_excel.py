#!/usr/bin/env python3
"""Build the full-history Excel workbook without touching the database.

Two sources:

  From a WhatsApp export (no GCP, no deployment, no credentials):
      python tools/build_full_excel.py --chat /path/to/_chat.txt

  From Cloud SQL, i.e. exactly what /excel2 sends (needs .env configured):
      python tools/build_full_excel.py --group '1203630xxxxxxxxxx@g.us'

Output is written next to the input as Site_Report_Full_<first>-<last>.xlsx
unless -o says otherwise.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import excel_generator as xls  # noqa: E402


def rows_from_chat(chat_path: Path) -> list[dict]:
    from extract_chat_history import parse_messages, extract
    return extract(parse_messages(str(chat_path)))


def rows_from_db(group_id: str) -> list[dict]:
    # Imported lazily: this path needs .env and GCP credentials, and the chat
    # path must keep working on a laptop with neither.
    import database as db
    return db.get_all_logs(group_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--chat", type=Path, help="path to _chat.txt from a WhatsApp export")
    src.add_argument("--group", help="group JID; reads from Cloud SQL instead")
    ap.add_argument("-o", "--out", type=Path, help="output .xlsx path")
    args = ap.parse_args()

    if args.chat:
        if not args.chat.exists():
            ap.error(f"no such file: {args.chat}")
        rows = rows_from_chat(args.chat)
    else:
        rows = rows_from_db(args.group)

    if not rows:
        print("No log rows found — nothing to write.", file=sys.stderr)
        return 1

    def as_date(v):
        return date.fromisoformat(v) if isinstance(v, str) else v

    first, last = as_date(rows[0]["log_date"]), as_date(rows[-1]["log_date"])
    out = args.out or Path(
        f"Site_Report_Full_{first:%d%b%Y}-{last:%d%b%Y}.xlsx"
    )
    out.write_bytes(xls.generate_full_excel(rows))
    print(f"{out}  —  {len(rows)} entries, {first:%d %b %Y} to {last:%d %b %Y}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
