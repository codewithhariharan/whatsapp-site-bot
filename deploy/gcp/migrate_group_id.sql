-- Repoint historical rows from the old Cloud-API key (the sender's phone
-- number) onto the real group JID the Baileys bridge reports.
--
-- Run AFTER you have the JID from the bridge's first-boot log. Check first:
--     SELECT group_id, count(*) FROM daily_logs GROUP BY group_id;
--
-- Replace both values, then run the whole file in one transaction so a
-- failure part-way cannot leave the tables keyed inconsistently.

\set old_id '6593471910'
\set new_id '1203630xxxxxxxxxx@g.us'

BEGIN;

INSERT INTO groups (group_id, group_name)
VALUES (:'new_id', 'CR106 LTA PJT (Site Work)')
ON CONFLICT (group_id) DO NOTHING;

UPDATE daily_logs     SET group_id = :'new_id' WHERE group_id = :'old_id';
UPDATE dwall_panels   SET group_id = :'new_id' WHERE group_id = :'old_id';
UPDATE location_order SET group_id = :'new_id' WHERE group_id = :'old_id';

DELETE FROM reorder_sessions WHERE group_id = :'old_id';  -- transient, not worth migrating
DELETE FROM groups           WHERE group_id = :'old_id';

SELECT group_id, count(*) AS logs FROM daily_logs GROUP BY group_id;

COMMIT;
