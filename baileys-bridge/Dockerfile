# ── Baileys bridge (unofficial WhatsApp Web transport for group messaging) ─────
FROM node:20-slim

ENV NODE_ENV=production

WORKDIR /app

# Install production deps first for layer caching. Use npm ci when a lockfile is
# present, otherwise fall back to npm install.
COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

COPY index.js ./

# WhatsApp Web session is persisted here; mount a volume at this path so it
# survives restarts (otherwise you must re-scan the QR every deploy).
ENV AUTH_DIR=/app/auth_info
EXPOSE 8088

CMD ["node", "index.js"]
