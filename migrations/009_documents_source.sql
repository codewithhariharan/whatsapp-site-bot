-- Where a dashboard document came from, when not the page's upload form.
-- FormSG submissions are filed as 'formsg:<submission id>:<field id>'; the
-- unique constraint makes a repeated webhook delivery a no-op.

ALTER TABLE dashboard_documents ADD COLUMN IF NOT EXISTS source TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_documents_source
    ON dashboard_documents (source);
