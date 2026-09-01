#!/usr/bin/env bash
# One-time GCP provisioning. Idempotent enough to re-run, but read it first.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT=your-gcp-project-id}"
REGION="${REGION:-asia-southeast1}"
ZONE="${ZONE:-${REGION}-a}"
REPO="${REPO:-site-bot}"
VM="${VM:-site-bot-vm}"

gcloud config set project "$PROJECT"

gcloud services enable \
  compute.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  sqladmin.googleapis.com \
  aiplatform.googleapis.com

# ── Service account ───────────────────────────────────────────────────────────
# One identity for the VM: it authenticates to Cloud SQL and to Vertex AI, so
# neither a database password nor an Anthropic API key is stored anywhere.
SA="site-bot-sa@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts create site-bot-sa \
  --display-name="WhatsApp site bot runtime" 2>/dev/null || echo "service account exists"

for ROLE in roles/cloudsql.client roles/cloudsql.instanceUser roles/aiplatform.user \
            roles/logging.logWriter roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}" --role="$ROLE" --condition=None --quiet >/dev/null
done

# ── Cloud SQL ─────────────────────────────────────────────────────────────────
# db-g1-small is the smallest tier that is not shared-core-throttled. The
# schema is tiny; the load is a handful of writes per minute plus one large
# read when someone runs /excel2.
gcloud sql instances create site-bot-db \
  --database-version=POSTGRES_16 \
  --tier=db-g1-small \
  --region="$REGION" \
  --storage-auto-increase \
  --backup-start-time=18:00 \
  --database-flags=cloudsql.iam_authentication=on 2>/dev/null || echo "sql instance exists"

gcloud sql databases create sitebot --instance=site-bot-db 2>/dev/null || true

# IAM database user. The username is the SA email WITHOUT the
# ".gserviceaccount.com" suffix — Cloud SQL truncates it and a full email is
# rejected at connect time with a confusing "role does not exist".
gcloud sql users create "site-bot-sa@${PROJECT}.iam" \
  --instance=site-bot-db --type=CLOUD_IAM_SERVICE_ACCOUNT 2>/dev/null || true

# ── Artifact Registry ─────────────────────────────────────────────────────────
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="WhatsApp site bot images" 2>/dev/null || echo "repo exists"

# ── Static IP ─────────────────────────────────────────────────────────────────
# Static, not ephemeral: Meta's webhook and your DNS A record both point here,
# and an ephemeral IP changes on every VM stop/start.
gcloud compute addresses create site-bot-ip --region="$REGION" 2>/dev/null || true
IP=$(gcloud compute addresses describe site-bot-ip --region="$REGION" --format='value(address)')
echo "Static IP: $IP  — point your DNS A record at this."

# ── Persistent disk for the WhatsApp session ──────────────────────────────────
# Separate from the boot disk so you can rebuild the VM without re-pairing.
gcloud compute disks create site-bot-data \
  --size=10GB --type=pd-balanced --zone="$ZONE" 2>/dev/null || true

# ── VM ────────────────────────────────────────────────────────────────────────
# e2-small (2 GB). e2-micro is free-tier but Baileys + Node + Python is tight
# and OOM shows up as a mystery reconnect loop, not a clear error.
gcloud compute instances create "$VM" \
  --zone="$ZONE" \
  --machine-type=e2-small \
  --image-family=cos-stable --image-project=cos-cloud \
  --address="$IP" \
  --disk="name=site-bot-data,device-name=botdata,mode=rw,auto-delete=no" \
  --scopes=cloud-platform \
  --service-account="$SA" \
  --tags=site-bot 2>/dev/null || echo "vm exists"

# ── Firewall ──────────────────────────────────────────────────────────────────
# 80 and 443 only. 8000 and 8088 stay closed: those services talk to each other
# over the Docker network and must not be reachable from the internet.
gcloud compute firewall-rules create site-bot-web \
  --allow=tcp:80,tcp:443 --target-tags=site-bot \
  --description="HTTP/HTTPS for Caddy" 2>/dev/null || echo "firewall rule exists"

cat <<NEXT

Provisioned. Next:
  0. Grant the DB user rights on the schema, then load it:
       gcloud sql connect site-bot-db --user=postgres --database=sitebot < ../../schema.sql
       # then, as postgres:
       #   GRANT ALL ON ALL TABLES IN SCHEMA public TO "site-bot-sa@$PROJECT.iam";
       #   GRANT USAGE ON SCHEMA public TO "site-bot-sa@$PROJECT.iam";
  1. Point DNS at $IP and set BOT_DOMAIN to that name.
  2. gcloud compute ssh $VM --zone=$ZONE
  3. Format+mount the data disk ONCE (skip if already done):
       sudo mkfs.ext4 -F /dev/disk/by-id/google-botdata
       sudo mkdir -p /mnt/disks/botdata && sudo mount /dev/disk/by-id/google-botdata /mnt/disks/botdata
  4. Copy .env and bridge.env up, then: docker compose -f docker-compose.prod.yml up -d
  5. docker compose logs -f bridge   # scan the pairing QR once
  6. Request access to the Claude models in Vertex AI Model Garden if you have
     not already — the first /ask will 403 until that is done.
NEXT
