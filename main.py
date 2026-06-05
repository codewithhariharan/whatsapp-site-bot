import hmac
import hashlib
import json
import logging
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from config import settings
from message_handler import handle_message

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("site_bot")

app = FastAPI(title="Site Bot")


# ── Webhook verification (Meta requires this on first setup) ──────────────────

@app.get("/webhook")
async def verify(request: Request):
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Incoming messages ─────────────────────────────────────────────────────────

async def _safe_handle(group_id: str, sender_name: str, sender_number: str, text: str):
    """Run a message handler in the background, logging any failure."""
    try:
        await handle_message(group_id, sender_name, sender_number, text)
    except Exception:
        logger.exception("handle_message failed for %s: %r", group_id, text)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    logger.debug("─── Incoming POST /webhook ───────────────────────────────")
    logger.debug("Headers:\n%s", "\n".join(f"  {k}: {v}" for k, v in request.headers.items()))
    try:
        logger.debug("Body:\n%s", json.dumps(json.loads(body), indent=2))
    except Exception:
        logger.debug("Body (raw): %s", body.decode(errors="replace"))
    logger.debug("──────────────────────────────────────────────────────────")

    # Verify the request is from Meta
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        settings.APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            # Map sender wa_id → display name (profile absent on status updates)
            name_map = {
                c["wa_id"]: c.get("profile", {}).get("name", c["wa_id"])
                for c in contacts
            }

            for msg in messages:
                # Skip delivery/read receipts — they have "status" not "type"
                if "status" in msg:
                    continue
                if msg.get("type") != "text":
                    continue  # Only process text messages (captions are text)

                sender_number = msg["from"]
                sender_name = name_map.get(sender_number, sender_number)
                text = msg["text"]["body"]

                # For group messages the conversation/group ID is in msg metadata.
                # WhatsApp Cloud API puts the group JID in the message context.
                # For 1-on-1, use the sender number as the "group_id".
                group_id = msg.get("context", {}).get("from") or sender_number

                # Acknowledge Meta immediately; do the real work after the
                # 200 is sent so the webhook never times out and gets retried.
                background_tasks.add_task(
                    _safe_handle, group_id, sender_name, sender_number, text
                )

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
