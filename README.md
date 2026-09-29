# WhatsApp Site Bot

A WhatsApp bot that turns a construction site's group chat into a structured reporting system. Site engineers send progress updates as ordinary WhatsApp messages; the bot uses Gemini to parse the free-form text into structured logs, stores them in a database, and generates daily summaries and Excel reports on demand — no app, no forms, and no training required for the people on site.

## Highlights

- **Natural-language logging** — engineers write updates however they like; Gemini extracts location, description and manpower into structured records. A message naming one location, or none, is still logged.
- **Conversational queries** — `/ask when was Panel 39 cast?` runs natural-language Q&A over the full site history.
- **Automated reporting** — daily summaries, a whole-history export, per-month Excel workbooks, and a specialised D-Wall panel tracker.
- **Single-group by design** — the bridge serves exactly one allowlisted group. See [Scope](#scope).

## Tech Stack

| Layer     | Technology                                                                    |
| --------- | ----------------------------------------------------------------------------- |
| Backend   | Python, FastAPI (async), Uvicorn                                              |
| AI        | Google Gemini — `gemini-2.5-flash` via Vertex AI, no API key; set in `.env` |
| Messaging | Baileys (Node) over WhatsApp Web                                              |
| Data      | Cloud SQL for PostgreSQL 15, via psycopg                                      |
| Reporting | openpyxl                                                                      |
| Hosting   | One Compute Engine VM running Docker Compose                                  |

## Architecture

```
WhatsApp group ──► Baileys bridge (Node) ──► /baileys/incoming (FastAPI)
   ▲                                                │
   │                                                ▼
   │                          message_handler ──► commands (/daily, /excel, /ask …)
   │                                                │
   │                                                ▼
   └──── whatsapp_client ◄──── message_parser ──► Gemini  (parse free-text)
                                                    │
                                                    ▼
                                        Cloud SQL for PostgreSQL
```

The bridge holds an outbound WebSocket to WhatsApp, so **nothing needs to be reachable from the internet**. Both containers bind to `127.0.0.1`; there is no load balancer, no TLS terminator and no public ingress. `/baileys/incoming` is authenticated with a shared secret.

There is no Meta Cloud API path. It was removed: the bot is used in groups, which the Cloud API cannot carry, so it was dead weight holding a live access token.

## Scope

The bridge **refuses to start without a group allowlist** and drops everything else before it reaches Python. This is deliberate — without it the bridge forwards every group the linked account belongs to, which on the live deployment was 125 groups and put stray rows into the log table.

Match on JID (`ALLOWED_GROUP_IDS`), not name. Group subjects are not unique in practice — several groups on the live account are called `CR106 …` — and a rename silently stops ingestion.

This makes the bot **single-tenant**. Serving a second site means a second deployment with its own database, or reworking the schema's `group_id` scoping into something the allowlist and the commands both understand.

## Setup

### 1 — Database

Create a PostgreSQL database and apply the schema:

```bash
gcloud sql connect <instance> --user=postgres --database=whatsapp_bot < schema.sql
```

Put the connection string in `.env` as `DATABASE_URL`. On GCP, prefer a private IP over VPC peering so the instance has no public address.

### 2 — Configure

```bash
cp .env.example .env                              # DATABASE_URL, GOOGLE_CLOUD_PROJECT, bridge secret
cp baileys-bridge/.env.example baileys-bridge/.env # ingest URL, same secret, group allowlist
```

`BRIDGE_SHARED_SECRET` must be identical in both files.

### 3 — Run

```bash
docker compose up -d --build
docker compose logs -f bridge     # link the device once, via QR or PAIRING_NUMBER
```

### 4 — Find the group JID

The bridge logs every group it can see on connect:

```bash
docker compose logs bridge | grep '"msg":"group"'
```

Copy the `jid` into `ALLOWED_GROUP_IDS` in `baileys-bridge/.env` and restart the bridge.

The linked-device session lives in the `baileys_auth` volume, so it survives rebuilds — you only link once.

## Bot Commands

| Command                         | Description                                         |
| ------------------------------- | --------------------------------------------------- |
| `/setorder Zone1, Zone2, Zone3` | Set fixed location order for reports                |
| `/daily`                        | Generate today's progress summary                   |
| `/reorder 3 1 2`                | Reorder the daily summary by position               |
| `/delete 3 5 7`                 | Remove entries from the preview (session only)      |
| `/confirm`                      | Post the final daily report                         |
| `/excel`                        | Export the complete record as one flat sheet        |
| `/excel Jan 2026`               | Export a single month, pivoted by location and date |
| `/dwall`                        | Export D-Wall panel tracker as Excel                |
| `/ask when was Panel 39 cast?`  | Ask a question about site history                   |
| `/help`                         | Show this list                                      |

Bare `/excel` is flat rather than pivoted on purpose. The monthly layout puts locations down the side and dates across the top, which does not survive the full range: the history holds roughly 6,100 distinct main locations over 1,500 days, so a full-range pivot would be 214 weekly sheets thousands of rows deep. A named month keeps the pivot, where the location axis stays in the hundreds.

The whole-history export takes 30–100 seconds to build; the bot says "⏳ Generating…" first so the wait is visible.

## How Engineers Log Updates

Engineers just send their captions normally. The bot does not reply to each post: posts are held in `pending_messages` and logged in one pass at 00:00, 06:00, 12:00 and 18:00 Singapore time (`ingest_batch.py`). After each run the bot posts a single message listing any posts it could not log, so they can be resent; a run where everything logged stays silent. A post is filed under the day it arrived, not the day the run happened, so the midnight run does not move the evening's posts onto the next day.

Commands and `/ask` still answer immediately, and `/ask` first files the group's waiting posts so its answer is current. `/daily` and `/excel` see posts up to the last run. A plain question typed without `/ask` in a site group is not answered — use `/ask`.

```
Main Location: Zone 3, S2-2
Sub Location: GL A-B/20, B2-23 & B2-25, Concourse Level, Column CO1-4
Description: Honeycomb rectification works in progress
Manpower: Worker – 1
```

That header is **not** required. Two locations are the norm — the first is the main location (the broad area), the second the sub location (the detail within it) — but a message naming a single location (`Zone 1 P4: FBCM materials fabrication`) is logged with that as the main location and a blank sub location, and a message naming none is still logged, under `Unknown`. Missing manpower is not disqualifying; it is absent from essentially the entire history. Only genuine chatter — greetings, "noted", leave notices — is ignored.

## Tunnel groups

Each tunnel contract has its own WhatsApp group (`TUNNEL_GROUP_IDS`), and every one sends the same template:

```
TUNNEL PROGRESS UPDATE
CONTRACT: CR146
DATE: 04-JAN-2026
DRIVE: EB - Main Drive 3
PROGRESS: 0 / 225 / 888 (25.3%)
TBM LOCATION: at side table of AMK Ave 3
INSTRUMENTATION: LG3053 breached AL
ISSUES: [shift change]
```

It is parsed by rules, not a model (`tunnel_parser.py`), into `tunnel_progress` — one row per contract, report date and drive; a resend replaces it. `PROGRESS` is rings built that day / current ring / total rings (% as written). An update that cannot be read is listed in the run's failure message so it can be resent.

Any line containing a `!` — in an update or any other message in a tunnel group — is stored word for word in `tunnel_flags`, under the day it was sent.

The **master group** (`MASTER_GROUP_IDS`) holds the senior group director and the bot:

| Command                     | Description                                                  |
| --------------------------- | ------------------------------------------------------------ |
| `/!!`                       | Today's `!` items from every contract, grouped by contract    |
| `/!! yesterday`, `/!! 28 Sep`, `/!! monday` | Another day's                                 |
| any other message           | Answered as a question across every contract                 |
| `/excel`                    | Every contract's updates and `!` items as a spreadsheet       |

`/!!` and questions first file any waiting posts from the tunnel groups, so they are current. In a contract's own group, `/!!`, `/ask` and `/excel` cover that contract only.

## Backfilling history

`load_history.py` loads a spreadsheet that is already in the export's shape (`Day, Date, Main Location, Sub Location, Description / Activity, Manpower`) into `daily_logs`, deduplicating against what is already there on `(log_date, main_location, sub_location, description)`.

```bash
python load_history.py Site_Report_Full.xlsx            # dry run, reports counts
python load_history.py Site_Report_Full.xlsx --commit
```

Backfilled rows are marked in `raw_message` and carry no sender; `logged_at` is derived from `log_date` plus row position so the sheet's within-day ordering survives.

## Running Locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env      # fill in
pytest
uvicorn main:app --reload --port 8000
```

The test suite seeds dummy config in `tests/conftest.py` and makes no network calls, so it runs on a fresh checkout with no credentials.

## Known limitations

- **Parse failures are silent.** `message_handler` catches every exception from the parser and returns without logging, so an API error or malformed response is indistinguishable from a message that was deliberately ignored.
- **Video captions are not read.** `extractText()` handles text, image and document captions plus the ephemeral/view-once wrappers, but not `videoMessage.caption`.
- **Baileys occasionally cannot decrypt a message.** WhatsApp session state drifts; affected messages never reach the parser at all. Re-linking the device clears it.
- **`log_date` is the processing date**, not the message date, so a message handled after midnight lands on the following day.
