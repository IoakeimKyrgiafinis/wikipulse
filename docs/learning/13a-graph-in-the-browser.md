# Learning note: 13a The knowledge graph, live in the browser

## What was built

The public demo used to *replay* the 5 November 2026 knowledge graph as static text. Now a
visitor can **query it**: the page starts a real graph database in their browser,
loads the frozen graph into it, and runs Cypher on it. No server, nothing leaves the
browser. The database is **Relate**, an embedded graph engine that speaks Cypher,
compiled to WebAssembly.

```
laptop (once)                                   GitHub Pages (static files)          visitor's browser
──────────────                                  ───────────────────────────          ─────────────────
neo4j-eval (snapshot 2026-11-05) ─┐                                                   index.html, app.js (replay)
data/qa_eval/12a-after/answers ───┼─ scripts/export_graph.py ─► site/data/graph.json   explore.js  ── button ──┐
wikipulse/qa/graph.py constants ──┘                                                                         ▼
                                                                                      graph-worker.js (Web Worker)
npm: @relate/relate-wasm 1.1.0 ─── scripts/vendor_relate.py ─► site/vendor/relate/     ├ relate.js + relate_bg.wasm
   (SHA-512 checked, git-ignored;   (also run by pages.yml)                            ├ graph-db.js: load, run, compare
    never committed)                                                                   └ cypher-check.js: read-only check
```

| File | What it does |
|------|--------------|
| `scripts/export_graph.py` | Reads the snapshot and writes `site/data/graph.json` (1.1 MB): the statements that rebuild the graph (`build_load()`), the 18 non-vector queries of `wikipulse/qa/graph.py` (`shipped_queries()`, with `relate_fulltext()`), the graph's clock (`time_parameters()`), and the 25 questions of the 12a run with the LLM's Cypher, Neo4j's rows (`canonical()` form) and the stored answer (`build_prepared()`). Also the comparison rules (`compare_rows()`, `tie_column()`, `unordered()`) and `bind_times()`. |
| `scripts/vendor_relate.py` | Downloads the pinned npm tarball, checks its integrity hash and each file's SHA-256, extracts four files plus the licence into `site/vendor/relate/`. Standard library only. |
| `scripts/vendor/relate-wasm-LICENSE.txt` | The Apache-2.0 text (the npm package ships none; copied from the same project's `relate-db` wheel). |
| `site/cypher-check.js` | A line-by-line JavaScript port of `validate_cypher()`, plus `bindTimes()` and `luceneTerms()`. |
| `site/graph-db.js` | `GraphDb`: loads `graph.json` into a `RelateDb`, runs named queries and visitor Cypher (validated, row-capped); `canonical()` and `compareRows()` (the same rules as the Python ones). No DOM, so Node can run it. |
| `site/graph-worker.js` | The module Web Worker: `init()` the WebAssembly, fetch `graph.json`, load, answer messages. |
| `site/explore.js` | The section's UI and `GraphClient`: one query at a time, a 5 s limit per query, terminate-and-reload when it runs out. |
| `site/index.html`, `style.css` | The "Explore the graph" section; the CSP gains `'wasm-unsafe-eval'`; result tables become one block per row on phones. |
| `.github/workflows/pages.yml` | Vendors and verifies Relate, runs the Node checks, then uploads; the deploy job runs only on `main`. |
| Tests | `tests/test_demo_graph.py` (17: schema, privacy, the rewrite, the shipped queries are the constants, comparison rules), `tests/qa/test_cypher_cases.py` (80: the shared validator cases in Python), `tests/test_demo_graph_js.py` (2: the same cases in JavaScript; the page's database with the real WebAssembly), `tests/test_vendor_relate.py` (5), `tests/test_demo_graph_neo4j.py` (4, integration: Neo4j vs Relate), updates in `tests/test_demo_site.py` and `tests/test_cicd.py`. |
| Fixtures | `tests/fixtures/cypher_cases.json` (79 validator cases, 5 time-binding cases), `tests/js/*.mjs` (the Node runners). |

## Key concepts

### Embedded vs server databases

Neo4j is a **server**: a separate process (here a Docker container) that many clients
talk to over the network (Bolt). It handles concurrency (many sessions, transactions,
locks), scale (data bigger than memory, indexes on disk), access control, and
operations like `neo4j-admin dump`. The price is deployment: something must run it.

Relate is **embedded**: a library in your own process, like SQLite. `relate.connect(":memory:")`
in Python or `new RelateDb()` in the browser *is* the database. What it gives up and gains
in this project:

- **Gains:** no deployment at all (a static host can serve it), start-up in milliseconds
  (the WebAssembly initialises in about 50 ms in Node, `init_ms` in
  `tests/js/graph_check.mjs`), no network round trip per query (a path query takes 8 ms).
- **Gives up:** one connection, one writer, one thread. There is nothing to share: every
  visitor gets their *own copy*, in memory, thrown away with the tab. That is exactly why
  it fits here, a read-only snapshot of 1,761 nodes. It would not fit the live pipeline,
  where the extractor, the analyst agent, the API and the MCP server write and read the
  same graph at once (and that stays on Neo4j: replacing it is out of scope).

Loading is a write workload and shows the embedded trade-offs. `build_load()` emits
`UNWIND $rows AS row CREATE (n:Entity) SET n = row` per label, sent in batches of 500.
Relationships need both ends found: `MATCH (a:Entity {_id: row.s}) MATCH (b:Event {_id: row.e})`.
Without an index on `_id` that is a label scan per row (the feasibility check measured
20 s); with `CREATE INDEX load_entity_id FOR (n:Entity) ON (n._id)` it is a lookup, and the
whole load takes about a second. The ids are compact integers (0..1760) instead of
Neo4j's `elementId` strings, which also cut `graph.json` from 1.6 to 1.1 MB.
`cleanup` then removes `_id` and its indexes, so the graph the visitor queries has exactly
Neo4j's properties.

The schema statements are WikiPulse's own, unchanged: `wikipulse.graph.store.SCHEMA`
(uniqueness constraints, range indexes) and `wikipulse.qa.index.FULLTEXT_INDEXES` (the
two full-text indexes with the `standard-folding` analyzer). Relate accepted all eight
verbatim, and `SHOW INDEXES` lists the same names as Neo4j's manifest, minus the vector
indexes.

### WebAssembly in the browser: loading, CSP, worker

WebAssembly (`.wasm`) is a compact binary instruction format browsers compile to machine
code. Relate's Rust source is compiled to it once by its authors; `relate.js` is the
JavaScript "glue" generated by `wasm-bindgen`: `await init()` fetches `relate_bg.wasm`
next to it (`new URL('relate_bg.wasm', import.meta.url)`), compiles and instantiates it,
and `RelateDb` wraps the exported functions. `db.execute(query, paramsJson)` returns a
JSON string `{columns, rows, error, ...}`; engine errors come back in `error`, they don't
throw.

**The Content-Security-Policy.** The page's policy is `script-src 'self'`: only scripts
from this site. Compiling WebAssembly counts as code generation, which CSP blocks unless
the policy says `'wasm-unsafe-eval'`. That keyword allows *only* WebAssembly compilation,
not JavaScript's `eval()` (that would be `'unsafe-eval'`). It is the only change to the
policy; `tests/test_demo_site.py` checks that `script-src` is exactly
`'self' 'wasm-unsafe-eval'` and nothing else says "unsafe". Workers have no `worker-src`,
so they fall back to `script-src 'self'`: only our own worker file can run. The `.wasm`
file is fetched from this site (`connect-src 'self'`), never from a CDN.

**Why a worker.** WebAssembly has no statement timeout and no cancel: a query like
`MATCH (a:Entity), (b:Entity), (c:Entity) RETURN count(*)` (2 billion combinations) would
freeze the page. In `graph-worker.js` it freezes only the worker. `GraphClient.send()` in
`explore.js` starts a 5 s timer with each message; if no answer arrives, it calls
`worker.terminate()` (the browser kills the thread, whatever it is doing), answers the
query with "Stopped after 5 s", creates a new worker and reloads the graph (about 1.2 s).
A Rust panic is handled the same way: `trap.js`'s `isWasmTrap()` recognises a dead
instance. `GraphClient.run()` sends one query at a time, so the timer measures that
query, not the queue in front of it.

**Loading on demand.** `index.html` loads `explore.js`, `graph-db.js` and
`cypher-check.js` (40 KB of plain JavaScript). Nothing else happens until the visitor
presses "Start the graph database": only then does `GraphClient.start()` create the
worker, which loads `relate.js`, the `.wasm` and `graph.json`. The headless browser check
confirmed it: before the button, the network shows the page's own files and `day.json`,
no `.wasm`, no `graph.json`.

### Supply-chain hygiene for a binary dependency

A 7.1 MB binary we didn't build runs in our visitors' browsers. `scripts/vendor_relate.py`
treats it as untrusted until proven otherwise:

- **Pinning.** `VERSION = "1.1.0"` and the exact tarball URL. No "latest", no version range.
- **Integrity hash.** `INTEGRITY` is the registry's own `dist.integrity` for that
  version, a Subresource-Integrity string (`sha512-` + base64 of the SHA-512). The script
  hashes what it downloaded and compares (`verify_tarball()`); a different byte anywhere
  fails it, so a compromised registry, mirror or network can't slip in another file. The
  value is committed, so changing it is a reviewed change in git.
- **Per-file hashes.** Each extracted file's SHA-256 is pinned too (`FILES`). That also
  lets `--check` (and the test `test_the_vendored_files_stay_under_their_own_limit`) verify
  an existing copy without the network.
- **Extract by name.** Only four named members are read, and written to paths from
  `FILES`, never to the tarball's own paths (a crafted archive with `../../evil.js` writes
  nothing; `test_only_named_files_are_written_and_each_is_checked`).
- **Not in git.** `site/vendor/` is in `.gitignore`. The repository stays small and the
  binary's provenance is the registry plus the hash, not "someone committed it".
- **In CI.** `pages.yml` runs the script before uploading; a mismatch exits 1 and nothing
  is deployed. Then Node runs the page's database code on exactly those files
  (`tests/js/graph_check.mjs`).
- **Licence.** Apache-2.0 requires giving recipients a copy of the licence. The npm
  package declares Apache-2.0 but contains no LICENSE file, so the text is committed
  (`scripts/vendor/relate-wasm-LICENSE.txt`, "Copyright 2026 The Relate Authors", from the
  same project's `relate-db` wheel) and copied next to the files; the footer links it.

`@relate/relate-wasm` is therefore *not* an npm dependency: the project has no
`package.json`, no `node_modules`, no build step (a test enforces that). `relate-db` (the
same engine for Python) *is* a new **dev** dependency, pinned `==1.1.0` to match the
browser build, used only by the integration test. The `wikipulse` package never imports
it.

### Proving two engines agree: differential testing

The claim "the page gives the answers WikiPulse's retrieval gets from Neo4j" is tested by
running the same queries on both engines over the same data and comparing results
(differential testing). `tests/test_demo_graph_neo4j.py`:

1. loads the committed `graph.json` into Relate with the file's own statements (as the
   page does), and re-exports the snapshot to prove the file is current;
2. runs all 18 shipped queries with 78 parameter sets taken from the snapshot (the 12
   most connected entities, the 12 biggest events, Santiago Peña and Ueno Bank, ...) and
   the 25 prepared questions on both;
3. compares with `export_graph.compare_rows()`.

Both engines' values first go through `canonical()`: nodes become `{labels, properties}`,
datetimes UTC ISO text with six fraction digits (Neo4j writes
`2026-11-05T16:00:36.573000000+00:00`, Relate `2026-11-05T16:00:36.573Z`: the same
instant), integral floats integers (the WebAssembly JSON writes `1.0` as `1`).

Why "identical" is the wrong test: a query's result is often *a set*, not a sequence.
Without `ORDER BY`, the order is whatever the engine's storage and plan produce. Even with
`ORDER BY page_count DESC LIMIT 25`, if the 25th to 40th events all have 1 page, *which*
of them make the cut is unspecified, and both engines are right. So the comparison has
levels:

| Verdict | Meaning | Example |
|---------|---------|---------|
| identical | same rows, same order | most queries |
| other order | same multiset of rows | q24 (no `ORDER BY`) |
| list order | same rows once lists inside them are sorted | q16 (`collect(DISTINCT ...)` without an order), `EVENTS` (pages ordered by `po.score`, with ties) |
| tie | rows agree except those tied with the last row on a sorted column | q01, q18 (`pages`), q19 (`story_count`) |
| limit tie | (shipped queries) both limited results are subsets of the same full result | `ENTITY_RELATIONS` (sorted by `confidence`, not returned), `PATHS` limit 3 |
| different | anything else: a failure, unless listed | |

The "tie" check (`tie_column()`, and `tieColumn()` in the browser) needs no knowledge of
the query: it looks for a returned column that is sorted in both results, whose last
value is the same, with at least two rows at that value, and checks that everything
*above* the cut-off agrees. For shipped queries the stronger "limit tie" check runs the
query again with `limit: 10000` on both engines, requires the full results to agree, and
both limited results to come from them.

Approximate vector search would be a second reason: Neo4j's vector index is approximate
(HNSW, quantised), Relate's search exact, so scores differ in the third decimal and
near-equal hits swap places. The vector queries aren't shipped (no embeddings, no model
in the browser), so that question doesn't arise here.

## Results

**Neo4j vs Relate** (`uv run pytest -m integration tests/test_demo_graph_neo4j.py`):

- 78 shipped query runs: 49 identical, 22 other order, 2 list order, 4 limit tie, and
  **1 known difference**.
- 25 prepared questions: 20 identical, q24 other order, q16 list order, q01, q18 and q19
  tie at the LIMIT. All 25 also give exactly the row count the 12a run recorded.
- The known difference, checked by `test_the_known_difference_is_only_the_tokenizer`:
  searching `california OR governor`, Relate also finds "California's death row".
  Lucene's standard tokenizer keeps "california's" as one token, so in Neo4j the word
  "california" doesn't match it; Relate splits at the apostrophe. Relate finds a superset.
- The one incompatibility from the feasibility check stays the only one:
  `db.index.fulltext.queryNodes` takes 2 arguments in Relate. `relate_fulltext()` turns
  `CALL db.index.fulltext.queryNodes('qa_entity_names', $query, {limit: $limit}) YIELD node RETURN ...`
  into `... queryNodes('qa_entity_names', $query) YIELD node RETURN ... LIMIT $limit`;
  both engines yield the best hits first, so the results agree (all 10 full-text runs
  were identical or other order except the tokenizer case).

**In the browser** (headless Chrome and Edge, cold, cache disabled, served by the
`demo-site` preview on localhost; see "How it was checked" below):

| Measure | Result |
|---------|--------|
| Graph section usable (button to "Ready") | **1.1 to 1.6 s** over 4 runs (Chrome 1.1, 1.3, 1.6 s; Edge 1.3 s); reload after a stopped query 1.1 to 1.3 s |
| Load alone, in Node with the same WebAssembly | 0.8 to 1.3 s (`load_ms` of `graph_check.mjs`) |
| Downloaded by the graph section | **8.31 MB**: `relate_bg.wasm` 7.13 MB, `graph.json` 1.15 MB, JavaScript 40 KB. Compressed with gzip it would be 2.77 MB, if the host compresses (GitHub Pages compresses text; whether it compresses `application/wasm` I could not check without deploying). |
| Downloaded by the page before the button | 756 KB (as before plus 40 KB of new JavaScript) |
| Queries | search and entity view a few ms each, the Peña to Ueno Bank path 8 ms, a prepared question about 20 ms, "busiest entities" 50 to 70 ms |
| Committed `site/` | 1.91 MB (cap 3 MB); vendored files 7.17 MB (own limit 8 MB) |

On a laptop with a fast connection the 8.3 MB add their transfer time to that (about
1.3 s at 50 Mbit/s uncompressed), which keeps the section usable within 3 s.

**How it was checked.** No interactive browser pane was available to me, so a throwaway
script outside the repository drove headless Chrome and Edge over the DevTools protocol
(with `websockets`, already a project dependency; nothing installed). It opened the
`demo-site` preview, pressed the button, and checked: "pena" finds Santiago Peña;
Santiago Peña → Ueno Bank shows three paths (all via Paraguay; Neo4j's first three go via
the shared events, the "limit tie" above: both engines have the same five shortest
paths); "Run all and compare" reports 25 of 25 matching (20 identical, 2 same rows in
another order, 3 ties at the LIMIT); the "A write" example is refused ("not a read-only
query (uses SET)"); "A slow one" is stopped after 5 s (6.3 s including the reload) and
the next query works; no console errors, no request to another origin; at 375 px width
the page is exactly 375 px wide (no horizontal scrolling).

## Decisions and deviations

- **The load statements travel in `graph.json`.** Instead of the browser knowing how to
  build the graph, the file carries the Cypher (`load`, `cleanup`) and the rows. The
  browser (`GraphDb.load()`) and the integration test run the *same* statements, so the
  test loads exactly what visitors get.
- **Datetimes are converted while they are created** (`CREATE (n:Event) SET n = row SET
  n.started_at = datetime(row.started_at), ...`), not in a second pass: one statement per
  label, the same result. `datetimes` in the file lists them, as the spec asks. A
  datetime with sub-microsecond digits would stop the export (`to_plain_property()`);
  none has any.
- **Time parameters.** JSON can't carry datetimes, so `$now`, `$today_start` and
  `$week_start` travel as ISO text and every use becomes `datetime($now)` (`bindTimes()`
  / `bind_times()`, which skip strings and comments). Visitors can use them too.
- **The prepared questions' Neo4j rows are re-run, not read.** The 12a answers file
  stores `cypher_rows` as a *count*, not the rows. The export runs each stored query on
  the snapshot with the 12a run's time parameters; every count matches what 12a
  recorded, and the integration test checks the stored rows against a fresh run.
- **All 18 non-vector queries are shipped**, though the page uses six (`SEARCH_ENTITIES`,
  `ENTITIES_BY_KEY`, `ENTITY_EVENTS`, `ENTITY_RELATIONS`, `EVENT_ENTITIES`, `PATHS`): the
  comparison covers the whole retrieval layer, and they cost 6 KB. `PATHS` is shipped with
  `max_hops` 4, the default of `QAGraph.paths()`. "The people, organisations and places it
  connects to" are the entities sharing its events, grouped by their involvement type
  (`EVENT_ENTITIES`), so no new query was written.
- **Row caps:** 25 for prepared questions (as in the evaluation), 100 for the Cypher box,
  200 for shipped queries, whatever their `$limit`.
- **Validator cases** were copied from `tests/qa/test_cypher.py` into
  `tests/fixtures/cypher_cases.json` (plus a few: Unicode around words, a write after a
  UNION, `allShortestPaths` without a bound); the existing tests stay as they are.
  The JavaScript port uses `\p{L}\p{N}_` for Python's Unicode `\w`.
- **Node runs the JavaScript tests.** It is on every GitHub runner and needs no project
  dependency. Locally the tests skip without Node; on CI a missing Node fails. The site's
  modules are copied to a temporary folder with `{"type": "module"}` (`tests/js/stage.mjs`)
  so any Node version loads them, while `site/` keeps no `package.json`.
- **The npm package has no LICENSE**; see above. The script also extracts `trap.js`
  (`isWasmTrap`) and the OPFS snippet, which `relate.js` imports even when OPFS isn't used.
- **The deploy job runs only on `main`.** Before this change, a `workflow_dispatch` on a
  branch would have tried to deploy; the `github-pages` environment only accepts `main`,
  so it would have failed rather than published (checked read-only with
  `gh api .../environments/github-pages/deployment-branch-policies`). With
  `if: github.ref == 'refs/heads/main'` a branch run vendors, checks and uploads, skips
  the deploy and succeeds. `pages.yml` also triggers on changes to
  `scripts/vendor_relate.py`, so a version bump redeploys.
- **Not done here:** the branch run of `pages.yml` and the deploy. They need a push, which
  the coordinator does after the owner approves.

## Questions to test your understanding

1. The page refuses `SET` with the validator, but the database is an in-memory copy in
   the visitor's tab. What would actually happen if a write got through, and why does
   the page still refuse it?
2. Why can't the page simply cancel a slow query, and what does terminating the worker
   cost?
3. q18 returns different events from Neo4j and from Relate. Why is that not a bug, and
   how does the test tell it apart from one?

<details>
<summary>Answers</summary>

1. It would change only that visitor's copy of the graph, in their own tab, until they
   reload: no one else is affected, and nothing is sent anywhere. The page still refuses
   writes with WikiPulse's own validator (`cypher-check.js`, the port of
   `validate_cypher()`), so it behaves like WikiPulse's text-to-Cypher and never shows a
   graph the visitor has changed while it claims to be the 5 November snapshot. The
   shared cases file makes sure the port refuses exactly what the Python validator does.

2. WebAssembly runs a function to completion: the engine has no timeout and the
   JavaScript that called it can't interrupt it (the thread is busy). Only the browser
   can stop the whole thread, with `worker.terminate()`. That throws away the database
   too, so the page starts a new worker and reloads the graph, about a second
   (`GraphClient.run()`).

3. q18 asks for the 25 biggest events by `page_count`. Many events have 1 page, so the
   last places tie; the query doesn't say which tied events to keep, and each engine
   keeps different ones. `tie_column()` finds a returned column (`pages`) sorted in both
   results with the same last value, and checks that every row *above* that value is
   identical in both. A real bug (a wrong row above the cut-off, a different count) fails
   that check.

</details>
