"""Paging over PostgREST's row cap.

PostgREST returns at most 1000 rows and gives no indication it truncated, so a
project whose history passes that silently loses its tail — from Excel exports,
from /ask, and (worse) from the backfill's duplicate check, where a short list
means every row past the cap is inserted again.
"""
import database as db


class _FakeQuery:
    """Mimics the supabase query builder for .range(...).execute().data."""

    def __init__(self, rows):
        self._rows = rows

    def range(self, start, end):
        self._slice = self._rows[start:end + 1]
        return self

    def execute(self):
        return type("Result", (), {"data": self._slice})()


def _pager(total):
    rows = [{"id": i} for i in range(total)]
    calls = []

    def build():
        calls.append(1)
        return _FakeQuery(rows)

    return build, calls


def test_returns_everything_below_the_cap():
    build, calls = _pager(300)
    assert len(db._fetch_all(build)) == 300
    assert len(calls) == 1  # one short page is enough to know we're done


def test_pages_past_the_cap():
    build, calls = _pager(1574)  # the real CR106 count that exposed this
    rows = db._fetch_all(build)
    assert len(rows) == 1574
    assert [r["id"] for r in rows] == list(range(1574))  # no gaps, no repeats
    assert len(calls) == 2


def test_exact_multiple_of_the_page_size_needs_a_final_empty_page():
    # 1000 rows come back full, so the loop cannot assume it has finished.
    build, calls = _pager(2000)
    assert len(db._fetch_all(build)) == 2000
    assert len(calls) == 3


def test_empty_table():
    build, _ = _pager(0)
    assert db._fetch_all(build) == []
