# Spec 13a: The knowledge graph, live in the browser

Status: done
Stage: 13. Showcase
Owner: agent
Depends on: 10, 12a

## Goal
The public demo on GitHub Pages lets a visitor explore and query the real 5 November 2026
knowledge graph live in their browser, with no server. The graph runs on Relate by Neo4j,
an embedded graph database that speaks Cypher, compiled to WebAssembly. The answers it
gives match what WikiPulse's own retrieval gets from Neo4j on the same graph.

## Context
- The demo (`site/`, spec 10 and the demo export `scripts/export_demo.py`) replays one
  recorded day. Its "Prepared answers" are static text, because asking the graph needs a
  database and an LLM, and Pages has neither.
- **A feasibility check (18 November 2026) showed the database part works.** The check
  was run outside the repository:
  - **Export:** the frozen graph in `neo4j-eval` (snapshot `2026-11-05`) was exported
    to JSON: 1,761 nodes and 2,534 relationships, matching `data/snapshots/2026-11-05/manifest.json`.
  - **Comparison with Neo4j:** the graph was loaded into Relate 1.1.0 (`relate-db` on
    PyPI) and 71 statements were run on both databases: the 20 fixed retrieval queries
    of `wikipulse/qa/graph.py` and every distinct LLM-written Cypher query stored by the
    05b and 12a evaluation runs (`data/qa_eval/*/answers.jsonl`).
    - 54 gave identical rows.
    - 7 gave the same rows in another order (no full `ORDER BY`).
    - 7 differed only where rows tie, or in the third decimal of vector scores. Neo4j's
      vector index is approximate and quantised; Relate's search is exact.
    - 3 failed on both databases (the same broken LLM queries).
  - **The one incompatibility:** `db.index.fulltext.queryNodes` takes 2 arguments in
    Relate, not 3 (`{limit: $limit}`). Dropping the third argument and adding
    `LIMIT $limit`, or using Relate's `SEARCH` clause, both work.
  - **The browser build:** `@relate/relate-wasm` 1.1.0 (Apache-2.0, a 7.1 MB `.wasm`
    file) ran in a browser. The graph without embeddings is 1.6 MB of JSON and loaded
    in **1.1 s**, once each relationship's ends were matched by label with an index on a
    temporary id; without that index the load took 20 s. Queries took 4–17 ms: full-text
    with accent folding, `duration` arithmetic, `allShortestPaths` and relation lookups.
  - **Datetimes:** JSON parameters can't carry datetime values, so timestamps are loaded
    as ISO strings and converted with `datetime()` in Cypher after the load.
- **Relate's limits that matter here:**
  - no APOC (WikiPulse uses none);
  - the browser store is `:memory:` or `opfs://` only;
  - **no statement timeout or cancel in WebAssembly**;
  - a Rust panic kills the instance (`isWasmTrap`).
- The site's Content-Security-Policy (`site/index.html`) is strict: `script-src 'self'`.
  Instantiating WebAssembly needs `'wasm-unsafe-eval'` in `script-src`. Nothing else
  should be loosened.
- `tests/test_demo_site.py` caps the site at 3 MB (`MAX_SITE_BYTES`) and checks the
  exported data for privacy. The `.wasm` file alone is bigger than that cap.

## Inputs
- The frozen graph in `neo4j-eval` (`docker compose --profile eval up -d`, restored with
  `scripts/snapshot_graph.py restore`), and the stored eval answers in
  `data/qa_eval/12a-after/answers.jsonl` with the questions in `evals/labels/qa_questions.jsonl`.
- The pinned packages `@relate/relate-wasm` (npm) and `relate-db` (PyPI), version 1.1.0.

## Outputs
- **The graph export.** `scripts/export_demo.py` (or a sibling script) writes
  `site/data/graph.json` from the frozen eval graph.
  - It holds nodes and relationships with their properties, and the datetime properties
    as ISO strings with a list of which properties are datetimes.
  - It holds no embeddings.
  - It holds the same privacy guarantees as `day.json`: every field named, no editor
    names or IP addresses, no edit summaries.
- **The queries, shipped as data.** The Cypher the page runs is exported into the data
  file from the Python constants in `wikipulse/qa/graph.py`, so it is not copied by hand.
  The full-text call gets the 2-argument rewrite, as one tested function.
  - **Prepared questions:** for each eval question whose stored 12a query ran, the data
    holds the question, the Cypher the LLM wrote for it, the rows Neo4j returned
    (`cypher_rows`), and the stored answer with its judge scores.
- **The Relate WebAssembly build, vendored without bloating git.** A script
  (`scripts/vendor_relate.py`) downloads the pinned npm tarball, checks its integrity
  hash against a committed value, and extracts `relate.js` and `relate_bg.wasm` into
  `site/vendor/relate/` together with its license.
  - `pages.yml` runs the script before uploading the site.
  - The vendored files are git-ignored.
  - Local preview runs the same script.
- **The page** (`site/`). A new "Explore the graph" section starts the database only when
  the visitor opens it, so the replay stays fast. It offers:
  1. **Search:** accent-insensitive entity search (full-text index).
  2. **Entity view:** the entity's events, its relations with their source revisions,
     and the people, organisations and places it connects to.
  3. **Connection finder:** how two entities are connected (`allShortestPaths`, bounded
     as in `wikipulse/qa/graph.py`).
  4. **Prepared questions:** each shows the LLM-written Cypher, a "Run it" button that
     runs it live, and the live rows next to the stored answer.
  5. **"Try your own Cypher":** an editable box, read-only by construction (see
     Requirements), with a row cap.
- **The database runs in a Web Worker.** A long or runaway query can then be stopped by
  terminating the worker and reloading the graph, since WebAssembly has no statement
  timeout.
- **Tests,** an updated learning note for the demo
  (`docs/learning/13a-graph-in-the-browser.md`), and README and `docs/usage.md` updates.

## Requirements
1. **Same answers as Neo4j.** An integration test (marked `integration`, needs
   `neo4j-eval`) runs every shipped query and every prepared question's Cypher on Neo4j
   and on Relate (`relate-db`, the same version as the WebAssembly build) over the same
   exported graph. Results must match, ignoring row order wherever the query doesn't
   fully order them, and vector scores aren't involved. Any remaining difference is
   listed and explained in the learning note.
2. **Unit tests without Docker:**
   - the export's schema and privacy;
   - the full-text rewrite;
   - the shipped queries are exactly the Python constants after the rewrite;
   - the site size rule.
3. **Read-only in the browser.** Visitor Cypher goes through the same validator as
   WikiPulse (`wikipulse/qa/cypher.py`), ported or re-implemented in JavaScript, with
   its tests' cases shared as a JSON fixture that both implementations pass. The
   database is in memory and thrown away, so a write could only change the visitor's
   own copy; the validator still refuses writes, so the page never shows that it can
   change.
4. **Bounded work.**
   - Every query has a row cap.
   - The worker is terminated after a time limit (for example 5 s), and the page says so
     and reloads the graph.
   - Variable-length patterns are bounded, as the validator already requires.
5. **The CSP gains only `'wasm-unsafe-eval'`.** Everything else stays as strict as now.
   No third-party requests at run time: the `.wasm` file is served from the site itself.
6. **Size and speed.**
   - The replay must not load the database; the graph section loads it on demand.
   - With the database, the graph section is usable within 3 s on a laptop on a fast
     connection; record the measured time.
   - Update the size test: committed files stay under the current cap, and the
     vendored, git-ignored files are counted separately with their own documented
     limit.
7. **Pinned and verified.** The exact package version and integrity hash are committed;
   a mismatch fails the vendoring script and the Pages workflow.
8. **No LLM and no embeddings in the browser.** Prepared answers stay the stored ones,
   and the page says plainly that free-form questions need the local LLM.
9. **Accessible and phone-friendly** like the rest of the demo: keyboard navigation,
   labels, no horizontal scrolling at phone width.

## Acceptance criteria
- `uv run pytest`, `ruff check` and `ruff format --check` pass. `uv run pytest -m
  integration` passes the Neo4j-vs-Relate comparison with `neo4j-eval` running.
- The page runs locally (the `demo-site` preview, after the vendoring script). The agent
  checks it in a browser:
  - search for "pena" finds Santiago Peña;
  - Santiago Peña → Ueno Bank shows a path;
  - every prepared question's live rows match its stored rows, or the difference is
    explained;
  - a write query typed into the Cypher box is refused;
  - a deliberately slow query is stopped by the time limit.
- The measured load time and the size of what the browser downloads are recorded in the
  learning note.
- The Pages workflow succeeds on a branch run (`workflow_dispatch`). The human approves
  deploying to the public site (it publishes).

## What you'll learn
- **Embedded vs server databases:** what an in-process engine gives up (concurrency,
  scale) and gains (no deployment, millisecond start-up), and when each fits.
- **WebAssembly in the browser:** loading it, the CSP it needs, and running it in a worker.
- **Supply-chain hygiene for a binary dependency:** pinning, integrity hashes, licences.
- **Proving two engines agree:** differential testing of query results, and why ties
  and approximate vector search make "identical" the wrong test.

## Look at it yourself (optional, never blocking)
- Open the graph section on your phone and find how two people from a story are connected.
- Type a query of your own into the Cypher box.

## Out of scope
- Running an LLM or an embedding model in the browser (no free-form questions, no
  vector search).
- Replacing Neo4j anywhere else in WikiPulse (pipeline, API, MCP, Kubernetes).
- Entering the Relate hackathon: the human decides and submits.

## Notes for the agent
- Start from the feasibility check's approach (summarised in Context); its throwaway
  scripts aren't in the repository.
- `relate-db` is a new dev dependency (for the integration test); `@relate/relate-wasm`
  is not an npm dependency of the project, only a vendored, verified file. Explain both
  in the learning note.
- If Relate behaves differently from what Context describes (a newer version, a broken
  feature), record it, and choose the closest working approach.
