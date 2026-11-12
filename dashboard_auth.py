"""Logins for the web dashboard.

Anyone can look at the record; asking questions, running commands and
uploading documents need a login, and only people an admin has added have
one. There is no sign-up: an admin adds someone by email, the page shows a
one-time password exactly once for the admin to pass on, and that person must
choose their own at first login. The first admin is made from the command line
on the server (manage_dashboard.py), so no password ever travels through the
page before someone can log in to it.

Passwords are hashed with scrypt (standard library, no new dependency). A
session is a random token in an HttpOnly cookie; only its SHA-256 is stored,
so a copy of the database cannot be replayed as a login. Sessions are rows,
not signed cookies, so disabling or deleting a user ends their session on the
next request.
"""

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import database as db
from config import settings
from sitetime import site_today

COOKIE = "site_session"
SESSION_DAYS = 14
MIN_PASSWORD = 10

# scrypt cost: 16 MiB of memory per hash — slow enough to blunt guessing, quick
# enough that a login on the shared f1-micro database host stays well under a
# second.
_N, _R, _P = 2 ** 14, 8, 1


# ── Passwords ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                   n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def temporary_password() -> str:
    """A one-time password an admin can read out or paste: 12 characters,
    without the look-alikes (0/O, 1/l/I)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


# Hashed once at import, so a login for an email that does not exist still
# spends the same scrypt time — response time must not reveal who has an account.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


# ── Users ────────────────────────────────────────────────────────────────────

_USER_COLUMNS = ("id, email, name, is_admin, must_change_password, disabled, "
                 "created_at, created_by, last_login_at")


def _norm(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(email: str) -> dict | None:
    rows = db._fetch(f"SELECT {_USER_COLUMNS}, password_hash FROM dashboard_users "
                     "WHERE email = %s", (_norm(email),))
    return rows[0] if rows else None


def list_users() -> list[dict]:
    return db._fetch(f"SELECT {_USER_COLUMNS} FROM dashboard_users "
                     "ORDER BY is_admin DESC, email")


def create_user(email: str, password: str, name: str = "", is_admin: bool = False,
                must_change: bool = True, created_by: str | None = None) -> dict:
    rows = db._fetch(
        f"INSERT INTO dashboard_users (email, name, password_hash, is_admin, "
        f"must_change_password, created_by) VALUES (%s, %s, %s, %s, %s, %s) "
        f"ON CONFLICT (email) DO NOTHING RETURNING {_USER_COLUMNS}",
        (_norm(email), name.strip() or None, hash_password(password), is_admin,
         must_change, created_by),
    )
    if not rows:
        raise ValueError("That email already has an account.")
    return rows[0]


def set_password(user_id: int, password: str, must_change: bool) -> None:
    db._execute("UPDATE dashboard_users SET password_hash = %s, "
                "must_change_password = %s WHERE id = %s",
                (hash_password(password), must_change, user_id))


def end_sessions(user_id: int, keep_token_hash: str | None = None) -> None:
    db._execute("DELETE FROM dashboard_sessions WHERE user_id = %s "
                "AND token_hash IS DISTINCT FROM %s", (user_id, keep_token_hash))


# ── Sessions ─────────────────────────────────────────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db._execute("INSERT INTO dashboard_sessions (token_hash, user_id, expires_at) "
                "VALUES (%s, %s, %s)", (_token_hash(token), user_id, expires))
    # Expired rows are cleared on the way in, so the table never needs a job.
    db._execute("DELETE FROM dashboard_sessions WHERE expires_at < NOW()")
    db._execute("UPDATE dashboard_users SET last_login_at = NOW() WHERE id = %s",
                (user_id,))
    return token


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
        secure=settings.DASHBOARD_COOKIE_SECURE, samesite="lax", path="/",
    )


def current_user(request: Request) -> dict:
    """The logged-in user, or 401. A user who still has a one-time password
    may only change it — every other endpoint answers 403 until they do."""
    user = _session_user(request)
    if user["must_change_password"] and request.url.path not in _ALLOWED_BEFORE_CHANGE:
        raise HTTPException(status_code=403, detail="Choose a new password first.")
    return user


def optional_user(request: Request) -> dict | None:
    """The logged-in user on a public endpoint, or None for a visitor. Used
    only to decorate what anyone may see (e.g. which documents you may
    remove), never to grant anything."""
    if not request.cookies.get(COOKIE):
        return None
    try:
        user = _session_user(request)
    except HTTPException:
        return None
    return None if user["must_change_password"] else user


def _session_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Please log in.")
    rows = db._fetch(
        f"SELECT {', '.join('u.' + c.strip() for c in _USER_COLUMNS.split(','))} "
        "FROM dashboard_sessions s JOIN dashboard_users u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > NOW() AND NOT u.disabled",
        (_token_hash(token),),
    )
    if not rows:
        raise HTTPException(status_code=401, detail="Your session has ended. Please log in.")
    user = rows[0]
    user["token_hash"] = _token_hash(token)
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


# ── Endpoints ────────────────────────────────────────────────────────────────

router = APIRouter()

_ALLOWED_BEFORE_CHANGE = {"/api/me", "/api/me/password", "/api/logout"}


def _public(user: dict) -> dict:
    return {k: user.get(k) for k in ("id", "email", "name", "is_admin",
                                     "must_change_password", "disabled",
                                     "created_at", "created_by", "last_login_at")}


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


def _visitor(request: Request) -> str:
    # Behind the proxy every request comes from the proxy, so take the address
    # it forwards. A forged header only moves a visitor into another bucket.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


class RateLimiter:
    """At most `per_visitor` events per visitor per `window` seconds, and
    `per_day` from everyone together. In memory: a restart resets it, which is
    acceptable — it is there to stop guessing and a runaway bill, not to meter."""

    def __init__(self, per_visitor: int, window: float, per_day: int):
        self.per_visitor, self.window, self.per_day = per_visitor, window, per_day
        self._seen: dict[str, deque] = defaultdict(deque)
        self._day: tuple[date, int] = (site_today(), 0)
        self._lock = threading.Lock()

    def allow(self, visitor: str, now: float | None = None,
              today: date | None = None) -> str | None:
        """None if allowed (and counted), else why not."""
        now = time.monotonic() if now is None else now
        today = today or site_today()
        with self._lock:
            day, used = self._day
            if day != today:
                day, used = today, 0
            if used >= self.per_day:
                return "The dashboard has reached today's limit. Try again tomorrow."
            seen = self._seen[visitor]
            while seen and now - seen[0] >= self.window:
                seen.popleft()
            if len(seen) >= self.per_visitor:
                # A limit of 0 leaves nothing in the window to wait out.
                remaining = self.window - (now - seen[0]) if seen else self.window
                wait = int(remaining) // 60 + 1
                return f"Too many at once — try again in about {wait} min."
            seen.append(now)
            self._day = (day, used + 1)
            return None


# Ten tries per address per 10 minutes. No daily cap: that would let one
# person lock everyone out.
_login_limiter = RateLimiter(10, 600, 10 ** 9)


@router.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response):
    if _login_limiter.allow(_visitor(request)):
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a few minutes.")

    user = get_user_by_email(body.email)
    ok = verify_password(body.password, user["password_hash"] if user else _DUMMY_HASH)
    if not user or not ok or user["disabled"]:
        # One message for all three, so the page does not confirm who has an account.
        raise HTTPException(status_code=401, detail="Wrong email or password.")

    _set_cookie(response, start_session(user["id"]))
    return {"user": _public(user)}


@router.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        db._execute("DELETE FROM dashboard_sessions WHERE token_hash = %s",
                    (_token_hash(token),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"user": _public(user)}


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


@router.post("/api/me/password")
def change_password(body: PasswordChange, user: dict = Depends(current_user)):
    full = get_user_by_email(user["email"])
    if not full or not verify_password(body.current, full["password_hash"]):
        raise HTTPException(status_code=400, detail="Your current password is wrong.")
    problem = _password_problem(body.new)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if body.new == body.current:
        raise HTTPException(status_code=400, detail="Choose a password different from the current one.")
    set_password(user["id"], body.new, must_change=False)
    # Any other device logged in as this person is signed out; this one stays.
    end_sessions(user["id"], keep_token_hash=user["token_hash"])
    return {"ok": True}


# ── Admin ────────────────────────────────────────────────────────────────────

@router.get("/api/admin/users")
def admin_list(_: dict = Depends(require_admin)):
    return {"users": list_users()}


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field("", max_length=120)
    is_admin: bool = False


@router.post("/api/admin/users")
def admin_create(body: NewUser, admin: dict = Depends(require_admin)):
    password = temporary_password()
    try:
        user = create_user(body.email, password, body.name, body.is_admin,
                           must_change=True, created_by=admin["email"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # The only time this password is ever shown. Only its hash is kept.
    return {"user": user, "temporary_password": password}


def _target(user_id: int, admin: dict) -> None:
    if user_id == admin["id"]:
        raise HTTPException(status_code=400,
                            detail="You can't do that to your own account here.")
    if not db._fetch("SELECT 1 AS ok FROM dashboard_users WHERE id = %s", (user_id,)):
        raise HTTPException(status_code=404, detail="No such user.")


@router.post("/api/admin/users/{user_id}/reset")
def admin_reset(user_id: int, admin: dict = Depends(require_admin)):
    _target(user_id, admin)
    password = temporary_password()
    set_password(user_id, password, must_change=True)
    end_sessions(user_id)
    return {"temporary_password": password}


class Enabled(BaseModel):
    disabled: bool


@router.post("/api/admin/users/{user_id}/disabled")
def admin_disable(user_id: int, body: Enabled, admin: dict = Depends(require_admin)):
    _target(user_id, admin)
    db._execute("UPDATE dashboard_users SET disabled = %s WHERE id = %s",
                (body.disabled, user_id))
    if body.disabled:
        end_sessions(user_id)
    return {"ok": True}


@router.delete("/api/admin/users/{user_id}")
def admin_delete(user_id: int, admin: dict = Depends(require_admin)):
    _target(user_id, admin)
    db._execute("DELETE FROM dashboard_users WHERE id = %s", (user_id,))
    return {"ok": True}
