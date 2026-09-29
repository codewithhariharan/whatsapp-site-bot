"""Tunnel groups, the master group, batch filing and /!!.

Two things matter most here. The records stay apart: a tunnel group never
reaches the site parser or daily_logs, and the reverse. And /!! is exact: every
"!" line posted that day, word for word, grouped by contract.
"""
import asyncio
from datetime import date, datetime

import pytest

import database as db
import ingest_batch as ib
import message_handler as mh
import tunnel_handler as th
from config import settings
from sitetime import SITE_TZ

TUNNEL_GROUP = "120363111111111111@g.us"
OTHER_TUNNEL = "120363222222222222@g.us"
MASTER_GROUP = "120363999999999999@g.us"
SITE_GROUP = "120363021760406818@g.us"

SAMPLE = """*TUNNEL PROGRESS UPDATE*
*CONTRACT*: CR146
*DATE*: 04-JAN-2026

*DRIVE*: EB-Main Drive 3
*PROGRESS*: 0 / 225 / 888 (25.3%)
*TBM LOCATION*: at side table of AMK Ave 3

*INSTRUMENTATION*: LG3053 breached AL!!
*ISSUES*: [shift change]"""

# 23:30 on 4 Jan, site time. The run happens after midnight; the flag must
# still belong to the 4th, the day it was sent.
RECEIVED = datetime(2026, 1, 4, 23, 30, tzinfo=SITE_TZ)


@pytest.fixture
def groups(monkeypatch):
    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", f"{TUNNEL_GROUP},{OTHER_TUNNEL}")
    monkeypatch.setattr(settings, "MASTER_GROUP_IDS", MASTER_GROUP)


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound messages; block every other write."""
    outbox = []

    async def send(group_id, text):
        outbox.append(text)

    monkeypatch.setattr(th, "send_message", send)
    monkeypatch.setattr(ib, "send_message", send)
    monkeypatch.setattr(db, "upsert_group", lambda *a, **k: None)
    return outbox


@pytest.fixture
def queued(monkeypatch):
    """Posts held for the batch run, as pending_messages rows."""
    rows = []

    def enqueue(group_id, sender_name, sender_number, text):
        rows.append({
            "id": len(rows) + 1, "group_id": group_id,
            "sender_name": sender_name, "sender_number": sender_number,
            "text": text, "received_at": RECEIVED.isoformat(), "status": "pending",
        })

    def pending(cutoff=None, group_ids=None):
        return [r for r in rows if r["status"] == "pending"
                and (group_ids is None or r["group_id"] in group_ids)]

    def mark(message_id, status, error=None):
        rows[message_id - 1]["status"] = status

    monkeypatch.setattr(db, "enqueue_message", enqueue)
    monkeypatch.setattr(db, "get_pending_messages", pending)
    monkeypatch.setattr(db, "mark_message", mark)
    return rows


@pytest.fixture
def stored(monkeypatch, sent, queued):
    """What would have been written to tunnel_progress and tunnel_flags."""
    w = {"updates": [], "flags": []}

    def save_update(group_id, update, flags, sender_name, sender_number, sent_at):
        w["updates"].append({"group_id": group_id, **update})
        w["flags"] += [{"group_id": group_id, "contract": update["contract"],
                        "drive": update["drive"], "flag_text": f,
                        "sent_at": sent_at} for f in flags]
        return False

    def save_flags(group_id, contract, flags, sender_name, sent_at):
        w["flags"] += [{"group_id": group_id, "contract": contract, "drive": None,
                        "flag_text": f, "sent_at": sent_at} for f in flags]

    monkeypatch.setattr(db, "save_tunnel_update", save_update)
    monkeypatch.setattr(db, "save_tunnel_flags", save_flags)
    monkeypatch.setattr(db, "group_contract", lambda g: "CR146")
    return w


def route(text, group_id=TUNNEL_GROUP):
    return asyncio.run(mh.handle_message(group_id, "Hariharan", "6500000000", text))


def batch():
    return asyncio.run(ib.run_batch())


# ── The records stay apart ────────────────────────────────────────────────────

class TestSeparation:
    def test_a_tunnel_group_never_reaches_the_site_parser(self, groups, stored, monkeypatch):
        def explode(_text):
            raise AssertionError("site parser must not see tunnel messages")

        monkeypatch.setattr(ib, "classify_and_parse", explode)
        route(SAMPLE)
        batch()
        assert len(stored["updates"]) == 1

    def test_a_tunnel_update_never_lands_in_daily_logs(self, groups, stored, monkeypatch):
        def explode(**_k):
            raise AssertionError("tunnel updates must not write daily_logs")

        monkeypatch.setattr(db, "insert_log", explode)
        route(SAMPLE)
        batch()
        assert stored["updates"][0]["group_id"] == TUNNEL_GROUP

    def test_a_site_group_never_reaches_the_tunnel_path(
            self, groups, sent, queued, monkeypatch):
        async def explode(*_a, **_k):
            raise AssertionError("site groups must not reach tunnel handlers")

        monkeypatch.setattr(mh, "handle_tunnel_message", explode)
        monkeypatch.setattr(mh, "handle_master_message", explode)
        monkeypatch.setattr(ib, "classify_and_parse", lambda _t: {"type": "ignore"})
        route("Zone 3, honeycomb rectification", group_id=SITE_GROUP)
        assert batch() == {}

    def test_nothing_said_in_the_master_group_is_filed(self, groups, sent, queued, monkeypatch):
        async def ask(*_a, **_k):
            pass

        monkeypatch.setattr(th, "handle_tunnel_ask", ask)
        route(SAMPLE, group_id=MASTER_GROUP)
        route("TBM stopped!", group_id=MASTER_GROUP)
        assert queued == []

    def test_unset_lists_make_no_group_a_tunnel_or_master_group(self, monkeypatch, sent, queued):
        monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", "")
        monkeypatch.setattr(settings, "MASTER_GROUP_IDS", "")

        async def explode(*_a, **_k):
            raise AssertionError("no group is special when unset")

        monkeypatch.setattr(mh, "handle_tunnel_message", explode)
        monkeypatch.setattr(mh, "handle_master_message", explode)
        route(SAMPLE)

    def test_the_lists_take_several_jids(self, monkeypatch):
        monkeypatch.setattr(settings, "MASTER_GROUP_IDS", f" {MASTER_GROUP} , 1@g.us ,")
        assert settings.master_group_ids == {MASTER_GROUP, "1@g.us"}


# ── Filing ────────────────────────────────────────────────────────────────────

class TestFiling:
    def test_an_update_is_held_with_no_reply(self, groups, stored, queued, sent):
        route(SAMPLE)
        assert len(queued) == 1
        assert stored["updates"] == []
        assert sent == []

    def test_the_run_files_the_update_and_its_flag(self, groups, stored, sent):
        route(SAMPLE)
        batch()
        update = stored["updates"][0]
        assert (update["contract"], update["report_date"]) == ("CR146", date(2026, 1, 4))
        assert update["instrumentation"] == "LG3053 breached AL"
        assert [f["flag_text"] for f in stored["flags"]] == [
            "INSTRUMENTATION: LG3053 breached AL"]
        assert sent == []                      # a clean run says nothing

    def test_a_loose_flag_message_is_filed_under_the_groups_contract(
            self, groups, stored, sent):
        route("‼️ TBM stopped – face collapse ‼️")
        batch()
        assert stored["updates"] == []
        assert stored["flags"][0]["contract"] == "CR146"
        assert stored["flags"][0]["flag_text"] == "TBM stopped – face collapse"

    def test_flags_carry_the_time_they_were_sent(self, groups, stored):
        route("Cutterhead jammed!")
        batch()
        assert stored["flags"][0]["sent_at"] == RECEIVED

    def test_chat_is_neither_queued_nor_answered(self, groups, stored, queued, sent):
        route("noted sir")
        assert queued == []
        assert sent == []

    def test_a_broken_update_is_reported_to_the_group_at_the_run(self, groups, stored, sent):
        route(SAMPLE.replace("0 / 225 / 888 (25.3%)", "going well"))
        assert sent == []                      # nothing at post time
        batch()
        assert stored["updates"] == []
        assert len(sent) == 1
        assert "couldn't be logged" in sent[0]

    def test_an_engineers_line_starting_with_bangs_is_a_flag_not_a_command(
            self, groups, stored, queued, sent):
        route("!! TBM stopped !!")
        assert len(queued) == 1
        assert sent == []


# ── /!! ───────────────────────────────────────────────────────────────────────

FLAGS = [
    {"contract": "CR146", "drive": "EB - Main Drive 3", "flag_text": "LG3053 breached AL"},
    {"contract": "CR136", "drive": None, "flag_text": "HV cable extension delayed"},
    {"contract": "CR146", "drive": "EB - Main Drive 3", "flag_text": "LG3053 breached AL"},
]


class TestFlagsReply:
    def test_grouped_by_contract_word_for_word_and_deduplicated(self):
        reply = th.format_flags(date(2026, 1, 4), FLAGS, all_contracts=True)
        assert reply == (
            "‼️ *Critical items – 4 Jan 2026*\n"
            "\n*CR136*\n• HV cable extension delayed\n"
            "\n*CR146*\n• EB - Main Drive 3: LG3053 breached AL"
        )

    def test_nothing_flagged_says_so(self):
        assert "No \"!\" items" in th.format_flags(date(2026, 1, 4), [], True)

    @pytest.mark.parametrize("args, expected", [
        ("", date(2026, 9, 29)), ("today", date(2026, 9, 29)),
        ("yesterday", date(2026, 9, 28)), ("of yesterday", date(2026, 9, 28)),
        ("29th", date(2026, 9, 29)), ("28", date(2026, 9, 28)),
        ("28 Sep", date(2026, 9, 28)), ("Sep 28", date(2026, 9, 28)),
        ("28-Sep-2026", date(2026, 9, 28)), ("28/09", date(2026, 9, 28)),
        ("monday", date(2026, 9, 28)), ("sun", date(2026, 9, 27)),
        ("31", date(2026, 8, 31)),              # no 31 Sep: last month's
        ("5 Oct", date(2025, 10, 5)),           # not in the future
        ("next week", None), ("thu 28", None),
    ])
    def test_parse_day(self, args, expected):
        assert th.parse_day(args, date(2026, 9, 29)) == expected


class TestFlagsCommand:
    @pytest.fixture
    def flags_asked(self, monkeypatch, sent, queued):
        calls = {"days": [], "caught_up": []}

        def get_flags(day, group_id=None):
            calls["days"].append((day, group_id))
            return FLAGS

        async def catch_up(group_ids):
            calls["caught_up"].append(sorted(group_ids))

        monkeypatch.setattr(db, "get_flags_for_day", get_flags)
        monkeypatch.setattr(ib, "catch_up", catch_up)
        monkeypatch.setattr(th, "site_today", lambda: date(2026, 9, 29))
        return calls

    def test_master_sees_every_contract_for_today(self, groups, sent, flags_asked):
        route("/!!", group_id=MASTER_GROUP)
        assert flags_asked["days"] == [(date(2026, 9, 29), None)]
        assert "*CR136*" in sent[0] and "*CR146*" in sent[0]

    def test_master_files_every_tunnel_groups_waiting_posts_first(
            self, groups, sent, flags_asked):
        route("/!!", group_id=MASTER_GROUP)
        assert flags_asked["caught_up"] == [sorted([TUNNEL_GROUP, OTHER_TUNNEL])]

    @pytest.mark.parametrize("command", ["!! of yesterday", "/!! yesterday",
                                         "‼️ yesterday", "/critical yesterday"])
    def test_master_can_ask_for_another_day(self, groups, sent, flags_asked, command):
        route(command, group_id=MASTER_GROUP)
        assert flags_asked["days"] == [(date(2026, 9, 28), None)]

    def test_a_contract_group_sees_only_its_own(self, groups, sent, flags_asked):
        route("/!!")
        assert flags_asked["days"] == [(date(2026, 9, 29), TUNNEL_GROUP)]
        assert flags_asked["caught_up"] == [[TUNNEL_GROUP]]

    def test_an_unreadable_day_is_explained(self, groups, sent, flags_asked):
        route("/!! next week", group_id=MASTER_GROUP)
        assert flags_asked["days"] == []
        assert "couldn't read that day" in sent[0]


# ── Questions ─────────────────────────────────────────────────────────────────

class TestQuestions:
    @pytest.fixture
    def asked(self, monkeypatch, sent):
        questions = []

        async def ask(group_id, question, all_contracts=False):
            questions.append((question, all_contracts))

        monkeypatch.setattr(th, "handle_tunnel_ask", ask)
        return questions

    def test_anything_in_the_master_group_is_a_question_across_contracts(
            self, groups, asked):
        route("where is every TBM now", group_id=MASTER_GROUP)
        route("/ask which contracts are behind?", group_id=MASTER_GROUP)
        assert asked == [("where is every TBM now", True),
                         ("which contracts are behind?", True)]

    def test_slash_ask_in_a_contract_group_is_scoped_to_it(self, groups, asked):
        route("/ask how many rings this week?")
        assert asked == [("how many rings this week?", False)]

    def test_a_plain_question_in_a_contract_group_is_answered(self, groups, asked):
        route("What is our completion now?")
        assert asked == [("What is our completion now?", False)]

    @pytest.mark.parametrize("chat", ["ok", "noted", "on leave tomorrow", "thanks"])
    def test_statements_are_not_questions(self, groups, asked, chat):
        route(chat)
        assert asked == []

    def test_a_failed_search_reports_rather_than_hangs(self, groups, sent, monkeypatch):
        import tunnel_ai

        async def no_catch_up(_g):
            pass

        def explode(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(ib, "catch_up", no_catch_up)
        monkeypatch.setattr(tunnel_ai, "answer_tunnel_query", explode)
        asyncio.run(th.handle_tunnel_ask(TUNNEL_GROUP, "what happened?"))
        assert "Searching" in sent[0]
        assert "search failed" in sent[1]


# ── Export ────────────────────────────────────────────────────────────────────

class TestExport:
    @pytest.fixture
    def documents(self, monkeypatch):
        sends = []

        async def send_doc(group_id, data, filename, caption=""):
            sends.append((filename, len(data), caption))

        monkeypatch.setattr(th, "send_document", send_doc)
        return sends

    def test_an_empty_record_is_reported_not_exported(
            self, groups, sent, documents, monkeypatch):
        monkeypatch.setattr(db, "get_tunnel_progress", lambda *a: [])
        asyncio.run(th.handle_tunnel_export(TUNNEL_GROUP))
        assert "Generating" in sent[0]
        assert "No tunnel updates" in sent[1]
        assert documents == []

    def test_master_exports_every_contract(self, groups, sent, documents, monkeypatch):
        scopes = []

        def progress(group_id=None):
            scopes.append(group_id)
            return [
                {"contract": "CR146", "report_date": "2026-01-04", "drive": "EB - Main Drive 3",
                 "rings_built": 0, "current_ring": 225, "total_rings": 888,
                 "pct_complete": 25.3, "tbm_location": "x", "instrumentation": "y",
                 "issues": None, "sender_name": "H", "sent_at": RECEIVED.isoformat()},
                {"contract": "CR136", "report_date": "2026-01-04", "drive": "WB - Main Drive 2",
                 "rings_built": 11, "current_ring": 253, "total_rings": 1000,
                 "pct_complete": 25.3, "tbm_location": "x", "instrumentation": "y",
                 "issues": None, "sender_name": "H", "sent_at": RECEIVED.isoformat()},
            ]

        monkeypatch.setattr(db, "get_tunnel_progress", progress)
        monkeypatch.setattr(db, "get_all_flags", lambda g=None: [
            {"sent_date": "2026-01-04", "contract": "CR146", "drive": None,
             "flag_text": "LG3053 breached AL", "sender_name": "H",
             "sent_at": RECEIVED.isoformat()}])
        route("/excel", group_id=MASTER_GROUP)
        assert scopes == [None]
        assert documents[0][1] > 0
        assert "2 updates across 2 contracts" in documents[0][2]


class TestContractIsolation:
    def test_a_contract_groups_ask_is_run_scoped_to_that_group(self, monkeypatch):
        import tunnel_ai
        seen = {}

        def run(sql, group_id, **kw):
            seen.update(kw, group_id=group_id)
            return [{"n": 1}], False

        monkeypatch.setattr(tunnel_ai, "_write_sql",
                            lambda *a, **k: {"sql": "SELECT COUNT(*) AS n FROM tunnel_progress "
                                                    "WHERE group_id = current_setting('app.group_id')"})
        monkeypatch.setattr(db, "run_readonly_query", run)
        tunnel_ai._run_sql_path(TUNNEL_GROUP, "how many updates?", all_contracts=False)
        assert seen["group_id"] == TUNNEL_GROUP
        assert seen["scope_to_group"] is True
        assert seen["tables"] == ("tunnel_progress", "tunnel_flags")

    def test_only_the_master_group_runs_unscoped(self, monkeypatch):
        import tunnel_ai
        seen = {}

        def run(sql, group_id, **kw):
            seen.update(kw)
            return [{"n": 1}], False

        monkeypatch.setattr(tunnel_ai, "_write_sql",
                            lambda *a, **k: {"sql": "SELECT COUNT(*) AS n FROM tunnel_progress"})
        monkeypatch.setattr(db, "run_readonly_query", run)
        tunnel_ai._run_sql_path(MASTER_GROUP, "how many updates?", all_contracts=True)
        assert seen["scope_to_group"] is False

    def test_the_site_groups_ask_cannot_reach_tunnel_tables(self, monkeypatch):
        import ai_handler
        seen = {}

        def run(sql, group_id, **kw):
            seen.update(kw)
            return [], False

        monkeypatch.setattr(ai_handler, "_write_sql",
                            lambda *a, **k: {"sql": "SELECT 1 FROM daily_logs WHERE "
                                                    "group_id = current_setting('app.group_id')"})
        monkeypatch.setattr(db, "run_readonly_query", run)
        ai_handler._run_sql_path(SITE_GROUP, "q")
        assert seen["scope_to_group"] is True
        assert "tunnel_progress" not in seen["tables"]
