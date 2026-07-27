"""Backfill selection and history-loading logic.

No network and no database — these cover the filters that decide what gets an
API call and what reaches Supabase, plus reading history-harvester output.
"""
import json
from datetime import date, datetime

import pytest

from backfill import _sender_number, dedupe_key, load_history, select_messages
from whatsapp_export import ExportedMessage


def _msg(day: int, text: str, sender: str = "Rajesh") -> ExportedMessage:
    return ExportedMessage(datetime(2026, 7, day, 9, 0), sender, text)


WINDOW = (date(2026, 7, 11), date(2026, 7, 26))


def test_keeps_a_normal_log():
    picked = select_messages([_msg(12, "Main Location: Zone 3 rebar works")], *WINDOW)
    assert len(picked) == 1


def test_drops_messages_outside_the_window():
    messages = [_msg(10, "Main Location: Zone 3 works"), _msg(27, "Main Location: Zone 9 works")]
    assert select_messages(messages, *WINDOW) == []


def test_window_bounds_are_inclusive():
    messages = [_msg(11, "Main Location: Zone 1 works"), _msg(26, "Main Location: Zone 2 works")]
    assert len(select_messages(messages, *WINDOW)) == 2


def test_drops_commands():
    # Replaying /excel would regenerate and re-send an export.
    assert select_messages([_msg(12, "/excel july 2026 please")], *WINDOW) == []


@pytest.mark.parametrize("reply", [
    "✅ Logged",
    "🔍 Searching...",
    "🤖 The data shows Panel CN284A was cast on 22/02/26 at Zone 3, S2-2 by DS",
])
def test_drops_the_bots_own_replies(reply):
    # The bot posts from the work phone, so its replies come back in the export.
    # The 🤖 answer is long and quotes real locations — classifying it would
    # invent a log row out of the bot's own summary.
    assert select_messages([_msg(12, reply)], *WINDOW) == []


def test_drops_short_chatter():
    assert select_messages([_msg(12, "ok noted")], *WINDOW) == []


# ── history-harvester output ──────────────────────────────────────────────────

def _write_history(tmp_path, rows):
    path = tmp_path / "history.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_load_history_reads_rows(tmp_path):
    ts = datetime(2026, 7, 13, 14, 30).timestamp()
    path = _write_history(tmp_path, [{
        "timestamp": ts, "group_id": "1203@g.us", "sender_name": "Rajesh",
        "sender_number": "6588257614", "text": "Main Location: Zone 3",
    }])
    messages = load_history(path, "1203@g.us")
    assert len(messages) == 1
    assert messages[0].log_date == date(2026, 7, 13)
    assert messages[0].sender == "Rajesh"
    # The harvester knows real numbers; exports don't.
    assert messages[0].sender_number == "6588257614"


def test_load_history_filters_other_groups(tmp_path):
    ts = datetime(2026, 7, 13, 14, 30).timestamp()
    path = _write_history(tmp_path, [
        {"timestamp": ts, "group_id": "1203@g.us", "text": "wanted", "sender_name": "A"},
        {"timestamp": ts, "group_id": "9999@g.us", "text": "other group", "sender_name": "B"},
    ])
    assert [m.text for m in load_history(path, "1203@g.us")] == ["wanted"]


def test_load_history_skips_empty_text(tmp_path):
    # Uncaptioned photos come through with no text and carry nothing to log.
    ts = datetime(2026, 7, 13, 14, 30).timestamp()
    path = _write_history(tmp_path, [
        {"timestamp": ts, "group_id": "1203@g.us", "text": "  ", "sender_name": "A"},
        {"timestamp": ts, "group_id": "1203@g.us", "text": "real log", "sender_name": "A"},
    ])
    assert len(load_history(path, "1203@g.us")) == 1


def test_load_history_sorts_chronologically(tmp_path):
    # Paging backwards means the harvester can emit out of order; later edits to
    # a D-Wall panel must still win, so order matters before writes.
    base = datetime(2026, 7, 13, 9, 0).timestamp()
    path = _write_history(tmp_path, [
        {"timestamp": base + 60, "group_id": "1203@g.us", "text": "second", "sender_name": "A"},
        {"timestamp": base, "group_id": "1203@g.us", "text": "first", "sender_name": "A"},
    ])
    assert [m.text for m in load_history(path, "1203@g.us")] == ["first", "second"]


# ── dedupe ────────────────────────────────────────────────────────────────────
# daily_logs has no unique constraint, so this key is the only thing preventing
# a rerun from doubling the table. The same caption does not arrive
# byte-identically live and via export, so the comparison has to be forgiving.

@pytest.mark.parametrize("stored,incoming", [
    ("CCW2\nSouth side", "CCW2\nSouth side  "),        # trailing space
    ("CCW2\nSouth side", "CCW2\n\nSouth side"),        # extra blank line
    ("CCW2\nSouth side", "ccw2\nsouth side"),          # capitalisation
    ("CCW2  South side", "CCW2 South side"),           # collapsed run of spaces
])
def test_dedupe_key_treats_cosmetic_differences_as_the_same(stored, incoming):
    assert dedupe_key("2026-07-13", stored) == dedupe_key("2026-07-13", incoming)


def test_dedupe_key_separates_genuinely_different_updates():
    # A progress update rewording "in progress" -> "ongoing" is a new log.
    a = dedupe_key("2026-06-19", "U3-10: Top bar installation in progress")
    b = dedupe_key("2026-06-19", "U3-10: Top bar installation ongoing")
    assert a != b


def test_dedupe_key_separates_the_same_text_on_different_days():
    assert dedupe_key("2026-07-13", "No activity") != dedupe_key("2026-07-14", "No activity")


# ── sender numbers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sender,expected", [
    ("+65 8511 6942", "6585116942"),
    ("+6588257614", "6588257614"),
    ("Rajesh", ""),          # a saved contact — export gives no number
    ("LTA William", ""),
])
def test_sender_number_extraction(sender, expected):
    assert _sender_number(sender) == expected
