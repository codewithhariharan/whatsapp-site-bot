# WhatsApp Site Bot — Setup Guide

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
