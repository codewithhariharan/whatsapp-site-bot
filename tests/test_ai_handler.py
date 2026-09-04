"""The /ask pipeline: SQL generation, guard rails, and fallback (no DB / network).

/ask answers by having the model write a SELECT that Postgres runs across the
whole record, rather than by loading rows into the prompt. These tests cover the
parts that decide whether that is safe and whether it degrades gracefully:
the group-scope guard, the retry-on-error loop, and the fallback to keyword
search when SQL is the wrong tool.
"""
import json
from unittest.mock import patch

import pytest

import ai_handler
import database as db


def _reply(payload: str):
    class Block:
        type, text = "text", payload

    class Response:
        content = [Block()]

    return Response()


SCOPED = "SELECT COUNT(*) AS n FROM daily_logs WHERE group_id = current_setting('app.group_id')"


class TestExtractJson:
    def test_plain(self):
        assert ai_handler._extract_json('{"sql": "SELECT 1"}') == {"sql": "SELECT 1"}

    def test_strips_code_fences(self):
        assert ai_handler._extract_json('```json\n{"sql": null}\n```') == {"sql": None}

    def test_malformed_raises(self):
        with pytest.raises(json.JSONDecodeError):
            ai_handler._extract_json("I cannot answer that")


class TestGroupScopeGuard:
    def test_unscoped_query_is_never_executed(self):
        unscoped = "SELECT COUNT(*) FROM daily_logs"
        with patch.object(ai_handler, "_write_sql", return_value={"sql": unscoped}), \
             patch.object(db, "run_readonly_query") as run:
            ai_handler._run_sql_path("G", "how many entries?")
        run.assert_not_called()

    def test_unscoped_query_is_retried_with_an_explanation(self):
        calls = []

        def fake_write(question, previous_error=None, previous_sql=None):
            calls.append(previous_error)
            return {"sql": "SELECT 1 FROM daily_logs"}  # never scoped

        with patch.object(ai_handler, "_write_sql", side_effect=fake_write), \
             patch.object(db, "run_readonly_query") as run:
            assert ai_handler._run_sql_path("G", "q") is None

        run.assert_not_called()
        assert calls[0] is None
        assert "current_setting('app.group_id')" in calls[1]

    def test_double_quoted_scope_is_accepted(self):
        sql = 'SELECT 1 FROM daily_logs WHERE group_id = current_setting("app.group_id")'
        with patch.object(ai_handler, "_write_sql", return_value={"sql": sql}), \
             patch.object(db, "run_readonly_query", return_value=([{"n": 1}], False)) as run:
            assert ai_handler._run_sql_path("G", "q") is not None
        run.assert_called_once()

    def test_group_id_is_passed_to_the_executor(self):
        with patch.object(ai_handler, "_write_sql", return_value={"sql": SCOPED}), \
             patch.object(db, "run_readonly_query", return_value=([{"n": 5}], False)) as run:
            ai_handler._run_sql_path("120363@g.us", "how many?")
        assert run.call_args.args[1] == "120363@g.us"


class TestRetryOnDatabaseError:
    def test_postgres_error_is_fed_back_to_the_model(self):
        seen = []

        def fake_write(question, previous_error=None, previous_sql=None):
            seen.append(previous_error)
            return {"sql": SCOPED}

        with patch.object(ai_handler, "_write_sql", side_effect=fake_write), \
             patch.object(db, "run_readonly_query",
                          side_effect=db.QueryError('column "zone" does not exist')):
            assert ai_handler._run_sql_path("G", "q") is None

        # The model gets the database's own wording, which is what lets it fix itself.
        assert seen[0] is None
        assert 'column "zone" does not exist' in seen[1]

    def test_second_attempt_can_succeed(self):
        outcomes = [db.QueryError("syntax error"), ([{"n": 7}], False)]

        def fake_run(sql, group_id, **kw):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(ai_handler, "_write_sql", return_value={"sql": SCOPED}), \
             patch.object(db, "run_readonly_query", side_effect=fake_run):
            result = ai_handler._run_sql_path("G", "q")

        assert result is not None
        assert '"n":7' in result[0]

    def test_gives_up_after_two_attempts(self):
        with patch.object(ai_handler, "_write_sql", return_value={"sql": SCOPED}), \
             patch.object(db, "run_readonly_query",
                          side_effect=db.QueryError("boom")) as run:
            assert ai_handler._run_sql_path("G", "q") is None
        assert run.call_count == 2

    def test_generation_failure_falls_back(self):
        with patch.object(ai_handler, "_write_sql", side_effect=RuntimeError("api down")):
            assert ai_handler._run_sql_path("G", "q") is None

    def test_model_declining_sql_falls_back_immediately(self):
        with patch.object(ai_handler, "_write_sql", return_value={"sql": None}) as write:
            assert ai_handler._run_sql_path("G", "q") is None
        write.assert_called_once()


class TestResultRendering:
    def test_result_block_carries_the_query_and_rows(self):
        with patch.object(ai_handler, "_write_sql", return_value={"sql": SCOPED}), \
             patch.object(db, "run_readonly_query", return_value=([{"n": 40024}], False)):
            block, truncated = ai_handler._run_sql_path("G", "how many entries?")
        assert "40024" in block
        assert "daily_logs" in block          # the query itself is shown
        assert truncated is False

    def test_truncation_is_declared(self):
        rows = [{"i": i} for i in range(300)]
        with patch.object(ai_handler, "_write_sql", return_value={"sql": SCOPED}), \
             patch.object(db, "run_readonly_query", return_value=(rows, True)):
            block, truncated = ai_handler._run_sql_path("G", "list everything")
        assert truncated is True
        assert "the query matched more" in block


class TestKeywordFallback:
    def test_stopwords_are_dropped(self):
        with patch.object(db, "search_logs", return_value=([], 0)) as logs, \
             patch.object(db, "search_panels", return_value=([], 0)):
            ai_handler._keyword_fallback("G", "what were the activities at CCW2?")
        assert logs.call_args.kwargs["keywords"] == ["ccw2"]

    def test_keeps_distinctive_terms(self):
        with patch.object(db, "search_logs", return_value=([], 0)) as logs, \
             patch.object(db, "search_panels", return_value=([], 0)):
            ai_handler._keyword_fallback("G", "when was TDP27 casting completed?")
        assert "tdp27" in logs.call_args.kwargs["keywords"]


class TestAnswerQueryProvenance:
    """The model must know whether it saw the whole record or only a sample."""

    def _run(self, sql_result):
        sent = {}

        def fake_create(**kwargs):
            sent["content"] = kwargs["messages"][0]["content"]
            return _reply("answer")

        with patch.object(ai_handler, "_run_sql_path", return_value=sql_result), \
             patch.object(ai_handler, "_keyword_fallback", return_value="KEYWORD DATA"), \
             patch.object(ai_handler.client.messages, "create", side_effect=fake_create):
            answer = ai_handler.answer_query("G", "q")
        return answer, sent["content"]

    def test_sql_path_states_the_result_is_complete(self):
        answer, prompt = self._run(("RESULT — 1 row(s):\n[{\"n\":40024}]", False))
        assert answer == "answer"
        assert "ENTIRE record" in prompt
        assert "that figure is complete" in prompt

    def test_truncated_sql_path_says_the_listing_is_partial(self):
        _, prompt = self._run(("RESULT", True))
        assert "listing is partial" in prompt

    def test_fallback_path_warns_against_totals(self):
        _, prompt = self._run(None)
        assert "KEYWORD DATA" in prompt
        assert "NOT the whole" in prompt
        assert "Do not give totals" in prompt

    def test_answer_is_whatsapp_formatted(self):
        _, prompt = self._run(("RESULT", False))
        assert "single asterisks" in prompt
        assert "Never use #, ##, or **" in prompt


class TestFit:
    def test_truncates_to_budget(self):
        rows = [{"description": "x" * 200} for _ in range(2000)]
        blob, shown = ai_handler._fit(rows, 20_000)
        assert len(blob) <= 20_000
        assert 0 < shown < 2000

    def test_single_oversized_row_does_not_loop_forever(self):
        assert ai_handler._fit([{"a": "x" * 500}], 10) == ("[]", 0)

    def test_no_rows(self):
        assert ai_handler._fit([], 1000) == ("[]", 0)
