"""Excel export generation — verify it produces valid, openable workbooks."""
import io
from datetime import date

from openpyxl import load_workbook

import excel_generator as xls


class TestWeekRanges:
    def test_weeks_cover_the_whole_month(self):
        weeks = xls._week_ranges(2026, 6)
        first_start, _ = weeks[0]
        _, last_end = weeks[-1]
        # The covering weeks must bracket June 1st and June 30th.
        assert first_start <= date(2026, 6, 1)
        assert last_end >= date(2026, 6, 30)

    def test_each_range_is_a_full_monday_to_sunday_week(self):
        for start, end in xls._week_ranges(2026, 6):
            assert start.weekday() == 0   # Monday
            assert (end - start).days == 6


class TestFormatDowntime:
    def test_empty_downtime_is_blank(self):
        assert xls._format_downtime([]) == ""
        assert xls._format_downtime(None) == ""

    def test_structured_downtime_is_rendered(self):
        out = xls._format_downtime(
            [{"date": "21/02/26", "start": "14:00hrs", "end": "15:30hrs",
              "reason": "Equipment fault"}]
        )
        assert "21/02/26" in out
        assert "Equipment fault" in out


class TestMonthlyExcel:
    def test_produces_openable_workbook(self):
        logs = [{
            "main_location": "Zone1", "sub_location": "GL-A",
            "description": "Rebar", "manpower": "2",
            "log_date": date(2026, 6, 15).isoformat(),
        }]
        data = xls.generate_monthly_excel("g1", 2026, 6, logs, ["Zone1"])
        assert isinstance(data, bytes) and len(data) > 0
        wb = load_workbook(io.BytesIO(data))
        assert len(wb.sheetnames) >= 1  # one sheet per week


class TestDwallExcel:
    def test_produces_workbook_with_panel_tracker_sheet(self):
        panels = [{"panel_number": "CN284A", "entry_number": "Ent-2",
                   "downtime": []}]
        data = xls.generate_dwall_excel(panels)
        wb = load_workbook(io.BytesIO(data))
        assert wb.active.title == "Panel Tracker"
