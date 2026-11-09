-- Per-hour statistics of the data lake (spec 06b), written by the event-driven worker.

-- One row per hour of one archive. `dataset` is '<bucket>/<key prefix>' (for example
-- 'wikipulse-lake/edits'), so the live archive, recorded history and test data never
-- share a row. `hour` is the event-time hour of the partition the file lives in.
-- The handler always recomputes the whole hour from the files, so writing the same row
-- twice changes nothing (idempotent upsert by dataset and hour).
-- `source_size` and `source_modified` describe the files the numbers were computed
-- from: the backfill compares them with the lake to find hours that are missing or
-- out of date, and an upsert never replaces numbers from newer files with older ones.
CREATE TABLE hourly_stats (
    dataset          text             NOT NULL,
    hour             timestamptz      NOT NULL,
    edits            integer          NOT NULL,
    distinct_pages   integer          NOT NULL,
    distinct_editors integer          NOT NULL,
    bot_edits        integer          NOT NULL,
    bot_share        double precision NOT NULL,  -- bot_edits / edits (0 for an empty hour)
    top_pages        jsonb            NOT NULL,  -- [{"title", "edits", "editors"}, ...]
    source_files     integer          NOT NULL,
    source_size      bigint           NOT NULL,  -- bytes, all files of the hour
    source_modified  timestamptz      NOT NULL,  -- newest file's last-modified time
    computed_at      timestamptz      NOT NULL DEFAULT now(),  -- wall clock
    PRIMARY KEY (dataset, hour)
);
