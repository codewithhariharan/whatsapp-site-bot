# tools/

## extract_chat_history.py

Backfills site logs from a WhatsApp group export, for history that predates
the bot going live.

    python tools/extract_chat_history.py /path/to/_chat.txt rows.json

Deterministic — no LLM call, so it costs nothing and is reproducible. It
splits each caption on the first colon, pulls a parenthetical or trailing
grid-line reference into `sub_location`, and flattens bullet lists.

### Known limits

Measured against Site_Report_Full_20Apr2026-27Jul2026.xlsx (1,574 rows
produced by the live Haiku parser over the same window):

  - 64% of those rows are reproduced closely enough to match.
  - Over the same window this extractor returns 2,557 rows vs 1,574. It is
    deliberately broader: the live parser dropped most P-series caisson
    entries and short status captions, and this does not.
  - `manpower` is always blank. The live parser inferred it from free text
    ("4 machines dayshift" -> "Coring/Splitting machines - 6"); that is
    judgement, not a rule, and guessing it would be worse than leaving it
    empty.

Treat the output as a superset to review, not a drop-in replacement for
parser output.

## build_full_excel.py

Produces the same workbook `/excel2` sends, from either source.

    # from a chat export — no GCP, no .env, no deployment
    python tools/build_full_excel.py --chat /path/to/_chat.txt

    # from Cloud SQL — needs .env and application-default credentials
    python tools/build_full_excel.py --group '1203630xxxxxxxxxx@g.us'

Only `openpyxl` is needed for the `--chat` path; `excel_generator` has no
database or GCP imports, so it runs on a laptop with nothing configured.
