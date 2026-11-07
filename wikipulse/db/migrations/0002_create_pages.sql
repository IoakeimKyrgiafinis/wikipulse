-- Page enrichment (spec 03a).

-- One row per spiking page title, as the spike names it. `canonical_title` is the title
-- the API returned after normalization and redirects, so a renamed page still joins to
-- its spikes by `title` while later stages can use the current name.
CREATE TABLE pages (
    title           text        PRIMARY KEY,
    canonical_title text        NOT NULL,
    status          text        NOT NULL
                    CHECK (status IN ('ok', 'missing', 'disambiguation', 'invalid', 'error')),
    is_redirect     boolean     NOT NULL DEFAULT false,
    page_id         bigint,
    wikidata_id     text,                         -- e.g. Q76; NULL if the page has none
    description     text,                         -- Wikidata's short description
    summary         text,                         -- the lead extract from page/summary
    categories      text[]      NOT NULL DEFAULT '{}',  -- visible categories, no prefix
    links           text[]      NOT NULL DEFAULT '{}',  -- outgoing links to articles (ns 0)
    links_truncated boolean     NOT NULL DEFAULT false, -- more than ENRICH_MAX_LINKS links
    error           text,                         -- what went wrong when status = 'error'
    enriched_at     timestamptz NOT NULL DEFAULT now()  -- wall clock: age of this row
);

CREATE INDEX pages_canonical_title_idx ON pages (canonical_title);
CREATE INDEX pages_wikidata_id_idx ON pages (wikidata_id);

-- Response cache of the enrichment client: one row per distinct request.
-- `request_key` is a hash of the URL and its parameters. `status` is the HTTP status
-- (200, or 404 for a page that doesn't exist); `fetched_at` decides freshness.
CREATE TABLE api_cache (
    request_key text        PRIMARY KEY,
    url         text        NOT NULL,
    params      jsonb       NOT NULL,
    status      integer     NOT NULL,
    body        jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX api_cache_fetched_at_idx ON api_cache (fetched_at);
