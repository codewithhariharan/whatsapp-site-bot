"""Documents on the web dashboard: PDFs and Excel workbooks people share.

Anyone can list and download them, like the rest of the record. Uploading
and deleting need a login: a public page that accepts files from strangers
would soon be hosting someone else's. Deleting is the uploader's or an admin's.

Narrow on purpose:

  - Only .pdf, .xlsx, .xlsm and .xls, and the bytes must match the extension
    (a PDF header, an Excel zip, an old-Excel OLE file). A renamed .exe is
    refused, not stored.
  - The file is written under a random name in DASHBOARD_DOCS_DIR; the name
    the visitor gave is only ever data in the table.
  - A per-file size cap, a cap on all documents together, and a floor of free
    disk below which uploads stop — the bot's database and logs share the disk.
  - Excel is always a download. A PDF opens in the browser's viewer.

The body of an upload is the raw file (not a multipart form), with its name
in the query string: no extra parsing dependency, and the size is enforced
while it streams in rather than after it has all arrived.
"""

import hashlib
import logging
import os
import re
import secrets
import shutil
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

import database as db
from config import settings
from dashboard_auth import RateLimiter, current_user, optional_user

logger = logging.getLogger("site_bot")

KINDS = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xls": "application/vnd.ms-excel",
}
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MB = 1024 * 1024

public = APIRouter()
api = APIRouter(dependencies=[Depends(current_user)])

# Uploads per person per 10 minutes, and from everyone per day.
_upload_limiter = RateLimiter(20, 600, 300)


def _dir() -> Path:
    path = Path(settings.DASHBOARD_DOCS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clean_filename(name: str) -> str:
    """The visitor's file name, safe to show and to offer back: no path, no
    control characters, at most 150 characters, extension kept."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip(" .")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:150 - len(ext) - 1] + dot + ext) if dot else name[:150]
    return name


def kind_of(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext if ext in KINDS else None


def content_matches(kind: str, path: Path) -> bool:
    """Whether the file's bytes are what its extension says."""
    with open(path, "rb") as f:
        head = f.read(8)
    if kind == "pdf":
        return head.startswith(b"%PDF-")
    if kind == "xls":
        return head == _OLE
    # xlsx / xlsm: a zip holding a workbook part.
    if not head.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return "xl/workbook.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def _stored_total() -> int:
    rows = db._fetch("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM dashboard_documents")
    return int(rows[0]["n"]) if rows else 0


def _who(user: dict) -> str:
    return (user.get("name") or "").strip() or user["email"].split("@")[0]


def _row(r: dict, user: dict | None) -> dict:
    mine = bool(user) and (user.get("is_admin") or r.get("uploaded_by_id") == user.get("id"))
    return {k: r.get(k) for k in ("id", "filename", "kind", "size_bytes", "note",
                                  "uploaded_by", "uploaded_at")} | {"can_delete": mine}


# ── Public ───────────────────────────────────────────────────────────────────

@public.get("/api/documents")
def list_documents(q: str = "", user: dict | None = Depends(optional_user)):
    params: tuple = ()
    where = ""
    if q.strip():
        where = "WHERE filename ILIKE %s OR note ILIKE %s OR uploaded_by ILIKE %s"
        params = (f"%{q.strip()}%",) * 3
    rows = db._fetch(
        "SELECT id, filename, kind, size_bytes, note, uploaded_by_id, uploaded_by, "
        f"uploaded_at FROM dashboard_documents {where} ORDER BY uploaded_at DESC, id DESC",
        params)
    return {"documents": [_row(r, user) for r in rows],
            "max_mb": settings.DASHBOARD_DOC_MAX_MB}


@public.get("/api/documents/{doc_id}")
def download(doc_id: int, download: bool = False):
    rows = db._fetch("SELECT filename, kind, storage_key FROM dashboard_documents "
                     "WHERE id = %s", (doc_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="No such document")
    r = rows[0]
    path = _dir() / r["storage_key"]
    if not path.is_file():
        logger.error("document %s is in the table but not on disk", doc_id)
        raise HTTPException(status_code=404, detail="That file is missing.")
    inline = r["kind"] == "pdf" and not download
    # RFC 6266: an ASCII fallback plus the real name, UTF-8 encoded.
    ascii_name = r["filename"].encode("ascii", "replace").decode().replace('"', "'")
    disposition = (f'{"inline" if inline else "attachment"}; filename="{ascii_name}"; '
                   f"filename*=UTF-8''{quote(r['filename'])}")
    return FileResponse(path, media_type=KINDS[r["kind"]],
                        headers={"Content-Disposition": disposition,
                                 "Cache-Control": "no-cache"})


# ── Logged in ────────────────────────────────────────────────────────────────

@api.post("/api/documents")
async def upload(request: Request, filename: str = Query(..., min_length=1, max_length=300),
                 note: str = Query("", max_length=300),
                 user: dict = Depends(current_user)):
    name = clean_filename(filename)
    kind = kind_of(name)
    if not name or not kind:
        raise HTTPException(status_code=400, detail="Only PDF and Excel files (.pdf, .xlsx, "
                                                    ".xlsm, .xls) can be uploaded.")

    limit = settings.DASHBOARD_DOC_MAX_MB * _MB
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413,
                            detail=f"That file is over {settings.DASHBOARD_DOC_MAX_MB} MB.")

    folder = _dir()
    if shutil.disk_usage(folder).free < settings.DASHBOARD_DOCS_MIN_FREE_MB * _MB:
        raise HTTPException(status_code=507, detail="The server is short of disk space; "
                                                    "uploads are paused.")
    if _stored_total() >= settings.DASHBOARD_DOCS_TOTAL_MB * _MB:
        raise HTTPException(status_code=507, detail="The document store is full. "
                                                    "Ask an admin to remove old files.")

    refusal = _upload_limiter.allow(user["email"])
    if refusal:
        raise HTTPException(status_code=429, detail=refusal)

    key = secrets.token_hex(16)
    path = folder / key
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "xb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"That file is over {settings.DASHBOARD_DOC_MAX_MB} MB.")
                digest.update(chunk)
                f.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="That file is empty.")
        if not content_matches(kind, path):
            raise HTTPException(status_code=400,
                                detail=f"That file isn't a real .{kind} — its contents don't match.")
        rows = db._fetch(
            "INSERT INTO dashboard_documents (filename, kind, size_bytes, sha256, storage_key, "
            "note, uploaded_by_id, uploaded_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, filename, kind, size_bytes, note, uploaded_by_id, uploaded_by, "
            "uploaded_at",
            (name, kind, size, digest.hexdigest(), key, note.strip() or None,
             user["id"], _who(user)))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    logger.info("dashboard: %s uploaded %r (%d bytes)", user["email"], name, size)
    return {"document": _row(rows[0], user)}


@api.delete("/api/documents/{doc_id}")
def delete(doc_id: int, user: dict = Depends(current_user)):
    rows = db._fetch("SELECT uploaded_by_id, storage_key, filename FROM dashboard_documents "
                     "WHERE id = %s", (doc_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="No such document")
    r = rows[0]
    if not (user.get("is_admin") or r["uploaded_by_id"] == user["id"]):
        raise HTTPException(status_code=403,
                            detail="Only the person who uploaded it, or an admin, can remove it.")
    db._execute("DELETE FROM dashboard_documents WHERE id = %s", (doc_id,))
    try:
        os.unlink(_dir() / r["storage_key"])
    except FileNotFoundError:
        pass
    logger.info("dashboard: %s removed document %r", user["email"], r["filename"])
    return {"ok": True}


router = APIRouter()
router.include_router(public)
router.include_router(api)
