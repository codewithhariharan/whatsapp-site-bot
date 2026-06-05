import os
import logging
import httpx
import mimetypes
from config import settings

logger = logging.getLogger("site_bot")

BASE_URL = f"https://graph.facebook.com/v19.0/{settings.WHATSAPP_PHONE_NUMBER_ID}"
HEADERS = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}

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
    """Log and raise if the WhatsApp API returned an error."""
    if response.status_code >= 400:
        logger.error("WhatsApp %s failed: HTTP %s — %s",
                     action, response.status_code, response.text)
        response.raise_for_status()


async def send_message(to: str, text: str):
    """Send a plain text message to a group or individual."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/messages",
            headers=HEADERS,
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
    _check(response, "send_message")


async def upload_media(file_bytes: bytes, filename: str) -> str:
    """Upload a file to WhatsApp media API and return media_id."""
    mime_type = _mime_for(filename)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/media",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            files={"file": (filename, file_bytes, mime_type)},
            data={"messaging_product": "whatsapp"},
        )
    _check(response, "upload_media")
    return response.json()["id"]


async def send_document(to: str, file_bytes: bytes, filename: str, caption: str = ""):
    """Upload a file then send it as a document message."""
    media_id = await upload_media(file_bytes, filename)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/messages",
            headers=HEADERS,
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": {
                    "id": media_id,
                    "filename": filename,
                    "caption": caption,
                },
            },
        )
    _check(response, "send_document")
