-- Standardised tunnel progress updates, and the "!" flags for the director.
--
-- Replaces tunnel_updates (002), which held the older free-form template.
-- That table is left in place, unused: it holds only two test rows, and
-- dropping data is not something a migration should do quietly.
--
-- Every contract group sends the same template:
--
--   TUNNEL PROGRESS UPDATE
--   CONTRACT: CR146
--   DATE: 04-JAN-2026
--   DRIVE: EB - Main Drive 3
--   PROGRESS: 0 / 225 / 888 (25.3%)
--   TBM LOCATION: at side table of AMK Ave 3
--   INSTRUMENTATION: LG3053 breached AL
--   ISSUES: [shift change]
--
-- One row per (group, contract, report date, drive). A resend for the same
-- four is a correction and replaces the earlier row. The group is part of the
-- key so a group that mistypes its contract code cannot overwrite another
-- contract group's row — each group's data stays its own.

CREATE TABLE IF NOT EXISTS tunnel_progress (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT NOT NULL,

    contract TEXT NOT NULL,          -- 'CR146'
    report_date DATE NOT NULL,       -- the DATE written in the message
    drive TEXT NOT NULL,             -- 'EB - Main Drive 3'

    -- PROGRESS: rings_built / current_ring / total_rings (pct_complete%)
    rings_built NUMERIC,             -- rings built on this report's day
    current_ring NUMERIC,            -- cumulative ring reached
    total_rings NUMERIC,             -- rings in the whole drive
    pct_complete NUMERIC,            -- the % as written, not recomputed

    tbm_location TEXT,
    instrumentation TEXT,
    issues TEXT,                     -- NULL when the message says [None]

    raw_message TEXT NOT NULL,
    sender_name TEXT,
    sender_number TEXT,
    sent_at TIMESTAMPTZ NOT NULL,    -- when the message was posted to the group
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT tunnel_progress_group_contract_date_drive_unique
        UNIQUE (group_id, contract, report_date, drive)
);

CREATE INDEX IF NOT EXISTS idx_tunnel_progress_contract_date
    ON tunnel_progress (contract, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_tunnel_progress_group
    ON tunnel_progress (group_id, report_date DESC);


-- Every line containing a "!" from a contract group — inside an update or in
-- any other message. /!! reads this table and nothing else, for the day the
-- message was SENT (sent_date, site time).
--
-- A flag from an update carries that update's (contract, report_date, drive),
-- so a corrected resend can replace its flags along with its figures. A flag
-- from any other message leaves report_date NULL and is never replaced.

CREATE TABLE IF NOT EXISTS tunnel_flags (
    id BIGSERIAL PRIMARY KEY,
    group_id TEXT NOT NULL,
    contract TEXT,                   -- NULL only if the group's contract is unknown
    drive TEXT,
    report_date DATE,                -- set only for flags that came from an update
    flag_text TEXT NOT NULL,         -- the line, "!" marks stripped
    sender_name TEXT,
    sent_at TIMESTAMPTZ NOT NULL,
    sent_date DATE NOT NULL,         -- sent_at in Asia/Singapore
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tunnel_flags_day
    ON tunnel_flags (sent_date, contract);
CREATE INDEX IF NOT EXISTS idx_tunnel_flags_update
    ON tunnel_flags (group_id, contract, report_date, drive)
    WHERE report_date IS NOT NULL;
