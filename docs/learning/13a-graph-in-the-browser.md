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
| `scripts/export_graph.py` | Reads the snapshot and writes `site/data/graph.json` (1.2 MB): the statements that rebuild the graph (`build_load()`), the 18 non-vector queries of `wikipulse/qa/graph.py` (`shipped_queries()`, with `relate_fulltext()`), the graph's clock (`time_parameters()`), and the 25 questions of the 12a run with the LLM's Cypher, Neo4j's rows (`canonical()` form) and the stored answer (`build_prepared()`). Also the comparison rules (`compare_rows()`, `unordered()`), the tie proof (`order_keys()`, `with_final_limit()`, `tie_at_limit()`, `tie_evidence()`) and `bind_times()`. |
| `scripts/vendor_relate.py` | Downloads the pinned npm tarball, checks its integrity hash and each file's SHA-256, extracts four files plus the licence into `site/vendor/relate/`. Standard library only. |
| `scripts/vendor/relate-wasm-LICENSE.txt` | The Apache-2.0 text (the npm package ships none; copied from the same project's `relate-db` wheel). |
| `site/cypher-check.js` | A line-by-line JavaScript port of `validate_cypher()` that reads Unicode text the way Python's regular expressions do, plus `bindTimes()` and `luceneTerms()`. |
| `site/graph-db.js` | `GraphDb`: loads `graph.json` into a `RelateDb`, runs named queries (`runNamed()`, datetime parameters as ISO text), visitor Cypher (validated, row-capped) and prepared questions with their comparison (`runPrepared()`); `canonical()`, `compareRows()` and `tieAtLimit()` (the same rules as the Python ones). No DOM, so Node can run it. |
| `site/graph-worker.js` | The module Web Worker: `init()` the WebAssembly, fetch `graph.json`, load, answer messages. |
| `site/explore.js` | The section's UI and `GraphClient`: one query at a time, a 5 s limit per query, terminate-and-reload when it runs out (and a clear error if that reload fails). |
| `site/index.html`, `style.css` | The "Explore the graph" section; the CSP gains `'wasm-unsafe-eval'`; result tables become one block per row on phones. |
| `.github/workflows/pages.yml` | Vendors and verifies Relate, runs the Node checks on a pinned Node (24.21.0), then uploads; the deploy job runs only on `main`. |
| Tests | `tests/test_demo_graph.py` (21: schema, privacy, the rewrite, the shipped queries are the constants, the bounds and tie evidence, comparison and tie rules), `tests/qa/test_cypher_cases.py` (148: the shared validator cases in Python), `tests/test_demo_graph_relate.py` (13: each refused GQL write really writes in Relate), `tests/test_demo_graph_js.py` (2: the same cases in JavaScript; the page's database with the real WebAssembly), `tests/test_vendor_relate.py` (6), `tests/test_demo_graph_neo4j.py` (4, integration: Neo4j vs Relate), updates in `tests/test_demo_site.py` and `tests/test_cicd.py`. |
| Fixtures | `tests/fixtures/cypher_cases.json` (147 validator cases, 5 time-binding cases), `tests/js/*.mjs` (the Node runners). |

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
so the *page's* policy falls back to `script-src 'self'` to decide which worker file may
start: only our own. But the page's `<meta>` policy does **not** govern what happens
*inside* the worker. A dedicated worker loaded from a URL gets its policy from the HTTP
headers of its own response, and GitHub Pages sends no CSP header, so inside
`graph-worker.js` there is no `connect-src 'self'`: it could fetch any origin. Today
nothing a visitor types can make it do so (`LOAD CSV` and `CALL` are refused, and the
worker's own code fetches only `data/graph.json` and the `.wasm` from this site, which the
headless check confirmed: no request to another origin). A host that can set headers
would let us give the worker its own policy; Pages can't. The `.wasm` file is fetched
from this site, never from a CDN.

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
  Its SHA-256 is pinned too (`LICENSE_SHA256`), so `--check` fails on a changed licence,
  as on any other changed file. The `.wasm` also contains third-party Rust crates
  compiled in. The `relate-db` wheel ships a software bill of materials (SBOM) listing
  them, but the npm package ships no notices for them, so there was nothing more to copy.
  If the human publishes the demo widely, linking that SBOM is the honest next step.

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
3. compares with `export_graph.compare_rows()`, and proves every tie with `prove_tie()`
   (both engines, keyed queries, with and without the LIMIT).

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
| tie | only rows tied at the LIMIT's cut-off differ, **proved** with the ORDER BY keys and the full results | q01, q18 (`ev.page_count`), q19 (`story_count`), `ENTITY_RELATIONS` (`r.confidence`, not returned), `PATHS` (no ORDER BY: every path ties) |
| different | anything else: a failure, unless listed | |

**How a tie is proved.** The first version guessed: it looked for any returned column
that was sorted in both results and checked the rows above its last value. The review
showed why that is too weak: a column that happens to be constant (`['x','a'],['x','b']`
against `['x','c'],['x','d']`) is "sorted", every row ties on it, and two completely
different results passed as a tie. A tie is a claim about the query's **ORDER BY key**,
not about whichever column looks sorted, and about rows the LIMIT **cut off**, which the
limited result alone can't show. So now:

1. `compare_rows()` / `compareRows()` never say "tie". They stop at "different".
2. `order_keys()` rewrites the query so that it also returns its final ORDER BY keys as
   extra columns: `RETURN ev.key AS k, ev.page_count AS pages ORDER BY pages DESC LIMIT 25`
   becomes `RETURN ev.key AS k, ev.page_count AS pages, ev.page_count AS _order_key0
   ORDER BY pages DESC LIMIT 25` (an alias is replaced by its expression, because RETURN
   can't see its own aliases). Without an ORDER BY no key is added: then every row ties,
   which is exactly what an unordered LIMIT means. When the order can't be read (a
   UNION, or an ORDER BY before the final RETURN, like q05's) there is no proof, so no
   tie.
3. That keyed query runs on **both engines** twice: with its own LIMIT, and with a much
   larger one (`with_final_limit(..., 10000)` for prepared questions, `limit: 10000` for
   shipped ones).
4. `tie_at_limit()` / `tieAtLimit()` accept the difference only if: the LIMIT really cut
   the result; both full results have the same keys (and, in the integration test, the
   same rows); both limited results are rows of the full result with the same key
   sequence; the rows above the last row's key are the same in both and exactly as many,
   with the same keys, as the full result ranks above it; and more rows share that last
   key in the full result than the LIMIT kept, so there was a real choice to make.

The browser has only one engine, so `graph.json` carries Neo4j's half of the evidence
for the six prepared questions whose LIMIT cut Neo4j's result (`tie` in each question:
the keyed query, its wide version, Neo4j's keyed rows and the keys of Neo4j's full
result, about 55 KB). `GraphDb.runPrepared()` runs the question in the worker, and when
the rows differ it runs the keyed query with and without the LIMIT and calls
`tieAtLimit()`. The integration test checks that this evidence is still Neo4j's current
answer, and that every tie it proves is one the browser can prove too.

Approximate vector search would be a second reason: Neo4j's vector index is approximate
(HNSW, quantised), Relate's search exact, so scores differ in the third decimal and
near-equal hits swap places. The vector queries aren't shipped (no embeddings, no model
in the browser), so that question doesn't arise here.

## Results

**Neo4j vs Relate** (`uv run pytest -m integration tests/test_demo_graph_neo4j.py`):

- 78 shipped query runs: 49 identical, 22 other order, 2 list order, 4 proved ties, and
  **1 known difference**. (Re-run after the review with the strict tie proof: the same
  four, all proved.)
- 25 prepared questions: 20 identical, q24 other order, q16 list order, q01, q18 and q19
  proved ties at the LIMIT (on both engines; in Node and in the browser too). All 25 also give exactly the row count the 12a run recorded.
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
| Downloaded by the graph section | **8.39 MB**: `relate_bg.wasm` 7.13 MB, `graph.json` 1.21 MB (1.15 MB before the tie evidence), JavaScript 49 KB. Compressed with gzip it would be 2.77 MB, if the host compresses (GitHub Pages compresses text; whether it compresses `application/wasm` I could not check without deploying). |
| Downloaded by the page before the button | 756 KB (as before plus 40 KB of new JavaScript) |
| Queries | search and entity view a few ms each, the Peña to Ueno Bank path 8 ms, a prepared question about 20 ms, "busiest entities" 50 to 70 ms |
| Committed `site/` | 1.97 MB (cap 3 MB); vendored files 7.17 MB (own limit 8 MB) |

On a laptop with a fast connection the 8.3 MB add their transfer time to that (about
1.3 s at 50 Mbit/s uncompressed), which keeps the section usable within 3 s.

**After the review fixes** (headless Chrome, cold, the same way): ready in 1.1 s; "Run all
and compare" reports 25 of 25 (20 identical, 2 same rows in another order, 3 proved
ties); the review's `INSERT` and a `NEXT ... INSERT` are refused and nothing was
inserted; "A slow one" stops after 5 s (6.2 s with the reload) and the next query
works; with `Worker` replaced by one that can't start, a slow query ends with "could not
restart: reload the page", the status says so, and the next query answers at once with
that error; no console errors, no request to another origin.

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

## Changes after the review

The independent review (`docs/reviews/13a-graph-in-the-browser.md`) found one high and two
medium problems, and several low ones. What changed:

- **GQL writes (high).** Relate speaks GQL, the ISO graph query language, as well as
  Cypher, and GQL has its own write and chaining words. `MATCH (n) INSERT (:X) RETURN n`
  passed both validators and wrote to the visitor's graph. I ran candidate keywords
  unchecked in `relate-db` 1.1.0, one by one, on a small graph:
  - `INSERT` writes (alone, after `WITH`, `FOR`, `LET`, in `OPTIONAL CALL { }`, after a
    `UNION`): **refused**.
  - `NEXT` chains a second statement onto the first (`... RETURN a NEXT INSERT ...`, or
    `NEXT MATCH (m) DETACH DELETE m`): **refused**, like `;`.
  - `NODETACH DELETE` deletes (DELETE was already refused): **refused** too, for clarity.
  - `LET`, `FILTER`, `FOR` and `FINISH` only read: **allowed**. Relate rejects `UPSERT`,
    `REPLACE`, `COMMIT`, `ROLLBACK`, `SELECT`, `CLEAR`, `TRUNCATE`, `COPY`, `ATTACH`,
    `IMPORT`, `EXPORT`, `SESSION` and `BEGIN` as syntax errors, and writes inside
    `COUNT { }`, `EXISTS { }` and `COLLECT { }` ("writes are not allowed in subquery
    expressions").

  The change is in **both** validators, so the server's text-to-Cypher refuses these
  words too. On the server, Neo4j's READ transaction would have stopped such a write
  anyway; in the browser the validator is the only layer. None of the 75 stored
  LLM-written queries uses these words, and every test in `tests/qa` still passes. The
  shared fixture marks the 12 write cases `relate_writes`, and
  `tests/test_demo_graph_relate.py` runs each one *unchecked* in Relate and checks that
  the graph changed, so every refusal guards against a real write. One fixture case had
  to target an unconnected node: `NODETACH DELETE` correctly refuses to delete a node
  that still has relationships.
- **Ties (medium).** Proved, not guessed: see "How a tie is proved" above. The unit test
  that expected the old rule to call a tie from the rows alone now expects "different"
  for those rows, and new tests cover `tie_at_limit()` (the review's constant-column
  example included) and `order_keys()`.
- **Unicode in the JavaScript port (medium).** Python's `re` is Unicode-aware: `\w`,
  `\d`, `\s` and `\b` cover every script, `IGNORECASE` lets "ı" and "İ" match "i", "ſ"
  match "s" and the Kelvin sign "K" match "k", `int()` reads "７" or "٣" as digits,
  `str.strip()` knows other spaces than JavaScript's `trim()`, and `len()` counts code
  points, not UTF-16 units. `cypher-check.js` now spells all of that out (`WORD_CHAR`,
  `DIGIT`, `SPACE`, `keyword()`, `digitsValue()`, `strip()`, `codePoints()`). I compared
  the character classes over all 1.1 million code points: they are identical for every
  character Python 3.12 knows (Unicode 15.0). Node 24 knows Unicode 17, so only
  characters added since then can still be read differently. 50 non-ASCII cases, with
  the Python validator's output as the expected value, joined the shared fixture; the old
  port failed 26 of them. Along the way the Python validator turned out to **crash** on
  `LIMIT ²` (`isdigit()` accepts "²", `int()` doesn't); it now uses `isdecimal()`.
- **Low ones:** `MAX_HOPS` and `MAX_ROWS` are read from `RetrievalConfig.max_hops` and the
  `qa_cypher_max_rows` default instead of copied; `canonical()` keeps only the exported
  properties of a whole node or relationship in the prepared rows; `runNamed()` wraps
  `$start`, `$end` and `$since` in `datetime()` (`EVENTS_BETWEEN` returned 0 rows with ISO
  text, now 15); a failed reload no longer leaves the page on "Running..."; `pages.yml`
  pins Node and also runs when the licence, `tests/js/` or the shared cases change; the
  licence text is checked by its hash; the CSP paragraph above now says plainly that the
  worker isn't covered by the page's policy.
- **Not changed:** the review's problem 8 (`relate_fulltext()` assumes nothing filters
  between `YIELD` and `RETURN`) was not in the list of fixes I was given. The branch run
  of `pages.yml` still needs a push, which the coordinator does.

## Questions to test your understanding

1. The page refuses `SET` with the validator, but the database is an in-memory copy in
   the visitor's tab. What would actually happen if a write got through, and why does
   the page still refuse it?
2. Why can't the page simply cancel a slow query, and what does terminating the worker
   cost?
3. q18 returns different events from Neo4j and from Relate. Why is that not a bug, and
   how do the test and the page prove it rather than assume it?

<details>
<summary>Answers</summary>

1. It would change only that visitor's copy of the graph, in their own tab, until they
   reload: no one else is affected, and nothing is sent anywhere. The page still refuses
   writes with WikiPulse's own validator (`cypher-check.js`, the port of
   `validate_cypher()`, which also knows GQL's `INSERT` and `NEXT`, since Relate runs
   both), so it behaves like WikiPulse's text-to-Cypher and never shows a
   graph the visitor has changed while it claims to be the 5 November snapshot. The
   shared cases file makes sure the port refuses exactly what the Python validator does.

2. WebAssembly runs a function to completion: the engine has no timeout and the
   JavaScript that called it can't interrupt it (the thread is busy). Only the browser
   can stop the whole thread, with `worker.terminate()`. That throws away the database
   too, so the page starts a new worker and reloads the graph, about a second
   (`GraphClient.run()`).

3. q18 asks for the 25 biggest events by `page_count`. Many events have 1 page, so the
   last places tie; the query doesn't say which tied events to keep, and each engine
   keeps different ones. `order_keys()` makes the query also return its ORDER BY key
   (`ev.page_count AS _order_key0`), and it runs with and without the LIMIT on both
   engines. `tie_at_limit()` then requires the full results to agree, both limited
   results to be rows of it with the same keys, the rows above the last key to match,
   and more 1-page events in the full result than the LIMIT kept. A real bug (a wrong
   row above the cut-off, a row that isn't in the answer at all) fails it, and so does a
   column that only *looks* sorted, because the key comes from the ORDER BY.

</details>
