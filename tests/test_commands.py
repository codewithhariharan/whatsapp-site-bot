"""Pure report-building logic in commands.py (no DB / network)."""
from datetime import date

import commands as cmd


class TestResolveLocation:
    def test_exact_match(self):
        assert cmd._resolve_location("Zone1", ["Zone1", "Zone2"]) == "Zone1"

    def test_db_value_is_prefix_of_ordered(self):
        # "CCW" stored in the DB resolves to "CCW1" in the configured order.
        assert cmd._resolve_location("CCW", ["CCW1", "Zone2"]) == "CCW1"

    def test_ordered_value_is_prefix_of_db(self):
        assert cmd._resolve_location("CCW1", ["CCW", "Zone2"]) == "CCW"

    def test_no_match_returns_original(self):
        assert cmd._resolve_location("Basement", ["Zone1", "Zone2"]) == "Basement"


class TestFormatDailyReport:
    REPORT_DATE = date(2026, 6, 20)

    def _logs(self):
        return [
            {"id": 1, "main_location": "Zone2", "sub_location": "GL-A",
             "description": "Concrete pour"},
            {"id": 2, "main_location": "Zone1", "sub_location": "GL-B",
             "description": "Rebar fixing"},
            # Duplicate description in the same group — must be de-duplicated.
            {"id": 3, "main_location": "Zone1", "sub_location": "GL-B",
             "description": "Rebar fixing"},
        ]

    def test_header_has_formatted_date(self):
        report = cmd._format_daily_report(self._logs(), ["Zone1", "Zone2"], self.REPORT_DATE)
        assert "20 June 2026" in report

    def test_follows_configured_location_order(self):
        report = cmd._format_daily_report(self._logs(), ["Zone1", "Zone2"], self.REPORT_DATE)
        assert report.index("Zone1") < report.index("Zone2")

    def test_duplicate_descriptions_collapsed(self):
        report = cmd._format_daily_report(self._logs(), ["Zone1", "Zone2"], self.REPORT_DATE)
        assert report.count("Rebar fixing") == 1

    def test_location_missing_from_order_still_appears(self):
        logs = [{"id": 9, "main_location": "Annex", "sub_location": "",
                 "description": "Survey"}]
        report = cmd._format_daily_report(logs, ["Zone1"], self.REPORT_DATE)
        assert "Annex" in report
        assert "Survey" in report
