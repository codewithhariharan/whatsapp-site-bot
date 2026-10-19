"""The October 2026 template (SHIFT, follow-on works, soil disposal, storage)
and the /tunnel email built from it.

The samples are from Tunnel_Progress_Test_Data.docx, the format the user set.
"""
import asyncio
from datetime import date

import pytest

import database as db
import ingest_batch as ib
import mailer
import tunnel_handler as th
import tunnel_parser as tp
import tunnel_summary as ts
from config import settings

P103_DAY = """*TUNNEL PROGRESS UPDATE*
*CONTRACT*: P103
*DATE*: 15-Aug-2026
*SHIFT*: Day
*DRIVE*: LDTBM Main Drive
*PROGRESS*: 3 / 1205 / 1759 (68.5%)
- Follow-on works: First stage concrete 0 / 731 / 1759 rings
- Soil disposal: 102 loads (TSSG), approx. 5.2 rings vs 3 rings excavated, delta +2.2 rings
- On site storage: 3.6 / 62 rings
*TBM LOCATION*: Underneath Punggol Central Road
*INSTRUMENTATION*: All within AL
*ISSUES*: [None]"""

P103_NIGHT = """*TUNNEL PROGRESS UPDATE*
*CONTRACT*: P103
*DATE*: 15-Aug-2026
*SHIFT*: Night
*DRIVE*: LDTBM Main Drive
*PROGRESS*: 4 / 1209 / 1759 (68.7%)
- Follow-on works: First stage concrete 0 / 731 / 1759 rings
- Soil disposal: 0 loads (TSSG) vs 4 rings excavated. Total for the date: 102 loads (approx. 5.2 rings) vs 7 rings excavated, delta -1.8 rings
- On site storage: 7.6 / 62 rings
*TBM LOCATION*: Underneath Punggol Central Road
*INSTRUMENTATION*: All within AL
*ISSUES*: [Soil disposal 1.8 rings behind excavation. No TBM delay]"""

CR116_DAY = """*TUNNEL PROGRESS UPDATE*
*CONTRACT*: CR116
*DATE*: 15-Aug-2026
*SHIFT*: Day
*DRIVE*: EB - 3rd Tunnel Drive
*PROGRESS*: 0 / 133 / 316 (42.1%)
- Follow-on works: First stage concrete, #1 Tunnel EB (towards N109A), P35 to P90 cast
- Soil disposal: Nil (TBM stationary)
- On site storage: Nil
*TBM LOCATION*: AMK Ave 3 (EB) slow lane, near AMK Hub PUDO exit
*INSTRUMENTATION*: All within AL
*ISSUES*: [Ground improvement drilling locations being finalised]"""


def _row(text):
    row = tp.parse_progress_update(text)
    row.pop("raw_message")
    return {**row, "report_date": row["report_date"].isoformat()}


# ── Parsing the new template ──────────────────────────────────────────────────

def test_the_new_lines_are_read():
    row = tp.parse_progress_update(P103_DAY)
    assert row["shift"] == "Day"
    assert (row["rings_built"], row["current_ring"], row["total_rings"],
            row["pct_complete"]) == (3, 1205, 1759, 68.5)
    assert row["follow_on_works"] == "First stage concrete 0 / 731 / 1759 rings"
    assert row["soil_disposal"].startswith("102 loads (TSSG)")
    assert row["on_site_storage"] == "3.6 / 62 rings"
    assert row["issues"] is None


def test_total_for_the_date_is_not_a_date_label():
    # "Total for the date:" sits inside the soil disposal line.
    row = tp.parse_progress_update(P103_NIGHT)
    assert row["report_date"] == date(2026, 8, 15)
    assert row["soil_disposal"].endswith("vs 7 rings excavated, delta -1.8 rings")
    assert row["issues"] == "Soil disposal 1.8 rings behind excavation. No TBM delay"


def test_nil_is_kept_as_written():
    row = tp.parse_progress_update(CR116_DAY)
    assert row["soil_disposal"] == "Nil (TBM stationary)"
    assert row["on_site_storage"] == "Nil"


def test_the_new_template_run_together_on_one_line_still_reads():
    assert _row(P103_NIGHT.replace("\n", "")) == _row(P103_NIGHT)


@pytest.mark.parametrize("written, expected", [
    ("Day", "Day"), ("day shift", "Day"), ("D", "Day"), ("*Night*", "Night"),
    ("NIGHT", "Night"), ("N/S", "Night"), ("", ""),
])
def test_shift_spellings(written, expected):
    assert tp.normalise_shift(written) == expected


def test_the_older_template_has_no_shift():
    old = P103_DAY.replace("*SHIFT*: Day\n", "")
    row = tp.parse_progress_update(old)
    assert row["shift"] == ""
    assert row["current_ring"] == 1205


# ── The email ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [P103_DAY, P103_NIGHT, CR116_DAY])
def test_each_update_is_written_back_in_the_template(text):
    assert ts.format_update(_row(text)) == text


def test_overview_adds_the_shifts_and_ends_on_the_night():
    [line] = ts.overview([_row(P103_DAY), _row(P103_NIGHT)])
    assert line["rings"] == 7
    assert line["shifts"] == ["Day", "Night"]
    assert (line["current_ring"], line["pct_complete"]) == (1209, 68.7)


def test_the_email_carries_figures_highlights_flags_and_updates():
    rows = [_row(P103_DAY), _row(P103_NIGHT), _row(CR116_DAY)]
    flags = [{"contract": "CR116", "flag_text": "Trial trench <P139> shoring"}]
    subject, text, page = ts.build_email(
        date(2026, 8, 15), rows, flags, "P103\n- 7 rings", "All contracts")
    assert subject == "Tunnel Progress Summary – All contracts – 15 Aug 2026"
    assert "3 updates from 2 contracts" in text
    assert "- P103 LDTBM Main Drive: 7 rings [Day + Night], at ring 1209 / 1759 (68.7%)" in text
    assert "P103\n- 7 rings" in text
    assert "CR116: Trial trench <P139> shoring" in text
    assert P103_NIGHT in text
    assert "&lt;P139&gt;" in page and "<P139>" not in page
    assert page.count("<pre") == 3


def test_without_highlights_the_email_still_goes_and_says_so():
    _, text, _ = ts.build_email(date(2026, 8, 15), [_row(P103_DAY)], [], None, "P103")
    assert "Highlights unavailable" in text


# ── /tunnel ───────────────────────────────────────────────────────────────────

TUNNEL_GROUP = "120363111111111111@g.us"
MASTER_GROUP = "120363999999999999@g.us"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", TUNNEL_GROUP)
    monkeypatch.setattr(settings, "MASTER_GROUP_IDS", MASTER_GROUP)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "TUNNEL_EMAIL_TO", "director@example.com")
    w = {"sent": [], "emails": [], "days": [], "caught_up": []}

    async def send(group_id, text):
        w["sent"].append(text)

    async def catch_up(group_ids):
        w["caught_up"].append(sorted(group_ids))

    def progress_for_day(day, group_id=None):
        w["days"].append((day, group_id))
        return [_row(P103_DAY), _row(P103_NIGHT)]

    monkeypatch.setattr(th, "send_message", send)
    monkeypatch.setattr(ib, "catch_up", catch_up)
    monkeypatch.setattr(th, "site_today", lambda: date(2026, 8, 16))
    monkeypatch.setattr(db, "latest_tunnel_report_date", lambda g=None: date(2026, 8, 15))
    monkeypatch.setattr(db, "get_tunnel_progress_for_day", progress_for_day)
    monkeypatch.setattr(db, "get_flags_for_report", lambda d, g=None: [])
    monkeypatch.setattr(ts, "highlights", lambda *a: "P103\n- 7 rings, 68.7%")
    monkeypatch.setattr(mailer, "send_email",
                        lambda to, subject, text, html=None: w["emails"].append((to, subject)))
    return w


def run(text, group_id=MASTER_GROUP):
    handler = th.handle_master_message if group_id == MASTER_GROUP else th.handle_tunnel_message
    asyncio.run(handler(group_id, "H", "65", text))


@pytest.mark.parametrize("command", ["/tunnel", "/tunnel updates", "/Tunnel update"])
def test_master_emails_the_latest_day_for_every_contract(world, command):
    run(command)
    assert world["days"] == [(date(2026, 8, 15), None)]
    assert world["caught_up"] == [[TUNNEL_GROUP]]
    [(to, subject)] = world["emails"]
    assert to == ["director@example.com"]
    assert subject == "Tunnel Progress Summary – All contracts – 15 Aug 2026"
    assert "emailed to d…@example.com" in world["sent"][-1]


@pytest.mark.parametrize("command, day", [
    ("/tunnel yesterday", date(2026, 8, 15)),
    ("/tunnel updates for 14 Aug", date(2026, 8, 14)),
])
def test_another_day_can_be_asked_for(world, command, day):
    run(command)
    assert world["days"] == [(day, None)]


def test_a_contract_group_emails_only_its_own(world):
    run("/tunnel", group_id=TUNNEL_GROUP)
    assert world["days"] == [(date(2026, 8, 15), TUNNEL_GROUP)]
    assert world["emails"][0][1] == "Tunnel Progress Summary – P103 – 15 Aug 2026"


def test_a_model_failure_still_sends_the_figures(world, monkeypatch):
    def broken(*a):
        raise RuntimeError("vertex down")
    monkeypatch.setattr(ts, "highlights", broken)
    run("/tunnel")
    assert len(world["emails"]) == 1


def test_a_send_failure_is_reported(world, monkeypatch):
    def broken(*a, **k):
        raise OSError("smtp refused")
    monkeypatch.setattr(mailer, "send_email", broken)
    run("/tunnel")
    assert "didn't send" in world["sent"][-1]


def test_unconfigured_email_says_so(world, monkeypatch):
    monkeypatch.setattr(settings, "TUNNEL_EMAIL_TO", "")
    run("/tunnel")
    assert world["emails"] == [] and "isn't set up" in world["sent"][0]


def test_a_login_without_a_password_is_not_set_up(world, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", "hariharannvenugopal@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    run("/tunnel")
    assert world["emails"] == [] and "isn't set up" in world["sent"][0]


def test_an_unreadable_day_is_explained(world):
    run("/tunnel next week")
    assert world["emails"] == [] and "couldn't read that day" in world["sent"][0]


def test_excel_is_still_the_spreadsheet(world, monkeypatch):
    exported = []

    async def export(group_id, all_contracts=False):
        exported.append(all_contracts)

    monkeypatch.setattr(th, "handle_tunnel_export", export)
    run("/excel")
    assert exported == [True] and world["emails"] == []
