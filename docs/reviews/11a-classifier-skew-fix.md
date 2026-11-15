# Review: Spec 11a, Fix the news classifier's training/serving skew

Branch `spec-11a-classifier-skew-fix` compared with `main` (4 commits: efbd9ef, a30add5,
7c84b0b, 0c6ec50). Reviewed 2026-11-15.

## Acceptance criteria

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | The shared-features test and the impossible-values test pass | pass | `uv run pytest` 1325 passed, 1 skipped, 79 deselected; `tests/ml/test_cutoff.py` includes `test_dataset_and_serving_compute_identical_features_for_the_same_spike`, `test_serving_mode_rows_only_hold_values_serving_can_produce`, and a control test in candidates mode that would fail if the synthetic day were too easy. |
| 2 | A new dataset version exists, and the learning note reports its row counts, label coverage and class balance | pass | `dataset list`: v5 `mode serving`, 410 rows (283/55/72), known labels 278/55/72, positive share 54.3/69.1/51.4%. These match the note. v1 to v4 are still listed and readable. On the real data: v5 has 0 rows with `edits_before_start < 5` (min 5.0). In v4, 133 of the 379 default-rule rows have value 4. Both match the note. |
| 3 | A challenger is trained, compared on identical test rows, and promoted or rejected, with the numbers in the note | pass | MLflow v6 is tagged `decision=reject`: "0.967 (challenger v6) vs. 0.971 (champion v3) on the same 72 rows of v5: gap -0.003 (95% -0.032 to +0.019)". v3 is still `@champion`. These match the note. |
| 4 | A drift report on the new reference shows whether `edits_before_start` still drifts | pass | I re-ran `drift --run-id 07a-default --since 2026-11-08 --until 2026-11-11 --version 3 [--reference-dataset v5] --no-store`. Against v2: `drift`, 11 features, `edits_before_start` PSI 2.951 (floor 0.219). Against v5: `ok`, 2 features (page_age_days, bytes_added), `edits_before_start` PSI 0.0765 (floor 0.160). Both match the note. |
| 5 | `uv run pytest`, `ruff check` and `ruff format --check` pass | pass (locally), **fail in CI** | All three pass locally. CI run 43739204868 on this branch fails in "Run integration tests": `tests/ml/test_dataset_s3.py::test_build_on_s3_and_postgres - ValueError: only 0 days with a final portal`. I reproduced it locally. With `ML_DATASET_MODE=candidates` the same test passes. |

## Problems

1. **high**, `wikipulse/config.py:403` / `tests/ml/test_dataset_s3.py:64`: When the default mode changed to `serving`, the S3/Postgres integration build started failing (no day is "final" at the default as-of), so CI is red. The test was neither pinned to `mode="candidates"` like its unit-test siblings nor given a serving-mode version.
2. **medium**, `wikipulse/jobs/retrain.py:233` (`step_build`): The weekly DAG builds with as-of = end of data. In serving mode this always delays the newest ~5 days, because a spike's portal days run to +48 h and need 3 more days to become final. So the test split lags the lake by about a week. The build also raises a hard `ValueError`, failing the task, whenever the contiguous lake is shorter than about 10 days. Neither the learning note nor the ROADMAP says this about making `serving` the default.
3. **medium**, `wikipulse/ml/drift.py:368` with `drift_reports UNIQUE (model, version, run, period)`: A `--reference-dataset` what-if report is stored under the same key as the model's own monitoring report, so it can replace that report. The stored v3 / `07a-default` / 8-11 Nov row now says `ok` against v5, and the Prometheus drift metrics read the newest report as v3's drift status. It should default to `--no-store`, or the key should include the reference.
4. **medium**, `wikipulse/ml/serve.py:204` and `:338` vs `wikipulse/ml/dataset.py:731` and `:185`: The single shared path covers only window, revision and features. Turning edits into `PageEdit`s (`serve.page_edit` vs `dataset.page_edits`) and fetching page snapshots (`WikiPages.snapshots` vs `fetch_snapshots`) are still two near-identical copies. The equality test injects the same edits list and snapshot dict into both sides, so it never exercises those copies, or the live `EditBuffer` pruning.
5. **low**, `wikipulse/ml/dataset.py:162-169`: `select_spikes` still computes the cutoff through the private `window_of`, not `ml.cutoff.at_cutoff`. That leaves a second definition of the cutoff in the dataset module.
6. **low**, v5 manifest (`labels_file_sha256 1163dc4f…`): v5 was built from a dirty tree, and its labels hash matches no commit (`0c6ec50` has `536064…`). The dataset that decided the rejection can't be rebuilt exactly from the repository.
7. **low**, commit `0c6ec50`: The commit is described as "186 LLM labels" but also carries 78 human labels the human was still adding in the working tree, so the human's in-progress work was committed under the agent's commit.
8. **low**, `wikipulse/config.py:187`: `qwen3:4b` was the right labeler for consistency (all 454 earlier LLM labels used it), but `EVAL_LABEL_MODEL` is still `qwen3:8b`. The next default run, and 11b's DAG task, will relabel every covered spike with another model. The ROADMAP 11b follow-up doesn't mention this.
9. **low**, `wikipulse/jobs/retrain.py:368` (`rows_seen_by`): The overlap guard matches on `spike_id`, and ids differ between detector runs. So it silently reports 0 for any candidates-trained champion evaluated on serving rows. The note mentions this, but no follow-up records it.
10. **low**, MLflow v6 tags: `trigger=weekly retraining` and `retrain_key=retrain-2026-11-09` record a manual run made on 15 Nov as a weekly run. The provenance is misleading.
11. **low**, `wikipulse/ml/drift.py` `reference_table`: In serving mode the reference description says "training rows the live default detector also found", but every row is a default spike, so the qualifier no longer means anything.

## Notes on the focus questions

- **One shared path:** this holds for the window, the revision and `check_no_leakage` features. Both `build_row` and `score_spikes` call `ml.cutoff`, and the spy test proves it. It doesn't hold for edit conversion or snapshot fetching (problem 4). The impossible-values test is stronger than its name: it also checks that every serving-mode row equals `score_spikes` output for the same spike.
- **Splits:** purge and time order are kept. Delayed days are removed before purging and never go into training. The test label as-of is the build's as-of (15 Nov), and the other splits use the next split's start. `assign_final_splits` handles non-final days inside the range correctly.
- **Fairness:** both models scored the same 72 rows. The note discloses that v3's model family was chosen on v2's test split, which includes 9 Nov, and that v3's threshold was set on candidate rows. Two more points: all 72 test labels come from the same LLM labeler (`qwen3:4b`, 47 of them created in this spec), so this measures agreement with that labeler. And with 72 rows the paired 95% interval is about 5 AP points wide, much wider than the 0.01 margin. The rule as applied could hardly have promoted anything, which structurally favours the skewed incumbent.
- **Tests weakened:** no. The unit tests in `test_dataset.py` only gained `mode="candidates"`, with no assertion changed. The integration test was missed (problem 1).

Verdict: fix

## Questions for the human before merging

1. The champion and the new model scored almost the same AP on serving rows. Why can the skew still cost you something when ranking quality didn't suffer, and what in the weekly pipeline stays "blind" until a serving-mode model is promoted?
2. In serving mode, why is a day only usable as a test day about five days after it ends? What happens to the weekly `build_dataset` task if the history lake has a gap and its last contiguous stretch is only eight days long?
3. The equality test passes the same `edits` list and snapshot dict to `build_row` and `score_spikes`. Which parts of the live path could still produce different features for the same spike, and how would you test them?
