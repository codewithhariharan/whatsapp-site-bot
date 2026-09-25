"""Natural-language Q&A over the tunnel record (backs /ask in a tunnel group).

Same three-stage shape as ai_handler: the model writes one SELECT, Postgres does
the work across every row, and only the result reaches the answering call. What
differs is the schema it is given and the table it is scoped to — a tunnel
group's /ask must never see daily_logs or dwall_panels, and the reverse.

The generic machinery (JSON extraction, row fitting, the read-only query guard)
is imported from ai_handler and database rather than copied; only the parts that
are genuinely tunnel-specific live here.
"""

import json
import logging

import anthropic

from ai_handler import _extract_json, _fit
from config import settings
from sitetime import site_today
import database as db

logger = logging.getLogger("site_bot")

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

_SQL_MODEL = "claude-sonnet-4-6"
_ANSWER_MODEL_SMALL = "claude-haiku-4-5-20251001"
_ANSWER_MODEL_LARGE = "claude-sonnet-4-6"
_SMALL_RESULT_ROWS = 20

_QUERY_TIMEOUT_MS = 5000
_MAX_RESULT_ROWS = 200
_CONTEXT_CHAR_BUDGET = 25_000
_MAX_FALLBACK_ROWS = 40


_SCHEMA = """TABLE tunnel_updates  -- one row per contract per reporting day
    contract       TEXT NOT NULL  'P103' — the contract code alone
    title_line     TEXT           the raw first line, e.g. 'P103 Tunnel Update'
    update_date    DATE NOT NULL  the date WRITTEN IN the message, not when it arrived
    drive_name     TEXT           'LDTBM Main Drive'
    sender_name    TEXT           engineer who sent it
    logged_at      TIMESTAMPTZ    when the message actually arrived (UTC; site is UTC+8)

  -- Verbatim blocks, exactly as the engineer wrote them:
    main_drive     TEXT   the 'LDTBM Main Drive' bullets: mined, ring built, first stage concrete
    tbm_progress   TEXT   the 'TBM Progress' bullets: shifts, % completion, TBM position, instrumentation
    delays         TEXT   the 'Delays' block: soil disposal, loads, storage, subcontractor
    exclamation    TEXT   ** the critical / flagged item ** — the text the engineer put between
                          the ‼ markers, e.g. 'CHANGING TBM MACHINE'. NULL or '' on most days.
    other_sections JSONB  {header: body} for any section outside the three above

  -- The same numbers, pulled out of those blocks so they can be counted:
    mined_from, mined_to, ring_built_from, ring_built_to    TEXT  ring refs like 'P1203'
    fsc_shift, fsc_cumulative        NUMERIC  first stage concrete, this report / to date
    rings_total                      NUMERIC  rings in the whole drive (the denominator)
    day_shift_rings, day_shift_cumulative      NUMERIC
    night_shift_rings, night_shift_cumulative  NUMERIC
        -- 'Day Shift: 3/1205/1759' means 3 rings this shift, cumulative ring 1205, 1759 total
    pct_completion                   NUMERIC  68.73 (a number, not a string, no % sign)
    tbm_location                     TEXT     'Punggol Central Road'
    instrumentation                  TEXT     'All within AL'
    delay_flag                       TEXT     'No', or 'Yes' plus a reason
    ds_loads, ns_loads               NUMERIC  soil disposal loads, day / night shift
    total_disposed_loads             NUMERIC
    disposed_rings_equiv             NUMERIC  the 'approx. 5.2 rings' figure
    rings_excavated                  NUMERIC
    delta_disposal                   NUMERIC  can be negative
    storage_rings, storage_capacity_rings  NUMERIC  '7.6/62 Full (Rings)' -> 7.6 and 62
    earthwork_subcon                 TEXT     'KTC'

CRITICAL FACTS — ignoring these produces wrong answers:

* THIS IS THE ONLY TABLE YOU MAY QUERY. The site-work tables (daily_logs,
  dwall_panels) belong to a different group and are out of scope here.

* Questions about what is CRITICAL, FLAGGED, URGENT, "needs attention", or
  "the exclamation" are asking about the `exclamation` column and that column
  ALONE. Do not widen them into `delays` — a delay is routine reporting, a ‼
  flag is the engineer deliberately raising something. Filter with
  `exclamation IS NOT NULL AND exclamation <> ''`.

* update_date is a real DATE and is the reliable date. Prefer it over
  logged_at, which is only when WhatsApp delivered the message — an update can
  arrive weeks after the day it reports on.

* The numeric columns may be NULL on rows where the engineer wrote the figure
  in an unusual way. When you aggregate, prefer the numeric column but consider
  whether a NULL would silently drop the row from a COUNT or an AVG.

* Progress is CUMULATIVE. 'day_shift_cumulative' is a running ring number, not
  a daily rate. Work out a rate as a difference between dates, never by summing
  the cumulative column."""


_SQL_SYSTEM = """You write ONE PostgreSQL SELECT that answers a question about a tunnelling
progress record, then someone else turns your result into a sentence.

{schema}

RULES
1. Emit exactly one SELECT. No INSERT/UPDATE/DELETE/DDL — the connection is
   read-only and they will fail.
2. ALWAYS scope to the group: include
   `group_id = current_setting('app.group_id')`
   for every tunnel_updates reference. Write it exactly like that — the value
   is set for you. Use NO bound parameters and no placeholders of any kind;
   inline every literal value directly into the SQL.
3. Return the SMALLEST result that answers the question. Aggregate (COUNT, MIN,
   MAX, SUM, GROUP BY) rather than returning raw rows whenever the question is
   about totals, extremes or trends. Add a LIMIT.
4. Include the columns needed to make the answer readable — always return
   contract and update_date alongside whatever was asked for.
5. When the question asks what the update SAID, return the verbatim block
   (main_drive, tbm_progress, delays) rather than the extracted numbers. When
   it asks how much or how many, return the numbers.
6. If the question cannot be answered with SQL because it turns on the nuance
   of free-text wording, return {"sql": null} and a keyword search runs instead.

Respond ONLY with JSON, no explanation, no code fences:
{"sql": "SELECT ...", "reasoning": "one short line"}

EXAMPLES

"what are the critical activities?"
{"sql": "SELECT contract, update_date, exclamation FROM tunnel_updates WHERE group_id = current_setting('app.group_id') AND exclamation IS NOT NULL AND exclamation <> '' ORDER BY update_date DESC LIMIT 30", "reasoning": "the flagged column alone, newest first"}

"any critical items for P103 this month?"   (if the user turn said today is 2026-09-06)
{"sql": "SELECT update_date, exclamation FROM tunnel_updates WHERE group_id = current_setting('app.group_id') AND contract = 'P103' AND exclamation IS NOT NULL AND exclamation <> '' AND update_date >= '2026-09-01' ORDER BY update_date DESC LIMIT 30", "reasoning": "flagged column, one contract, month from the user turn — never from this example"}

"what is the TBM completion now?"
{"sql": "SELECT contract, update_date, pct_completion, night_shift_cumulative, rings_total FROM tunnel_updates WHERE group_id = current_setting('app.group_id') AND pct_completion IS NOT NULL ORDER BY update_date DESC LIMIT 5", "reasoning": "latest reported completion per the newest rows"}

"how many rings did we build in August?"
{"sql": "SELECT contract, MIN(update_date) AS from_date, MAX(update_date) AS to_date, MAX(night_shift_cumulative) - MIN(night_shift_cumulative) AS rings_built FROM tunnel_updates WHERE group_id = current_setting('app.group_id') AND update_date BETWEEN '2026-08-01' AND '2026-08-31' AND night_shift_cumulative IS NOT NULL GROUP BY contract", "reasoning": "cumulative column: difference across the window, never a SUM"}

"what did P103 report on 15 Aug?"
{"sql": "SELECT contract, update_date, drive_name, main_drive, tbm_progress, delays, exclamation FROM tunnel_updates WHERE group_id = current_setting('app.group_id') AND contract = 'P103' AND update_date = '2026-08-15' LIMIT 5", "reasoning": "the verbatim blocks for one day"}

"which day had the most soil disposal?"
{"sql": "SELECT contract, update_date, total_disposed_loads FROM tunnel_updates WHERE group_id = current_setting('app.group_id') AND total_disposed_loads IS NOT NULL ORDER BY total_disposed_loads DESC LIMIT 5", "reasoning": "extreme on the extracted number"}
"""


def _write_sql(question: str, previous_error: str | None = None,
               previous_sql: str | None = None) -> dict:
    """Ask the model for a query. On a retry, show it what Postgres said."""
    user = f"Today is {site_today().isoformat()}.\n\nQuestion:\n\"\"\"{question}\"\"\""
    if previous_error:
        user += (
            f"\n\nYour previous attempt failed. Fix it.\n"
            f"Query:\n{previous_sql}\n\nPostgres said:\n{previous_error}"
        )

    response = client.messages.create(
        model=_SQL_MODEL,
        max_tokens=400,
        system=[{
            "type": "text",
            "text": _SQL_SYSTEM.replace("{schema}", _SCHEMA),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    )
    return _extract_json(response.content[0].text)


def _run_sql_path(group_id: str, question: str) -> tuple[str, bool, int] | None:
    """Write and run a query, retrying once on error. None = fall back."""
    attempt_error = attempt_sql = None

    for attempt in (1, 2):
        try:
            plan = _write_sql(question, attempt_error, attempt_sql)
        except Exception:
            logger.exception("tunnel /ask: SQL generation failed (attempt %d)", attempt)
            return None

        sql = (plan or {}).get("sql")
        if not sql:
            return None

        normalised = sql.replace('"', "'")
        if "current_setting('app.group_id')" not in normalised:
            attempt_error, attempt_sql = (
                "every tunnel_updates reference must be scoped with "
                "group_id = current_setting('app.group_id')", sql,
            )
            continue

        # The tunnel record is deliberately walled off from the site record.
        # A model that reaches for daily_logs here would answer a tunnel
        # question with another group's data, which is worse than not
        # answering — so refuse and make it try again.
        lowered = normalised.lower()
        if "daily_logs" in lowered or "dwall_panels" in lowered:
            attempt_error, attempt_sql = (
                "daily_logs and dwall_panels are out of scope in this group. "
                "Query tunnel_updates only.", sql,
            )
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


def _keyword_fallback(group_id: str, question: str) -> str:
    """Keyword retrieval, for when SQL is the wrong tool or it failed twice."""
    words = [w.strip(".,?!'\"").lower() for w in question.split()]
    keywords = [w for w in words if len(w) > 2 and w not in _STOPWORDS][:6]

    rows, total = db.search_tunnel_updates(
        group_id, keywords=keywords, limit=_MAX_FALLBACK_ROWS
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


def answer_tunnel_query(group_id: str, question: str) -> str:
    """Answer a natural language question about the tunnel record.

    Synchronous: psycopg and the Anthropic client both block. Callers on the
    event loop must run this in a thread.
    """
    sql_result = _run_sql_path(group_id, question)

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
        data = _keyword_fallback(group_id, question)
        provenance = (
            "The entries below are the best keyword matches, NOT the whole "
            "record. Do not give totals or say something never happened — say "
            "what you found in the entries searched."
        )

    context = f"""You are a tunnelling progress assistant for a construction project.
Answer the question using only the data below.
Today is {site_today().isoformat()} (site local time, UTC+8). Work out "today",
"yesterday" and "last week" from that date and no other — never state a date you
were not given here.

Be concise and direct.

The answer is sent straight into a WhatsApp group, which does not render
Markdown. Use WhatsApp formatting only: *single asterisks* for bold, and "•" or
"-" for bullets. Never use #, ##, or ** — they appear literally as punctuation
in the message. Keep it short enough to read on a phone.

{provenance}

The `exclamation` field holds what the engineer flagged as critical by putting
it between ‼ markers. When the question is about critical or urgent items,
report those and say which contract and date each came from. An empty
exclamation field means nothing was flagged that day — say that rather than
substituting the delays block for it.

Progress figures are cumulative ring counts, not daily rates. Do not add them
up.

What the column names mean — do not infer them from the abbreviation, and never
describe a figure in terms this list does not support:
• ds_loads / ns_loads — soil disposal loads on the DAY shift and the NIGHT
  shift. DS and NS are shifts. They are not directions, sides or locations.
• day_shift_* / night_shift_* — rings built on that shift, and the cumulative
  ring number reached by the end of it.
• fsc_* — first stage concrete.
• delta_disposal — disposal running ahead of (+) or behind (-) excavation.
• storage_rings / storage_capacity_rings — spoil held on site, and the site's
  capacity, both counted in rings.
• pct_completion — percent of the drive's rings built.
If a value's meaning is not covered here, report the number and the column it
came from rather than inventing a reading for it.

If the data below answers the question, ANSWER IT — lead with the figure or the
finding, and do not preface it with a disclaimer. "I could not find a record"
in front of an answer you did go on to give is worse than saying nothing: it
reads as a failure and the engineer stops trusting the number that follows.

Reserve that phrasing for a genuinely empty result. When the result is empty,
say "I could not find a record of X", never "X did not happen" — the query
finding nothing is not the same as the work not having been done.

If the record is older than the period asked about, give it and say how old it
is. That is an answer, not a miss.

{data}

Question: {question}"""

    model = (_ANSWER_MODEL_SMALL if rows_shown <= _SMALL_RESULT_ROWS
             else _ANSWER_MODEL_LARGE)
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": context}],
    )
    return response.content[0].text.strip()
