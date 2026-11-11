-- Correlation ids (spec 09b).

-- A spike's correlation id is the event_id (Wikimedia's meta.id) of the edit that made
-- its start rule hold; an event's is the one of its earliest spike. Logs and LLM traces
-- of every stage carry the same id, so one search follows a story from its first edit to
-- its briefing. Null for rows written before this migration.
ALTER TABLE spikes ADD COLUMN correlation_id text;
ALTER TABLE events ADD COLUMN correlation_id text;
