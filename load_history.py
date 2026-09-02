"""Backfill daily_logs from the full-history spreadsheet.

The sheet already carries the target schema (Day, Date, Main Location, Sub
Location, Description / Activity, Manpower), so nothing is re-parsed here: this
is a load, not an extraction. Rows are inserted only where the group has no
matching row already, so it can be re-run without doubling anything up.

Dry run by default; pass --commit to write.

    python load_history.py Site_Report_Full_05Jan2022-01Sep2026.xlsx
    python load_history.py Site_Report_Full_05Jan2022-01Sep2026.xlsx --commit
"""
import os
import sys
import re
from datetime import datetime, timedelta, timezone

import openpyxl
import psycopg

GROUP_ID = "120363021760406818@g.us"
SHEET = "All Logs"

# Columns as they appear in the sheet.
C_DATE, C_MAIN, C_SUB, C_DESC, C_MANPOWER = 1, 2, 3, 4, 5


def norm(v: str | None) -> str:
    """Collapse a cell to its comparable form.

    Whitespace differences are not real differences here — the sheet was built
    from the same messages the live parser saw, and one of them may have kept a
    trailing space or a wrapped newline. Case is preserved: location codes like
    "B2-36" vs "b2-36" are worth keeping distinct if they ever occur.
    """
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def key(log_date, main, sub, desc) -> tuple:
    return (log_date, norm(main), norm(sub), norm(desc))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    commit = "--commit" in sys.argv

    wb = openpyxl.load_workbook(path, read_only=True)
    rows = list(wb[SHEET].iter_rows(values_only=True))[1:]
    print(f"sheet rows: {len(rows)}")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        existing = {
            key(r[0], r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT log_date, main_location, sub_location, description "
                "FROM daily_logs WHERE group_id = %s",
                (GROUP_ID,),
            )
        }
        print(f"existing rows for this group: {len(existing)}")

        seen = set(existing)
        to_insert = []
        dup_db = dup_file = skipped = 0
        # Preserve the sheet's within-day ordering: logged_at is unknown for a
        # backfill, so derive it from log_date plus the row's position in that
        # day. That keeps /excel2's "ORDER BY log_date, logged_at" stable and
        # sorts backfilled rows ahead of same-day live rows, which is honest —
        # the sheet is the older record.
        per_day: dict = {}

        for r in rows:
            raw_date = r[C_DATE]
            if not raw_date:
                skipped += 1
                continue
            try:
                log_date = datetime.strptime(str(raw_date), "%d %b %Y").date()
            except ValueError:
                skipped += 1
                continue

            main_loc = norm(r[C_MAIN])
            if not main_loc:
                # main_location is NOT NULL, and a row with no location and no
                # description carries nothing. Mirror the parser's convention.
                main_loc = "Unknown"

            k = key(log_date, main_loc, r[C_SUB], r[C_DESC])
            if k in existing:
                dup_db += 1
                continue
            if k in seen:
                dup_file += 1
                continue
            seen.add(k)

            n = per_day.get(log_date, 0)
            per_day[log_date] = n + 1
            logged_at = datetime(
                log_date.year, log_date.month, log_date.day, tzinfo=timezone.utc
            ) + timedelta(seconds=n)

            to_insert.append(
                (
                    GROUP_ID,
                    log_date,
                    logged_at,
                    "",                      # sender_name — unknown for a backfill
                    "",                      # sender_number
                    main_loc,
                    norm(r[C_SUB]),
                    norm(r[C_DESC]),
                    norm(r[C_MANPOWER]),
                    f"[backfill] {os.path.basename(path)}",
                )
            )

        print(f"  already in db  : {dup_db}")
        print(f"  dup within file: {dup_file}")
        print(f"  unusable rows  : {skipped}")
        print(f"  to insert      : {len(to_insert)}")

        if not commit:
            print("\nDRY RUN — nothing written. Re-run with --commit.")
            return 0

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO daily_logs
                    (group_id, log_date, logged_at, sender_name, sender_number,
                     main_location, sub_location, description, manpower, raw_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                to_insert,
            )
        total = conn.execute(
            "SELECT count(*) FROM daily_logs WHERE group_id = %s", (GROUP_ID,)
        ).fetchone()[0]
        print(f"\ninserted {len(to_insert)}; group now holds {total} rows")
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
