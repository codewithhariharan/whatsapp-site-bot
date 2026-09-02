"""Outbound WhatsApp messaging, via the Baileys bridge.

Every recipient is a group JID. The Meta Cloud API path that used to handle
1:1 chats has been removed — the bot is only used in groups, and the Cloud API
cannot send to one, so the two-transport split earned nothing and kept a live
access token in the process.
"""
import os
import base64
import logging
import httpx
import mimetypes
from config import settings

logger = logging.getLogger("site_bot")


async def _bridge_post(path: str, payload: dict, timeout: float = 30):
    """Send a request to the Baileys bridge."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.BAILEYS_BRIDGE_URL.rstrip('/')}{path}",
            headers={"X-Bridge-Secret": settings.BRIDGE_SHARED_SECRET},
            json=payload,
        )
    _check(response, f"bridge {path}")


# WhatsApp validates the upload's MIME type against a fixed allow-list and
# rejects application/octet-stream. mimetypes.guess_type() is platform-
# dependent (it returns None for .xlsx on a bare Linux container), so map the
# extensions we actually send explicitly rather than trusting the OS.
_MIME_BY_EXT = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _mime_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return (
        _MIME_BY_EXT.get(ext)
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )


def _check(response: httpx.Response, action: str):
    """Log and raise if the bridge returned an error."""
    if response.status_code >= 400:
        logger.error("WhatsApp %s failed: HTTP %s — %s",
                     action, response.status_code, response.text)
        response.raise_for_status()


async def send_message(to: str, text: str):
    """Send a plain text message into a group."""
    await _bridge_post("/send", {"to": to, "text": text})


async def send_document(to: str, file_bytes: bytes, filename: str, caption: str = ""):
    """Send a file as a document message, base64-encoded to the bridge."""
    # The bridge uploads the file to WhatsApp before it answers, so this
    # timeout has to cover the upload, not just the local POST. The full
    # /excel export is ~1.7 MB (~2.3 MB base64) and 30s is not enough
    # headroom for it on a slow link — a timeout here loses the document
    # silently, since the send already succeeded on WhatsApp's side.
    await _bridge_post("/send-document", timeout=180, payload={
        "to": to,
        "file_base64": base64.b64encode(file_bytes).decode(),
        "filename": filename,
        "mimetype": _mime_for(filename),
        "caption": caption,
    })
