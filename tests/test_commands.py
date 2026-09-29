"""Pure report-building logic in commands.py (no DB / network)."""
import asyncio
from datetime import date

import pytest

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


# ── Excel: say something while the file is built ──────────────────────────────

class TestExcelProgress:
    GROUP = "120363021760406818@g.us"

    @pytest.fixture
    def outbox(self, monkeypatch):
        import commands as cmd
        import database as db
        box = []

        async def send(group_id, text):
            box.append(("text", text))

        async def send_doc(group_id, data, filename, caption=""):
            box.append(("file", filename))

        monkeypatch.setattr(cmd, "send_message", send)
        monkeypatch.setattr(cmd, "send_document", send_doc)
        monkeypatch.setattr(db, "get_location_order", lambda g: [])
        return box

    def test_full_record_says_it_is_working_before_the_file(self, outbox, monkeypatch):
        import commands as cmd
        import database as db
        monkeypatch.setattr(db, "get_all_logs", lambda g: [{"log_date": "2026-09-01"}])
        monkeypatch.setattr(cmd.xls, "generate_full_excel", lambda logs: b"x")
        asyncio.run(cmd.handle_excel(self.GROUP, ""))
        assert outbox[0][0] == "text" and "Generating" in outbox[0][1]
        assert outbox[-1][0] == "file"

    def test_month_says_it_is_working_before_the_file(self, outbox, monkeypatch):
        import commands as cmd
        import database as db
        monkeypatch.setattr(db, "get_logs_for_month", lambda g, y, m: [{"log_date": "2026-01-02"}])
        monkeypatch.setattr(cmd.xls, "generate_monthly_excel", lambda *a: b"x")
        asyncio.run(cmd.handle_excel(self.GROUP, "Jan 2026"))
        assert "Generating the January 2026" in outbox[0][1]
        assert outbox[-1] == ("file", "Site_Report_January_2026.xlsx")

    def test_a_failure_is_reported_instead_of_silence(self, outbox, monkeypatch):
        import commands as cmd
        import database as db

        def boom(g):
            raise RuntimeError("db down")
        monkeypatch.setattr(db, "get_all_logs", boom)
        asyncio.run(cmd.handle_excel(self.GROUP, ""))
        assert [kind for kind, _ in outbox] == ["text", "text"]
        assert "Couldn't build" in outbox[-1][1]

    def test_dwall_export_says_it_is_working(self, outbox, monkeypatch):
        import commands as cmd
        import database as db
        monkeypatch.setattr(db, "get_all_panels", lambda g: [{"panel_number": "CN270"}])
        monkeypatch.setattr(cmd.xls, "generate_dwall_excel", lambda p: b"x")
        asyncio.run(cmd.handle_dwall_export(self.GROUP))
        assert "Generating" in outbox[0][1]
        assert outbox[-1] == ("file", "DWall_Panel_Tracker.xlsx")
