"""Natural-language Q&A over the site record (backs /ask).

Three generations of this file, each fixing the last one's ceiling:

  1. Load every row into one prompt. At 40k entries that was several million
     tokens, the API rejected it, and the group saw "🔍 Searching..." forever.
  2. Keyword retrieval — send only the top-N matching rows. Bounded and fast,
     but it only ever sees a *sample*. "How many days did we pour in Zone 3?"
     or "which zone was busiest?" cannot be answered from the top 400 matches,
     and the model has no way to know its view was partial.
  3. This one: push the computation into Postgres. The model writes a SELECT,
     the database aggregates across all 40k rows in milliseconds, and only the
     result — usually a handful of numbers — reaches the model.

Generation 3 is both broader and cheaper than 2: it sees the entire record, and
the answering call's input drops from ~18k tokens of raw rows to a few hundred.

Retrieval is kept as a fallback. SQL is the wrong tool when the question turns
on the *meaning* of free text rather than on counting, and a query that fails
twice should still produce an answer rather than an apology.
"""

import json
import logging
from datetime import date

import anthropic

from config import settings
import database as db

logger = logging.getLogger("site_bot")

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

_SQL_MODEL = "claude-sonnet-4-6"      # writes the query; needs real capability
_ANSWER_MODEL = "claude-sonnet-4-6"   # turns results into a WhatsApp reply

_QUERY_TIMEOUT_MS = 5000
_MAX_RESULT_ROWS = 300
_CONTEXT_CHAR_BUDGET = 200_000

# Fallback retrieval caps, used only when the SQL path gives up.
_MAX_LOG_ROWS = 400
_MAX_PANEL_ROWS = 300


# ── Schema shown to the model ─────────────────────────────────────────────────

# Written for the model, not for humans: it states the traps that would
# otherwise produce confidently wrong SQL.
_SCHEMA = """TABLE daily_logs  -- ~40,000 rows, one per site update message
    log_date       DATE          the day the work happened  (indexed with group_id)
    logged_at      TIMESTAMPTZ   when the message arrived (UTC; site is UTC+8)
    sender_name    TEXT          engineer who sent it
    main_location  TEXT NOT NULL broad area: 'Zone 3', 'Zone 4 P46', 'CCW2'
    sub_location   TEXT          detail within it: 'P32', 'GL A-B/20'; often ''
    description    TEXT          what was done
    manpower       TEXT          free text, usually ''
    raw_message    TEXT          the original WhatsApp text

TABLE dwall_panels  -- only ~19 rows, one per D-Wall / Barrette panel
    panel_number   TEXT   'CN270', 'CN284A'
    panel_group    TEXT
    engineer_initials TEXT
    report_date    TEXT   'DD/MM/YY' as TEXT, NOT a date
    design_depth, final_depth, rock_hit, panel_size,
    guide_wall_level, cut_off_level, design_toe_level   TEXT
    excavation_start/_end, koden_start/_end, desanding_start/_end,
    water_stop_start/_end, rebar_cage_start/_end,
    tremie_pipe_start/_end, casting_start/_end          TEXT
                          format '09:45hrs (04/02/26)' -- TEXT, not timestamps
    theo_volume, actual_volume, overbreak_pct, notes    TEXT
    downtime       JSONB  [{date, start, end, reason}, ...]

TABLE groups (group_id, group_name), location_order (group_id, location_name, order_index)

CRITICAL FACTS — ignoring these produces wrong answers:

* The panel/point identifier is stored INCONSISTENTLY. 'Zone 4 P46: ...' may be
  stored as main_location='Zone 4 P46', sub_location='' OR as
  main_location='Zone 4', sub_location='P46'. Roughly 3 in 4 take the first
  form. So NEVER match a location on one column alone. Use, e.g.:
      WHERE (main_location ILIKE '%P46%' OR sub_location ILIKE '%P46%')
  and to group by zone, group on the zone prefix, not the raw column:
      substring(main_location from '^[A-Za-z]+ ?[0-9]+')

* Panel stage times are TEXT like '09:45hrs (04/02/26)' (DD/MM/YY). To order
  them chronologically you must parse the date out:
      to_date(substring(casting_start from '\\((\\d{2}/\\d{2}/\\d{2})\\)'), 'DD/MM/YY')
  Ordering these columns as plain text gives the wrong answer.

* log_date is the reliable date for daily_logs. Prefer it over logged_at."""


_SQL_PROMPT = """Today is {today}.

You write ONE PostgreSQL SELECT that answers a question about a construction
site record, then someone else turns your result into a sentence.

{schema}

RULES
1. Emit exactly one SELECT. No INSERT/UPDATE/DELETE/DDL — the connection is
   read-only and they will fail.
2. ALWAYS scope to the group: include
   `group_id = current_setting('app.group_id')`
   for every daily_logs or dwall_panels reference. Write it exactly like that —
   the value is set for you. Use NO bound parameters and no placeholders of any
   kind; inline every literal value directly into the SQL.
3. Return the SMALLEST result that answers the question. Aggregate (COUNT,
   MIN, MAX, SUM, GROUP BY) rather than returning raw rows whenever the
   question is about totals, counts, extremes or trends. Add a LIMIT.
4. Include the columns needed to make the answer readable — if you count by
   zone, return the zone name alongside the count.
5. When the question asks *what happened* somewhere, returning the matching
   log rows (log_date, main_location, sub_location, description) is right —
   just bound it with a sensible LIMIT.
6. If the question cannot be answered with SQL because it depends on the
   nuance of free-text wording rather than on counting or filtering, return
   {{"sql": null, "keywords": ["..."]}} and a keyword search will run instead.

Respond ONLY with JSON, no explanation, no code fences:
{{"sql": "SELECT ...", "reasoning": "one short line"}}

EXAMPLES

"how many entries are there for Zone 3 in August 2026?"
{{"sql": "SELECT COUNT(*) AS n FROM daily_logs WHERE group_id = current_setting('app.group_id') AND main_location ILIKE 'Zone 3%' AND log_date BETWEEN '2026-08-01' AND '2026-08-31'", "reasoning": "count with date range"}}

"which zone had the most activity last month?"
{{"sql": "SELECT substring(main_location from '^[A-Za-z]+ ?[0-9]+') AS zone, COUNT(*) AS n FROM daily_logs WHERE group_id = current_setting('app.group_id') AND log_date >= '2026-08-01' AND log_date <= '2026-08-31' GROUP BY 1 ORDER BY n DESC LIMIT 10", "reasoning": "group by zone prefix"}}

"what was the first dwall to cast in the duration of the project?"
{{"sql": "SELECT panel_number, casting_start, casting_end FROM dwall_panels WHERE group_id = current_setting('app.group_id') AND casting_start IS NOT NULL AND casting_start <> '' ORDER BY to_date(substring(casting_start from '\\((\\d{{2}}/\\d{{2}}/\\d{{2}})\\)'), 'DD/MM/YY') LIMIT 3", "reasoning": "parse DD/MM/YY out of the text stage time"}}

"what were the activities today?"
{{"sql": "SELECT main_location, sub_location, description FROM daily_logs WHERE group_id = current_setting('app.group_id') AND log_date = '{today}' ORDER BY main_location LIMIT 200", "reasoning": "one day's rows"}}

"when was P46 last worked on?"
{{"sql": "SELECT log_date, main_location, sub_location, description FROM daily_logs WHERE group_id = current_setting('app.group_id') AND (main_location ILIKE '%P46%' OR sub_location ILIKE '%P46%') ORDER BY log_date DESC LIMIT 5", "reasoning": "identifier lives in either column"}}

Question:
\"\"\"{question}\"\"\""""


def _extract_json(text: str) -> dict:
    text = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def _write_sql(question: str, previous_error: str | None = None,
               previous_sql: str | None = None) -> dict:
    """Ask the model for a query. On a retry, show it what Postgres said."""
    content = _SQL_PROMPT.format(
        today=date.today().isoformat(), schema=_SCHEMA, question=question
    )
    if previous_error:
        content += (
            f"\n\nYour previous attempt failed. Fix it.\n"
            f"Query:\n{previous_sql}\n\nPostgres said:\n{previous_error}"
        )

    response = client.messages.create(
        model=_SQL_MODEL,
        max_tokens=900,
        messages=[{"role": "user", "content": content}],
    )
    return _extract_json(response.content[0].text)


def _run_sql_path(group_id: str, question: str) -> tuple[str, bool] | None:
    """Write and run a query, retrying once on error. None = fall back.

    Returns (rendered result block, hit_row_cap).
    """
    attempt_error = attempt_sql = None

    for attempt in (1, 2):
        try:
            plan = _write_sql(question, attempt_error, attempt_sql)
        except Exception:
            logger.exception("/ask: SQL generation failed (attempt %d)", attempt)
            return None

        sql = (plan or {}).get("sql")
        if not sql:
            return None  # the model itself asked for keyword search

        if "current_setting('app.group_id')" not in sql.replace('"', "'"):
            # Refusing beats silently answering across another group's data.
            # Single-tenant today, but the schema is group-scoped and the
            # allowlist could widen; an unscoped query would go unnoticed.
            attempt_error, attempt_sql = (
                "every daily_logs / dwall_panels reference must be scoped with "
                "group_id = current_setting('app.group_id')", sql,
            )
            continue

        try:
            rows, truncated = db.run_readonly_query(
                sql, group_id,
                timeout_ms=_QUERY_TIMEOUT_MS, max_rows=_MAX_RESULT_ROWS,
            )
        except db.QueryError as exc:
            logger.warning("/ask: query rejected (attempt %d): %s", attempt, exc)
            attempt_error, attempt_sql = str(exc), sql
            continue

        logger.info("/ask: %d rows from %s", len(rows), sql.replace("\n", " ")[:200])
        blob, shown = _fit(rows, _CONTEXT_CHAR_BUDGET)
        note = (
            f" (showing the first {shown}; the query matched more)" if truncated else ""
        )
        return (
            f"QUERY RUN AGAINST THE FULL RECORD:\n{sql}\n\n"
            f"RESULT — {shown} row(s){note}:\n{blob}",
            truncated,
        )

    return None


# ── Fallback retrieval ────────────────────────────────────────────────────────

def _fit(rows: list[dict], budget: int) -> tuple[str, int]:
    """Serialise rows compactly, dropping the least relevant until they fit."""
    kept = list(rows)
    while kept:
        blob = json.dumps(kept, separators=(",", ":"), default=str)
        if len(blob) <= budget:
            return blob, len(kept)
        shrunk = max(1, int(len(kept) * budget / len(blob)))
        kept = kept[:shrunk] if shrunk < len(kept) else kept[:-1]
    return "[]", 0


def _section(title: str, rows: list[dict], total: int, budget: int) -> str:
    blob, shown = _fit(rows, budget)
    if total == 0:
        return f"{title} — no matching entries.\n[]"
    header = (
        f"{title} — showing the {shown} most relevant of {total} matching entries"
        if shown < total else f"{title} — all {total} matching entries"
    )
    return f"{header}:\n{blob}"


def _keyword_fallback(group_id: str, question: str) -> str:
    """Keyword retrieval, for when SQL is the wrong tool or it failed twice."""
    words = [w.strip(".,?!'\"").lower() for w in question.split()]
    stop = {
        "what", "when", "where", "which", "who", "why", "how", "was", "were",
        "is", "are", "the", "a", "an", "in", "on", "at", "for", "of", "to",
        "did", "do", "does", "and", "or", "it", "we", "there", "any", "all",
        "site", "project", "work", "works", "activity", "activities", "happened",
    }
    keywords = [w for w in words if len(w) > 2 and w not in stop][:6]

    logs, log_total = db.search_logs(group_id, keywords=keywords, limit=_MAX_LOG_ROWS)
    panels, panel_total = db.search_panels(group_id, keywords=keywords,
                                           limit=_MAX_PANEL_ROWS)
    half = _CONTEXT_CHAR_BUDGET // 2
    return (
        f"KEYWORD SEARCH (SQL was not usable for this question). Terms: {keywords}\n\n"
        + _section("DAILY SITE LOGS", logs, log_total, half)
        + "\n\n"
        + _section("D-WALL / BARRETTE PANEL RECORDS", panels, panel_total, half)
    )


# ── Answer ────────────────────────────────────────────────────────────────────

def answer_query(group_id: str, question: str) -> str:
    """Answer a natural language question about the site record.

    Synchronous: psycopg and the Anthropic client both block. Callers on the
    event loop must run this in a thread.
    """
    sql_result = _run_sql_path(group_id, question)

    if sql_result is not None:
        data, truncated = sql_result
        provenance = (
            "The result below comes from a query run across the ENTIRE record "
            "(~40,000 entries), not a sample. If it reports a count or an "
            "extreme, that figure is complete and you can state it plainly."
        )
        if truncated:
            provenance += (
                " The row list was cut off at the display limit, so say the "
                "listing is partial."
            )
    else:
        data = _keyword_fallback(group_id, question)
        provenance = (
            "The entries below are the best keyword matches, NOT the whole "
            "record. Do not give totals or say something never happened — say "
            "what you found in the entries searched."
        )

    context = f"""You are a construction site assistant. Answer the question using only the data below.
Be concise and direct.

The answer is sent straight into a WhatsApp group, which does not render
Markdown. Use WhatsApp formatting only: *single asterisks* for bold, and "•" or
"-" for bullets. Never use #, ##, or ** — they appear literally as punctuation
in the message. Keep it short enough to read on a phone.

{provenance}

If the answer is not in the data, say so plainly rather than guessing.
Panel stage times read '13:00hrs (20/02/26)' — that is DD/MM/YY.

{data}

Question: {question}"""

    response = client.messages.create(
        model=_ANSWER_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": context}],
    )
    return response.content[0].text.strip()
