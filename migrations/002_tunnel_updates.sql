-- Tunnel updates: a table of its own, deliberately separate from daily_logs
-- and dwall_panels.
--
-- The tunnel group carries one templated message per contract per day (see
-- tunnel_parser.py for the shape). Its columns come in two halves:
--
--   * The verbatim section blocks — main_drive, tbm_progress, delays,
--     exclamation — which reproduce the reporting spreadsheet cell for cell.
--     These are what a person reads.
--   * The numbers pulled out of those blocks — ring counts, percentages,
--     disposal loads. These are what /ask counts, compares and trends across
--     dates. Keeping the prose alone would force every numeric question back
--     through the model's reading of free text, one row at a time.
--
-- Both halves are written from the same parse, so they never disagree.

CREATE TABLE IF NOT EXISTS tunnel_updates (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT NOT NULL,

    -- ── Identity ──────────────────────────────────────────────────────────
    contract TEXT NOT NULL,          -- 'P103' — code only, boilerplate stripped
    title_line TEXT,                 -- 'P103 Tunnel Update' — the raw first line
    update_date DATE NOT NULL,       -- the date WRITTEN in the message, not arrival
    drive_name TEXT,                 -- 'LDTBM Main Drive' — which drive reported

    -- ── Verbatim section blocks (the spreadsheet columns) ─────────────────
    main_drive TEXT,
    tbm_progress TEXT,
    delays TEXT,
    exclamation TEXT,                -- text between the ‼ markers, emoji stripped
    other_sections JSONB DEFAULT '{}'::jsonb,   -- any header not among the three

    -- ── Extracted numbers: main drive ─────────────────────────────────────
    mined_from TEXT,
    mined_to TEXT,
    ring_built_from TEXT,
    ring_built_to TEXT,
    fsc_shift NUMERIC,               -- first stage concrete, this report
    fsc_cumulative NUMERIC,          -- first stage concrete, to date
    rings_total NUMERIC,             -- rings in the whole drive (the denominator)

    -- ── Extracted numbers: TBM progress ───────────────────────────────────
    -- 'Day Shift: 3/1205/1759' = rings this shift / cumulative ring / total.
    day_shift_rings NUMERIC,
    day_shift_cumulative NUMERIC,
    night_shift_rings NUMERIC,
    night_shift_cumulative NUMERIC,
    pct_completion NUMERIC,
    tbm_location TEXT,
    instrumentation TEXT,

    -- ── Extracted numbers: delays / disposal ──────────────────────────────
    delay_flag TEXT,                 -- 'No', 'Yes — <reason>'
    ds_loads NUMERIC,                -- day shift soil disposal loads
    ns_loads NUMERIC,
    total_disposed_loads NUMERIC,
    disposed_rings_equiv NUMERIC,    -- 'approx. 5.2 rings'
    rings_excavated NUMERIC,
    delta_disposal NUMERIC,          -- may be negative
    storage_rings NUMERIC,           -- '7.6/62 Full (Rings)' — the 7.6
    storage_capacity_rings NUMERIC,  -- and the 62
    earthwork_subcon TEXT,

    -- ── Provenance ────────────────────────────────────────────────────────
    sender_name TEXT,
    sender_number TEXT,
    raw_message TEXT,
    logged_at TIMESTAMPTZ DEFAULT NOW(),

    -- One row per contract per reporting date. A resend or a correction
    -- overwrites, so the table always mirrors the spreadsheet one-for-one.
    CONSTRAINT tunnel_updates_contract_date_unique
        UNIQUE (group_id, contract, update_date)
);

CREATE INDEX IF NOT EXISTS idx_tunnel_group_date
    ON tunnel_updates(group_id, update_date DESC);
CREATE INDEX IF NOT EXISTS idx_tunnel_contract
    ON tunnel_updates(group_id, contract, update_date DESC);

-- The director's question ("what are the critical activities?") reads this
-- column and nothing else, so it gets an index that skips the ~majority of
-- rows carrying no ‼ block at all.
CREATE INDEX IF NOT EXISTS idx_tunnel_exclamation
    ON tunnel_updates(group_id, update_date DESC)
    WHERE exclamation IS NOT NULL AND exclamation <> '';
