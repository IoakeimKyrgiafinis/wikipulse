# Learning note: demo-pages (a static public demo on GitHub Pages)

There was no spec file; the coordinator's brief was the spec.

## What was built

A public demo that needs no server: visitors open
https://ioakeimkyrgiafinis.github.io/wikipulse/ (once Pages is enabled) and watch
5 November 2026 replay in their browser, the way the live page (05e) shows it. Events
appear, grow as pages join, turn from "active" to ended, and show their headline,
entities, relations and, for five of them, the analyst's briefing.

| Piece | File | What it does |
|-------|------|--------------|
| Exporter | `scripts/export_demo.py` | Reads one grouping run from Postgres and Neo4j (read-only) and writes `site/data/day.json`. Pydantic models (`DemoDay`, `DemoEvent`, `DemoPage`, `DemoSpike`, `DemoEntity`, `DemoRelation`, `DemoBriefing`, `DemoAnswer`) declare every field. |
| Page | `site/index.html`, `site/app.js`, `site/style.css`, `site/favicon.svg` | Plain HTML, CSS and JS with no build step, nothing from another origin, and the live page's look. |
| Data | `site/data/day.json` | 572 events, 665 spikes, 233 events in the graph, 1,442 entity mentions, 825 relations, 5 briefings, 8 prepared answers; 659 KB |
| Deploy | `.github/workflows/pages.yml` | Uploads `site/` and deploys it to Pages on pushes to `main` that touch `site/**`, or by hand |
| Tests | `tests/test_demo_site.py` (15), `tests/test_cicd.py` (+4) | Schema, counts, privacy, no external origins, size, the export's pure functions, and the workflow's triggers, pins and permissions |

How it fits:

```
Postgres: events, event_pages, spikes, pages ─┐
Neo4j: Event, Page, Entity, RELATED_TO ───────┼─ scripts/export_demo.py ─► site/data/day.json
evals/results/..05b-v1.json + data/qa_eval ───┘   (run once on the laptop, committed)
                                                              │
GitHub Pages ◄─ pages.yml (upload site/ as is) ◄──────────────┘
     │
browser: app.js fetches data/day.json, then a replay clock decides what is visible
```

## Key concepts

### A replay in the browser instead of a server

The live page receives events over a WebSocket. A static host can't push anything, but
everything the WebSocket would send can be computed from the recording, because every
record carries its **event time** (the project's core rule). In `app.js`:

- `prepare()` turns every timestamp into seconds since the start of the day, once.
- `viewAt(event, t)` says what an event looks like at replay time `t`: not there yet
  (before its first spike), which pages have joined (a page joins at its first spike's
  start), how many spikes, and whether it is still active (`t < ended_at`).
- `frame()` advances `t` by wall-clock time multiplied by the speed (60x to 3600x), using
  `requestAnimationFrame`, and `renderList()` redraws only when something visible changed
  (a per-event signature: pages/spikes/active), flashing cards that are new or grew.
- The scrubber sets `t` directly; replaying backwards needs no special code, because the
  view is a pure function of `t`.

Because the view is a function of event time, the same scrubber position always shows the
same list, which is the browser version of "a replay gives the same result as the run".

### Privacy by declaration

The exporter never dumps rows. Each output model has `extra="forbid"` and names its
fields, and `MEMBERS_SQL` selects named columns only. A spike becomes `start`, `end`,
`edits`, `editor_count` (a number) and `score`. The spikes table holds no usernames anyway,
and the correlation id (the edit that started a spike, 09b) is left out. The tests
check this from three directions:

- `test_no_personal_fields_are_exported`: no key such as `user`, `comment`, `summary`,
  `ip` or `correlation_id` anywhere in the file;
- `test_no_addresses_or_user_pages_in_the_text`: no IPv4/IPv6-looking string and no
  `User:` page in any value;
- `test_the_export_keeps_only_declared_columns`: rows that *do* contain a user, an edit
  summary and a correlation id go through `build_day()`, and none of it comes out.

### "No external origins", enforced twice

- **By the browser.** `index.html` carries a Content-Security-Policy in a `<meta>` tag
  (GitHub Pages can't set headers): `default-src 'none'; script-src 'self'; style-src
  'self'; img-src 'self'; connect-src 'self'`. Even injected markup could not load a
  script or call another server. Data from Wikipedia and the LLM is written with
  `textContent`, and only Wikipedia and Wikidata URLs become links (`link()`).
- **By the tests.** `test_the_page_loads_nothing_from_another_origin` (every `<script>`,
  `<link>`, `<img>` and similar is relative; absolute URLs only as navigation links to
  Wikipedia, Wikidata, the repository and the licence),
  `test_the_page_declares_a_same_origin_content_security_policy` and
  `test_scripts_and_styles_reach_no_other_origin` (no absolute URL in `fetch`, no `url(`
  or `@import` in CSS, the data URL relative so it works under `/wikipulse/`). A quick
  mutation check (adding a CDN `<script>` to the page) made the first test fail.

### Deploying static files with least privilege

`pages.yml` has two jobs and no default permissions (`permissions: {}`): `upload` gets
`contents: read` to check out the repository, and `deploy` gets only `pages: write` and
`id-token: write`. The id token is how `deploy-pages` proves to GitHub Pages which
workflow run is publishing (OIDC), so no secret is stored. All four actions are pinned to
commit SHAs with `# vX.Y.Z` comments, so the existing pin, checkout and secrets tests in
`tests/test_cicd.py` now run on `pages.yml` too.

## Decisions and deviations

- **Which day and run.** Grouping run `03b-default-fix` (detector run `02b-loose`) on
  5 November 2026. It is the only grouping run with graph extraction (233 events, all
  starting between 16:00 and 23:00 UTC), it has the five 05d briefings, and it is the day
  the evaluations, the snapshot and the quickstart use. The other runs (`03b-default`,
  `03b-close180`, `03b-thr050`) have no extractions; `live` and `replay` have 4 and 1 events.
- **The whole day, starting at 16:00.** All 572 non-merged events are exported, so the
  scrubber covers the full day, but the replay starts at 16:00 UTC (`START_AT`), where
  the graph data begins. Events outside 16:00 to 23:00 say "not in the graph". There is
  an "in the graph" filter. The one merged event is left out: its spikes moved to the
  surviving event.
- **Headlines are shown from the start.** The graph was extracted from each event's final
  form after the day, and its extraction time doesn't correspond to an event time. The
  page shows the headline as soon as the event appears and says so in the event detail
  ("live, they appear a few minutes after detection"). Simulating a delay would have
  invented timing.
- **Prepared answers.** Eight real answers from the 05b evaluation (asked against the
  frozen snapshot of this day's graph): six that both judges scored 2/2 and two they
  scored 0 (`q19`, `q24`), with the judges' scores and the overall 48% shown, so the
  sample doesn't oversell. The answer texts and citations come from
  `data/qa_eval/05b-v1/answers.jsonl`, which is local and not committed. Without it the
  exporter writes no answers. Nothing was invented.
- **News-classifier scores left out.** Only 89 of the run's 665 spikes have one (07c;
  the rest were never scored), so a badge on a few cards would mislead.
- **A small SVG graph** of the relations (entities on a circle, at most 14, coloured by
  type, a tooltip per line) above the full list of relations and entities.
- **At most 200 cards are drawn**, with a note and filters, so 3600x stays smooth.
- **The `pages.yml` checkout job gets `contents: read`.** The brief named `pages: write`
  and `id-token: write`; those are exactly the deploy job's permissions, and checking out
  needs read access to the contents.
- **Docker was down at the end.** The export was made while the stack was running. The
  last change to the exporter (revision numbers on briefing sources) was made before
  that final export, which the committed file shows. The tests read the committed file,
  so they don't need Docker.

## Results

- Site size: **708 KB** in total, of which `day.json` is 659 KB.
- Headless Chrome (DevTools protocol, a throwaway script) against
  `python -m http.server` from `site/`: the replay started by itself at 16:00 and
  reached 17:01 after 13 s at 300x (318 events, then 349). Scrubbing to 19:30 and
  clicking event 341 showed "Paraguayan president resigns after Ueno Bank collapse",
  the briefing with linked `[n]` markers, five pages and a graph of nine relations. At
  24:00 all 572 events were there (29 with two or more pages). At 390 px wide (mobile
  emulation) there was no horizontal overflow, including after selecting an event. Over
  the whole session there were **no console errors, exceptions, failed or 4xx requests**,
  and the only host requested was `127.0.0.1:8765`.
- `uv run pytest`: 1,317 passed, 2 skipped (19 of them new). `uv run ruff check .` and
  `uv run ruff format --check .`: clean.
- `.gitignore`'s `data/` matched every folder named `data`, `site/data/` included; one
  exception line `!site/data/` lets the export through, and the top-level `data/` is
  still ignored (checked with `git check-ignore`).

## Questions to test your understanding

1. Why can a static page show events "appearing live" without a WebSocket, and which
   project rule makes it possible?
2. The page sets a Content-Security-Policy in a `<meta>` tag. Why not a header, and what
   would still stop an injected `<script src="https://evil.example/x.js">`?
3. The deploy job has `id-token: write` but no secret. What is the token for?

<details>
<summary>Answers</summary>

1. Every record in the export has its own event time (spike starts and ends, event
   start and end). What the live page would have been told by time `t` is a pure
   function of the data and `t` (`viewAt()`), so the browser computes it. It's the
   "event time, not wall-clock time" rule. Wall-clock time only decides how fast `t`
   moves.
2. GitHub Pages doesn't let a site set response headers; a `<meta http-equiv>` CSP is
   the static-site equivalent (minus a few directives such as `frame-ancestors`). With
   `script-src 'self'`, the browser refuses to load a script from any other origin, and
   inline scripts too, so the injected tag does nothing. On top of that, `app.js` never
   writes data as HTML.
3. OpenID Connect: the job asks GitHub for a short-lived signed token saying "this is
   workflow pages.yml in this repository on main", and `deploy-pages` presents it to
   the Pages service, which checks the signature. Nothing long-lived is stored that
   could leak.

</details>
