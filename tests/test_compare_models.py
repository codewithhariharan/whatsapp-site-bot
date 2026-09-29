"""The comparison rules that decide whether Gemini is trusted with the record."""
from decimal import Decimal

import compare_models as cm

STORED = {"main_location": "Zone 3", "sub_location": "GL A-B/20",
          "description": "Honeycomb rectification works", "manpower": "Worker - 1"}


def log(**data):
    return {"type": "log", "data": {**STORED, **data}}


class TestLogs:
    def test_identical_parse_agrees(self):
        assert cm.compare_log(STORED, log()) == {"problems": [], "notes": []}

    def test_whitespace_and_case_are_not_differences(self):
        got = cm.compare_log(STORED, log(main_location="  zone 3 ", manpower="worker -  1"))
        assert got["problems"] == []

    def test_changed_location_is_a_problem(self):
        got = cm.compare_log(STORED, log(sub_location=""))
        assert got["problems"] == ["sub_location: 'GL A-B/20' -> ''"]

    def test_falling_back_to_unknown_is_called_out(self):
        got = cm.compare_log(STORED, log(main_location="Unknown"))
        assert any("location lost" in p for p in got["problems"])

    def test_unknown_on_both_sides_is_not_a_loss(self):
        got = cm.compare_log({**STORED, "main_location": "Unknown"},
                             log(main_location="Unknown"))
        assert got["problems"] == []

    def test_reworded_description_is_a_note_not_a_problem(self):
        got = cm.compare_log(STORED, log(description="Fixing concrete defects"))
        assert got["problems"] == []
        assert "reworded" in got["notes"][0]

    def test_a_log_classified_as_chat_is_a_problem(self):
        got = cm.compare_log(STORED, {"type": "ignore"})
        assert got["problems"] == ["classified as 'ignore', Claude filed a log"]


class TestSkipped:
    def test_chat_staying_chat_agrees(self):
        assert cm.compare_skipped({"type": "ignore"})["problems"] == []
        assert cm.compare_skipped({"type": "query", "query": "?"})["problems"] == []

    def test_chat_becoming_a_row_is_a_problem(self):
        assert cm.compare_skipped({"type": "log", "data": {}})["problems"]
        assert cm.compare_skipped({"type": "dwall", "data": {}})["problems"]


class TestTunnel:
    def test_numeric_columns_compare_as_numbers(self):
        stored = {"pct_completion": Decimal("68.73"), "day_shift_cumulative": Decimal("1205")}
        parsed = {"pct_completion": 68.73, "day_shift_cumulative": 1205.0}
        assert cm.compare_tunnel(stored, parsed)["problems"] == []

    def test_a_different_number_is_shown_plainly(self):
        got = cm.compare_tunnel({"ds_loads": Decimal("102")}, {"ds_loads": 12})
        assert got["problems"] == ["ds_loads: 102 -> 12"]

    def test_a_lost_value_is_a_problem_and_a_new_one_a_note(self):
        got = cm.compare_tunnel({"ds_loads": Decimal("102"), "ns_loads": None},
                                {"ns_loads": 0, "tbm_location": "Punggol"})
        assert got["problems"] == ["ds_loads: 102 lost"]
        assert len(got["notes"]) == 2

    def test_text_fields_compare_loosely(self):
        got = cm.compare_tunnel({"earthwork_subcon": "KTC "}, {"earthwork_subcon": "ktc"})
        assert got["problems"] == []

    def test_no_numbers_at_all_reads_as_a_failed_call(self):
        got = cm.compare_tunnel({"ds_loads": Decimal("102")}, {"contract": "P103"})
        assert "model call likely failed" in got["problems"][0]

    def test_not_recognised_is_a_problem(self):
        assert cm.compare_tunnel({}, None)["problems"]


def test_run_one_records_an_error_instead_of_stopping(monkeypatch):
    def boom(_text):
        raise ValueError("bad json")
    monkeypatch.setattr(cm, "classify_and_parse", boom)
    got = cm.run_one("logs", {"id": 1, "raw_message": "x", **STORED})
    assert got["problems"] == ["error: ValueError: bad json"]
