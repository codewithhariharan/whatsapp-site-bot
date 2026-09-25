-- Pending messages: the inbox that batch logging drains.
--
-- Posts are no longer parsed and logged as they arrive. They are stored here
-- raw, and ingest_batch.py processes them in one pass at 00:00, 06:00, 12:00
-- and 18:00 Singapore time, then reports only the ones it could not log.
--
-- Stored in the database rather than held in memory so that a container
-- restart between runs loses nothing: whatever is still 'pending' is picked up
-- by the next run, or by the catch-up run at startup if a slot was missed.
--
-- received_at is the time the post arrived, and it is what the site log's
-- log_date is taken from. Using the time of the batch run instead would file
-- everything posted between 18:00 and midnight under the following day.

CREATE TABLE IF NOT EXISTS pending_messages (
    id BIGSERIAL PRIMARY KEY,
    group_id TEXT NOT NULL,
    sender_name TEXT,
    sender_number TEXT,
    text TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- 'pending' → 'logged' | 'skipped' (chat, not a log) | 'failed'
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    processed_at TIMESTAMPTZ
);

-- The batch run reads only the pending rows, oldest first.
CREATE INDEX IF NOT EXISTS pending_messages_pending_idx
    ON pending_messages (received_at)
    WHERE status = 'pending';
