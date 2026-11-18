# Review: 13a The knowledge graph, live in the browser

Branch `spec-13a-graph-in-the-browser`, 7 commits on top of `main` (29dc8ba..6194c2e).
Reviewed by an independent agent on 18 November 2026. Nothing was pushed and no workflow
was started. No LLM was called and nothing was requested from Wikimedia. The npm registry
was contacted once, to re-download the pinned tarball and check its hash.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| `uv run pytest` passes | pass | 1555 passed, 1 skipped, 85 deselected (5 min 9 s). |
| `ruff check` / `ruff format --check` pass | pass | "All checks passed!"; "510 files already formatted". |
| `uv run pytest -m integration` passes the Neo4j-vs-Relate comparison | pass | `tests/test_demo_graph_neo4j.py`: 4 passed. 78 shipped-query runs: 49 identical, 22 in another order, 2 with lists in another order, 4 tied at the LIMIT, 1 different (the known tokenizer case). Of the 25 prepared questions, 20 are identical, 1 has lists in another order, 1 has rows in another order and 3 are ties (q01, q18, q19). I checked q18 and q19 row by row: both are real ties on an `ORDER BY ... DESC LIMIT 25` column. The full `-m integration` suite: 81 passed, 1 skipped (8 min 37 s). |
| Vendoring script works and checks integrity | pass | `python scripts/vendor_relate.py` says "Already vendored and verified", 7.2 MB. I also downloaded the tarball again into the scratchpad: `verify_tarball()` passed on the registry's bytes. Its `package.json` says `"license": "Apache-2.0"`. |
| Node check of the page's code | pass | `node tests/js/graph_check.mjs site`: load 1337 ms, 1761 nodes and 2534 relationships, every prepared question identical, same rows or tie, `failures: []`. `cypher_cases.mjs`: 84 passed, 0 failed. |
| Browser: "pena" finds Santiago Peña | pass | I drove headless Edge over CDP against `site/` served on localhost, with the cache off. First hit: "Santiago Peña person". |
| Browser: Santiago Peña → Ueno Bank shows a path | pass | "Santiago Peña → Paraguay → Ueno Bank". |
| Browser: prepared questions' live rows match or are explained | pass | "25 of 25 match Neo4j's rows: 20 identical, 2 the same rows in another order, 3 differ only among rows tied at the LIMIT". |
| Browser: a write query is refused | **partial** | The `SET` example is refused. But `MATCH (n:Entity {name: 'Ueno Bank'}) INSERT (:Entity {name: 'Injected by visitor'}) RETURN n.name` is accepted and runs, and a later query returns the injected node (problem 1). |
| Browser: a slow query is stopped by the time limit | pass | "A slow one" timed out after 6.8 s including the reload ("Stopped after 5 s ..."). Status became "Graph reloaded in 1.6 s", and the next query worked. |
| Load time and download size recorded in the learning note | pass | Learning note, section "Results": 1.1 to 1.6 s, 8.31 MB. I measured 1.9 s to "Ready" (2.2 s wall clock), which is under the 3 s target. |
| Pages workflow succeeds on a branch run (`workflow_dispatch`) | **not done** | `gh run list --workflow pages.yml` shows only the 15 November push run on `main`. The branch has no remote. The learning note says so ("Not done here"). This needs the human. |
| No third-party requests, no console errors, phone width | pass | CDP: no request to any origin other than localhost, no console errors, no exceptions. `scrollWidth` is 375 at 375 px width. |
| XSS | pass | `RETURN '<img src=x onerror=alert(1)>' AS \`<b>x</b>\`` is rendered as text, and no `img` or `b` element appears. Every value goes through `el()` / `append(String)`. Links are only made for Wikipedia and Wikidata URLs. |
| Privacy of `graph.json` | pass | Every key is on the allowlist. There are no `embedding*` keys, no IPv4 addresses, no e-mail addresses, and no `user` or `comment` keys. No prepared row contains a whole node. Neo4j's extra properties are only `embedding`, `embedding_hash` and `embedding_model`, and all are dropped. |
| Nothing under `data/`, no wasm, no personal e-mail committed | pass | The diff has no `data/`, `.wasm` or `site/vendor/` paths, and no e-mail address other than `noreply`. `.gitignore` has `site/vendor/`. |

## Problems

1. **high**, `site/cypher-check.js:20` (and `wikipulse/qa/cypher.py:42`): `FORBIDDEN_WORDS` does not contain the GQL write clause `INSERT` (nor the chaining keyword `NEXT`). Relate 1.1.0 runs both, so `MATCH (n) INSERT (:X) RETURN n` passes both validators and writes to the visitor's graph, which breaks Requirement 3 ("the validator still refuses writes") and the page's "no writes" promise. I reproduced it in `relate-db`, in Node through `GraphDb.runCypher`, and in the browser. On the server, Neo4j's READ transaction still stops it, so the Python side only needs the fix for parity. The fix is to forbid `INSERT` (and `NEXT`, or treat it as a second statement) and add both to `tests/fixtures/cypher_cases.json`.
2. **medium**, `scripts/export_graph.py:259` (`tie_column`) and `site/graph-db.js:166` (`tieColumn`): the "tie" verdict accepts any two results of the same length with a column that is constant across all rows. That column is "monotonic" and every row ties with the last one, so nothing is left to compare. For example, `compare_rows([['x','a'],['x','b']], [['x','c'],['x','d']])` returns `"tie"`. Neither function checks that the column is the query's sort key or that the sort direction matches. For prepared questions, `tests/test_demo_graph_neo4j.py:224` accepts "tie" without the full-result check that `compare_query` runs for shipped queries. A future question that returns, say, `n.type` for "which people..." could therefore hide a real difference. Today's three ties are genuine.
3. **medium**, `site/cypher-check.js:27-34`: the port is not exact for non-ASCII text. JavaScript's `\b` and `\d` are ASCII-only, while Python's are Unicode, and 5 of my 114 differential queries got different results. `MATCH (n) RETURN n.name AS éUNION` becomes `... AS é\nLIMIT 25UNION\nLIMIT 25` in JS. `LIMIT ５` / `LIMIT 1٠` are kept by Python but replaced by JS. `n.éLIMIT` is rewritten differently. None of these is a safety bypass: Relate rejects those characters, and results are sliced to `maxRows` anyway. But the shared fixture claims the two "refuse and rewrite exactly alike" and has no case that would catch this.
4. **low**, `site/explore.js:132-151`: if the reload after a timeout or trap fails (`this.start()` throws), `run()` rejects. `runOwn`/`runPrepared` don't catch that, so the result box stays on "Running..." and the status stays on "Reloading the graph..." with an unhandled rejection.
5. **low**, `docs/learning/13a-graph-in-the-browser.md:89-92`: the note suggests the page's meta CSP also governs the worker. A dedicated worker loaded from a URL gets its policy from its own response headers, and GitHub Pages sends none, so inside the worker `connect-src 'self'` does not apply. No visitor-controlled network sink exists today (`LOAD` and `CALL` are refused), but the learning note should say this plainly.
6. **low**, `scripts/export_graph.py:63-64`: `MAX_HOPS = 4` and `MAX_ROWS = 25` copy `RetrieverConfig.max_hops` (`wikipulse/qa/retrieve.py:96`) and `Settings.qa_cypher_max_rows` (`wikipulse/config.py:278`) by hand. If either changes, the shipped bound silently diverges. `test_the_bound_of_the_connection_finder_is_the_retrievals` only checks that it is at most 6.
7. **low**, `scripts/export_graph.py:209-218`: `canonical()` drops only `HIDDEN_PROPERTIES` from nodes in the prepared rows, not the `NODE_PROPERTIES` allowlist. An LLM query that returns a whole node would export every stored property. Nothing leaks today, but the export's privacy guarantee rests on the allowlist alone.
8. **low**, `scripts/export_graph.py:150-166`: `relate_fulltext()` moves the limit to the end of the query. That is only equivalent when nothing filters between `YIELD` and `RETURN`. Nothing does today, but the function doesn't check it, so a future `YIELD node WHERE ...` would silently change the results.
9. **low**, `site/graph-db.js:58-62`: `runNamed` does not wrap datetime parameters. The shipped `EVENTS_BETWEEN` and `TRENDING` take `$start`/`$end`/`$since` as ISO text and would silently return nothing if the page called them. The integration test wraps them itself (`TIME_NAMES`).
10. **low**, `.github/workflows/pages.yml:47-53`: the Node step uses whatever Node the runner happens to have (there is no pinned `actions/setup-node`). The push `paths` also leave out `scripts/vendor/relate-wasm-LICENSE.txt` and `tests/js/**`, which change what gets checked or shipped.
11. **low**, `scripts/vendor_relate.py:135`: `problems()` checks only that `LICENSE` exists, not its content. The other vendored files are checked by hash. The licence text itself is correct: it is byte-identical, apart from line endings, to `relate_db-1.1.0.dist-info/licenses/LICENSE` ("Copyright 2026 The Relate Authors"). The wasm also bundles third-party Rust crates. The wheel ships an SBOM for those, and the npm package ships no notices, so nothing more could be copied. That is worth a line in the note.
12. **low**, `docs/specs/13a-graph-in-the-browser.md:3`: `Status: done` although the branch run of `pages.yml` (an acceptance criterion) hasn't happened. AGENTS.md's definition of done requires every criterion to be verified. The learning note and the roadmap do say this openly.

Checked and fine:
- Vendoring: SHA-512 integrity is checked before extraction, then SHA-256 for each file. Files are extracted by exact name, `isfile()` is checked, the download is capped at 16 MB, the version is pinned and the User-Agent is generic.
- Workflow: the deploy job only runs on `main` (`if: github.ref == 'refs/heads/main'`, tested in `test_cicd.py`). Vendoring and the checks run before the upload. Permissions are unchanged and every action is pinned by SHA.
- CSP: only `'wasm-unsafe-eval'` was added, with no `worker-src`, so workers fall back to `script-src 'self'`.
- The shipped queries are generated from `wikipulse.qa.graph` and tested to equal the constants after the rewrite.
- Tests were not weakened. The size test now leaves out `vendor/`, as the spec asks, and the CSP test is stricter.
- `relate-db==1.1.0` is a dev dependency only.

Verdict: fix

## Questions for the human before merging

1. On the server, a generated `INSERT` would be harmless, yet in the browser it isn't. Which layer stops it on the server, why does the browser lack that layer, and what does that tell you about keeping one keyword list for two engines with different Cypher dialects?
2. `compare_rows` calls q01, q18 and q19 "tie". Walk through how `tie_column` decides that. Then construct two clearly different results that it would also call a tie, and say what extra check would rule them out (hint: what `compare_query` does for shipped queries with a `$limit`).
3. A visitor's query runs for 30 seconds. Trace exactly what happens between `GraphClient.send()`, `worker.terminate()` and `GraphClient.start()`. What state does the visitor lose, and what happens if the reload itself fails?
