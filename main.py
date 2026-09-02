import hmac
import logging
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from config import settings
from message_handler import handle_message

logging.basicConfig(
    # INFO (not DEBUG): DEBUG logs full request/response headers, which include
    # the database URL and the bridge secret. Keep secrets out of logs.
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("site_bot")

app = FastAPI(title="Site Bot")


# ── Incoming messages ─────────────────────────────────────────────────────────

async def _safe_handle(group_id: str, sender_name: str, sender_number: str, text: str):
    """Run a message handler in the background, logging any failure."""
    try:
        await handle_message(group_id, sender_name, sender_number, text)
    except Exception:
        logger.exception("handle_message failed for %s: %r", group_id, text)


# ── Incoming group messages from the Baileys bridge ───────────────────────────

@app.post("/baileys/incoming")
async def baileys_incoming(request: Request, background_tasks: BackgroundTasks):
    """Receive a normalized group message from the Baileys bridge service.

    This is the bot's only ingress. The Meta Cloud API webhook that used to
    serve 1:1 chats has been removed: the bot is used in groups, which the
    Cloud API cannot carry, so that path was dead weight holding live
    credentials.
    """
    secret = settings.BRIDGE_SHARED_SECRET
    provided = request.headers.get("X-Bridge-Secret", "")
    if not secret or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=403, detail="Invalid bridge secret")

    data = await request.json()
    group_id = data.get("group_id")
    text = (data.get("text") or "").strip()
    if not group_id or not text:
        return {"status": "ignored"}

    sender_number = data.get("sender_number", "")
    sender_name = data.get("sender_name") or sender_number

    background_tasks.add_task(_safe_handle, group_id, sender_name, sender_number, text)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
