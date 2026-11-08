# Learning note: 05b Question-answering evals

## What was built

The GraphRAG question answering of 05a now has a scoreboard: 25 fixed questions, asked
against a frozen copy of the graph, answered by the real pipeline and graded by two LLM
judges. One command gives the numbers, and any change to retrieval or prompts can be
judged by running it again.

```
uv run python scripts/snapshot_graph.py dump        # freeze the live graph (data/snapshots/2026-11-05/)
uv run python scripts/snapshot_graph.py restore     # load it into the neo4j-eval server (port 7688)
uv run python -m wikipulse.eval.qa_questions        # 25 questions -> evals/labels/qa_questions.jsonl
uv run python -m wikipulse.eval.qa --name 05b-v1    # answer, judge, report
uv run python -m wikipulse.eval.qa --name 05b-v1 --rate   # optional: rate 15 answers yourself
```

| File | What it does |
|------|--------------|
| `scripts/snapshot_graph.py` | `dump`: stops the `neo4j` container, runs `neo4j-admin database dump` in a one-off container, starts it again, writes `manifest.json` (counts, time range, indexes, SHA-256). `restore`: `neo4j-admin database load` into the `neo4j-eval` service, waits for the indexes, checks the counts against the manifest. `info`: prints a manifest. |
| `docker-compose.yml` | New service `neo4j-eval` (profile `eval`, ports 7475/7688, own volume `neo4j-eval-data`). |
| `wikipulse/eval/qa_questions.py` | `QAQuestion` (the record), `Snapshot` (read-only access to the copy), one generator per question type (`time_questions()`, `event_questions()`, `connection_questions()`, `aggregation_questions()`, `unanswerable_questions()`), `generate()`, and the CLI. |
| `wikipulse/eval/qa.py` | The run: step `answer` (`answer_all()`, one `QASession` for all questions), step `judge` (`judge_all()`, one judge model at a time), step `report` (`run_report()`), and `--rate` (`rate_manually()`). |
| `wikipulse/eval/qa_judge.py` | `QAJudge` (correctness and faithfulness calls), the messages it sends, `parse_correctness()` / `parse_faithfulness()`, and the judgements file (`QAJudgement`, `append_judgement()`, `find_judgement()`). |
| `wikipulse/eval/prompts/correctness_v1.md`, `faithfulness_v1.md` | The two judge prompts. |
| `wikipulse/eval/qa_metrics.py` | Pure functions: `entity_hits()`, `event_hits()`, `score_question()`, `summarize()`, `failure_split()`, `cohen_kappa()`, `correctness_agreement()`, `claim_agreement()`. |
| `wikipulse/eval/qa_report.py` | `build_report()` (the JSON) and `render_markdown()`. |
| `evals/labels/qa_questions.jsonl` | The 25 questions. |
| `evals/labels/qa_judgements.jsonl` | Every judgement, append-only, with its labeler (`llm` + model + prompt version, or `human`). |
| `evals/results/2026-11-08_05b-v1.md`, `.json` | The report of the first run. |
| `wikipulse/config.py`, `.env.example` | `EVAL_QA_NEO4J_URI`, `EVAL_QA_JUDGES`, `NEO4J_EVAL_HTTP_PORT`, `NEO4J_EVAL_BOLT_PORT`. |
| `tests/eval/test_qa_*.py`, `test_snapshot_graph.py` | 58 unit tests (mocked LLM, fake snapshot) and one integration test (`test_qa_questions_graph.py`, read-only, on the snapshot). |

How a run flows:

```
live Neo4j ──dump──► data/snapshots/2026-11-05/neo4j.dump + manifest.json
                         │ restore
                         ▼
                   neo4j-eval (bolt :7688), frozen: "now" = 2026-11-05 23:22:51 UTC
                    │                                  │
   qa_questions: deterministic queries          qa.py step 1 "answer" (qwen3:8b only)
   ─► 25 questions + expected answers           QASession(neo4j_uri=snapshot).ask(q)
      + expected entities (QIDs) / events       ─► data/qa_eval/05b-v1/answers.jsonl
                                                   (answer, fact ids, all facts, timings)
                                                       │
                              step 2 "judge": judge 1 (qwen3:8b) over all 25 answers,
                              then judge 2 (qwen3:4b) over all 25
                              ─► evals/labels/qa_judgements.jsonl
                                                       │
                              step 3 "report": recall from the stored facts, judge scores,
                              refusals, latency, failure split, judge agreement
                              ─► evals/results/<date>_05b-v1.md / .json
```

## Key concepts

### Evals for a changing world: why the graph is frozen

An evaluation compares an answer with an expected answer. In WikiPulse both sides move:
the stream adds events every minute, so "What happened today?" has a different right
answer tomorrow, "Which person is involved in the most events?" changes whenever a new
event is extracted, and the graph's "now" (`QAGraph.clock()`, the newest event) moves
with it. If the evaluation asked the live graph, a score that drops from one run to the
next could mean the retrieval got worse, or only that the world changed. Two runs would
not be comparable, and the expected answers in the question file would rot within a day.

So the evaluation asks a **snapshot**: the graph as it was at one moment, never written
again. Everything that defines a right answer is then fixed: the events, entities and
relations, and also the clock, so "today" in the questions is always 2026-11-05 and
"the last two hours" always ends at 23:22:51 UTC. A rerun on the same snapshot gives the
same contexts; only the models' own randomness remains (they run at temperature 0, but
local inference is not bit-reproducible, as 04b found). This is the project's "event
time, not wall-clock time" rule applied to the evaluation data.

How it is done here (`scripts/snapshot_graph.py`):

- **A real dump.** `neo4j-admin database dump` writes the whole store, including the
  vector and full-text indexes and the embeddings of 05a, into one 4.3 MB file. Neo4j
  Community can only dump a *stopped* database, so the script stops the `neo4j`
  container, runs the dump in a one-off container on the same volume
  (`docker compose run --rm --no-deps`), and starts it again in a `finally` block. The
  live graph was down for about 25 seconds.
- **A separate database is a separate server.** Neo4j Community has exactly one user
  database per server, so `CREATE DATABASE snapshot` is not available. The snapshot is
  loaded into a second Neo4j server, the `neo4j-eval` service, with its own volume and
  port 7688. It has the profile `eval`, so a plain `docker compose up -d` doesn't start
  it.
- **Checked, not trusted.** `manifest.json` records what the graph held (1,285
  entities, 233 events, 243 pages, 2,534 relationships, 476 embedded nodes, 12
  indexes) and the dump's SHA-256. `restore` refuses a dump that doesn't match its hash
  and compares the restored graph with the manifest.
- **The QA code doesn't know.** The evaluation passes the snapshot's address as
  `neo4j_uri` to `QASession.from_settings()` (`eval_settings()`). Retrieval and
  answering run exactly the code that `wikipulse.qa.ask` runs.

For the devlog, in one sentence: *the eval asks a frozen copy of the graph so that a
change in the score means a change in the system, not in the news.*

### Generating questions from the graph itself

`wikipulse.eval.qa_questions` writes the 25 questions with deterministic queries on the
snapshot, five of each type of spec 05a. The same snapshot always gives the same file
(the integration test generates twice and compares). Subjects are picked by rules, not
by hand: the biggest events with at least four linked entities (`EVENTS_WITH_ENTITIES`),
pairs of people or organizations that share an event but have no direct relation
(`SHARED_EVENT_PAIRS`), pairs with a direct relation (`RELATED_PAIRS`), and pairs two
steps apart through a third entity and in no shared event (`TWO_HOP_PAIRS`). Each record
keeps its rule in `derived_by`.

- **time**: today, the last two hours, 20:00 to 21:00 UTC, "this afternoon (12:00 to
  18:00 UTC)", the latest news. Expected: the five biggest events of the period.
- **event**: by quoted title, by number ("event 316"), by a page title, and two
  filtered by type ("Which people ...", "Which organizations ..."). Expected: the
  linked entities.
- **connection**: two through a shared event, two through a direct relation, one two
  steps apart. Expected: the chosen connection *and every shortest connection in the
  graph* (`other_connections()`, see "Deviations").
- **aggregation**: places in at least three events, how many events started today,
  the event with the most pages, the person in the most events, how many events the
  most frequent organization is in. None of these is a query shape the Cypher prompt
  shows as an example.
- **unanswerable**: the Federal Reserve, Eurovision 2026, "yesterday", Apple, and a
  near miss: "Who was the chief executive of Ueno Bank?" (the bank is in the graph, its
  chief executive is not). The generator *checks* each one: `found_terms()` searches
  every entity name and alias, page title, description and summary, event title and
  description, and relation, and stops if the snapshot mentions what the question asks
  about.

Two choices keep the set honest. Events 341 and 496 (the Paraguayan banking crisis) are
left out of event and connection questions, because 05a tuned its prompts on them. And
the questions name entities as the graph spells them ("Democratic Party", not "the
Democrats"). That favours 05a's name matching; paraphrased questions would test vector
search harder (see "Open points").

Every record has `source: synthetic` and `labeler: llm`: the generator was written by a
coding agent and nobody has checked its picks yet. A question a person writes or
corrects gets `source: human`, `labeler: human`, and `--force` never replaces those
(`merge_questions()`).

### Retrieval vs generation failures

A wrong answer has two possible causes: the facts were never retrieved, or they were
retrieved and the model misused them. The fixes differ (a better query or traversal,
or a better prompt or model), so the report measures the two halves apart.

**Retrieval** is measured without any judge, from the facts stored with each answer:

- *entity recall*: of a question's expected entities (Wikidata IDs), the share whose
  name or an alias appears in the retrieved facts (`entity_hits()`: whole words, case
  and accents ignored; names of up to three letters such as "US" must match case);
- *event recall*: of the expected events, the share that retrieval looked at, or whose
  key or title appears in the facts (`event_hits()`).

**Generation** is measured by the judges: correctness, faithfulness and refusals.

`failure_split()` then puts every answerable question in one box: correct; wrong
although retrieval found at least 80% of what was expected (a **generation** failure);
wrong with less (a **retrieval** failure); or wrong where nothing could be checked (a
count). It is a 2×2 table in words, and it decides where to work next.

### LLM-as-judge, and how far to trust it

Each answer gets two judge calls, both at temperature 0 without thinking, with a JSON
schema that puts `reason` before the verdict (the trick from 04c: write the check
first, then the label):

- **Correctness** (`correctness_v1.md`) sees the question, the expected answer marked
  "answerable" or "not answerable", and the answer without its citation numbers
  (`judge_text()`). 2 = gives what was asked, in agreement with the reference (for a
  list: the main items); 1 = partly; 0 = wrong, or a refusal of an answerable question.
  For an unanswerable question a refusal scores 2 and an answer 0. The judge is told to
  trust the reference over its own knowledge, because the events are newer than its
  training data.
- **Faithfulness** (`faithfulness_v1.md`) sees the retrieved facts (all of them, each
  cut to 400 characters) and the answer's numbered claims, and says for each claim
  whether the facts state it. The claims are the sentences and list items of
  `Answer.fact_text`, split by 05a's own `claims()` (`answer_claims()`). Long answers
  go in batches of ten (`CLAIMS_PER_CALL`). Score: supported claims / judged claims.
  Refusals make no claims.

Correctness and faithfulness are different things, and this run shows both ways they
part. Q24 ("Who was the chief executive of Ueno Bank?") got "Santiago Peña was the
president of Paraguay [1]": faithful to its fact, and wrong (correctness 0). Q01 lists
21 events, every claim supported, but mostly not the ones a reader would call the day's
news.

**Calibration** asks whether a judge's numbers mean what they say. Without human
ratings, the next best check is a second judge: if two different models agree, the
verdict at least doesn't hang on one model's quirks (both can still be wrong the same
way). `correctness_agreement()` reports exact agreement, agreement within one point,
the 3×3 confusion matrix and **Cohen's kappa** with linear weights.
`claim_agreement()` does the same for faithfulness, claim by claim.

Kappa corrects agreement for luck: `kappa = (observed − chance) / (1 − chance)`, where
"chance" is the agreement two judges would reach by labeling at random with their own
label frequencies. 1 is perfect, 0 is no better than chance. Linear weights count a 0
against 1 as half of a 0 against 2, because the scores are ordered.

The human check is `--rate`: it shows an answer and the expected answer, asks for 0, 1
or 2, and only then shows the judge's score, so it can't anchor you. Ratings are
`labeler: human` records in the same file, and the report adds "human vs judge"
agreement as soon as there are any.

Judgements are labels, so they follow the label rules: append-only, every record names
its labeler, model and prompt version, and a human judgement is never overwritten. A
judgement is about one exact answer (`answer_hash`) and, for correctness, one exact
reference (`reference_hash`). Changing either makes the old judgement stop counting
instead of silently scoring a different text.

## Design decisions and deviations from the spec

- **"Restored into a separate database" = a second Neo4j server.** Community edition
  has one user database per server (see above), so the snapshot lives in the
  `neo4j-eval` compose service. `EVAL_QA_NEO4J_URI` points the evaluation at it.
- **"A dump of a chosen day" = the whole graph, named by its day.** The live graph holds
  only 2026-11-05 (16:00 to 23:00 UTC), so the dump of the graph *is* the dump of that
  day. The name defaults to the day of the newest event (`--name` chooses another). When
  the graph spans many days, a snapshot still freezes everything up to that moment,
  which is what makes answers repeatable. Cutting a single day out would mean deleting
  nodes in the copy, and that wasn't needed yet.
- **Questions are generated by code, not by an LLM.** The spec allows it ("derive
  expected answers ... with deterministic queries where possible"). Templates plus
  queries are repeatable and checkable, and they need no GPU time. The price is plain
  wording. The `labeler` is `llm` because the agent that wrote the generator is an LLM
  and the picks are unreviewed (the label rules only know `llm` and `human`).
- **Two extra fields per question**: `expected_events` (event keys, for time and
  aggregation questions that name no entity) and `derived_by`. Also `snapshot`.
- **Two LLM judges, both local**: `qwen3:8b` (primary) and `qwen3:4b`. Those are the two
  models on this machine. The primary judge is the same model as the answerer, so it
  grades its own model's work; the 4B judge is the independent one. `--judges` takes
  any `backend:model`, for example a larger Anthropic model later.
- **Three steps, one model at a time** (the spec's two passes, plus the report). The
  answer step runs one warm-up question first (not recorded), so the first question's
  latency isn't mostly model loading (05a measured about 40 s for that).
- **Faithfulness uses all retrieved facts**, as the spec says ("supported by retrieved
  facts"), not only the cited ones. That fits in `OLLAMA_NUM_CTX` 8192 with facts cut
  to 400 characters and ten claims per call.
- **Correct refusals are counted from the system, not the judge**: an unanswerable
  question is a correct refusal when `Answer.answerable` is false. False refusals
  (answerable questions refused) are reported next to it, because a system that always
  refuses would score 100% on the first.
- **Two fixes to the evaluation after the first judge pass**, both visible in the
  judgements file (the old lines stay):
  1. The expected answers of connection questions named only the pair's chosen
     connection, but the graph has others: Q15's answer ("both play in the Premier
     League and the FA Cup") is a true connection, and the judge gave it 0 against a
     reference that only said "EFL Cup". `other_connections()` now adds every shortest
     path between the two (as `allShortestPaths`, ≤ 4 steps). Q12 and Q15 changed; Q15
     went from 0 to 1, Q12 from 1 to 2. Correcting the reference after seeing the
     answer is a judgement call, so it is written down here: the reference was
     incomplete about the graph, not too strict about the system.
  2. Q20's answer "The Democratic Party is involved in 11 events: [F22]...[F52]" has
     no claim by 05a's `claims()`, because a line that ends in ":" (once the citation
     markers are removed) counts as a heading. `answer_claims()` then judges the whole
     answer as one claim, so a wrong count can't escape faithfulness.
  Because judgements now carry `reference_hash`, every correctness judgement was redone
  after the first fix (about 100 s per judge).
- **No new dependency.** Docker Compose and `neo4j-admin` (in the Neo4j image) do the
  snapshot; kappa is ten lines in `cohen_kappa()`.

## Results

All on 2026-11-08/09 against snapshot `2026-11-05` (233 events, 1,285 entities, 243
pages; "now" 2026-11-05 23:22:51 UTC), QA model `ollama:qwen3:8b` with prompts
`cypher_v1` and `answer_v1`, all three channels. Report:
`evals/results/2026-11-08_05b-v1.md`. The whole run took about 10 minutes: answering 3
minutes (50 LLM calls, 156 s of LLM time), each judge 1 to 2.5 minutes. Nothing had to
be scaled down for the 60-minute rule.

```
uv run python -m wikipulse.eval.qa --name 05b-v1
25 questions: correctness 1.20 of 2 (48.0% fully correct), faithfulness 95.9%, entity recall 92.3%,
event recall 82.2%, correct refusals 80.0%, false refusals 20.0%, median 4.8 s
```

| | Questions | Entity recall | Event recall | Correctness (0–2) | Fully correct | Faithfulness (claims) | Correct refusals | False refusals | Latency median / p90 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **all** | 25 | 92.3% | 82.2% | 1.20 | 48% | 95.9% (49) | 80% | 20% | 4.8 / 11.3 |
| time | 5 | n/a | 56.0% | 0.40 | 0% | 100% (25) | n/a | 40% | 5.5 / 22.9 |
| event | 5 | 100% | 100% | 1.60 | 60% | 100% (6) | n/a | 0% | 5.4 / 7.8 |
| connection | 5 | 100% | 100% | 1.60 | 60% | 92.9% (14) | n/a | 0% | 6.5 / 8.8 |
| aggregation | 5 | 66.7% | 66.7% | 0.80 | 40% | 66.7% (3) | n/a | 40% | 4.0 / 11.3 |
| unanswerable | 5 | n/a | n/a | 1.60 | 80% | 100% (1) | 80% | n/a | 2.1 / 4.5 |

Correctness and faithfulness are the primary judge's (`qwen3:8b`). The slowest
question was "What happened today?" at 22.9 s, a long list.

### Where it fails: retrieval or generation

| Outcome (answerable questions) | Questions |
|---|---|
| correct | 8: q06, q07, q09, q11, q12, q14, q16, q18 |
| wrong, retrieval complete: **generation** failure | 8: q01, q02, q05, q08, q10, q13, q15, q20 |
| wrong, retrieval incomplete: **retrieval** failure | 3: q03, q04, q19 |
| wrong, nothing to measure (a count) | 1: q17 |

Reading the answers behind the boxes:

- **All four "retrieval" failures (q03, q04, q17, q19) are generated Cypher that never
  ran**, and with no named entity and nothing similar enough for vector search, the
  context stayed empty and the system refused an answerable question (the four false
  refusals):
  - q03 and q04 ("between 20:00 and 21:00 UTC today", "this afternoon") got SQL date
    arithmetic: `$today_start + interval '1 hour'`, a Cypher syntax error. The prompt
    says no date arithmetic is needed, which is true for "today" but not for an hour
    range. A `duration()` example in `cypher_v1` is the obvious fix.
  - q19 ("Which person is involved in the most events?") ordered and limited inside a
    `WITH` and then used `ev` after it had gone out of scope: a syntax error.
  - q17 ("How many events started today?") wrote a correct query,
    `... < date($today_start) + duration({days:1})`, and **05a's validator rejected
    it**: `_check_quantifiers()` reads `) +` as a quantified path pattern ("needs an
    upper bound"). That is a false positive in `validate_cypher()`, found by this
    eval. It is not fixed here (05a code, outside this spec); see "Open points".
- **The generation failures are mostly lists that pick the wrong items.** Retrieval had
  the right events or entities in all eight, but:
  - q01, q02 and q05 (time) list many events, mostly not the biggest ones of the
    period: the context mixes the generated query's 25 rows with the period listing,
    and the model takes them in order. q02 ("last two hours") also includes events
    from before the period.
  - q08 (event) names 4 of the event's 14 entities and adds Ted Cruz, who belongs to
    another event. q10 names all 7 expected organizations and adds "Virat Garjana
    Morcha", which the graph types as an *event*: by the graph, a wrong item.
  - q13 and q15 (connection): q15 picks true connections but not the chosen one and
    adds unrelated ones; q13 is discussed under the judges below.
  - q20 ("How many events is Democratic Party involved in?") says 11. The generated
    query's `CONTAINS 'democratic party'` also matched four other entities (the 2028
    primaries, the Institutional Democratic Party, ...), 14 rows in all, and the model
    counted 11 of them. The right answer, 9, needs an exact name match.
- **Unanswerable questions: 4 of 5 refused**, all four with no facts at all, so no model
  call was needed. The near miss q24 (Ueno Bank's chief executive) found 46 facts about
  the bank and answered "Santiago Peña was the president of Paraguay", a faithful but
  wrong answer: the model didn't notice that no fact answers the question.
- **Faithfulness is high (95.9%)**: the answer prompt's "cite every sentence" and the
  code that drops uncited sentences do their job. Two of the unsupported claims: q14
  adds that Richard Allen Davis was among the 548 inmates whose sentences were commuted
  (the facts don't say so), and q20's count of 11. So the system rarely invents facts;
  its errors are in choosing, counting and refusing.

What to work on first, by the numbers: the Cypher prompt and the validator (four false
refusals, 16% of all questions, come from queries that never ran), then list
answers (8 generation failures).

### Judge agreement: qwen3:8b against qwen3:4b

| 8b \ 4b | 0 | 1 | 2 |
|---|---:|---:|---:|
| 0 | 5 | 1 | 1 |
| 1 | 1 | 5 | 0 |
| 2 | 0 | 0 | 12 |

- **Correctness**: the judges give the same score on 22 of 25 answers (88%), differ by
  at most one point on 24 (96%), Cohen's kappa with linear weights **0.82** (very good
  agreement). Mean score 1.20 (8b) against 1.28 (4b): the 4B judge is slightly more
  lenient. The three disagreements: q02 (8b 0, 4b 1), q13 (8b 1, 4b 0), and q17, where
  the 4B judge gave 2 to a *refusal* of "How many events started today?" although the
  reference says 233. That one is plainly a judge error, against its prompt's own rule.
- **Faithfulness**: the judges agree on 46 of 49 claims (93.9%), but Cohen's kappa is
  **−0.03**. This is the "kappa paradox": almost every claim is supported (96% and 98%),
  so two judges that say "supported" nearly always agree by chance alone, and the few
  unsupported claims, the only ones that matter, are exactly where they disagree (8b
  flagged q14's extra claim and q20's count; 4b flagged a different claim in q11). On
  this sample, the faithfulness *score* is stable (95.9% vs 98.0%), but *which* claims
  are unfaithful is not reliable with either judge. A sample with more unsupported
  claims, or a human, is needed to calibrate it.

My reading of the 25 correctness verdicts (not recorded as labels; those are for the
human): the 8B judge's reasons follow the prompt (what the question asks, what the
reference says, what the answer says) and most scores hold up when checked against the
graph. Even "Ted Cruz is not in the reference" (q08) and "Virat Garjana Morcha is not
in the reference" (q10) are right once you look: Cruz belongs to another event, and the
graph types the Morcha as an event, not an organization. The clear miss is q13: "James
Talarico defeated Ken Paxton" is in the retrieved facts (faithfulness says supported),
but the reference only has "part of", and the judge counted the extra true detail as a
contradiction, against its prompt. So the judge treats the reference as complete,
and a reference that leaves something out turns a true detail into an error. That is
the main reason to keep references generous (`other_connections()`) and to have a
human rate a sample.

### The human check (optional)

Not done yet: `uv run python -m wikipulse.eval.qa --name 05b-v1 --rate` asks for 15
answers, and `--steps report` then adds "human vs judge" agreement to the report.

### Tests and checks

- New unit tests: `tests/eval/test_qa_judge.py` (18), `test_qa_metrics.py` (16),
  `test_qa_questions.py` (10), `test_qa_eval.py` (9), `test_snapshot_graph.py` (5). A
  scripted LLM (`ScriptedClient`), a fake snapshot and a fake QA session: no Neo4j, no
  Ollama, no network.
- New integration test: `tests/eval/test_qa_questions_graph.py` generates the question
  set twice from the snapshot server and checks it is identical, 5 per type, with
  Wikidata IDs. It only reads, and it skips itself if `neo4j-eval` isn't running.
- `uv run pytest`: 523 passed. `uv run pytest -m integration`: 41 passed; afterwards the
  live graph still had exactly 233 events, 1,285 entities, 243 pages. `ruff check` and
  `ruff format --check`: clean.

## Open points

- **Validator false positive (05a)**: `validate_cypher()` rejects `x + duration(...)`
  after a closing parenthesis as a quantified path. Worth a small fix-up spec, with q17
  as the test.
- **Cypher prompt**: an example with `duration()` for hour ranges, and exact name
  matching (`toLower(n.name) = '...'`) before `CONTAINS`.
- **Paraphrased questions**: all 25 name entities as the graph spells them. Five
  paraphrased variants ("the Democrats", "the Paraguayan bank that collapsed") would
  measure the vector channel, and re-calibrate `QA_VECTOR_MIN_COSINE` as 05a asked.
- **Human ratings**: none yet. The 8B-vs-4B agreement says the judges agree with each
  other, not that they agree with you.

## Questions to test your understanding

1. Next week the live graph holds seven days of events. You change the Cypher prompt and
   re-run the evaluation against the *live* graph instead of the snapshot. "What
   happened today?" goes from correctness 1 to 2. What can you conclude, and what
   would you have to do to conclude more?
2. "How many events started today?" (q17) was refused, and the report files it under
   "retrieval not measurable", not under generation. Which component actually failed,
   and why is no recall number able to show it?
3. The two judges agree on 93.9% of claims for faithfulness, yet Cohen's kappa is
   −0.03. Is the faithfulness score of 95.9% useless, then? What does the kappa tell
   you, and what would raise it?

<details>
<summary>Answers</summary>

1. Almost nothing. On the live graph, "today" is a different day with different events,
   so both the context and the right answer changed along with the prompt; the
   question file's expected answer (about 2026-11-05) doesn't even apply any more. To
   attribute the change to the prompt, run both prompt versions against the same
   snapshot (`scripts/snapshot_graph.py restore`), where the clock, the events and the
   expected answers are fixed. Even then one question is one sample, and local
   inference varies a little from run to run, so look at the whole set and at
   repeated runs before believing a one-point move.
2. The Cypher channel. The model wrote a correct query (`date($today_start) +
   duration({days: 1})`), and 05a's `validate_cypher()` rejected it as a quantified path
   pattern, a false positive. With no entity named and no vector hit above the
   threshold, the context was empty and the system refused without asking the answer
   model. Recall can't show it because the question expects no entity or event: the
   answer is a number that only a query can produce. Recall only covers what retrieval
   should *find*, not what it should *compute*. The notes stored with each answer
   ("cypher: query rejected, ...") are what pointed at the cause.
3. Not useless, but limited. The kappa says that the judges' agreement on *which* claims
   are unsupported is no better than chance: with 96 to 98% "supported", agreeing by
   luck is almost certain, and the few unsupported claims are flagged by one judge and
   not the other. So the overall score (both judges say about 96 to 98%) is stable, but
   any single "unsupported" verdict needs a second look. More unsupported claims in the
   sample (questions that tempt the model to add details), a clearer prompt for edge
   cases such as counts, or human labels on the disputed claims would make the kappa
   meaningful.

</details>
