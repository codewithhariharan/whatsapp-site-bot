/**
 * One-off history harvester — recovers group messages the bot never saw.
 *
 * Why this exists: the site logs in this project are typed as *photo captions*,
 * and WhatsApp's "Export chat" drops captions for any photo the phone no longer
 * holds. The caption lives in the message payload (`imageMessage.caption`), not
 * in the media file, so pulling history through WhatsApp's own protocol
 * recovers captions that no export can.
 *
 *   this harvester ──> history.json ──> backfill.py --from-history ──> Supabase
 *
 * IMPORTANT — this links a SEPARATE device. Do not point AUTH_DIR at the
 * bridge's auth_info and do not copy its credentials here. Two sockets sharing
 * one session can get that session invalidated, and baileys-bridge/index.js
 * responds to a logout by deleting its auth_info and exiting — you would take
 * the live bot down. WhatsApp allows up to four linked devices; this uses one
 * of them, and you unlink it afterwards from:
 *   WhatsApp → Settings → Linked devices → (this device) → Log out
 *
 * Usage:
 *   npm install
 *   node index.js --jid "1203...@g.us" --since 2026-07-13 --until 2026-07-25
 *
 * Optional flags:
 *   --out FILE        output path            (default ./history.json)
 *   --pair 6512345678 link by pairing code instead of QR
 *   --timeout 600     overall budget in seconds (default 600)
 */
import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

// ── Arguments ─────────────────────────────────────────────────────────────────
function arg(name, fallback = undefined) {
  const i = process.argv.indexOf(`--${name}`)
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const TARGET_JID = arg('jid')
const SINCE = arg('since')
const UNTIL = arg('until')
const OUT = resolve(arg('out', './history.json'))
const PAIRING_NUMBER = (arg('pair', '') || '').replace(/[^0-9]/g, '')
const TIMEOUT_MS = parseInt(arg('timeout', '600'), 10) * 1000
const AUTH_DIR = resolve(process.env.AUTH_DIR || './auth_info')

if (!TARGET_JID || !SINCE) {
  console.error('Usage: node index.js --jid "1203...@g.us" --since YYYY-MM-DD [--until YYYY-MM-DD]')
  process.exit(2)
}

// Guard the footgun described above: never drive the live bridge's session.
if (/baileys-bridge/i.test(AUTH_DIR)) {
  console.error(
    `Refusing to run: AUTH_DIR (${AUTH_DIR}) points inside the live bridge.\n` +
      'This tool must link its own device — see the header comment.',
  )
  process.exit(2)
}

// Inclusive day bounds, in seconds (WhatsApp timestamps are unix seconds).
const sinceTs = Math.floor(new Date(`${SINCE}T00:00:00`).getTime() / 1000)
const untilTs = UNTIL
  ? Math.floor(new Date(`${UNTIL}T23:59:59`).getTime() / 1000)
  : Math.floor(Date.now() / 1000)

const logger = pino({ level: process.env.LOG_LEVEL || 'warn' })

// Quiet debounce windows: history arrives in bursts, so "nothing for N seconds"
// is the only reliable signal that a sync has finished.
const INITIAL_SYNC_IDLE_MS = 20000
const ON_DEMAND_IDLE_MS = 25000
const PAGE_SIZE = 50 // WhatsApp's per-request maximum
const MAX_STALLED_PAGES = 3

// ── Text extraction — mirrors baileys-bridge/index.js so the Python parser
// ── sees exactly the same strings it would have seen live.
function extractText(message) {
  if (!message) return ''
  if (message.conversation) return message.conversation
  if (message.extendedTextMessage?.text) return message.extendedTextMessage.text
  if (message.imageMessage?.caption) return message.imageMessage.caption
  if (message.documentMessage?.caption) return message.documentMessage.caption
  if (message.videoMessage?.caption) return message.videoMessage.caption
  if (message.ephemeralMessage?.message) return extractText(message.ephemeralMessage.message)
  if (message.viewOnceMessage?.message) return extractText(message.viewOnceMessage.message)
  if (message.viewOnceMessageV2?.message) return extractText(message.viewOnceMessageV2.message)
  return ''
}

function toSeconds(ts) {
  // messageTimestamp is a number or a Long depending on where it came from.
  if (ts == null) return 0
  return typeof ts === 'number' ? ts : Number(ts.toNumber ? ts.toNumber() : ts)
}

// ── Collected state ───────────────────────────────────────────────────────────
const collected = new Map() // message id -> record, deduped across sync rounds
let oldest = null // { key, timestamp } for the target chat, drives paging
let sawAnyForTarget = false

function record(m) {
  const jid = m.key?.remoteJid
  if (jid !== TARGET_JID) return
  sawAnyForTarget = true

  const ts = toSeconds(m.messageTimestamp)
  if (ts && (!oldest || ts < oldest.timestamp)) {
    oldest = { key: m.key, timestamp: ts }
  }

  const text = extractText(m.message).trim()
  if (!text) return // a bare photo with no caption carries nothing to log
  if (ts < sinceTs || ts > untilTs) return
  if (m.key.id && collected.has(m.key.id)) return

  const participant =
    m.key.participant || m.participant || (m.key.fromMe ? sock?.user?.id || '' : '')
  const senderNumber = participant.split('@')[0].split(':')[0]

  collected.set(m.key.id || `${ts}-${collected.size}`, {
    id: m.key.id,
    timestamp: ts,
    iso: new Date(ts * 1000).toISOString(),
    group_id: jid,
    sender_name: m.pushName || (m.key.fromMe ? sock?.user?.name : '') || senderNumber,
    sender_number: senderNumber,
    from_me: !!m.key.fromMe,
    text,
  })
}

function progress() {
  const reach = oldest ? new Date(oldest.timestamp * 1000).toISOString().slice(0, 10) : '—'
  console.log(`   collected ${collected.size} in range | oldest seen: ${reach} | target: ${SINCE}`)
}

// ── Wait until history events stop arriving for `idleMs` ──────────────────────
let lastEventAt = Date.now()
function waitForQuiet(idleMs, budgetMs) {
  const deadline = Date.now() + budgetMs
  return new Promise((done) => {
    const tick = setInterval(() => {
      if (Date.now() - lastEventAt >= idleMs || Date.now() > deadline) {
        clearInterval(tick)
        done()
      }
    }, 1000)
  })
}

function finish(reason) {
  const rows = [...collected.values()].sort((a, b) => a.timestamp - b.timestamp)
  writeFileSync(OUT, JSON.stringify(rows, null, 2), 'utf-8')

  const byDay = {}
  for (const r of rows) byDay[r.iso.slice(0, 10)] = (byDay[r.iso.slice(0, 10)] || 0) + 1

  console.log(`\n${'─'.repeat(60)}`)
  console.log(`Stopped: ${reason}`)
  console.log(`Wrote ${rows.length} messages with text to ${OUT}`)
  if (rows.length) {
    console.log('\nPer day:')
    for (const d of Object.keys(byDay).sort()) console.log(`  ${d}  ${byDay[d]}`)
  }
  if (!sawAnyForTarget) {
    console.log(`\nNo messages at all for ${TARGET_JID} — check the JID is correct.`)
  }
  console.log(
    '\nNow unlink this device: WhatsApp → Settings → Linked devices → Log out.\n' +
      `Then: python backfill.py --from-history ${OUT} --group-id "${TARGET_JID}" ` +
      `--from ${SINCE} --to ${UNTIL || SINCE}`,
  )
  process.exit(0)
}

// ── Main ──────────────────────────────────────────────────────────────────────
let sock = null
let pairingRequested = false
let closeCount = 0
const MAX_RECONNECTS = 5

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  const { version } = await fetchLatestBaileysVersion()

  sock = makeWASocket({
    version,
    auth: state,
    logger: logger.child({ module: 'baileys' }),
    // A desktop profile is served a much longer history than a phone-like one,
    // and syncFullHistory asks for all of it rather than the recent slice.
    //
    // Must be ubuntu(), NOT the macOS('Desktop') the Baileys README suggests:
    // measured against WA web 2.3000.x, pairing 'Mac OS' or 'Windows' *together
    // with* syncFullHistory is refused with a 428 before any QR is emitted.
    // Either option alone connects fine; only the combination trips it.
    browser: Browsers.ubuntu('Desktop'),
    syncFullHistory: true,
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('messaging-history.set', ({ messages, syncType, progress: pct }) => {
    lastEventAt = Date.now()
    for (const m of messages || []) record(m)
    console.log(`   … history batch: ${messages?.length || 0} messages (syncType=${syncType}, ${pct ?? '?'}%)`)
  })

  // Live messages can also arrive while we're connected; harmless to keep.
  sock.ev.on('messages.upsert', ({ messages }) => {
    for (const m of messages || []) record(m)
  })

  sock.ev.on('connection.update', async (update) => {
    const { connection, qr, lastDisconnect } = update

    if (qr) {
      if (PAIRING_NUMBER && !pairingRequested) {
        pairingRequested = true
        try {
          const code = await sock.requestPairingCode(PAIRING_NUMBER)
          console.log(`\n┌────────────────────────────────────────────┐`)
          console.log(`│  Pairing code: ${code}`)
          console.log(`│  WhatsApp → Settings → Linked devices →`)
          console.log(`│  Link a device → "Link with phone number"`)
          console.log(`└────────────────────────────────────────────┘\n`)
        } catch (err) {
          console.error('Failed to request pairing code:', err?.message || err)
        }
      } else if (!PAIRING_NUMBER) {
        console.log('\nScan with the phone that has the group')
        console.log('(WhatsApp → Settings → Linked devices → Link a device):\n')
        qrcode.generate(qr, { small: true })
      }
    }

    if (connection === 'open') {
      console.log('\nLinked. Waiting for WhatsApp to push history…')
      console.log('(this can take a few minutes on a large account)\n')
      lastEventAt = Date.now()
      await harvest()
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode
      if (statusCode === DisconnectReason.loggedOut) {
        console.error('Logged out. Delete ./auth_info and re-link to try again.')
        process.exit(1)
      }
      // Surface why: a silent reconnect loop here is indistinguishable from a
      // hang, and 401/403/405 each need a different fix from the operator.
      const reason = Object.keys(DisconnectReason).find(
        (k) => DisconnectReason[k] === statusCode,
      )
      console.log(
        `Connection closed (status ${statusCode ?? '?'}` +
          `${reason ? `, ${reason}` : ''}): ${lastDisconnect?.error?.message || 'no detail'}`,
      )
      if (++closeCount > MAX_RECONNECTS) {
        console.error(
          `\nGave up after ${MAX_RECONNECTS} reconnects. If this is status 401/403, ` +
            'delete ./auth_info and start again to link fresh.',
        )
        process.exit(1)
      }
      console.log(`Reconnecting (${closeCount}/${MAX_RECONNECTS})…`)
      setTimeout(() => start().catch((e) => console.error(e)), 5000)
    }
  })
}

async function harvest() {
  const deadline = Date.now() + TIMEOUT_MS

  // Phase 1: whatever WhatsApp volunteers on link.
  await waitForQuiet(INITIAL_SYNC_IDLE_MS, TIMEOUT_MS)
  console.log('\nInitial sync settled.')
  progress()

  // Phase 2: page backwards until we reach --since.
  let stalled = 0
  while (Date.now() < deadline) {
    if (!oldest) {
      return finish('no messages for this chat — nothing to page back from')
    }
    if (oldest.timestamp <= sinceTs) {
      return finish(`reached ${SINCE}`)
    }
    if (stalled >= MAX_STALLED_PAGES) {
      return finish(`WhatsApp stopped serving older history (stalled ${stalled}x)`)
    }

    const before = oldest.timestamp
    const countBefore = collected.size
    console.log(
      `\nRequesting ${PAGE_SIZE} older messages before ` +
        `${new Date(before * 1000).toISOString().slice(0, 16)}…`,
    )
    try {
      await sock.fetchMessageHistory(PAGE_SIZE, oldest.key, oldest.timestamp)
    } catch (err) {
      console.error('  fetchMessageHistory failed:', err?.message || err)
      stalled++
      continue
    }

    lastEventAt = Date.now()
    await waitForQuiet(ON_DEMAND_IDLE_MS, ON_DEMAND_IDLE_MS * 2)

    // No older reference and no new rows means WhatsApp served nothing new.
    if (oldest.timestamp >= before && collected.size === countBefore) stalled++
    else stalled = 0
    progress()
  }

  finish('overall timeout reached')
}

process.on('SIGINT', () => finish('interrupted (Ctrl+C)'))

start().catch((err) => {
  console.error('Failed to start:', err)
  process.exit(1)
})
