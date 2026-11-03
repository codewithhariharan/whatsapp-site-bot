"""The dashboard's test console: run the real commands without side effects.

The console (dashboard.py, /api/console) sends a command through the same
handlers WhatsApp messages take, against the real record, so a tester sees
exactly what the group would. While it runs, four things the bot would
otherwise do to the outside world are switched off by the context set here:

  - Replies and files are collected (`outbox`) instead of going to WhatsApp.
  - Email is collected too — /tunnel shows the summary it would have sent
    rather than mailing the real distribution list.
  - The state commands keep (/setorder's location order, and the /daily →
    /reorder → /delete → /confirm session) is stored under the tester's own
    key (`state_group`), not the group's. Testers can reorder freely without
    touching CR106's order or a /daily an engineer has open in the group.
  - The on-demand catch-up is skipped, and nothing can be queued as a post.
    The dashboard shows the record as the 6-hourly batches leave it; a test
    must not file real posts early or add fake ones.

ContextVars carry this per request: a WhatsApp message handled at the same
moment sees none of it, and asyncio.to_thread() copies the context, so the
blocking database and mail calls run in threads still see it.

No imports from the app, so database.py, mailer.py and whatsapp_client.py can
all import this without a cycle.
"""

from contextvars import ContextVar

# What the bot would have sent: [{"type": "text" | "file" | "email", ...}].
outbox: ContextVar[list | None] = ContextVar("sandbox_outbox", default=None)

# The key state is stored under in place of the group id.
state_group: ContextVar[str | None] = ContextVar("sandbox_state_group", default=None)


def active() -> bool:
    return outbox.get() is not None


def capture(item: dict) -> bool:
    """Record `item` if a console run is active. True means it was captured
    and the caller must not send it for real."""
    box = outbox.get()
    if box is None:
        return False
    box.append(item)
    return True


def state_key(group_id: str) -> str:
    """Where a group's /setorder and /daily session live: the group itself
    normally, the tester's own key inside the console."""
    return state_group.get() or group_id
