"""Photos from the site group: storing them, and finding them for /ask.

The bridge downloads every photo posted to an allowlisted group and sends it
here with its caption. Site-group photos are shrunk and stored in site_photos
as they arrive — WhatsApp's media links expire, so they cannot wait for the
batch run — and each is linked to its daily_logs row when the run files the
caption (database.link_site_photos).

"/ask show me the pictures of Exit 3 in the last two months" is answered by
finding_photos(): the model writes a SELECT over site_photos and daily_logs
that returns photo ids, and the photos come back as an Excel sheet, one per row.
"""

import io
import logging
import re

from PIL import Image, ImageOps

import database as db
import llm
from ai_handler import _SCHEMA as _SITE_SCHEMA, _extract_json
from sitetime import site_today

logger = logging.getLogger("site_bot")

# Stored size: enough to read a rebar tag or a crack, a fraction of the original.
_STORE_LONG_EDGE = 1600
_STORE_QUALITY = 80

# Most photos one reply will carry — per location, and in all. Each is ~150 KB
# in the sheet, so the total keeps the file inside what WhatsApp and the bridge
# will send.
MAX_PER_LOCATION = 100
MAX_PHOTOS = 300

_PHOTO_WORDS = re.compile(
    r"\b(photos?|pictures?|pics?|images?|snaps?|snapshots?)\b",
    re.I,
)


def wants_photos(question: str) -> bool:
    """Is this question asking for pictures rather than for an answer?"""
    return bool(_PHOTO_WORDS.search(question))


# ── Storing ───────────────────────────────────────────────────────────────────

def prepare_image(raw: bytes, long_edge: int = _STORE_LONG_EDGE,
                  quality: int = _STORE_QUALITY) -> tuple[bytes, int, int]:
    """Re-encode a photo as an upright JPEG no larger than `long_edge`.

    Phones record rotation in EXIF rather than in the pixels; exif_transpose
    applies it, so a portrait shot is not stored lying on its side. The EXIF
    itself is dropped with the re-encode.
    """
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((long_edge, long_edge))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), img.width, img.height


def store_photo(group_id: str, sender_name: str, sender_number: str,
                caption: str, raw: bytes, wa_message_id: str | None) -> bool:
    """Shrink and store one site-group photo. False if it was a repeat."""
    image, width, height = prepare_image(raw)
    return db.insert_site_photo(
        group_id=group_id, wa_message_id=wa_message_id,
        sender_name=sender_name, sender_number=sender_number,
        caption=caption or None, image=image, width=width, height=height,
    )


# ── Finding ───────────────────────────────────────────────────────────────────

_PHOTO_SCHEMA = _SITE_SCHEMA + """

TABLE site_photos  -- one row per photo posted to the group
    id             BIGINT        the photo's id — this is what you return
    sent_at        TIMESTAMPTZ   when it was posted (site time is Asia/Singapore)
    sender_name    TEXT
    caption        TEXT          the photo's own caption; NULL for most album photos
    log_id         UUID          the daily_logs row it belongs to (daily_logs.id);
                                 NULL if no log was posted with it
    (the image itself is in a column you must NEVER select)

  A photo's location and description are its log's: JOIN daily_logs ON
  daily_logs.id = site_photos.log_id. A photo's location is ONLY its log's
  main_location and sub_location, and its activity ONLY its log's
  description — never its caption."""

_PHOTO_SQL_SYSTEM = """You write ONE PostgreSQL SELECT that finds the photos a question asks for,
from a construction site's record. Someone else fetches the pictures and puts
each tab you list on its own tab of a spreadsheet.

{schema}

RULES
1. Emit exactly one SELECT. No INSERT/UPDATE/DELETE/DDL.
2. List the tabs the question asks for, in the order it names them. Each tab
   has a name, a location and an activity:
   - location: the place, exactly as asked ("Exit 3"), or null if none.
   - activity: the work asked about, as a few words or stems any of which in
     a description marks it ("waterproofing" -> ["waterproof", "membrane"]),
     or [] if none.
   "Exit 3 and U3-12" -> two tabs, locations only.
   "waterproofing at Exit 3" -> one tab, location and activity.
   "rebar photos" -> one tab, activity only, location null.
   "all photos from yesterday" -> one tab named for the date, e.g.
   "30 Sep 2026", location null, activity [].
3. Write one branch per tab, each tagged with that tab's name exactly as
   listed and its position, joined with UNION ALL:
     SELECT 'Exit 3' AS tab, 1 AS tab_order, site_photos.id AS photo_id,
            site_photos.sent_at
     FROM site_photos JOIN daily_logs ON daily_logs.id = site_photos.log_id
     WHERE (...) ...
   Return exactly those four columns — never the image column.
   Wrap each branch in parentheses and give it its own
   ORDER BY site_photos.sent_at LIMIT {per_location}.
4. A location is matched ONLY in main_location and sub_location:
     (daily_logs.main_location ILIKE '%Exit 3%' OR daily_logs.sub_location
      ILIKE '%Exit 3%')
   An activity is matched ONLY in description, with the tab's activity words:
     (daily_logs.description ILIKE '%waterproof%' OR daily_logs.description
      ILIKE '%membrane%')
   A tab with both needs both (AND). Never match on the caption, and never
   look for a location in the description or an activity in the locations.
5. Filter dates on site_photos.sent_at in site time:
     (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date >= '...'
   "The last two months" means from today minus two months up to today.
6. End with ORDER BY tab_order, sent_at.
7. Use NO bound parameters or placeholders; inline every literal.

Respond ONLY with JSON, no explanation, no code fences:
{{"sql": "...", "tabs": [{{"name": "Exit 3", "location": "Exit 3", "activity": []}}], "period": "a short phrase for the dates, e.g. '30 Jul - 30 Sep 2026'"}}

EXAMPLES   (dates come from the user turn — never from these examples)

"show me the pictures of Exit 3 and U3-12 in the last two months"   (if today is 2026-09-30)
{{"sql": "(SELECT 'Exit 3' AS tab, 1 AS tab_order, site_photos.id AS photo_id, site_photos.sent_at FROM site_photos JOIN daily_logs ON daily_logs.id = site_photos.log_id WHERE (daily_logs.main_location ILIKE '%Exit 3%' OR daily_logs.sub_location ILIKE '%Exit 3%') AND (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date >= '2026-07-30' ORDER BY site_photos.sent_at LIMIT {per_location}) UNION ALL (SELECT 'U3-12' AS tab, 2 AS tab_order, site_photos.id AS photo_id, site_photos.sent_at FROM site_photos JOIN daily_logs ON daily_logs.id = site_photos.log_id WHERE (daily_logs.main_location ILIKE '%U3-12%' OR daily_logs.sub_location ILIKE '%U3-12%') AND (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date >= '2026-07-30' ORDER BY site_photos.sent_at LIMIT {per_location}) ORDER BY tab_order, sent_at", "tabs": [{{"name": "Exit 3", "location": "Exit 3", "activity": []}}, {{"name": "U3-12", "location": "U3-12", "activity": []}}], "period": "30 Jul - 30 Sep 2026"}}

"pictures of waterproofing at Exit 3 this month"   (if today is 2026-09-30)
{{"sql": "(SELECT 'Exit 3 waterproofing' AS tab, 1 AS tab_order, site_photos.id AS photo_id, site_photos.sent_at FROM site_photos JOIN daily_logs ON daily_logs.id = site_photos.log_id WHERE (daily_logs.main_location ILIKE '%Exit 3%' OR daily_logs.sub_location ILIKE '%Exit 3%') AND (daily_logs.description ILIKE '%waterproof%' OR daily_logs.description ILIKE '%membrane%') AND (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date >= '2026-09-01' ORDER BY site_photos.sent_at LIMIT {per_location}) ORDER BY tab_order, sent_at", "tabs": [{{"name": "Exit 3 waterproofing", "location": "Exit 3", "activity": ["waterproof", "membrane"]}}], "period": "1 - 30 Sep 2026"}}

"rebar photos from last week"   (if today is 2026-09-30)
{{"sql": "(SELECT 'Rebar' AS tab, 1 AS tab_order, site_photos.id AS photo_id, site_photos.sent_at FROM site_photos JOIN daily_logs ON daily_logs.id = site_photos.log_id WHERE (daily_logs.description ILIKE '%rebar%' OR daily_logs.description ILIKE '%reinforcement%') AND (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date BETWEEN '2026-09-21' AND '2026-09-27' ORDER BY site_photos.sent_at LIMIT {per_location}) ORDER BY tab_order, sent_at", "tabs": [{{"name": "Rebar", "location": null, "activity": ["rebar", "reinforcement"]}}], "period": "21 - 27 Sep 2026"}}
"""

_TABLES = ("daily_logs", "site_photos")


def find_photos(group_id: str, question: str) -> tuple[list[tuple[str, list[dict]]], str, bool]:
    """([(location, its photos oldest first), ...] in the order asked, the
    period searched, whether a cap cut photos off).

    Every location asked about gets an entry, even with no photos, so the reply
    shows it was searched rather than missed. A photo matching two locations is
    in both. Synchronous: the model call and psycopg both block.
    """
    user = f"Today is {site_today().isoformat()}.\n\nQuestion:\n\"\"\"{question}\"\"\""
    system = _PHOTO_SQL_SYSTEM.format(schema=_PHOTO_SCHEMA,
                                      per_location=MAX_PER_LOCATION + 1)
    error = sql = None

    for _attempt in (1, 2):
        prompt = user
        if error:
            prompt += f"\n\nYour previous attempt failed. Fix it.\nQuery:\n{sql}\n\nPostgres said:\n{error}"
        plan = _extract_json(llm.generate(prompt, model=llm.SMART_MODEL, max_tokens=1500,
                                          system=system, json_output=True)) or {}
        sql = plan.get("sql")
        if not sql:
            return [], "", False
        try:
            # Confined to this group's rows by the database layer.
            rows, _ = db.run_readonly_query(
                sql, group_id, max_rows=MAX_PHOTOS * 2,
                tables=_TABLES, scope_to_group=True,
            )
        except db.QueryError as exc:
            logger.warning("/ask photos: query rejected: %s", exc)
            error = str(exc)
            continue
        return _sections(group_id, rows, plan)

    raise RuntimeError(f"photo query failed twice: {error}")


def _location_pattern(location: str) -> re.Pattern:
    """`location` as a whole place name: case-insensitive, spaces, hyphens and
    slashes interchangeable or absent ("U3-12" ~ "u3 12"), and not part of a
    longer name ("Exit 3" is not in "Exit 30")."""
    parts = [re.escape(p) for p in re.split(r"[\s\-/]+", location.strip()) if p]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s\-/]*".join(parts) + r"(?![A-Za-z0-9])", re.I)


def in_location(location: str, main: str, sub: str) -> bool:
    """Is `location` in this log's main_location or sub_location?

    Main and sub together count too: "Zone 4 P46" is filed either as main
    'Zone 4 P46' or as main 'Zone 4' + sub 'P46'."""
    pattern = _location_pattern(location)
    return any(pattern.search(text) for text in (main, sub, f"{main} {sub}") if text.strip())


def is_activity(words: list[str], description: str) -> bool:
    """Does this description mention any of the activity's words? Each must
    start a word, so "waterproof" finds "waterproofing" but "cast" does not
    find "forecast"."""
    for word in words:
        parts = [re.escape(p) for p in word.split()]
        if parts and re.search(r"(?<![A-Za-z0-9])" + r"\s+".join(parts), description, re.I):
            return True
    return False


def _tab_filters(plan: dict) -> dict[str, tuple[str | None, list[str]]]:
    """{tab name: (location or None, activity words)} in the order asked."""
    filters = {}
    for tab in plan.get("tabs") or []:
        if not isinstance(tab, dict) or not str(tab.get("name") or "").strip():
            continue
        location = str(tab.get("location") or "").strip() or None
        activity = [str(w).strip() for w in tab.get("activity") or [] if str(w).strip()]
        filters[str(tab["name"]).strip()] = (location, activity)
    return filters


def _sections(group_id: str, rows: list[dict], plan: dict):
    """Group the query's rows into one section per tab, capped.

    A photo stays on a tab only if its log matches what the tab asked for —
    whatever the model's SQL matched: the location in main_location or
    sub_location, the activity in the description, both when both were
    asked. A caption is never enough, nor is a photo with no log."""
    filters = _tab_filters(plan)
    by_tab: dict[str, list[int]] = {name: [] for name in filters}
    for row in rows:
        tab = str(row.get("tab") or "Photos").strip()
        if tab not in by_tab:
            by_tab[tab] = []          # a tab the model used but did not list
        if row.get("photo_id") is not None and row["photo_id"] not in by_tab[tab]:
            by_tab[tab].append(row["photo_id"])

    def keep(tab: str, log: tuple[str, str, str] | None) -> bool:
        # A tab the model did not list is held to its name as a location.
        location, activity = filters.get(tab, (tab, []))
        if location is None and not activity:
            return True               # dates only: nothing to check
        if log is None:
            return False
        main, sub, description = log
        return ((location is None or in_location(location, main, sub))
                and (not activity or is_activity(activity, description)))

    logs = db.get_site_photo_logs(group_id, sorted({i for ids in by_tab.values() for i in ids}))
    for tab, ids in by_tab.items():
        ids[:] = [i for i in ids if keep(tab, logs.get(i))]

    capped = False
    total = 0
    for tab, ids in by_tab.items():
        room = min(MAX_PER_LOCATION, MAX_PHOTOS - total)
        if len(ids) > room:
            capped = True
            del ids[room:]
        total += len(ids)

    wanted = sorted({i for ids in by_tab.values() for i in ids})
    photos_by_id = {p["id"]: p for p in db.get_site_photos(group_id, wanted)}
    sections = [
        (tab, [photos_by_id[i] for i in ids if i in photos_by_id])
        for tab, ids in by_tab.items()
    ]
    return sections, plan.get("period") or "", capped
