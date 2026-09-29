"""The standardised TUNNEL PROGRESS UPDATE, and the "!" flags.

The samples are the ones the contracts were given, typed the way WhatsApp
delivers them: bold asterisks round the labels, non-breaking spaces, and the
small inconsistencies real engineers produce.
"""
from datetime import date

import pytest

import tunnel_parser as tp

CR146 = """*TUNNEL PROGRESS UPDATE*
*CONTRACT*: CR146
*DATE*: 04-JAN-2026
\xa0
*DRIVE*: EB-Main Drive 3
*PROGRESS*: 0 / 225 / 888 (25.9%)
*TBM LOCATION*: at side table of AMK Ave 3
\xa0
*INSTRUMENTATION*: LG3053 breached AL
*ISSUES*: [shift change]"""

CR125 = """*TUNNEL PROGRESS UPDATE*
*CONTRACT*: CR125
*DATE*: 01-Jan-2026 \xa0
*DRIVE*: EB - Initial Drive 1
*PROGRESS*: 2 / 7 / 1200 (0.6%)
*TBM LOCATION*: within site boundary
*INSTRUMENTATION*: All within AL
*ISSUES*: [Grout leak from entrance seal]"""


# ── Recognising and reading an update ─────────────────────────────────────────

def test_every_field_of_the_template_is_read():
    row = tp.parse_progress_update(CR146)
    assert row["contract"] == "CR146"
    assert row["report_date"] == date(2026, 1, 4)
    assert row["drive"] == "EB - Main Drive 3"
    assert (row["rings_built"], row["current_ring"], row["total_rings"]) == (0, 225, 888)
    assert row["pct_complete"] == 25.9          # as written, not recomputed
    assert row["tbm_location"] == "at side table of AMK Ave 3"
    assert row["instrumentation"] == "LG3053 breached AL"
    assert row["issues"] == "shift change"
    assert row["raw_message"] == CR146


def test_the_template_without_bold_marks_reads_the_same():
    plain = CR146.replace("*", "")
    assert tp.parse_progress_update(plain) == {
        **tp.parse_progress_update(CR146), "raw_message": plain}


def test_fields_run_together_on_one_line_still_read():
    # How the sample document delivers it, and how a paste can arrive.
    flat = CR125.replace("\n", "")
    row = tp.parse_progress_update(flat)
    assert row["contract"] == "CR125"
    assert row["tbm_location"] == "within site boundary"
    assert row["issues"] == "Grout leak from entrance seal"


@pytest.mark.parametrize("written", ["EB-Main Drive 3", "EB - Main Drive 3",
                                     "EB  -Main Drive 3", "*EB -Main Drive 3*"])
def test_drive_spellings_are_one_drive(written):
    # The drive is part of the key a resend replaces on.
    assert tp.normalise_drive(written) == "EB - Main Drive 3"


@pytest.mark.parametrize("written, expected", [
    ("01-JAN-2026", date(2026, 1, 1)),
    ("2-Jan-2026", date(2026, 1, 2)),
    ("02-JAN -2026", date(2026, 1, 2)),
    ("01-JAN-2026*", date(2026, 1, 1)),
    ("05-January-2026", date(2026, 1, 5)),
    ("04/01/2026", date(2026, 1, 4)),
    ("2026-01-04", date(2026, 1, 4)),
])
def test_date_spellings(written, expected):
    assert tp.parse_date(written) == expected


@pytest.mark.parametrize("written, expected", [
    ("[None]", None), ("[none]", None), ("None", None), ("[NIL]", None),
    ("[None]v", None), ("[-]", None),
    ("[Gantry crane breakdown]", "Gantry crane breakdown"),
    ("HV cable extension", "HV cable extension"),
])
def test_issues(written, expected):
    assert tp._issues(written) == expected


def test_percentage_is_optional():
    row = tp.parse_progress_update(CR146.replace(" (25.9%)", ""))
    assert row["pct_complete"] is None
    assert row["current_ring"] == 225


def test_contract_is_normalised():
    row = tp.parse_progress_update(CR146.replace("CR146", "cr 146"))
    assert row["contract"] == "CR146"


# ── What is and isn't an update ───────────────────────────────────────────────

@pytest.mark.parametrize("chat", [
    "ok noted", "TBM stopped for maintenance", "Date: tomorrow?",
    "progress is slow today",
])
def test_ordinary_chat_is_not_an_update(chat):
    assert tp.is_progress_update(chat) is False
    assert tp.parse_progress_update(chat) is None


def test_contract_and_progress_labels_without_the_header_still_count():
    body = CR146.replace("*TUNNEL PROGRESS UPDATE*\n", "")
    assert tp.parse_progress_update(body)["contract"] == "CR146"


@pytest.mark.parametrize("breakage, complaint", [
    (("*DATE*: 04-JAN-2026", "*DATE*: sometime"), "DATE not readable"),
    (("*PROGRESS*: 0 / 225 / 888 (25.9%)", "*PROGRESS*: going well"), "PROGRESS"),
    (("*DRIVE*: EB-Main Drive 3\n", ""), "missing DRIVE"),
])
def test_a_broken_update_is_an_error_not_a_skip(breakage, complaint):
    # An update that cannot be filed must reach the failure report, so the
    # engineer resends it; silently skipping it would lose the day.
    with pytest.raises(tp.TunnelParseError, match=complaint):
        tp.parse_progress_update(CR146.replace(*breakage))


def test_a_later_label_inside_the_issues_does_not_overwrite_the_date():
    text = CR146.replace("[shift change]", "[revised date: 10-JAN-2026 for repair]")
    assert tp.parse_progress_update(text)["report_date"] == date(2026, 1, 4)


# ── Flags ─────────────────────────────────────────────────────────────────────

def test_any_line_with_an_exclamation_mark_is_a_flag():
    text = CR146.replace("*ISSUES*: [shift change]", "*ISSUES*: [Gantry crane breakdown!!]")
    assert tp.extract_flags(text) == ["ISSUES: [Gantry crane breakdown]"]


def test_flags_in_a_separate_message_count_too():
    msg = "‼️ TBM stopped – face collapse ‼️\nwill update by 3pm"
    assert tp.extract_flags(msg) == ["TBM stopped – face collapse"]


def test_every_flagged_line_is_kept_in_order():
    msg = "LG3053 breached AL!\nnormal line\n! Night shift cancelled"
    assert tp.extract_flags(msg) == ["LG3053 breached AL", "Night shift cancelled"]


@pytest.mark.parametrize("courtesy", ["Thanks!", "Noted!!", "Good morning!",
                                      "thank you all!", "OK!"])
def test_courtesies_are_not_flags(courtesy):
    assert tp.extract_flags(courtesy) == []


def test_no_exclamation_no_flags():
    assert tp.extract_flags(CR146) == []


def test_the_template_parses_the_same_with_a_flag_in_it():
    text = CR146.replace("LG3053 breached AL", "LG3053 breached AL!!")
    row = tp.parse_progress_update(text)
    assert row["instrumentation"] == "LG3053 breached AL"
