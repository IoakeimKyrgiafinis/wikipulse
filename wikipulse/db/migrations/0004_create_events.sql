-- Events: spikes grouped into stories (spec 03b).

-- One row per event of a grouping run. `run_id` names the grouping run; `spike_run_id`
-- the detector run whose spikes it groups (often the same name, e.g. "live").
-- `number` counts events within the run (1, 2, 3, ...). A merged event keeps its row
-- with status 'merged' and `merged_into` = the number of the event that absorbed it.
-- All times are event times (from the spikes), except `updated_at`.
CREATE TABLE events (
    id            bigserial        PRIMARY KEY,
    run_id        text             NOT NULL,
    spike_run_id  text             NOT NULL,
    number        integer          NOT NULL,
    status        text             NOT NULL CHECK (status IN ('active', 'ended', 'merged')),
    title         text             NOT NULL,  -- provisional: the highest-scoring page
    started_at    timestamptz      NOT NULL,  -- earliest spike start
    last_spike_at timestamptz      NOT NULL,  -- latest spike start
    ended_at      timestamptz,                -- last edit of its last spike; NULL if not ended
    page_count    integer          NOT NULL,
    score         double precision NOT NULL,  -- highest spike score
    merged_into   integer,
    updated_at    timestamptz      NOT NULL DEFAULT now(),  -- wall clock, metadata only
    UNIQUE (run_id, number)
);

CREATE INDEX events_run_status_idx ON events (run_id, status);

-- Which spike belongs to which event. Each spike is in exactly one event per grouping
-- run; when events merge, their rows move to the surviving event.
CREATE TABLE event_pages (
    run_id       text             NOT NULL,
    spike_id     bigint           NOT NULL REFERENCES spikes (id) ON DELETE CASCADE,
    event_number integer          NOT NULL,
    title        text             NOT NULL,  -- the spike's title, for reading
    joined_score double precision NOT NULL,  -- pair score it joined with (1 for the first)
    PRIMARY KEY (run_id, spike_id),
    FOREIGN KEY (run_id, event_number) REFERENCES events (run_id, number) ON DELETE CASCADE
);

CREATE INDEX event_pages_event_idx ON event_pages (run_id, event_number);
