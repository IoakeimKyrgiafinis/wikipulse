# Learning note: Spec 03a, Page enrichment

## What was built

For every page that spikes, WikiPulse now fetches what later stages need: the canonical
title, a short summary, the Wikidata ID, the categories and the outgoing article links.
Results go into a new Postgres table `pages`; every API answer is cached in a second
table, `api_cache`, for 24 hours.

| File | Role |
|------|------|
| `wikipulse/enrich/client.py` | `WikimediaClient`: a polite **async** HTTP client. User-Agent, at most `WIKI_API_CONCURRENCY` requests in flight, request starts spaced `ENRICH_MIN_INTERVAL_S` apart, `maxlag=5` on Action API calls, retries with backoff (honouring `Retry-After`), and a response cache with a time-to-live. Counts requests, cache hits and misses in `ClientStats`. |
| `wikipulse/enrich/cache.py` | The caches behind the client: `PostgresCache` (table `api_cache`) and `MemoryCache` (tests, scripts). |
| `wikipulse/enrich/pages.py` | `enrich(title, client, api_url, rest_url) -> PageInfo` (and `Enricher`, which binds the client and URLs so callers write `await enricher.enrich(title)`). Parses redirects, missing pages, invalid titles and disambiguation pages. |
| `wikipulse/db/pages.py` | The `pages` table: `upsert_page`, `record_error`, `get_page`, and `pending_titles` (which spike titles need enrichment). |
| `wikipulse/db/migrations/0002_create_pages.sql` | Creates `pages` and `api_cache`. |
| `wikipulse/enrich/run.py` | `uv run python -m wikipulse.enrich.run`: polls the `spikes` table and enriches new spikes' pages; `--once` for a backfill, with a report of API calls and the cache hit rate. |
| `scripts/make_enrich_fixtures.py` | Records real API responses for the tests into `tests/fixtures/enrich/` (bodies only, never the User-Agent). |
| `tests/enrich/` | 32 unit tests (saved responses, no network) and 4 integration tests (real Postgres). |

How the pieces fit:

```
spike detector ──► Postgres `spikes`
                        │  every 10 s: pending_titles()  (new spike, no fresh `pages` row)
                        ▼
              wikipulse.enrich.run ── asyncio.gather(enrich(title) for each title)
                        │
                        ▼
              enrich(title)                         WikimediaClient
                1. action=query (redirects=1,  ──►  cache lookup (api_cache, 24 h)
                   pageprops|links|categories,       └─ miss: semaphore (4 in flight),
                   follow `continue`)                   spacing, User-Agent, retries
                2. REST page/summary/<canonical>
                        │
                        ▼
              Postgres `pages` (one row per spike title)
```

## Key concepts

### Working with external APIs at scale: caching, batching, rate limits, retries

Wikimedia's API etiquette (https://www.mediawiki.org/wiki/API:Etiquette) asks clients
to identify themselves, not to hammer the servers, to back off when told to, and to use
`maxlag`. `WikimediaClient` in `wikipulse/enrich/client.py` does each of these:

- **Identify yourself.** Every request carries `USER_AGENT` from config.
- **Limit the load.** `_send()` holds an `asyncio.Semaphore(WIKI_API_CONCURRENCY)` while
  a request is in flight, and `_wait_for_turn()` spaces request *starts* at least
  `ENRICH_MIN_INTERVAL_S` (0.2 s) apart. Together: at most 4 requests at once and at
  most 5 new ones per second, however many pages are waiting.
- **`maxlag`.** Every Action API request gets `maxlag=5`. When the database replicas
  lag more than 5 seconds, the API answers with error code `maxlag` instead of doing
  the work; the client treats that like a 503 and waits.
- **Retries with backoff.** Network errors, HTTP 429 and 5xx and `maxlag` are retried
  up to 4 attempts (`MAX_ATTEMPTS`, shared with `wikipulse/eval/wiki_client.py`),
  waiting about 1, 2, 4 s with jitter (`Backoff` from `wikipulse/stream/resilience.py`)
  or longer if the server sent `Retry-After`. The semaphore is released while waiting,
  so a sleeping retry doesn't block a slot. Other errors (403, a MediaWiki error body
  like `badvalue`) are raised at once as `WikiApiError`: retrying wouldn't help.
- **Caching.** `get_json()` looks the request up in the cache first. The key is a
  SHA-256 of the URL and its sorted parameters (`request_key()`), so the same question
  is asked at most once per `ENRICH_CACHE_TTL_H` (24 h). Both HTTP 200 and **404** are
  cached: "this page doesn't exist" is a valid answer and shouldn't be asked again on
  every poll (the 02b review found that the eval client re-requested 404s). Error
  bodies are never cached, so a temporary failure can't stick.

There are two levels of "once a day". The `pages` table says whether a page needs
enriching at all (`pending_titles()` in `wikipulse/db/pages.py`: no row yet, or a
spike started after the row was written and the row is older than the TTL). The
`api_cache` table makes even a forced re-enrichment free while the answers are fresh.

**Batching.** The Action API accepts up to 50 titles per request, and stage 2's
evaluation used that for redirects. Here it was left out on purpose: the expensive part
is the link list, and `pllimit=max` (500 links) is shared by *all* titles of a batch, so
a batch of 50 pages would need about as many `continue` requests as 50 single
requests, with much harder bookkeeping. What *is* batched is the work per page: one
`action=query` returns redirects, normalization, existence, the disambiguation flag, the
Wikidata ID, links and categories together (`query_params()` in `pages.py`).

### Wikidata

Every Wikipedia article is connected to a Wikidata item with a stable ID: *Susie
Wiles* is `Q113560461`, *Frozen yogurt* is `Q13319`. The ID doesn't change when the
article is renamed or when another language calls the person differently, which is why
stage 4 uses it to decide that two mentions are the same entity. `enrich()` takes it
from the REST summary's `wikibase_item`, and falls back on the Action API's
`pageprops.wikibase_item` (they agree; the fallback matters if the summary is missing).
Open https://www.wikidata.org/wiki/Q113560461 to see her structured facts (position
held, employer, dates).

### Async concurrency with limits

Enriching 53 pages one request at a time, with about 0.3 s per request and 2 to 4
requests per page, takes over a minute. `enrich_titles()` in `wikipulse/enrich/run.py`
starts all pages at once with `asyncio.gather`, and the client's semaphore makes sure
only 4 requests are ever on the wire. While one request waits for the network, the
event loop sends others. The test
`test_never_more_than_concurrency_requests_in_flight` (in
`tests/enrich/test_client.py`) fires 20 requests at a client with a limit of 3 and
checks, from inside the fake server, that exactly 3 were ever in flight at the same
time: the limit is reached and never exceeded.

One practical lesson: psycopg's async mode doesn't work on Windows' default event loop
(`ProactorEventLoop`). `PostgresCache` therefore uses a normal (thread-safe) psycopg
connection and runs each query in a worker thread with `asyncio.to_thread`, which works
the same on Windows and Linux.

### Redirects, missing pages and disambiguation pages

`enrich()` never raises for an odd page; it returns a `PageInfo` with a `status`:

| Case | How it's detected | What is stored |
|------|------|------|
| Redirect (*Susan Wiles*) | `query.redirects` in the Action API response (`resolve_title()`) | `title` = the spike's title, `canonical_title` = *Susie Wiles*, `is_redirect` = true, data of the target |
| Normalization (*frozen yogurt*) | `query.normalized` | canonical *Frozen yogurt*, not a redirect |
| Missing page (deleted, never created) | `missing: true` on the page | status `missing`, no summary request |
| Invalid title (`Invalid [title]`) | `invalid: true` | status `invalid` |
| Disambiguation (*Shipley*) | `pageprops.disambiguation`, or summary `type` = `disambiguation` | status `disambiguation`, links kept (they are the candidate meanings) |
| API keeps failing | `WikiApiError` after retries | status `error` with the message; retried after 10 minutes; never overwrites an older good row |
