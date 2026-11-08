-- Knowledge graph extraction (spec 04b).

-- One row per event of a grouping run that the graph runner has tried to extract. The
-- graph itself lives in Neo4j; this table remembers which events are done, which failed
-- (and when to try again), and what each extraction cost.
-- `fingerprint` lists the event's spike ids at extraction time: when spikes join (or the
-- event absorbs another one), it no longer matches and the event is extracted again.
CREATE TABLE graph_extractions (
    run_id         text             NOT NULL,
    event_number   integer          NOT NULL,
    status         text             NOT NULL CHECK (status IN ('ok', 'error')),
    fingerprint    text             NOT NULL,
    failures       integer          NOT NULL DEFAULT 0,  -- failed attempts in a row
    error          text,                                -- the last failure, if status = 'error'
    model          text,
    prompt_version text,
    pages          integer,          -- pages shown to the model
    entities       integer,          -- distinct entities written
    linked         integer,          -- of which linked to Wikidata
    relations      integer,
    seconds        double precision, -- wall-clock time of the whole extraction
    llm_seconds    double precision, -- of which waiting for the LLM
    input_tokens   integer,
    output_tokens  integer,
    attempted_at   timestamptz      NOT NULL DEFAULT now(),  -- wall clock
    PRIMARY KEY (run_id, event_number),
    FOREIGN KEY (run_id, event_number) REFERENCES events (run_id, number) ON DELETE CASCADE
);
