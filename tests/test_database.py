"""Data-layer coercion — the read side must hand callers JSON-shaped values.

No database is touched: get_engine() builds lazily, so _coerce and _rows can be
exercised on their own.
"""
from datetime import date, datetime
from uuid import UUID

import database as db


class TestCoerce:
    def test_dates_become_iso_strings(self):
        # excel_generator keys its log index on this value and looks it up with
        # date.isoformat(); a date object here empties the whole report.
        assert db._coerce(date(2026, 6, 15)) == "2026-06-15"

    def test_datetimes_become_iso_strings(self):
        # datetime subclasses date, so order of the isinstance checks matters:
        # the date branch would truncate the time component.
        got = db._coerce(datetime(2026, 6, 15, 14, 30))
        assert got == "2026-06-15T14:30:00"

    def test_uuids_become_strings(self):
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert db._coerce(u) == "12345678-1234-5678-1234-567812345678"

    def test_other_values_pass_through_untouched(self):
        for value in ("text", 42, None, ["a"], {"k": "v"}):
            assert db._coerce(value) is value
