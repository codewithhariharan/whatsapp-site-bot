# WhatsApp Site Bot

A WhatsApp bot that turns a construction site's group chat into a structured reporting system. Site engineers send progress updates as ordinary WhatsApp messages; the bot uses Claude to parse the free-form text into structured logs, stores them in a database, and generates daily summaries and Excel reports on demand — no app, no forms, and no training required for the people on site.

## Highlights

- **Natural-language logging** — engineers write updates however they like; Claude (`claude-sonnet-4-6`) extracts location, description, and manpower into structured records.
- **Conversational queries** — `/ask when was Panel 39 cast?` runs natural-language Q&A over the full site history.
- **Automated reporting** — one-command daily summaries and monthly Excel exports (openpyxl), including a specialised D-Wall panel tracker.
- **Multi-tenant** — one bot number can serve many sites, each WhatsApp group with its own isolated dataset and configuration. Currently pinned to a single group (see below).
- **Group support** — the official Cloud API handles 1:1 chats; an optional Node/Baileys bridge extends the bot into WhatsApp groups.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI (async), Uvicorn |
| AI | Anthropic Claude (`claude-sonnet-4-6`) |
| Messaging | Meta WhatsApp Cloud API + webhooks; Baileys (Node) bridge for groups |
| Data | Supabase (PostgreSQL) |
| Reporting | openpyxl (Excel generation) |
| Deployment | Railway (auto-deploy from GitHub) |

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
                               Supabase (Postgres)

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
3. A **Railway account** (free) — railway.app
4. A **Supabase account** (free) — supabase.com
5. An **Anthropic API key** — console.anthropic.com

---

## Step 1 — Supabase (Database)

1. Go to supabase.com → New Project
2. Once created, go to **SQL Editor**
3. Paste the entire contents of `schema.sql` and run it
4. Go to **Project Settings → API**
   - Copy the **Project URL** → this is `SUPABASE_URL`
   - Copy the **service_role key** (not anon key) → this is `SUPABASE_KEY`

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

## Step 3 — Deploy to Railway

1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo
4. Go to **Variables** and add all values from `.env.example`:
   ```
   WHATSAPP_TOKEN=...
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_BUSINESS_ACCOUNT_ID=...
   WEBHOOK_VERIFY_TOKEN=mysitebot2026
   APP_SECRET=...
   SUPABASE_URL=...
   SUPABASE_KEY=...
   ANTHROPIC_API_KEY=...
   ```
5. Go to **Settings → Networking → Generate Domain**
   - Your webhook URL will be: `https://your-app.railway.app/webhook`

---

## Step 4 — Connect Webhook to Meta

1. Back in Meta Developer portal → WhatsApp → Configuration
2. Set **Callback URL**: `https://your-app.railway.app/webhook`
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

The bot is flexible — engineers don't need to follow the exact format. Claude
will parse the meaning. Two locations are the norm: the first is the main
location (the broad area), the second the sub location (the detail within it).

That header is *not* required, though. A message naming a single location
("Rebar fixing at Shaft B, 4 workers") is logged with that location as the main
location and a blank sub location, and a message with no location at all is
still logged, under `Unknown`. Only genuine chatter — greetings, "noted", leave
notices — is ignored.

---

## Which Group the Bot Serves

The bot currently answers in **one group only**: `CR106 LTA PJT (Site Work)`.
The bot phone can sit in any number of other groups — their messages are read
and dropped, and never reach the database.

The group is matched on its name (case- and whitespace-insensitive) and is set
in two places, which must agree:

| Where | Variable | Default |
|---|---|---|
| Bridge (`baileys-bridge/.env`) | `ALLOWED_GROUP_NAME` | `CR106 LTA PJT (Site Work)` |
| Python bot (`.env`) | `ALLOWED_GROUP_NAME` | `CR106 LTA PJT (Site Work)` |

The bridge filters first, at the transport edge; the Python check is a backstop
for a bridge that is misconfigured or running older code. Both default to the
group above, so neither `.env` needs the variable unless you're changing it.

**If the group is renamed in WhatsApp, update both values** or the bot goes
quiet.

### Serving more than one group

Set `ALLOWED_GROUP_NAME=` (empty) in both `.env` files. The bot then serves
every group its phone is in, and each group gets:
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
