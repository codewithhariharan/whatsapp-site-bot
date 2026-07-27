# history-harvester

One-off tool for recovering group messages the bot never processed — used after
the 11–26 July 2026 bridge outage.

## Why it exists

The site logs in this project are typed as **photo captions**. WhatsApp's
"Export chat" only preserves a caption when it can also attach the photo, so
any picture the phone has since dropped exports as `<Media omitted>` with the
caption gone. The 27 July export recovered 5 of 16 days for exactly this reason.

The caption lives in the message payload (`imageMessage.caption`), not in the
media file. Pulling history through WhatsApp's own protocol therefore recovers
captions no export can reach.

```
harvester ──> history.json ──> backfill.py --from-history ──> Supabase
```

## Before you run it

**This links a separate device. Never point it at the bridge's credentials.**
Two sockets sharing one session can get that session invalidated, and
`baileys-bridge/index.js` reacts to a logout by deleting its `auth_info` and
exiting — you would take the live bot down while trying to recover data from
the last time it was down. The tool refuses to start if `AUTH_DIR` resolves
inside `baileys-bridge/`, but don't rely on that alone.

WhatsApp allows four linked devices. This uses one, and you remove it afterwards.

## Usage

```bash
npm install     # see "Dependencies" below if this fails

node index.js --jid "120363021760406818@g.us" \
              --since 2026-07-13 --until 2026-07-25
```

Scan the QR with the phone that's in the group (WhatsApp → Settings → Linked
devices → Link a device), or pass `--pair 6588257614` to link with an
8-character code instead.

It then:
1. takes whatever history WhatsApp pushes on link, and
2. pages backwards with `fetchMessageHistory` (50 at a time) until it reaches
   `--since`, WhatsApp stops serving older messages, or `--timeout` (default
   600s) expires.

Output is `history.json`, plus a per-day count so you can see the coverage.

Then feed it in — dry run first, `--apply` to commit:

```bash
cd ..
python backfill.py --from-history history-harvester/history.json \
    --group-id "120363021760406818@g.us" --from 2026-07-13 --to 2026-07-25
```

**Finally, unlink:** WhatsApp → Settings → Linked devices → this device → Log out.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--jid` | *required* | Group JID. `python backfill.py --list-groups` lists known ones |
| `--since` | *required* | Page back to this date (YYYY-MM-DD) |
| `--until` | today | Ignore messages after this date |
| `--out` | `./history.json` | Output path |
| `--pair` | — | Phone number for pairing-code linking instead of QR |
| `--timeout` | `600` | Overall budget in seconds |

## Dependencies

Baileys pulls `libsignal` from git. If `npm install` fails with `EALLOWGIT`,
this directory's `node_modules` is a junction to `baileys-bridge/node_modules`,
which already has a complete install — that's deliberate, and it avoids a
second copy of a large dependency tree. Recreate it with:

```powershell
New-Item -ItemType Junction -Path .\node_modules -Target ..\baileys-bridge\node_modules
```

## Gotcha worth knowing

`browser` must be `Browsers.ubuntu('Desktop')`. The Baileys README suggests
`Browsers.macOS('Desktop')` for full history, but against WhatsApp Web
2.3000.x, pairing `Mac OS` *or* `Windows` **together with** `syncFullHistory:
true` is refused with a 428 before any QR appears. Either setting alone
connects fine; only the combination trips it. `Ubuntu` works and is still a
desktop profile, which is what earns the longer history window.

## Limits

WhatsApp decides how far back it will serve. If it stops short of `--since`,
the tool reports where it got to and writes what it has — the remaining days
aren't recoverable this way.
