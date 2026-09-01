"""Data access against Cloud SQL for PostgreSQL.

Connections go through the Cloud SQL Python Connector rather than a host/port:
it handles TLS and authorises via the runtime service account, so no database
password or certificate is stored on the VM.

The engine is built lazily. Creating it at import time would open a socket the
moment anything imports this module, which breaks test collection and makes a
cold start fail on a transient network blip instead of on first use.
"""
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    from google.cloud.sql.connector import Connector, IPTypes

    ip_type = IPTypes.PRIVATE if settings.DB_PRIVATE_IP else IPTypes.PUBLIC
    connector = Connector(ip_type=ip_type)

    def _connect():
        return connector.connect(
            settings.INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=settings.DB_USER,
            password=settings.DB_PASSWORD or None,
            db=settings.DB_NAME,
            enable_iam_auth=settings.DB_IAM_AUTH,
        )

    _engine = create_engine(
        "postgresql+pg8000://",
        creator=_connect,
        # Cloud SQL closes idle connections; recycle below that window and test
        # liveness on checkout so a stale socket surfaces as a retry, not a 500.
        pool_size=5,
        max_overflow=2,
        pool_recycle=1800,
        pool_pre_ping=True,
    )
    return _engine


def _coerce(value):
    """Return ids and dates as strings, the way the Supabase client used to.

    Supabase delivered rows as JSON, so UUIDs and dates arrived as strings and
    the rest of the app was written against those forms. SQLAlchemy returns
    real UUID/date/datetime objects instead, which silently breaks equality:
    excel_generator indexes logs by log_date and looks them up with
    date.isoformat(), so a date object key never matches and every cell in the
    monthly report renders "-". Coerce here rather than in seven callers.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):   # must precede date: datetime subclasses it
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _rows(result) -> list[dict]:
    return [{k: _coerce(v) for k, v in r.items()} for r in result.mappings()]


# ── Groups ────────────────────────────────────────────────────────────────────

def upsert_group(group_id: str, group_name: str | None = None):
    with get_engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO groups (group_id, group_name)
                VALUES (:group_id, :group_name)
                ON CONFLICT (group_id) DO UPDATE
                    SET group_name = COALESCE(EXCLUDED.group_name, groups.group_name)
            """),
            {"group_id": group_id, "group_name": group_name},
        )


# ── Location order ────────────────────────────────────────────────────────────

def set_location_order(group_id: str, locations: list[str]):
    """Replace the location order for a group.

    Delete and insert run in ONE transaction: a failure partway through would
    otherwise leave the group with no ordering at all.
    """
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM location_order WHERE group_id = :group_id"),
            {"group_id": group_id},
        )
        if locations:
            conn.execute(
                text("""
                    INSERT INTO location_order (group_id, location_name, order_index)
                    VALUES (:group_id, :location_name, :order_index)
                """),
                [
                    {"group_id": group_id, "location_name": loc, "order_index": i}
                    for i, loc in enumerate(locations)
                ],
            )


def get_location_order(group_id: str) -> list[str]:
    with get_engine().connect() as conn:
        result = conn.execute(
            text("""
                SELECT location_name FROM location_order
                WHERE group_id = :group_id ORDER BY order_index
            """),
            {"group_id": group_id},
        )
        return [r["location_name"] for r in _rows(result)]


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
    with get_engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO daily_logs (
                    group_id, log_date, sender_name, sender_number,
                    main_location, sub_location, description, manpower, raw_message
                ) VALUES (
                    :group_id, :log_date, :sender_name, :sender_number,
                    :main_location, :sub_location, :description, :manpower, :raw_message
                )
            """),
            {
                "group_id": group_id, "log_date": log_date,
                "sender_name": sender_name, "sender_number": sender_number,
                "main_location": main_location, "sub_location": sub_location,
                "description": description, "manpower": manpower,
                "raw_message": raw_message,
            },
        )


def get_logs_for_date(group_id: str, log_date: date) -> list[dict]:
    with get_engine().connect() as conn:
        return _rows(conn.execute(
            text("""
                SELECT * FROM daily_logs
                WHERE group_id = :group_id AND log_date = :log_date
                ORDER BY logged_at
            """),
            {"group_id": group_id, "log_date": log_date},
        ))


def get_logs_for_month(group_id: str, year: int, month: int) -> list[dict]:
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    with get_engine().connect() as conn:
        return _rows(conn.execute(
            text("""
                SELECT * FROM daily_logs
                WHERE group_id = :group_id AND log_date BETWEEN :start AND :end
                ORDER BY log_date, logged_at
            """),
            {"group_id": group_id, "start": start, "end": end},
        ))


def get_all_logs(group_id: str) -> list[dict]:
    """Every log for a group, oldest first.

    Streamed rather than paged. The old PostgREST client capped a select at
    1000 rows, which silently truncated a full-history export; Postgres has no
    such cap, and yield_per keeps a ~38k-row export off the heap in one lump.
    """
    with get_engine().connect().execution_options(yield_per=1000) as conn:
        result = conn.execute(
            text("""
                SELECT * FROM daily_logs
                WHERE group_id = :group_id
                ORDER BY log_date, logged_at
            """),
            {"group_id": group_id},
        )
        return [dict(r) for r in result.mappings()]


# ── Reorder sessions ──────────────────────────────────────────────────────────

def save_reorder_session(group_id: str, session_date: date, ordered_logs: list[dict]):
    import json
    with get_engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO reorder_sessions (group_id, session_date, ordered_logs)
                VALUES (:group_id, :session_date, CAST(:ordered_logs AS JSONB))
                ON CONFLICT (group_id) DO UPDATE SET
                    session_date = EXCLUDED.session_date,
                    ordered_logs = EXCLUDED.ordered_logs
            """),
            {
                "group_id": group_id,
                "session_date": session_date,
                "ordered_logs": json.dumps(ordered_logs, default=str),
            },
        )


def get_reorder_session(group_id: str) -> dict | None:
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(
            text("SELECT * FROM reorder_sessions WHERE group_id = :group_id"),
            {"group_id": group_id},
        ))
        return rows[0] if rows else None


def clear_reorder_session(group_id: str):
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM reorder_sessions WHERE group_id = :group_id"),
            {"group_id": group_id},
        )


# ── D-Wall panels ─────────────────────────────────────────────────────────────

_DWALL_COLUMNS = [
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
]
_JSONB_COLUMNS = {"downtime"}


def upsert_dwall_panel(group_id: str, panel_data: dict[str, Any]):
    import json

    present = [c for c in _DWALL_COLUMNS if c in panel_data]
    params: dict[str, Any] = {"group_id": group_id}
    for c in present:
        v = panel_data[c]
        params[c] = json.dumps(v, default=str) if c in _JSONB_COLUMNS else v

    cols = ["group_id"] + present
    placeholders = [
        f"CAST(:{c} AS JSONB)" if c in _JSONB_COLUMNS else f":{c}" for c in cols
    ]
    # Only overwrite the fields this message actually carried. A later entry
    # that mentions casting times alone must not blank out the excavation
    # times an earlier entry recorded for the same panel.
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in present)

    sql = f"""
        INSERT INTO dwall_panels ({", ".join(cols)})
        VALUES ({", ".join(placeholders)})
        ON CONFLICT ON CONSTRAINT dwall_panels_group_panel_unique
        DO UPDATE SET {updates}
    """ if present else """
        INSERT INTO dwall_panels (group_id) VALUES (:group_id)
        ON CONFLICT ON CONSTRAINT dwall_panels_group_panel_unique DO NOTHING
    """

    with get_engine().begin() as conn:
        conn.execute(text(sql), params)


def get_all_panels(group_id: str) -> list[dict]:
    with get_engine().connect() as conn:
        return _rows(conn.execute(
            text("""
                SELECT * FROM dwall_panels
                WHERE group_id = :group_id ORDER BY panel_number
            """),
            {"group_id": group_id},
        ))
