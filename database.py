"""Postgres data layer — direct psycopg access to Cloud SQL.

Connection comes from settings.DATABASE_URL, e.g.
    postgresql://botuser:PASSWORD@10.2.0.5:5432/whatsapp_bot

The VM reaches the instance over private IP, so the database has no public
address and this string never leaves the VPC.
"""

from datetime import date, datetime
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from config import settings


def _coerce(value):
    """Return ids and dates as strings, not as UUID/date/datetime objects.

    psycopg hands back real Python objects; the rest of the app is written
    against the string forms, and passing a UUID into save_reorder_session()
    raises "not JSON serializable". Converting at the boundary keeps that
    contract in one place instead of spreading str() calls through seven
    modules. Callers may rely on this — do not remove it lightly.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):   # must precede date: datetime subclasses it
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _json_row(cursor):
    # INSERT/UPDATE/DELETE produce no result set and cursor.description is None.
    # The factory is still built for them, so handle that case first.
    if cursor.description is None:
        return lambda values: values
    fields = [c.name for c in cursor.description]

    def make_row(values):
        return {f: _coerce(v) for f, v in zip(fields, values)}

    return make_row


# Built on first use, not at import time. Keeps `import database` cheap and
# lets the test suite import the module without a database present.
_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": _json_row},
        )
    return _pool


def _fetch(sql: str, params: tuple = ()) -> list[dict]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _execute(sql: str, params: tuple = ()) -> None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


# ── Groups ────────────────────────────────────────────────────────────────────

def upsert_group(group_id: str, group_name: str = None):
    # COALESCE keeps an existing name when called without one. The Supabase
    # version would have overwritten it with NULL; no caller passes a name
    # today, so this is the same behaviour with the sharp edge removed.
    _execute(
        """
        INSERT INTO groups (group_id, group_name)
        VALUES (%s, %s)
        ON CONFLICT (group_id) DO UPDATE
            SET group_name = COALESCE(EXCLUDED.group_name, groups.group_name)
        """,
        (group_id, group_name),
    )

# ── Pending messages (batch logging inbox) ────────────────────────────────────

def enqueue_message(group_id: str, sender_name: str, sender_number: str, text: str):
    """Hold a post for the next batch run. See migrations/003_pending_messages.sql."""
    _execute(
        """
        INSERT INTO pending_messages (group_id, sender_name, sender_number, text)
        VALUES (%s, %s, %s, %s)
        """,
        (group_id, sender_name, sender_number, text),
    )


def get_pending_messages(received_before: datetime | None = None) -> list[dict]:
    """Every post still waiting, oldest first.

    Oldest first matters: a tunnel resend replaces the earlier row for the same
    day, so the later message has to be applied last to win.
    """
    if received_before is None:
        return _fetch(
            "SELECT * FROM pending_messages WHERE status = 'pending' "
            "ORDER BY received_at, id"
        )
    return _fetch(
        "SELECT * FROM pending_messages WHERE status = 'pending' "
        "AND received_at < %s ORDER BY received_at, id",
        (received_before,),
    )


def mark_message(message_id: int, status: str, error: str | None = None):
    _execute(
        """
        UPDATE pending_messages
        SET status = %s, error = %s, processed_at = NOW()
        WHERE id = %s
        """,
        (status, error, message_id),
    )


# ── Location order ────────────────────────────────────────────────────────────


def set_location_order(group_id: str, locations: list[str]):
    """Replace the location order for a group."""
    # One transaction: the delete and the insert succeed or fail together.
    # The Supabase version issued two independent calls and could leave a
    # group with no locations if the second one failed.
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM location_order WHERE group_id = %s", (group_id,))
            if locations:
                cur.executemany(
                    """
                    INSERT INTO location_order (group_id, location_name, order_index)
                    VALUES (%s, %s, %s)
                    """,
                    [(group_id, loc, i) for i, loc in enumerate(locations)],
                )


def get_location_order(group_id: str) -> list[str]:
    rows = _fetch(
        """
        SELECT location_name FROM location_order
        WHERE group_id = %s
        ORDER BY order_index
        """,
        (group_id,),
    )
    return [r["location_name"] for r in rows]


# ── Daily logs ────────────────────────────────────────────────────────────────

def insert_log(
    group_id: str,
    log_date: date,
    sender_name: str,
    sender_number: str,
    main_location: str,
    sub_location: str,
    description: str,
    manpower: str,
    raw_message: str,
):
    _execute(
        """
        INSERT INTO daily_logs (
            group_id, log_date, sender_name, sender_number,
            main_location, sub_location, description, manpower, raw_message
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            group_id,
            log_date,
            sender_name,
            sender_number,
            main_location,
            sub_location,
            description,
            manpower,
            raw_message,
        ),
    )


def get_logs_for_date(group_id: str, log_date: date) -> list[dict]:
    return _fetch(
        """
        SELECT * FROM daily_logs
        WHERE group_id = %s AND log_date = %s
        ORDER BY logged_at
        """,
        (group_id, log_date),
    )


def get_logs_for_month(group_id: str, year: int, month: int) -> list[dict]:
    from calendar import monthrange

    last_day = monthrange(year, month)[1]
    return _fetch(
        """
        SELECT * FROM daily_logs
        WHERE group_id = %s AND log_date >= %s AND log_date <= %s
        ORDER BY log_date
        """,
        (group_id, date(year, month, 1), date(year, month, last_day)),
    )


def get_all_logs(group_id: str) -> list[dict]:
    return _fetch(
        """
        SELECT * FROM daily_logs
        WHERE group_id = %s
        ORDER BY log_date, logged_at
        """,
        (group_id,),
    )


# ── Reorder sessions ──────────────────────────────────────────────────────────

def save_reorder_session(group_id: str, session_date: date, ordered_logs: list[dict]):
    _execute(
        """
        INSERT INTO reorder_sessions (group_id, session_date, ordered_logs)
        VALUES (%s, %s, %s)
        ON CONFLICT (group_id) DO UPDATE
            SET session_date = EXCLUDED.session_date,
                ordered_logs = EXCLUDED.ordered_logs
        """,
        (group_id, session_date, Jsonb(ordered_logs)),
    )


def get_reorder_session(group_id: str) -> dict | None:
    rows = _fetch(
        "SELECT * FROM reorder_sessions WHERE group_id = %s", (group_id,))
    return rows[0] if rows else None


def clear_reorder_session(group_id: str):
    _execute("DELETE FROM reorder_sessions WHERE group_id = %s", (group_id,))


# ── D-Wall panels ─────────────────────────────────────────────────────────────

# Schema order, so a row built from these reads the way the paper form does.
# _DWALL_COLUMNS (the write allow-list) is derived from it, and search_panels()
# reuses the order when handing rows to the model.
_DWALL_FIELDS = (
    "report_date", "engineer_initials", "entry_number", "panel_number", "panel_group",
    "panel_size", "guide_wall_level", "cut_off_level", "design_toe_level",
    "design_depth", "final_depth", "rock_hit",
    "excavation_start", "excavation_end",
    "koden_start", "koden_end",
    "desanding_start", "desanding_end",
    "water_stop_start", "water_stop_end",
    "rebar_cage_start", "rebar_cage_end",
    "tremie_pipe_start", "tremie_pipe_end",
    "casting_start", "casting_end",
    "theo_volume", "actual_volume", "overbreak_pct",
    "downtime", "notes", "raw_message",
)

_DWALL_COLUMNS = {"group_id", *_DWALL_FIELDS}

_JSONB_COLUMNS = {"downtime"}


def upsert_dwall_panel(group_id: str, panel_data: dict):
    clean = {k: v for k, v in panel_data.items() if k in _DWALL_COLUMNS}
    clean["group_id"] = group_id

    cols = list(clean.keys())
    values = [
        Jsonb(clean[c]) if c in _JSONB_COLUMNS and not isinstance(clean[c], str)
        else clean[c]
        for c in cols
    ]

    # Only the columns supplied are updated on conflict — same partial-update
    # behaviour as the Supabase upsert. Column names come from _DWALL_COLUMNS,
    # never from user input, so this interpolation is safe.
    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c not in ("group_id", "panel_number")
    )
    conflict_action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"

    _execute(
        f"""
        INSERT INTO dwall_panels ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (group_id, panel_number) {conflict_action}
        """,
        tuple(values),
    )


def get_all_panels(group_id: str) -> list[dict]:
    return _fetch(
        """
        SELECT * FROM dwall_panels
        WHERE group_id = %s
        ORDER BY panel_number
        """,
        (group_id,),
    )


# ── Search (backs /ask) ───────────────────────────────────────────────────────

# Columns worth putting in front of the model. `raw_message` is deliberately
# absent: it largely repeats description, and across 40k+ rows it was the single
# biggest contributor to the oversized /ask prompt. Internal ids and phone
# numbers are absent for the same reason — no answer needs them.
_LOG_SEARCH_COLUMNS = (
    "log_date", "sender_name", "main_location", "sub_location",
    "description", "manpower",
)

_PANEL_SEARCH_COLUMNS = tuple(c for c in _DWALL_FIELDS if c != "raw_message")

# Free-text columns a keyword is allowed to match. Matching every panel column
# would score hits off level readings and volumes, which no question is about.
_LOG_MATCH_COLUMNS = ("main_location", "sub_location", "description")
_PANEL_MATCH_COLUMNS = ("panel_number", "panel_group",
                        "engineer_initials", "notes")


def _keyword_score(columns: tuple[str, ...], keywords: list[str]) -> tuple[str, list]:
    """Build a SQL expression counting how many keywords a row matches.

    Keywords are OR'd, never AND'd: engineers write "cast" where the question
    says "casting", so requiring every term would drop the very row being asked
    about. Narrowing happens through ordering instead — a row matching three
    keywords outranks one matching a single keyword.

    Column names come from the module constants above, never from user input,
    so interpolating them is safe; the search terms themselves stay parameters.
    """
    parts, params = [], []
    for kw in keywords:
        ors = " OR ".join(f"{c} ILIKE %s" for c in columns)
        parts.append(f"(CASE WHEN ({ors}) THEN 1 ELSE 0 END)")
        params.extend([f"%{kw}%"] * len(columns))
    return " + ".join(parts), params


def search_logs(
    group_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    keywords: list[str] | None = None,
    limit: int = 400,
) -> tuple[list[dict], int]:
    """Return (most relevant rows, total number that matched).

    The total is reported separately so the caller can tell the model when it is
    looking at a subset — an answer drawn from the top 400 of 5,000 matches must
    not be presented as if it covered the whole record.
    """
    keywords = [k for k in (keywords or []) if k.strip()]

    where = ["group_id = %s"]
    where_params: list = [group_id]
    if date_from:
        where.append("log_date >= %s")
        where_params.append(date_from)
    if date_to:
        where.append("log_date <= %s")
        where_params.append(date_to)

    cols = ", ".join(_LOG_SEARCH_COLUMNS)

    if keywords:
        score_sql, score_params = _keyword_score(_LOG_MATCH_COLUMNS, keywords)
        inner = f"""
            SELECT {cols}, ({score_sql}) AS score
            FROM daily_logs
            WHERE {' AND '.join(where)}
        """
        params = score_params + where_params
        order = "ORDER BY score DESC, log_date DESC"
        having = "WHERE score > 0"
    else:
        # No keywords means the question is scoped by date alone ("what happened
        # today"), so every row in the window is equally relevant.
        inner = f"SELECT {cols} FROM daily_logs WHERE {' AND '.join(where)}"
        params = where_params
        order = "ORDER BY log_date DESC"
        having = ""

    total = _fetch(
        f"SELECT COUNT(*) AS n FROM ({inner}) t {having}", tuple(params))
    rows = _fetch(
        f"SELECT {cols} FROM ({inner}) t {having} {order} LIMIT %s",
        tuple(params) + (limit,),
    )
    return rows, total[0]["n"]


def search_panels(
    group_id: str,
    keywords: list[str] | None = None,
    limit: int = 300,
) -> tuple[list[dict], int]:
    """Return (most relevant panel rows, total number that matched).

    Panels are not filtered by date: report_date is a TEXT column holding
    "22/02/26"-style strings, so a range comparison on it would be wrong rather
    than merely imprecise. Questions about panel timing are answered from the
    stage columns instead.
    """
    keywords = [k for k in (keywords or []) if k.strip()]
    cols = ", ".join(_PANEL_SEARCH_COLUMNS)

    if keywords:
        score_sql, score_params = _keyword_score(
            _PANEL_MATCH_COLUMNS, keywords)
        inner = f"""
            SELECT {cols}, ({score_sql}) AS score
            FROM dwall_panels
            WHERE group_id = %s
        """
        params = score_params + [group_id]
        order = "ORDER BY score DESC, panel_number"
        having = "WHERE score > 0"
    else:
        inner = f"SELECT {cols} FROM dwall_panels WHERE group_id = %s"
        params = [group_id]
        order = "ORDER BY panel_number"
        having = ""

    total = _fetch(
        f"SELECT COUNT(*) AS n FROM ({inner}) t {having}", tuple(params))
    rows = _fetch(
        f"SELECT {cols} FROM ({inner}) t {having} {order} LIMIT %s",
        tuple(params) + (limit,),
    )
    return rows, total[0]["n"]


# ── Read-only ad-hoc queries (backs /ask) ─────────────────────────────────────

class QueryError(Exception):
    """A model-written query that Postgres rejected. Carries the DB's message
    so the caller can hand it back to the model for a second attempt."""


def run_readonly_query(
    sql: str,
    group_id: str,
    timeout_ms: int = 5000,
    max_rows: int = 300,
) -> tuple[list[dict], bool]:
    """Run one model-written SELECT and return (rows, hit_row_cap).

    Three independent guards, none of which rely on inspecting the SQL text:

    1. `SET TRANSACTION READ ONLY` — Postgres itself refuses INSERT/UPDATE/
       DELETE/DDL inside the transaction. Pattern-matching the query string for
       dangerous keywords is a blocklist and would eventually be wrong; this is
       the database enforcing it. It is transaction-scoped, so a pooled
       connection is never left in a modified state.
    2. `statement_timeout` — a runaway join cannot pin the instance. This
       matters more than usual: the database is a db-f1-micro shared with the
       live logging path, so a slow /ask must not block engineers' updates.
    3. A fetch cap — the query is not rewritten (that would risk changing its
       meaning); we simply stop reading rows. One extra row is fetched so the
       caller can tell the model its view was truncated.

    psycopg's extended query protocol also refuses multiple statements in one
    execute, so a trailing `; DROP ...` cannot ride along.

    The group scope travels as a transaction-local setting rather than a bound
    parameter, and the query is executed with NO parameters. That is deliberate:
    psycopg only parses `%` placeholders when parameters are supplied, so a
    perfectly ordinary `ILIKE '%P46%'` would otherwise blow up as a malformed
    placeholder. The model writes `current_setting('app.group_id')` instead, and
    literal percent signs stay literal.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryError("empty query")

    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    # SET takes no bound parameters; int() is the guard here and
                    # set_config() is the parameterised form for the group id.
                    cur.execute(
                        f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    cur.execute("SELECT set_config('app.group_id', %s, true)",
                                (group_id,))
                    cur.execute(statement)
                    if cur.description is None:
                        raise QueryError("query returned no result set")
                    rows = cur.fetchmany(max_rows + 1)
    except QueryError:
        raise
    except Exception as exc:
        # Surface the database's own wording — "column x does not exist" is
        # exactly what lets the model correct itself on the retry.
        raise QueryError(str(exc).strip()) from exc

    return rows[:max_rows], len(rows) > max_rows


# ── Tunnel updates ────────────────────────────────────────────────────────────

# Schema order, so a row built from these reads the way the reporting sheet
# does. _TUNNEL_COLUMNS (the write allow-list) is derived from it, and
# search_tunnel_updates() reuses the order when handing rows to the model.
_TUNNEL_FIELDS = (
    "contract", "title_line", "update_date", "drive_name",
    "main_drive", "tbm_progress", "delays", "exclamation", "other_sections",
    "mined_from", "mined_to", "ring_built_from", "ring_built_to",
    "fsc_shift", "fsc_cumulative", "rings_total",
    "day_shift_rings", "day_shift_cumulative",
    "night_shift_rings", "night_shift_cumulative",
    "pct_completion", "tbm_location", "instrumentation",
    "delay_flag", "ds_loads", "ns_loads", "total_disposed_loads",
    "disposed_rings_equiv", "rings_excavated", "delta_disposal",
    "storage_rings", "storage_capacity_rings", "earthwork_subcon",
    "sender_name", "sender_number", "raw_message",
)

_TUNNEL_COLUMNS = {"group_id", *_TUNNEL_FIELDS}

_TUNNEL_JSONB_COLUMNS = {"other_sections"}


def upsert_tunnel_update(group_id: str, data: dict) -> bool:
    """Insert one tunnel update, replacing any earlier one for the same day.

    Returns True when a row already existed for (group, contract, date) — the
    caller says so in its reply, because silently overwriting yesterday's
    figures with a mistyped resend is exactly the kind of thing a group needs
    to see happen.

    Unlike upsert_dwall_panel, every column in the allow-list is written, not
    only the ones supplied: a correction that drops a section must clear that
    section, not leave the previous message's text sitting in it.
    """
    clean = {k: data.get(k) for k in _TUNNEL_FIELDS}
    clean["group_id"] = group_id

    cols = list(clean.keys())
    values = [
        Jsonb(clean[c]) if c in _TUNNEL_JSONB_COLUMNS and not isinstance(clean[c], str)
        else clean[c]
        for c in cols
    ]

    # Column names come from _TUNNEL_FIELDS, never from user input, so this
    # interpolation is safe.
    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in cols
        if c not in ("group_id", "contract", "update_date")
    )

    rows = _fetch(
        f"""
        INSERT INTO tunnel_updates ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (group_id, contract, update_date) DO UPDATE
            SET {updates}, logged_at = NOW()
        RETURNING (xmax <> 0) AS replaced
        """,
        tuple(values),
    )
    return bool(rows and rows[0]["replaced"])


def get_tunnel_updates(
    group_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    contract: str | None = None,
) -> list[dict]:
    """Every stored update, newest last — the order the Excel export wants."""
    where = ["group_id = %s"]
    params: list = [group_id]
    if date_from:
        where.append("update_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("update_date <= %s")
        params.append(date_to)
    if contract:
        where.append("contract ILIKE %s")
        params.append(contract)

    return _fetch(
        f"""
        SELECT * FROM tunnel_updates
        WHERE {' AND '.join(where)}
        ORDER BY contract, update_date
        """,
        tuple(params),
    )


def get_tunnel_exclamations(
    group_id: str,
    date_from: date | None = None,
    limit: int = 50,
) -> list[dict]:
    """The flagged (‼) entries only, newest first.

    This is the director's question — "what are the critical activities" — and
    it reads the exclamation column and nothing else, by design. Routing it
    through the general SQL path would let the model widen the search into the
    delays block, which is not the same thing and would bury the real flags.
    """
    where = ["group_id = %s", "exclamation IS NOT NULL", "exclamation <> ''"]
    params: list = [group_id]
    if date_from:
        where.append("update_date >= %s")
        params.append(date_from)

    return _fetch(
        f"""
        SELECT contract, update_date, exclamation, sender_name
        FROM tunnel_updates
        WHERE {' AND '.join(where)}
        ORDER BY update_date DESC, contract
        LIMIT %s
        """,
        tuple(params) + (limit,),
    )


# Columns worth putting in front of the model in the fallback path. raw_message
# is absent for the same reason it is absent from the site-log search: it
# repeats the section blocks verbatim and doubles the prompt for nothing.
_TUNNEL_SEARCH_COLUMNS = tuple(
    c for c in _TUNNEL_FIELDS
    if c not in ("raw_message", "sender_number", "title_line")
)

_TUNNEL_MATCH_COLUMNS = (
    "contract", "drive_name", "main_drive", "tbm_progress", "delays",
    "exclamation", "tbm_location", "earthwork_subcon",
)


def search_tunnel_updates(
    group_id: str,
    keywords: list[str] | None = None,
    limit: int = 40,
) -> tuple[list[dict], int]:
    """Return (most relevant tunnel rows, total that matched).

    Mirrors search_logs: keywords are OR'd and used for ranking rather than
    filtering, so a question phrased "casting" still reaches a row that says
    "cast".
    """
    keywords = [k for k in (keywords or []) if k.strip()]
    cols = ", ".join(_TUNNEL_SEARCH_COLUMNS)

    if keywords:
        score_sql, score_params = _keyword_score(
            _TUNNEL_MATCH_COLUMNS, keywords)
        inner = f"""
            SELECT {cols}, ({score_sql}) AS score
            FROM tunnel_updates
            WHERE group_id = %s
        """
        params = score_params + [group_id]
        order = "ORDER BY score DESC, update_date DESC"
        having = "WHERE score > 0"
    else:
        inner = f"SELECT {cols} FROM tunnel_updates WHERE group_id = %s"
        params = [group_id]
        order = "ORDER BY update_date DESC"
        having = ""

    total = _fetch(
        f"SELECT COUNT(*) AS n FROM ({inner}) t {having}", tuple(params))
    rows = _fetch(
        f"SELECT {cols} FROM ({inner}) t {having} {order} LIMIT %s",
        tuple(params) + (limit,),
    )
    return rows, total[0]["n"]
