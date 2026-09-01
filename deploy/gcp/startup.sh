#!/bin/bash
# GCE startup script: installs Docker Engine + the Compose plugin, then mounts
# the persistent data disk. Runs on every boot; every step is idempotent.
set -euxo pipefail

if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
fi

# Persistent disk for the WhatsApp session. Format ONLY if it has no
# filesystem — formatting an already-populated disk would destroy the
# linked-device state and force a re-pair.
DISK=/dev/disk/by-id/google-botdata
if [ -b "$DISK" ]; then
  if ! blkid "$DISK" >/dev/null 2>&1; then
    mkfs.ext4 -F "$DISK"
  fi
  mkdir -p /mnt/disks/botdata
  grep -q '/mnt/disks/botdata' /etc/fstab \
    || echo "$DISK /mnt/disks/botdata ext4 discard,defaults,nofail 0 2" >> /etc/fstab
  mount -a
  mkdir -p /mnt/disks/botdata/auth_info
fi
