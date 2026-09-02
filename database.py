"""Postgres data layer.

Replaces the Supabase client with direct psycopg access so the app can run
against Cloud SQL. Every public function keeps the same name, signature and
return shape as the Supabase version, so no other module needs to change.

Connection comes from settings.DATABASE_URL, e.g.
    postgresql://botuser:PASSWORD@10.2.0.5:5432/whatsapp_bot
"""

from datetime import date, datetime
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from config import settings


def _coerce(value):
    """Match what the Supabase client used to return.

    Supabase delivered rows as JSON, so ids arrived as strings and dates as
    ISO strings. psycopg returns real UUID, date and datetime objects. The
    rest of the app was written against the string forms — and passing UUIDs
    into save_reorder_session() would raise "not JSON serializable" — so
    convert here rather than changing seven other modules.
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


# ── Location order ────────────────────────────────────────────────────────────

def set_location_order(group_id: str, locations: list[str]):
    """Replace the location order for a group."""
    # One transaction: the delete and the insert succeed or fail together.
    # The Supabase version issued two independent calls and could leave a
    # group with no locations if the second one failed.
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM location_order WHERE group_id = %s", (group_id,))
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
    rows = _fetch("SELECT * FROM reorder_sessions WHERE group_id = %s", (group_id,))
    return rows[0] if rows else None


def clear_reorder_session(group_id: str):
    _execute("DELETE FROM reorder_sessions WHERE group_id = %s", (group_id,))


# ── D-Wall panels ─────────────────────────────────────────────────────────────

_DWALL_COLUMNS = {
    "group_id",
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
}

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
