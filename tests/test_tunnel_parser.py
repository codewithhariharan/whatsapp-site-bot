"""Tests for the deterministic half of tunnel parsing.

The numeric extraction is a model call and is stubbed out; everything tested
here is pure Python and must be exactly right, because it decides which day a
row overwrites and whether a critical flag is recorded at all.
"""
from datetime import date

import pytest

import tunnel_parser as tp


SAMPLE = """P103 Tunnel Update
15 Aug 2026

‼️ CHANGING TBM MACHINE ‼️

LDTBM Main Drive
· Mined: P1203 to P1209
· Ring Built: P1203 to P1209
· First stage concrete: 0/731 / 1759 rings casting completed.

TBM Progress:
· Day Shift: 3/1205/1759
· Night Shift: 4/1209/1759
· % Completion: 68.73%

· TBM is underneath Punggol Central Road.

· Instrumentation: All within AL

Delays:
Soil Disposal
DS – 102 loads (TSSG); NS - 0 load (TSSG)
· Delay LTBM#1: No
· Total Disposed: 102 loads /approx. 5.2 rings
· Total Rings Excavated: 7 rings
· Delta Disposal: -1.8 ring
· On site Storage: 7.6/62 Full (Rings)
· Earthwork subcon is KTC

Thank you."""


@pytest.fixture
def no_model(monkeypatch):
    """Stub the numeric extraction — these tests are about the text splitting."""
    monkeypatch.setattr(tp, "extract_numbers", lambda known: {})


# ── Contract ──────────────────────────────────────────────────────────────────

def test_contract_is_the_code_not_the_boilerplate():
    assert tp.extract_contract(SAMPLE) == ("P103", "P103 Tunnel Update")


def test_contract_survives_a_differently_worded_title():
    code, line = tp.extract_contract("C1234 Daily Tunnelling Report\n1 Sep 2026\n")
    assert code == "C1234"
    assert line == "C1234 Daily Tunnelling Report"


def test_no_contract_means_not_an_update():
    assert tp.extract_contract("morning all, any update?") == (None, None)


# ── Sections ──────────────────────────────────────────────────────────────────

def test_sections_match_the_reporting_spreadsheet():
    known, other, drive = tp.split_sections(SAMPLE)

    assert drive == "LDTBM Main Drive"
    assert other == {}
    assert set(known) == {"main_drive", "tbm_progress", "delays"}

    assert known["main_drive"].startswith("· Mined: P1203 to P1209")
    assert "1759 rings casting completed." in known["main_drive"]

    assert "Day Shift: 3/1205/1759" in known["tbm_progress"]
    assert "Instrumentation: All within AL" in known["tbm_progress"]

    # "Soil Disposal" has no colon, so it stays inside the Delays block rather
    # than opening a section of its own — matching the spreadsheet's cell.
    assert known["delays"].startswith("Soil Disposal")
    assert "Earthwork subcon is KTC" in known["delays"]


def test_closing_courtesy_is_not_part_of_the_last_section():
    known, _, _ = tp.split_sections(SAMPLE)
    assert "Thank you" not in known["delays"]


def test_the_header_spelling_ldtmb_is_accepted_too():
    # The spreadsheet's own header transposes it; engineers do the same.
    _, _, drive = tp.split_sections("P1 Update\nLDTMB Main Drive\n· Mined: A to B\n")
    assert drive == "LDTMB Main Drive"


def test_an_unknown_section_is_kept_rather_than_dropped():
    text = (
        "P103 Tunnel Update\n1 Sep 2026\n\n"
        "TBM Progress:\n· Day Shift: 1/2/3\n\n"
        "Safety:\n· Toolbox meeting held\n"
    )
    known, other, _ = tp.split_sections(text)
    assert "tbm_progress" in known
    assert other == {"Safety": "· Toolbox meeting held"}


# ── Exclamation / critical ────────────────────────────────────────────────────

def test_paired_markers_yield_the_inner_text_only():
    assert tp.extract_exclamations(SAMPLE) == ["CHANGING TBM MACHINE"]


def test_a_single_marker_still_counts_as_critical():
    # A typo in the template must not silently drop the flag — the director's
    # question reads this column and nothing else.
    assert tp.extract_exclamations("‼ TBM BREAKDOWN") == ["TBM BREAKDOWN"]
    assert tp.extract_exclamations("TBM BREAKDOWN ‼") == ["TBM BREAKDOWN"]


def test_other_warning_glyphs_are_accepted():
    assert tp.extract_exclamations("❗ CABLE STRIKE ❗") == ["CABLE STRIKE"]
    assert tp.extract_exclamations("⚠️ WATER INGRESS") == ["WATER INGRESS"]


def test_every_flagged_line_is_kept(no_model):
    text = (
        "P103 Tunnel Update\n1 Sep 2026\n"
        "‼️ CHANGING TBM MACHINE ‼️\n"
        "‼️ CABLE STRIKE AT P1210 ‼️\n"
        "Delays:\n· none\n"
    )
    row = tp.parse_tunnel_update(text, date(2026, 9, 1))
    assert row["exclamation"] == "CHANGING TBM MACHINE\nCABLE STRIKE AT P1210"


def test_no_marker_means_no_critical_entry(no_model):
    text = "P103 Tunnel Update\n1 Sep 2026\nDelays:\n· none\n"
    assert tp.parse_tunnel_update(text, date(2026, 9, 1))["exclamation"] is None


# ── Dates ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    ("15 Aug 2026", date(2026, 8, 15)),
    ("15 August 2026", date(2026, 8, 15)),
    ("15/08/2026", date(2026, 8, 15)),
    ("15/08/26", date(2026, 8, 15)),
    ("15-Aug-2026", date(2026, 8, 15)),
    ("2026-08-15", date(2026, 8, 15)),
])
def test_date_line_formats(line, expected):
    assert tp.parse_date_line(line) == expected


def test_a_prose_line_is_not_a_date():
    assert tp.parse_date_line("· Day Shift: 3/1205/1759") is None
    assert tp.parse_date_line("LDTBM Main Drive") is None


def test_the_written_date_wins_over_the_arrival_date(no_model):
    # The sample was sent in September but reports on 15 August. Filing it
    # under the arrival date would overwrite the wrong day's row.
    row = tp.parse_tunnel_update(SAMPLE, date(2026, 9, 6))
    assert row["update_date"] == date(2026, 8, 15)
    assert row["date_was_stated"] is True


def test_arrival_date_is_used_only_when_none_is_written(no_model):
    text = "P103 Tunnel Update\n\nDelays:\n· nil\n"
    row = tp.parse_tunnel_update(text, date(2026, 9, 6))
    assert row["update_date"] == date(2026, 9, 6)
    assert row["date_was_stated"] is False


# ── Recognition ───────────────────────────────────────────────────────────────

def test_the_sample_is_recognised():
    assert tp.is_tunnel_update(SAMPLE) is True


@pytest.mark.parametrize("chat", [
    "noted sir",
    "ok thanks",
    "Anyone reaching site by 8?",
    "P103 team, please call me",          # contract code but no section header
    "TBM Progress: good",                 # header inline, no contract, not a section line
])
def test_ordinary_chat_is_not_an_update(chat):
    assert tp.is_tunnel_update(chat) is False


def test_recognition_makes_no_model_call(monkeypatch):
    # is_tunnel_update runs on every message in the group; a model call here
    # would bill for every "noted".
    def explode(*_a, **_k):
        raise AssertionError("is_tunnel_update must not call the model")

    monkeypatch.setattr(tp.client.messages, "create", explode)
    assert tp.is_tunnel_update(SAMPLE) is True
    assert tp.is_tunnel_update("thanks") is False


# ── Numeric coercion ──────────────────────────────────────────────────────────

def test_numbers_written_as_strings_are_salvaged():
    # Postgres would reject these against a NUMERIC column and lose the whole
    # row, verbatim blocks included.
    clean = tp._coerce_numbers({
        "pct_completion": "68.73%",
        "day_shift_cumulative": "1,205",
        "disposed_rings_equiv": "approx. 5.2 rings",
        "delta_disposal": "-1.8 ring",
        "earthwork_subcon": " KTC ",
    })
    assert clean["pct_completion"] == 68.73
    assert clean["day_shift_cumulative"] == 1205
    assert clean["disposed_rings_equiv"] == 5.2
    assert clean["delta_disposal"] == -1.8
    assert clean["earthwork_subcon"] == "KTC"


def test_unusable_and_absent_values_are_dropped_not_zeroed():
    clean = tp._coerce_numbers({
        "pct_completion": None,
        "ds_loads": "",
        "ns_loads": "n/a",
        "rings_total": 1759,
    })
    assert "pct_completion" not in clean
    assert "ds_loads" not in clean
    assert "ns_loads" not in clean          # a wrong 0 would corrupt a total
    assert clean["rings_total"] == 1759


def test_a_failed_extraction_still_stores_the_text(monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("API down")

    monkeypatch.setattr(tp.client.messages, "create", explode)
    row = tp.parse_tunnel_update(SAMPLE, date(2026, 9, 6))
    assert row is not None
    assert row["contract"] == "P103"
    assert "Day Shift: 3/1205/1759" in row["tbm_progress"]
    assert row["exclamation"] == "CHANGING TBM MACHINE"
    assert "pct_completion" not in row


def test_parse_returns_none_for_chat(no_model):
    assert tp.parse_tunnel_update("ok noted", date(2026, 9, 6)) is None
