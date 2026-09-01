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
  secretmanager.googleapis.com

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
  --tags=site-bot 2>/dev/null || echo "vm exists"

# ── Firewall ──────────────────────────────────────────────────────────────────
# 80 and 443 only. 8000 and 8088 stay closed: those services talk to each other
# over the Docker network and must not be reachable from the internet.
gcloud compute firewall-rules create site-bot-web \
  --allow=tcp:80,tcp:443 --target-tags=site-bot \
  --description="HTTP/HTTPS for Caddy" 2>/dev/null || echo "firewall rule exists"

cat <<NEXT

Provisioned. Next:
  1. Point DNS at $IP and set BOT_DOMAIN to that name.
  2. gcloud compute ssh $VM --zone=$ZONE
  3. Format+mount the data disk ONCE (skip if already done):
       sudo mkfs.ext4 -F /dev/disk/by-id/google-botdata
       sudo mkdir -p /mnt/disks/botdata && sudo mount /dev/disk/by-id/google-botdata /mnt/disks/botdata
  4. Copy .env and bridge.env up, then: docker compose -f docker-compose.prod.yml up -d
  5. docker compose logs -f bridge   # scan the pairing QR once
NEXT
