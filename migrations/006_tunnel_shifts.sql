-- The tunnel template gains a SHIFT line and three lines under PROGRESS.
--
--   *TUNNEL PROGRESS UPDATE*
--   *CONTRACT*: P103
--   *DATE*: 15-Aug-2026
--   *SHIFT*: Day
--   *DRIVE*: LDTBM Main Drive
--   *PROGRESS*: 3 / 1205 / 1759 (68.5%)
--   - Follow-on works: First stage concrete 0 / 731 / 1759 rings
--   - Soil disposal: 102 loads (TSSG), approx. 5.2 rings vs 3 rings excavated
--   - On site storage: 3.6 / 62 rings
--   *TBM LOCATION*: Underneath Punggol Central Road
--   *INSTRUMENTATION*: All within AL
--   *ISSUES*: [None]
--
-- A day now has a Day and a Night update for each drive, so the shift joins
-- the key a resend replaces on — without it the night update would overwrite
-- the day's. Rows from before the change have no shift and keep ''.
-- '' rather than NULL so the unique constraint still treats them as one key.

ALTER TABLE tunnel_progress
    ADD COLUMN IF NOT EXISTS shift TEXT NOT NULL DEFAULT '',   -- 'Day', 'Night', ''
    ADD COLUMN IF NOT EXISTS follow_on_works TEXT,
    ADD COLUMN IF NOT EXISTS soil_disposal TEXT,
    ADD COLUMN IF NOT EXISTS on_site_storage TEXT;

ALTER TABLE tunnel_progress
    DROP CONSTRAINT IF EXISTS tunnel_progress_group_contract_date_drive_unique;
ALTER TABLE tunnel_progress
    DROP CONSTRAINT IF EXISTS tunnel_progress_group_contract_date_drive_shift_unique;
ALTER TABLE tunnel_progress
    ADD CONSTRAINT tunnel_progress_group_contract_date_drive_shift_unique
        UNIQUE (group_id, contract, report_date, drive, shift);

-- A flag from an update carries its shift too, so a corrected night update
-- replaces the night's flags and leaves the day's alone. NULL for a flag from
-- a loose message, and for flags filed before this change.
ALTER TABLE tunnel_flags
    ADD COLUMN IF NOT EXISTS shift TEXT;
