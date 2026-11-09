-- Daily batch results (spec 06c), written by the scheduled jobs in wikipulse/jobs/.
-- Both tables are keyed by the day they describe, and the jobs always replace a whole
-- row (upsert), so re-running a job for any past day (a backfill) is safe.

-- One row per UTC day of one archive, aggregated from hourly_stats (spec 06b), with the
-- result of the data-quality checks. `dataset` is the same '<bucket>/<prefix>' as there.
CREATE TABLE daily_stats (
    dataset           text             NOT NULL,
    day               date             NOT NULL,  -- UTC day of the edits' event time
    hours_present     integer          NOT NULL,  -- hours with a row in hourly_stats
    missing_hours     integer[]        NOT NULL,  -- hours of the day (0-23) without one
    edits             bigint           NOT NULL,
    bot_edits         bigint           NOT NULL,
    bot_share         double precision NOT NULL,  -- bot_edits / edits (0 without edits)
    min_hour_edits    integer,                    -- NULL when no hour is present
    median_hour_edits double precision,
    max_hour_edits    integer,
    peak_hour         timestamptz,                -- the hour with the most edits
    baseline_edits    double precision,           -- mean edits of earlier complete days
    baseline_days     integer          NOT NULL,  -- how many days that mean is over
    issues            jsonb            NOT NULL,  -- [{"check", "message"}, ...]
    passed            boolean          NOT NULL,  -- no issues
    computed_at       timestamptz      NOT NULL DEFAULT now(),  -- wall clock
    PRIMARY KEY (dataset, day)
);

-- One row per detector run and UTC day: the detection evaluation of spec 02b for that
-- day, against the "Current events" portal. `metrics` holds the headline numbers,
-- `report` the whole report (the same JSON `wikipulse.eval.detection` writes).
CREATE TABLE detection_eval_daily (
    run_id          text        NOT NULL,
    day             date        NOT NULL,
    metrics         jsonb       NOT NULL,
    report          jsonb       NOT NULL,
    portal_missing  date[]      NOT NULL,  -- portal days used that didn't exist (yet)
    evaluated_at    timestamptz NOT NULL DEFAULT now(),  -- wall clock
    PRIMARY KEY (run_id, day)
);
