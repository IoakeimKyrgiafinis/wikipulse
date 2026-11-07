# Review: Spec 03a, Page enrichment

Branch `spec-03a-page-enrichment`, commits `31d0c75` (WIP) and `e349403` (finish), compared with `main`.
Reviewed on 2026-11-07. Docker services (Redis, Postgres) were running.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests with saved API responses (no network): parsing, redirects, missing pages, disambiguation, cache hits | pass | `uv run pytest`: 220 passed, 17 deselected. `tests/enrich/test_pages.py` replays the recorded responses in `tests/fixtures/enrich/*.json` through `FixtureTransport` (`httpx.MockTransport`, which fails on any request it has no saved response for). It covers an ordinary page, a redirect (*Susan Wiles* -> *Susie Wiles*), normalization, a disambiguation page, a missing page, an invalid title, a `/` in a title, `continue` pages and the link cap. `tests/enrich/test_client.py` covers cache hits, TTL expiry, cached 404s, retries, `maxlag` and the User-Agent. |
| An integration test for the cache table | pass | `uv run pytest -m integration tests/enrich`: 4 passed. `test_cache_roundtrip_and_ttl` (`api_cache` upsert, expiry, purge) and `test_client_reuses_the_postgres_cache` (a second client makes 0 requests). The full `-m integration` suite also passes (17 passed). |
| A live run enriches a day's spikes; report API calls and cache hit rate | pass | I checked this from the database and the cache rather than with a new cold run. Every one of the 563 distinct titles of run `02b-loose` with spikes started on 2026-11-05 has a `pages` row: a non-forced `--once` run of that day found 0 pending pages and made 0 API calls. A `--force --once` run took 9.4 s, enriched all 563 pages (554 ok, 6 disambiguation, 3 missing) with **0 API calls, 1313 cache hits, 0 misses (100 %)**. The cold-run numbers in the learning note (1,237 API calls, 20 of them retries, hit rate 0.1 %) fit the 2,415 rows now in `api_cache`. |
| Req. 1: new spikes enriched within a minute | pass (steady state) | `uv run python scripts/enrich_latency.py 03a-latency`: 12 of 12 within 60 s, median 23.4 s, max 31.6 s. This depends on the stage-2 snapshot change (see below) and on there being no backlog (problem 7). |
| Req. 2: missing pages, redirects and disambiguation pages recorded, not crashes | pass | The `pages` table holds 5 missing, 15 disambiguation and 17 redirect rows. Tests cover each case, plus invalid titles, API errors and unexpected answer shapes (`test_unexpected_answers_are_recorded_as_errors`). Gaps: problems 1, 2 and 11. |
| Req. 3: never more than `WIKI_API_CONCURRENCY` in flight | pass | `test_never_more_than_concurrency_requests_in_flight` measures from inside the fake server: the in-flight count reaches the limit of 3 and never exceeds it. `ClientStats.max_in_flight` is reported by the runner. |
| ruff | pass | `uv run ruff check .`: All checks passed. `uv run ruff format --check .`: 131 files already formatted. |
| Scope and secrets | pass | No grouping and no LLM use. No new dependencies (`pyproject.toml` is unchanged). Nothing under `data/`, no `.env`, and no real email address in the diff (fixtures hold response bodies only). `docs/devlog.md` is untouched. |

### The `DETECT_SNAPSHOT_INTERVAL_S` change from 60 to 30

**Justified.** The detector writes spikes to Postgres only when it takes a snapshot (`SpikeRunner.snapshot` in `wikipulse/detect/runner.py`). With 60 s, a spike could reach the enricher up to 60 s after it started, before the 10 s poll and the API calls. That makes requirement 1 impossible to meet in the worst case. The change:
- is the smallest one that makes the requirement reachable,
- stays a setting that can be overridden,
- is documented in `config.py`, `.env.example` and the learning note,
- was measured: max 31.6 s over a 40-minute live run.

Spec 02a only says "for example, every minute", so 30 s is still within 02a's intent. The cost is twice as many Redis snapshot transactions, which is negligible.

Two caveats, neither of which blocks the merge:
- A cleaner long-term design is to flush *new* spikes to Postgres as soon as they are created, separately from the state checkpoint (or to use LISTEN/NOTIFY in 06b). That would decouple enrichment latency from checkpointing.
- `docs/learning/02a-spike-detector.md:71` still says 60 s.

## Problems

1. **medium**, `wikipulse/db/pages.py:30-35` and `:46-48`: when re-enriching a *stale good row* fails, `RECORD_ERROR` doesn't touch the row (`WHERE pages.status = 'error'`), so `enriched_at` stays old. The title therefore stays pending, and it is retried on every poll (every 10 s) indefinitely. This bypasses the 10-minute `ERROR_RETRY` and repeatedly hits Wikimedia (for example, one 403 every 10 s, or six backed-off attempts per cycle for a persistent 5xx). No test covers this case.
2. **medium**, `wikipulse/enrich/pages.py:100-111` and `:180-196`: `resolve_title` drops the redirect's `tofragment`, so a redirect to a *section* is stored with the parent article's Wikidata ID. Real rows show this: *Methos* -> *List of Highlander characters* (#Recurring characters), and *Wilkinson spelling* -> *Richard James Wilkinson* (Q4120852, "Colonial administrator"). Stage 4 relies on these IDs as entity identity, and nothing in `pages` lets it tell these cases apart.
3. **low**, `wikipulse/enrich/run.py:187-196`: unlike `stream/consume.py` (`warn_if_placeholder_user_agent`), the enrichment runner doesn't warn when `USER_AGENT` is still a placeholder. The learning note confirms the ~1,300 live requests were sent without a real contact address, which goes against AGENTS.md's politeness rule and may have contributed to the HTTP 429s.
4. **low**, `wikipulse/enrich/run.py:38,40` and `wikipulse/enrich/client.py:42,110-112,128`: some tuning values are hard-coded instead of living in `config.py`:
   - `ERROR_RETRY` (10 min), `PAGES_PER_SLOT`, `MAX_ATTEMPTS` and the 30 s HTTP timeout,
   - constructor defaults that duplicate config and partly disagree with it: `min_interval_s=0.2` versus `ENRICH_MIN_INTERVAL_S=0.5`, `concurrency=4`, and `max_links=5000` in `pages.py:204,220`.
5. **low**, `wikipulse/enrich/run.py:195`: the report is written with `print` inside the package, while AGENTS.md asks for `logging`. Other CLIs (for example `check.py`) do the same, so this is a nitpick.
6. **low**, `wikipulse/enrich/pages.py:127`: an Action API answer with no `pages` at all (an unexpected shape, or a warning-only body) is classified as `invalid` (a permanent fact about the title) instead of `error` (which would be retried).
7. **low**, `wikipulse/enrich/run.py:162-181`: each poll enriches its whole pending list before it looks for new spikes, oldest first. After a restart or with a backlog (the cold day took 710 s), new spikes wait behind old ones, so the one-minute requirement only holds in steady state.
8. **low**, `tests/enrich/test_enrich_db.py:75-78`: the integration test deletes the real `api_cache` rows for *Syed Soharwardy* from the shared development database, not just rows it created.
9. **low**, `wikipulse/enrich/pages.py:157-160`: when the link cap stops the `continue` loop, any remaining category pages (`clcontinue`) are dropped too, without a flag. `links_truncated` is also set when the list holds exactly `max_links` links and only categories were still continuing.
10. **low**, `wikipulse/enrich/pages.py:107-110`: chained redirects are resolved only if the API lists them in chain order. A one-pass loop over `redirects` misses an out-of-order chain, after which `find_page` falls back to the single page.
11. **low**, `wikipulse/enrich/run.py:62-79`: `enrich_one` catches API and parsing errors but not database errors from `upsert_page` (for example a NUL character in an extract). One bad row would crash the whole `gather` and stop the runner.

Verdict: fix

The core work is solid and well tested, and the snapshot change is justified. Problem 1 (unthrottled retries against Wikimedia) and problem 2 (wrong Wikidata IDs for section redirects, which stage 4 depends on) should be fixed before merging. Both are small changes.

## Questions for the human before merging

1. A page was enriched successfully two days ago. It spikes again, and the re-enrichment now fails with HTTP 403. What does the `pages` row look like afterwards, and how often will `wikipulse.enrich.run` ask Wikimedia about that page from then on?
2. Why does the delay between a spike starting and its page being enriched depend on a *stage 2* setting (`DETECT_SNAPSHOT_INTERVAL_S`)? With the current defaults, what is the worst-case delay, and what situations could still push it past a minute?
3. The spike title *Methos* redirects to *List of Highlander characters#Recurring characters*. Which Wikidata ID ends up in `pages.wikidata_id`, and what could go wrong in stage 4 if it is used as the identity of the person *Methos*?
