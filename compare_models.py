"""Re-parse past posts with Gemini and compare against what Claude stored.

The parsing prompts were written for Claude. A model that reads them differently
would not fail loudly: it would file "Unknown" locations, drop a sub-location,
or turn chat into log rows, and nothing downstream checks. This runs the live
parsers over real messages whose Claude result is already in the database and
reports where the two disagree, before the switch is trusted.

Read-only. It runs SELECTs and model calls; it never writes a row.

Three samples, newest first by default:

  logs     daily_logs rows Claude filed. Gemini should also say "log" and
           extract the same locations and manpower. Descriptions are compared
           loosely, since rewording is expected and harmless.
  skipped  posts Claude classed as chat or a question (pending_messages
           status 'skipped'). Gemini filing one of these would ADD a row.
  tunnel   tunnel_updates rows. The verbatim blocks are parsed in Python and
           cannot differ; only the extracted numbers are compared.

Rows backfilled from the spreadsheet are excluded: their raw_message is a
"[backfill] <file>" marker, not a message, so there is nothing to re-parse.

Run it inside the api container, so it uses the same credentials as the bot:

    docker compose exec api python compare_models.py
    docker compose exec api python compare_models.py --logs 40 --random
    docker compose exec api python compare_models.py --out /tmp/compare.json
"""

import argparse
import json
import re
import sys
import time
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

import database as db
import llm
from config import settings
from message_parser import classify_and_parse
from tunnel_parser import _NUMBER_ONLY, _NUMERIC_FIELDS, parse_tunnel_update

# Below this, a description counts as reworded rather than the same. Reworded
# descriptions are listed for a human to read but are not counted as problems.
_DESCRIPTION_SIMILAR = 0.8

_LOG_FIELDS = ("main_location", "sub_location", "manpower")


# ── Comparison ────────────────────────────────────────────────────────────────

def norm(value) -> str:
    """Whitespace and case are not real differences between two parsers."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def similarity(a, b) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def compare_log(stored: dict, parsed: dict) -> dict:
    """Compare a stored daily_logs row with Gemini's parse of its message.

    `problems` are disagreements that change what the record says; `notes` are
    differences a human may want to read but that are not wrong in themselves.
    """
    problems, notes = [], []

    if parsed.get("type") != "log":
        problems.append(f"classified as {parsed.get('type')!r}, Claude filed a log")
        return {"problems": problems, "notes": notes}

    data = parsed.get("data") or {}
    for field in _LOG_FIELDS:
        old, new = stored.get(field), data.get(field)
        if norm(old) != norm(new):
            problems.append(f"{field}: {old!r} -> {new!r}")

    if (norm(stored.get("main_location")) != "unknown"
            and norm(data.get("main_location")) == "unknown"):
        problems.append("location lost: Gemini fell back to 'Unknown'")

    score = similarity(stored.get("description"), data.get("description"))
    if score < _DESCRIPTION_SIMILAR:
        notes.append(
            f"description reworded ({score:.0%} similar): "
            f"{stored.get('description')!r} -> {data.get('description')!r}"
        )
    return {"problems": problems, "notes": notes}


def compare_skipped(parsed: dict) -> dict:
    """A post Claude skipped. Filing it as a log or panel would add a row."""
    kind = parsed.get("type")
    if kind in ("log", "dwall"):
        return {"problems": [f"Claude skipped this; Gemini would file a {kind}"],
                "notes": []}
    return {"problems": [], "notes": []}


def _same_number(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def compare_tunnel(stored: dict, parsed: dict | None) -> dict:
    """Compare the extracted numbers of a stored tunnel update with Gemini's."""
    if parsed is None:
        return {"problems": ["Gemini's run no longer recognises this as an update"],
                "notes": []}

    if not any(parsed.get(f) not in (None, "") for f in _NUMERIC_FIELDS):
        # extract_numbers swallows its own errors (the verbatim text is still
        # stored), so a failed call shows up here as every number missing.
        return {"problems": ["no numbers extracted -- the model call likely "
                             "failed; see the traceback above"], "notes": []}

    problems, notes = [], []
    for field in _NUMERIC_FIELDS:
        old, new = stored.get(field), parsed.get(field)
        if isinstance(old, Decimal):        # NUMERIC columns; show 5.2, not Decimal('5.2')
            old = float(old) if old != old.to_integral() else int(old)
        if old in (None, "") and new in (None, ""):
            continue
        if old in (None, ""):
            notes.append(f"{field}: Claude had nothing, Gemini found {new!r}")
            continue
        if new in (None, ""):
            problems.append(f"{field}: {old!r} lost")
            continue
        same = (_same_number(old, new) if field in _NUMBER_ONLY
                else norm(old) == norm(new))
        if not same:
            problems.append(f"{field}: {old!r} -> {new!r}")
    return {"problems": problems, "notes": notes}


# ── Samples ───────────────────────────────────────────────────────────────────

def _order(random: bool, newest: str) -> str:
    return "random()" if random else f"{newest} DESC"


def sample_logs(n: int, random: bool, group: str | None) -> list[dict]:
    return db._fetch(
        f"""
        SELECT id, group_id, log_date, main_location, sub_location,
               description, manpower, raw_message
        FROM daily_logs
        WHERE raw_message IS NOT NULL AND raw_message <> ''
          AND raw_message NOT LIKE '[backfill]%%'
          AND (%s::text IS NULL OR group_id = %s)
        ORDER BY {_order(random, 'logged_at')}
        LIMIT %s
        """,
        (group, group, n),
    )


def sample_skipped(n: int, random: bool, group: str | None) -> list[dict]:
    # Tunnel groups never go through the site classifier, so their skipped
    # posts say nothing about it.
    tunnel = sorted(settings.tunnel_group_ids) or [""]
    return db._fetch(
        f"""
        SELECT id, group_id, text AS raw_message, received_at
        FROM pending_messages
        WHERE status = 'skipped'
          AND group_id <> ALL(%s)
          AND (%s::text IS NULL OR group_id = %s)
        ORDER BY {_order(random, 'received_at')}
        LIMIT %s
        """,
        (tunnel, group, group, n),
    )


def sample_tunnel(n: int, random: bool, group: str | None) -> list[dict]:
    return db._fetch(
        f"""
        SELECT *
        FROM tunnel_updates
        WHERE raw_message IS NOT NULL AND raw_message <> ''
          AND (%s::text IS NULL OR group_id = %s)
        ORDER BY {_order(random, 'logged_at')}
        LIMIT %s
        """,
        (group, group, n),
    )


# ── Running ───────────────────────────────────────────────────────────────────

def _call(fn, *args):
    """One retry on an outage or rate limit, so a blip is not read as a verdict."""
    try:
        return fn(*args)
    except Exception as exc:
        if not llm.is_transient(exc):
            raise
        time.sleep(10)
        return fn(*args)


def run_one(kind: str, row: dict) -> dict:
    text = row["raw_message"]
    started = time.monotonic()
    try:
        if kind == "tunnel":
            parsed = _call(parse_tunnel_update, text, date.fromisoformat(row["update_date"]))
            result = compare_tunnel(row, parsed)
        else:
            parsed = _call(classify_and_parse, text)
            result = (compare_log(row, parsed) if kind == "logs"
                      else compare_skipped(parsed))
    except Exception as exc:
        parsed = None
        result = {"problems": [f"error: {type(exc).__name__}: {exc}"[:300]],
                  "notes": []}
    return {
        "kind": kind,
        "id": row["id"],
        "message": text,
        "gemini": parsed,
        "seconds": round(time.monotonic() - started, 1),
        **result,
    }


def _first_line(text: str, width: int = 70) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line if len(line) <= width else line[: width - 3] + "..."


def report(results: list[dict]) -> str:
    out = []
    for kind in ("logs", "skipped", "tunnel"):
        rows = [r for r in results if r["kind"] == kind]
        if not rows:
            out.append(f"\n== {kind}: no rows to compare")
            continue
        bad = [r for r in rows if r["problems"]]
        out.append(f"\n== {kind}: {len(rows) - len(bad)}/{len(rows)} agree")
        for r in rows:
            if not (r["problems"] or r["notes"]):
                continue
            mark = "✗" if r["problems"] else "~"
            out.append(f"\n{mark} [{r['id']}] {_first_line(r['message'])}")
            out += [f"    - {p}" for p in r["problems"]]
            out += [f"    · {n}" for n in r["notes"]]

    total = len(results)
    failed = sum(1 for r in results if r["problems"])
    lost = sum(1 for r in results
               if any("location lost" in p for p in r["problems"]))
    errors = sum(1 for r in results
                 if any(p.startswith("error:") for p in r["problems"]))
    slowest = max((r["seconds"] for r in results), default=0)
    out.append(
        f"\n== summary: {total - failed}/{total} agree; "
        f"{lost} lost a location to 'Unknown'; {errors} errored; "
        f"slowest call {slowest}s"
    )
    out.append(f"   models: fast={llm.FAST_MODEL} smart={llm.SMART_MODEL}")
    out.append("   ✗ = changes what the record says   ~ = reworded, read and judge")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--logs", type=int, default=20, help="daily_logs rows (default 20)")
    parser.add_argument("--skipped", type=int, default=10, help="skipped posts (default 10)")
    parser.add_argument("--tunnel", type=int, default=5, help="tunnel updates (default 5)")
    parser.add_argument("--group", help="limit to one group JID")
    parser.add_argument("--random", action="store_true", help="random sample, not newest")
    parser.add_argument("--out", help="also write every result as JSON to this path")
    args = parser.parse_args(argv)

    samples = [
        ("logs", sample_logs(args.logs, args.random, args.group)),
        ("skipped", sample_skipped(args.skipped, args.random, args.group)),
        ("tunnel", sample_tunnel(args.tunnel, args.random, args.group)),
    ]
    total = sum(len(rows) for _, rows in samples)
    print(f"Comparing {total} messages against Gemini ({llm.FAST_MODEL})...",
          file=sys.stderr)

    results = []
    for kind, rows in samples:
        for i, row in enumerate(rows, 1):
            print(f"  {kind} {i}/{len(rows)}", file=sys.stderr, end="\r")
            results.append(run_one(kind, row))
    print(file=sys.stderr)

    print(report(results))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nfull results: {args.out}")
    return 1 if any(r["problems"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
