"""Tunnel group routing, storage and confirmation.

The separation from the site-work path is the thing under test here: a tunnel
group must never reach classify_and_parse or daily_logs, and a site group must
never reach tunnel_updates. Getting that wrong mixes two records that the
project deliberately keeps apart.
"""
import asyncio
from datetime import date

import pytest

import database as db
import message_handler as mh
import tunnel_handler as th
import tunnel_parser as tp
from config import settings

TUNNEL_GROUP = "120363111111111111@g.us"
SITE_GROUP = "120363021760406818@g.us"

SAMPLE = """P103 Tunnel Update
15 Aug 2026

‼️ CHANGING TBM MACHINE ‼️

LDTBM Main Drive
· Mined: P1203 to P1209

TBM Progress:
· Day Shift: 3/1205/1759
· % Completion: 68.73%

Delays:
· Total Disposed: 102 loads

Thank you."""


@pytest.fixture
def tunnel_group(monkeypatch):
    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", TUNNEL_GROUP)


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound messages; block every write and model call."""
    outbox = []

    async def send(group_id, text):
        outbox.append(text)

    monkeypatch.setattr(th, "send_message", send)
    monkeypatch.setattr(mh, "send_message", send)
    monkeypatch.setattr(tp, "extract_numbers", lambda known: {"pct_completion": 68.73})
    monkeypatch.setattr(db, "upsert_group", lambda *a, **k: None)
    return outbox


@pytest.fixture
def stored(monkeypatch, sent):
    """Capture what would have been written to tunnel_updates."""
    rows = []

    def upsert(group_id, data):
        rows.append((group_id, data))
        return False

    monkeypatch.setattr(db, "upsert_tunnel_update", upsert)
    return rows


def route(text, group_id=TUNNEL_GROUP):
    return asyncio.run(mh.handle_message(group_id, "Hariharan", "6500000000", text))


# ── The two records stay apart ────────────────────────────────────────────────

class TestSeparation:
    def test_a_tunnel_group_never_reaches_the_site_parser(
        self, tunnel_group, stored, monkeypatch
    ):
        def explode(_text):
            raise AssertionError("site parser must not see tunnel messages")

        monkeypatch.setattr(mh, "classify_and_parse", explode)
        route(SAMPLE)
        assert len(stored) == 1

    def test_a_tunnel_update_never_lands_in_daily_logs(
        self, tunnel_group, stored, monkeypatch
    ):
        def explode(**_k):
            raise AssertionError("tunnel updates must not write daily_logs")

        monkeypatch.setattr(db, "insert_log", explode)
        route(SAMPLE)
        assert stored[0][0] == TUNNEL_GROUP

    def test_a_site_group_never_reaches_the_tunnel_handler(
        self, tunnel_group, sent, monkeypatch
    ):
        async def explode(*_a, **_k):
            raise AssertionError("site groups must not reach tunnel_handler")

        monkeypatch.setattr(mh, "handle_tunnel_message", explode)
        monkeypatch.setattr(mh, "classify_and_parse", lambda _t: {"type": "ignore"})
        route("Zone 3, honeycomb rectification", group_id=SITE_GROUP)

    def test_an_unset_allowlist_makes_no_group_a_tunnel_group(
        self, monkeypatch, sent
    ):
        # Fails closed, unlike the bridge allowlist: an unset value must not
        # turn every group into a tunnel group.
        monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", "")
        monkeypatch.setattr(mh, "classify_and_parse", lambda _t: {"type": "ignore"})

        async def explode(*_a, **_k):
            raise AssertionError("no group is a tunnel group when unset")

        monkeypatch.setattr(mh, "handle_tunnel_message", explode)
        route(SAMPLE)

    def test_the_allowlist_takes_several_jids(self, monkeypatch):
        monkeypatch.setattr(
            settings, "TUNNEL_GROUP_IDS", f" {TUNNEL_GROUP} , 123@g.us ,"
        )
        assert settings.tunnel_group_ids == {TUNNEL_GROUP, "123@g.us"}


# ── Logging an update ─────────────────────────────────────────────────────────

class TestLogging:
    def test_the_update_is_stored_with_its_written_date(self, tunnel_group, stored):
        route(SAMPLE)
        _group, row = stored[0]
        assert row["contract"] == "P103"
        assert row["update_date"] == date(2026, 8, 15)
        assert row["exclamation"] == "CHANGING TBM MACHINE"
        assert row["sender_name"] == "Hariharan"
        assert row["raw_message"] == SAMPLE

    def test_the_verbatim_blocks_are_stored_untouched(self, tunnel_group, stored):
        route(SAMPLE)
        _group, row = stored[0]
        assert row["main_drive"] == "· Mined: P1203 to P1209"
        assert "% Completion: 68.73%" in row["tbm_progress"]
        assert row["delays"] == "· Total Disposed: 102 loads"

    def test_the_internal_date_flag_is_not_written_to_the_table(
        self, tunnel_group, stored
    ):
        # date_was_stated drives the reply wording only; there is no such column.
        route(SAMPLE)
        assert "date_was_stated" not in stored[0][1]

    def test_the_reply_is_just_logged(self, tunnel_group, stored, sent):
        route(SAMPLE)
        assert sent == ["✅ Logged"]

    def test_chat_is_neither_stored_nor_answered(self, tunnel_group, stored, sent):
        route("noted sir")
        assert stored == []
        assert sent == []

    def test_a_parse_failure_says_so_instead_of_going_quiet(
        self, tunnel_group, stored, sent, monkeypatch
    ):
        def explode(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(th, "parse_tunnel_update", explode)
        route(SAMPLE)
        assert stored == []
        assert "wasn't logged" in sent[0]


# ── The confirmation message ──────────────────────────────────────────────────

class TestConfirmation:
    def base(self, **overrides):
        row = {
            "contract": "P103",
            "update_date": date(2026, 8, 15),
            "main_drive": "x",
            "tbm_progress": "y",
            "delays": "z",
            "exclamation": None,
            "other_sections": {},
            "pct_completion": 68.73,
        }
        row.update(overrides)
        return row

    def test_a_clean_parse_says_only_logged(self):
        # The group asked for a receipt, not a recital.
        text = th._confirmation(self.base(), replaced=False, date_was_stated=True)
        assert text == "✅ Logged"

    def test_nothing_is_recited_back_on_the_happy_path(self):
        text = th._confirmation(
            self.base(exclamation="CHANGING TBM MACHINE",
                      other_sections={"Safety": "toolbox"}),
            replaced=False, date_was_stated=True,
        )
        assert text == "✅ Logged"

    def test_a_replacement_is_still_announced(self):
        # Overwriting a day's figures silently is data loss the sender cannot
        # see from their own message.
        text = th._confirmation(self.base(), replaced=True, date_was_stated=True)
        assert text.startswith("✅ Logged")
        assert "Replaced" in text and "2026-08-15" in text

    def test_a_missing_date_line_is_still_flagged(self):
        text = th._confirmation(self.base(), replaced=False, date_was_stated=False)
        assert text.startswith("✅ Logged")
        assert "No date line" in text

    def test_both_warnings_can_appear_together(self):
        text = th._confirmation(self.base(), replaced=True, date_was_stated=False)
        assert len(text.splitlines()) == 3


# ── Questions ─────────────────────────────────────────────────────────────────

class TestQuestions:
    @pytest.fixture
    def asked(self, monkeypatch, sent):
        questions = []

        async def ask(group_id, question):
            questions.append(question)

        monkeypatch.setattr(th, "handle_tunnel_ask", ask)
        return questions

    def test_slash_ask_forwards_the_question(self, tunnel_group, asked):
        route("/ask what are the critical activities?")
        assert asked == ["what are the critical activities?"]

    def test_a_plain_question_is_answered_without_the_command(
        self, tunnel_group, asked
    ):
        # The director should not have to know there is a /ask command.
        route("What are the critical activities this week?")
        assert asked == ["What are the critical activities this week?"]

    @pytest.mark.parametrize("chat", ["ok", "noted", "on leave tomorrow", "thanks"])
    def test_statements_are_not_treated_as_questions(self, tunnel_group, asked, chat):
        route(chat)
        assert asked == []

    def test_an_empty_question_is_rejected(self, tunnel_group, sent):
        asyncio.run(th.handle_tunnel_ask(TUNNEL_GROUP, "   "))
        assert "Usage" in sent[0]

    def test_a_failed_search_reports_rather_than_hangs(
        self, tunnel_group, sent, monkeypatch
    ):
        import tunnel_ai

        def explode(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(tunnel_ai, "answer_tunnel_query", explode)
        asyncio.run(th.handle_tunnel_ask(TUNNEL_GROUP, "what happened?"))
        assert "Searching" in sent[0]
        assert "search failed" in sent[1]


# ── /critical ─────────────────────────────────────────────────────────────────

class TestCritical:
    def test_it_reads_the_column_with_no_model_in_the_path(
        self, tunnel_group, sent, monkeypatch
    ):
        monkeypatch.setattr(db, "get_tunnel_exclamations", lambda *a, **k: [
            {"contract": "P103", "update_date": "2026-08-15",
             "exclamation": "CHANGING TBM MACHINE", "sender_name": "H"},
            {"contract": "P104", "update_date": "2026-08-14",
             "exclamation": "CABLE STRIKE", "sender_name": "H"},
        ])
        asyncio.run(th.handle_critical(TUNNEL_GROUP))
        assert "CHANGING TBM MACHINE" in sent[0]
        assert "CABLE STRIKE" in sent[0]
        assert "2026-08-15" in sent[0]

    def test_it_can_be_narrowed_to_one_contract(
        self, tunnel_group, sent, monkeypatch
    ):
        monkeypatch.setattr(db, "get_tunnel_exclamations", lambda *a, **k: [
            {"contract": "P103", "update_date": "2026-08-15",
             "exclamation": "CHANGING TBM MACHINE", "sender_name": "H"},
            {"contract": "P104", "update_date": "2026-08-14",
             "exclamation": "CABLE STRIKE", "sender_name": "H"},
        ])
        asyncio.run(th.handle_critical(TUNNEL_GROUP, "P104"))
        assert "CABLE STRIKE" in sent[0]
        assert "CHANGING TBM MACHINE" not in sent[0]

    def test_nothing_flagged_says_so_plainly(self, tunnel_group, sent, monkeypatch):
        monkeypatch.setattr(db, "get_tunnel_exclamations", lambda *a, **k: [])
        asyncio.run(th.handle_critical(TUNNEL_GROUP))
        assert "No critical items" in sent[0]

    def test_it_routes_from_the_group(self, tunnel_group, sent, monkeypatch):
        seen = []

        async def critical(group_id, args):
            seen.append(args)

        monkeypatch.setattr(th, "handle_critical", critical)
        route("/critical P103")
        assert seen == ["P103"]


# ── Export ────────────────────────────────────────────────────────────────────

class TestExport:
    def test_an_empty_table_is_reported_not_exported(
        self, tunnel_group, sent, monkeypatch
    ):
        monkeypatch.setattr(db, "get_tunnel_updates", lambda *a, **k: [])
        sends = []

        async def send_doc(*a, **k):
            sends.append(a)

        monkeypatch.setattr(th, "send_document", send_doc)
        asyncio.run(th.handle_tunnel_export(TUNNEL_GROUP))
        assert "No tunnel updates" in sent[0]
        assert sends == []

    def test_the_workbook_is_sent_as_a_document(
        self, tunnel_group, sent, monkeypatch
    ):
        monkeypatch.setattr(db, "get_tunnel_updates", lambda *a, **k: [
            {"contract": "P103", "update_date": date(2026, 8, 15),
             "main_drive": "a", "tbm_progress": "b", "delays": "c",
             "exclamation": "CHANGING TBM MACHINE", "other_sections": {},
             "pct_completion": 68.73, "sender_name": "H"},
        ])
        sends = []

        async def send_doc(group_id, data, filename, caption=""):
            sends.append((filename, len(data)))

        monkeypatch.setattr(th, "send_document", send_doc)
        asyncio.run(th.handle_tunnel_export(TUNNEL_GROUP))
        assert sends[0][0] == "Tunnel_Updates.xlsx"
        assert sends[0][1] > 0
