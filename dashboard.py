"""Web dashboard — the site and tunnel record in a browser, read-only.

Everything the WhatsApp groups can look at, without WhatsApp: browse and filter
each table, open a row in full, see the photos, download what is on screen as
Excel, and ask questions in plain language through the same /ask engines the
groups use.

The page's address is public; its data is not. Every /api endpoint here needs
a login, and only people an admin has added have one (dashboard_auth.py).
Beyond that, three things are deliberately narrow:

  - Nothing here writes. Every endpoint is a SELECT, and the tables and their
    columns come from the allowlist below — never from the request — so a
    crafted URL cannot reach a table or column that is not listed.
  - Phone numbers (`sender_number`) and the raw WhatsApp text are left out.
    Names stay; numbers are personal data and add nothing to the record.
  - /ask costs Gemini calls, so it is rate-limited per person and per day.

Scope mirrors the bot's: site tables show only DASHBOARD_SITE_GROUP_ID's rows,
and tunnel tables only TUNNEL_GROUP_IDS'. Either one empty shows nothing rather
than every group's rows — the bot phone is in 125 groups, and stray rows from
the others still sit in daily_logs.
"""

import io
import json
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from PIL import Image
from psycopg import sql
from pydantic import BaseModel, Field

import database as db
from config import settings
from dashboard_auth import RateLimiter, current_user
from ingest_batch import next_slot, previous_slot
from sitetime import site_now, site_today

logger = logging.getLogger("site_bot")

# The page itself loads for anyone, so it can show the login form; everything
# on `api` needs a session. `router` is what main.py mounts.
router = APIRouter()
api = APIRouter(dependencies=[Depends(current_user)])

_STATIC = Path(__file__).parent / "dashboard"

_MAX_PAGE_SIZE = 200
_MAX_EXPORT_ROWS = 20_000
_THUMB_EDGE = 480


# ── What can be seen ──────────────────────────────────────────────────────────
#
# `columns` is every column a row's detail view shows; `list` is the subset in
# the table itself. `date` filters the from/to range; `search` is what the
# search box matches. `scope` picks which groups' rows are visible.

TABLES: dict[str, dict] = {
    "daily_logs": {
        "title": "Site logs",
        "scope": "site",
        "columns": ["log_date", "logged_at", "main_location", "sub_location",
                    "description", "manpower", "sender_name"],
        "list": ["log_date", "logged_at", "main_location", "sub_location",
                 "description", "manpower", "sender_name"],
        "date": "log_date",
        "order": "log_date DESC, logged_at DESC",
        "search": ["main_location", "sub_location", "description", "sender_name"],
    },
    "dwall_panels": {
        "title": "D-wall panels",
        "scope": "site",
        "columns": ["panel_number", "panel_group", "panel_size", "report_date",
                    "engineer_initials", "entry_number", "guide_wall_level",
                    "cut_off_level", "design_toe_level", "design_depth",
                    "final_depth", "rock_hit", "excavation_start", "excavation_end",
                    "koden_start", "koden_end", "desanding_start", "desanding_end",
                    "water_stop_start", "water_stop_end", "rebar_cage_start",
                    "rebar_cage_end", "tremie_pipe_start", "tremie_pipe_end",
                    "casting_start", "casting_end", "theo_volume", "actual_volume",
                    "overbreak_pct", "downtime", "notes", "created_at"],
        "list": ["panel_number", "panel_group", "panel_size", "report_date",
                 "final_depth", "excavation_start", "casting_end",
                 "actual_volume", "overbreak_pct"],
        # report_date is free text ('20/02/26'), so the range filters on when
        # the panel was recorded instead.
        "date": "created_at",
        "order": "created_at DESC",
        "search": ["panel_number", "panel_group", "engineer_initials", "notes"],
    },
    "tunnel_progress": {
        "title": "Tunnel progress",
        "scope": "tunnel",
        "columns": ["report_date", "contract", "drive", "shift", "rings_built",
                    "current_ring", "total_rings", "pct_complete", "tbm_location",
                    "instrumentation", "issues", "follow_on_works",
                    "soil_disposal", "on_site_storage", "sender_name", "sent_at"],
        "list": ["report_date", "contract", "drive", "shift", "rings_built",
                 "current_ring", "total_rings", "pct_complete", "tbm_location",
                 "issues"],
        "date": "report_date",
        "order": "report_date DESC, contract, drive, shift",
        "search": ["contract", "drive", "tbm_location", "instrumentation", "issues"],
    },
    "tunnel_flags": {
        "title": "Flagged items",
        "scope": "tunnel",
        "columns": ["sent_date", "sent_at", "contract", "drive", "shift",
                    "report_date", "flag_text", "sender_name"],
        "list": ["sent_date", "contract", "drive", "shift", "flag_text",
                 "sender_name"],
        "date": "sent_date",
        "order": "sent_at DESC",
        "search": ["contract", "drive", "flag_text", "sender_name"],
    },
}


def _scope_condition(scope: str, alias: str = "") -> tuple[sql.Composable, list]:
    """The WHERE clause that confines a table to the groups the dashboard may
    show. Unconfigured means FALSE — see the module docstring."""
    column = sql.Identifier(alias, "group_id") if alias else sql.Identifier("group_id")
    if scope == "site":
        gid = settings.DASHBOARD_SITE_GROUP_ID.strip()
        if gid:
            return sql.SQL("{} = %s").format(column), [gid]
    elif scope == "tunnel":
        gids = sorted(settings.tunnel_group_ids)
        if gids:
            return sql.SQL("{} = ANY(%s)").format(column), [gids]
    return sql.SQL("FALSE"), []


def build_query(name: str, q: str = "", date_from: date | None = None,
                date_to: date | None = None) -> tuple[sql.Composable, sql.Composable, list]:
    """(SELECT ... WHERE ..., the WHERE clause alone, params) for a table.

    Identifiers come from TABLES only and are quoted by psycopg; the request
    supplies values, which are bound as parameters.
    """
    cfg = TABLES[name]
    cond, params = _scope_condition(cfg["scope"])
    conditions = [cond]

    if q.strip():
        pattern = f"%{q.strip()}%"
        conditions.append(sql.SQL("({})").format(sql.SQL(" OR ").join(
            sql.SQL("{} ILIKE %s").format(sql.Identifier(c)) for c in cfg["search"])))
        params += [pattern] * len(cfg["search"])
    if date_from:
        conditions.append(sql.SQL("{}::date >= %s").format(sql.Identifier(cfg["date"])))
        params.append(date_from)
    if date_to:
        conditions.append(sql.SQL("{}::date <= %s").format(sql.Identifier(cfg["date"])))
        params.append(date_to)

    where = sql.SQL(" AND ").join(conditions)
    select = sql.SQL("SELECT {cols} FROM {table} WHERE {where} ORDER BY {order}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cfg["columns"]),
        table=sql.Identifier(name),
        where=where,
        # A fixed string from TABLES, never from the request.
        order=sql.SQL(cfg["order"]),
    )
    return select, where, params


def _table_or_404(name: str) -> dict:
    if name not in TABLES:
        raise HTTPException(status_code=404, detail="No such table")
    return TABLES[name]


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC / "index.html")


# ── Data ──────────────────────────────────────────────────────────────────────

@api.get("/api/meta")
def meta():
    return {
        "tables": {name: {k: cfg[k] for k in ("title", "columns", "list", "date")}
                   for name, cfg in TABLES.items()},
        "site_configured": bool(settings.DASHBOARD_SITE_GROUP_ID.strip()),
        "tunnel_configured": bool(settings.tunnel_group_ids),
    }


@api.get("/api/overview")
def overview():
    site, site_params = _scope_condition("site")
    tunnel, tunnel_params = _scope_condition("tunnel")

    def one(query: sql.Composable, params: list) -> dict:
        rows = db._fetch(query, tuple(params))
        return rows[0] if rows else {}

    logs = one(sql.SQL(
        "SELECT count(*) AS total, min(log_date) AS first, max(log_date) AS last, "
        "count(*) FILTER (WHERE log_date >= (now() AT TIME ZONE 'Asia/Singapore')::date - 6) AS last_7_days "
        "FROM daily_logs WHERE {}").format(site), site_params)
    panels = one(sql.SQL("SELECT count(*) AS total FROM dwall_panels WHERE {}")
                 .format(site), site_params)
    photos = one(sql.SQL("SELECT count(*) AS total FROM site_photos WHERE {}")
                 .format(site), site_params)
    flags = one(sql.SQL(
        "SELECT count(*) AS total, count(*) FILTER (WHERE sent_date >= (now() AT TIME ZONE 'Asia/Singapore')::date - 6) "
        "AS last_7_days FROM tunnel_flags WHERE {}").format(tunnel), tunnel_params)

    per_day = db._fetch(sql.SQL(
        "SELECT log_date AS day, count(*) AS entries FROM daily_logs "
        "WHERE {} AND log_date >= (now() AT TIME ZONE 'Asia/Singapore')::date - 29 GROUP BY log_date ORDER BY log_date"
    ).format(site), tuple(site_params))

    # The latest report for each drive. current_ring is a running total, so
    # the newest row is the drive's position; rings_built is per shift and
    # would need summing, which this view does not attempt.
    drives = db._fetch(sql.SQL(
        "SELECT DISTINCT ON (contract, drive) contract, drive, report_date, shift, "
        "current_ring, total_rings, pct_complete, tbm_location, issues "
        "FROM tunnel_progress WHERE {} "
        "ORDER BY contract, drive, report_date DESC, sent_at DESC"
    ).format(tunnel), tuple(tunnel_params))

    recent_flags = db._fetch(sql.SQL(
        "SELECT sent_date, contract, drive, flag_text FROM tunnel_flags "
        "WHERE {} ORDER BY sent_at DESC LIMIT 8").format(tunnel), tuple(tunnel_params))

    top_locations = db._fetch(sql.SQL(
        "SELECT main_location AS location, count(*) AS entries FROM daily_logs "
        "WHERE {} AND log_date >= (now() AT TIME ZONE 'Asia/Singapore')::date - 29 "
        "GROUP BY main_location ORDER BY entries DESC LIMIT 8").format(site),
        tuple(site_params))

    # The record moves at the batch slots, not as posts arrive (ingest_batch),
    # so the page shows when it last moved and when it next will. Unlike the
    # groups' /ask, the dashboard never runs a catch-up of its own: a public
    # page should not be able to trigger parsing, and a 6-hourly record is
    # what it is meant to show.
    now = site_now()
    waiting = db._fetch(sql.SQL(
        "SELECT count(*) AS n FROM pending_messages WHERE status = 'pending' "
        "AND ({} OR {})").format(site, tunnel), tuple(site_params + tunnel_params))

    return {
        "updated": {"last": previous_slot(now).isoformat(),
                    "next": next_slot(now).isoformat(),
                    "waiting": waiting[0]["n"] if waiting else 0},
        "logs": logs, "panels": panels, "photos": photos, "flags": flags,
        "per_day": per_day, "drives": drives, "recent_flags": recent_flags,
        "top_locations": top_locations,
    }


@api.get("/api/table/{name}")
def table(name: str, q: str = "", date_from: date | None = Query(None, alias="from"),
          date_to: date | None = Query(None, alias="to"),
          page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=_MAX_PAGE_SIZE)):
    _table_or_404(name)
    select, where, params = build_query(name, q, date_from, date_to)
    count = db._fetch(sql.SQL("SELECT count(*) AS n FROM {} WHERE {}")
                      .format(sql.Identifier(name), where), tuple(params))
    rows = db._fetch(sql.SQL("{} LIMIT %s OFFSET %s").format(select),
                     tuple(params + [size, (page - 1) * size]))
    return {"total": count[0]["n"] if count else 0, "page": page, "size": size,
            "rows": rows}


def _cell(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


@api.get("/api/table/{name}/export")
def export(name: str, q: str = "", date_from: date | None = Query(None, alias="from"),
           date_to: date | None = Query(None, alias="to")):
    cfg = _table_or_404(name)
    select, _, params = build_query(name, q, date_from, date_to)
    rows = db._fetch(sql.SQL("{} LIMIT %s").format(select),
                     tuple(params + [_MAX_EXPORT_ROWS]))

    wb = Workbook()
    ws = wb.active
    ws.title = cfg["title"][:31]
    ws.append([c.replace("_", " ").title() for c in cfg["columns"]])
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for row in rows:
        ws.append([_cell(row.get(c)) for c in cfg["columns"]])
    # Fit each column to its widest value in the first 500 rows, capped so a
    # long description does not make the sheet unreadably wide.
    for i, col in enumerate(cfg["columns"], start=1):
        width = max([len(col)] + [min(len(str(r.get(col) or "")), 60) for r in rows[:500]])
        ws.column_dimensions[get_column_letter(i)].width = width + 2
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    filename = f"{name}_{site_today().isoformat()}.xlsx"
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Photos ────────────────────────────────────────────────────────────────────

@api.get("/api/photos")
def photo_list(q: str = "", date_from: date | None = Query(None, alias="from"),
               date_to: date | None = Query(None, alias="to"),
               page: int = Query(1, ge=1), size: int = Query(48, ge=1, le=_MAX_PAGE_SIZE)):
    cond, params = _scope_condition("site", alias="p")
    conditions = [cond]
    if q.strip():
        conditions.append(sql.SQL(
            "(p.caption ILIKE %s OR d.main_location ILIKE %s OR d.sub_location ILIKE %s "
            "OR d.description ILIKE %s OR p.sender_name ILIKE %s)"))
        params += [f"%{q.strip()}%"] * 5
    if date_from:
        conditions.append(sql.SQL("(p.sent_at AT TIME ZONE 'Asia/Singapore')::date >= %s"))
        params.append(date_from)
    if date_to:
        conditions.append(sql.SQL("(p.sent_at AT TIME ZONE 'Asia/Singapore')::date <= %s"))
        params.append(date_to)
    where = sql.SQL(" AND ").join(conditions)
    base = sql.SQL("FROM site_photos p LEFT JOIN daily_logs d ON d.id = p.log_id WHERE {}"
                   ).format(where)

    count = db._fetch(sql.SQL("SELECT count(*) AS n {}").format(base), tuple(params))
    rows = db._fetch(sql.SQL(
        "SELECT p.id, p.sent_at, p.sender_name, p.caption, p.width, p.height, "
        "d.log_date, d.main_location, d.sub_location, d.description "
        "{} ORDER BY p.sent_at DESC, p.id DESC LIMIT %s OFFSET %s").format(base),
        tuple(params + [size, (page - 1) * size]))
    return {"total": count[0]["n"] if count else 0, "page": page, "size": size,
            "rows": rows}


@api.get("/api/photo/{photo_id}")
def photo(photo_id: int, thumb: bool = False):
    cond, params = _scope_condition("site")
    rows = db._fetch(sql.SQL("SELECT image FROM site_photos WHERE id = %s AND {}")
                     .format(cond), tuple([photo_id] + params))
    if not rows:
        raise HTTPException(status_code=404, detail="No such photo")
    data = bytes(rows[0]["image"])
    if thumb:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((_THUMB_EDGE, _THUMB_EDGE))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=75)
        data = buf.getvalue()
    # A stored photo never changes, so the browser may keep it — privately:
    # it is behind a login, so no shared cache may.
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


# ── Ask ───────────────────────────────────────────────────────────────────────

# Questions per person per 10 minutes, and from everyone per day.
_limiter = RateLimiter(settings.DASHBOARD_ASK_PER_VISITOR, 600,
                       settings.DASHBOARD_ASK_PER_DAY)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    scope: str = Field("site", pattern="^(site|tunnel)$")


@api.post("/api/ask")
def ask(body: AskRequest, user: dict = Depends(current_user)):
    refusal = _limiter.allow(user["email"])
    if refusal:
        raise HTTPException(status_code=429, detail=refusal)

    question = body.question.strip()
    try:
        if body.scope == "tunnel":
            if not settings.tunnel_group_ids:
                raise HTTPException(status_code=503, detail="Tunnel groups are not set up.")
            from tunnel_ai import answer_tunnel_query
            # The director's view: every tunnel group. The scope is any one of
            # them; all_contracts lifts it to all of TUNNEL_GROUP_IDS' data.
            gid = sorted(settings.tunnel_group_ids)[0]
            return {"kind": "text",
                    "answer": answer_tunnel_query(gid, question, all_contracts=True)}

        gid = settings.DASHBOARD_SITE_GROUP_ID.strip()
        if not gid:
            raise HTTPException(status_code=503, detail="The site group is not set up.")

        import photos
        if photos.wants_photos(question):
            sections, period, capped = photos.find_photos(gid, question)
            return {
                "kind": "photos", "period": period, "capped": capped,
                "sections": [
                    {"title": title,
                     "photos": [{k: p.get(k) for k in ("id", "sent_at", "sender_name",
                                                       "caption", "main_location",
                                                       "sub_location", "description")}
                                for p in items]}
                    for title, items in sections
                ],
            }

        from ai_handler import answer_query
        return {"kind": "text", "answer": answer_query(gid, question)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("dashboard /ask failed: %r", question)
        raise HTTPException(status_code=502,
                            detail="Couldn't answer that just now. Try rephrasing it.")


router.include_router(api)
