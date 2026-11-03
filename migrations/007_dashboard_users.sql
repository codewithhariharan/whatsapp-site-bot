-- Who may log in to the web dashboard (dashboard.py, dashboard_auth.py).
--
-- The page is public; its data is not. An admin adds each person by email and
-- hands them a one-time password, which they must change on first login.
-- Sessions live here rather than in a signed cookie so that disabling or
-- removing someone logs them out at once, not when their cookie expires.

CREATE TABLE IF NOT EXISTS dashboard_users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,          -- stored lower-case
    name TEXT,
    password_hash TEXT NOT NULL,         -- scrypt$n$r$p$salt$hash
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
    disabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT,                     -- the admin's email
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash TEXT PRIMARY KEY,         -- sha256 of the cookie; the cookie itself is never stored
    user_id BIGINT NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_user
    ON dashboard_sessions (user_id);
