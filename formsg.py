"""Documents from a FormSG form — the way in from WOG laptops.

The WOG network blocks uploading to the dashboard's address, but form.gov.sg
is open to it. So the form takes the files, and FormSG's webhook hands each
submission to POST /formsg/webhook, which files the attachments under the
dashboard's Documents (documents.store_bytes) — the same checks as an upload.

The form is a Storage-mode form, so the submission arrives encrypted to the
form's public key. Only FORMSG_SECRET_KEY (the form's secret key, which the
form's owner keeps; set on the VM, never committed) opens it. The format is
FormSG's SDK's (formsg-javascript-sdk, src/util/crypto.ts):

  - encryptedContent is "<submission public key>;<nonce>:<ciphertext>", all
    base64, a NaCl box from that one-off key to the form's key.
  - An attachment is not in the payload: attachmentDownloadUrls maps field id
    to a pre-signed S3 URL (valid one hour) serving JSON
    {"encryptedFile": {"submissionPublicKey", "nonce", "binary"}}, the same
    box. The decrypted response for that field carries the file name.
  - The X-FormSG-Signature header is "t=<epoch ms>,s=<submission>,f=<form>,
    v1=<sig>": an ed25519 signature by FormSG's production signing key over
    "<webhook URL>.<submission>.<form>.<epoch>". A signature older than five
    minutes is refused, against replay.

Who can send here: only FormSG (the signature), and only for our form
(FORMSG_FORM_ID). The endpoint answers 404 until both are configured.

FormSG wants a quick 2xx, so the reply goes back at once and the downloads
run afterwards. A retried delivery is harmless: each attachment is filed once
(`source` is unique).
"""

import base64
import json
import logging
import time

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from nacl.exceptions import BadSignatureError, CryptoError
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import VerifyKey

import documents
from config import settings

logger = logging.getLogger("site_bot")

router = APIRouter()

_MAX_AGE_MS = 5 * 60 * 1000
_MAX_BODY = 1024 * 1024          # the payload is text; attachments come by URL


# ── Crypto ───────────────────────────────────────────────────────────────────

def parse_signature_header(header: str) -> dict:
    parts = {}
    for item in header.split(","):
        key, sep, value = item.partition("=")
        if sep:
            parts[key.strip()] = value.strip()
    return parts


def verify_signature(header: str, url: str, public_key: str,
                     now_ms: int | None = None) -> dict:
    """The header's parts if FormSG signed this delivery for `url` within the
    last five minutes; raises ValueError otherwise."""
    parts = parse_signature_header(header or "")
    try:
        epoch, submission, form, sig = parts["t"], parts["s"], parts["f"], parts["v1"]
        epoch_ms = int(epoch)
    except (KeyError, ValueError):
        raise ValueError("malformed X-FormSG-Signature")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now_ms - epoch_ms) > _MAX_AGE_MS:
        raise ValueError("signature too old")
    base = f"{url}.{submission}.{form}.{epoch}".encode()
    try:
        VerifyKey(base64.b64decode(public_key)).verify(base, base64.b64decode(sig))
    except (BadSignatureError, ValueError, TypeError):
        raise ValueError("bad signature")
    return parts


def _box(secret_key: str, their_public: str) -> Box:
    return Box(PrivateKey(base64.b64decode(secret_key)),
               PublicKey(base64.b64decode(their_public)))


def decrypt_content(secret_key: str, encrypted: str) -> list[dict]:
    """The submission's responses: [{_id, question, fieldType, answer, ...}]."""
    try:
        their_public, rest = encrypted.split(";", 1)
        nonce, cipher = (base64.b64decode(p) for p in rest.split(":", 1))
        plain = _box(secret_key, their_public).decrypt(cipher, nonce)
    except (ValueError, CryptoError, TypeError):
        raise ValueError("could not decrypt the submission")
    responses = json.loads(plain.decode("utf-8"))
    if not isinstance(responses, list):
        raise ValueError("unexpected submission format")
    return responses


def decrypt_attachment(secret_key: str, payload: dict) -> bytes:
    f = payload["encryptedFile"]
    try:
        return _box(secret_key, f["submissionPublicKey"]).decrypt(
            base64.b64decode(f["binary"]), base64.b64decode(f["nonce"]))
    except (ValueError, CryptoError, TypeError):
        raise ValueError("could not decrypt an attachment")


# ── Reading the form ─────────────────────────────────────────────────────────
#
# The form's own wording decides what is what, so it needs no field ids here:
# a field whose question mentions a name is the submitter's name; one that
# mentions a note, description, remark or title is the note.

def _answer(responses: list[dict], words: tuple[str, ...]) -> str:
    for r in responses:
        if r.get("fieldType") in ("attachment", "email", "mobile"):
            continue
        q = (r.get("question") or "").lower()
        if any(w in q for w in words) and isinstance(r.get("answer"), str):
            return r["answer"].strip()
    return ""


def describe(responses: list[dict]) -> tuple[str, str]:
    """(uploaded_by, note) for the dashboard."""
    name = _answer(responses, ("name",))
    note = _answer(responses, ("note", "description", "remark", "title", "comment"))
    return (f"{name} (FormSG)" if name else "FormSG"), note


def attachment_names(responses: list[dict]) -> dict[str, str]:
    return {r["_id"]: r["answer"] for r in responses
            if r.get("fieldType") == "attachment" and r.get("answer") and r.get("_id")}


# ── Filing ───────────────────────────────────────────────────────────────────

def file_submission(data: dict) -> int:
    """Decrypt a verified submission and file its attachments. Returns how
    many were filed. Runs after the webhook has replied."""
    secret = settings.FORMSG_SECRET_KEY.strip()
    submission = data.get("submissionId", "?")
    try:
        responses = decrypt_content(secret, data["encryptedContent"])
    except (KeyError, ValueError):
        logger.exception("formsg: submission %s could not be read", submission)
        return 0
    names = attachment_names(responses)
    urls = data.get("attachmentDownloadUrls") or {}
    uploaded_by, note = describe(responses)

    filed = 0
    with httpx.Client(timeout=60) as client:
        for field_id, url in urls.items():
            name = names.get(field_id)
            if not name:
                logger.warning("formsg: %s has a file for unknown field %s", submission, field_id)
                continue
            try:
                r = client.get(url)
                r.raise_for_status()
                content = decrypt_attachment(secret, r.json())
            except Exception:
                logger.exception("formsg: %s — could not fetch %r", submission, name)
                continue
            if documents.store_bytes(name, content, note, uploaded_by,
                                     source=f"formsg:{submission}:{field_id}"):
                filed += 1
    logger.info("formsg: submission %s — %d of %d attachment(s) filed",
                submission, filed, len(urls))
    return filed


@router.post("/formsg/webhook", include_in_schema=False)
async def webhook(request: Request, background: BackgroundTasks):
    url = settings.FORMSG_WEBHOOK_URL.strip()
    if not (settings.FORMSG_SECRET_KEY.strip() and settings.FORMSG_FORM_ID.strip() and url):
        raise HTTPException(status_code=404)

    try:
        parts = verify_signature(request.headers.get("x-formsg-signature", ""), url,
                                 settings.FORMSG_SIGNING_PUBLIC_KEY)
    except ValueError as exc:
        logger.warning("formsg: refused a delivery — %s", exc)
        raise HTTPException(status_code=401, detail="Unauthorized")
    if parts["f"] != settings.FORMSG_FORM_ID.strip():
        logger.warning("formsg: refused a delivery for another form (%s)", parts["f"])
        raise HTTPException(status_code=403, detail="Wrong form")

    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HTTPException(status_code=413)
    try:
        data = json.loads(body)["data"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Bad payload")
    if data.get("submissionId") != parts["s"] or data.get("formId", parts["f"]) != parts["f"]:
        raise HTTPException(status_code=400, detail="Payload does not match its signature")

    background.add_task(file_submission, data)
    return {"ok": True}
