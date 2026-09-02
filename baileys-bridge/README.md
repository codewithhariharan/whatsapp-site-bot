# Baileys Bridge — WhatsApp group transport

The site bot's only transport. Meta's official Cloud API cannot serve WhatsApp
groups, so the bot talks to WhatsApp Web through Baileys instead.

It contains **no business logic**. All parsing, the database, Excel export and
AI stay in the Python service. This bridge only moves messages:

```
group message ──> bridge ──POST /baileys/incoming──> Python bot
Python bot ──POST /send | /send-document──> bridge ──> group
```

## ⚠️ Read before running

- Baileys drives a **real WhatsApp Web session** — this is **unofficial and
  violates Meta's Terms of Service**. The number can be **banned**. Don't use a
  number you care about.
- Add that number to the group like any normal member.

## Setup (local)

```bash
cd baileys-bridge
npm install
cp .env.example .env        # then edit the values
npm start
```

On first run a **QR code** prints in the terminal. On the bot phone:
**WhatsApp → Settings → Linked devices → Link a device** → scan it. Set
`PAIRING_NUMBER` instead to link with an 8-character code. The session is saved
in `AUTH_DIR` (`./auth_info`) so you only link once — persist that directory.

## Configuration

| Variable | Purpose |
|----------|---------|
| `PYTHON_INGEST_URL` | The Python bot's `/baileys/incoming` URL |
| `BRIDGE_SHARED_SECRET` | Shared secret; **must equal** `BRIDGE_SHARED_SECRET` in the Python `.env` |
| `ALLOWED_GROUP_IDS` | Comma-separated group JIDs to serve. **Required** (or `ALLOWED_GROUP_NAMES`) |
| `ALLOWED_GROUP_NAMES` | Match on group subject instead. Convenient, but fragile — see below |
| `PORT` | HTTP port the bridge listens on (default `8088`) |
| `AUTH_DIR` | Where the WhatsApp session is stored (default `./auth_info`) |
| `PAIRING_NUMBER` | Link by pairing code instead of QR; digits only |
| `LOG_LEVEL` | pino level (default `info`). Dropped messages log at `debug` |

## The allowlist is not optional

**The bridge throws on startup if neither allowlist variable is set.** Without
one it forwards every group the linked account belongs to — 125 of them on the
live deployment, which put stray rows across 11 unrelated chats into the log
table before this existed.

Prefer `ALLOWED_GROUP_IDS`. JIDs are stable; group subjects are not unique in
practice (several groups on the live account are named `CR106 …`) and a rename
silently stops ingestion.

To find the JID, watch the log on connect — the bridge lists every group it can
see:

```bash
docker compose logs bridge | grep '"msg":"group"'
```

## Message handling

Messages are dropped, in order, when they are: the bot's own reply (tracked by
`key.id`), not a group, not in the allowlist, empty after text extraction, or a
repeat delivery of an id already forwarded.

That last one matters: WhatsApp delivers a message typed on the **work phone**
twice — once as the local echo, once as the server-confirmed copy — and both
carry the same `key.id`. Only the second has `pushName`, so before dedupe each
one produced its own row, one attributed to a raw participant id and one to the
display name. For `fromMe` messages the sender name now falls back to the
socket's own user name so the surviving row keeps the display name.

`extractText()` reads `conversation`, `extendedTextMessage`, image and document
captions, and unwraps ephemeral / view-once messages. It does **not** read
`videoMessage.caption` — a caption typed on a video is invisible to the bot.

## Endpoints

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `GET` | `/health` | — | `{ status, connected, state }`; 503 when the socket is unusable, 200 while `awaiting_link` |
| `POST` | `/send` | `{ to, text }` | requires `X-Bridge-Secret` |
| `POST` | `/send-document` | `{ to, file_base64, filename, mimetype, caption }` | requires `X-Bridge-Secret` |

`/health` reports the **WhatsApp socket**, not just the HTTP server — an Express
process with a dead socket is exactly the failure this bridge used to hide.
`awaiting_link` stays 200 deliberately: it needs a human with the phone, not a
restart, so a healthcheck must not loop on it.

On logout the bridge clears `AUTH_DIR` and exits non-zero, so the supervisor
restarts it and Baileys prints a fresh pairing code. Keeping the dead
credentials is what previously stopped one from ever appearing — a session lost
on 2026-07-10 went unnoticed for 17 days.

## Deploying

Runs as the `bridge` service in `docker-compose.yml`. Mount a volume at
`AUTH_DIR` so the linked-device session survives rebuilds, and set
`PYTHON_INGEST_URL` to `http://api:8000/baileys/incoming` — on the compose
network the two services reach each other by name. `restart: unless-stopped`
provides the supervisor the logout path depends on.
