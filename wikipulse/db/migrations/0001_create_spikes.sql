-- Spikes found by the detector (spec 02a).
-- One row per spike. `run_id` separates the live detector ("live") from replays and
-- experiments, so the same recording can be detected twice and compared.
CREATE TABLE spikes (
    id               bigserial PRIMARY KEY,
    run_id           text             NOT NULL DEFAULT 'live',
    title            text             NOT NULL,
    status           text             NOT NULL CHECK (status IN ('active', 'ended')),
    started_at       timestamptz      NOT NULL,  -- when the start rule first held
    first_edit_at    timestamptz      NOT NULL,  -- oldest edit of the burst
    last_edit_at     timestamptz      NOT NULL,
    ended_at         timestamptz,                -- NULL while active
    total_edits      integer          NOT NULL,
    distinct_editors integer          NOT NULL,
    revert_edits     integer          NOT NULL DEFAULT 0,
    peak_edits_10m   integer          NOT NULL,
    score            double precision NOT NULL,
    updated_at       timestamptz      NOT NULL DEFAULT now(),  -- wall clock, metadata only
    UNIQUE (run_id, title, started_at)
);

CREATE INDEX spikes_run_status_idx ON spikes (run_id, status);
CREATE INDEX spikes_started_at_idx ON spikes (started_at);
