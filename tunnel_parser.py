"""Parse a tunnel update message into a tunnel_updates row.

The messages follow a template:

    P103 Tunnel Update
    15 Aug 2026

    !! CHANGING TBM MACHINE !!

    LDTBM Main Drive
    - Mined: P1203 to P1209
    ...

    TBM Progress:
    - Day Shift: 3/1205/1759
    ...

    Delays:
    ...

Two things happen here, in this order:

  1. `split_sections()` and friends do the deterministic work in Python --
     splitting on the section headers, pulling the exclamation block out,
     reading the date. These are exact rules; handing them to a model would
     only add a way for them to be wrong.
  2. `parse_tunnel_update()` then asks Claude for the *numbers* inside those
     blocks, which is where the wording genuinely varies ("Total Disposed: 102
     loads /approx. 5.2 rings" versus "102 loads (5.2 rings)").

The verbatim blocks reach the database untouched either way, so a bad numeric
extraction degrades /ask's arithmetic but never corrupts the record itself.
"""

import json
import logging
import re
from datetime import date, datetime

import anthropic

from config import settings

logger = logging.getLogger("site_bot")

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

_MODEL = "claude-haiku-4-5-20251001"

# ── Section headers ───────────────────────────────────────────────────────────

# The three the spreadsheet has columns for. Matched case-insensitively, with or
# without a trailing colon, and tolerant of the LDTBM/LDTMB transposition that
# appears in both the sample message and the sheet's own header.
_KNOWN_SECTIONS = {
    "main_drive": re.compile(
        r"^\s*(?:[*\-·•]\s*)?"
        r"((?:[A-Z]{2,6}\s+)?(?:LDTBM|LDTMB|TBM|RTBM|EPB)\s+MAIN\s+DRIVE)\s*:?\s*$",
        re.I,
    ),
    "tbm_progress": re.compile(
        r"^\s*(?:[*\-·•]\s*)?(TBM\s+PROGRESS)\s*:?\s*$", re.I
    ),
    "delays": re.compile(
        r"^\s*(?:[*\-·•]\s*)?(DELAYS?)\s*:?\s*$", re.I
    ),
}

# A line that introduces some other section: a short, bullet-free line ending in
# a colon. Anything matching becomes a key in other_sections rather than being
# silently dropped.
_OTHER_SECTION = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&()\-\.]{2,48})\s*:\s*$")

# Closing courtesies that belong to no section.
_TRAILER = re.compile(r"^\s*(thank\s*you|thanks|tq|regards|noted)\b[\s.!]*$", re.I)

# ── Critical / exclamation markers ────────────────────────────────────────────

# U+203C is the house marker, but a phone keyboard offers several near-identical
# glyphs and engineers reach for whichever is closest. Accepting the family
# costs nothing; missing a critical line because someone typed U+2757 once is
# the failure that matters -- the director's question reads this column and
# nothing else.
_MARKERS = "‼❗❕⚠⁉"
_VS = "️︎"  # emoji / text variation selectors
_MARKER_RUN = f"[{_MARKERS}][{_MARKERS}{_VS}\\s]*"

_PAIRED = re.compile(f"{_MARKER_RUN}(.+?){_MARKER_RUN}")
_LEADING = re.compile(f"^{_MARKER_RUN}(.+?)\\s*$")
_TRAILING = re.compile(f"^\\s*(.+?){_MARKER_RUN}$")

# ── Dates ─────────────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y",
    "%d %b %y", "%d %B %y", "%d.%m.%Y", "%Y-%m-%d",
)

_DATE_LINE = re.compile(
    r"^\s*(\d{1,2}\s*[-/. ]\s*(?:\d{1,2}|[A-Za-z]{3,9})\s*[-/. ]\s*\d{2,4}"
    r"|\d{4}-\d{2}-\d{2})\s*$"
)

_CONTRACT = re.compile(r"\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b")


def strip_markers(text: str) -> str:
    """Remove exclamation glyphs and their variation selectors from a string."""
    return re.sub(f"[{_MARKERS}{_VS}]", "", text).strip(" \t*_-·•")


def extract_exclamations(text: str) -> list[str]:
    """Return every flagged line's content, markers stripped, order preserved.

    A paired marker line is the documented form. A line carrying only one marker
    is accepted too -- that is a typo in the template, not a decision to make the
    line non-critical.
    """
    found: list[str] = []
    for line in text.splitlines():
        if not any(ch in line for ch in _MARKERS):
            continue
        match = _PAIRED.search(line)
        if match:
            body = match.group(1)
        else:
            stripped = line.strip()
            single = _LEADING.match(stripped) or _TRAILING.match(stripped)
            body = single.group(1) if single else line
        body = strip_markers(body)
        if body:
            found.append(body)
    return found


def parse_date_line(line: str) -> date | None:
    """Parse a standalone date line. Returns None if the line is not a date."""
    raw = line.strip().strip("*_ ")
    if not _DATE_LINE.match(raw):
        return None
    normalised = re.sub(r"\s*([-/.])\s*", r"\1", raw)
    candidates = (raw, normalised, normalised.replace("-", " ").replace(".", " "))
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate.strip(), fmt).date()
            except ValueError:
                continue
    return None


def extract_contract(text: str) -> tuple[str | None, str | None]:
    """Return (contract code, the raw line it came from).

    'P103 Tunnel Update' -> ('P103', 'P103 Tunnel Update'). The code alone is
    the key: the boilerplate after it varies between senders, and two spellings
    of the same contract must not read as two contracts.
    """
    for line in text.splitlines()[:5]:
        stripped = strip_markers(line).strip()
        if not stripped:
            continue
        match = _CONTRACT.search(stripped.upper())
        if match:
            return match.group(1), stripped
    return None, None


def split_sections(text: str) -> tuple[dict[str, str], dict[str, str], str | None]:
    """Split the body into (known sections, other sections, drive name).

    Lines before the first recognised header -- the title, the date, the flagged
    block -- belong to no section and are dropped here; they are read separately.
    """
    known: dict[str, list[str]] = {}
    other: dict[str, list[str]] = {}
    drive_name = None
    current: list[str] | None = None

    for line in text.splitlines():
        header_key = None
        for key, pattern in _KNOWN_SECTIONS.items():
            match = pattern.match(line)
            if match:
                header_key = key
                if key == "main_drive":
                    drive_name = match.group(1).strip()
                break

        if header_key:
            current = known.setdefault(header_key, [])
            continue

        # Only look for an unknown header once the templated part has started;
        # before that, a "P103 Tunnel Update:" style line would match.
        candidate = _OTHER_SECTION.match(line)
        if candidate and current is not None:
            label = candidate.group(1).strip()
            current = other.setdefault(label, [])
            continue

        if current is not None and not _TRAILER.match(line):
            current.append(line)

    def join(block: list[str]) -> str:
        return "\n".join(block).strip("\n").rstrip()

    return (
        {k: join(v) for k, v in known.items() if join(v)},
        {k: join(v) for k, v in other.items() if join(v)},
        drive_name,
    )


def is_tunnel_update(text: str) -> bool:
    """Cheap structural test: does this look like a templated tunnel update?

    Deliberately not a model call. The tunnel group will carry ordinary chat
    too, and paying for a classification on every "noted" is waste. A message
    needs a contract code near the top and at least one known section header --
    chat has neither.
    """
    contract, _ = extract_contract(text)
    if not contract:
        return False
    known, _, _ = split_sections(text)
    return bool(known)


# ── Numeric extraction ────────────────────────────────────────────────────────

_NUMERIC_FIELDS = (
    "mined_from", "mined_to", "ring_built_from", "ring_built_to",
    "fsc_shift", "fsc_cumulative", "rings_total",
    "day_shift_rings", "day_shift_cumulative",
    "night_shift_rings", "night_shift_cumulative",
    "pct_completion", "tbm_location", "instrumentation",
    "delay_flag", "ds_loads", "ns_loads", "total_disposed_loads",
    "disposed_rings_equiv", "rings_excavated", "delta_disposal",
    "storage_rings", "storage_capacity_rings", "earthwork_subcon",
)

_NUMBER_ONLY = {
    "fsc_shift", "fsc_cumulative", "rings_total",
    "day_shift_rings", "day_shift_cumulative",
    "night_shift_rings", "night_shift_cumulative",
    "pct_completion", "ds_loads", "ns_loads", "total_disposed_loads",
    "disposed_rings_equiv", "rings_excavated", "delta_disposal",
    "storage_rings", "storage_capacity_rings",
}

_EXTRACT_SYSTEM = """You read one tunnelling progress update and pull the numbers out of it.

Return ONLY a JSON object with these keys. Use null for anything the message
does not state -- never guess, never carry a value over from an example.

mined_from, mined_to          ring/chainage refs: "Mined: P1203 to P1209" -> "P1203", "P1209"
ring_built_from, ring_built_to  same, from the "Ring Built" line
fsc_shift, fsc_cumulative     first stage concrete "0/731" -> 0 and 731
rings_total                   the drive's total ring count -- the denominator in "3/1205/1759" and in "1759 rings"
day_shift_rings, day_shift_cumulative     "Day Shift: 3/1205/1759" -> 3 and 1205
night_shift_rings, night_shift_cumulative "Night Shift: 4/1209/1759" -> 4 and 1209
pct_completion                "68.73%" -> 68.73  (a number, no % sign)
tbm_location                  "TBM is underneath Punggol Central Road." -> "Punggol Central Road"
instrumentation               "All within AL"
delay_flag                    the answer to "Delay LTBM#1": "No", or "Yes" plus the reason
ds_loads, ns_loads            day shift and night shift soil disposal loads.
                              "DS - 102 loads (TSSG); NS - 0 load (TSSG)" -> 102 and 0
                              DS = day shift, NS = night shift. The bracketed
                              word is the disposal ground, not a quantity.
total_disposed_loads          "Total Disposed: 102 loads" -> 102
disposed_rings_equiv          the "/approx. 5.2 rings" alongside it -> 5.2
rings_excavated               "Total Rings Excavated: 7 rings" -> 7
delta_disposal                "Delta Disposal: -1.8 ring" -> -1.8  (keep the sign)
storage_rings, storage_capacity_rings   "On site Storage: 7.6/62 Full (Rings)" -> 7.6 and 62
earthwork_subcon              "Earthwork subcon is KTC" -> "KTC"

The three-number form a/b/c is always: this shift / cumulative to date / drive total.
Numeric keys must be JSON numbers or null -- not strings, not "1,205", no units.
Text keys must be plain strings or null.

Respond with the JSON object and nothing else."""


def _coerce_numbers(data: dict) -> dict:
    """Force the numeric keys to real numbers, dropping anything unusable.

    The model is asked for numbers and usually obliges, but "1,205", "68.73%"
    and "5.2 rings" all turn up. Postgres would reject those against a NUMERIC
    column and lose the whole row -- including the verbatim blocks, which were
    parsed correctly. Salvaging the number here keeps a formatting slip from
    costing the record.
    """
    clean = {}
    for key in _NUMERIC_FIELDS:
        value = data.get(key)
        if value is None or value == "":
            continue
        if key in _NUMBER_ONLY:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                clean[key] = value
                continue
            match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
            if match:
                clean[key] = float(match.group())
        else:
            clean[key] = str(value).strip()
    return clean


def extract_numbers(known: dict[str, str]) -> dict:
    """Ask the model for the numbers inside the section blocks."""
    body = "\n\n".join(f"{k}:\n{v}" for k, v in known.items())
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=800,
            system=[{
                "type": "text",
                "text": _EXTRACT_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": body}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return _coerce_numbers(json.loads(raw))
    except Exception:
        # The verbatim blocks are already parsed and are the record of truth.
        # Losing the derived numbers makes /ask do more reading; losing the row
        # would lose the update. Log and carry on.
        logger.exception("tunnel: numeric extraction failed; storing text only")
        return {}


def parse_tunnel_update(text: str, fallback_date: date) -> dict | None:
    """Parse a tunnel update. Returns None if the message is not one.

    `fallback_date` is used only when the message states no date of its own --
    the written date wins whenever there is one, so a late-posted update files
    under the day it reports on.
    """
    contract, title_line = extract_contract(text)
    if not contract:
        return None

    known, other, drive_name = split_sections(text)
    if not known:
        return None

    update_date = None
    for line in text.splitlines()[:6]:
        update_date = parse_date_line(line)
        if update_date:
            break

    exclamations = extract_exclamations(text)

    row = {
        "contract": contract,
        "title_line": title_line,
        "update_date": update_date or fallback_date,
        "date_was_stated": update_date is not None,
        "drive_name": drive_name,
        "main_drive": known.get("main_drive"),
        "tbm_progress": known.get("tbm_progress"),
        "delays": known.get("delays"),
        # Several flagged lines in one message all belong to the column -- the
        # director asking "what's critical" must see every one of them.
        "exclamation": "\n".join(exclamations) if exclamations else None,
        "other_sections": other,
        "raw_message": text,
    }
    row.update(extract_numbers(known))
    return row
