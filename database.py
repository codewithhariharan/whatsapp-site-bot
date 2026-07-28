from datetime import date
from supabase import create_client
from config import settings

db = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

# PostgREST caps every response (1000 rows by default) and gives no signal that
# it truncated. A single group passed that in July 2026, so any query that can
# return a whole project's history has to page or it silently loses the tail.
_PAGE = 1000


def _fetch_all(build_query) -> list[dict]:
    """Run a query in pages until a short page says we've reached the end.

    `build_query` takes no arguments and returns a fresh, un-executed query;
    a new one is needed per page because .range() is not re-assignable.
    """
    rows: list[dict] = []
    while True:
        page = build_query().range(len(rows), len(rows) + _PAGE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE:
            return rows


# ── Groups ────────────────────────────────────────────────────────────────────

def upsert_group(group_id: str, group_name: str = None):
    # Omit group_name when we don't have one: a payload column that isn't sent
    # is left out of the ON CONFLICT SET list, so a known name survives a later
    # write from a transport that can't see it (the Cloud API 1:1 webhook).
    row = {"group_id": group_id}
    if group_name:
        row["group_name"] = group_name
    db.table("groups").upsert(row, on_conflict="group_id").execute()


# ── Location order ────────────────────────────────────────────────────────────

def set_location_order(group_id: str, locations: list[str]):
    """Replace the location order for a group."""
    db.table("location_order").delete().eq("group_id", group_id).execute()
    rows = [
        {"group_id": group_id, "location_name": loc, "order_index": i}
        for i, loc in enumerate(locations)
    ]
    db.table("location_order").insert(rows).execute()


def get_location_order(group_id: str) -> list[str]:
    result = (
        db.table("location_order")
        .select("location_name")
        .eq("group_id", group_id)
        .order("order_index")
        .execute()
    )
    return [r["location_name"] for r in result.data]


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
    db.table("daily_logs").insert({
        "group_id": group_id,
        "log_date": log_date.isoformat(),
        "sender_name": sender_name,
        "sender_number": sender_number,
        "main_location": main_location,
        "sub_location": sub_location,
        "description": description,
        "manpower": manpower,
        "raw_message": raw_message,
    }).execute()


def get_logs_for_date(group_id: str, log_date: date) -> list[dict]:
    return _fetch_all(lambda: (
        db.table("daily_logs")
        .select("*")
        .eq("group_id", group_id)
        .eq("log_date", log_date.isoformat())
        .order("logged_at")
    ))


def get_logs_for_month(group_id: str, year: int, month: int) -> list[dict]:
    from calendar import monthrange
    last_day = monthrange(year, month)[1]
    start = date(year, month, 1).isoformat()
    end = date(year, month, last_day).isoformat()
    return _fetch_all(lambda: (
        db.table("daily_logs")
        .select("*")
        .eq("group_id", group_id)
        .gte("log_date", start)
        .lte("log_date", end)
        .order("log_date")
    ))


def get_all_logs(group_id: str) -> list[dict]:
    return _fetch_all(lambda: (
        db.table("daily_logs")
        .select("*")
        .eq("group_id", group_id)
        .order("log_date")
    ))


# ── Reorder sessions ──────────────────────────────────────────────────────────

def save_reorder_session(group_id: str, session_date: date, ordered_logs: list[dict]):
    db.table("reorder_sessions").upsert(
        {
            "group_id": group_id,
            "session_date": session_date.isoformat(),
            "ordered_logs": ordered_logs,
        },
        on_conflict="group_id",
    ).execute()


def get_reorder_session(group_id: str) -> dict | None:
    result = (
        db.table("reorder_sessions")
        .select("*")
        .eq("group_id", group_id)
        .execute()
    )
    return result.data[0] if result.data else None


def clear_reorder_session(group_id: str):
    db.table("reorder_sessions").delete().eq("group_id", group_id).execute()


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

def upsert_dwall_panel(group_id: str, panel_data: dict):
    clean = {k: v for k, v in panel_data.items() if k in _DWALL_COLUMNS}
    clean["group_id"] = group_id
    db.table("dwall_panels").upsert(
        clean, on_conflict="group_id,panel_number"
    ).execute()


def get_all_panels(group_id: str) -> list[dict]:
    return _fetch_all(lambda: (
        db.table("dwall_panels")
        .select("*")
        .eq("group_id", group_id)
        .order("panel_number")
    ))
