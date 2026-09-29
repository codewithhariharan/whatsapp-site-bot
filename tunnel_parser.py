"""Read the standardised tunnel progress update, and the "!" flags.

Every contract group sends the same template:

    TUNNEL PROGRESS UPDATE
    CONTRACT: CR146
    DATE: 04-JAN-2026

    DRIVE: EB - Main Drive 3
    PROGRESS: 0 / 225 / 888 (25.3%)
    TBM LOCATION: at side table of AMK Ave 3

    INSTRUMENTATION: LG3053 breached AL
    ISSUES: [shift change]

The template is fixed, so this is parsed with rules, not a model: the result is
exact, instant and free. What varies is only typing — WhatsApp bold asterisks
around the labels ("*CONTRACT*:"), stray spaces ("02-JAN -2026"), "EB-Main" vs
"EB - Main", brackets round the issues. Those are normalised here.

Separately, any line containing "!" in a contract group is a flag for the
director's /!! command, whether it sits inside an update or in any other
message. See extract_flags().
"""

import re
from datetime import date, datetime

# ── Labels ────────────────────────────────────────────────────────────────────

_FIELDS = {
    "CONTRACT": "contract",
    "DATE": "date",
    "DRIVE": "drive",
    "PROGRESS": "progress",
    "TBM LOCATION": "tbm_location",
    "INSTRUMENTATION": "instrumentation",
    "ISSUES": "issues",
    "ISSUE": "issues",
}

# A label, optionally wrapped in WhatsApp bold/italic marks, then a colon. The
# value runs to the next label or the end, so the parser works whether or not
# each field is on its own line.
_LABEL = re.compile(
    r"[*_~\s]*\b(CONTRACT|DATE|DRIVE|PROGRESS|TBM\s+LOCATION|INSTRUMENTATION|ISSUES?)"
    r"\b[*_~\s]*:",
    re.I,
)

_HEADER = re.compile(r"TUNNEL\s+PROGRESS\s+UPDATE", re.I)

_REQUIRED = ("contract", "date", "drive", "progress")

_PROGRESS = re.compile(
    r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)"
    r"(?:\s*\(\s*(\d+(?:\.\d+)?)\s*%?\s*\))?"
)

_DATE_FORMATS = (
    "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y", "%d/%m/%Y", "%d/%m/%y",
    "%d %b %Y", "%d %B %Y", "%d.%m.%Y", "%Y-%m-%d",
)

_NONE_WORDS = {"none", "nil", "na", "n/a", "-", "no", "no issue", "no issues", ""}


class TunnelParseError(ValueError):
    """The message is an update, but a required field is missing or unreadable.

    Raised rather than returning None so the batch run marks the post failed and
    tells the group to resend it — a half-read update must not be filed.
    """


def _clean(value: str) -> str:
    """Strip WhatsApp formatting marks and collapse whitespace."""
    value = value.replace("\xa0", " ")
    value = re.sub(r"[*_~]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def is_progress_update(text: str) -> bool:
    """Does this message claim to be a progress update?

    The header, or a CONTRACT and a PROGRESS label. A message that passes this
    and then fails to parse is reported back to the group, not ignored.
    """
    if _HEADER.search(text):
        return True
    labels = {_FIELDS[" ".join(m.group(1).upper().split())]
              for m in _LABEL.finditer(text)}
    return {"contract", "progress"} <= labels


def _fields(text: str) -> dict[str, str]:
    matches = list(_LABEL.finditer(text))
    found: dict[str, str] = {}
    for i, match in enumerate(matches):
        key = _FIELDS[" ".join(match.group(1).upper().split())]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = _clean(text[match.end():end])
        # The first occurrence wins; a later "date:" inside the ISSUES text must
        # not overwrite the report date.
        found.setdefault(key, value)
    return found


def parse_date(value: str) -> date | None:
    raw = _clean(value).strip(" .")
    candidates = {
        raw,
        re.sub(r"\s*([-/.])\s*", r"\1", raw),          # "02-JAN -2026"
    }
    for candidate in list(candidates):
        candidates.add(candidate.title())
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def normalise_drive(value: str) -> str:
    """'EB-Main Drive 3' and 'EB - Main Drive 3' are the same drive.

    The drive is part of the key a resend replaces on, so two spellings of it
    must not read as two drives.
    """
    return re.sub(r"\s*-\s*", " - ", _clean(value)).strip(" -")


def _issues(value: str | None) -> str | None:
    if value is None:
        return None
    inner = _clean(value)
    # "[Grout leak]" -> "Grout leak"; the template's own stray trailing "v"
    # ("[None]v") goes with the brackets.
    bracketed = re.fullmatch(r"\[(.*)\]\s*v?", inner, flags=re.S)
    if bracketed:
        inner = bracketed.group(1).strip()
    inner = strip_marks(inner)
    return None if inner.lower().strip(" .") in _NONE_WORDS else inner


def parse_progress_update(text: str) -> dict | None:
    """Parse one update. None if the message is not an update at all.

    Raises TunnelParseError when it IS an update but cannot be filed.
    """
    if not is_progress_update(text):
        return None

    fields = _fields(text)
    missing = [f for f in _REQUIRED if not fields.get(f)]
    if missing:
        raise TunnelParseError(
            "missing " + ", ".join(f.upper() for f in missing)
        )

    report_date = parse_date(fields["date"])
    if report_date is None:
        raise TunnelParseError(f"DATE not readable: {fields['date']!r}")

    progress = _PROGRESS.search(fields["progress"])
    if not progress:
        raise TunnelParseError(
            f"PROGRESS not in 'built / current / total (x%)' form: "
            f"{fields['progress']!r}"
        )
    built, current, total, pct = progress.groups()

    def number(value):
        if value is None:
            return None
        n = float(value)
        return int(n) if n.is_integer() else n

    contract = re.sub(r"\s+", "", strip_marks(fields["contract"])).upper()

    return {
        "contract": contract,
        "report_date": report_date,
        "drive": normalise_drive(strip_marks(fields["drive"])),
        "rings_built": number(built),
        "current_ring": number(current),
        "total_rings": number(total),
        "pct_complete": number(pct),
        "tbm_location": strip_marks(fields.get("tbm_location") or "") or None,
        "instrumentation": strip_marks(fields.get("instrumentation") or "") or None,
        "issues": _issues(fields.get("issues")),
        "raw_message": text,
    }


# ── Flags ─────────────────────────────────────────────────────────────────────

# "!" plus the phone-keyboard glyphs engineers reach for instead of it.
_MARKS = "!‼❗❕⁉"
_VS = "️︎"          # emoji / text variation selectors

# A line that is only a courtesy is not a flag, however enthusiastic:
# "Thanks!" and "Noted!!" would otherwise fill the director's list.
_COURTESY = re.compile(
    r"^(thanks?( you)?|thank u|tq|ty|ok(ay)?|noted|received|well done|good job|"
    r"great|nice|congrats?|congratulations|good (morning|afternoon|evening|night)|"
    r"morning|hi|hello|yes|no|sure|done)"
    r"( (all|everyone|team|guys|sir|boss))?$",
    re.I,
)


def strip_marks(text: str) -> str:
    """Remove "!" marks, their emoji forms and formatting, and tidy the ends."""
    text = re.sub(f"[{_MARKS}{_VS}]", "", text)
    return _clean(text).strip(" -•·:")


def extract_flags(text: str) -> list[str]:
    """Every line containing a "!", marks stripped, in order.

    The line is kept whole — label included — so "ISSUES: [Gantry crane
    breakdown!!]" reads as "ISSUES: [Gantry crane breakdown]" and the director
    sees what kind of item it is.
    """
    flags: list[str] = []
    for line in text.splitlines():
        if not any(ch in line for ch in _MARKS):
            continue
        body = strip_marks(line)
        if not body or _COURTESY.match(body.strip(" .")):
            continue
        flags.append(body)
    return flags
