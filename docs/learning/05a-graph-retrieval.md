# Learning note: 05a GraphRAG retrieval and answering

## What was built

You can now ask the knowledge graph of stage 4 a question in plain English and get an
answer in which every claim points at the Wikipedia page and revision it came from:

```
uv run python -m wikipulse.qa.ask "How are Ueno Bank and Santiago Peña connected?"

Ueno Bank and Santiago Peña are connected through the events of the 2026 Paraguay
banking crisis [1][2][3][4]. ...

Sources:
  [1] Ueno Bank, revision 1383953174  https://en.wikipedia.org/w/index.php?oldid=1383953174
  ...
```

| File | What it does |
|------|--------------|
| `wikipulse/qa/types.py` | The data passed around: `Fact` (one statement with its `Citation`s), `Context` (all facts for a question, numbered F1, F2, ..., plus notes and timings), `Answer`. |
| `wikipulse/qa/graph.py` | `QAGraph`: every read the QA package does, always in a Neo4j READ session. Entity and page search, traversal (`entity_events`, `entity_relations`, `paths`, `event_entities`, ...), vector search (`similar_pages`, `similar_events`) and `run_query()` for generated Cypher. |
| `wikipulse/qa/mentions.py` | Which entities, pages and events a question names: `find_mentions()`, `find_titles()`, `event_references()`. |
| `wikipulse/qa/cypher.py` | Text-to-Cypher: `CypherWriter` (the LLM writes one query) and `validate_cypher()` (read-only, bounded, with a LIMIT). |
| `wikipulse/qa/retrieve.py` | `Retriever.retrieve()` / `retrieve(question)`: runs the three channels and turns what they find into facts, in priority order. |
| `wikipulse/qa/answer.py` | `Answerer.answer()` / `answer(question)`: the cited answer; `QASession` holds the driver, LLM client and embedding model for many questions. |
| `wikipulse/qa/index.py` | `uv run python -m wikipulse.qa.index`: full-text and vector indexes, embeddings of pages and events. |
| `wikipulse/qa/ask.py` | `uv run python -m wikipulse.qa.ask "question"`: the command line. |
| `wikipulse/qa/prompts/cypher_v1.md`, `answer_v1.md` | The two prompts. |
| `wikipulse/config.py`, `.env.example` | `QA_MODEL`, `QA_CYPHER_MAX_ROWS`, `QA_CYPHER_TIMEOUT_S`, `QA_VECTOR_TOP_K`, `QA_VECTOR_MIN_COSINE`, `QA_MAX_FACTS`. |
| `tests/qa/` | 61 unit tests (a fake graph, a scripted LLM) and 6 integration tests against the Docker Neo4j. |

How a question flows:

```
question
  │
  ├─ clock(): "now" = newest event in the graph (event time) ─► $now, $today_start, $week_start
  │
  ├─ graph channel (no LLM)
  │    full-text index ─► find_mentions(): entities the question names ("Ueno Bank", "Santiago Peña")
  │    event 341 / page titles / "what happened today?" ─► events to look at
  │    paths(a, b): shortest connections between each pair of entities (≤ 4 steps)
  │    entity_relations(), entity_events(); event_entities(), event_relations()
  │
  ├─ cypher channel (LLM call 1)
  │    CypherWriter.write() ─► validate_cypher() ─► run_query() in a READ transaction, 5 s, 25 rows
  │
  ├─ vector channel (no LLM; bge-small on the CPU)
  │    embed question ─► qa_event_embedding, qa_page_embedding ─► hits with cosine ≥ 0.63
  │    their events are expanded by the graph channel (entities, relations)
  │
  ▼
Context: up to 60 facts F1..Fn, each with page title + revision, plus notes
  │
  ▼
Answerer (LLM call 2): answer citing [F3] ─► check ids, find uncited sentences (one retry)
  │                                       ─► [F3] becomes [1][2] = page + revision
  ▼
Answer: text, citations, facts used, uncited sentences (if any), the context, timings
```

## Key concepts

### How GraphRAG differs from plain RAG

Plain RAG (retrieval-augmented generation) cuts documents into chunks, embeds them,
and for a question fetches the few chunks whose embeddings are closest; the LLM answers
from those chunks. That works when the answer sits *inside one chunk*: "what did the
Central Bank of Paraguay do?" is answered by the lead of one article.

It fails for questions whose answer is *spread over many facts*:

- "How are Ueno Bank and Santiago Peña connected?" The bank's article says it collapsed;
  Peña's article says he resigned. That they are linked, and how, is in no single chunk.
- "Which organizations appear in more than one event this week?" This is a count over
  the whole collection. No chunk contains it, and the chunks most similar to that
  sentence are random ones (the best cosine was 0.58, see "Results").

GraphRAG keeps the *structure* that extraction (04b) already built, nodes and
relationships, and retrieves by walking it. In this project:

- **Connections are paths.** `QAGraph.paths()` asks Neo4j for the shortest paths of at
  most 4 steps over `RELATED_TO` and `INVOLVED_IN` between every pair of entities the
  question names (`allShortestPaths`). For Ueno Bank and Santiago Peña it returns
  "Ueno Bank —involved in→ *Paraguayan president resigns after Ueno Bank collapse*
  ←involved in— Santiago Peña" and "Ueno Bank —had 2.94 million customers→ Paraguay
  ←was president of— Santiago Peña". `path_fact()` turns each path into one fact whose
  sources are the revisions of *every* step, so a two-step answer cites both pages.
- **Neighbourhoods are traversals.** For "who is involved in event X" the event's
  `INVOLVED_IN` entities and its `RELATED_TO` facts are the answer
  (`event_entities()`, `event_relations()`).
- **Aggregations are queries.** For counting and listing, the LLM writes Cypher (next
  section).

Vector search still matters: it is how a question that names no entity exactly ("the
event about the Paraguayan banking crisis") finds its way into the graph. The vector
hits are *entry points*, and the graph channel expands their events. That combination
is "hybrid retrieval", below.

### Text-to-query: the LLM writes Cypher, the code decides whether it runs

`CypherWriter.write()` sends the question, the current time and the row limit to the
model with `prompts/cypher_v1.md` as the system prompt: the graph schema (labels,
properties, relationship types and their directions), the time parameters, rules, and
four example question/query pairs. The answer is constrained by a one-field JSON schema
(`{"cypher": "..."}`).

A generated query is untrusted input, like text typed into a web form. The model can
simply be wrong ("DETACH DELETE" would empty the graph), or be steered by text in the
data it has read (prompt injection). Three layers make it harmless:

1. **`validate_cypher()`** blanks out strings, comments and `quoted` names
   (`blank_out()`, so `WHERE n.name = 'Set It Off'` is fine) and then rejects: any
   write or admin clause (`CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, `DROP`,
   `FOREACH`, `LOAD CSV`, `CALL`, `SHOW`, ...), more than one statement, a query that
   does not start with `MATCH`/`OPTIONAL MATCH`/`WITH`/`UNWIND`/`RETURN` or has no
   `RETURN`, and paths without an upper bound or longer than 6 steps: variable-length
   patterns (`-[*]-` may walk the whole graph) and quantified path patterns (`-[:R]-+`,
   `-->*`, `-{1,}`, `((a)-->(b))+`, and with them `SHORTEST` searches without a
   bound). Then it forces a result limit on every branch of a `UNION`: a branch
   without a final `LIMIT` gets `LIMIT 25`, a larger limit is lowered. `CALL` is refused entirely,
   because procedures can write and `CALL { ... } IN TRANSACTIONS` is a bulk-write tool.
2. **The database refuses too.** `QAGraph.run_query()` runs the query in a session
   opened with `READ_ACCESS`; Neo4j then rejects any write ("Writing in read access
   mode not allowed"), whatever the validator missed. The integration test
   `test_neo4j_itself_refuses_writes_from_a_generated_query` sends a `CREATE` past the
   validator on purpose and checks that nothing was written. The query also gets a
   transaction timeout (`Query(cypher, timeout=5)`, `QA_CYPHER_TIMEOUT_S`; test
   `test_a_slow_query_is_stopped_by_the_timeout`), and the client reads at most
   `QA_CYPHER_MAX_ROWS` rows whatever the query says.
3. **Results are data, not instructions.** Rows become facts that the answering model
   reads; nothing in a row is ever executed.

Why both 1 and 2? The validator is a word filter, and word filters have gaps; the READ
session is enforced by the database but limits neither size nor time. Each covers what
the other can't. The validator also says *why* ("variable-length pattern needs an upper
bound"), and that goes into the context's notes.

What the model got wrong on this graph, and what helped:

- "What happened today?" first came back with an extra `AND ev.started_at <
  date($now)`: a datetime compared with a date, so 0 rows. The prompt now says to
  compare the parameters directly with `started_at` and that no other date arithmetic
  is needed; the query then followed the example.
- "Who is involved in the event about the Paraguayan banking crisis?" produced
  `(ev:Event)-[:INVOLVED_IN]->(n:Entity)`, the wrong direction, so 0 rows. An example
  for "who is involved in ..." was added, but wrong directions and too-literal title
  matches (`CONTAINS 'paraguayan banking crisis'`) still happen; for that question the
  graph and vector channels carry the answer.
- The prompt's examples use invented names ("Acme Bank", "the Riverside flood").
  Examples about real entities of this graph would make the acceptance questions look
  better than the method is.
- Generated Cypher is cited through what it returns. The prompt asks for event keys
  and, for relations, `source_title`/`source_revision`. `row_sources()` uses a row's own
  provenance; otherwise `row_event_keys()` finds event keys anywhere in the row, and the
  row cites the top page of each of its events (at most 3 citations per row).
  Columns that hold only event keys are left out of the fact's text (`query_fact()`):
  they serve the citations, and long lists of keys confused the answering model (see
  "Results").

### Hybrid retrieval: graph traversal plus vector search

Neo4j has vector indexes built in (`CREATE VECTOR INDEX ... OPTIONS {indexConfig:
{vector.dimensions: 384, vector.similarity_function: 'cosine'}}`), so the vectors live
next to the graph and one database answers both kinds of lookups.
`wikipulse.qa.index` embeds, with the same local model as event grouping
(`BAAI/bge-small-en-v1.5`, `wikipulse.events.embed.Embedder`):

- every `Page`: title, short description and summary (`page_text()` from 03b), stored
  as `Page.embedding`;
- every `Event`: its LLM-written title and description and its page titles
  (`event_text()`), stored as `Event.embedding`.

Each node also gets `embedding_hash` (a hash of the model name and the text), so a
second run only embeds what changed.

At question time `Retriever._vector()` embeds the question **with bge's query
instruction** ("Represent this sentence for searching relevant passages: "), because
bge models are trained to compare a short *query* with longer *passages* that way, and
asks both indexes for their top 5 (`db.index.vector.queryNodes`). Neo4j reports a
score of (1 + cosine) / 2; `cosine_from_score()` converts it back, so the threshold
reads like the cosines of 03b.

Hybrid here means three things working together:

1. **Vector hits are entry points into the graph.** An event or page found by vector
   search becomes a seed, and the graph channel expands the first three seed events
   with their entities and relations (`Retriever._assemble()`). "Who is involved in the
   event about the Paraguayan banking crisis?" names no entity of the graph exactly
   (only the unlinked "Paraguayan banking crisis"); vector search finds events 496 and
   341 (cosine 0.71) and the page *2026 Paraguay banking crisis* (0.80), and traversal
   lists the entities of both events.
2. **Exact names go through a full-text index, not embeddings.** A Lucene full-text
   index over entity names and aliases (`qa_entity_names`, analyzer `standard-folding`,
   so "pena" finds "Peña") proposes candidates that share a word with the question, and
   `find_mentions()` keeps those whose whole name or alias appears in the question as a
   run of words, longest first ("Democratic Party", not also "Party"; "US" only when
   written in capitals, so "tell us" doesn't name the United States). Embeddings are
   good at "about the same thing" and bad at "exactly this name".
3. **A threshold says "nothing relevant".** Vector search always returns *something*.
   `QA_VECTOR_MIN_COSINE` (0.63) drops hits that are only the least unrelated; how it
   was chosen is in "Results".

### Grounded generation: answers that cite their revisions

Grounding means the model may only say what the retrieved facts say, and the reader can
check every claim. The design puts the checking in code, not in the model's goodwill:

- **Facts carry provenance.** Every `Fact` has `sources`: the page title and revision
  that 04b stored on the relationship it came from (`INVOLVED_IN.source_revision`,
  `RELATED_TO.source_revision`) or, for an event, the newest burst revision of its two
  highest-scoring pages (`PART_OF.revision`).
- **The model cites ids, not pages.** `build_message()` shows the facts as `[F1] ...`;
  the prompt (`answer_v1.md`) demands fact ids at the end of every sentence and list
  item. Copying "[F7]" is much easier for a model than copying "revision 1383953174".
- **Code checks the citations.** `drop_unknown_ids()` removes ids that don't exist;
  `uncited_sentences()` finds sentences and list items without any id, however short
  (headings such as "Events:" or "**Events**" and a lone "Yes." don't count, and
  "U.S." does not end a sentence). If there are any, the model gets one retry that
  names them. Whatever is still uncited is removed from the answer
  (`drop_sentences()`) and listed in `Answer.removed_sentences`, which the CLI prints.
  A sentence that cites only facts without a source (a query row naming no event)
  stays, but is listed in `Answer.uncited_sentences` as a warning: its citation would
  show no page. An answer that claims to
  answer but cites nothing at all becomes a refusal (`NOTHING_CITED`): nothing in it
  could be checked.
- **Code resolves the citations.** `resolve_citations()` turns each run of fact ids
  into the sorted numbers of their sources (`[F2][F1]` -> `[1][2]`) and builds the
  sources list with permanent links (`https://en.wikipedia.org/w/index.php?oldid=...`
  shows exactly the version a fact was read from). `Answer.fact_text` keeps the
  version with fact ids, for 05b's faithfulness measure.
- **Refusing is a real answer** (requirement 2). With no facts at all, `answer()`
  refuses without calling the model (`NO_FACTS`). With facts, the model sets
  `"answerable": false` when they don't answer the question. The notes ("no connection
  between A and B within 4 steps", "vector: nothing similar enough") are shown to the
  model as "search notes (not facts, do not cite)", so it can tell *why*.

### Event time, again

"What happened today?" needs a "today". Following the project's rule, it is not the
wall clock: `QAGraph.clock()` returns the newest start or end of any event in the graph,
and `time_parameters()` derives `$now`, `$today_start` (midnight UTC of that day) and
`$week_start` (seven days earlier). On the replayed graph, "today" is 5 November 2026,
the day that was replayed, not 8 November when the question was asked. `--as-of`
overrides it. The wall clock is used only when the graph is empty.

## Design decisions and deviations from the spec

- **Model: `qwen3:8b` for both LLM steps** (new setting `QA_MODEL`). The 8B model writes
  better Cypher and follows the citation rules more closely than the 4B one, and it
  fits the 8 GB GPU at `OLLAMA_NUM_CTX` 8192 (6.2 GB in `ollama ps`). Using one model
  for the query and the answer means a question never swaps models on the GPU. Both
  calls run at temperature 0 with a JSON schema and thinking off.
- **Graph traversal needs no LLM call.** Entity lookup is a full-text index plus exact
  word matching (`mentions.py`), not an LLM asked "which entities does this question
  name?". That saves a call per question, is deterministic and is unit-tested. The
  price: a name must appear in the question as the graph spells it (accents and case
  don't matter; "Peña" as an alias does). Paraphrases are left to vector search.
- **A fourth way into the graph: the time window.** For general questions ("what
  happened today?", "any news this week?") that name nothing, `time_window()` lists the
  biggest events of the period by pages touched (`QAGraph.events_between()`). This is
  traversal anchored on time rather than on an entity, and it makes the most common
  question independent of the LLM writing correct Cypher (in the first run, the
  generated query for it returned 0 rows). Events the generated query already lists are
  not listed twice (`_listed_by_query()`). Specific questions ("which organizations ...
  this week") don't trigger it.
- **Events are embedded too, not only page summaries** (the spec says page summaries).
  A page summary describes the *topic* ("Ueno Bank was a Paraguayan bank ..."), the
  event's LLM-written title and description describe the *news* ("Paraguayan president
  resigns after Ueno Bank collapse"). Questions are usually about the news. Both
  indexes are searched and their hits feed the same seeds.
- **New indexes and properties, nothing destructive.** `wikipulse.qa.index` adds two
  full-text indexes, two vector indexes and the properties `embedding`,
  `embedding_hash`, `embedding_model` on `Page` and `Event` nodes. It never changes the
  graph's facts. The graph runner (04b) does not call it, so after new events are
  extracted, `wikipulse.qa.index` has to be run (it embeds only what changed). Until
  then, new events are still found by the graph and Cypher channels, just not by vector
  search. Scheduling it is a job for 06b/06c.
- **The vector threshold is a calibrated number, not a guess.** See "Results".
- **The context is capped and ordered.** At most `QA_MAX_FACTS` (60) facts, added in
  priority order: connection paths, query rows, events, the named entities' relations
  and events, the seed events' entities and relations, then page summaries. Duplicates
  (the same relation found by two channels) are kept once (`Context.add()`).
- **Page summaries are cut to 400 characters** in facts, and one page found in two
  events is one fact naming both (`page_fact()`).
- **Four Cypher examples, not two** (the design notes suggest two): "what happened
  today", "which people appear in more than one event this week", "who is involved in
  ..." and "what is X related to". Each shows one pattern: the time parameters,
  aggregation with `collect`, the direction of `INVOLVED_IN` (added after the model
  reversed it), and returning provenance columns. To be fair about it: the aggregation
  example (people in more than one event) is close to acceptance question 4
  (organizations), and "what happened today?" is an example word for word. The pattern
  is taught, not the answer, but 05b's questions should include query shapes the
  prompt does not show.
- **Prompts are files with a version** (`cypher_v1`, `answer_v1`), like 04b's
  extraction prompts, so 05b can compare versions. Both were changed during this spec
  (see "Results"); the version stays `v1` because no answers were stored under the
  earlier wording.
- **`retrieve()` and `answer()` are the spec's functions**; they build everything from
  settings for one question. For many questions, `QASession` builds the Neo4j driver,
  the LLM client and the embedding model once (the embedding model takes seconds to
  load; 05b, 05c and 05e should hold one session).
- **Channels can be switched off** (`Retriever(channels=...)`, `ask --channels`), as the
  design notes ask ("start with graph traversal only, then add Cypher, then vector
  search, measuring each with spec 05b"). 05b does not exist yet, so the measurement
  here is the five acceptance questions per channel (see "Results"), not a score.
- **What a refusal looks like.** With no facts: "The knowledge graph has no facts about
  this question." (no model call). With facts that don't answer: the model's own
  one-sentence refusal, without citations.
- **No new dependency.** Neo4j's vector and full-text indexes and the embedding model
  from 03b cover everything.

## Results

All on 2026-11-08 against the 04b graph (grouping run `03b-default-fix`: 233 events of
2026-11-05, 16:00 to 23:00 UTC, 1,285 entities, 243 pages), `QA_MODEL=ollama:qwen3:8b`
on the RTX 5060 Laptop GPU (8 GB). "Now" in the graph is 2026-11-05 23:22:51 UTC.

### Indexing

`uv run python -m wikipulse.qa.index`: the first run embedded 243 pages and 233 events
and created the four indexes in 96 s (most of it loading the embedding model and its
Hugging Face checks; the embedding itself takes seconds). A second run: "Embedded 0
pages and 0 events", 24 s.

### Calibrating the vector threshold

The best cosine between a question and *any* page or event is never near 0: unrelated
English texts sit around 0.45 to 0.6 with this model (03b saw the same). So the
threshold has to be measured. Six questions about stories that are in the graph and six
that are not (`data/qa/cosines.py`, best hit of either index):

| Questions about stories in the graph | best | Questions about other things | best |
|---|---:|---|---:|
| Paraguayan banking crisis | 0.80 | Federal Reserve interest rates | 0.54 |
| Texas Senate election | 0.76 | Eurovision Song Contest 2026 | 0.62 |
| California death sentences | 0.79 | weather in Tokyo | 0.51 |
| SAFF Championship | 0.71 | a new smartphone | 0.52 |
| Gothenburg orchestra (Blomstedt) | 0.64 | "What happened today?" | 0.59 |
| anti-CEC protests in India | 0.77 | organizations in more than one event | 0.58 |

Every relevant question has a hit at 0.64 or more, every unrelated one stays at 0.62 or
less, so `QA_VECTOR_MIN_COSINE` is 0.63 (the first guess, 0.60, let Eurovision's sport
pages through). The gap is narrow: twelve questions are a sanity check, not a
measurement, and 05b should re-calibrate it on its question set. A threshold this close
to the noise is also why the answering model must still be able to refuse.

### The five acceptance questions

Command per question: `uv run python -m wikipulse.qa.ask "<question>" --show-context`
(the run used one `QASession` for all of them; logs in `data/qa/final3.log`, not
committed). Checked against deterministic Cypher queries on the graph. The run on the
final code (`final3`) repeated the one before it (`final2`) answer for answer, except
that question 3 was worded differently and cited one more revision.

| # | Question (kind) | Answer | Check against the graph | Citations |
|---|---|---|---|---:|
| 1 | "What happened today?" (time) | 5 events: the US elections (16 pages), Peña's resignation after the Ueno Bank collapse (5 + 3 pages), California's death-sentence commutations (3), the Youth Olympics badminton (2), Salon closing (2) | All are events of 2026-11-05 and the descriptions match the graph. Not the 5 *biggest*: the SAFF Championship (4 pages) and the Senate races (3) were left out | 6 |
| 2 | "Who is involved in the event about the Paraguayan banking crisis?" (event X) | 11 entities: Santiago Peña, Central Bank of Paraguay, Ueno Bank, Visión Banco, Paraguay, Pedro Alliana, Authentic Radical Liberal Party, Catholic University of Asunción, Colorado Party, Paraguayan Football Association, CrediCentro | All 11 are `INVOLVED_IN` events 341 or 496 (the two events of that story); the two events have 22 distinct entities, so the list is correct but incomplete | 6 |
| 3 | "How are Ueno Bank and Santiago Peña connected?" (connection) | Both are part of the 2026 Paraguay banking crisis events: the bank's collapse triggered the crisis that led to Peña's resignation | Three shortest paths exist (two shared events, and via *Paraguay*); the answer uses the shared events and names both | 5 |
| 4 | "Which organizations appear in more than one event this week?" (aggregation) | 22 organizations, Democratic Party (9 events) first | The generated query is the right one (type organization, `size(events) > 1`, since `$week_start`). 29 organizations qualify; the query returned the maximum of 25 rows (noted), and the answer named 22 of them, all correct; it skipped Democrats Abroad, Paraguayan Football Association and Central Bank of Paraguay | 35 |
| 5 | "What did the Federal Reserve decide about interest rates today?" (unanswerable) | "The knowledge graph has no facts about this question." | No entity named, generated query returned 0 rows, best vector hit 0.54: no facts, so a refusal without a model call | 0 |

Every sentence of answers 1 to 4 ends with a citation; `uncited_sentences` was empty
for all of them (one retry was needed for an extra question in a later smoke test,
"How are Ken Paxton and the Democratic Party connected?"). Extra questions in the same
run: Ken Paxton and the Democratic Party (connected through the event "Democratic
candidates win key Senate races in Mississippi and Texas", correct), "Who won the
Eurovision Song Contest 2026?" (refused: the graph only knows Eurovision 2022, and its
best vector hit, 0.62, is below the threshold), "Who is involved in event 458?" (the
California commutation event's entities, correct).

So: questions 3, 4 and 5 are fully right; 1 and 2 are right in everything they say and
incomplete (the model picks a few items when there are many). That is the main
remaining weakness, a generation one, not retrieval: the facts for all 25 events and
all 22 entities were in the context.

Times: 2 to 8 s per question when warm (Cypher about 2 s, answer 2 to 6 s, graph and
vector steps under 0.2 s). The first question of a session takes about 40 s: 25 s for
loading the embedding model and the rest for loading `qwen3:8b`. The longest answer
(the list of 22 organizations) took 4 s, a 25-item list of events up to 31 s.

### Each channel on its own (the design notes' "measure each")

Same five questions, `--channels` restricted (`data/qa/only-*.log`). Y = correct and
cited, P = correct but partial, N = no useful answer.

| Question | graph | cypher | vector | graph + cypher | all three |
|---|:-:|:-:|:-:|:-:|:-:|
| 1 What happened today? | Y (14 events) | Y (13 events) | N (refused) | P (6 events) | P (5 events) |
| 2 Who is involved in the Paraguayan banking crisis event? | N (1 fact) | N (0 rows) | P (3 entities) | N (refused) | P (11 entities) |
| 3 How are Ueno Bank and Santiago Peña connected? | Y | N (0 rows) | Y (via event descriptions) | Y | Y |
| 4 Organizations in more than one event this week | N | Y (18) | N | Y (24) | Y (22) |
| 5 Federal Reserve (unanswerable) | Y (refused) | Y | Y | Y | Y |

What this shows:

- **No channel answers everything.** Traversal handles connections and time, Cypher
  handles aggregation, vector search handles questions that don't name the graph's
  entities. Only the combination answers all five.
- **Hybrid is more than the sum.** For question 2, vector search alone finds the two
  events but only their descriptions (3 entities); the graph alone finds only an
  unlinked entity named "Paraguayan banking crisis"; together, vector search supplies
  the events and traversal their 22 entities.
- **More context is not always a better answer.** For question 1 the graph channel's
  compact event facts gave a 14-event answer; with the generated query's 25 wordier rows
  the model wrote 5 or 6 events (13 with the query alone). Run-to-run variation is part
  of it: in an earlier run, from the same 25 rows (then still showing their event
  keys), it listed all 25. The 04b note found the same instability for extraction.

### Prompt iterations that made a difference

Measured by re-asking the same questions (`data/qa/run1.log` to `final2.log`):

- `cypher_v1`, rule "compare `$today_start` directly with `started_at`": "What happened
  today?" went from 0 rows to the right 25.
- Answer prompt rule 7 ("query results are rows ... list them all, by the name in the
  row") did nothing on its own (re-answering the saved context gave the same 10-item
  answer, with *Herbert Blomstedt* listed as an "organization" because seven orchestra
  rows shared his events). Hiding the event-key columns from query facts
  (`query_fact()`) did: the next answer listed 22 organizations and no Blomstedt. The
  lists of event keys and titles made the rows look like they were *about* the events.
- Merging runs of citations (`[F2][F1][F2]` -> `[1][2]`) and capping query-row
  citations made answers readable: the first run printed 54 citation numbers after one
  sentence.

### Tests

- `tests/qa/`: 61 unit tests (a fake graph and a scripted LLM; no Neo4j, no network):
  the validator on 14 kinds of writes and 8 other rejections (`test_cypher.py`), name
  matching (`test_mentions.py`), the three channels, priorities, notes and the time
  window (`test_retrieve.py`), citation resolution, retries and refusals
  (`test_answer.py`).
- 6 integration tests against the Docker Neo4j (`test_qa_neo4j.py`): a path through a
  shared entity with both revisions as sources, "today" in event time, Neo4j refusing a
  write in the READ session, the timeout, the row limit, and vector search plus a
  (mocked) answer citing the page's revision. Each test writes its own made-up events
  (unique run id, 2031 dates, invented page titles) and deletes only them; afterwards
  the graph had exactly its 233 events, 1,285 entities and 243 pages again.
- `uv run pytest`: 439 passed. `uv run pytest -m integration`: 38 passed. `ruff check`
  and `ruff format --check`: clean. (After the review fixes below: 465 and 40.)

### Review fixes

The review (`docs/reviews/05a-graph-retrieval.md`) found four medium problems; all are
fixed.

1. **The forced LIMIT could be fooled.** `_with_limit()` used to search the raw query,
   so `RETURN n.name // LIMIT 1000` counted as limited (the LIMIT was in a comment), and
   in a `UNION` only the last branch got one. It now works on `mask()`, a version of
   the query where comments and strings are blanked but every character keeps its
   position, splits the query at top-level `UNION`s (`_branches()`), and gives each
   branch its own final LIMIT. A LIMIT inside a subquery's braces (`COUNT { ... LIMIT
   5 }`) or followed by another clause does not count.
2. **Quantified paths slipped through.** Neo4j 5 also writes repetition after a
   relationship: `(a)-[:R]-+(b)`, `-->*`, `-{1,}`. `_check_quantifiers()` now finds a
   quantifier after `-`, `>` or `)`, and treats it like `[*..n]`: it needs an upper
   bound of at most `MAX_HOPS` (`{1,3}` and `{,4}` pass, `+`, `*` and `{1,}` don't).
   `SHORTEST` needs no rule of its own: its pattern is quantified, so an unbounded one is
   caught. I kept `MAX_HOPS` (6) as the one limit for both kinds of pattern; aligning it
   with traversal's 4 is the separate low problem 5.
3. **Short uncited claims escaped.** `claims()` skipped pieces under three words, so
   "Peña resigned." or a list item "- Ueno Bank" needed no citation. Now every sentence
   or list item with a letter in it is a claim (`is_claim()`), except headings and a lone
   "Yes."/"No.". And what is still uncited after the retry is now *removed* from the
   answer (`drop_sentences()`), listed in `Answer.removed_sentences` and printed by the
   CLI, instead of staying in the text with a warning.
4. **Cut-off lists looked complete.** When the generated query returns
   `QA_CYPHER_MAX_ROWS` rows, or the "today"/"this week" listing returns its maximum,
   retrieval adds a line to `Context.limits` (no longer a search note). The model sees
   it under "Incomplete lists (do not present these lists as complete)", rule 10 of
   `answer_v1.md` asks it to say "include" rather than present the list as complete,
   and the code appends it to the answer as a "Note: ..." line, so the user sees it
   without `--show-context`.

Re-run of the five acceptance questions with `qwen3:8b` (`data/qa/review-fixes.log`):
no regression. Q1 lists 11 events of the day, now including SAFF Championship and the
Senate races, and ends with both limit notes. Q2 names 13 entities, now including Mario
Abdo Benítez and Carlos Carvallo, all cited. Q3 is the same connection through the two
banking-crisis events. Q4 lists 22 organizations, introduced as "... include:", and
ends with "Note: The query results stop at the limit of 25 rows; there may be more."
Q5 still refuses with 0 facts and no model call. Two answers needed the one retry; no
sentence had to be removed.

## Questions to test your understanding

1. Plain RAG over the same page summaries would embed "How are Ueno Bank and Santiago
   Peña connected?" and fetch the five closest summaries. Why can that still give a
   worse answer than `QAGraph.paths()`, even if both pages are among the five?
2. The validator rejects `SET`, `DELETE` and `CALL`, and the query runs in a READ
   session. Name one thing each layer protects against that the other does not.
3. With `QA_VECTOR_MIN_COSINE` at 0.55 instead of 0.63, what would change for "Who won
   the Eurovision Song Contest 2026?", and which part of the system would then have to
   prevent a wrong answer?

<details>
<summary>Answers</summary>

1. The two summaries describe a bank and a politician; neither states the link, so the
   model would have to infer it (and might invent one). The graph has the link as data:
   both entities are `INVOLVED_IN` the same events, and `Ueno Bank —had 2.94 million
   customers→ Paraguay ←was president of— Santiago Peña`. `path_fact()` hands the
   model the connection itself, with the revisions of every step as sources, so the
   answer can cite exactly the facts that make up the connection.
2. The validator stops what a READ session allows: unbounded variable-length patterns,
   missing limits, procedure calls that only read but may be expensive or leak
   information (`CALL dbms...`), and it explains the rejection in a note. The READ
   session stops what a word filter can miss: any write the validator's word list or
   string-blanking fails to see, now or after a future change to the validator, is
   refused by Neo4j itself. Together with the timeout, a bad query can neither change
   the graph nor run for long.
3. Its best vector hits (cosine 0.60 to 0.62: a Paris Masters page, Youth Olympics
   events) would pass, so the context would hold sport facts instead of being empty.
   Then the answer would no longer be a refusal without a model call; the answering
   model would have to see that none of the facts is about Eurovision 2026 and set
   `"answerable": false` (prompt rule 3 and 4). In run 3, with the threshold at 0.60,
   it did exactly that, but it is a model's judgement, not a guarantee, which is why a
   calibrated threshold is the first line.

</details>
