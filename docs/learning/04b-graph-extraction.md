# Learning note: 04b Knowledge graph extraction

## What was built

Every event from stage 3 now becomes a small piece of a knowledge graph in Neo4j: the
people, organizations, places and named events involved, the relationships between them,
an LLM-written headline and one-sentence description, and, on every fact, the page and
revision it came from.

| File | What it does |
|------|--------------|
| `docker-compose.yml` | New service `neo4j` (Neo4j 5 Community) with the volume `neo4j-data`, a `cypher-shell` health check and modest memory (1 GB heap, 512 MB page cache). Browser on http://localhost:7474, Bolt on 7687. |
| `wikipulse/graph/inputs.py` | What the LLM sees: `EventPage`/`EventInput`, `pages_from_rows()` (several spikes of one page become one page), and `fetch_changes()`: the burst's revisions (`prop=revisions`, in event time), the diff from before the burst to its last revision (`action=compare`), reduced to readable added text (`changes_text()`, `clean_wikitext()`). |
| `wikipulse/graph/prompts/extract_v1.md` | The system prompt: what to extract, the five entity types, relation phrasing, rules ("only what the sources say"). |
| `wikipulse/graph/extract.py` | `extraction_schema()`, `build_message()`, `parse_extraction()` and `Extractor.extract()`: one LLM call per event through `wikipulse.llm`, constrained by a JSON schema. |
| `wikipulse/graph/names.py` | `normalize_name()`, `is_proper_name()`, `without_qualifier()`: comparing names. |
| `wikipulse/graph/link.py` | `Linker.link()`: names to Wikidata IDs in three steps (event pages, their links, Wikidata search). |
| `wikipulse/graph/store.py` | The graph model, `build_graph_event()` (extraction + links -> nodes and relationships) and `GraphStore` (constraints, idempotent `write_event()`, `delete_event()`, `run_counts()`). |
| `wikipulse/graph/run.py` | `uv run python -m wikipulse.graph.run`: live polling or `--once` over a time range, per-event failure handling, the report. |
| `wikipulse/db/migrations/0005_create_graph_extractions.sql`, `wikipulse/db/graph.py` | Table `graph_extractions`: which events are done, which failed (retry with backoff), what each extraction cost. |
| `wikipulse/config.py`, `.env.example` | `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `EXTRACT_MODEL`, `WIKIDATA_API_URL`, `EXTRACT_MAX_PAGES`, `EXTRACT_MAX_CHARS`, `GRAPH_POLL_INTERVAL_S`. |
| `wikipulse/check.py` | The service check now also connects to Neo4j. |
| `tests/graph/` | 28 unit tests (mocked LLM, saved Wikimedia answers in `tests/fixtures/graph/wikimedia.json`) and 9 integration tests (real Postgres and Neo4j). |

How the pieces fit:

```
Postgres events + event_pages + spikes + pages (stage 3)
        │  every 30 s (live) or once (a recorded day):
        ▼  pending_events(): new, changed (spike ids differ) or failed-and-due
GraphRunner.process(event)
  1. load_event()            pages of the event, highest spike score first
  2. with_changes()          top 6 pages: burst revisions + diff ─► added text   (Wikimedia, cached)
  3. Extractor.extract()     ONE LLM call: title, description, entities, relations  (wikipulse.llm)
  4. Linker.link()           page ─► link ─► Wikidata search                       (Wikimedia, cached)
  5. build_graph_event()     merge names with the same Wikidata ID, keys, provenance
  6. GraphStore.write_event() one Neo4j transaction: drop old facts, MERGE new ones, drop orphans
  7. record_success() / record_failure()  ─► Postgres graph_extractions
```

## Key concepts

### Information extraction with LLMs: prompts and schemas

Extraction turns free text into records. The LLM is good at reading ("Kaylub Giamanco,
an armed civilian, shot the perpetrator"), but its output must be machine-readable and
must not contain things we can't store. Two tools do that here:

- **The prompt** (`wikipulse/graph/prompts/extract_v1.md`) says *what* to extract: the
  five entity types, full names as Wikipedia titles ("Barack Obama", not "Obama"),
  relations as short phrases that read "subject relation object" ("leader of",
  "member of"), one source number per item, and rules ("use only what the sources say",
  "leave out dates and generic words"). `build_message()` numbers the event's pages as
  SOURCE 1, SOURCE 2, ... with their summary and the text added during the burst.
- **The JSON schema** (`extraction_schema()`) says *what shape* the answer has, and
  Ollama enforces it while the model writes (constrained decoding, see the 04a note).
  Every choice the code depends on is an `enum`: `type` can only be one of the five
  types, `certainty` only `high`/`medium`/`low`, and `source` only one of the numbers
  that exist for *this* event (the schema is built per event: `"enum": [1, 2, 3]`). So
  whole classes of errors can't happen: no "country" type, no SOURCE 7 of 3, no "pretty
  sure". The test `test_the_schema_allows_only_existing_sources_and_known_types` shows
  each one being rejected.

What a schema can't express is checked afterwards, in `parse_extraction()`: a relation
naming an entity missing from the list (added as type "other"), the same entity twice
("the Bangladesh national football team" and "Bangladesh national football team"),
relations from an entity to itself, and generic nouns and dates that a 4b model lists
despite the prompt ("a teacher", "three children", "October 15, 2026"). The last check is
`is_proper_name()` in `names.py`: an English name starts with a capital letter or a
digit (after an optional "the") and contains a capital letter ("2026 Paraguay banking
crisis", "the Senate"), and a bare date, year or number is not an entity. It also drops
"by June 2026" and "20 years", and, the price of a simple rule, lower-case brand names
like "iPhone". Before that filter, a test event got "a teacher"
linked by Wikidata search to an unrelated item whose alias is "a teacher", which is how
the filter came about.

Confidence is asked as a word, not a number, and mapped to 0.9 / 0.6 / 0.3
(`CONFIDENCE`). A small model's "0.87" carries no information; three words are easier
for it to use consistently. (In practice qwen3:4b says "high" for almost everything, see
"Results".)

### Knowledge graph modeling

Rule of thumb: things you want to find and connect are **nodes**, how they connect are
**relationships**, and facts about one thing are **properties**. The model
(`wikipulse/graph/store.py`):

```
(:Entity {key, wikidata_id, name, type, link_method, aliases})
    -[:INVOLVED_IN {source_title, source_revision, extracted_at, confidence, model, prompt_version}]->
(:Event {key, run_id, number, title, description, provisional_title, started_at, ended_at, ...})
    <-[:PART_OF {revision, first_edit_at, last_edit_at, score}]-
(:Page {title, wikidata_id, description, summary})

(:Entity)-[:RELATED_TO {relation, event_key, source_title, source_revision, extracted_at, confidence, ...}]->(:Entity)
```

- `Entity` and `Event` are the spec's nodes. An event's key is `<grouping run>:<number>`
  (`event_key()`), so two grouping runs of the same day never collide.
- `RELATED_TO` keeps the relation as a property (`relation: "leader of"`) rather than as
  the relationship *type* (`:LEADER_OF`). A 4b model invents many phrasings; Cypher
  can't take a relationship type as a parameter, and a property can be searched and
  normalized later (04c).
- A `RELATED_TO` belongs to the event it was read in (`event_key`). "A plays for B" read
  in two events is two relationships, each with its own source revision, which is what
  "full provenance" asks for.
- **Revision of the model: `Page` nodes.** The spec's model has no pages; I added
  `(:Page)-[:PART_OF]->(:Event)` with the summary and the burst's newest revision. Stage
  5 answers with citations ("page titles and revision IDs") and wants vector search over
  page summaries stored in Neo4j; both need the pages in the graph. A page shared by two
  events is one node.
- `name` is the Wikipedia title without its qualifier for entities linked through a
  page or a link ("Mohammad Ibrahim", not "Mohammad Ibrahim (footballer, born 1997)"),
  the Wikidata label for a search match, and the model's name otherwise. All names the
  model used are in `aliases`.

### Entity resolution with stable IDs

Entity resolution means deciding when two mentions are the same thing. Names are bad
keys: "Trump", "Donald Trump" and "President Trump" are one person, two "Georgia"s are
not one place. A Wikidata ID is a good key: one item per thing, the same in every
language, never renamed. So:

- A linked entity's node key **is** its Wikidata ID (`entity_key()`), and Neo4j has
  uniqueness constraints on both `Entity.key` and `Entity.wikidata_id` (`SCHEMA` in
  `store.py`). Two names for Q22686 can only ever reach one node: requirement 2 is
  enforced by the database, not just by careful code. `build_graph_event()` merges such
  names inside one event first (all names go into `aliases`), and `MERGE` merges them
  across events. Tests: `test_two_names_for_one_wikidata_item_become_one_entity` (unit)
  and `test_one_entity_per_wikidata_id_however_it_was_named` (Neo4j).
- **Linking** (`Linker.link()` in `link.py`) tries the most trustworthy evidence first:
  1. **page**: the name is one of the event's own pages, whose ID stage 3 stored. No
     request at all.
  2. **link**: the name is an article the event's pages link to. This is the key idea:
     the page's editors already chose which "Georgia" they meant, so the link title
     resolves the ambiguity *in this story's context*. `title_index()` also matches a
     title without its qualifier ("Mohammad Ibrahim" finds *Mohammad Ibrahim
     (footballer, born 1997)*) but only if that is unambiguous among the titles. The IDs
     of all matched titles come from one batched `prop=pageprops` request (50 titles
     each), following redirects but not redirects to a section (the 03a lesson).
  3. **search**: Wikidata's `wbsearchentities`. Only results whose label or alias *is*
     the name count; never disambiguation pages, family names, or (for anything but
     "other") creative works; Wikidata's best-ranked one wins unless another shares
     clearly more words with the event's summaries (`pick_search_result()`,
     `context_words()`).

  Names none of them resolve are kept, unlinked (`link_method = "none"`, key
  `name:<normalized name>`). The test `test_linking_tries_pages_then_links_then_search`
  checks the order through the requests made: none for a page match, then one batched
  pageprops query, then one search, only for the name nothing else found.
- **Unlinked entities are keyed by name across events.** "Kaylub Giamanco" in two events
  of the same story is one node. The risk is two different people with the same name
  and no Wikidata item merging into one; they are marked unlinked, so stage 5 can treat
  them with care. Keying them per event would avoid that, but would split every new
  person into one node per event, which hurts the questions stage 5 asks ("which
  organizations appear in more than one event?").

### Neo4j and Cypher

Neo4j stores nodes and relationships directly; Cypher describes patterns with ASCII
art: `(a:Entity)-[:RELATED_TO]->(b:Entity)`. The statements in `store.py` are worth
reading in order:

- `MERGE (n:Entity {key: e.key})` finds the node with that key or creates it (an
  "upsert"); `SET` then updates its properties. With `UNWIND $entities AS e` one
  statement handles the whole list.
- **Idempotent writes** (requirement 1): `_write_event()` runs in one transaction
  (`session.execute_write`, which also retries on transient errors). It first deletes the
  event's old `INVOLVED_IN`, `RELATED_TO {event_key}` and `PART_OF` relationships
  (`DROP_*`), remembering which nodes they touched, then MERGEs everything from the new
  extraction, then deletes the touched nodes that now have no relationships at all
  (`DROP_ORPHANS`). Same input twice: the same graph. New input: the old facts are gone,
  nodes other events use stay. Tests in `tests/graph/test_store_neo4j.py`.

Queries to try in Neo4j Browser (http://localhost:7474, user `neo4j`, password from
`NEO4J_PASSWORD`, default `wikipulse-graph`):

```cypher
// One event and everything around it
MATCH (ev:Event {key: '03b-default-fix:341'})<-[r]-(x) RETURN ev, r, x

// Entities in more than one event of the run
MATCH (n:Entity)-[:INVOLVED_IN]->(ev:Event {run_id: '03b-default-fix'})
WITH n, count(ev) AS events WHERE events > 1
RETURN n.name, n.wikidata_id, events ORDER BY events DESC

// Facts with their source revision (open https://en.wikipedia.org/?oldid=<revision>)
MATCH (a)-[r:RELATED_TO]->(b)
RETURN a.name, r.relation, b.name, r.source_title, r.source_revision LIMIT 25
```

### Cost-aware architecture

The LLM runs once per event, never per edit or per spike. On 5 November 2026 the
detector found 665 spikes in the day's edit stream (hundreds of thousands of English
article edits) and the grouper made 572 events of them, so the expensive step sees a
tiny fraction of the input. Further savings:

- an event is extracted again only when its spikes change (`fingerprint` in
  `graph_extractions`), not on every poll;
- a failed event waits 10 minutes, doubling with each failure in a row, up to about 5
  hours (`PENDING` in `wikipulse/db/graph.py`), so a broken event doesn't burn GPU time
  every 30 s;
- at most `EXTRACT_MAX_PAGES` (6) pages per event, each trimmed to `EXTRACT_MAX_CHARS`
  (1200) characters of summary and of added text, keep the prompt well inside the 8192
  token context (a 16-page event would otherwise not fit, see the 04a note);
- every Wikimedia request goes through stage 3's cached `WikimediaClient`, so a re-run
  of the same day costs almost no requests.

## Design decisions and deviations from the spec

- **The model is the installed `qwen3:4b`**, the spec's default `EXTRACT_MODEL`. It is
  the thinking-only 2507 build (see the 04a note). With a JSON schema, constrained
  decoding leaves it no room for free-text reasoning, so it answers directly (about 500
  output tokens per event, 6.6 s; see "Results"); nothing larger was pulled.
- **"Text of recent changes" = the diff of the burst.** For each page the runner asks
  for the revisions between the spike's first and last edit (plus one minute either
  side, in event time), then `action=compare` from the revision before the burst to the
  burst's last one, and keeps the *added* lines, cleaned of references, templates,
  tables and links (`changes_text()`). Sentences come first, infobox fields
  (`birth place: Cox's Bazar`) after them, so trimming cuts fields, not prose. A page
  created during its burst has no diff (its summary is its text); a diff that fails
  (e.g. a revision hidden since) only leaves the text empty. A burst whose edits cancel
  out (vandalism and its revert) has an empty diff, which is correct: nothing was added.
- **The newest revision of the burst is `source_revision`** of everything read from that
  page. It is the version of the page the model saw (summary aside, which is the
  current lead from stage 3).
- **One LLM call per event**, not per page: the title and description need the whole
  event, and one call is cheaper. Provenance stays per page because every entity and
  relation names its SOURCE number, which the schema limits to the event's pages.
- **Linking uses all of the event's pages**, the extraction only the top 6 by spike
  score (`EXTRACT_MAX_PAGES`).
- **Postgres tracks the work** (table `graph_extractions`, not in the spec): the graph
  lives in Neo4j, but "which events are done, which failed and when to retry" belongs
  next to the events. An event is extracted again when its spike ids change
  (`fingerprint`), and removed from the graph when stage 3 merges it into another
  (`GraphRunner.remove_merged()`).
- **Failures**: any error of one event (LLM, Wikimedia, Neo4j, an unexpected answer) is
  logged with the event's key and title and stored in `graph_extractions.error`; the
  runner goes on with the next event. The event is retried 10 minutes later, then 20,
  40, ... up to 320 minutes (requirement 3). The graph keeps the last good extraction
  until a new one succeeds. Test: `test_a_failed_event_is_recorded_and_retried_later`.
- **`extracted_at` is wall-clock time**: it is metadata about our processing, like
  `pages.enriched_at` in 03a. Which revisions are read is decided by event time.
- **Generic nouns and dates are dropped** after extraction (`is_proper_name()`), see
  "Information extraction". It assumes English capitalization, which holds for English
  Wikipedia.
- **Search is stricter than the spec's "Wikidata search for the rest"**
  (`pick_search_result()`). Looking at the first run's search links showed about half of
  them wrong in obvious ways: *Los Angeles* linked to an album, *Europe* to a French
  magazine, *Emperor Nero* to a painting, *Charleroi* to a section of the city. Two
  rules came out of that: (1) for a person, organization, place or event, a result
  described as a creative work or a section ("album of", "1980 film directed by",
  "French magazine", "Croatian TV series", "section of") never counts (`fits_type()`;
  "American film director" still does); (2) Wikidata's ranking decides, and the event's
  text overrides it only if a lower-ranked result shares at least two more words with
  it (`CONTEXT_MARGIN`). The first version let a single shared word decide, which is how
  "album of the American punk band X" beat the city in a story about an American. The
  context rule still finds *Bend, Oregon* in an Oregon story.
- **An entity's type is a vote.** The model types the same entity differently in
  different events (India as "place" in one, "organization" in another). Each
  `INVOLVED_IN` keeps the type of its mention, and the node's `type` is the one most of
  its events gave it, ties broken alphabetically (`RETYPE` in `store.py`, run after
  every write and delete). The first version simply kept the last write; re-extracting
  one hour of events then flipped the type of *India* and *United States House of
  Representatives* in events that were not even re-extracted, because the result
  depended on processing order. Test:
  `test_an_entitys_type_is_the_one_most_of_its_events_gave_it`. The `aliases` list only
  grows; `name` is the latest write (the names differ only in spelling).
- **Integration tests share the Docker Neo4j** (Community Edition has one database per
  server). Each test uses its own run id, names and made-up Wikidata IDs (`Q99...`) and
  deletes only what it wrote; the cleanup only removes nodes left without any
  relationship, so it can't remove real data.
- **Neo4j settings**: the container reads every `NEO4J_*` environment variable as a
  Neo4j setting and refuses to start on unknown ones, so the health check's password is
  passed as `HEALTHCHECK_PASSWORD`. Changing `NEO4J_PASSWORD` after the first start
  needs the `neo4j-data` volume recreated (Neo4j stores the password in its data).
- **New dependency**: the official `neo4j` Python driver (the spec calls for Neo4j).
- **Small additions outside the graph package**: `wikipulse.check` also checks Neo4j, the
  README has a stage 4 section, and the `integration` marker mentions Neo4j.
- **Wikidata IDs were rebuilt first.** Pages enriched before 03a's review fix could
  carry the parent article's ID for a redirect to a section. Before the run I
  re-enriched the day (`uv run python -m wikipulse.enrich.run --run-id 02b-loose --since
  2026-11-05 --until 2026-11-06 --force --once`: 563 pages in 10 s, 1,313 cache hits, 0
  requests); one page now has a `redirect_fragment` and no ID.
- **A sample, not the whole day** (AGENTS.md's 60-minute rule). At about 10 s per event
  the 572 events of 2026-11-05 would take over 90 minutes. The acceptance run covers
  the 131 events that started between 19:00 and 23:00 UTC, the busiest hours, with the
  evening wave of US election results; the 101 events from 16:00 to 19:00 were
  extracted by an earlier run with the same code. The report below uses the 19:00 to
  23:00 window only, because those numbers come from a run I watched from start to end.
- **The run report counts per event and per graph.** `RunStats.summary()` adds up
  entities over the events processed (an entity in three events counts three times),
  `graph_summary()` counts distinct nodes. The wording of the first line was changed to
  say so ("Summed over events (an entity counts once per event)").

## Results

All on 2026-11-08 with `ollama:qwen3:4b` (RTX 5060 Laptop GPU, 8 GB, `OLLAMA_NUM_CTX`
8192), over the events of grouping run `03b-default-fix` (stage 3's replay of
2026-11-05).

**The replayed sample** (acceptance criterion 2):

```
uv run python -m wikipulse.graph.run --run-id 03b-default-fix --since 2026-11-05T19:00:00+00:00 --until 2026-11-05T23:00:00+00:00 --once
```

Final state of the window, after the retries and the forced re-run of 19:00 to 19:30
described below (times from `graph_extractions`, counts from Neo4j):

| | 19:00 to 23:00 UTC |
|---|---:|
| Events processed | **132 of 132** (129 in the first run, 3 failed and succeeded on retry) |
| Distinct entities in the window's events | 777 |
| Linked to Wikidata | **619 (80%)**: page 99, link 382, search 138; unlinked 158 |
| Entity mentions summed over events | 840, of which 680 (81%) linked |
| Relations | 497 |
| Average time per event | **9.4 s** (median 8.1 s, 90th percentile 17.6 s, max 32.9 s) |
| of which waiting for the LLM | 6.7 s on average |
| Tokens per event | 838 in, 510 out |

The first run printed (before the retries; its slowest event took 44.9 s):

```
Processed 129 events (3 failed). Summed over events (an entity counts once per event): 810 entities, 81% linked, 479 relations. Average time per event 10.0 s (LLM 6.5 s, 502 tokens out).
Graph of run 03b-default-fix: 230 events, 1248 distinct entities, 78% linked to Wikidata (page 175, link 585, search 216, none 272), 808 relations.
Wikimedia API: 664 requests, 32 cache hits, 24 retries.
LLM ollama:qwen3:4b: 129 calls, 108195 tokens in, 64775 out, 840 s.
```

The whole graph of the run (both windows, 233 events, no failures left) has 1,280
distinct entities, 78% linked, and 826 relations. Most entities sit in one event; the
ones shared most are *United States* (10 events), *Democratic Party* (9), *United States
Senate* (5), *Republican Party* and *United States House of Representatives* (4).

Where the time goes: the LLM call is two thirds of it. The rest is Wikimedia: the
first run met HTTP 429 (too many requests) from the Action API 24 times and waited as
asked (`Retry-After`), which is why the slowest events took up to 45 s. Re-running the
same events costs almost no requests (72 of 88 lookups were cache hits on the forced
re-run below).

**Failures and retries** (requirement 3, for real): the laptop went to sleep twice
during the run (the log has gaps from 11:44 to 16:56 and 16:56 to 18:55). Three events
failed and were recorded in `graph_extractions` with their error: *Big Brother series
23* (`LLMUnavailableError: Ollama gave no answer within 120 s`, the call cut by the
sleep) and *Jose Chameleone* and *Vessel* (`WikiApiError` after `getaddrinfo failed`:
no network right after waking up). The runner logged each one and went on. Running the
command again 10 minutes later picked up exactly the events whose retry delay had
passed and extracted them. Wall-clock time of that run is meaningless (7.5 hours for 21
minutes of work), so all times above are per event, from `graph_extractions.seconds`.

**Idempotent writes on real data** (requirement 1): I saved the graph's counts, then ran
the 15 events of 19:00 to 19:30 again with `--force`. Afterwards (before the last
retry): still 232 `Event` nodes; 1,277 entities (1,266 before: the new answers named a
few more) with 1,277 distinct keys, and 1,000 distinct Wikidata IDs on 1,000 nodes; 0
duplicated `INVOLVED_IN` or `RELATED_TO` relationships. Each re-processed event was
replaced, not added to.

**But a re-run is not a replay.** 6 of those 15 events came back different (*"Frozen
yogurt definition updated with international standards"*, 3 entities, became *"Frozen
yogurt's health benefits and origins clarified"*, 10 entities). The input was the same:
the revisions and diffs came from the same cache entries, and the message built twice
was byte for byte identical. Three calls in a row with that message gave three
identical answers, so the model is deterministic *within* a session, but not across
sessions. The likely cause is that Ollama (llama.cpp) reuses the cached prompt prefix
from the previous request, which changes how the prompt is split into batches, and
floating-point sums in a different order can flip one greedy token, after which the
answer diverges. So the project's event-time rule holds for everything up to the
LLM (the same revisions and the same diffs are read), and the graph is stable under
re-processing, but the LLM's answer itself is not guaranteed to repeat. The
`graph_extractions` table stores model and prompt version, not the answer; storing the
raw answer would make a replay exactly reproducible (an idea for 04c).

**Quality, by looking** (not a measured evaluation; that is 04c):

- *Ueno Bank* (event 341): "Paraguayan president resigns after Ueno Bank collapse",
  12 entities, 10 linked, facts like *Pedro Alliana* "became" *President of Paraguay*
  and *Ueno Bank* "collapsed" *2026 Paraguay banking crisis*, each with its page and
  revision. *Asunción* is typed "other", because the model named it only in a relation.
- Search links are the weak step. Of 21 sampled search links, about 15 are right
  (*Atlanta*, *Lisa Loeb*, *Hanns Seidel Foundation*, *Marathon County*). Wrong ones:
  *Georgia* in a Europa League story linked to the US state, three G.I. Joe character
  names (*Airborne*, *Avalanche*, *Altitude*) linked to unrelated items, *Rock of Ages*
  in a Russell Brand story, and *Polonia* (a football club) linked to Poland.
- Titles are mostly good headlines. One is plainly wrong: "Lisa Loeb passes away in
  1999 after health issues", described as "died in 1999 after a fight with Christy".
  The page's summary says she was born in 1968 and is a living musician; the burst was
  most likely vandalism, and the model took the added text as fact despite the prompt's
  rule to leave vandalism out. Telling vandalism from news needs more than a prompt
  rule (for example the spike's revert counts from stage 2), a topic for 04c.
- Confidence carries little information: 96% of mentions are "high" (1,380 of 1,438),
  49 "medium", 9 "low" (whole graph).
- Types (whole graph): 344 other, 293 person, 258 place, 253 organization, 132 event.

**Tests:** 37 new tests in `tests/graph/`: 28 unit tests (mocked LLM, saved Wikimedia
answers) for parsing, linking order, inputs and building the graph, and 9 integration
tests against the Docker Postgres and Neo4j (idempotent writes, one entity per Wikidata
ID, provenance, type voting, re-extraction when spikes change, failure and retry,
merged events). `uv run pytest`: 349 passed. `uv run pytest -m integration`: 29 passed.
`ruff check` and `ruff format --check` are clean.

## Questions to test your understanding

1. The model writes "Trump" in one event and "Donald Trump" in another. Walk through
   what decides whether the graph gets one node or two, and what would happen if neither
   name could be linked to Wikidata.
2. Why is `source` in the extraction schema an `enum` of the event's SOURCE numbers
   instead of a plain integer, and what would go wrong in the graph without it?
3. You run `wikipulse.graph.run --force` over yesterday's events. Which parts of the
   result are guaranteed to be the same as the first run, and which are not? Why?

<details>
<summary>Answers</summary>

1. `Linker.link()` tries the event's own pages, then the articles they link to, then
   Wikidata search. If both names reach the same Wikidata ID (say through a link to
   *Donald Trump* and a search whose alias matches), `entity_key()` gives both the key
   `Q22686`, `MERGE` finds the same node, and the uniqueness constraints on
   `Entity.key` and `Entity.wikidata_id` make a second node impossible; both names end
   up in `aliases`. If neither links, the keys are `name:trump` and `name:donald trump`:
   two unlinked nodes, because without a stable ID the code has no safe way to know they
   are the same person (merging by surname would also merge different Trumps).
2. Every entity and relation gets its `source_title` and `source_revision` from the
   SOURCE it cites (`parse_extraction()` maps the number to an `EventPage`). With a
   plain integer the model could cite SOURCE 7 of an event with 3 pages, and the fact
   would get no page or the wrong one: the provenance, the thing that lets a reader
   check a fact, would be wrong. With the enum, Ollama's constrained decoding can only
   produce numbers that exist, so the error can't happen at all.
3. The inputs are the same: the revisions are chosen by the spikes' event times and
   the API answers come from the cache, so the message to the model is byte for byte
   identical. The *structure* is guaranteed too: `write_event()` replaces each event's
   facts in one transaction, so there are no duplicate events, entities or
   relationships. The LLM's answer is not guaranteed to repeat: in this project's
   measurement, 6 of 15 events came back with a different title or entity list,
   although three calls in a row with the same message agreed. Local inference is not
   bit-reproducible across sessions, so the titles, entities and relations can change.
   Storing the raw answers would make a replay exactly reproducible.

</details>
