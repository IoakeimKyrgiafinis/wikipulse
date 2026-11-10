-- The news classifier's scores and drift reports (spec 07c).

-- One score per spike: the champion model's answer to "is this real news?", computed
-- FEATURE_WINDOW_MIN after the spike started (at `cutoff`, event time). A table of its
-- own, one row per spike, because the detector rewrites its `spikes` rows every few
-- seconds and knows nothing about models. `features` holds the inputs the model saw
-- (null for unknown), which is what drift monitoring compares with the training data.
CREATE TABLE spike_scores (
    spike_id         bigint           PRIMARY KEY REFERENCES spikes (id) ON DELETE CASCADE,
    run_id           text             NOT NULL,  -- the spike's detector run (as in spikes)
    model_name       text             NOT NULL,
    model_version    text             NOT NULL,  -- the registry version that scored it
    score            double precision NOT NULL,  -- 0..1, higher = more likely news
    is_news          boolean          NOT NULL,  -- score >= the model's threshold
    threshold        double precision NOT NULL,
    cutoff           timestamptz      NOT NULL,  -- started_at + feature window
    features         jsonb            NOT NULL,
    page_revision_id bigint,                     -- the page's revision at the cutoff
    history_complete boolean          NOT NULL,  -- were all 24 h before the burst seen?
    scored_at        timestamptz      NOT NULL DEFAULT now()  -- wall clock, metadata only
);

CREATE INDEX spike_scores_run_cutoff_idx ON spike_scores (run_id, cutoff);

-- One drift report per model version, detector run and period (replaced on a re-run).
CREATE TABLE drift_reports (
    id             bigserial   PRIMARY KEY,
    model_name     text        NOT NULL,
    model_version  text        NOT NULL,
    run_id         text        NOT NULL,
    period_start   timestamptz NOT NULL,  -- spikes whose cutoff is in [start, end)
    period_end     timestamptz NOT NULL,
    reference      text        NOT NULL,  -- e.g. 'datasets/v2 train, default rule fired'
    rows           integer     NOT NULL,  -- scored spikes in the period
    status         text        NOT NULL CHECK (status IN ('ok', 'drift', 'too_few_rows')),
    report         jsonb       NOT NULL,  -- per feature and the score: PSI, KS, means
    created_at     timestamptz NOT NULL DEFAULT now(),  -- wall clock, metadata only
    UNIQUE (model_name, model_version, run_id, period_start, period_end)
);
