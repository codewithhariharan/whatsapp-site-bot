"""Natural-language Q&A over the tunnel record.

Backs /ask in a contract's tunnel group (that group's rows only) and every
question in the master group (all contracts, for the senior group director).

Same three-stage shape as ai_handler: the model writes one SELECT, Postgres does
the work across every row, and only the result reaches the answering call. The
tunnel record must never reach into daily_logs or dwall_panels, and the reverse.

The generic machinery (JSON extraction, row fitting, the read-only query guard)
is imported from ai_handler and database rather than copied; only the parts that
are genuinely tunnel-specific live here.
"""

import logging
import re

import llm
from ai_handler import _extract_json, _fit
from sitetime import site_today
import database as db

logger = logging.getLogger("site_bot")

_SQL_MODEL = llm.SMART_MODEL
_ANSWER_MODEL_SMALL = llm.FAST_MODEL
_ANSWER_MODEL_LARGE = llm.SMART_MODEL
_SMALL_RESULT_ROWS = 20

_QUERY_TIMEOUT_MS = 5000
_MAX_RESULT_ROWS = 200
_CONTEXT_CHAR_BUDGET = 25_000
_MAX_FALLBACK_ROWS = 40

# Anything the model writes must stay inside the tunnel tables.
_OUT_OF_SCOPE_TABLES = (
    "daily_logs", "dwall_panels", "pending_messages", "location_order",
    "reorder_sessions", "tunnel_updates", "groups",
)


_SCHEMA = """TABLE tunnel_progress  -- one row per contract, per report date, per drive
    contract        TEXT     'CR146' — each contract has its own WhatsApp group
    report_date     DATE     the DATE written in the update (the day it reports on)
    drive           TEXT     'EB - Main Drive 3', 'WB - Main Drive 2'
    rings_built     NUMERIC  rings built on that report's day
    current_ring    NUMERIC  the cumulative ring reached (running total)
    total_rings     NUMERIC  rings in the whole drive
    pct_complete    NUMERIC  the % completion AS WRITTEN by the engineer
    tbm_location    TEXT     'at side table of AMK Ave 3', 'undercrossing PIE'
    instrumentation TEXT     'All within AL', or which instruments breached / trend
                             toward AL (Alert Level), e.g. 'LG3053 breached AL'
    issues          TEXT     what held work up; NULL when the update said [None]
    sender_name     TEXT     engineer who sent it
    sent_at         TIMESTAMPTZ  when it was posted (site time is Asia/Singapore)

  The update's PROGRESS line 'a / b / c (p%)' is rings_built / current_ring /
  total_rings (pct_complete).

TABLE tunnel_flags  -- every line an engineer wrote with a "!" in it
    contract    TEXT
    drive       TEXT     set when the flag came from an update
    flag_text   TEXT     the line, "!" marks removed
    sender_name TEXT
    sent_at     TIMESTAMPTZ  when it was posted
    sent_date   DATE     the day it was posted, in site time

CRITICAL FACTS — ignoring these produces wrong answers:

* ONLY these two tables may be queried. The site-work tables (daily_logs,
  dwall_panels) and the old tunnel_updates table are out of scope.

* Questions about what is CRITICAL, FLAGGED, URGENT, "needs attention", "!!"
  or "exclamation" are about tunnel_flags, and tunnel_flags ALONE — filter it by
  sent_date, the day the flag was posted. Do not widen them into `issues`.

* rings_built is per day, so rings over a period is SUM(rings_built).
  current_ring is cumulative: never SUM it; its change over a period is
  MAX(current_ring) - MIN(current_ring) for one drive.

* A contract can report more than one drive; keep drives apart when comparing
  progress or completion.

* pct_complete is the engineer's figure; it can disagree with
  current_ring / total_rings. Report the written figure; mention a mismatch only
  if the question is about accuracy.

* "Latest" or "now" means the row with the highest report_date for that
  contract and drive."""


_SCOPED_RULE = """2. ALWAYS scope to the group: include
   `group_id = current_setting('app.group_id')`
   for every tunnel_progress and tunnel_flags reference. Write it exactly like
   that — the value is set for you."""

_ALL_CONTRACTS_RULE = """2. This is the director's view of EVERY contract. Do not filter by group
   unless the question names a contract; then filter by contract."""


_SQL_SYSTEM = """You write ONE PostgreSQL SELECT that answers a question about a tunnelling
progress record, then someone else turns your result into a sentence.

{schema}

RULES
1. Emit exactly one SELECT. No INSERT/UPDATE/DELETE/DDL — the connection is
   read-only and they will fail.
{scope_rule}
   Use NO bound parameters and no placeholders of any kind; inline every
   literal value directly into the SQL.
3. Return the SMALLEST result that answers the question. Aggregate (COUNT, MIN,
   MAX, SUM, GROUP BY) rather than returning raw rows whenever the question is
   about totals, extremes or trends. Add a LIMIT.
4. Include the columns needed to make the answer readable — always return
   contract (and drive, report_date where relevant) alongside what was asked.
5. If the question cannot be answered with SQL because it turns on the nuance
   of free-text wording, return {{"sql": null}} and a keyword search runs instead.

Respond ONLY with JSON, no explanation, no code fences:
{{"sql": "SELECT ...", "reasoning": "one short line"}}

EXAMPLES   (dates come from the user turn — never from these examples)

"what are the critical items today?"   (if the user turn said today is 2026-01-04)
{{"sql": "SELECT contract, drive, flag_text, sent_at FROM tunnel_flags WHERE {scope_sql}sent_date = '2026-01-04' ORDER BY contract, sent_at LIMIT 100", "reasoning": "flags posted that day"}}

"where is every TBM now?"
{{"sql": "SELECT DISTINCT ON (contract, drive) contract, drive, report_date, current_ring, total_rings, pct_complete, tbm_location FROM tunnel_progress WHERE {scope_sql}TRUE ORDER BY contract, drive, report_date DESC LIMIT 50", "reasoning": "latest row per contract and drive"}}

"how many rings did CR146 build this week?"   (if today is 2026-01-05, a Monday)
{{"sql": "SELECT contract, drive, SUM(rings_built) AS rings, MIN(report_date) AS from_date, MAX(report_date) AS to_date FROM tunnel_progress WHERE {scope_sql}contract = 'CR146' AND report_date BETWEEN '2026-01-05' AND '2026-01-05' GROUP BY contract, drive", "reasoning": "rings_built is per day, so SUM"}}

"which contracts have instruments breaching AL?"
{{"sql": "SELECT DISTINCT ON (contract, drive) contract, drive, report_date, instrumentation FROM tunnel_progress WHERE {scope_sql}instrumentation NOT ILIKE 'all within al%' ORDER BY contract, drive, report_date DESC LIMIT 50", "reasoning": "latest instrumentation that is not all-clear"}}

"what issues did CR125 have?"
{{"sql": "SELECT report_date, drive, issues FROM tunnel_progress WHERE {scope_sql}contract = 'CR125' AND issues IS NOT NULL ORDER BY report_date DESC LIMIT 30", "reasoning": "issues column, None filtered out"}}
"""


def _sql_system(all_contracts: bool) -> str:
    return _SQL_SYSTEM.format(
        schema=_SCHEMA,
        scope_rule=_ALL_CONTRACTS_RULE if all_contracts else _SCOPED_RULE,
        scope_sql="" if all_contracts else "group_id = current_setting('app.group_id') AND ",
    )


def _write_sql(question: str, all_contracts: bool,
               previous_error: str | None = None,
               previous_sql: str | None = None) -> dict:
    """Ask the model for a query. On a retry, show it what Postgres said."""
    user = f"Today is {site_today().isoformat()}.\n\nQuestion:\n\"\"\"{question}\"\"\""
    if previous_error:
        user += (
            f"\n\nYour previous attempt failed. Fix it.\n"
            f"Query:\n{previous_sql}\n\nPostgres said:\n{previous_error}"
        )
    reply = llm.generate(
        user, model=_SQL_MODEL, max_tokens=400,
        system=_sql_system(all_contracts), json_output=True,
    )
    return _extract_json(reply)


def _scope_problem(sql: str, all_contracts: bool) -> str | None:
    """Why this query must not run, or None."""
    normalised = sql.replace('"', "'")
    lowered = normalised.lower()
    # The tunnel record is walled off from the site record. A model reaching
    # for daily_logs here would answer with another group's data, which is
    # worse than not answering — so refuse and make it try again.
    for table in _OUT_OF_SCOPE_TABLES:
        if re.search(rf"\b{table}\b", lowered):
            return (f"{table} is out of scope here. Query tunnel_progress and "
                    f"tunnel_flags only.")
    if not all_contracts and "current_setting('app.group_id')" not in normalised:
        return ("every tunnel_progress / tunnel_flags reference must be scoped with "
                "group_id = current_setting('app.group_id')")
    return None


def _run_sql_path(group_id: str, question: str,
                  all_contracts: bool) -> tuple[str, bool, int] | None:
    """Write and run a query, retrying once on error. None = fall back."""
    attempt_error = attempt_sql = None

    for attempt in (1, 2):
        try:
            plan = _write_sql(question, all_contracts, attempt_error, attempt_sql)
        except Exception:
            logger.exception("tunnel /ask: SQL generation failed (attempt %d)", attempt)
            return None

        sql = (plan or {}).get("sql")
        if not sql:
            return None

        problem = _scope_problem(sql, all_contracts)
        if problem:
            attempt_error, attempt_sql = problem, sql
            continue

        try:
            rows, truncated = db.run_readonly_query(
                sql, group_id,
                timeout_ms=_QUERY_TIMEOUT_MS, max_rows=_MAX_RESULT_ROWS,
            )
        except db.QueryError as exc:
            logger.warning("tunnel /ask: query rejected (attempt %d): %s", attempt, exc)
            attempt_error, attempt_sql = str(exc), sql
            continue

        logger.info("tunnel /ask: %d rows from %s",
                    len(rows), sql.replace("\n", " ")[:200])
        blob, shown = _fit(rows, _CONTEXT_CHAR_BUDGET)
        note = (
            f" (showing the first {shown}; the query matched more)" if truncated else ""
        )
        return (
            f"QUERY RUN AGAINST THE FULL TUNNEL RECORD:\n{sql}\n\n"
            f"RESULT — {shown} row(s){note}:\n{blob}",
            truncated,
            shown,
        )

    return None


_STOPWORDS = {
    "what", "when", "where", "which", "who", "why", "how", "was", "were",
    "is", "are", "the", "a", "an", "in", "on", "at", "for", "of", "to",
    "did", "do", "does", "and", "or", "it", "we", "there", "any", "all",
    "tunnel", "update", "updates", "report", "reported", "happened",
}


def _keyword_fallback(group_id: str | None, question: str) -> str:
    """Keyword retrieval, for when SQL is the wrong tool or it failed twice."""
    words = [w.strip(".,?!'\"").lower() for w in question.split()]
    keywords = [w for w in words if len(w) > 2 and w not in _STOPWORDS][:6]

    rows, total = db.search_tunnel_progress(
        keywords=keywords, group_id=group_id, limit=_MAX_FALLBACK_ROWS
    )
    blob, shown = _fit(rows, _CONTEXT_CHAR_BUDGET)
    if total == 0:
        header = "TUNNEL UPDATES — no matching entries."
    elif shown < total:
        header = (f"TUNNEL UPDATES — showing the {shown} most relevant "
                  f"of {total} matching entries:")
    else:
        header = f"TUNNEL UPDATES — all {total} matching entries:"
    return (
        f"KEYWORD SEARCH (SQL was not usable for this question). "
        f"Terms: {keywords}\n\n{header}\n{blob}"
    )


def answer_tunnel_query(group_id: str, question: str,
                        all_contracts: bool = False) -> str:
    """Answer a natural language question about the tunnel record.

    `all_contracts` is the master group's view; otherwise only `group_id`'s
    rows are visible. Synchronous: psycopg and the Gemini client both block.
    Callers on the event loop must run this in a thread.
    """
    sql_result = _run_sql_path(group_id, question, all_contracts)

    if sql_result is not None:
        data, truncated, rows_shown = sql_result
        provenance = (
            "The result below comes from a query run across the ENTIRE tunnel "
            "record, not a sample. If it reports a count or an extreme, that "
            "figure is complete and you can state it plainly."
        )
        if truncated:
            provenance += (
                " The row list was cut off at the display limit, so say the "
                "listing is partial."
            )
    else:
        rows_shown = _MAX_FALLBACK_ROWS
        data = _keyword_fallback(None if all_contracts else group_id, question)
        provenance = (
            "The entries below are the best keyword matches, NOT the whole "
            "record. Do not give totals or say something never happened — say "
            "what you found in the entries searched."
        )

    context = f"""You are a tunnelling progress assistant for a construction programme.
Answer the question using only the data below.
Today is {site_today().isoformat()} (site local time, UTC+8). Work out "today",
"yesterday" and "last week" from that date and no other — never state a date you
were not given here.

Be concise and direct.

The answer is sent straight into a WhatsApp group, which does not render
Markdown. Use WhatsApp formatting only: *single asterisks* for bold, and "•" or
"-" for bullets. Never use #, ##, or ** — they appear literally as punctuation
in the message. Keep it short enough to read on a phone.

When the answer covers more than one contract, group it by contract: one *bold
heading* per contract (e.g. *CR146*), then one bullet per drive or item.

{provenance}

What the fields mean — do not infer them from the name:
• PROGRESS 'a / b / c (p%)' = rings built that day / current (cumulative) ring /
  total rings in the drive (percent complete as written).
• rings_built is per day and can be added up; current_ring is a running total
  and must not be.
• AL = Alert Level for ground instrumentation. "All within AL" is the all-clear;
  a named instrument "breached AL" or "close to AL" needs attention.
• tunnel_flags are the lines engineers marked with "!" — report them word for
  word, with the contract each came from. Never substitute `issues` for them.

If the data below answers the question, ANSWER IT — lead with the figure or the
finding, and do not preface it with a disclaimer.

Reserve "I could not find a record of X" for a genuinely empty result, and
never turn it into "X did not happen".

If the record is older than the period asked about, give it and say how old it
is. That is an answer, not a miss.

{data}

Question: {question}"""

    model = (_ANSWER_MODEL_SMALL if rows_shown <= _SMALL_RESULT_ROWS
             else _ANSWER_MODEL_LARGE)
    return llm.generate(context, model=model, max_tokens=1500)
