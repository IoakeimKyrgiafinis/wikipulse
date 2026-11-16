# Learning note: 12a Better Cypher generation for question answering

## What was built

05b's evaluation found that 4 of its 25 questions failed because the Cypher query the
LLM wrote never ran. With no query result, and no entity named in the question, the
context stayed empty and the system refused questions it could have answered. This spec
fixes those failures in three places and measures the result on the same frozen graph:

```
question ─► CypherWriter.write()         prompt cypher_v2 (new; cypher_v1 still selectable)
              │
              ▼
          validate_cypher()  ── rejected ──┐   (fixed: arithmetic after ")" is no path)
              │                            │
              ▼                            ▼
          QAGraph.run_query() ── error ──► CypherWriter.repair(): the model sees its query
              │                            and the error, one more try (QA_CYPHER_REPAIRS)
              ▼
          rows ─► facts                    Context.cypher_failures keeps every failure
```

| File | What changed |
|------|--------------|
| `wikipulse/qa/cypher.py` | `_is_arithmetic()` / `_group_start()`: a `+`, `*` or `{..}` after `)` only counts as a path quantifier when the bracketed group holds a relationship. `_ARROW_CHARS`: the other dash and arrow characters Neo4j accepts are checked like `-`, `<`, `>`. `PROMPT_VERSIONS`, `load_prompt()` refuses unknown versions. `CypherWriter.repair()` and `repair_message()`. |
| `wikipulse/qa/prompts/cypher_v2.md` | The new prompt: a "Time" section (durations on the parameters), a "Names" section (exact match on name or alias), rules on `WITH` scope, counting and "the most", eight examples on invented names. |
| `wikipulse/qa/retrieve.py` | `Retriever._run_generated()`: write, validate, run; on a rejection or a Neo4j error, at most `cypher_repairs` repairs. `RetrievalConfig.cypher_repairs`. |
| `wikipulse/qa/types.py` | `Context.cypher_prompt`, `cypher_repairs`, `cypher_ran`, `cypher_failures`. |
| `wikipulse/config.py`, `.env.example`, `docs/usage.md` | `QA_CYPHER_PROMPT` (default `cypher_v2`), `QA_CYPHER_REPAIRS` (0 or 1, default 1). |
| `wikipulse/eval/qa.py` | `--cypher-prompt`, `--cypher-repairs`; answer records keep prompt, repairs, whether the query ran and why tries failed. |
| `wikipulse/eval/qa_metrics.py`, `qa_report.py` | `cypher_outcome()` (ran / repaired / failed / none; older records are read from their notes), a "Failed queries" column and a line on repaired and failed queries in every QA report. |
| `wikipulse/eval/qa_compare.py` | New: `uv run python -m wikipulse.eval.qa_compare --name ... before.json after.json` puts runs side by side, with after − before and a paired-bootstrap margin. |
| `tests/qa/test_cypher.py`, `test_prompts.py`, `test_retrieve.py`, `tests/eval/test_qa_*.py` | The regression test, the leak test, repair tests, report and comparison tests. |
| `evals/results/2026-11-16_12a-before.*`, `12a-after.*`, `12a-cypher.*` | The two runs and the comparison. |

## Key concepts

### Text-to-query generation: why schema-grounded examples and error feedback help

A small model like `qwen3:8b` has seen far more SQL than Cypher. When a question asks
for something the prompt doesn't show, it falls back on what it knows best. All three
failures that still happened in the baseline run (`12a-before`) are that kind of
fallback:

- q03 and q04 ("between 20:00 and 21:00 UTC today", "this afternoon (12:00 to 18:00
  UTC)") got `$today_start + interval '1 hour'`, which is PostgreSQL, not Cypher. The
  v1 prompt said "no other date arithmetic is needed". That is true for "today", but it
  doesn't tell the model what to write for an hour range.
- q19 ("Which person is involved in the most events?") counted inside a `WITH` and then
  used `ev` after the `WITH` had dropped it. SQL has no such scoping rule, so the model
  never learned it.

Few-shot examples work because the model copies their *shape*: which parameters, which
functions, where to aggregate. `cypher_v2` therefore adds one example per missing shape,
all written against the schema with invented names:

- **time ranges**: `$today_start + duration({hours: 6})` to `duration({hours: 9,
  minutes: 30})` ("this morning, from 06:00 to 09:30 UTC"), and `$now -
  duration({minutes: 45})` ("the past 45 minutes"), plus a rule that names the SQL
  forms that don't exist;
- **exact names**: `toLower(n.name) = 'acme bank' OR any(a IN coalesce(n.aliases, [])
  WHERE toLower(a) = 'acme bank')`. v1 taught `CONTAINS`, so for q20 "Democratic Party"
  also matched "Institutional Democratic Party" and the 2028 primaries, and the answer
  said 11 events instead of 9;
- **ranking in one `WITH`**: count and collect in the same `WITH`, then sort.

**Error feedback** is the second tool. Neo4j's error messages are precise ("Variable `ev`
not defined (line 6, column 71)"), and so are the validator's ("quantified path pattern
... needs an upper bound"). `CypherWriter.repair()` sends a short conversation: the
original request, the model's own answer `{"cypher": "..."}`, then "That query could not
run: <error>". The repair is **bounded** (`QA_CYPHER_REPAIRS` is 0 or 1, enforced by
config validation), **logged** (each failure is a note, a log line and an entry in
`Context.cypher_failures`), and **still safe**: the corrected query goes through
`validate_cypher()` and the READ session like any other. A test
(`test_the_repair_is_bounded_and_an_unsafe_query_still_never_runs`) checks that a model
that keeps writing `DETACH DELETE` gets exactly one repair and nothing reaches the graph.

On the development questions (below) the repair fixed one of two failing queries: "Who
appears in more than two stories?" failed with `Variable ev not defined` and the
corrected query ran. On the evaluation set it was never needed, because every v2 query
ran the first time. So the eval numbers are the prompt's effect. The repair is a safety
net that this question set didn't exercise.

### The validator's false positive, and staying strict

`_check_quantifiers()` looked for `+`, `*` or `{n,m}` after `-`, `>` or `)`, because
Neo4j 5 writes repeated path patterns that way: `((a)-->(b))+`. But `)` also ends every
function call, so in `date($today_start) + duration({days: 1})` the `) +` looked like an
unbounded quantified path, and q17's query was rejected.

The fix uses a rule of Cypher itself: a quantified group must contain a relationship.
`_is_arithmetic()` finds the `(` that the `)` closes (`_group_start()`) and looks inside
for any piece of a relationship (`--`, `-[`, `]-`, `->`, `<-`, with spaces allowed).
`date($today_start)` has none, so the `+` is arithmetic. `((a)-->(b)) +` has one, so it
is still checked. In doubt (an unbalanced bracket, or any arrow inside, as in
`size((a)-->()) * 2`), the quantifier is checked, so the error stays on the strict side.
`test_quantified_groups_with_a_relationship_are_still_checked` covers spaces, nesting,
`- -` and juxtaposed groups. Every existing safety test still passes unchanged.

While checking what Neo4j accepts (with `EXPLAIN` on the eval server), I found a real
gap that 05a's validator had: Neo4j also accepts an en dash, a minus sign and other
dashes as relationship lines, so `MATCH (a)–[*]–(b)` (en dashes) passed the validator
with an unbounded pattern. `_ARROW_CHARS` now maps those characters to ASCII before the
path checks (`test_variable_length_patterns_with_other_dash_characters_are_checked`).
The READ session would still have stopped any write, but not a walk over the whole
graph.

The validator fix alone did not fix q17. In `12a-before` (v1 prompt, fixed validator)
the query ran and returned **0**, because `ev.started_at < date($today_start) + ...`
compares a datetime with a date, which matches nothing in Cypher. The answer was "0
events started today". The v2 rule "never wrap the parameters in `date()`" and the
`duration` examples fixed it: "233 events started today", correct. A query that runs
isn't the same as a query that's right.

### Prompt versioning and eval-driven iteration without overfitting

The risk with a fixed eval set is that you tune the prompt until it passes those 25
questions and nothing else. Four habits kept that in check:

1. **A new version, not an edit.** `cypher_v1.md` is unchanged and selectable
   (`QA_CYPHER_PROMPT=cypher_v1`, `--cypher-prompt cypher_v1`). Every answer record
   stores the prompt version and the repair count, so a report can't mix them up.
2. **Examples from the schema, not from the eval.** All names are invented ("Acme
   Bank", "the Riverside flood"), all times differ from the eval's, and the event key in
   the schema description is a made-up `run-name:12`. `tests/qa/test_prompts.py` checks
   it: no eval question, no expected answer, no name from an expected answer ("Ueno
   Bank", "Democratic Party", every quoted event title) and no expected event key may
   appear in the prompt. The test also proves the check can find each kind of leak. It
   found two in **cypher_v1**: its first example is "What happened today?", which is
   eval question q01 word for word, and its schema used the real key
   `03b-default-fix:341`, an expected event of q01 and q04. v1 was written by 05a before
   the eval set existed and stays frozen as the baseline, so the leak test covers v2 and
   later only.
3. **Develop on other questions.** Before running the eval I tried v2 on twelve
   development questions of my own, with the Cypher channel only (a scratch script:
   write, validate, repair, run): "Which stories began between 17:30 and 18:15 UTC
   today?", "What happened in the last 30 minutes?", "How many stories began after
   19:00 UTC?", "Which event has the most people involved?", "Which place appears in the
   most events?", "How many events is Colorado Party involved in?", "Which people are
   related to Central Bank of Paraguay?", "Which events touched at least three pages
   this evening (18:00 to 23:00 UTC)?", "Who appears in more than two stories?", "How
   many people are in the graph?", "Which country is mentioned in the most stories this
   week?", "What happened before 17:00 UTC today?". `cypher_v1` failed 4 of the first
   10. The first v2 draft failed 1 of 10 and repaired 1. It also copied a "the most"
   example built on `Page` for a question about places. I replaced that example with an
   entity ranking in one `WITH` (commit "cypher_v2 ranks entities in one WITH"), and
   then all 12 ran. Only then did v2 meet the eval set, and the prompt wasn't changed
   after that.
4. **Be honest about closeness.** Teaching a *shape* still helps the eval questions of
   that shape: the "organization in the most stories since midnight" example has the
   same shape as q19 (person, most events), and "In how many stories does Acme Bank
   appear?" has the shape of q20. That is what the spec asks for: date and name
   examples. But it means the eval measures "questions whose shape the prompt teaches".
   A fresh question set with new shapes would be the real test of generalisation (see
   "Open points").

## Design decisions and deviations

- **Before = a fresh run of v1, not only the committed 05b numbers.** The QA code changed
  since 05b (tracing, metrics, docstrings) and the validator fix changes v1's
  behaviour too. So `12a-before` runs cypher_v1 *without repair* on today's code and
  the same snapshot, judges and settings as `12a-after`. The 05b numbers are shown as a
  third column for reference. The difference between 05b-v1 and 12a-before (same
  prompt; correctness 1.20 against 1.08) is an honest picture of run-to-run noise plus
  the validator fix.
- **"The 05b margins".** The 05b report has no margins, so I read this as "the
  uncertainty of the 05b-style numbers". `qa_compare` gives every change a 95% interval
  from a **paired bootstrap** over the 25 questions (2,000 resamples, seed 12): resample
  25 questions with replacement, compute the metric for both runs on the *same*
  questions, keep after − before. This measures how much the change depends on which
  questions were asked. It does not measure model randomness between runs: for that,
  compare 05b-v1 with 12a-before.
- **Repair: one try, on validator rejections and Neo4j errors, not on "0 rows".** Zero
  rows is a correct result for unanswerable questions. Repairing it would invite the
  model to loosen the query until something matches.
- **The repair is a conversation** (request, the model's own answer, the error), not
  one long new message. This is the natural form for "fix what you just wrote" and needs
  no new prompt file. Errors are cut to 400 characters.
- **Answer records gained `cypher_prompt`, `cypher_repairs`, `cypher_ran`,
  `cypher_failures`.** At first the report took the repair count from the settings at
  report time, and a re-rendered 05b report claimed "1 repair". Now each answer carries
  it, and older answers show "not recorded". Older records get their query outcome from
  their notes (`cypher_outcome()`), which is how the comparison counts 05b's 4 failed
  queries.
- **`QA_CYPHER_PROMPT` is a `Literal` setting**, so a typo fails at startup instead of
  at the first question.
- **One run each, not two.** I started a second before/after pair to measure run-to-run
  noise, but Docker Desktop had stopped (the engine pipe was gone), so both runs failed
  at the first connection and wrote nothing. I didn't restart Docker Desktop on the
  owner's machine. Run 1 is complete. The "before" report was written before
  per-question claim counts existed, so the comparison shows its own faithfulness
  (94.0%) without a margin. It also means the integration tests (`-m integration`)
  could not be run at the end (see "Tests").
- **No new dependency.**

## Results

All on 2026-11-16 against snapshot `2026-11-05` on `neo4j-eval` (port 7688), QA model
`ollama:qwen3:8b`, answer prompt `answer_v1`, all three channels, judges `qwen3:8b`
(primary) and `qwen3:4b`. Each run took under 8 minutes (answering about 3 minutes,
171 s and 182 s of LLM time; judging about 4 minutes). Nothing needed scaling down for
the 60-minute rule. Commands:

```
uv run python -m wikipulse.eval.qa --name 12a-before --cypher-prompt cypher_v1 --cypher-repairs 0
uv run python -m wikipulse.eval.qa --name 12a-after  --cypher-prompt cypher_v2 --cypher-repairs 1
uv run python -m wikipulse.eval.qa_compare --name 12a-cypher <05b-v1.json> <12a-before.json> <12a-after.json>
```

Report: `evals/results/2026-11-16_12a-cypher.md`.

| Metric | 05b-v1 | 12a-before | 12a-after | Change | Margin (95%) |
|---|---:|---:|---:|---:|---:|
| Correctness (0-2) | 1.20 | 1.08 | 1.44 | +0.36 | +0.12 to +0.64 |
| Fully correct | 48.0% | 40.0% | 56.0% | +16.0 | +4.0 to +32.0 |
| Faithfulness (claims) | 95.9% | 94.0% | 100.0% | +6.0 | n/a |
| Entity recall | 92.3% | 92.3% | 100.0% | +7.7 | 0.0 to +26.7 |
| Event recall | 82.2% | 82.2% | 100.0% | +17.8 | +1.2 to +36.5 |
| Correct refusals | 80.0% | 80.0% | 100.0% | +20.0 | 0.0 to +66.7 |
| False refusals | 20.0% | 15.0% | 0.0% | -15.0 | -33.3 to 0.0 |
| Cypher queries that never ran | 4 | 3 | 0 | -3 | -6 to 0 |
| Latency median (s) | 4.8 | 5.3 | 5.2 | -0.0 | -1.5 to +2.2 |

Before the full runs, a Cypher-only check on the 25 eval questions (no answering, no
judges; a measurement, the prompt was not changed afterwards) gave the same picture:
cypher_v1 without repair, 3 queries never ran (q03, q04, q19); cypher_v2 without repair,
0; cypher_v2 with repair, 0.

How to read it:

- **Failed queries: 4 (05b) → 3 (validator fix) → 0 (v2).** This is the acceptance
  criterion, and it holds.
- **Correctness rose by 0.36, and the margin excludes 0**, as does "fully correct"
  (+16 points, interval +4 to +32). But the margin only covers which questions were
  drawn. The same v1 prompt scored 1.20 in 05b and 1.08 today: q01, q11 and q16 each
  lost a point with near-identical answers. So run-to-run noise is about ±0.1 to 0.2
  on this set, and the true gain is likely smaller than 0.36. I'd still call it a
  gain, because the questions that improved did so for the reason the change targets:
  q03 (0 → 1, the hour range now runs), q17 (0 → 2, "233 events" from a correct
  `duration` query), q20 (0 → 2, the exact name match gives 9 events instead of 11).
  Others moved partly by luck: q01 (0 → 1), q15 (1 → 2).
- **Running isn't the same as right.** q04 now gets 25 events of the afternoon and the
  answer is still judged 0 (it lists events, but not the biggest ones of the period: the
  same list-picking problem as q01, q02 and q05). q19's query now runs, but it dropped
  the `type = 'person'` filter, and the answer names "the United States" as the person
  in the most events. These are semantic errors that no validator or repair can see.
  They're the next thing to work on.
- **Faithfulness 94.0% → 100%.** In the before run, q24 ("Who was the chief executive of
  Ueno Bank?") invented "Santiago Peña was the chief executive of Ueno Bank". In the
  after run the system refused it (correct). That refusal is partly luck: the v2 query
  had an `OR ... AND` precedence slip and returned other Ueno Bank relations, and the
  model saw that none of them answered the question. The +20 points of correct refusals
  are this one question out of five, hence the wide margin (0 to +67).
- **Recall reached 100%** because the three questions that used to have empty contexts
  (q03, q04, q19) now get query rows.
- **Latency unchanged** (median 5.2 s against 5.3 s). No repair was needed, so no extra
  LLM call. The p90 for time questions went up (the answers are now real lists instead
  of instant refusals).

### Human judgements

There are no human QA judgements yet: `evals/labels/qa_judgements.jsonl` and
`qa_questions.jsonl` contain 0 records with `labeler: "human"`. So every correctness and
faithfulness number above is an LLM judge's. The two judges agreed as in 05b (see the
two run reports). `uv run python -m wikipulse.eval.qa --name 12a-after --rate` would add
human ratings, and the report would then show human vs judge agreement.

### Tests

- New unit tests: the regression test for q17's query and three other arithmetic forms
  (`test_arithmetic_after_a_closing_parenthesis_is_not_a_path_quantifier`), nine
  quantified groups that must still be refused, dash variants, the repair conversation
  (`test_cypher.py`); the prompt tests and leak test (`test_prompts.py`); five repair
  tests (`test_retrieve.py`); query outcomes in metrics, records and reports
  (`test_qa_metrics.py`, `test_qa_eval.py`); the comparison (`test_qa_compare.py`). No
  existing test was changed except `FixedWriter` in `tests/qa/helpers.py`, which gained
  a `repair()` method so the existing "unsafe query never reaches the graph" test now
  also goes through a repair.
- `uv run pytest`: 1,447 passed, 2 skipped (Airflow is only in its image; promtool
  needs Docker), 81 deselected. `uv run pytest tests/qa`: 113 passed (the 101 from
  before 12a's tests included). `ruff check .` and `ruff format --check .`: clean. The
  integration tests could not be run at the end, because Docker Desktop had stopped. Before that, every v2 example query ran on the
  eval graph (`EXPLAIN`, then for real), and the full evaluation itself ran
  end to end against Neo4j.

## Open points

- **Semantic query errors**: a missing type filter (q19) or `OR`/`AND` precedence (q24)
  pass every check. Ideas for a later spec: return the matched entity's `type` so the
  answer model can notice, or a cheap self-check ("does the query filter what the
  question names?").
- **List answers** (q01, q02, q04, q05) remain the biggest group of wrong answers. That
  is the answer prompt (answer_v1), not Cypher.
- **A fresh question set** with query shapes that no prompt example shows, to measure
  generalisation rather than taught shapes. And a second run of each configuration to
  measure model noise directly.
- **Human ratings** of a sample, to check the judges.

## Questions to test your understanding

1. In `12a-before`, q17's query ran (the validator no longer rejected it) and still
   produced a wrong answer. What went wrong, and why could neither the validator nor the
   repair step catch it?
2. The comparison says correctness rose by +0.36 with a 95% margin of +0.12 to +0.64.
   Does that mean v2 is better with 95% certainty? What does the margin leave out, and
   which number in the table hints at it?
3. Why does the leak test check names from the expected answers, and not only the
   question texts? Give an example of a leak it would catch that a question-text check
   would miss.

<details>
<summary>Answers</summary>

1. The query compared `ev.started_at` (a datetime) with `date($today_start) +
   duration({days: 1})` (a date). In Cypher that comparison matches nothing, so the
   count was 0 and the answer said "0 events started today". The validator only checks
   safety (read-only, bounded, limited), not meaning. The repair only runs when a query
   is rejected or raises an error, and a query that returns a wrong number does neither.
   The fix had to come from the prompt: the v2 rule against wrapping parameters in
   `date()` and the `$today_start + duration(...)` examples.
2. No. The paired bootstrap only asks: if we had drawn a different 25 questions of the
   same kind, how much would after − before vary? It treats each run's answers and
   judgements as fixed. But local models aren't fully repeatable, and judges disagree.
   The 05b-v1 and 12a-before columns use the same prompt and differ by 0.12 in
   correctness, so part of the +0.36 could be run noise. Repeated runs of both
   configurations would measure it.
3. A prompt can leak an answer without ever asking the question. An example like "Which
   organizations are in the Democratic Party's stories?" with a query matching
   `'democratic party'` shows the model the exact entity, and the spelling, that q20's
   answer depends on. The question-text check wouldn't notice, because the example's
   wording differs. Checking expected answer names (and event keys, which is how
   cypher_v1's `03b-default-fix:341` was caught) catches prompts that are tuned to the
   eval's subjects rather than to the schema.

</details>
