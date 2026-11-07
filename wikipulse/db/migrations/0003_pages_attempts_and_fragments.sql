-- Page enrichment fixes (spec 03a review).

-- `attempted_at` is the wall-clock time of the last enrichment attempt, successful or
-- not. A failed refresh of an old good row keeps its data and its `enriched_at` (the age
-- of that data) but moves `attempted_at`, so the runner waits before trying again
-- instead of retrying on every poll.
ALTER TABLE pages ADD COLUMN attempted_at timestamptz;
UPDATE pages SET attempted_at = enriched_at;
ALTER TABLE pages
    ALTER COLUMN attempted_at SET NOT NULL,
    ALTER COLUMN attempted_at SET DEFAULT now();

-- The section a redirect points to (`tofragment`), e.g. "Recurring characters" for
-- Methos -> List of Highlander characters#Recurring characters. NULL for whole pages.
-- Such a row has no `wikidata_id`: the article's ID is not the identity of the topic.
ALTER TABLE pages ADD COLUMN redirect_fragment text;
