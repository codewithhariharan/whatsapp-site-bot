# Baileys Bridge — WhatsApp group support

A thin transport service that lets the (Cloud-API) site bot work in **WhatsApp
groups**, which Meta's official API cannot do.

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
- It must be a **different phone number** from your Cloud API bot. A number on
  the WhatsApp Business Platform cannot also run WhatsApp Web.
- Add that number to the group(s) like any normal member.

## Setup (local)

```bash
cd baileys-bridge
npm install
cp .env.example .env        # then edit the values
npm start
```

On first run a **QR code** prints in the terminal. On the bot phone:
**WhatsApp → Settings → Linked devices → Link a device** → scan it. The session
is saved in `AUTH_DIR` (`./auth_info`) so you only scan once.

## Configuration

| Variable | Purpose |
|----------|---------|
| `PYTHON_INGEST_URL` | Your Python bot's `/baileys/incoming` URL |
| `BRIDGE_SHARED_SECRET` | Shared secret; **must equal** `BRIDGE_SHARED_SECRET` in the Python `.env` |
| `PORT` | HTTP port the bridge listens on (default `8088`) |
| `AUTH_DIR` | Where the WhatsApp session is stored (default `./auth_info`) |
| `LOG_LEVEL` | pino level (default `info`) |

The Python service needs two matching variables (see its `.env.example`):

```
BAILEYS_BRIDGE_URL=http://localhost:8088      # the bridge's base URL
BRIDGE_SHARED_SECRET=<same secret as above>
```

## How routing works

`group_id` is the routing key. Group JIDs end in `@g.us`, so
`whatsapp_client.py` sends those replies to this bridge; bare phone numbers
still go through the Cloud API. The 1:1 bot is unaffected.

## Endpoints

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `GET` | `/health` | — | `{ status, connected }` |
| `POST` | `/send` | `{ to, text }` | requires `X-Bridge-Secret` |
| `POST` | `/send-document` | `{ to, file_base64, filename, mimetype, caption }` | requires `X-Bridge-Secret` |

## Deploying on Railway

Run this as a **second Railway service** pointing at the `baileys-bridge`
directory.

- Start command: `npm start` (or set root directory to `baileys-bridge`).
- **Mount a volume** at `AUTH_DIR` so the session survives redeploys — otherwise
  you must re-scan the QR on every deploy. You'll need to view the deploy logs
  once to scan the first QR.
- Set `PYTHON_INGEST_URL` to the Python service's public URL +
  `/baileys/incoming`, and use the same `BRIDGE_SHARED_SECRET` on both.
