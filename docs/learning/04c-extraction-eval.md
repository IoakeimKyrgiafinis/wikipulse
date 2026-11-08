# Learning note: 04c Extraction evaluation

## What was built

A way to put numbers on the knowledge graph of 04b: how often an entity is linked to the
right Wikidata item, how many extracted relationships Wikidata also knows, and how many
are actually what their source text says.

| File | What it does |
|------|--------------|
| `wikipulse/eval/extraction.py` | The command line: `report`, `label`, `extract`, `compare` (see below). Fetches, runs, writes `evals/results/<date>_<name>.md` and `.json`. |
| `wikipulse/eval/extraction_report.py` | The measurements, as pure functions: `check_links()`/`LinkCounts`, `check_support()`/`support_summary()`, `count_labels()`/`LabelCounts`, the per-type table (`type_rows()`), the report (`build_report()`, `render_markdown()`) and `compare_reports()`. |
| `wikipulse/eval/wikidata.py` | The ground truth: `Wikidata.items_by_label()` (which items carry a name as their label) and `Wikidata.statements_between()` (which statements connect two items), both as batched SPARQL queries to the Wikidata Query Service. |
| `wikipulse/eval/extraction_data.py` | What is evaluated (`ExtractionData`: events, entities, relations), read from Neo4j (`load_from_graph()`) or from a saved re-extraction (`load_from_file()`); `parse_events()` for `--events`. |
| `wikipulse/eval/relation_labels.py` | The labels file `evals/labels/relation_labels.jsonl`, the fixed sample (`sample_relations()`), the LLM judge (`LLMRelationJudge`, `JUDGE_PROMPT`) and the manual labeler (`label_manually()`). |
| `wikipulse/graph/prompts/extract_v2.md` | A second extraction prompt, for the prompt comparison. |
| `wikipulse/config.py`, `.env.example` | `WIKIDATA_SPARQL_URL`. |
| `tests/eval/test_wikidata.py`, `test_extraction_report.py`, `test_relation_labels.py`, `test_extraction_data.py` | Unit tests; the Wikidata ones answer from real saved responses (`tests/fixtures/eval/wikidata.json`). `test_extraction_graph.py` is an integration test against the Docker Neo4j. |

How it fits together:

```
Neo4j graph (04b)  ─or─  data/extractions/<name>.jsonl (extract: same pipeline, other prompt)
        │ load_from_graph() / load_from_file()  ─►  ExtractionData
        ▼
report: ├─ every name the model used ──► items_by_label() ──► check_links()   (1) linking accuracy
        ├─ relations, both ends linked ─► statements_between() ─► check_support() (2) Wikidata support
        └─ sample_relations(50, seed) ──► labels file ──► count_labels()       (3) labeled precision
                                              ▲
label:  sample ─► source_texts() (the page as the model saw it) ─► LLMRelationJudge / --manual
```

Commands (all take `--run-id`, `--events`, `--name`):

```
uv run python -m wikipulse.eval.extraction report  --run-id 03b-default-fix --events 2026-11-05 --name 04c-v1
uv run python -m wikipulse.eval.extraction label   --run-id 03b-default-fix --events 2026-11-05 --name 04c-v1
uv run python -m wikipulse.eval.extraction extract --run-id 03b-default-fix --events 2026-11-05 --name 04c-v2 --prompt extract_v2
uv run python -m wikipulse.eval.extraction report  ... --name 04c-v2 --from 04c-v2
uv run python -m wikipulse.eval.extraction compare 04c-v1 04c-v2
```

## Key concepts

### Evaluating LLM extraction: three numbers, three failures

An extraction can fail in different places, and one number would hide which one. So
the report has three, each aimed at one step of the 04b pipeline:

- **Linking accuracy** checks the *linker* (`wikipulse/graph/link.py`): given a name,
  did it pick the right Wikidata item? Ground truth: if exactly one Wikidata item has
  the name as its label, that item is the answer. `check_links()` makes one
  `LinkCheck` per (entity, name the model used), and `LinkCheck.outcome` is `correct`,
  `wrong`, `unlinked` or `no_label`. A name that several items carry ("Paraguay": the
  country and four other items) has no single right answer, so it is
  counted apart (`shared_*` in `LinkCounts`): only "did the link go to one of them" can
  be checked. The report splits linking by method (`page`, `link`, `search`), which
  tells you *which step* of the linker fails.
- **Relation support** checks the *relations* against a knowledge base: of the
  relationships whose two ends are both linked, how many does Wikidata state, with any
  property, in either direction (`check_support()`)? It is a **lower bound** on
  correctness, for two reasons: Wikidata doesn't know yet what happened yesterday, and
  most things an article says ("X criticized Y", "X was hacked into Z") are never
  Wikidata statements. And it is not proof either: "Santiago Peña *resigned* President
  of Paraguay" is "supported" because Wikidata says he held that office, not because it
  knows he resigned. Support says "Wikidata also connects these two", not "the relation
  phrase is right".
- **Labeled precision** checks the *extraction model* itself: is each relationship what
  its source text says? Only a reader can tell, so a fixed sample of 50 relationships
  (`sample_relations()`: the first 50 in a hash order of their keys, so the same
  relationships are picked every time) is labeled `correct`, `wrong` or `unsure`
  against the page as the model saw it (`source_texts()` rebuilds it from the same
  cached revisions and diffs, trimmed the same way). Precision = correct / (correct +
  wrong), and the report says who labeled (`llm (ollama:qwen3:8b)` or `human`).

What about *recall* (entities and relations the model missed)? That needs a list of
everything that *should* have been extracted, which neither Wikidata nor a quick label
gives. The report shows unlinked names that do have exactly one Wikidata item
(`unique_unlinked`): links the linker missed. Extraction recall is left for a human
with time; the spec asks for precision.

### Wikidata as ground truth, and its blind spots

`wikipulse/eval/wikidata.py` asks the Wikidata Query Service two kinds of SPARQL query,
each for a batch (25 names, or 40 item pairs per request):

```sparql
# Which items carry these names as their label?
SELECT ?label ?item WHERE {
  VALUES ?label { "Frozen yogurt"@en "Frozen yogurt"@mul "frozen yogurt"@en ... }
  ?item rdfs:label ?label .
  FILTER NOT EXISTS { ?item wdt:P31 ?class . VALUES ?class { wd:Q4167410 ... } }
}
# Which statements connect these items?
SELECT DISTINCT ?a ?b ?property ?propertyLabel WHERE {
  VALUES (?a ?b) { (wd:Q22695687 wd:Q733) (wd:Q733 wd:Q22695687) ... }
  ?a ?direct ?b .
  ?property wikibase:directClaim ?direct .
}
```

Choices and why:

- **"Exactly a label"** means the English (`en`) or language-neutral (`mul`) label, with
  the first letter in either case: Wikipedia titles start with a capital ("Frozen
  yogurt"), Wikidata labels of common nouns don't ("frozen yogurt"). Wikimedia's own
  items (disambiguation pages, categories, templates, lists) and name items (family
  name, given name) are left out: "Netanyahu" is the label of a family-name item, but
  the model meant the person.
- **Truthy statements only** (`wdt:`, the best rank of each property). Asking for every
  rank with the property left open (`?a ?p ?s . ?s ?ps ?b`) timed out after 60 s even
  for ten pairs, because the United States has thousands of statements; the truthy
  query answers 40 pairs in under a second.
- **SPARQL, not the Action API.** `wbsearchentities` is a ranked prefix search (no
  guarantee to see *all* items with a label), and `wbgetentities` returns megabytes for
  big items. The whole day took 76 small requests.
- **Truthy hides the past.** The first version asked for every rank and found that
  *Paraguay* has *Santiago Peña* as head of state and head of government; the truthy
  query finds only his citizenship. Those statements still exist, but ranked below
  the statement for the current holder. So a relation like "Peña was
  president of Paraguay" can be supported by Wikidata and still not count here: one
  more reason the number is a lower bound.
- **Answers are cached for 30 days** (`EVAL_CACHE_TTL`) in the same `api_cache` table as
  stage 3, so re-running a report gives the same numbers instead of following
  Wikidata's edits from one day to the next (Wikidata is edited all the time, and
  catches up with the news within days).

The blind spots show up in the results below; in short:

- **Too new.** Things created by the news ("Ueno Bank", "2026 Paraguay banking crisis")
  have no item yet: no label, no statements. These names land in `no_label`, and their
  relations can't be supported. That is why support is a lower bound.
- **The label is not the identity.** Some "wrong" links are right and the ground truth
  is wrong: the linker followed the article's own link to the archipelago *Galapagos
  Islands* (Q38095, labeled without the accent), while the item labeled exactly
  "Galápagos Islands" is the UNESCO World Heritage site (Q117188122). The exact-label
  rule only works when Wikidata's label and Wikipedia's title agree. "Results" below
  goes through all the disagreements.
- **Shared names have no answer.** "Paraguay", "Georgia" and "Santiago Peña" (two
  people) are carried by several items, so the most interesting linking decisions are
  the ones this measure can't judge.

### Prompt iteration with evidence

"The new prompt seems better" is how prompts get worse: you remember the two examples
that improved and not the ten that didn't. The procedure here:

1. **Fix the measurement first.** The same events (the 233 extracted events of
   2026-11-05, 16:00 to 23:00 UTC), the same ground truth (cached Wikidata answers), the
   same judge (`ollama:qwen3:8b`, `JUDGE_PROMPT_VERSION` 1), the same sample rule (the
   first 50 relations in a seeded hash order).
2. **Change one thing.** `extract_v2.md` differs from `extract_v1.md` only in the
   relations part: a relation must be stated in one sentence or infobox field of the
   cited source, must be a verb phrase that makes a true sentence ("plays for", never a
   bare noun like "club" or a vague "associated with"), and the model is told to check
   each relation against its source and to prefer fewer, correct relations. The schema,
   the model and everything else stay the same.
3. **Re-run without touching the graph.** `extract --prompt extract_v2 --name 04c-v2`
   runs the same pipeline as `wikipulse.graph.run` (`extract_event()`: changes, LLM,
   linking, `build_graph_event()`) but appends each event to
   `data/extractions/04c-v2.jsonl` instead of writing Neo4j. The 04b graph stays as it
   is, and the file keeps the answers, so the comparison can be re-read later.
4. **Compare numbers, then read the differences.** `compare 04c-v1 04c-v2` puts the two
   reports side by side; the labeled samples show *which* relations changed.

One caveat to keep in mind with any comparison here: the 04b note found that the same
prompt re-run gives a different answer for about 6 of 15 events (local inference is not
bit-reproducible across sessions). Part of any difference between two runs is that
noise, not the prompt. A difference of a few points on 50 labels is within it.

## Design decisions and deviations from the spec

- **Subcommands.** The spec names one command with `--events` and `--name`; there are
  four (`report`, `label`, `extract`, `compare`), as in the grouping evaluation, because
  labeling (minutes of GPU) and reporting (seconds) are run at different times. All
  take `--run-id` (the grouping run: event keys are `<run>:<number>`), `--events` and
  `--name`.
- **"A run over one day" covers the extracted part of the day.** `--events 2026-11-05`
  selects every event of that day in the graph, which is the 233 events 04b extracted
  (16:00 to 23:00 UTC; the rest of the day was never extracted because of the 60-minute
  rule). Extracting the other ~340 events would have taken another hour and changed
  the 04b graph, which this spec must not do.
- **Ground truth through SPARQL**, a new setting `WIKIDATA_SPARQL_URL` (no new
  dependency: the requests go through stage 3's `WikimediaClient` with one request at
  a time, the User-Agent from config and a 30-day cache).
- **What "exactly matches a Wikidata label" means**: the `en` or `mul` label, the first
  letter in either case, after leaving out Wikimedia-internal and name items
  (`EXCLUDED_CLASSES`). Every name the model used for an entity is checked (its
  `aliases`), each (entity, name) pair once. Names several items carry are reported
  apart, because they have no single right answer.
- **Truthy statements only**, both directions, for support (the all-ranks query timed
  out; see above).
- **Re-extraction goes to a file, never to Neo4j.** The graph keys events by grouping
  run (`03b-default-fix:341`), and entity nodes and their type votes are shared by all
  events, so writing a second extraction of the same events would have changed the 04b
  graph. `extract` runs the same steps as `GraphRunner._extract()` (six lines,
  repeated in `extract_event()` rather than refactoring `wikipulse/graph/run.py`, which
  is outside this spec) and appends to `data/extractions/<name>.jsonl`. This also keeps
  the raw outcome of each run, which the 04b note wished for.
- **The judge's prompt went through two versions** (`JUDGE_PROMPT_VERSION`). Version 1
  was lenient; version 2 asks for three explicit checks (subject, object, relation) and
  puts `reason` before `label` in the JSON schema, so the model writes its check
  before it commits to a label. Version 2 was written after reading version 1's
  answers on this very sample, so it is tuned on it; the labels of both versions are
  in the file (148 lines: 74 relations twice), and the reports use the latest
  (version 2). A human label would beat both.
- **The labeler** is `ollama:qwen3:8b` (`EVAL_LABEL_MODEL`, pulled for this spec),
  thinking off, temperature 0, about one second per relation; the extraction under
  test is `qwen3:4b`, so the judge is not the model judging itself.
- **The report lists every linking disagreement** with what both items are (one extra
  `describe` query), because the number alone hides that half of the "errors" are
  errors of the ground truth.

## Results

All on 2026-11-08, over the 233 extracted events of grouping run `03b-default-fix`
(2026-11-05, 16:00 to 23:00 UTC). Reports: `evals/results/2026-11-08_04c-v1.md` (the
04b graph, prompt `extract_v1`) and `evals/results/2026-11-08_04c-v2.md` (prompt
`extract_v2`).

```
uv run python -m wikipulse.eval.extraction label  --run-id 03b-default-fix --events 2026-11-05 --name 04c-v1
uv run python -m wikipulse.eval.extraction report --run-id 03b-default-fix --events 2026-11-05 --name 04c-v1
233 events: linking accuracy 95.9% (484 names), Wikidata support 31.9% (527 relations), labeled precision 93.9% (49 labeled)
```

### The 04b graph (extract_v1)

| Measure | Value | Based on |
|---|---:|---|
| Linking accuracy (names exactly one Wikidata item carries) | **95.9%** ± 1.8 pp | 484 linked names |
| Linked to one of the items carrying the name (shared names) | 92.3% | 392 linked names |
| Relation support in Wikidata (lower bound) | **31.9%** ± 4.0 pp | 527 relations between linked entities (of 825) |
| Labeled precision | **93.9%** ± 6.7 pp | 46 correct, 3 wrong, 1 unsure of 50; labeled by `llm (ollama:qwen3:8b, judge prompt v2)` |

By entity type (relations under their subject's type):

| Type | Entities | Linked | Linking accuracy | Wikidata support | Labeled precision |
|---|---:|---:|---:|---:|---:|
| person | 293 | 78.8% | 99.3% (142/143) | 32.2% (76/236) | 88.0% (22/25) |
| organization | 257 | 78.6% | 96.1% (99/103) | 26.6% (21/79) | 100% (11/11) |
| place | 257 | 90.7% | 94.8% (73/77) | 41.2% (7/17) | 100% (1/1) |
| event | 134 | 78.4% | 95.5% (64/67) | 21.1% (15/71) | 100% (6/6) |
| other | 344 | 67.4% | 91.5% (86/94) | 39.5% (49/124) | 100% (6/6) |

The per-type precision rests on very few labels (one for places); only the person
row has enough to say anything.

By link method: **page 99.1%** (111 of 112), **link 96.1%** (294 of 306), **search
89.4%** (59 of 66). The order matches the trust the linker puts in each step, and
search is the weak one, as 04b's look at a sample suggested. Three names that exactly
one item carries stayed unlinked (*BEL*, *St Trinian's*, *Verian*).

**Reading the 20 "wrong" links** (listed with both items in the report) changes the
picture. My reading, not recorded as labels:

- 9 are right and the ground truth is wrong: the exact label belongs to a lesser item
  (*Atlanta, Georgia* is also a painting, *Paktia* an insect genus, *Beverly Hills
  90210* a soundtrack discography, *SVK* a version control system, *Galápagos Islands*
  the UNESCO site), or the name is a synonym with its own item (*Tragelaphus oryx* ->
  common eland).
- 6 are real linker errors: *SRAM* (typed organization, so the bicycle parts company)
  -> static RAM; *GER* -> a French commune; *Francisco Marto* -> the joint item of two
  saints; *Play It Again Sports* -> its parent company; *District of Columbia* ->
  Washington, D.C. (a Wikipedia redirect); *Sky Patrol* -> a list of action figures (a
  redirect to a list). Four of the six came from search.
- 5 are unclear: Wikidata has two items for one thing (*Cognosphere*, and *2028
  Democratic Party presidential primaries*, whose page points at a new duplicate item),
  a dynasty against its state (*Fatimids*, *Almohads*), or a renamed cup.

So the linker's accuracy on these names is closer to 98% than 96%, and the honest
summary is "page and link links are almost always right; search is wrong about one
time in ten".

**Wikidata support: 31.9%, a lower bound.** The matching properties are what Wikidata
models well: cast member (18), country of citizenship (12), voice actor, country,
platform, place of birth. Events have the lowest support (21%): relations to brand-new
events exist only if someone has already added them, and some have been (*Ken Paxton*
is a `candidate` of the 2026 Senate election in Texas). Wikidata has even caught up
with the main story of the day: three days later it connects *Pedro Alliana* and
Paraguay as head of state. Support also counts relations whose *phrase* is off:
*Saint Peter* "crucified" *Rome* is "supported" because Rome is his patron city and
residence.

**Labeled precision, and how far to trust the judge.** The two judge versions agree on
48 of 50 relations; version 2 moved one to "unsure" and called *Santiago Peña
"resigned as president of" Paraguay* wrong because the source says Congress did not
accept the resignation, a fair point. But reading the 50 judgements myself, about six
"correct" ones are wrong: *Cyclone Murasu* "rescheduled elections for" *Repalle* (the
election commission did, twice in the sample), *Ramayana: Part 1* "based on"
*Valmiki* (on his epic), *2026 Los Angeles mayoral election* "involves" *Karen Bass*
(vague, which the prompt calls wrong), *Mizanur Rahman Azhari* "has visa cancelled"
*Australian authorities* (garbled), *Manuck Hacobdjanian* "frequented" *Java* (the
community did). With those, precision would be about 80%, not 94%. An 8B judge is good
at "does the source mention this?" and weak at "is the subject really the one doing
it?". This is the strongest reason for the optional human labeling
(`label --manual`): a human label beats the LLM label in every report.

### Prompt iteration: extract_v1 against extract_v2

```
uv run python -m wikipulse.eval.extraction extract --run-id 03b-default-fix --events "2026-11-05T16:00:00+00:00..2026-11-05T23:00:00+00:00" --name 04c-v2 --prompt extract_v2
Extracted 233 events (0 failed) with ollama:qwen3:4b, prompt extract_v2, into data\extractions\04c-v2.jsonl. LLM: 233 calls, 118413 tokens out, 1712 s.
uv run python -m wikipulse.eval.extraction compare 04c-v1 04c-v2
```

| Measure | extract_v1 | extract_v2 |
|---|---:|---:|
| Distinct entities | 1285 | 1248 |
| Linked share | 78.1% | 77.2% |
| Relations | 825 | 823 |
| Relations between linked entities | 527 | 542 |
| Linking accuracy | 95.9% | 96.0% |
| Wikidata support | 31.9% (168) | 35.2% (191) |
| Labeled precision (judge v2) | 93.9% (46/49) | 91.8% (45/49) |
| One-word relation phrases | 215 | 197 |
| "part of" | 34 | 52 |

The re-extraction took 30 minutes (7.3 s of LLM time per event). The two samples share
26 of their 50 relations (same event, entities, phrase and revision), so 24 new
relations were labeled for v2.

**Verdict: no evidence that v2 is better.** The prompt asked for fewer, verb-phrase
relations; the model gave the same number (823 against 825), a few fewer one-word
phrases (197 against 215), and more of the vague "part of" (52 against 34). Support
rose 3.3 points with a margin of ±4 points, precision fell 2 points with a margin of
±7 points, and the 04b note showed that re-running the *same* prompt changes 6 of 15
events. None of these differences is larger than the noise. The 4B model follows the
schema closely and the prose rules loosely; a next attempt should change what the
model can't ignore (for example an `enum` of allowed relation phrases in the schema)
and be judged by human labels. The graph keeps `extract_v1`.

### Tests and checks

- New unit tests: `tests/eval/test_wikidata.py` (6, two of them on saved real answers
  of the query service), `test_extraction_report.py` (9), `test_relation_labels.py`
  (6), `test_extraction_data.py` (3). New integration test: `test_extraction_graph.py`
  (reads events back from the Docker Neo4j under its own run id, deletes only them).
- `uv run pytest`: 378 passed. `uv run pytest -m integration`: 32 passed. `ruff check`
  and `ruff format --check`: clean.
- Wikidata: 76 requests for the whole day of the 04b graph, 76 for the v2 extraction
  (other names and pairs), cached afterwards; re-running a report costs at most one.

## Questions to test your understanding

1. The report says linking accuracy is 95.9%. Give a reason why the linker's real
   accuracy can be higher than that number, and one why it can be lower.
2. Why is Wikidata support a *lower bound* on relation correctness, and why is a
   "supported" relation still not proof that the relation is right?
3. You change the extraction prompt, and labeled precision goes from 93.9% to 91.8% on
   about 50 labels each. What would you need before saying the new prompt is worse?

<details>
<summary>Answers</summary>

1. Higher: the ground truth is sometimes wrong, because the item that carries the exact
   label is a lesser one (*Atlanta, Georgia* the painting, *Paktia* the insect genus)
   or a synonym item, while the linker found the thing the article means; 9 of the 20
   "wrong" links were of that kind. Lower: only names that exactly match the label of a
   single item are checked, and those are the easy ones. The hard decisions (shared
   names like "Georgia", names without any item) are not in the number, and search,
   the weakest step, is wrong in 7 of its 66 checkable names already.
2. Lower bound: a relation counts as supported only if Wikidata already has a
   statement between the two items. News relations are often too new (Wikidata lags
   by days), most relations an article states are never Wikidata statements ("X
   criticized Y"), only truthy (best-rank) statements are asked for, and relations
   with an unlinked entity can't be checked at all. Not proof: any statement between
   the two items counts, so *Saint Peter* "crucified" *Rome* is supported by "patron
   saint", and "Peña resigned President of Paraguay" by "position held".
3. A margin of error and a sense of the noise. With 49 labels each, the 95% margin is
   about ±7 points, so a 2-point drop is well inside it; re-running the same prompt
   changes the answers for about 40% of events (04b), so the comparison needs many
   more labels, or the same relations labeled for both runs. And the labels must be
   trustworthy: here the 8B judge accepted about six relations a careful reader
   rejects, so human labels (`label --manual`) should decide.

</details>
