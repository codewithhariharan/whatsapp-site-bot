"""Measure message_parser.classify_and_parse against hand-labelled real messages.

Why this exists: the parser splits "Zone 3 P34:" into main/sub locations
inconsistently — the same message shape landed in main_location 315 times and in
sub_location 106 times across the live record, leaving 6,300 distinct
main_location values and a /daily report that scatters one zone across several
headings. Prompt changes aimed at that cannot be judged by eye, so this scores
them, and re-runs each case to separate a real improvement from model variance.

Every case is a real message from the group, hand-labelled with the intended
split. Labels encode one rule: main_location is the broad area ALONE (Zone 3,
U3, B2, CCW1, Exit 3, Vent shaft), and everything more specific goes to
sub_location.

Usage (needs ANTHROPIC_API_KEY, so in practice inside the api container):
    python evals/run_parser_eval.py                    # score once
    python evals/run_parser_eval.py --repeat 3         # also measure consistency
    python evals/run_parser_eval.py --save before.json
    python evals/run_parser_eval.py --compare before.json
"""
import argparse
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from message_parser import classify_and_parse  # noqa: E402

CASES = Path(__file__).with_name("parser_cases.jsonl")


def _norm(value) -> str:
    """Compare locations case- and punctuation-insensitively.

    'Zone 3' vs 'zone 3' vs 'Zone 3 ' are the same answer; the eval is about
    which field a token lands in, not about capitalisation.
    """
    return " ".join(str(value or "").lower().replace(",", " ").split())


def run_once(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        row = {"id": case["id"], "note": case.get("note", "")}
        try:
            parsed = classify_and_parse(case["msg"])
        except Exception as exc:
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            results.append(row)
            continue

        got_type = parsed.get("type")
        data = parsed.get("data") or {}
        row["type_ok"] = got_type == case["type"]
        row["got_type"] = got_type

        if case["main"] is None:            # non-log case: only the type matters
            row["main_ok"] = row["sub_ok"] = None
            row["ok"] = row["type_ok"]
        else:
            row["got_main"] = data.get("main_location", "")
            row["got_sub"] = data.get("sub_location", "")
            row["main_ok"] = _norm(row["got_main"]) == _norm(case["main"])
            row["sub_ok"] = _norm(row["got_sub"]) == _norm(case["sub"])
            row["ok"] = row["type_ok"] and row["main_ok"] and row["sub_ok"]
        results.append(row)
    return results


def summarise(runs: list[list[dict]]) -> dict:
    by_id = collections.defaultdict(list)
    for run in runs:
        for row in run:
            by_id[row["id"]].append(row)

    def rate(field):
        vals = [r[field] for run in runs for r in run if r.get(field) is not None]
        return (sum(vals) / len(vals) * 100) if vals else 0.0

    # A case is "stable" only if every repeat produced the same verdict. An
    # unstable case is not really passing, however often it happens to pass.
    unstable = [cid for cid, rows in by_id.items()
                if len({(r.get("got_main"), r.get("got_sub"), r.get("got_type"))
                        for r in rows}) > 1]

    return {
        "runs": len(runs),
        "cases": len(by_id),
        "overall_pass_pct": rate("ok"),
        "type_pct": rate("type_ok"),
        "main_location_pct": rate("main_ok"),
        "sub_location_pct": rate("sub_ok"),
        "unstable_cases": sorted(unstable),
        "per_case": {cid: all(r["ok"] for r in rows) for cid, rows in by_id.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1,
                    help="run every case N times to separate a fix from model variance")
    ap.add_argument("--save", help="write the summary to this JSON file")
    ap.add_argument("--compare", help="diff against a previously saved summary")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set — run this inside the api container.")
        return 2

    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    runs = []
    for i in range(args.repeat):
        print(f"run {i + 1}/{args.repeat} ...", flush=True)
        runs.append(run_once(cases))

    summary = summarise(runs)
    last = {r["id"]: r for r in runs[-1]}

    print(f"\n{'case':22} {'type':>5} {'main':>5} {'sub':>5}   got")
    print("-" * 96)
    for case in cases:
        r = last[case["id"]]
        if r.get("error"):
            print(f"{case['id']:22} {'ERR':>5}                 {r['error'][:52]}")
            continue
        mark = lambda v: " -  " if v is None else (" ok " if v else "FAIL")
        got = ""
        if case["main"] is not None:
            got = f"main={r.get('got_main','')!r} sub={r.get('got_sub','')!r}"
            if r["ok"]:
                got = ""
            else:
                got = f"want main={case['main']!r} sub={case['sub']!r}  |  {got}"
        elif not r["type_ok"]:
            got = f"want type={case['type']!r} got {r.get('got_type')!r}"
        print(f"{case['id']:22} {mark(r['type_ok']):>5} {mark(r['main_ok']):>5} "
              f"{mark(r['sub_ok']):>5}   {got[:70]}")

    print("\n" + "=" * 60)
    print(f"  overall pass    {summary['overall_pass_pct']:6.1f}%   "
          f"({summary['cases']} cases x {summary['runs']} run(s))")
    print(f"  type correct    {summary['type_pct']:6.1f}%")
    print(f"  main_location   {summary['main_location_pct']:6.1f}%   <- the inconsistency")
    print(f"  sub_location    {summary['sub_location_pct']:6.1f}%")
    if summary["runs"] > 1:
        n = len(summary["unstable_cases"])
        print(f"  unstable        {n} case(s) differed between runs"
              + (f": {', '.join(summary['unstable_cases'][:6])}" if n else ""))

    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n=== vs {args.compare} ===")
        for key in ("overall_pass_pct", "type_pct", "main_location_pct", "sub_location_pct"):
            delta = summary[key] - old[key]
            print(f"  {key:20} {old[key]:6.1f}% -> {summary[key]:6.1f}%  ({delta:+.1f})")
        fixed = [c for c, ok in summary["per_case"].items() if ok and not old["per_case"].get(c, False)]
        broke = [c for c, ok in summary["per_case"].items() if not ok and old["per_case"].get(c, False)]
        print(f"  fixed:  {', '.join(fixed) if fixed else '(none)'}")
        print(f"  BROKE:  {', '.join(broke) if broke else '(none)'}")

    if args.save:
        Path(args.save).write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"\nsaved -> {args.save}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
