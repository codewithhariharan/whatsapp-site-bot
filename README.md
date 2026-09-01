# WhatsApp Site Bot

A WhatsApp bot that turns a construction site's group chat into a structured reporting system. Site engineers send progress updates as ordinary WhatsApp messages; the bot uses Claude to parse the free-form text into structured logs, stores them in a database, and generates daily summaries and Excel reports on demand — no app, no forms, and no training required for the people on site.

## Highlights

- **Natural-language logging** — engineers write updates however they like; Claude (`claude-sonnet-4-6`) extracts location, description, and manpower into structured records.
- **Conversational queries** — `/ask when was Panel 39 cast?` runs natural-language Q&A over the full site history.
- **Automated reporting** — one-command daily summaries and monthly Excel exports (openpyxl), including a specialised D-Wall panel tracker.
- **Multi-tenant** — one bot number serves many sites; each WhatsApp group gets its own isolated dataset and configuration.
- **Group support** — the official Cloud API handles 1:1 chats; an optional Node/Baileys bridge extends the bot into WhatsApp groups.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI (async), Uvicorn |
| AI | Claude via Vertex AI |
| Messaging | Meta WhatsApp Cloud API + webhooks; Baileys (Node) bridge for groups |
| Data | Cloud SQL for PostgreSQL 16 (IAM auth) |
| Reporting | openpyxl (Excel generation) |
| Deployment | GCE VM + Docker Compose + Caddy (see `deploy/gcp/`) |

## Architecture

```
WhatsApp ──► Meta Cloud API ──► /webhook (FastAPI)
   ▲                                  │
   │                                  ▼
   │                          message_handler ──► commands (/daily, /excel, /ask …)
   │                                  │
   │                                  ▼
   └──── whatsapp_client ◄──── message_parser ──► Claude  (parse free-text)
                                      │
                                      ▼
                          Cloud SQL for PostgreSQL

Groups:  WhatsApp group ──► Baileys bridge (Node) ──► /webhook
```

Incoming webhooks are signature-verified against the Meta App Secret (HMAC-SHA256), and logging is pinned to `INFO` so service-role keys and tokens never reach the logs.

---

## Setup Guide

> **Group support:** The official Cloud API (this bot) only does 1:1 chats.
> To run the bot inside WhatsApp **groups**, see `baileys-bridge/README.md` —
> an optional unofficial companion service. (Unofficial = violates Meta's ToS;
> use a separate, throwaway number.)

## What You Need Before Starting

1. A **Meta Business Account** — business.facebook.com
2. A **dedicated phone number** (SIM not currently on WhatsApp)
3. A **Google Cloud project** with billing enabled
4. The **gcloud CLI**, authenticated (`gcloud auth login`)
5. Access to the Claude models in the **Vertex AI Model Garden**

---

## Step 1 — Cloud SQL (Database)

`deploy/gcp/provision.sh` creates the instance, database, and IAM user. Then
load the schema and grant the runtime account rights on it:

```bash
gcloud sql connect site-bot-db --user=postgres --database=sitebot < schema.sql
# then, still as postgres:
#   GRANT USAGE ON SCHEMA public TO "site-bot-sa@PROJECT.iam";
#   GRANT ALL ON ALL TABLES IN SCHEMA public TO "site-bot-sa@PROJECT.iam";
```

There is no database password: the Cloud SQL Python Connector authenticates
with the VM's service account. See `deploy/gcp/README.md` for the two things
that commonly fail on first connect.

---

## Step 2 — Meta Developer Setup (WhatsApp API)

1. Go to developers.facebook.com → My Apps → Create App → Business
2. Add **WhatsApp** product to your app
3. Under **WhatsApp → API Setup**:
   - Note your **Phone Number ID** → `WHATSAPP_PHONE_NUMBER_ID`
   - Note your **WhatsApp Business Account ID** → `WHATSAPP_BUSINESS_ACCOUNT_ID`
   - Click **Generate permanent token** (under System Users in Business Manager) → `WHATSAPP_TOKEN`
4. Go to **App Settings → Basic**
   - Copy **App Secret** → `APP_SECRET`
5. Make up a random string for `WEBHOOK_VERIFY_TOKEN` (e.g. `mysitebot2026`)

> Note: You must add a real phone number and verify it with Meta.
> The test number works for development.

---

## Step 3 — Deploy to GCP

See **`deploy/gcp/README.md`** for the full walkthrough. In short:

```bash
export PROJECT=your-project-id
./deploy/gcp/provision.sh          # APIs, service account, Cloud SQL, VM, firewall
gcloud builds submit --config deploy/gcp/cloudbuild.yaml
```

Then create `.env` and `bridge.env` on the VM from their examples and run
`docker compose -f deploy/gcp/docker-compose.prod.yml up -d`. Your webhook URL
is `https://<your-domain>/webhook`, served by Caddy with an automatic
Let's Encrypt certificate.

---

## Step 4 — Connect Webhook to Meta

1. Back in Meta Developer portal → WhatsApp → Configuration
2. Set **Callback URL**: `https://<your-domain>/webhook`
3. Set **Verify Token**: the same string you used for `WEBHOOK_VERIFY_TOKEN`
4. Click **Verify and Save**
5. Under **Webhook fields**, subscribe to **messages**

---

## Step 5 — Add Bot to WhatsApp Group

1. Add the bot's phone number to your WhatsApp group
2. Type `/help` in the group to confirm it's working

---

## Bot Commands

| Command | Description |
|---|---|
| `/setorder Zone1, Zone2, Zone3` | Set fixed location order for reports |
| `/daily` | Generate today's progress summary |
| `/reorder 3 1 2` | Reorder the daily summary by position |
| `/confirm` | Post the final daily report |
| `/excel` | Export this month's logs as Excel |
| `/excel Jan 2026` | Export a specific month |
| `/dwall` | Export D-Wall panel tracker as Excel |
| `/ask when was Panel 39 cast?` | Ask a question about site history |
| `/help` | Show this list |

## How Engineers Log Updates

Engineers just send their captions normally. The bot reads and stores them automatically, then replies ✅ Logged.

```
Main Location: Zone 3, S2-2
Sub Location: GL A-B/20, B2-23 & B2-25, Concourse Level, Column CO1-4
Description: Honeycomb rectification works in progress
Manpower: Worker – 1
```

The bot is flexible — engineers don't need to follow the exact format. Claude will parse the meaning.

---

## Sharing Across Multiple Contracts

Just add the same bot number to any other WhatsApp group. Each group has:
- Its own separate database (logs, location order, panel records)
- Its own `/setorder` configuration
- Its own Excel exports

No extra setup needed per group.

---

## Running Locally (for development)

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env values
uvicorn main:app --reload --port 8000
# Use ngrok to expose: ngrok http 8000
```
