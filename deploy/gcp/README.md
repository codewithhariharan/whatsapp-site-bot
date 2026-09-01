# Deploying to GCP

One `e2-small` VM in `asia-southeast1` running three containers via Compose,
with Caddy terminating TLS.

```
Meta Cloud API ──HTTPS──► Caddy :443 ──► api :8000
                                          │  ▲
WhatsApp group ──► bridge :8088 ──────────┘  │
                     ▲                       │
                     └───────────────────────┘
                       (compose network only)
```

## Why not Cloud Run

The API alone would run fine on Cloud Run. The bridge would not, and splitting
them costs more than it saves.

Baileys holds a persistent WebSocket to WhatsApp and treats `auth_info/` as
mutable local state. Cloud Run scales to zero and may run several instances of
one revision. Two instances sharing one WhatsApp session gets the device
force-unlinked — you lose messages and re-scan the QR. Pinning
`min-instances=1 --max-instances=1` plus a GCS FUSE mount for `auth_info` can
be made to work, but you pay for an always-on container anyway and inherit a
filesystem whose rename semantics Baileys' multi-file auth state does not
expect.

A VM gives a real disk, one process, and a stable IP for the webhook.

## Data and AI

| Concern  | Service                        | Auth                                    |
|----------|--------------------------------|-----------------------------------------|
| Database | Cloud SQL for PostgreSQL 16    | IAM database auth via the VM's SA       |
| Claude   | Vertex AI (`AnthropicVertex`)  | Application Default Credentials         |

Neither is meant to store a secret on the VM. The Cloud SQL Python Connector
authorises with the service account and handles TLS, so there is no database
password and no certificate to rotate; Vertex resolves credentials from ADC, so
there is no `ANTHROPIC_API_KEY`.

**Vertex requires a Model Garden grant, and that is a separate approval.** On an
org-managed project you will not have it by default, and without it every call
returns 404 — including Google's own Gemini models, so a 404 here says nothing
about your model ids being wrong. Check before assuming the deploy is broken:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/global/publishers/anthropic/models/claude-sonnet-4-6:rawPredict" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -d '{"anthropic_version":"vertex-2023-10-16","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

Until the grant lands, leave `VERTEX_PROJECT_ID` empty and set
`ANTHROPIC_API_KEY` — `ai_client.py` picks the backend from whichever is set and
rewrites the model ids to match (dated snapshots are `claude-haiku-4-5@20251001`
on Vertex and `claude-haiku-4-5` on the direct API). Switching back later is an
env change and a restart.

Two things that bite on first connect:

- The IAM database username is the service-account email with
  `.gserviceaccount.com` **stripped** — `site-bot-sa@PROJECT.iam`. Passing the
  full email fails with a "role does not exist" error that does not hint at
  the truncation.
- Creating the IAM user does not grant it anything. Connect once as `postgres`
  and `GRANT USAGE ON SCHEMA public` plus table privileges, or every query
  returns a permission error.

Model IDs on Vertex differ from the direct API: current models are unsuffixed
(`claude-sonnet-4-6`), older ones keep a version suffix with an `@`
(`claude-haiku-4-5@20251001`). `VERTEX_REGION` defaults to `global` because
per-region model availability varies and `asia-southeast1` does not serve every
model — check the Model Garden before pinning a specific region.

## Cost

`e2-small` + 10 GB pd-balanced + static IP in `asia-southeast1` lands around
SGD 25/month, plus roughly SGD 35/month for a `db-g1-small` Cloud SQL instance
with automated backups. Vertex AI is per-token on top. `e2-micro` is free-tier eligible but 1 GB is tight for Node +
Chromium-free Baileys + Python; memory pressure surfaces as an unexplained
reconnect loop rather than a clean OOM, so it is a bad place to save money.

## Steps

```bash
export PROJECT=your-project-id
./provision.sh                 # APIs, Artifact Registry, static IP, disk, VM, firewall
```

Point a DNS A record at the printed IP, then:

```bash
gcloud builds submit --config deploy/gcp/cloudbuild.yaml \
  --substitutions=_REGION=asia-southeast1,_REPO=site-bot
```

On the VM, create `.env` (from `.env.example`) and `bridge.env` (from
`baileys-bridge/.env.example`) with these production values:

| file         | key                  | value                                  |
|--------------|----------------------|----------------------------------------|
| `.env`       | `BAILEYS_BRIDGE_URL` | `http://bridge:8088`                   |
| `.env`       | `ALLOWED_GROUP_IDS`  | the CR106 group JID                    |
| `.env`       | `INSTANCE_CONNECTION_NAME` | `PROJECT:asia-southeast1:site-bot-db` |
| `.env`       | `DB_USER`            | `site-bot-sa@PROJECT.iam`              |
| `.env`       | `VERTEX_PROJECT_ID`  | your project ID                        |
| `bridge.env` | `PYTHON_INGEST_URL`  | `http://api:8000/baileys/incoming`     |
| `bridge.env` | `ALLOWED_GROUP_IDS`  | the same JID                           |
| both         | `BRIDGE_SHARED_SECRET` | the same random string               |

Then:

```bash
export REGISTRY=asia-southeast1-docker.pkg.dev/$PROJECT/site-bot
export BOT_DOMAIN=bot.example.com
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f bridge   # scan the QR once
```

Finally, set the Meta webhook URL to `https://$BOT_DOMAIN/webhook`.

## Secrets

The database and Vertex no longer need secrets — IAM covers both. What remains
in `env_file` is `WHATSAPP_TOKEN`, `APP_SECRET`, `WEBHOOK_VERIFY_TOKEN` and
`BRIDGE_SHARED_SECRET`, still plaintext on the VM disk. Acceptable for a
single-operator deployment; for anything shared, store them in Secret Manager
and fetch them in a startup script — the VM already has the `cloud-platform`
scope and the SA can be granted `roles/secretmanager.secretAccessor`.

Rotate `WHATSAPP_TOKEN` and `APP_SECRET` if they have ever been committed,
pasted into a chat, or included in a zip. The old Supabase service-role key
should be revoked in Supabase even though nothing references it any more.

## Getting the group JID

The bridge refuses to start without an allowlist. On first boot it logs every
group the account belongs to:

```bash
docker compose -f docker-compose.prod.yml logs bridge | grep '"msg":"group"'
```

Copy the `jid` for `CR106 LTA PJT (Site Work)` into `ALLOWED_GROUP_IDS` in both
env files and restart. As a first-boot stopgap you can set
`ALLOWED_GROUP_NAMES=CR106 LTA PJT (Site Work)` instead, but do not leave it
there: a group rename silently stops ingestion.
