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
| `wikipulse/enrich/run.py` | `uv run python -m wikipulse.enrich.run`: polls the `spikes` table and enriches new spikes' pages with a small worker pool; `--once` for a backfill, with a report of API calls, cache hit rate and HTTP statuses. |
| `scripts/enrich_latency.py` | For a run, how long after each spike started its page was enriched (requirement 1). |
| `scripts/make_enrich_fixtures.py` | Records real API responses for the tests into `tests/fixtures/enrich/` (bodies only, never the User-Agent). |
| `tests/enrich/` | 36 unit tests (saved responses, no network) and 4 integration tests (real Postgres). |

How the pieces fit:

```
spike detector ──► Postgres `spikes`
                        │  every 10 s: pending_titles()  (new spike, no fresh `pages` row)
                        ▼
              wikipulse.enrich.run ── 8 workers, each: enrich(title), store, next title
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
  `ENRICH_MIN_INTERVAL_S` (0.5 s) apart. Together: at most 4 requests at once and at
  most 2 new ones per second, however many pages are waiting. (The first version
  allowed 5 per second; Wikimedia answered that with HTTP 429, see "Results".)
- **`maxlag`.** Every Action API request gets `maxlag=5`. When the database replicas
  lag more than 5 seconds, the API answers with error code `maxlag` instead of doing
  the work; the client treats that like a 503 and waits.
- **Retries with backoff.** Network errors, HTTP 429 and 5xx and `maxlag` are retried
  up to 6 attempts (`MAX_ATTEMPTS` in `client.py`), waiting about 1, 2, 4, 8, 16 s with
  jitter (`Backoff` from `wikipulse/stream/resilience.py`) or longer if the server sent
  `Retry-After`. Other errors (403, a MediaWiki error body like `badvalue`) are raised
  at once as `WikiApiError`: retrying wouldn't help.
- **Slow down as a whole.** A 429 means "this client is too fast", not "this request
  is bad". So a retry also calls `_pause_everyone(delay)`: until the delay is over,
  `_wait_for_turn()` lets *no* request start, not just the throttled one. Without
  this, the other three slots kept sending while one request waited, and four retries
  in seven seconds were not enough (the first backfill gave up on 12 pages). The test
  `test_a_throttled_request_pauses_every_other_request` checks it with real timers.
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
runs a small *worker pool*: `2 * WIKI_API_CONCURRENCY` workers (`PAGES_PER_SLOT`) share
one iterator over the titles; each takes a title, enriches and stores it, then takes the
next. The client's semaphore makes sure only 4 requests are ever on the wire, and while
one request waits for the network, the event loop sends others.

Why not simply `asyncio.gather(enrich(t) for t in titles)`? The first version did, and a
backfill of 508 pages stored *nothing* for over an hour. The semaphore is first come,
first served: all 508 first requests (the `action=query`) were queued before any
page's second request (its summary), so no page could finish until nearly every page
had been asked about, and an interruption would have lost everything. With the worker
pool, pages finish one after another from the first seconds, and a Ctrl+C takes effect
after the pages in progress (`stop` is checked before each title). Tests:
`test_enrich_titles_keeps_a_bounded_number_of_pages_in_progress` and
`test_enrich_titles_stops_after_the_pages_in_progress`.

The limit on requests is tested too:
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
| Redirect to a section (*Methos* -> *List of Highlander characters#Recurring characters*) | `tofragment` in `query.redirects` | as a redirect, plus `redirect_fragment` = the section; `wikidata_id` and `description` left empty (they describe the whole article, not the topic) |
| Normalization (*frozen yogurt*) | `query.normalized` | canonical *Frozen yogurt*, not a redirect |
| Missing page (deleted, never created) | `missing: true` on the page | status `missing`, no summary request |
| Invalid title (`Invalid [title]`) | `invalid: true` | status `invalid` |
| Disambiguation (*Shipley*) | `pageprops.disambiguation`, or summary `type` = `disambiguation` | status `disambiguation`, links kept (they are the candidate meanings) |
| API keeps failing | `WikiApiError` after retries | status `error` with the message; retried after 10 minutes; never overwrites an older good row |

## Design decisions and deviations

- **`enrich(title, client, api_url, rest_url) -> PageInfo`** instead of the spec's
  `enrich(title) -> PageInfo`. Passing the client in makes the function testable with a
  fake transport and lets all pages share one client (one semaphore, one cache).
  `Enricher.enrich(title)` gives the spec's one-argument form.
- **Two tables, one migration** (`0002_create_pages.sql`): `pages` as the spec asks,
  plus `api_cache` for the client's "response cache in Postgres with a time-to-live".
  A `pages` row is keyed by the spike's title; `canonical_title` holds the title the
  API returned, so a renamed page still joins to its spikes.
- **Five statuses** (`ok`, `missing`, `disambiguation`, `invalid`, `error`). The spec
  names missing pages, redirects and disambiguation pages; invalid titles and API
  failures are recorded the same way, so nothing crashes the runner. A redirect is not a
  status but a flag (`is_redirect`), because the target page is a normal page. An error
  row never overwrites an older good row, and is retried after 10 minutes
  (`ERROR_RETRY` in `run.py`). Answers shaped unlike anything we know (a `KeyError`
  while parsing) are recorded as errors too.
- **Freshness uses the wall clock**, not event time. AGENTS.md's "event time" rule is
  about processing logic (windows, baselines, spikes). How old our copy of a web page
  is, is a property of the copy, so `api_cache.fetched_at` and `pages.enriched_at` use
  Postgres' `now()`. Enrichment results don't affect detection.
- **Polling, not notifications.** `run.py` asks Postgres for pending titles every
  `ENRICH_POLL_INTERVAL_S` (10 s). Postgres `LISTEN/NOTIFY` or a Redis stream would be
  faster, but spec 06b (event-driven worker) is where that belongs; polling is simple
  and well inside the one-minute budget.
- **Detector snapshots every 30 s instead of 60 s** (`DETECT_SNAPSHOT_INTERVAL_S`, a
  stage 2 setting). The detector writes spikes to Postgres only at snapshots, so with
  60 s a spike could reach the enricher up to 60 s after it started, and requirement 1
  ("within a minute") could not hold. 30 s snapshot + 10 s poll + a few seconds of API
  calls stays under a minute. The 02a learning note now mentions the change.
- **Request rate: 2 per second** (`ENRICH_MIN_INTERVAL_S=0.5`, a setting the spec
  didn't ask for). The first version allowed 5 per second and got HTTP 429. With 2 per
  second, Wikimedia still answered 429 now and then (about once a minute, with
  `Retry-After` of 8 to 13 s), but every request succeeded after the pause.
- **Extra settings**: `ENRICH_MIN_INTERVAL_S`, `ENRICH_MAX_LINKS` (5000; long list
  articles have thousands of links) and `ENRICH_POLL_INTERVAL_S`, all in
  `.env.example`.
- **Hidden categories are left out** (`clshow=!hidden`): they are maintenance labels
  like "Articles with short description", useless for grouping events.
- **No batching across titles** (see "Batching" above).
- **Its own retry count**: `MAX_ATTEMPTS = 6` in `enrich/client.py` instead of reusing
  the evaluation client's 4, so a request survives about half a minute of throttling.
- **Finishing a paused implementation.** This spec was started by one agent and
  finished by another. The second one found that the first backfill (02b-loose,
  2026-11-06, 508 pages) had taken over an hour and failed 12 pages with HTTP 429.
  That led to the worker pool, the shared pause after a throttle and the lower request
  rate. It also fixed a flaky spacing test: asyncio may wake a sleeper up to one clock
  tick early (about 16 ms on Windows), so `_wait_for_turn()` now sleeps in a loop until
  the start time has really passed.
- **Review fixes** (migration `0003_pages_attempts_and_fragments.sql`). (1) A failed
  refresh of an old good row used to leave the row untouched, so the title stayed
  pending and was retried on every 10 s poll. `pages.attempted_at` now records every
  attempt; `record_error()` sets it (and the error message) even on a good row, while
  `enriched_at` and the data stay as they were, and `pending_titles()` waits
  `ERROR_RETRY` after a failed attempt. (2) A redirect to a section kept the parent
  article's Wikidata ID, which stage 4 would take as the topic's identity. The section
  is now stored in `redirect_fragment`, and such rows get no `wikidata_id` or
  `description`; the summary, links and categories of the article are kept as context.
  Rows enriched before this fix keep their old values until they are enriched again
  (`--force` reuses cached responses, which contain `tofragment`).
- **The User-Agent** in `.env` still contains placeholder text (the check warns about
  it); set your real contact there before running enrichment for long.

## Results

All runs on 2026-11-07, against the real Wikimedia APIs, with the defaults
(`WIKI_API_CONCURRENCY=4`, `ENRICH_MIN_INTERVAL_S=0.5`, `ENRICH_CACHE_TTL_H=24`).

**A day's spikes** (acceptance criterion 3). The live detector's own run (`live`) has
only 3 spikes, so the "day" is the busiest recorded day: run `02b-loose`, spikes started
on 2026-11-05 (563 distinct titles; 36 of them had been enriched earlier, so 527 were
pending):

```
uv run python -m wikipulse.enrich.run --run-id 02b-loose --since 2026-11-05 --until 2026-11-06 --once
```

| | First run (cold) | Same day again, `--force` |
|---|---:|---:|
| Pages enriched | 527 (518 ok, 6 disambiguation, 3 missing, 0 errors) | 563 |
| Time | 710 s (about 45 pages a minute) | 9.5 s |
| API calls (HTTP requests) | **1,237**, 20 of them retries after HTTP 429 | **0** |
| Cache lookups | 1 hit, 1,217 misses: **hit rate 0.1 %** | 1,313 hits, 0 misses: **hit rate 100 %** |
| Most requests in flight | 2 | 0 |

How to read it:
- About **2.3 requests per page**: one `action=query`, one summary, and extra `continue`
  pages for long link lists. Missing pages need just one.
- The cold run's hit rate is near zero by design: within one day, each page is asked
  about once. The cache pays off on the second look: re-enriching the same 563 pages
  within 24 hours cost nothing. In live use the `pages` table already stops a page from
  being enriched twice a day, so the cache mostly catches forced re-runs, restarts and
  two spike titles that redirect to the same page (the one hit above).
- At 2 requests per second, at most 2 were ever in flight: answers come back in under
  half a second, before the next request may start. `WIKI_API_CONCURRENCY=4` matters
  when Wikimedia is slow.
- **HTTP 429**: Wikimedia throttled us about once a minute (`Retry-After` 8 to 13 s);
  every throttled request succeeded after the pause, so no page failed. The earlier
  5-requests-per-second run of 2026-11-06 lost 12 of 508 pages to 429s; re-running it
  10 minutes later (`ERROR_RETRY`) enriched all 12 (24 calls, 0 errors).
- Of the 563 pages: 11 were redirects (e.g. *2026 Tampa shooting* -> *2026 Tampa
  Citibank shooting*), 33 ok pages had no Wikidata ID yet (mostly brand-new articles),
  none hit the 5000-link cap; on average 324 links and 10 visible categories per page.

**Within a minute of starting** (requirement 1). A live pipeline ran for 40 minutes
(17:20 to 18:00 UTC): the stream consumer into a separate stream
(`--stream-key wiki:latency03a:edits --namespace wiki:latency03a`), the detector as run
`03a-latency` with the loose thresholds of 02b (`DETECT_MIN_EDITS=3`,
`DETECT_MIN_EDITORS=2`, `DETECT_LONG_MIN_EDITS=6`, `DETECT_LONG_MIN_EDITORS=2`, so
there were enough spikes to measure; the default thresholds give about three an hour)
and `uv run python -m wikipulse.enrich.run --run-id 03a-latency`, all with the new
defaults (30 s snapshots, 10 s poll). Then `uv run python scripts/enrich_latency.py
03a-latency`:

- 15 spikes. **12 of 12** pages that needed enriching were enriched **within 60 s**
  of the spike's start: median **23.4 s**, max **31.6 s**, min 8.5 s.
- The other 3 (*2026–27 FA Cup*, *Frozen yogurt*, *Ueno Bank*) already had a row from
  earlier the same day, so they were rightly not fetched again (TTL 24 h).
- 28 API calls for 12 pages, no retries, no errors, no 429 (a trickle of requests,
  never more than 1 in flight).
- The delay is almost all waiting: up to 30 s until the detector's next snapshot
  writes the spike to Postgres, up to 10 s until the next poll, then about 1 s of API
  calls. With the old 60 s snapshots the worst case would have been about 71 s.

The run's 15 spikes stay in Postgres as run `03a-latency`; its Redis keys were deleted.

**Looking at 10 enriched pages** (the optional check, a random sample): *Softball*
(Q171038, "Team ball sport"), *Eevee* (Q1263559, "Pokémon species"), *Maumoon Abdul
Gayoom* (Q297957, "President of the Maldives from 1978 to 2008"), *Mithun Chakraborty*
(Q379604, "Indian actor (born 1950)"), *Swan Valley, Idaho* (Q1511174) and five more:
every summary matched its title and every Wikidata description matched the person,
place or thing. *2026 French supermarket refrigerator hack* had a Wikidata ID but no
description yet, normal for an article a few weeks old.

## Check your understanding

1. The first backfill started all 508 pages at once with `asyncio.gather`, and the
   client allowed only 4 requests in flight. Why was not a single page stored for more
   than an hour, and how does the worker pool in `enrich_titles()` fix it?
2. A spike for *Susan Wiles* and, an hour later, a spike for *Susie Wiles* both get
   enriched. How many HTTP requests does the second one cost, and why?
3. Why does `PostgresCache` decide freshness with Postgres' `now()` even though
   AGENTS.md insists on event time, and what would go wrong if `pages.enriched_at`
   used the spike's `started_at` instead?

<details>
<summary>Answers</summary>

1. Every page needs its query answer before it can ask for its summary. An
   `asyncio.Semaphore` serves waiters first come, first served, and all 508 query
   requests had queued before the first summary request, so every page sat waiting for
   its second request until the 508 queries were done (and with 429 pauses that took
   over an hour). The worker pool keeps only 8 pages in progress: each worker
   finishes and stores a page before taking the next, so pages reach Postgres steadily
   and a Ctrl+C loses at most the pages in progress.
2. One request: the `action=query` for *Susie Wiles* is a different request (different
   `titles` parameter), so it is a cache miss. The summary request uses the canonical
   title, *Susie Wiles*, which the first enrichment already fetched, so it is a cache
   hit. Both `pages` rows (one per spike title) point to the same canonical title and
   Wikidata ID.
3. Freshness is about how old our copy of a web page is, a wall-clock fact; event time
   is for decisions about the edit stream (windows, baselines), which enrichment doesn't
   make. With the spike's `started_at`, enriching last week's recorded spikes today
   would stamp fresh copies "a week old": they would count as stale at once, and a
   later spike of the same page would refetch them although they were minutes old.

</details>
