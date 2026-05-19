-- Run this in your Supabase SQL editor

CREATE TABLE groups (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT UNIQUE NOT NULL,
    group_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE location_order (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT NOT NULL,
    location_name TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(group_id, order_index)
);

CREATE TABLE daily_logs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT NOT NULL,
    log_date DATE NOT NULL,
    logged_at TIMESTAMPTZ DEFAULT NOW(),
    sender_name TEXT,
    sender_number TEXT,
    main_location TEXT NOT NULL,
    sub_location TEXT,
    description TEXT,
    manpower TEXT,
    raw_message TEXT
);

CREATE TABLE dwall_panels (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT NOT NULL,
    -- Identification
    report_date TEXT,
    engineer_initials TEXT,
    entry_number TEXT,
    panel_number TEXT,
    panel_group TEXT,
    -- Panel specs
    panel_size TEXT,
    guide_wall_level TEXT,
    cut_off_level TEXT,
    design_toe_level TEXT,
    design_depth TEXT,
    final_depth TEXT,
    rock_hit TEXT,
    -- Stage start/end times (stored as text e.g. "13:00hrs (20/02/26)")
    excavation_start TEXT,
    excavation_end TEXT,
    koden_start TEXT,
    koden_end TEXT,
    desanding_start TEXT,
    desanding_end TEXT,
    water_stop_start TEXT,
    water_stop_end TEXT,
    rebar_cage_start TEXT,
    rebar_cage_end TEXT,
    tremie_pipe_start TEXT,
    tremie_pipe_end TEXT,
    casting_start TEXT,
    casting_end TEXT,
    -- Volumes
    theo_volume TEXT,
    actual_volume TEXT,
    overbreak_pct TEXT,
    -- Downtime: array of {date, start, end, reason}
    downtime JSONB DEFAULT '[]'::jsonb,
    notes TEXT,
    raw_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT dwall_panels_group_panel_unique UNIQUE (group_id, panel_number)
);

-- ── Migration: run these if upgrading an existing dwall_panels table ──────────
-- ALTER TABLE dwall_panels ALTER COLUMN excavation_start TYPE TEXT USING excavation_start::TEXT;
-- ALTER TABLE dwall_panels ALTER COLUMN excavation_end   TYPE TEXT USING excavation_end::TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS report_date TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS engineer_initials TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS entry_number TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS panel_group TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS cut_off_level TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS design_toe_level TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS design_depth TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS final_depth TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS rock_hit TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS koden_start TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS koden_end TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS desanding_start TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS desanding_end TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS water_stop_start TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS water_stop_end TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS rebar_cage_start TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS rebar_cage_end TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS tremie_pipe_start TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS tremie_pipe_end TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS casting_start TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS casting_end TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS theo_volume TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS actual_volume TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS overbreak_pct TEXT;
-- ALTER TABLE dwall_panels ADD COLUMN IF NOT EXISTS downtime JSONB DEFAULT '[]'::jsonb;
-- -- Drop old simplified columns (optional, after confirming data migrated):
-- -- ALTER TABLE dwall_panels DROP COLUMN IF EXISTS koden_check;
-- -- ALTER TABLE dwall_panels DROP COLUMN IF EXISTS desanding;
-- -- ALTER TABLE dwall_panels DROP COLUMN IF EXISTS water_stop_installation;
-- -- ALTER TABLE dwall_panels DROP COLUMN IF EXISTS rebar_cage_lowering;
-- -- ALTER TABLE dwall_panels DROP COLUMN IF EXISTS cast_date;

CREATE TABLE reorder_sessions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id TEXT UNIQUE NOT NULL,
    session_date DATE NOT NULL,
    ordered_logs JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for fast queries
CREATE INDEX idx_daily_logs_group_date ON daily_logs(group_id, log_date);
CREATE INDEX idx_daily_logs_location ON daily_logs(group_id, main_location);
CREATE INDEX idx_dwall_group ON dwall_panels(group_id);
CREATE INDEX idx_location_order_group ON location_order(group_id, order_index);
