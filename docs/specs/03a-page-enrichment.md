# Spec 03a: Page enrichment

Status: done
Stage: 3. Event grouping
Owner: agent
Depends on: 02a

## Goal
For every spiking page, fetch the information later stages need: a short summary, its Wikidata ID, its categories and its links to other pages, cached so each page is fetched at most once a day.

## What you'll learn
- **Working with external APIs at scale:** caching, batching, rate limits and retries. Read Wikimedia's API etiquette page before running.
- **Wikidata:** open a few Wikidata pages in your browser (search any famous person) and see how every topic gets a stable ID with structured facts. Stage 4 depends on this.
- **Async concurrency with limits:** fetching many pages at once without overloading the service.

## Context
- The Wikipedia REST API's page summary endpoint (`https://en.wikipedia.org/api/rest_v1/page/summary/<title>`) returns a short extract and the page's Wikidata ID (`wikibase_item`).
- Links and categories come from the MediaWiki Action API (`action=query` with `prop=links|categories`). Limit links to articles (namespace 0).
- Page titles can change (renames); store the canonical title the API returns.

## Outputs
- `wikipulse/enrich/client.py`: a polite async Wikimedia client (User-Agent, concurrency limit, retries with backoff, response cache in Postgres with a time-to-live)
- `wikipulse/enrich/pages.py`: `enrich(title) -> PageInfo` with canonical title, summary, Wikidata ID, categories, outgoing links
- `uv run python -m wikipulse.enrich.run`: enriches pages as spikes start (reading new spikes from Postgres)
- Migration adding a `pages` table
- Config: `WIKI_API_CONCURRENCY=4`, `ENRICH_CACHE_TTL_H=24`

## Requirements
1. New spikes are enriched within a minute of starting.
2. Missing pages, redirects and disambiguation pages are handled and recorded, not crashes.
3. Never more than `WIKI_API_CONCURRENCY` requests in flight.

## Acceptance criteria
- Unit tests with saved API responses (no network): parsing, redirects, missing pages, disambiguation, cache hits.
- An integration test for the cache table.
- A live run enriches a day's spikes; report the number of API calls and the cache hit rate.

## Look at it yourself (optional, never blocking)
- Look at 10 enriched pages. Are summaries and Wikidata IDs right?

## Out of scope
- Grouping (spec 03b), any LLM use
