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

const PORT = parseInt(process.env.PORT || '8088', 10)
const AUTH_DIR = process.env.AUTH_DIR || './auth_info'
const PYTHON_INGEST_URL = process.env.PYTHON_INGEST_URL // e.g. https://your-bot.up.railway.app/baileys/incoming
const SHARED_SECRET = process.env.BRIDGE_SHARED_SECRET

if (!PYTHON_INGEST_URL) throw new Error('PYTHON_INGEST_URL is required')
if (!SHARED_SECRET) throw new Error('BRIDGE_SHARED_SECRET is required')

const logger = pino({ level: process.env.LOG_LEVEL || 'info' })

// ── Group allowlist ───────────────────────────────────────────────────────────
// Without this the bridge forwards EVERY group the linked account belongs to.
// Prefer ALLOWED_GROUP_IDS (JIDs are stable). ALLOWED_GROUP_NAMES matches the
// group subject instead, which is convenient but breaks if anyone renames the
// group — and two groups can share a name.
const ALLOWED_GROUP_IDS = new Set(
  (process.env.ALLOWED_GROUP_IDS || '')
    .split(',').map((s) => s.trim()).filter(Boolean),
)
const ALLOWED_GROUP_NAMES = new Set(
  (process.env.ALLOWED_GROUP_NAMES || '')
    .split(',').map((s) => s.trim().toLowerCase()).filter(Boolean),
)
if (!ALLOWED_GROUP_IDS.size && !ALLOWED_GROUP_NAMES.size) {
  throw new Error(
    'Set ALLOWED_GROUP_IDS or ALLOWED_GROUP_NAMES — refusing to start unfiltered.',
  )
}

// Cache jid -> subject so we do not call groupMetadata on every message
// (WhatsApp rate-limits it, and a throttled lookup would drop real logs).
const groupSubjects = new Map()

async function subjectFor(jid) {
  if (groupSubjects.has(jid)) return groupSubjects.get(jid)
  try {
    const meta = await sock.groupMetadata(jid)
    groupSubjects.set(jid, meta.subject || '')
    return meta.subject || ''
  } catch (err) {
    logger.warn({ err, jid }, 'groupMetadata lookup failed')
    return ''
  }
}

async function isAllowedGroup(jid) {
  if (ALLOWED_GROUP_IDS.has(jid)) return true
  if (!ALLOWED_GROUP_NAMES.size) return false
  const subject = await subjectFor(jid)
  return ALLOWED_GROUP_NAMES.has(subject.trim().toLowerCase())
}

const RECONNECT_DELAY_MS = 5000 // wait before reconnecting to avoid 405 rate-limiting

// If set, link via an 8-char pairing code instead of a QR scan. Digits only,
// with country code, no '+' or spaces (Baileys strips nothing for us).
const PAIRING_NUMBER = (process.env.PAIRING_NUMBER || '').replace(/[^0-9]/g, '')
let pairingRequested = false // ensure we only request one code per process

// Track the IDs of messages the bot itself sends, so its own replies (which come
// back through messages.upsert as fromMe) are skipped — while still logging
// messages a human types on the work phone (also fromMe, but not bot-sent).
const sentMessageIds = new Set()
const sentMessageOrder = []
const MAX_TRACKED_SENT = 1000
function recordSentId(id) {
  if (!id || sentMessageIds.has(id)) return
  sentMessageIds.add(id)
  sentMessageOrder.push(id)
  if (sentMessageOrder.length > MAX_TRACKED_SENT) {
    sentMessageIds.delete(sentMessageOrder.shift())
  }
}

// Shared socket handle. Reassigned on every (re)connect so the HTTP handlers
// below always use the live connection.
let sock = null
let connected = false

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
      logger.info('WhatsApp connection open')
      // Print every group this account is in, so the real JID for the group you
      // want can be copied straight into ALLOWED_GROUP_IDS.
      try {
        const all = await sock.groupFetchAllParticipating()
        for (const [jid, meta] of Object.entries(all)) {
          groupSubjects.set(jid, meta.subject || '')
          logger.info({ jid, subject: meta.subject }, 'group')
        }
      } catch (err) {
        logger.warn({ err }, 'Could not enumerate groups')
      }
    }

    if (connection === 'close') {
      connected = false
      const statusCode = lastDisconnect?.error?.output?.statusCode
      const loggedOut = statusCode === DisconnectReason.loggedOut
      logger.warn({ statusCode, loggedOut }, 'WhatsApp connection closed')
      if (loggedOut) {
        // Session was invalidated (unlinked). Delete AUTH_DIR and re-scan QR.
        logger.error(
          `Logged out — delete ${AUTH_DIR} and restart to re-link the device.`,
        )
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
      if (m.key.fromMe && sentMessageIds.has(m.key.id)) continue
      if (!isGroup) continue // groups only — 1:1 stays on Cloud API
      if (!(await isAllowedGroup(jid))) {
        logger.debug({ jid }, 'group not in allowlist — dropped')
        continue
      }

      const text = previewText
      if (!text) continue

      // In a group, key.participant is the actual sender; remoteJid is the group.
      // For our own (work-phone) messages, fall back to the bot's own JID.
      const participant =
        m.key.participant || m.participant || (m.key.fromMe ? sock.user?.id || '' : '')
      // Strip any device suffix, e.g. "6588257614:12@s.whatsapp.net" -> "6588257614".
      const sender_number = participant.split('@')[0].split(':')[0]
      const sender_name = m.pushName || sender_number

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

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', connected })
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
