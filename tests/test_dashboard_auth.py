import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard_auth as auth


# ── Passwords ─────────────────────────────────────────────────────────────────

def test_hash_round_trip():
    stored = auth.hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("correct horse batterY", stored)


def test_same_password_hashes_differently():
    assert auth.hash_password("abcdefghijk") != auth.hash_password("abcdefghijk")


@pytest.mark.parametrize("stored", ["", "plain", "bcrypt$1$2$3$aa$bb", "scrypt$x$8$1$zz$00"])
def test_malformed_hash_never_verifies(stored):
    assert not auth.verify_password("anything", stored)


def test_temporary_password_avoids_look_alikes():
    for _ in range(50):
        pw = auth.temporary_password()
        assert len(pw) == 12
        assert not set(pw) & set("0O1lI")


def test_short_passwords_are_refused():
    assert auth._password_problem("short")
    assert auth._password_problem("x" * auth.MIN_PASSWORD) is None


# ── Endpoints ─────────────────────────────────────────────────────────────────

ADMIN = {"id": 1, "email": "me@lta.gov.sg", "name": "Me", "is_admin": True,
         "must_change_password": False, "disabled": False, "token_hash": "t"}
VIEWER = {**ADMIN, "id": 2, "email": "v@lta.gov.sg", "is_admin": False}


@pytest.fixture
def client(monkeypatch):
    executed = []
    monkeypatch.setattr(auth.db, "_execute", lambda q, p=(): executed.append((q, p)))
    monkeypatch.setattr(auth.db, "_fetch", lambda q, p=(): [{"ok": 1}])
    monkeypatch.setattr(auth, "_login_limiter", auth.RateLimiter(100, 600, 10 ** 9))
    app = FastAPI()
    app.include_router(auth.router)
    c = TestClient(app)
    c.executed = executed
    return c


def as_user(monkeypatch, user):
    monkeypatch.setattr(auth, "_session_user", lambda request: dict(user))


def test_no_cookie_is_401(client):
    assert client.get("/api/me").status_code == 401


def test_unknown_email_and_wrong_password_look_the_same(client, monkeypatch):
    monkeypatch.setattr(auth, "get_user_by_email", lambda e: None)
    a = client.post("/api/login", json={"email": "nobody@x.sg", "password": "pw"})
    monkeypatch.setattr(auth, "get_user_by_email", lambda e: {
        **ADMIN, "password_hash": auth.hash_password("the right one")})
    b = client.post("/api/login", json={"email": "me@lta.gov.sg", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_disabled_user_cannot_log_in(client, monkeypatch):
    monkeypatch.setattr(auth, "get_user_by_email", lambda e: {
        **ADMIN, "disabled": True, "password_hash": auth.hash_password("pw-pw-pw-pw")})
    r = client.post("/api/login", json={"email": "me@lta.gov.sg", "password": "pw-pw-pw-pw"})
    assert r.status_code == 401


def test_login_sets_an_httponly_cookie(client, monkeypatch):
    monkeypatch.setattr(auth, "get_user_by_email", lambda e: {
        **ADMIN, "password_hash": auth.hash_password("pw-pw-pw-pw")})
    r = client.post("/api/login", json={"email": "ME@lta.gov.sg ", "password": "pw-pw-pw-pw"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE}=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "password_hash" not in r.json()["user"]
    # Only the token's hash reaches the database.
    token = cookie.split(";")[0].split("=", 1)[1]
    insert = next(p for q, p in client.executed if "INSERT INTO dashboard_sessions" in q)
    assert token not in insert and auth._token_hash(token) in insert


def test_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(auth, "_login_limiter", auth.RateLimiter(2, 600, 10 ** 9))
    monkeypatch.setattr(auth, "get_user_by_email", lambda e: None)
    codes = [client.post("/api/login", json={"email": "a@b.sg", "password": "x"}).status_code
             for _ in range(3)]
    assert codes == [401, 401, 429]


def test_one_time_password_only_allows_changing_it(monkeypatch):
    as_user(monkeypatch, {**VIEWER, "must_change_password": True})
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/api/meta")
    def meta(user: dict = auth.Depends(auth.current_user)):
        return {}

    c = TestClient(app)
    assert c.get("/api/me").status_code == 200
    assert c.get("/api/meta").status_code == 403


def test_admin_pages_refuse_viewers(client, monkeypatch):
    as_user(monkeypatch, VIEWER)
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/users", json={"email": "x@y.sg"}).status_code == 403


def test_admin_adds_a_user_and_sees_the_password_once(client, monkeypatch):
    as_user(monkeypatch, ADMIN)
    made = {}

    def fake_create(email, password, name, is_admin, must_change, created_by):
        made.update(email=email, password=password, must_change=must_change, by=created_by)
        return {"id": 9, "email": email}

    monkeypatch.setattr(auth, "create_user", fake_create)
    r = client.post("/api/admin/users", json={"email": "new@lta.gov.sg", "name": "New"})
    assert r.status_code == 200
    assert r.json()["temporary_password"] == made["password"]
    assert made["must_change"] is True and made["by"] == "me@lta.gov.sg"


def test_admin_cannot_lock_themselves_out(client, monkeypatch):
    as_user(monkeypatch, ADMIN)
    assert client.delete("/api/admin/users/1").status_code == 400
    assert client.post("/api/admin/users/1/disabled", json={"disabled": True}).status_code == 400


def test_disabling_someone_ends_their_sessions(client, monkeypatch):
    as_user(monkeypatch, ADMIN)
    assert client.post("/api/admin/users/2/disabled", json={"disabled": True}).status_code == 200
    assert any("DELETE FROM dashboard_sessions" in q and p[0] == 2 for q, p in client.executed)
