/**
 * Baileys transport bridge for the WhatsApp site bot.
 *
 * Baileys drives a regular WhatsApp Web session (unofficial — violates Meta's
 * ToS, so run it on a SEPARATE phone number from the Cloud API bot). This
 * service does NOT contain any business logic: it is a pure transport adapter
 * that mirrors what Meta's webhook does for the 1:1 bot.
 *
 *   group message  ──> this bridge ──POST /baileys/incoming──> Python bot
 *   Python bot ──POST /send|/send-document──> this bridge ──> group
 *
 * All parsing, the database, Excel export and AI live in the Python service.
 */
import 'dotenv/config'
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import express from 'express'
import qrcode from 'qrcode-terminal'
import pino from 'pino'
import { rmSync } from 'node:fs'

const PORT = parseInt(process.env.PORT || '8088', 10)
const AUTH_DIR = process.env.AUTH_DIR || './auth_info'
const PYTHON_INGEST_URL = process.env.PYTHON_INGEST_URL // e.g. https://your-bot.up.railway.app/baileys/incoming
const SHARED_SECRET = process.env.BRIDGE_SHARED_SECRET

if (!PYTHON_INGEST_URL) throw new Error('PYTHON_INGEST_URL is required')
if (!SHARED_SECRET) throw new Error('BRIDGE_SHARED_SECRET is required')

const logger = pino({ level: process.env.LOG_LEVEL || 'info' })

const RECONNECT_DELAY_MS = 5000 // wait before reconnecting to avoid 405 rate-limiting

// If set, link via an 8-char pairing code instead of a QR scan. Digits only,
// with country code, no '+' or spaces (Baileys strips nothing for us).
const PAIRING_NUMBER = (process.env.PAIRING_NUMBER || '').replace(/[^0-9]/g, '')
let pairingRequested = false // ensure we only request one code per process

const MAX_TRACKED_IDS = 1000

// A Set that remembers only the most recent `max` ids, so these never grow
// without bound on a long-lived connection.
function boundedIdSet(max) {
  const ids = new Set()
  const order = []
  return {
    has: (id) => ids.has(id),
    // True if `id` is new (and now recorded); false if it was already present.
    add(id) {
      if (!id || ids.has(id)) return false
      ids.add(id)
      order.push(id)
      if (order.length > max) ids.delete(order.shift())
      return true
    },
  }
}

// Track the IDs of messages the bot itself sends, so its own replies (which come
// back through messages.upsert as fromMe) are skipped — while still logging
// messages a human types on the work phone (also fromMe, but not bot-sent).
const sentMessages = boundedIdSet(MAX_TRACKED_IDS)
function recordSentId(id) {
  sentMessages.add(id)
}

// WhatsApp can deliver the same message to a linked device more than once — a
// message typed on the work phone arrives as a local echo and again as the
// server-confirmed copy. Both carry the same key.id, but pushName is only
// populated on the later one, so the two were logged as separate rows with
// different sender_names (the raw number, then the display name).
const seenMessages = boundedIdSet(MAX_TRACKED_IDS)

// Shared socket handle. Reassigned on every (re)connect so the HTTP handlers
// below always use the live connection.
let sock = null
let connected = false
// Distinguishes "waiting for a human to enter the pairing code" (restarting
// won't help) from "the socket dropped" (restarting might). /health reports
// the former as healthy so a restart policy doesn't loop while we wait.
let linkState = 'connecting' // connecting | awaiting_link | open | closed

// ── Extract plain text from the many shapes a WhatsApp message can take ───────
function extractText(message) {
  if (!message) return ''
  if (message.conversation) return message.conversation
  if (message.extendedTextMessage?.text) return message.extendedTextMessage.text
  if (message.imageMessage?.caption) return message.imageMessage.caption
  if (message.documentMessage?.caption) return message.documentMessage.caption
  // Disappearing / view-once messages wrap the real payload one level deeper.
  if (message.ephemeralMessage?.message) return extractText(message.ephemeralMessage.message)
  if (message.viewOnceMessage?.message) return extractText(message.viewOnceMessage.message)
  if (message.viewOnceMessageV2?.message) return extractText(message.viewOnceMessageV2.message)
  return ''
}

// ── Forward an incoming group message to the Python bot ───────────────────────
async function forwardToPython({ group_id, sender_name, sender_number, text }) {
  try {
    const resp = await fetch(PYTHON_INGEST_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Bridge-Secret': SHARED_SECRET,
      },
      body: JSON.stringify({ group_id, sender_name, sender_number, text }),
    })
    if (!resp.ok) {
      logger.error(
        { status: resp.status, body: await resp.text() },
        'Python ingest rejected message',
      )
    }
  } catch (err) {
    logger.error({ err }, 'Failed to reach Python ingest endpoint')
  }
}

// ── Baileys connection lifecycle ──────────────────────────────────────────────
async function startSock() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  // Pin to the WhatsApp Web version WhatsApp currently serves. A stale version
  // is rejected with HTTP 405 during registration before the QR appears.
  const { version } = await fetchLatestBaileysVersion()
  logger.info({ version }, 'Using WhatsApp Web version')

  sock = makeWASocket({
    version,
    auth: state,
    logger: logger.child({ module: 'baileys' }),
    // printQRInTerminal is deprecated; we render the qr field ourselves below.
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', async (update) => {
    const { connection, qr, lastDisconnect } = update

    // A `qr` event means the socket is ready to link a device. With a pairing
    // number configured we request a code here (correct timing); otherwise we
    // fall back to rendering the QR.
    if (qr) {
      linkState = 'awaiting_link'
      if (PAIRING_NUMBER) {
        if (!pairingRequested) {
          pairingRequested = true
          try {
            const code = await sock.requestPairingCode(PAIRING_NUMBER)
            console.log(`\n┌──────────────────────────────────────────────┐`)
            console.log(`│  Pairing code for +${PAIRING_NUMBER}`)
            console.log(`│  ──> ${code}`)
            console.log(`│  On the bot phone: WhatsApp → Settings →`)
            console.log(`│  Linked devices → Link a device →`)
            console.log(`│  "Link with phone number instead" → enter code`)
            console.log(`└──────────────────────────────────────────────┘\n`)
          } catch (err) {
            logger.error({ err }, 'Failed to request pairing code')
          }
        }
      } else {
        console.log('\nScan this QR code with WhatsApp on the bot phone:')
        console.log('  (Linked devices → Link a device)\n')
        qrcode.generate(qr, { small: true })
      }
    }

    if (connection === 'open') {
      connected = true
      linkState = 'open'
      logger.info('WhatsApp connection open')
    }

    if (connection === 'close') {
      connected = false
      linkState = 'closed'
      const statusCode = lastDisconnect?.error?.output?.statusCode
      const loggedOut = statusCode === DisconnectReason.loggedOut
      logger.warn({ statusCode, loggedOut }, 'WhatsApp connection closed')
      if (loggedOut) {
        // Session was invalidated (unlinked). The stored creds are now dead
        // weight: Baileys will keep replaying them and never surface a pairing
        // code, so clear them and exit non-zero. Exiting is what makes the
        // failure visible — the process used to stay alive with a dead socket,
        // which kept the HTTP healthcheck green and stopped Railway's
        // restartPolicy from ever firing. On restart we boot with an empty
        // AUTH_DIR and print a fresh pairing code.
        logger.error(
          `Logged out — clearing ${AUTH_DIR} and exiting so the supervisor ` +
            'restarts us with a fresh pairing code.',
        )
        try {
          rmSync(AUTH_DIR, { recursive: true, force: true })
        } catch (err) {
          logger.error({ err }, `Failed to clear ${AUTH_DIR}`)
        }
        process.exit(1)
      } else {
        // Transient drop — reconnect after a short delay. Reconnecting
        // instantly hammers WhatsApp's servers and gets rate-limited (405),
        // which is itself reported as a connection failure → infinite loop.
        setTimeout(() => {
          startSock().catch((err) => logger.error({ err }, 'Reconnect failed'))
        }, RECONNECT_DELAY_MS)
      }
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return // only brand-new messages, not history sync

    for (const m of messages) {
      const jid = m.key?.remoteJid
      if (!jid) continue

      const isGroup = jid.endsWith('@g.us')
      const previewText = extractText(m.message).trim()
      logger.debug(
        { jid, fromMe: !!m.key.fromMe, isGroup, hasText: !!previewText },
        'message received',
      )

      // Skip the bot's OWN replies (their IDs are tracked) so it never reacts to
      // itself. A message a human types on the work phone is also fromMe but is
      // NOT in that set, so we let it through and it gets logged like any other.
      if (m.key.fromMe && sentMessages.has(m.key.id)) continue
      if (!isGroup) continue // groups only — 1:1 stays on Cloud API

      const text = previewText
      if (!text) continue

      // Drop repeat deliveries of a message we've already forwarded. Checked
      // last so the set only ever holds ids we actually acted on. A message
      // with no id can't be deduped — forward it rather than drop it.
      if (m.key.id && !seenMessages.add(m.key.id)) {
        logger.debug({ id: m.key.id, jid }, 'duplicate delivery, skipping')
        continue
      }

      // In a group, key.participant is the actual sender; remoteJid is the group.
      // For our own (work-phone) messages, fall back to the bot's own JID.
      const participant =
        m.key.participant || m.participant || (m.key.fromMe ? sock.user?.id || '' : '')
      // Strip any device suffix, e.g. "6588257614:12@s.whatsapp.net" -> "6588257614".
      const sender_number = participant.split('@')[0].split(':')[0]
      // pushName is absent on the first delivery of a work-phone message and
      // only set on the server-confirmed copy — which dedupe now discards. For
      // our own messages take the name off the socket so those rows stay
      // attributed to the display name rather than falling back to the number.
      const sender_name =
        m.pushName || (m.key.fromMe ? sock.user?.name : '') || sender_number

      await forwardToPython({
        group_id: jid,
        sender_name,
        sender_number,
        text,
      })
    }
  })

  return sock
}

// ── HTTP server: Python calls these to deliver replies into the group ─────────
const app = express()
app.use(express.json({ limit: '25mb' })) // Excel exports arrive base64-encoded

function requireSecret(req, res) {
  if (req.headers['x-bridge-secret'] !== SHARED_SECRET) {
    res.status(403).json({ error: 'invalid bridge secret' })
    return false
  }
  if (!connected || !sock) {
    res.status(503).json({ error: 'whatsapp not connected' })
    return false
  }
  return true
}

// Reports the WhatsApp socket, not just the HTTP server — an Express process
// with a dead socket is exactly the failure this bridge used to hide.
// 'awaiting_link' stays 200: it needs a human with the phone, not a restart.
app.get('/health', (_req, res) => {
  const healthy = connected || linkState === 'awaiting_link'
  res.status(healthy ? 200 : 503).json({
    status: healthy ? 'ok' : 'degraded',
    connected,
    state: linkState,
  })
})

app.post('/send', async (req, res) => {
  if (!requireSecret(req, res)) return
  const { to, text } = req.body || {}
  if (!to || typeof text !== 'string') {
    return res.status(400).json({ error: 'to and text are required' })
  }
  try {
    const sent = await sock.sendMessage(to, { text })
    recordSentId(sent?.key?.id)
    res.json({ status: 'sent' })
  } catch (err) {
    logger.error({ err, to }, 'sendMessage failed')
    res.status(502).json({ error: String(err?.message || err) })
  }
})

app.post('/send-document', async (req, res) => {
  if (!requireSecret(req, res)) return
  const { to, file_base64, filename, mimetype, caption } = req.body || {}
  if (!to || !file_base64 || !filename) {
    return res
      .status(400)
      .json({ error: 'to, file_base64 and filename are required' })
  }
  try {
    const sent = await sock.sendMessage(to, {
      document: Buffer.from(file_base64, 'base64'),
      fileName: filename,
      mimetype: mimetype || 'application/octet-stream',
      caption: caption || '',
    })
    recordSentId(sent?.key?.id)
    res.json({ status: 'sent' })
  } catch (err) {
    logger.error({ err, to }, 'send-document failed')
    res.status(502).json({ error: String(err?.message || err) })
  }
})

app.listen(PORT, () => {
  logger.info(`Bridge HTTP server listening on :${PORT}`)
})

startSock().catch((err) => {
  logger.error({ err }, 'Failed to start Baileys socket')
  process.exit(1)
})
