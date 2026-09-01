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

## Cost

`e2-small` + 10 GB pd-balanced + static IP in `asia-southeast1` lands around
SGD 25/month. `e2-micro` is free-tier eligible but 1 GB is tight for Node +
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

`env_file` keeps secrets on the VM disk in plaintext. That is acceptable for a
single-operator deployment but not for anything shared. To move them into
Secret Manager, store each value as a secret and fetch them into the env files
in a startup script — the VM already has the `cloud-platform` scope.

Rotate `ANTHROPIC_API_KEY`, `SUPABASE_KEY` and `WHATSAPP_TOKEN` if they have
ever been committed, pasted into a chat, or included in a zip.

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
