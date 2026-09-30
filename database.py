"""Postgres data layer — direct psycopg access to Cloud SQL.

Connection comes from settings.DATABASE_URL, e.g.
    postgresql://botuser:PASSWORD@10.2.0.5:5432/whatsapp_bot

The VM reaches the instance over private IP, so the database has no public
address and this string never leaves the VPC.
"""

import re
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


def get_pending_messages(received_before: datetime | None = None,
                         group_ids: list[str] | None = None) -> list[dict]:
    """Every post still waiting, oldest first; optionally some groups' only.

    Oldest first matters: a tunnel resend replaces the earlier row for the same
    day, so the later message has to be applied last to win.
    """
    return _fetch(
        """
        SELECT * FROM pending_messages
        WHERE status = 'pending'
          AND (%s::timestamptz IS NULL OR received_at < %s)
          AND (%s::text[] IS NULL OR group_id = ANY(%s))
        ORDER BY received_at, id
        """,
        (received_before, received_before, group_ids, group_ids),
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
    logged_at: datetime | None = None,
):
    """`logged_at` is when the post was sent; None means now."""
    _execute(
        """
        INSERT INTO daily_logs (
            group_id, log_date, logged_at, sender_name, sender_number,
            main_location, sub_location, description, manpower, raw_message
        )
        VALUES (%s, %s, COALESCE(%s, NOW()), %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            group_id,
            log_date,
            logged_at,
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


# Every table that holds a group's data. A model-written query may touch only
# the ones its caller allows; naming any other is refused before it runs.
_DATA_TABLES = (
    "daily_logs", "dwall_panels", "groups", "location_order", "reorder_sessions",
    "pending_messages", "tunnel_updates", "tunnel_progress", "tunnel_flags",
    "site_photos",
)

_SCHEMA_QUALIFIED = re.compile(
    r"\b(public|pg_catalog|information_schema|pg_temp\w*)\s*\.", re.I)


def scoped_statement(statement: str, tables: tuple[str, ...],
                     scope_to_group: bool, group_id: str = "") -> str:
    """Wrap a model-written SELECT so it can only see what its group may see.

    With `scope_to_group`, each allowed table is shadowed by a CTE of the same
    name holding only this group's rows. Postgres resolves an unqualified table
    name to a CTE before a real table, so whatever the model writes — a
    missing filter, a UNION, a subquery — it is reading the group's rows and
    nothing else. The group scope is enforced here, not requested in a prompt.

    Tables outside `tables`, and any schema-qualified name (which would reach
    past the CTE to the real table), are refused.
    """
    lowered = statement.lower()
    for table in _DATA_TABLES:
        if table not in tables and re.search(rf"\b{table}\b", lowered):
            raise QueryError(f"table {table} is not available here")
    if _SCHEMA_QUALIFIED.search(statement):
        raise QueryError("schema-qualified names are not allowed; "
                         "write table names unqualified")
    if "set_config" in lowered:
        # It could rewrite app.group_id mid-query; nothing legitimate needs it.
        raise QueryError("set_config is not allowed")
    if not scope_to_group:
        return statement

    # The group id is written in as a literal rather than read back from
    # app.group_id, so nothing inside the statement can change which group it
    # sees. JIDs are digits, "@" and "."; quotes are doubled regardless.
    literal = "'" + group_id.replace("'", "''") + "'"
    shadows = ",\n".join(
        f"{t} AS (SELECT * FROM public.{t} WHERE group_id = {literal})"
        for t in tables
    )
    # The newline before ")" keeps a trailing "-- comment" in the model's SQL
    # from swallowing the closing parenthesis.
    return f"WITH {shadows}\nSELECT * FROM (\n{statement}\n) AS scoped_result"


def run_readonly_query(
    sql: str,
    group_id: str,
    timeout_ms: int = 5000,
    max_rows: int = 300,
    tables: tuple[str, ...] = ("daily_logs", "dwall_panels", "groups", "location_order"),
    scope_to_group: bool = True,
) -> tuple[list[dict], bool]:
    """Run one model-written SELECT and return (rows, hit_row_cap).

    It may read only `tables`, and with `scope_to_group` only `group_id`'s rows
    of them — see scoped_statement(). Only the tunnel master group passes
    scope_to_group=False.

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
    statement = scoped_statement(statement, tables, scope_to_group, group_id)

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


# ── Tunnel progress (standardised updates) ────────────────────────────────────
#
# See migrations/004_tunnel_progress.sql. Figures go to tunnel_progress, one row
# per (contract, report date, drive); every "!" line goes to tunnel_flags, which
# is all /!! ever reads.

_PROGRESS_FIELDS = (
    "contract", "report_date", "drive",
    "rings_built", "current_ring", "total_rings", "pct_complete",
    "tbm_location", "instrumentation", "issues", "raw_message",
)

_SITE_DATE = "(%s::timestamptz AT TIME ZONE 'Asia/Singapore')::date"


def save_tunnel_update(group_id: str, update: dict, flags: list[str],
                       sender_name: str, sender_number: str,
                       sent_at: datetime) -> bool:
    """File one update and its flags together. Returns True if it replaced one.

    A resend from the same group for the same (contract, report date, drive)
    is a correction: its
    figures overwrite the earlier row and its flags replace the earlier
    update's flags — a "!" the engineer removed in the correction must not stay
    on the director's list. One transaction, so a failure leaves neither half.
    """
    values = [update.get(f) for f in _PROGRESS_FIELDS]
    cols = ", ".join(_PROGRESS_FIELDS)
    placeholders = ", ".join(["%s"] * len(_PROGRESS_FIELDS))
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _PROGRESS_FIELDS
        if c not in ("contract", "report_date", "drive")
    )
    key = (update["contract"], update["report_date"], update["drive"])

    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # Column names come from _PROGRESS_FIELDS, never from input.
                cur.execute(
                    f"""
                    INSERT INTO tunnel_progress
                        (group_id, {cols}, sender_name, sender_number, sent_at)
                    VALUES (%s, {placeholders}, %s, %s, %s)
                    ON CONFLICT (group_id, contract, report_date, drive) DO UPDATE
                        SET {updates},
                            sender_name = EXCLUDED.sender_name,
                            sender_number = EXCLUDED.sender_number,
                            sent_at = EXCLUDED.sent_at,
                            logged_at = NOW()
                    RETURNING (xmax <> 0) AS replaced
                    """,
                    (group_id, *values, sender_name, sender_number, sent_at),
                )
                replaced = bool(cur.fetchone()["replaced"])
                cur.execute(
                    "DELETE FROM tunnel_flags WHERE group_id = %s "
                    "AND contract = %s AND report_date = %s AND drive = %s",
                    (group_id, *key),
                )
                for text in flags:
                    cur.execute(
                        f"""
                        INSERT INTO tunnel_flags
                            (group_id, contract, drive, report_date, flag_text,
                             sender_name, sent_at, sent_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, {_SITE_DATE})
                        """,
                        (group_id, update["contract"], update["drive"],
                         update["report_date"], text,
                         sender_name, sent_at, sent_at),
                    )
    return replaced


def save_tunnel_flags(group_id: str, contract: str | None, flags: list[str],
                      sender_name: str, sent_at: datetime) -> None:
    """File the "!" lines of a message that is not an update."""
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for text in flags:
                    cur.execute(
                        f"""
                        INSERT INTO tunnel_flags
                            (group_id, contract, flag_text, sender_name,
                             sent_at, sent_date)
                        VALUES (%s, %s, %s, %s, %s, {_SITE_DATE})
                        """,
                        (group_id, contract, text, sender_name, sent_at, sent_at),
                    )


def group_contract(group_id: str) -> str | None:
    """The contract a group reports on, learnt from its latest update.

    A flag posted as a loose message carries no CONTRACT line; each contract
    has its own group, so the group's own updates say which contract it is.
    """
    rows = _fetch(
        "SELECT contract FROM tunnel_progress WHERE group_id = %s "
        "ORDER BY report_date DESC, sent_at DESC LIMIT 1",
        (group_id,),
    )
    return rows[0]["contract"] if rows else None


def get_flags_for_day(day: date, group_id: str | None = None) -> list[dict]:
    """Every flag SENT on `day` (site time), by contract then time."""
    where, params = ["sent_date = %s"], [day]
    if group_id:
        where.append("group_id = %s")
        params.append(group_id)
    return _fetch(
        f"""
        SELECT contract, drive, flag_text, sender_name, sent_at
        FROM tunnel_flags
        WHERE {' AND '.join(where)}
        ORDER BY contract NULLS LAST, sent_at, id
        """,
        tuple(params),
    )


def get_all_flags(group_id: str | None = None) -> list[dict]:
    """Every flag, newest first — for the export."""
    where, params = "", ()
    if group_id:
        where, params = "WHERE group_id = %s", (group_id,)
    return _fetch(
        f"""
        SELECT sent_date, contract, drive, flag_text, sender_name, sent_at
        FROM tunnel_flags {where}
        ORDER BY sent_at DESC, id DESC
        """,
        params,
    )


def get_tunnel_progress(group_id: str | None = None) -> list[dict]:
    """Every update, oldest first within each contract and drive."""
    where, params = "", ()
    if group_id:
        where, params = "WHERE group_id = %s", (group_id,)
    return _fetch(
        f"""
        SELECT contract, report_date, drive, rings_built, current_ring,
               total_rings, pct_complete, tbm_location, instrumentation,
               issues, sender_name, sent_at
        FROM tunnel_progress {where}
        ORDER BY contract, drive, report_date
        """,
        params,
    )


_PROGRESS_SEARCH_COLUMNS = (
    "contract", "report_date", "drive", "rings_built", "current_ring",
    "total_rings", "pct_complete", "tbm_location", "instrumentation", "issues",
)
_PROGRESS_MATCH_COLUMNS = (
    "contract", "drive", "tbm_location", "instrumentation", "issues",
)


def search_tunnel_progress(
    keywords: list[str] | None = None,
    group_id: str | None = None,
    limit: int = 40,
) -> tuple[list[dict], int]:
    """Return (most relevant updates, total that matched). Mirrors search_logs."""
    keywords = [k for k in (keywords or []) if k.strip()]
    cols = ", ".join(_PROGRESS_SEARCH_COLUMNS)
    scope, scope_params = ("WHERE group_id = %s", [group_id]) if group_id else ("", [])

    if keywords:
        score_sql, score_params = _keyword_score(_PROGRESS_MATCH_COLUMNS, keywords)
        inner = f"SELECT {cols}, ({score_sql}) AS score FROM tunnel_progress {scope}"
        params = score_params + scope_params
        order = "ORDER BY score DESC, report_date DESC"
        having = "WHERE score > 0"
    else:
        inner = f"SELECT {cols} FROM tunnel_progress {scope}"
        params = scope_params
        order = "ORDER BY report_date DESC"
        having = ""

    total = _fetch(f"SELECT COUNT(*) AS n FROM ({inner}) t {having}", tuple(params))
    rows = _fetch(
        f"SELECT {cols} FROM ({inner}) t {having} {order} LIMIT %s",
        tuple(params) + (limit,),
    )
    return rows, total[0]["n"]


# ── Site photos ───────────────────────────────────────────────────────────────
#
# See migrations/005_site_photos.sql.

# How far apart a photo and its log's caption may be posted and still belong
# together. An album arrives within seconds; ten minutes allows for a slow
# upload without reaching the engineer's next location.
_PHOTO_LINK_WINDOW = "10 minutes"


def insert_site_photo(group_id: str, wa_message_id: str | None, sender_name: str,
                      sender_number: str, caption: str | None, image: bytes,
                      width: int, height: int) -> bool:
    """Store one photo. False if WhatsApp delivered the same message twice."""
    rows = _fetch(
        """
        INSERT INTO site_photos
            (group_id, wa_message_id, sender_name, sender_number, caption,
             image, width, height)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (wa_message_id) DO NOTHING
        RETURNING id
        """,
        (group_id, wa_message_id, sender_name, sender_number, caption,
         image, width, height),
    )
    return bool(rows)


def link_site_photos(group_ids: list[str] | None = None) -> int:
    """Attach unlinked photos to their logs. Returns how many were linked.

    A photo that carried its own caption belongs to the log made from THAT
    caption, and to nothing else: several captioned photos forwarded together
    arrive in the same second, so "nearest in time" is a tie between different
    locations, and a caption that was not filed as a log must not borrow a
    neighbour's. Such a photo stays unlinked.

    A photo with no caption — the rest of an album — belongs to the same
    sender's log posted just before it (the album's captioned photo comes
    first), or failing that, just after.

    Run after each batch writes its logs, so a photo stays unlinked until its
    caption is filed.
    """
    window = f"interval '{_PHOTO_LINK_WINDOW}'"
    same_sender = f"""
        FROM daily_logs d
        WHERE d.group_id = p.group_id
          AND d.sender_number = p.sender_number
          AND d.logged_at BETWEEN p.sent_at - {window} AND p.sent_at + {window}
    """
    own_caption = same_sender + " AND d.raw_message = p.caption"
    recent = """
          AND p.log_id IS NULL
          -- A photo whose caption was chat, not a log, never links; don't
          -- rescan it on every run forever.
          AND p.sent_at > NOW() - interval '3 days'
          AND (%s::text[] IS NULL OR p.group_id = ANY(%s))
    """
    captioned = _fetch(
        f"""
        UPDATE site_photos AS p
        SET log_id = (
            SELECT d.id {own_caption}
            ORDER BY abs(extract(epoch FROM d.logged_at - p.sent_at)), d.id
            LIMIT 1
        )
        WHERE p.caption IS NOT NULL {recent}
          AND EXISTS (SELECT 1 {own_caption})
        RETURNING p.id
        """,
        (group_ids, group_ids),
    )
    uncaptioned = _fetch(
        f"""
        UPDATE site_photos AS p
        SET log_id = (
            SELECT d.id {same_sender}
            -- Before the photo (a few seconds' arrival jitter allowed) wins
            -- over after it; then the closest.
            ORDER BY (d.logged_at > p.sent_at + interval '5 seconds'),
                     abs(extract(epoch FROM d.logged_at - p.sent_at)), d.id
            LIMIT 1
        )
        WHERE p.caption IS NULL {recent}
          AND EXISTS (SELECT 1 {same_sender})
        RETURNING p.id
        """,
        (group_ids, group_ids),
    )
    return len(captioned) + len(uncaptioned)


def get_site_photos(group_id: str, ids: list[int]) -> list[dict]:
    """The photos with these ids, oldest first — only this group's."""
    if not ids:
        return []
    return _fetch(
        """
        SELECT p.id, p.sent_at, p.sender_name, p.caption, p.image,
               p.width, p.height,
               d.main_location, d.sub_location, d.description
        FROM site_photos p
        LEFT JOIN daily_logs d ON d.id = p.log_id
        WHERE p.group_id = %s AND p.id = ANY(%s)
        ORDER BY p.sent_at, p.id
        """,
        (group_id, list(ids)),
    )
