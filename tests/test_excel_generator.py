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


class TestFullExcel:
    """The flat, whole-history sheet a bare /excel produces."""

    LOGS = [
        {"main_location": "Zone 1 P4", "sub_location": "", "description": "FBCM",
         "manpower": "", "log_date": date(2022, 1, 5).isoformat()},
        {"main_location": "Zone 2 P7", "sub_location": "NB D/U 04",
         "description": "Strand installation", "manpower": "3",
         "log_date": date(2026, 9, 2).isoformat()},
    ]

    def test_is_one_sheet_not_one_per_week(self):
        # The monthly export pivots locations against dates and emits a sheet
        # per week. That shape does not survive the full range — the history
        # has ~6,100 distinct locations over ~1,500 days — so this export is
        # deliberately flat.
        wb = load_workbook(io.BytesIO(xls.generate_full_excel(self.LOGS)))
        assert wb.sheetnames == ["All Logs"]

    def test_one_row_per_entry_plus_header(self):
        ws = load_workbook(io.BytesIO(xls.generate_full_excel(self.LOGS))).active
        assert ws.max_row == len(self.LOGS) + 1
        assert [c.value for c in ws[1]] == [
            "Day", "Date", "Main Location", "Sub Location",
            "Description / Activity", "Manpower",
        ]

    def test_rows_keep_the_order_they_were_given(self):
        ws = load_workbook(io.BytesIO(xls.generate_full_excel(self.LOGS))).active
        assert ws.cell(row=2, column=2).value == "05 Jan 2022"
        assert ws.cell(row=3, column=2).value == "02 Sep 2026"

    def test_header_is_frozen_and_filtered(self):
        # ~40k rows: an unfrozen header is unusable.
        ws = load_workbook(io.BytesIO(xls.generate_full_excel(self.LOGS))).active
        assert ws.freeze_panes == "A2"
        assert ws.auto_filter.ref == "A1:F3"

    def test_missing_fields_render_blank_never_the_string_none(self):
        # sub_location is absent from ~92% of the history and manpower from all
        # of it, so the common row is mostly empty. `or ""` must absorb both ""
        # and None — a bare str() would write the text "None" into the sheet.
        # openpyxl reads an empty cell back as None, so blank is None here.
        logs = self.LOGS + [{"main_location": "Z", "sub_location": None,
                             "description": None, "manpower": None,
                             "log_date": date(2022, 1, 6).isoformat()}]
        ws = load_workbook(io.BytesIO(xls.generate_full_excel(logs))).active
        for row in (2, 4):
            for col in (4, 5, 6):
                assert ws.cell(row=row, column=col).value != "None"
        assert ws.cell(row=2, column=4).value is None   # sub_location, ""
        assert ws.cell(row=4, column=6).value is None   # manpower, None

    def test_accepts_date_objects_as_well_as_iso_strings(self):
        logs = [dict(self.LOGS[0], log_date=date(2022, 1, 5))]
        ws = load_workbook(io.BytesIO(xls.generate_full_excel(logs))).active
        assert ws.cell(row=2, column=2).value == "05 Jan 2022"

    def test_empty_log_list_still_produces_a_valid_workbook(self):
        ws = load_workbook(io.BytesIO(xls.generate_full_excel([]))).active
        assert ws.max_row == 1
        assert ws.auto_filter.ref == "A1:F1"
