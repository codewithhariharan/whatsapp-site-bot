import logging
import httpx
import mimetypes
from config import settings

logger = logging.getLogger("site_bot")

BASE_URL = f"https://graph.facebook.com/v19.0/{settings.WHATSAPP_PHONE_NUMBER_ID}"
HEADERS = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}


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
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "application/octet-stream"
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
