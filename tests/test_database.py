"""SQL construction for the /ask search helpers (no DB connection).

search_logs / search_panels interpolate column names and a variable number of
keyword clauses into their SQL, so the placeholder count has to track the
parameter tuple exactly. A mismatch is a runtime error on a live query and is
invisible until someone asks a question, which is precisely the failure mode
these helpers were written to end.
"""
from datetime import date

import pytest

import database as db


@pytest.fixture
def queries(monkeypatch):
    """Capture the SQL and params each helper would execute."""
    captured = []

    def fake_fetch(sql, params=()):
        captured.append((sql, params))
        return [{"n": 0}]

    monkeypatch.setattr(db, "_fetch", fake_fetch)
    return captured


class TestPlaceholdersMatchParams:
    @pytest.mark.parametrize("call", [
        lambda: db.search_logs("G", date(2026, 9, 1), date(2026, 9, 2), ["TDP27", "cast"]),
        lambda: db.search_logs("G", None, None, ["TDP27"]),
        lambda: db.search_logs("G", date(2026, 9, 2), date(2026, 9, 2), []),
        lambda: db.search_logs("G"),
        lambda: db.search_panels("G", ["TDP27"]),
        lambda: db.search_panels("G"),
    ])
    def test_every_query_is_balanced(self, queries, call):
        call()
        assert queries, "helper issued no query"
        for sql, params in queries:
            assert sql.count("%s") == len(params), sql


class TestSearchLogs:
    def test_date_bounds_are_applied(self, queries):
        db.search_logs("G", date(2026, 9, 1), date(2026, 9, 2), [])
        sql, params = queries[-1]
        assert "log_date >= %s" in sql and "log_date <= %s" in sql
        assert date(2026, 9, 1) in params and date(2026, 9, 2) in params

    def test_open_ended_question_has_no_date_filter(self, queries):
        db.search_logs("G", None, None, ["TDP27"])
        assert "log_date >=" not in queries[-1][0]

    def test_keywords_are_or_ed_across_text_columns(self, queries):
        db.search_logs("G", None, None, ["TDP27"])
        sql = queries[-1][0]
        # OR, not AND: "cast" must still find an entry written as "casting".
        assert "main_location ILIKE %s OR sub_location ILIKE %s OR description ILIKE %s" in sql

    def test_keywords_become_substring_patterns(self, queries):
        db.search_logs("G", None, None, ["cast"])
        assert "%cast%" in queries[-1][1]

    def test_each_keyword_scores_independently(self, queries):
        db.search_logs("G", None, None, ["TDP27", "cast"])
        sql = queries[-1][0]
        # One CASE per keyword, summed — a row matching both outranks one match.
        assert sql.count("CASE WHEN") == 2
        assert "score DESC" in sql

    def test_non_matching_rows_are_excluded(self, queries):
        db.search_logs("G", None, None, ["TDP27"])
        assert "score > 0" in queries[-1][0]

    def test_no_keywords_means_no_score_filter(self, queries):
        db.search_logs("G", date(2026, 9, 2), date(2026, 9, 2), [])
        assert "score" not in queries[-1][0]

    def test_blank_keywords_are_ignored(self, queries):
        db.search_logs("G", None, None, ["", "  "])
        assert "score" not in queries[-1][0]

    def test_limit_is_applied(self, queries):
        db.search_logs("G", None, None, [], limit=400)
        assert queries[-1][1][-1] == 400

    def test_raw_message_is_never_selected(self, queries):
        # The column that made the old prompt unsendable.
        db.search_logs("G", None, None, ["TDP27"])
        assert "raw_message" not in queries[-1][0]

    def test_returns_rows_and_true_total(self, monkeypatch):
        results = iter([[{"n": 5231}], [{"log_date": "2026-09-02"}]])
        monkeypatch.setattr(db, "_fetch", lambda *a, **k: next(results))
        rows, total = db.search_logs("G", None, None, ["TDP27"])
        assert total == 5231
        assert len(rows) == 1


class TestSearchPanels:
    def test_not_filtered_by_date(self, queries):
        # report_date is TEXT holding "22/02/26", so a range comparison on it
        # would be wrong rather than merely imprecise.
        db.search_panels("G", ["TDP27"])
        assert "report_date >=" not in queries[-1][0]

    def test_keywords_match_identifying_columns_only(self, queries):
        db.search_panels("G", ["TDP27"])
        sql = queries[-1][0]
        assert "panel_number ILIKE %s" in sql
        # Level readings and volumes are not what anyone searches by.
        assert "cut_off_level ILIKE" not in sql
        assert "theo_volume ILIKE" not in sql

    def test_raw_message_is_never_selected(self, queries):
        db.search_panels("G")
        assert "raw_message" not in queries[-1][0]

    def test_stage_columns_are_selected(self, queries):
        # Panel timing questions are answered from these.
        db.search_panels("G")
        sql = queries[-1][0]
        assert "casting_start" in sql and "casting_end" in sql


class TestDwallColumnDerivation:
    def test_write_allowlist_is_unchanged_by_the_refactor(self):
        assert db._DWALL_COLUMNS == {"group_id", *db._DWALL_FIELDS}
        assert "group_id" in db._DWALL_COLUMNS
        assert "downtime" in db._DWALL_COLUMNS
        assert "raw_message" in db._DWALL_COLUMNS

    def test_search_columns_preserve_schema_order(self, queries):
        db.search_panels("G")
        sql = queries[-1][0]
        assert sql.index("excavation_start") < sql.index("casting_start")
