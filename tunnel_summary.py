"""The /tunnel email: one day's tunnel updates, summarised.

Four parts, in this order:

  1. Overview — per contract and drive, the day's rings (Day + Night), the ring
     reached and the % at the end of the day. Computed here, not by the model,
     so every figure in it is exactly what the engineers sent.
  2. Highlights — a short read of the day written by Gemini: what moved, what
     is behind, what needs attention. Optional: if the model fails the email
     still goes, saying the highlights are missing.
  3. Critical items — the "!" lines, word for word, as /!! shows them.
  4. The updates themselves, each in the standard template, so the email reads
     the way the groups write.
"""

import html
import json
import logging
from datetime import date

import llm

logger = logging.getLogger("site_bot")

_HIGHLIGHTS_MODEL = llm.SMART_MODEL


def _num(value) -> str:
    if value is None or value == "":
        return "?"
    n = float(value)
    return str(int(n)) if n.is_integer() else f"{n:g}"


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def when(day: date) -> str:
    return day.strftime("%d %b %Y").lstrip("0")


# ── The template ──────────────────────────────────────────────────────────────

def format_update(row: dict) -> str:
    """One update in the standard template, as the groups send it."""
    progress = (f"{_num(row.get('rings_built'))} / {_num(row.get('current_ring'))} / "
                f"{_num(row.get('total_rings'))}")
    if row.get("pct_complete") is not None:
        progress += f" ({_num(row['pct_complete'])}%)"

    lines = [
        "*TUNNEL PROGRESS UPDATE*",
        f"*CONTRACT*: {row['contract']}",
        f"*DATE*: {_day(row['report_date']).strftime('%d-%b-%Y')}",
    ]
    if row.get("shift"):
        lines.append(f"*SHIFT*: {row['shift']}")
    lines += [f"*DRIVE*: {row['drive']}", f"*PROGRESS*: {progress}"]
    for label, field in (("Follow-on works", "follow_on_works"),
                         ("Soil disposal", "soil_disposal"),
                         ("On site storage", "on_site_storage")):
        if row.get(field):
            lines.append(f"- {label}: {row[field]}")
    lines += [
        f"*TBM LOCATION*: {row.get('tbm_location') or '-'}",
        f"*INSTRUMENTATION*: {row.get('instrumentation') or '-'}",
        f"*ISSUES*: [{row.get('issues') or 'None'}]",
    ]
    return "\n".join(lines)


# ── Overview ──────────────────────────────────────────────────────────────────

def overview(rows: list[dict]) -> list[dict]:
    """One line per contract and drive. `rows` come Day before Night.

    rings_built is per shift, so the day's rings are the sum; current_ring is
    cumulative, so the day ends on the last shift's.
    """
    lines: dict[tuple[str, str], dict] = {}
    for row in rows:
        line = lines.setdefault((row["contract"], row["drive"]), {
            "contract": row["contract"], "drive": row["drive"],
            "rings": 0, "shifts": [], "instrumentation": [], "issues": [],
        })
        if row.get("rings_built") is not None:
            line["rings"] += float(row["rings_built"])
        if row.get("shift"):
            line["shifts"].append(row["shift"])
        line["current_ring"] = row.get("current_ring")
        line["total_rings"] = row.get("total_rings")
        line["pct_complete"] = row.get("pct_complete")
        line["tbm_location"] = row.get("tbm_location")
        instrumentation = row.get("instrumentation")
        if instrumentation and instrumentation not in line["instrumentation"]:
            line["instrumentation"].append(instrumentation)
        if row.get("issues"):
            label = f"{row['shift']}: {row['issues']}" if row.get("shift") else row["issues"]
            line["issues"].append(label)
    return list(lines.values())


def _instrumentation_note(values: list[str]) -> str:
    concerns = [v for v in values if not v.lower().startswith("all within al")]
    return "; ".join(concerns) if concerns else "All within AL"


# ── Highlights ────────────────────────────────────────────────────────────────

_HIGHLIGHTS_PROMPT = """You are writing the highlights of a daily tunnelling progress email for a
senior director. Below are the day's updates, as JSON, and any lines the
engineers marked critical with "!".

Field meanings — do not infer them from the name:
- rings_built / current_ring / total_rings (pct_complete): rings built in that
  shift / cumulative ring reached / rings in the drive (% as written).
- A day has a Day and a Night shift per drive; the day's rings are the sum of
  both. current_ring at the end of the day is the Night shift's.
- soil_disposal compares soil taken away with rings excavated. A negative delta
  means disposal is falling behind excavation.
- on_site_storage 'a / b rings' is muck held on site against capacity; near
  capacity can stop the TBM.
- AL = Alert Level for ground instrumentation. "All within AL" is the
  all-clear; anything else needs attention.

Write plain text, no Markdown (no #, no **). One line with the contract code in
capitals, then 1 to 4 lines starting with "- ", per contract. Lead with the
day's rings and the % complete, then only what needs attention: delays, TBM
stoppages, disposal behind excavation, storage near capacity, instruments near
or over AL, critical items. Say "no issues" when there are none. Use only the
figures given — never estimate or invent one. Keep it under 250 words.

Day: {day}

UPDATES:
{updates}

CRITICAL ITEMS:
{flags}"""


def highlights(day: date, rows: list[dict], flags: list[dict]) -> str:
    """Gemini's read of the day. Blocking; raises on failure."""
    keep = ("contract", "shift", "drive", "rings_built", "current_ring",
            "total_rings", "pct_complete", "follow_on_works", "soil_disposal",
            "on_site_storage", "tbm_location", "instrumentation", "issues")
    updates = [{k: row.get(k) for k in keep} for row in rows]
    flag_lines = [f"{f.get('contract') or '?'}: {f['flag_text']}" for f in flags]
    prompt = _HIGHLIGHTS_PROMPT.format(
        day=when(day),
        updates=json.dumps(updates, default=str, indent=1),
        flags="\n".join(flag_lines) or "None",
    )
    return llm.generate(
        prompt, model=_HIGHLIGHTS_MODEL, max_tokens=1500,
        cut_off_note="\n\n(The highlights were cut short — see the updates below.)",
    )


# ── The email ─────────────────────────────────────────────────────────────────

_BOX = ("background:#F5F5F5;border:1px solid #BFBFBF;padding:8px 12px;"
        "font-family:Consolas,'Courier New',monospace;font-size:13px;"
        "white-space:pre-wrap;margin:0 0 12px 0")
_CELL = "border:1px solid #BFBFBF;padding:4px 8px;text-align:left;vertical-align:top"


def build_email(day: date, rows: list[dict], flags: list[dict],
                ai_text: str | None, scope: str) -> tuple[str, str, str]:
    """(subject, plain text, html) for one day's updates.

    `scope` names whose updates these are: a contract, or "All contracts".
    """
    contracts = sorted({r["contract"] for r in rows})
    subject = f"Tunnel Progress Summary – {scope} – {when(day)}"
    counts = (f"{len(rows)} update{'s' if len(rows) != 1 else ''} from "
              f"{len(contracts)} contract{'s' if len(contracts) != 1 else ''}")
    lines = overview(rows)
    missing_ai = "(Highlights unavailable — the summary model did not answer. " \
                 "The figures below are complete.)"

    # Plain text.
    text = [f"TUNNEL PROGRESS SUMMARY – {when(day)}", f"{scope} · {counts}", "",
            "OVERVIEW"]
    for line in lines:
        pct = f" ({_num(line['pct_complete'])}%)" if line["pct_complete"] is not None else ""
        shifts = f" [{' + '.join(line['shifts'])}]" if line["shifts"] else ""
        text.append(
            f"- {line['contract']} {line['drive']}: {_num(line['rings'])} rings{shifts}, "
            f"at ring {_num(line['current_ring'])} / {_num(line['total_rings'])}{pct}. "
            f"Instrumentation: {_instrumentation_note(line['instrumentation'])}."
        )
    text += ["", "HIGHLIGHTS", ai_text or missing_ai]
    if flags:
        text += ["", "CRITICAL ITEMS (!)"]
        text += [f"- {f.get('contract') or '?'}: {f['flag_text']}" for f in flags]
    text += ["", "UPDATES"]
    for contract in contracts:
        text += ["", f"== {contract} =="]
        for row in rows:
            if row["contract"] == contract:
                text += [format_update(row), ""]
    plain = "\n".join(text).rstrip() + "\n"

    # HTML.
    e = html.escape
    h = [f"<h2 style='margin:0'>Tunnel Progress Summary – {e(when(day))}</h2>",
         f"<p style='color:#555;margin:4px 0 16px'>{e(scope)} · {e(counts)}</p>",
         "<h3>Overview</h3>",
         f"<table style='border-collapse:collapse;font-size:13px'><tr>"
         + "".join(f"<th style='{_CELL};background:#EEE'>{c}</th>" for c in (
             "Contract", "Drive", "Rings today", "Ring reached", "Total",
             "% complete", "Instrumentation", "Issues"))
         + "</tr>"]
    for line in lines:
        cells = (
            line["contract"], line["drive"],
            _num(line["rings"]) + (f" ({' + '.join(line['shifts'])})" if line["shifts"] else ""),
            _num(line["current_ring"]), _num(line["total_rings"]),
            _num(line["pct_complete"]) + "%" if line["pct_complete"] is not None else "",
            _instrumentation_note(line["instrumentation"]),
            "; ".join(line["issues"]) or "None",
        )
        h.append("<tr>" + "".join(f"<td style='{_CELL}'>{e(str(c))}</td>" for c in cells)
                 + "</tr>")
    h.append("</table>")
    h += ["<h3>Highlights</h3>",
          f"<div style='white-space:pre-wrap;font-size:14px'>{e(ai_text or missing_ai)}</div>"]
    if flags:
        h.append("<h3 style='color:#9C0006'>Critical items (!)</h3><ul>")
        h += [f"<li><b>{e(f.get('contract') or '?')}</b>: {e(f['flag_text'])}</li>"
              for f in flags]
        h.append("</ul>")
    h.append("<h3>Updates</h3>")
    for contract in contracts:
        h.append(f"<h4 style='margin:12px 0 6px'>{e(contract)}</h4>")
        h += [f"<pre style=\"{_BOX}\">{e(format_update(row))}</pre>"
              for row in rows if row["contract"] == contract]
    body = "\n".join(h)
    page = (f"<html><body style='font-family:Arial,sans-serif;color:#222'>"
            f"{body}</body></html>")
    return subject, plain, page
