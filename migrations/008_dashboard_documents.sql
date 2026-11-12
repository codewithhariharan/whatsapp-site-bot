-- Documents uploaded through the web dashboard (documents.py): PDFs and Excel
-- workbooks people want to share alongside the record.
--
-- Only the facts about each file live here. The file itself is on the api
-- container's `dashboard_docs` volume under `storage_key` — a random name, so
-- nothing a visitor typed ever becomes part of a path on disk.

CREATE TABLE IF NOT EXISTS dashboard_documents (
    id BIGSERIAL PRIMARY KEY,
    filename TEXT NOT NULL,              -- as uploaded, cleaned; shown and offered on download
    kind TEXT NOT NULL,                  -- pdf | xlsx | xlsm | xls
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,    -- the file's name on the volume
    note TEXT,
    uploaded_by_id BIGINT REFERENCES dashboard_users(id) ON DELETE SET NULL,
    uploaded_by TEXT,                    -- the uploader's name (or email) at the time
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dashboard_documents_uploaded
    ON dashboard_documents (uploaded_at DESC);
