"""Batch logging: the slots, the dates, and when the bot speaks.

The behaviour the groups asked for is narrow — no reply per post, one message
per run and only if something failed — so most of these assert on silence.
"""
import asyncio
from datetime import date, datetime, timezone

import anthropic
import httpx
import pytest

import database as db
import ingest_batch as ib
import message_handler as mh
from config import settings
from sitetime import SITE_TZ

SITE_GROUP = "120363021760406818@g.us"
OTHER_GROUP = "120363099999999999@g.us"


def sgt(*args):
    return datetime(*args, tzinfo=SITE_TZ)


# ── Slots ─────────────────────────────────────────────────────────────────────

class TestSlots:
    @pytest.mark.parametrize("now, expected", [
        (sgt(2026, 9, 25, 0, 0), sgt(2026, 9, 25, 6)),
        (sgt(2026, 9, 25, 5, 59), sgt(2026, 9, 25, 6)),
        (sgt(2026, 9, 25, 6, 0), sgt(2026, 9, 25, 12)),
        (sgt(2026, 9, 25, 13, 0), sgt(2026, 9, 25, 18)),
        (sgt(2026, 9, 25, 18, 0, 1), sgt(2026, 9, 26, 0)),
        (sgt(2026, 12, 31, 23, 0), sgt(2027, 1, 1, 0)),
    ])
    def test_next_slot(self, now, expected):
        assert ib.next_slot(now) == expected

    @pytest.mark.parametrize("now, expected", [
        (sgt(2026, 9, 25, 0, 0), sgt(2026, 9, 25, 0)),
        (sgt(2026, 9, 25, 5, 59), sgt(2026, 9, 25, 0)),
        (sgt(2026, 9, 25, 17, 0), sgt(2026, 9, 25, 12)),
        (sgt(2026, 9, 25, 23, 59), sgt(2026, 9, 25, 18)),
    ])
    def test_previous_slot(self, now, expected):
        assert ib.previous_slot(now) == expected

    def test_slots_are_in_site_time_not_utc(self):
        # 22:00 UTC is 06:00 the next day in Singapore — a slot, not 00:00 UTC.
        utc_now = datetime(2026, 9, 24, 21, 30, tzinfo=timezone.utc)
        assert ib.next_slot(utc_now) == sgt(2026, 9, 25, 6)


# ── Posting: held, not answered ───────────────────────────────────────────────

class TestPosting:
    def test_a_site_post_is_held_with_no_reply_and_no_model_call(self, monkeypatch):
        held = []
        monkeypatch.setattr(db, "enqueue_message", lambda *a: held.append(a))
        monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", "")
        asyncio.run(mh.handle_message(SITE_GROUP, "Ali", "65", "Zone 3 rebar fixing"))
        assert held == [(SITE_GROUP, "Ali", "65", "Zone 3 rebar fixing")]

    def test_commands_still_answer_immediately(self, monkeypatch):
        import commands as cmd
        asked = []

        async def ask(group_id, q):
            asked.append(q)

        monkeypatch.setattr(cmd, "handle_ask", ask)
        monkeypatch.setattr(db, "enqueue_message",
                            lambda *a: pytest.fail("a command must not be queued"))
        asyncio.run(mh.handle_message(SITE_GROUP, "Ali", "65", "/ask when was P39 cast?"))
        assert asked == ["when was P39 cast?"]


# ── A run ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch):
    """A fake inbox and database; records every write and every message sent."""
    w = {"pending": [], "logs": [], "panels": [], "marks": {}, "sent": [], "cutoff": "unset"}

    def pending(cutoff=None):
        w["cutoff"] = cutoff
        return list(w["pending"])

    async def send(group_id, text):
        w["sent"].append((group_id, text))

    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", "")
    monkeypatch.setattr(db, "get_pending_messages", pending)
    monkeypatch.setattr(db, "mark_message",
                        lambda mid, status, error=None: w["marks"].__setitem__(mid, status))
    monkeypatch.setattr(db, "upsert_group", lambda *a, **k: None)
    monkeypatch.setattr(db, "insert_log", lambda **k: w["logs"].append(k))
    monkeypatch.setattr(db, "upsert_dwall_panel", lambda g, d: w["panels"].append(d))
    monkeypatch.setattr(ib, "send_message", send)
    return w


def post(w, text, received, group_id=SITE_GROUP, sender="Ali"):
    w["pending"].append({
        "id": len(w["pending"]) + 1, "group_id": group_id,
        "sender_name": sender, "sender_number": "65",
        "text": text, "received_at": received.isoformat(),
    })


def parses_as(monkeypatch, mapping):
    def classify(text):
        result = mapping[text]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(ib, "classify_and_parse", classify)


LOG = {"type": "log", "data": {"main_location": "Zone 3", "description": "rebar"}}


class TestRun:
    def test_a_clean_run_logs_everything_and_says_nothing(self, world, monkeypatch):
        post(world, "a", sgt(2026, 9, 25, 7))
        post(world, "b", sgt(2026, 9, 25, 8))
        parses_as(monkeypatch, {"a": LOG, "b": LOG})
        asyncio.run(ib.run_batch())
        assert len(world["logs"]) == 2
        assert world["marks"] == {1: "logged", 2: "logged"}
        assert world["sent"] == []

    def test_an_empty_inbox_says_nothing(self, world):
        assert asyncio.run(ib.run_batch()) == {}
        assert world["sent"] == []

    def test_the_log_date_is_the_day_it_arrived_not_the_day_it_ran(
        self, world, monkeypatch
    ):
        # Posted 23:30 on the 24th, logged by the 00:00 run on the 25th.
        post(world, "late", sgt(2026, 9, 24, 23, 30))
        parses_as(monkeypatch, {"late": LOG})
        asyncio.run(ib.run_batch())
        assert world["logs"][0]["log_date"] == date(2026, 9, 24)

    def test_the_arrival_date_is_read_in_site_time(self, world, monkeypatch):
        # 17:00 UTC on the 24th is 01:00 on the 25th in Singapore.
        post(world, "night", sgt(2026, 9, 25, 1))
        world["pending"][0]["received_at"] = "2026-09-24T17:00:00+00:00"
        parses_as(monkeypatch, {"night": LOG})
        asyncio.run(ib.run_batch())
        assert world["logs"][0]["log_date"] == date(2026, 9, 25)

    def test_dwall_entries_are_logged_too(self, world, monkeypatch):
        post(world, "panel", sgt(2026, 9, 25, 9))
        parses_as(monkeypatch, {"panel": {"type": "dwall", "data": {"panel_number": "CN284A"}}})
        asyncio.run(ib.run_batch())
        assert world["panels"] == [{"panel_number": "CN284A"}]
        assert world["sent"] == []

    def test_chat_and_questions_are_skipped_silently(self, world, monkeypatch):
        post(world, "ok", sgt(2026, 9, 25, 9))
        post(world, "when was P39 cast?", sgt(2026, 9, 25, 9, 5))
        parses_as(monkeypatch, {
            "ok": {"type": "ignore"},
            "when was P39 cast?": {"type": "query", "query": "when was P39 cast?"},
        })
        asyncio.run(ib.run_batch())
        assert world["marks"] == {1: "skipped", 2: "skipped"}
        assert world["logs"] == []
        assert world["sent"] == []

    def test_only_the_failures_are_reported_in_one_message(self, world, monkeypatch):
        post(world, "good", sgt(2026, 9, 25, 7))
        post(world, "Zone 9\nhoneycomb repair", sgt(2026, 9, 25, 8, 15), sender="Bala")
        post(world, "bad2", sgt(2026, 9, 25, 9))
        parses_as(monkeypatch, {
            "good": LOG,
            "Zone 9\nhoneycomb repair": ValueError("not JSON"),
            "bad2": ValueError("not JSON"),
        })
        asyncio.run(ib.run_batch(label="12:00"))

        assert world["marks"] == {1: "logged", 2: "failed", 3: "failed"}
        assert len(world["sent"]) == 1
        group, text = world["sent"][0]
        assert group == SITE_GROUP
        assert "2 posts couldn't be logged" in text
        assert "12:00 run" in text
        assert "Bala, 25 Sep 08:15" in text
        assert "Zone 9" in text
        assert "good" not in text

    def test_each_group_hears_only_about_its_own_failures(self, world, monkeypatch):
        post(world, "bad-site", sgt(2026, 9, 25, 7))
        post(world, "bad-other", sgt(2026, 9, 25, 7), group_id=OTHER_GROUP)
        parses_as(monkeypatch, {"bad-site": ValueError(), "bad-other": ValueError()})
        asyncio.run(ib.run_batch())
        assert sorted(g for g, _ in world["sent"]) == sorted([SITE_GROUP, OTHER_GROUP])
        for group, text in world["sent"]:
            assert "1 post couldn't" in text
            assert ("bad-site" in text) == (group == SITE_GROUP)

    def test_one_failure_does_not_stop_the_rest(self, world, monkeypatch):
        post(world, "bad", sgt(2026, 9, 25, 7))
        post(world, "good", sgt(2026, 9, 25, 8))
        parses_as(monkeypatch, {"bad": ValueError(), "good": LOG})
        asyncio.run(ib.run_batch())
        assert world["marks"] == {1: "failed", 2: "logged"}

    def test_a_database_failure_counts_as_not_logged(self, world, monkeypatch):
        post(world, "a", sgt(2026, 9, 25, 7))
        parses_as(monkeypatch, {"a": LOG})

        def down(**_k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(db, "insert_log", down)
        asyncio.run(ib.run_batch())
        assert world["marks"] == {1: "failed"}
        assert len(world["sent"]) == 1

    def test_the_cutoff_is_passed_to_the_inbox_query(self, world):
        slot = sgt(2026, 9, 25, 12)
        asyncio.run(ib.run_batch(cutoff=slot))
        assert world["cutoff"] == slot


class TestRetry:
    def overloaded(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        return anthropic.APIConnectionError(request=request)

    def test_a_transient_model_error_is_retried(self, world, monkeypatch):
        monkeypatch.setattr(ib, "_RETRY_DELAYS", (0, 0))
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) < 3:
                raise self.overloaded()
            return LOG

        monkeypatch.setattr(ib, "classify_and_parse", flaky)
        post(world, "a", sgt(2026, 9, 25, 7))
        asyncio.run(ib.run_batch())
        assert len(calls) == 3
        assert world["marks"] == {1: "logged"}
        assert world["sent"] == []

    def test_a_model_that_stays_down_fails_the_post(self, world, monkeypatch):
        monkeypatch.setattr(ib, "_RETRY_DELAYS", (0, 0))

        def down(_text):
            raise self.overloaded()

        monkeypatch.setattr(ib, "classify_and_parse", down)
        post(world, "a", sgt(2026, 9, 25, 7))
        asyncio.run(ib.run_batch())
        assert world["marks"] == {1: "failed"}
        assert len(world["sent"]) == 1


class TestReport:
    def test_long_first_lines_are_trimmed(self):
        row = {"sender_name": "Ali", "sender_number": "65",
               "text": "x" * 200, "received_at": sgt(2026, 9, 25, 7).isoformat()}
        text = ib.failure_report([row])
        assert "x" * 57 + "..." in text
        assert "x" * 61 not in text

    def test_blank_leading_lines_are_skipped(self):
        row = {"sender_name": "", "sender_number": "6591234567",
               "text": "\n\n  Zone 3 works  \nmore", "received_at": sgt(2026, 9, 25, 7).isoformat()}
        text = ib.failure_report([row])
        assert "_Zone 3 works_" in text
        assert "6591234567" in text   # falls back to the number with no name
