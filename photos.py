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

# Most photos one reply will carry. Each is ~100 KB in the sheet, so this keeps
# the file comfortably inside what WhatsApp and the bridge will send.
MAX_PHOTOS = 150

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
  daily_logs.id = site_photos.log_id. Use a LEFT JOIN and also match
  site_photos.caption, so a photo whose caption was not filed as a log is not
  missed."""

_PHOTO_SQL_SYSTEM = """You write ONE PostgreSQL SELECT that finds the photos a question asks for,
from a construction site's record. Someone else fetches the pictures.

{schema}

RULES
1. Emit exactly one SELECT. No INSERT/UPDATE/DELETE/DDL.
2. Return exactly these columns, in this order:
     site_photos.id AS photo_id, site_photos.sent_at,
     daily_logs.main_location, daily_logs.sub_location,
     daily_logs.description, site_photos.caption, site_photos.sender_name
   and nothing else — never the image column.
3. Match a place in main_location, sub_location AND caption, case-insensitive,
   e.g. (daily_logs.main_location ILIKE '%Exit 3%' OR daily_logs.sub_location
   ILIKE '%Exit 3%' OR site_photos.caption ILIKE '%Exit 3%').
4. Filter dates on site_photos.sent_at in site time:
     (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date >= '...'
   "The last two months" means from today minus two months up to today.
5. ORDER BY site_photos.sent_at, and LIMIT {limit}.
6. Use NO bound parameters or placeholders; inline every literal.

Respond ONLY with JSON, no explanation, no code fences:
{{"sql": "SELECT ...", "description": "a short phrase naming what was found, e.g. 'Exit 3, 30 Jul - 30 Sep 2026'"}}

EXAMPLE   (dates come from the user turn — never from this example)

"show me the pictures of Exit 3 in the last two months"   (if today is 2026-09-30)
{{"sql": "SELECT site_photos.id AS photo_id, site_photos.sent_at, daily_logs.main_location, daily_logs.sub_location, daily_logs.description, site_photos.caption, site_photos.sender_name FROM site_photos LEFT JOIN daily_logs ON daily_logs.id = site_photos.log_id WHERE (daily_logs.main_location ILIKE '%Exit 3%' OR daily_logs.sub_location ILIKE '%Exit 3%' OR site_photos.caption ILIKE '%Exit 3%') AND (site_photos.sent_at AT TIME ZONE 'Asia/Singapore')::date >= '2026-07-30' ORDER BY site_photos.sent_at LIMIT {limit}", "description": "Exit 3, 30 Jul - 30 Sep 2026"}}
"""

_TABLES = ("daily_logs", "site_photos")


def find_photos(group_id: str, question: str) -> tuple[list[dict], str, bool]:
    """(photo rows with images, a description of the search, hit the cap).

    Synchronous: the model call and psycopg both block.
    """
    user = f"Today is {site_today().isoformat()}.\n\nQuestion:\n\"\"\"{question}\"\"\""
    system = _PHOTO_SQL_SYSTEM.format(schema=_PHOTO_SCHEMA, limit=MAX_PHOTOS + 1)
    error = sql = None

    for _attempt in (1, 2):
        prompt = user
        if error:
            prompt += f"\n\nYour previous attempt failed. Fix it.\nQuery:\n{sql}\n\nPostgres said:\n{error}"
        plan = _extract_json(llm.generate(prompt, model=llm.SMART_MODEL, max_tokens=500,
                                          system=system, json_output=True))
        sql = (plan or {}).get("sql")
        if not sql:
            return [], "", False
        try:
            # Confined to this group's rows by the database layer.
            rows, _ = db.run_readonly_query(
                sql, group_id, max_rows=MAX_PHOTOS + 1,
                tables=_TABLES, scope_to_group=True,
            )
        except db.QueryError as exc:
            logger.warning("/ask photos: query rejected: %s", exc)
            error = str(exc)
            continue

        capped = len(rows) > MAX_PHOTOS
        ids = [r["photo_id"] for r in rows[:MAX_PHOTOS] if r.get("photo_id") is not None]
        photos = db.get_site_photos(group_id, ids)
        details = {r["photo_id"]: r for r in rows}
        for photo in photos:
            photo.update({k: v for k, v in details.get(photo["id"], {}).items()
                          if k not in ("photo_id",) and v is not None})
        return photos, plan.get("description") or "", capped

    raise RuntimeError(f"photo query failed twice: {error}")
