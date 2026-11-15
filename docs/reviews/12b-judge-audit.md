# Review: Spec 12b, Audit the LLM labelers and judges against human labels

Branch `spec-12b-judge-audit` against `main` (5 commits, 6d62034..6ce77c4). Reviewed on
2026-11-15 by an independent reviewer; nothing in the code was changed.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Precondition: at least 50 human spike labels on one 02b run | pass | `spike_labels.jsonl` has 50 human labels with `run_id = 02b-baseline` (49 decisive, 1 `unsure`), and 37 on `07a-candidates`. |
| The audit report exists and covers every label family, with counts even where they are zero | pass | `evals/results/2026-11-15_audit.md` / `.json` list spikes, gate, grouping, relations, qa, qa questions, with zero counts shown (gate human 0, qa human 0, `news: 0, ...`). I re-ran `uv run python -m wikipulse.eval.audit --results-dir <scratch copy> --date 2026-11-15` (3 s): the Markdown is byte-identical to the committed one, and the JSON differs only in `created_at` and the `report` paths (my scratch directory). |
| The gate passes on the rebuilt labels and new baseline | pass | `uv run python -m wikipulse.eval.gate` prints `## Detection eval gate: PASS`. A rebuild into a scratch copy with `scripts/make_gate_fixture.py --labels-only --no-llm --labels <scratch>` gives a file byte-identical to the committed `detection_gate_labels.jsonl`. |
| CI is green | not verifiable | The branch is not pushed (`git ls-remote` finds no `spec-12b-judge-audit` on origin), so no CI run exists. Every CI step I could run locally passes (the rows above and below). |
| README results show both numbers with their sources | pass | README Detection, Grouping and Relations tables have an LLM and a human column with n and 95% intervals, and they cite `evals/results/2026-11-15_audit.md`. QA says "human n = 0". I checked every human/LLM figure in the README against the audit: all match (27% [16%, 40%] n = 49; 22/27/1 for strict/medium/loose; 80% [67%, 90%], kappa 0.58 [0.37, 0.79]; grouping 78% [65%, 87%] vs 98% [90%, 100%], recall 23% vs 25%; relations 98% [89%, 100%] n = 49; agreement "92 to 94%"). |
| `uv run pytest` passes | pass | `1350 passed, 1 skipped, 81 deselected` in 106 s. |
| `ruff check` passes | pass | `All checks passed!` |
| `ruff format --check` passes | pass | `482 files already formatted` |
| Requirement 3: the audit is read-only on label files | pass | `git diff --stat main...HEAD -- evals/labels evals/baselines tests/fixtures` is empty. `audit.py` only opens files under `results_dir` for writing (`write_report`). `test_report_covers_every_family_and_never_writes_labels` compares the label bytes before and after. |
| Requirement 2: `unsure` excluded and counted, the same way for both sides | pass | `agreement()` drops pairs with `unsure` on either side and counts `human_unsure` / `llm_unsure`. The confusion matrix keeps an `unsure` row and column. Covered by `test_unsure_is_counted_and_left_out_on_both_sides`. |
| Requirement 4: gate baseline change explained and flagged for review | pass | No baseline change happened. The learning note has the old and new table, an "Owner: please review" paragraph, and the CODEOWNERS paths. |

### The implementer's claims, checked

- **No human spike label covers the gate window.** Verified. The 87 human labels run from 2026-11-05 00:01 to 2026-11-06 23:24 UTC (latest `end_at`). The gate's 180 bursts start at 2026-11-07 01:04 or later. So no human label can overlap a gate burst, and the labels and baseline come out byte-identical.
- **Old precedence would have moved the gate without any human involved.** Verified with a simulation of the old order on the committed fixture. 11 gate labels would have been replaced by `spike_labels.jsonl` LLM labels, and 2 of them change value (both *Ramayana: Part 1*, `not news` and `unsure` become `news` from `07a-default`). That gives 38 to 40 news bursts, as the learning note says. The old order also could not run with `--no-llm`, because it re-asked the LLM for the 167 bursts that had no `spike_labels` label. So the precedence change was needed to rebuild the gate from human judgement. Human labels still win over LLM labels, so it stays within the spec. It is a deviation and is recorded in the learning note.
- **Grouping pairs matched by spike start times.** I found no start-key collisions between different title pairs in `grouping_labels.jsonl`. As an independent check I matched the 03b-default-fix pairs by title instead: 48 `same`, 1 `different` and 1 unmatched (a redirected title) in the `together` stratum, which agrees with the audit's 49/50.
- **Statistics.** The Wilson formula is correct. Bootstrap percentile intervals are reproducible (fixed seed). Kappa reuses `qa_metrics.cohen_kappa`, linear-weighted for QA correctness, with the 0/0 case (both raters used one and the same category) reported as undefined instead of 1. The kappa paradox is flagged for the relations family. With zero pairs every statistic returns None/`n/a` and nothing crashes (tested).
- **No personal email or external calls.** Verified. No address in the changed files apart from the CODEOWNERS handle that already exists on main. `audit.py` imports no network, database or LLM code.

## Problems

1. **medium**, `tests/eval/test_audit.py` (whole file) against `wikipulse/eval/audit.py:612-835`: `grouping_headline`, `pair_start_key`, `recall_interval`, `relation_headline` (`relation_row`/`report_row`/`entity_text` matching), `qa_headline` and the numeric value of the weighted kappa have no tests. The README's most striking human numbers (grouping 98%, relation precision 98%) come from this untested matching code, and the `llm_matches_report` runtime check only guards the LLM side, not the human side.
2. **low**, `wikipulse/eval/audit.py:185-191` (used at 242): `conclusion()` says "no human labels yet" whenever there are zero *decisive* pairs, so it also says it when humans did label but every pair was `unsure` or every human label was unpaired.
3. **low**, `wikipulse/eval/audit.py:301-319`: `gate_comparisons` never fills `unpaired`, so the gate rows' "Human unpaired" column is always 0 whether or not human labels missed the gate.
4. **low**, `wikipulse/eval/audit.py:98-99` and `980-984`: the docstring and the report text say a resample has no kappa when "one side used a single category", but `kappa()` returns None only when *both* sides used the same single category.
5. **low**, `wikipulse/eval/audit.py:198-201`: `kappa_words` labels a kappa of exactly 0 as "worse than chance" (`value <= 0`), although 0 is chance level.
6. **low**, `wikipulse/eval/audit.py:692-694`: `grouping_headline` builds the LLM label map from every LLM record regardless of model or prompt version (last one wins), unlike `relation_headline`, which filters by the report's judge. It matches the reports today, but a future prompt-3 relabel would silently change the LLM column (only the "differs from the report!" marker would show it).
7. **low**, `wikipulse/eval/audit.py:408-415`: faithfulness claims are paired by position with `zip(strict=False)`, so if a human and an LLM judgement have different claim counts, the extra claims are dropped silently instead of being counted.
8. **low**, `scripts/make_gate_fixture.py:259-262`: with `--labels-only`, any `--start`, `--evaluate-from` or `--end` that is given is silently ignored (the fixture's own hours are used), with no warning.
9. **low**, `README.md` (Detection and Relations sections): interval styles are mixed in the same tables. The relations judge row keeps the old normal-approximation "± 6.7 pp" next to Wilson brackets, and only the default detection row has an LLM interval. The gate sentence calls its labels "LLM labels (`qwen3:8b`)", but 13 of the 180 are `qwen3:4b` labels copied from 07a.
10. **low**, `docs/ROADMAP.md` (follow-up "No human QA judgements"): it proposes "a `--manual` mode for QA judgements", but `wikipulse.eval.qa --rate` (`rate_manually`) already lets a human rate correctness. Only faithfulness lacks a manual tool.

Verdict: merge

## Questions for the human before merging

1. The relation judge agrees with you on 94% of relations, yet its kappa is about -0.03 and is flagged as "rare category". What would you need to add to `relation_labels.jsonl` before kappa could tell you anything about this judge, and why does accuracy alone not settle it?
2. `scripts/make_gate_fixture.py` now keeps the gate file's own LLM label ahead of a newer LLM label in `spike_labels.jsonl`. Walk through what happens to a gate burst when (a) you add a human label for it via `wikipulse.eval.label --manual` on a 07a run, and (b) you later correct that human label in `spike_labels.jsonl`. Which label ends up in the gate file in case (b), and why? (Hint: the learning note's "known gap".)
3. The README now says the default detector's precision is 48% by LLM labels and 27% by human labels. The audit also shows the LLM at 47% on exactly the 49 spikes you labeled. What does that third number rule out, and what does it imply for the news classifier trained from spec 07b onward?
