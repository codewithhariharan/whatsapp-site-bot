-- /ask search indexes — trigram support for substring matching.
--
-- Why: the keyword fallback and most model-written location queries filter with
-- ILIKE '%TERM%'. A leading wildcard makes a B-tree index useless, so Postgres
-- sequentially scans all ~40,000 daily_logs rows (measured: ~725 ms). A GIN
-- trigram index handles leading wildcards and takes that to roughly 10 ms.
--
-- This matters more than the raw numbers suggest: the database is a
-- db-f1-micro shared with the live message-logging path, so every second /ask
-- spends scanning is a second engineers' updates queue behind it.
--
-- Safe to run on a live instance: CREATE INDEX CONCURRENTLY does not take a
-- write lock, so logging continues throughout. CONCURRENTLY cannot run inside a
-- transaction block — run this file statement by statement, not wrapped in
-- BEGIN/COMMIT. If a CONCURRENTLY build fails it leaves an INVALID index
-- behind; drop it and retry rather than leaving it in place.
--
-- Cost: a few MB per index at current row counts.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_daily_logs_description_trgm
    ON daily_logs USING GIN (description gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_daily_logs_main_location_trgm
    ON daily_logs USING GIN (main_location gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_daily_logs_sub_location_trgm
    ON daily_logs USING GIN (sub_location gin_trgm_ops);

-- dwall_panels is only ~19 rows; a sequential scan there is already optimal and
-- an index would cost more to maintain than it saves. Deliberately omitted.

-- Verify afterwards:
--   SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'daily_logs';
--   SELECT indexrelid::regclass, indisvalid FROM pg_index
--     WHERE indrelid = 'daily_logs'::regclass AND NOT indisvalid;  -- must be empty
