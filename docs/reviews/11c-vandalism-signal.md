# Review: Spec 11c, A vandalism signal for spike detection

Branch `spec-11c-vandalism-signal`, compared with `main` (6 commits, 063f62d..0e0436f).
Reviewed on 2026-11-16. No code changed, no LLM called, no Wikimedia request made.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| The analysis report exists, with sample sizes and margins | pass | `evals/results/2026-11-16_vandalism.md` and `.json`: 8 signals at 3 moments for human (18 news / 68 not), LLM (361 / 337) and gate LLM (38 / 130) bursts, each with n, class means, AUC and a 95% bootstrap interval; rule table with Wilson intervals; signals that don't help (revert share, reverted share, revert speed, top-editor share) are reported. Labelers and models are named per set. I checked the derived numbers: hypergeometric C(48,25)/C(63,25) = 0.00013 (the "about 0.0001" claim), one-sided 95% upper bound for 0 of 15 = 1 - 0.05^(1/15) = 0.181 (the "18%" claim), 43/245 = 17.6% LLM news lost. |
| On the gate fixture, precision rises and recall stays within the margin; the gate passes with the new baseline; baseline change explained | pass | `uv run python -m wikipulse.eval.gate`: PASS, exit 0, precision 0.600, recall 0.184 (15 spikes, 7 of 38 news bursts). Old baseline 0.526 / 0.211 (19 spikes, 8 of 38): one news burst lost, the most requirement 5 allows. Before/after table in the learning note; `labels_sha256` unchanged. |
| If the classifier changes, a challenger is compared with the champion | n/a | `wikipulse/ml/features.py` only moves `is_anonymous` to `detector.py` and imports it; feature values are unchanged, no retraining. |
| Determinism check passes | pass | `scripts/determinism_check.py --file data/recordings/history-20261106T2000Z.jsonl` with 02a's loose thresholds and `DETECT_MAX_ANON_SHARE=0.45`: "PASS: both replays produced identical spikes (all fields compared)", 17 spikes, `anon_held` 23 in both runs, run B restarted from its snapshot half way. |
| `uv run pytest` | pass | 1398 passed, 1 skipped, 81 deselected. |
| `uv run ruff check .` | pass | "All checks passed!" |
| `uv run ruff format --check .` | pass | "491 files already formatted" |

Other checks done:

- **Fixture recut.** Decompressed both versions of `tests/fixtures/detection_gate/edits.jsonl.gz`: 19,172 lines each; with `"a"` removed, all 19,171 edit lines are identical to `main`; only the info line's `note` changed. Every line has keys `t, p, u, r, a`; the flag is consistent per numbered user (821 of 3,443 users, 2,667 edits anonymous). No IPv4 address or `~YYYY-` temporary account appears in the file.
- **Labels.** `git diff main...HEAD -- evals/labels` is empty: `detection_gate_labels.jsonl` and `spike_labels.jsonl` are unchanged.
- **Personal data and `data/`.** Nothing under `data/` or `.env` is committed. The only IPs/temporary accounts in the diff are test literals (192.0.2.x documentation range, `~2026-1-1`); the results JSON has titles, times and labels, no user names or emails.
- **`make_gate_fixture.py` VARIANTS.** The four 02b variants are the old ones plus `max_anon_share=1.0`, so they reproduce exactly the pre-11c detector; `"current"` adds the rule-on defaults, whose spikes are (nearly) a subset. A labels rebuild therefore keeps covering what the rule drops. `"default": RULE_OFF` shares the dict object, but nothing mutates it. Correct.
- **Rule correctness.** `_too_anonymous` reads `PageWindows.users()` over the same event-time window as the start rule; bots and (with `count_reverts=false`) reverts never enter the window, which matches the analysis's `anon_share` (human non-revert edits). The detector and the classifier now share one `is_anonymous`. Only the start is affected; a held-back burst can start later. Inclusive comparison (`<=`) makes 1.0 a true "off".
- **Gate sensitivity.** Ran the gate with `--set`: `max_anon_share=1.0` (0.526 / 0.211), `min_edits=3` (0.478 / 0.237), 02b medium thresholds (0.478 / 0.237), `count_reverts=true` (0.500 / 0.184) and `min_edits=4` (0.529 / 0.184) all PASS; `min_edits=3,min_editors=2` (0.325) and `max_anon_share=0.0` (recall 0.132) fail. This confirms the implementer's own account.
- **README.** The old claim that the share of reverted edits is "the most promising detection feature" is correctly replaced; the new numbers (AUC 0.75 vs 0.53, 482 -> 327 spikes, 26% -> 41%, 0 of 15 human news lost, 43 of 245 LLM news lost) match the report.

## Problems

1. **high**, `tests/eval/test_gate.py` lines 251-297 (with `evals/baselines/detection_gate.json`): three gate tests that failed after the change were rewritten to grosser scenarios (medium -> loose thresholds, the 09c demo PR now pinned to `max_anon_share=1.0`, CLI `count_reverts=true` -> `min_edits=3,min_editors=2`) so the suite passes, while the gate itself now lets through a 12-point precision drop, the 09c demonstration PR, `count_reverts=true` (precision -10 points at equal recall) and switching the new rule off; however openly documented, this is weakening tests to make them pass, and the fix (a percentage-point floor on the margin or a longer fixture) or an explicit owner decision belongs in this change, not only in a ROADMAP follow-up.
2. **medium**, `wikipulse/config.py` line 69 / `wikipulse/eval/vandalism.py` line 591: 0.45 was chosen on the same 15 human news bursts and the same gate fixture it is then reported and gated on, with no held-out data; the "choosing rule written down before picking" cannot be verified from the history (grid, rule and choice land together, and 0.45 is an off-grid value between the natural 0.4 and 0.5), so the reported 41% precision and "0 news lost" are optimistic in-sample numbers.
3. **medium**, `evals/results/2026-11-16_vandalism.md` (start-change table) / `docs/learning/11c-vandalism-signal.md`: the choosing rule ignores detection delay, yet 0.45 raises the p90 delay of held-back spikes to 55 minutes (7.5 at 0.5) for a project whose point is catching breaking news early, and by LLM labels it loses 43 news bursts including election-night pages and a missile strike; the trade-off is reported but not weighed.
4. **low**, `wikipulse/config.py` lines 66-68 and `.env.example` lines 83-84: "0.45 means, in effect, less than half" is inaccurate, since e.g. 5 of 11 (0.455) or 7 of 15 (0.467) anonymous edits are less than half yet are held back.
5. **low**, `wikipulse/detect/detector.py` line 343: `stats["anon_held"]` counts start checks, not bursts, and can count twice on one edit when both the short and the long rule are held, so the number in logs and snapshots is easy to misread as "bursts held back".
6. **low**, `wikipulse/eval/vandalism.py` lines 818-831: `auc_interval` silently drops bootstrap resamples that lack one class, which biases intervals for small groups (e.g. `revert_minutes`, 4 / 18 human bursts) without saying how many resamples were used.
7. **low**, `tests/eval/test_vandalism.py` (e.g. lines 243-248, 256-276): many `# fmt: skip` markers to keep hand-formatted literals, which bypasses the project's formatter rather than restructuring the test data.
8. **low**, `docs/ROADMAP.md` (follow-up on training/serving skew): changing the `DetectorConfig` default shifts the spike population the current classifier champion sees in serving (about a third fewer, all anonymous-heavy); this is documented, but no drift check or retrain was run to show the champion still behaves.

Verdict: fix

The detector rule, the analysis and the fixture recut are careful and correct, and the determinism and personal-data requirements hold. The blocker is problem 1: a merged change should not leave the blocking CI gate unable to see the regressions its tests were written to catch, with the tests swapped to make the suite green. Either restore gate sensitivity in this branch, or the owner explicitly accepts the coarser gate when reviewing the CODEOWNERS-covered baseline.

## Questions for the human before merging

1. With the new 15-spike baseline, why does `DETECT_COUNT_REVERTS=true` (precision 0.600 -> 0.500, same recall) pass the gate now when it failed before, and what change to the gate (fixture length or margin definition) would you accept to make it fail again?
2. The rule loses 0 of 15 human-labeled news bursts but 43 of 245 LLM-labeled ones, including US election-night pages. What evidence would you want before believing the true news loss is near 0% rather than near the 18% upper bound, and why doesn't choosing 0.45 on these same 15 bursts count as that evidence?
3. A burst held back by the rule can still start later once registered editors join. Walk through what happens to `started_at`, `first_edit_at` and `total_edits` in that case, and explain why the decision is still identical between a live run and a replay that is stopped and restarted from a snapshot half way.
