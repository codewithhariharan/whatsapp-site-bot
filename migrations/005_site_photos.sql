-- Photos posted to the site group, stored in the database itself.
--
-- Site updates are mostly photo captions: the caption becomes a daily_logs row,
-- and this table keeps the picture that came with it. A WhatsApp album carries
-- the caption on one photo only, so every photo is linked to its log by
-- proximity instead — the same sender's log posted within ten minutes — which
-- also covers the photo that carried the caption itself (see
-- database.link_site_photos).
--
-- The image is re-encoded as a JPEG no larger than 1600px on its long edge
-- before it is stored (~150-300 KB), so a year of photos is a few GB rather
-- than tens.
--
-- Only photos posted from the day this was deployed exist here; the bridge did
-- not download media before that.

CREATE TABLE IF NOT EXISTS site_photos (
    id BIGSERIAL PRIMARY KEY,
    group_id TEXT NOT NULL,
    wa_message_id TEXT,              -- WhatsApp's id; a repeat delivery is ignored
    sender_name TEXT,
    sender_number TEXT,
    caption TEXT,                    -- the photo's own caption, if it had one
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    log_id UUID REFERENCES daily_logs(id) ON DELETE SET NULL,

    image BYTEA NOT NULL,            -- JPEG
    width INTEGER,
    height INTEGER,

    CONSTRAINT site_photos_wa_message_unique UNIQUE (wa_message_id)
);

CREATE INDEX IF NOT EXISTS idx_site_photos_group_sent
    ON site_photos (group_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_photos_log
    ON site_photos (log_id);
CREATE INDEX IF NOT EXISTS idx_site_photos_unlinked
    ON site_photos (group_id, sent_at)
    WHERE log_id IS NULL;
