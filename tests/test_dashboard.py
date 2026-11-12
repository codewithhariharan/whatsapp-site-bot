from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard
from config import settings

SITE = "120363000000000000@g.us"
TUNNEL = "120363111111111111@g.us"


def _sql(composable) -> str:
    """Render a psycopg Composable without a connection (identifiers quoted)."""
    from psycopg import sql
    def walk(c):
        if isinstance(c, sql.Composed):
            return "".join(walk(p) for p in c._obj)
        if isinstance(c, sql.SQL):
            return c._obj
        if isinstance(c, sql.Identifier):
            return ".".join(f'"{p}"' for p in c._obj)
        raise TypeError(c)
    return walk(composable)


@pytest.fixture
def groups(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_SITE_GROUP_ID", SITE)
    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", TUNNEL)


@pytest.fixture
def client(monkeypatch, groups):
    calls = []

    def fake_fetch(query, params=()):
        calls.append((_sql(query), params))
        return [{"n": 0}]

    monkeypatch.setattr(dashboard.db, "_fetch", fake_fetch)
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[dashboard.current_user] = lambda: {
        "id": 1, "email": "a@lta.gov.sg", "is_admin": False, "must_change_password": False}
    c = TestClient(app)
    c.calls = calls
    return c


# ── Scope ─────────────────────────────────────────────────────────────────────

def test_site_table_is_confined_to_the_site_group(groups):
    select, _, params = dashboard.build_query("daily_logs")
    assert '"group_id" = %s' in _sql(select)
    assert params == [SITE]


def test_tunnel_table_is_confined_to_the_tunnel_groups(groups):
    select, _, params = dashboard.build_query("tunnel_flags")
    assert '"group_id" = ANY(%s)' in _sql(select)
    assert params == [[TUNNEL]]


@pytest.mark.parametrize("name", list(dashboard.TABLES))
def test_unconfigured_scope_shows_nothing(monkeypatch, name):
    # Empty must not mean "every group": stray rows from 125 chats sit in daily_logs.
    monkeypatch.setattr(settings, "DASHBOARD_SITE_GROUP_ID", "")
    monkeypatch.setattr(settings, "TUNNEL_GROUP_IDS", "")
    _, where, params = dashboard.build_query(name)
    assert _sql(where) == "FALSE"
    assert params == []


def test_photo_scope_names_the_photo_table(groups):
    cond, _ = dashboard._scope_condition("site", alias="p")
    assert _sql(cond) == '"p"."group_id" = %s'


# ── Filters ───────────────────────────────────────────────────────────────────

def test_search_and_dates_are_bound_not_inlined(groups):
    select, _, params = dashboard.build_query(
        "daily_logs", q="U3'; DROP TABLE x", date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30))
    text = _sql(select)
    assert "DROP" not in text
    search = dashboard.TABLES["daily_logs"]["search"]
    assert params == ([SITE] + ["%U3'; DROP TABLE x%"] * len(search)
                      + [date(2026, 9, 1), date(2026, 9, 30)])
    assert '"log_date"::date >= %s' in text and '"log_date"::date <= %s' in text


def test_phone_numbers_and_raw_text_are_never_selected():
    for cfg in dashboard.TABLES.values():
        assert "sender_number" not in cfg["columns"]
        assert "raw_message" not in cfg["columns"]
        assert set(cfg["list"]) <= set(cfg["columns"])


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/meta", "/api/overview", "/api/table/daily_logs",
                                  "/api/table/daily_logs/export", "/api/photos"])
def test_the_record_is_public(monkeypatch, groups, path):
    monkeypatch.setattr(dashboard.db, "_fetch", lambda *a: [{"n": 0}])
    app = FastAPI()
    app.include_router(dashboard.router)
    assert TestClient(app).get(path).status_code == 200


@pytest.mark.parametrize("path,body", [("/api/ask", {"question": "what happened today"}),
                                       ("/api/console", {"text": "/help"})])
def test_ask_and_console_need_a_login(monkeypatch, groups, path, body):
    monkeypatch.setattr(dashboard.db, "_fetch", lambda *a: pytest.fail("queried without a login"))
    app = FastAPI()
    app.include_router(dashboard.router)
    assert TestClient(app).post(path, json=body).status_code == 401


def test_search_engines_are_turned_away():
    app = FastAPI()
    app.include_router(dashboard.router)
    assert "Disallow: /" in TestClient(app).get("/robots.txt").text


def test_page_loads_without_a_login():
    app = FastAPI()
    app.include_router(dashboard.router)
    assert TestClient(app).get("/").status_code == 200


def test_unknown_table_is_404(client):
    assert client.get("/api/table/pending_messages").status_code == 404
    assert client.get("/api/table/pending_messages/export").status_code == 404
    assert client.calls == []


def test_table_endpoint_pages(client):
    r = client.get("/api/table/daily_logs?q=U3&page=3&size=20")
    assert r.status_code == 200
    rows_query, params = client.calls[-1]
    assert rows_query.endswith("LIMIT %s OFFSET %s")
    assert params[-2:] == (20, 40)


def test_page_size_is_capped(client):
    assert client.get("/api/table/daily_logs?size=5000").status_code == 422


def test_export_is_a_workbook(client):
    r = client.get("/api/table/tunnel_flags/export")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"            # xlsx is a zip


def test_ask_refuses_over_the_limit(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_limiter", dashboard.RateLimiter(0, 600, 100))
    r = client.post("/api/ask", json={"question": "what happened today"})
    assert r.status_code == 429


def test_ask_rejects_an_unknown_scope(client):
    r = client.post("/api/ask", json={"question": "hello", "scope": "everything"})
    assert r.status_code == 422


def test_ask_routes_tunnel_questions_across_all_contracts(client, monkeypatch):
    import tunnel_ai
    seen = {}

    def fake(gid, question, all_contracts=False):
        seen.update(gid=gid, all_contracts=all_contracts)
        return {"answer": "*P103* 50%", "rows": [{"pct": 50}], "sql": "SELECT"}

    monkeypatch.setattr(tunnel_ai, "answer_tunnel_query_web", fake)
    r = client.post("/api/ask", json={"question": "progress?", "scope": "tunnel"})
    assert r.json() == {"kind": "text", "answer": "*P103* 50%", "rows": [{"pct": 50}],
                        "sql": "SELECT"}
    assert seen == {"gid": TUNNEL, "all_contracts": True}


# ── Rate limiter ──────────────────────────────────────────────────────────────

def test_limiter_per_visitor_window():
    lim = dashboard.RateLimiter(per_visitor=2, window=600, per_day=100)
    today = date(2026, 11, 3)
    assert lim.allow("a", now=0, today=today) is None
    assert lim.allow("a", now=10, today=today) is None
    assert lim.allow("a", now=20, today=today)          # third in the window
    assert lim.allow("b", now=20, today=today) is None  # someone else is fine
    assert lim.allow("a", now=601, today=today) is None  # first one aged out


def test_limiter_daily_cap_resets_next_day():
    lim = dashboard.RateLimiter(per_visitor=100, window=600, per_day=2)
    d1, d2 = date(2026, 11, 3), date(2026, 11, 4)
    assert lim.allow("a", now=0, today=d1) is None
    assert lim.allow("b", now=0, today=d1) is None
    assert "today" in lim.allow("c", now=0, today=d1)
    assert lim.allow("c", now=0, today=d2) is None
