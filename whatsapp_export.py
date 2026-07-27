"""Parser for WhatsApp's "Export chat" .txt files.

Used by backfill.py to recover messages the bot never saw (e.g. while the
Baileys bridge was down). Pure parsing — no network, no database.

WhatsApp's export format differs between platforms and locales:

    Android:  10/07/2026, 09:15 - Rajesh: Main Location: Zone 3
    iOS:      [10/07/2026, 09:15:32] Rajesh: Main Location: Zone 3
    12-hour:  10/07/2026, 9:15 am - Rajesh: Main Location: Zone 3

A message can span many lines; only the first carries a timestamp, so any line
that doesn't start one is a continuation of the message before it. That matters
here because site logs are inherently multi-line ("Main Location: …\nSub
Location: …\nManpower: …") — a line-at-a-time parser would shred them.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

# WhatsApp sprinkles directional marks (U+200E/U+200F) through iOS exports and
# uses non-breaking / narrow spaces around am/pm. Strip them before matching.
_INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‬﻿"))

# Tombstones — the whole message is gone, there is never a caption behind them.
_PLACEHOLDERS = {
    "this message was deleted",
    "you deleted this message",
    "null",
}

# First line of a media message. The caption, when there is one, follows on the
# NEXT line(s) — and it survives even when the media itself does not:
#
#   13/07/2026, 10:56 - Amira: <Media omitted>      <- media gone from the phone
#   P33: Curing for crosshead in progress           <- caption still exported
#
# This is why a media marker must only ever strip its own line. Treating the
# whole message as a placeholder because it *starts* with one discards the
# caption, which in this project is the entire site log.
#
#   Android (with media):    IMG-20260727-WA0012.jpg (file attached)
#   iOS (with media):        <attached: 00000042-PHOTO-2026-07-27-16-02-11.jpg>
#   Either, media missing:   <Media omitted> / image omitted / sticker omitted …
_ATTACHMENT = re.compile(
    r"""^\s*(?:
        <attached:[^>]*>
      | \S.*\.\w{2,5}\s*\(file\ attached\)
      | <?media\ omitted>?
      | (?:image|video|audio|sticker|document|gif|contact\ card)\ omitted
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def strip_attachment_marker(text: str) -> str:
    """Drop a leading media marker line, returning just the caption."""
    lines = text.split("\n")
    if lines and _ATTACHMENT.match(lines[0]):
        return "\n".join(lines[1:]).strip()
    return text

# [10/07/2026, 09:15:32] Sender: text   ← iOS (brackets, optional seconds)
# 10/07/2026, 09:15 - Sender: text      ← Android (dash separator)
_HEADER = re.compile(
    r"""^
    \[?                                   # iOS wraps the stamp in brackets
    (?P<d1>\d{1,2})[/.-](?P<d2>\d{1,2})[/.-](?P<year>\d{2,4})
    ,?\s+
    (?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?
    \s*(?P<meridiem>[ap]\.?m\.?)?
    \]?
    \s*(?:-|–)?\s*                        # Android's " - "; absent on iOS
    (?P<body>.*)$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# "Sender Name: message". Group/system notices ("Alice added Bob", the E2E
# encryption notice) have no colon, which is exactly how we tell them apart.
_SENDER = re.compile(r"^(?P<sender>[^:]{1,80}?):\s(?P<text>.*)$", re.DOTALL)


@dataclass
class ExportedMessage:
    """One recovered message, from an export file or the history harvester."""

    timestamp: datetime
    sender: str
    text: str
    # Export files name saved contacts rather than numbering them, so this is
    # only populated when the source knows the number (i.e. the harvester).
    sender_number: str = ""

    @property
    def log_date(self) -> date:
        return self.timestamp.date()


def _clean(line: str) -> str:
    line = line.translate(_INVISIBLE)
    # Narrow/non-breaking spaces appear between the time and "am"/"pm".
    return "".join(" " if unicodedata.category(c) == "Zs" else c for c in line)


def _resolve_year(year: int) -> int:
    return year + 2000 if year < 100 else year


def detect_date_order(lines: list[str]) -> str:
    """Return "DMY" or "MDY" by finding a date that can only read one way.

    A stamp like 27/07/2026 is unambiguous (there's no 27th month); 07/07 tells
    us nothing. We scan until something decides it, then default to DMY — the
    format everywhere except the US, and the one Singapore phones produce.
    """
    for raw in lines:
        m = _HEADER.match(_clean(raw))
        if not m:
            continue
        d1, d2 = int(m.group("d1")), int(m.group("d2"))
        if d1 > 12:
            return "DMY"
        if d2 > 12:
            return "MDY"
    return "DMY"


def _parse_timestamp(m: re.Match, date_order: str) -> datetime | None:
    d1, d2 = int(m.group("d1")), int(m.group("d2"))
    day, month = (d1, d2) if date_order == "DMY" else (d2, d1)

    hour = int(m.group("hour"))
    meridiem = (m.group("meridiem") or "").replace(".", "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    try:
        return datetime(
            _resolve_year(int(m.group("year"))),
            month,
            day,
            hour,
            int(m.group("minute")),
            int(m.group("second") or 0),
        )
    except ValueError:
        # Misread day/month order, or a line that merely looks like a header.
        return None


def is_placeholder(text: str) -> bool:
    """True for media stubs and tombstones — nothing a parser could log."""
    low = text.strip().lower()
    return not low or any(low.startswith(p) for p in _PLACEHOLDERS)


def parse_export(content: str, date_order: str | None = None) -> list[ExportedMessage]:
    """Parse the full text of a WhatsApp export into messages.

    System notices (joins, leaves, the encryption banner) are dropped: they have
    no "Sender: " prefix. Media placeholders are kept out too — see
    is_placeholder. `date_order` overrides the DMY/MDY autodetection.
    """
    lines = content.splitlines()
    order = date_order or detect_date_order(lines)

    messages: list[ExportedMessage] = []
    current: ExportedMessage | None = None

    for raw in lines:
        line = _clean(raw)
        header = _HEADER.match(line)
        timestamp = _parse_timestamp(header, order) if header else None

        if timestamp is None:
            # Continuation of the message in progress (or leading junk).
            if current is not None:
                current.text += "\n" + raw.translate(_INVISIBLE).rstrip()
            continue

        sender_match = _SENDER.match(header.group("body"))
        if not sender_match:
            # A system notice ends whatever message was being accumulated.
            current = None
            continue

        current = ExportedMessage(
            timestamp=timestamp,
            sender=sender_match.group("sender").strip(),
            text=sender_match.group("text").rstrip(),
        )
        messages.append(current)

    for msg in messages:
        # Reduce a media message to its caption; a bare photo becomes empty and
        # is dropped below, but a captioned one keeps the text that matters.
        msg.text = strip_attachment_marker(msg.text.strip())

    return [m for m in messages if not is_placeholder(m.text)]
