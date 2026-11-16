# Review: 12a Better Cypher generation for question answering

Reviewer: independent agent. Branch `spec-12a-cypher-generation` against `main` (8 commits,
83aa444..584cd99). Docker Desktop was stopped and was not started, so the integration tests
(`-m integration`) and anything needing `neo4j-eval` (re-running the eval, `EXPLAIN` against
Neo4j) were not run. As instructed, no LLM evals were run and no Wikimedia requests were made.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| The validator regression test passes, and so do all existing `tests/qa` tests | pass | `uv run pytest tests/qa`: 113 passed, 8 deselected. The regression test `test_arithmetic_after_a_closing_parenthesis_is_not_a_path_quantifier` uses q17's 05b query. `git diff main...HEAD -- tests/` removes only two lines, both in `FixedWriter` (`tests/qa/helpers.py`: docstring and `__init__` signature, extended with `repairs`). No safety test was edited, skipped or deleted. |
| The eval runs on the frozen graph with the new prompt; the report shows fewer failed queries; honest if correctness gain is within noise | pass (from committed artefacts; not re-run) | `evals/results/2026-11-16_12a-cypher.md`: queries that never ran went 4 (05b) to 3 (12a-before) to 0 (12a-after). All three configs record snapshot `bolt://localhost:7688` with the same node counts (233 events). Re-running `qa_compare` on the committed before/after JSON gives the same numbers and margins. The learning note says plainly that v1 scored 1.20 in 05b and 1.08 today, that the bootstrap doesn't cover run-to-run noise, and that the true gain is probably smaller than +0.36. |
| `uv run pytest`, `ruff check`, `ruff format --check` pass | pass | `uv run pytest`: 1447 passed, 2 skipped, 81 deselected. `uv run ruff check .`: all checks passed. `uv run ruff format --check .`: 500 files already formatted. |

Other checks requested:

- **Validator strictness (Req. 1).** I tried 24 hand-written probes against `validate_cypher`. Every unbounded form was still rejected: `((a)-->(b))+`, `){1,}`, QPP with an inner `WHERE`, juxtaposed groups, `-[:R]->+`, `- +`, `ANY SHORTEST ... -+`, a relationship split by a `/* */` or `//` comment, fullwidth/soft-hyphen/U+FE63/U+2212 dashes, and a mixed `-[*]–` pattern. The `_ARROW_CHARS` set matches the dash/arrow alternatives in Neo4j 5's Cypher grammar. I could not confirm this against a live server. The normalisation runs on the skeleton, where strings are already blanked, so an en dash inside a string literal is unaffected (accepted). The only `)`-quantifier that the exemption now allows without a relationship (`(a)((b))+(c)`) is not a valid QPP in Neo4j, so it fails to parse instead of running unbounded. Write keywords are checked before and independently of the quantifier logic, so the exemption can't let a write through.
- **Repair loop.** `Retriever._run_generated()` (`wikipulse/qa/retrieve.py:454-788`) loops `cypher_repairs + 1` times. `qa_cypher_repairs` is `Field(ge=0, le=1)`. Each repaired query goes back through `validate_cypher` and the READ session. Each failure is appended to `cypher_failures`, added as a note and logged with `logger.info`. A repair happens only on `UnsafeCypherError` or `Neo4jError`, never on zero rows. `test_the_repair_is_bounded_and_an_unsafe_query_still_never_runs` covers the bound.
- **Prompt v2.** All eight examples use invented names ("Acme Bank", "Riverside flood") and the schema's own labels and properties. The schema's event key is the made-up `run-name:12`. `cypher_v1.md` is unchanged and can still be selected through `QA_CYPHER_PROMPT` (a `Literal` setting), `--cypher-prompt` and `load_prompt("cypher_v1")`. `test_every_example_query_is_a_valid_read_query` also validates every v1 and v2 example.
- **Labels.** `evals/labels/qa_judgements.jsonl` gained 176 lines, all appended after line 131, none removed, all `labeler: "llm"`, runs `12a-before`/`12a-after`. `qa_questions.jsonl` and every other label file are unchanged.
- **Secrets/data.** Nothing under `data/` and no `.env` in the change set. The only email address in the changed files is the `you@example.com` placeholder in `.env.example`.

## Problems

1. **medium**: `tests/qa/test_prompts.py:201`. The answer-name check is case-sensitive (`re.search(rf"(?<!\w){re.escape(name)}(?!\w)", prompt)` without `re.IGNORECASE`), so a leak in the lowercase string form that cypher_v2 itself teaches, such as `toLower(n.name) = 'democratic party'` or `'ueno bank'`, is not found. I confirmed `leaks()` returns `[]` for both. The question's `expected_entities` (Wikidata IDs such as `Q29552`) aren't checked at all. The current v2 prompt has no leak, but the test won't protect v3.
2. **low**: `evals/results/2026-11-16_12a-cypher.md:3,21`. The 05b-v1 column was built from an uncommitted re-render of the 05b report. Its configuration line says "Cypher repairs 1", which the note admits is wrong. Re-running the documented command on the committed `2026-11-08_05b-v1.json` gives a correct config line ("none (before 12a)", commit 1cf426b) but `n/a` for failed queries. So the "4" in the committed report can't be reproduced from committed files (it does agree with the 05b learning note).
3. **low**: `evals/results/2026-11-16_12a-*.json` (`config.dirty: true`, commit dcd8f4d). All three runs were rendered from a dirty working tree at a commit before 4ffad80, so no single commit is exactly the code behind the numbers.
4. **low**: `evals/results/2026-11-16_12a-cypher.md:13`. Faithfulness, one of the metrics Req. 3 asks for, has no margin, because the before report was rendered before per-question claim counts existed. This is explained, but it means faithfulness has no measured uncertainty in a comparison that shows a +6 point change.
5. **low**: `wikipulse/eval/qa_compare.py:449`. The generated header says "an interval that includes 0 is within noise". That suggests an interval excluding 0 is beyond noise, but the module docstring and the learning note both say the margin ignores run-to-run noise, which here (0.12) is as big as the correctness interval's lower bound.
6. **low**: `wikipulse/qa/retrieve.py:755-767`. Every `Neo4jError` triggers a repair, including transient errors and transaction timeouts. The model is then asked to rewrite a query that may be fine, and a timeout can double the worst-case latency (timeout + LLM call + timeout).
7. **low**: `wikipulse/qa/retrieve.py:768-769`. An `LLMError` raised during the repair call is reported as "the model gave no query", which is misleading when the first query was written and failed.
8. **low**: `wikipulse/qa/types.py:808` / `wikipulse/eval/qa_metrics.py:535`. `cypher_repairs` stores how many repairs were *allowed*, not how many were used. The commit message ("answers record their Cypher repair count") and the field name suggest the latter, and readers have to derive actual repairs from `cypher_failures`.
9. **low**: `wikipulse/qa/cypher.py:81,168`. Some valid arithmetic is still rejected because its group looks like a relationship, for example `(xs[0]-1) * 2` (`]-`) and `(a.x - -1) * 2` (`- -`). This is the safe direction and partly documented (`size((a)-->()) * 2`), but these cases have no test or note.
10. **low**: `wikipulse/qa/cypher.py:34`. `MAX_ERROR_CHARS = 400` is a hard-coded threshold next to `MAX_QUERY_CHARS`/`MAX_HOPS`. It follows the existing module style rather than `config.py`, but it is a tunable value.

Verdict: merge

The validator fix is correct and stays strict. It also closes a real pre-existing gap: en-dash relationships used to bypass the hop checks. The repair loop is bounded and safe, tests are added rather than weakened, labels are append-only, and the report is honest about noise. Problem 1 should be fixed before anyone writes a cypher_v3 (add `re.IGNORECASE` and check `expected_entities`). It doesn't affect the shipped prompt.

## Questions for the human before merging

1. `_is_arithmetic()` exempts a `+`, `*` or `{..}` after `)` when the bracketed group contains no relationship piece. Why is that safe, that is, which Neo4j rule guarantees such a group can't be an unbounded quantified path, and what happens to a query like `(a)((b))+(c)` that the exemption lets through?
2. The correctness margin is +0.12 to +0.64, and v1 itself moved from 1.20 to 1.08 between two runs with the same prompt. What does the paired bootstrap resample, what does it hold fixed, and what experiment would tell you how much of the +0.36 is the prompt?
3. If a future cypher_v3 contained the example `WHERE toLower(n.name) = 'democratic party'`, would `tests/qa/test_prompts.py` fail? Why or why not, and why does that matter for the "don't overfit the eval set" goal?
