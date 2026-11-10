# Review: 07a-training-data

Reviewer: independent agent. Branch `spec-07a-training-data` vs `main` (one commit, a30c203).
Inspected `datasets/v1` in RustFS and the lake `history/edits` directly (read-only); nothing was rebuilt.
I didn't run a `build --dry-run` because it is not side-effect free (see problem 8).

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests for features and the leakage guard | pass | `uv run pytest`: 901 passed, 1 skipped, 70 deselected. `tests/ml/test_features.py` covers each feature group on a synthetic spike, `test_features_ignore_everything_after_the_cutoff` (Requirement 1), the boundary at the cutoff, a deliberately broken `visible_edits` that the guard catches, and a refused post-cutoff snapshot. The integration test `tests/ml/test_dataset_s3.py` also passes (1 passed, 30 s). `ruff check` and `ruff format --check` are clean. |
| Requirement 1: no feature uses data after the feature window | pass | `compute_features` takes a `SpikeWindow`, not the `SpikeRecord`. Edits pass only through `visible_edits`. The page snapshot is the revision at the cutoff, and `usable_snapshot` raises on a later one. `check_no_leakage` ran on every row (manifest `leakage_check.rows_checked = 794`). I read every feature function and found no use of `ended_at`, totals or score. There is one small leak through missing values (problem 6). |
| Requirement 2: time-based splits only | pass | `assign_splits` splits by sorted UTC day. In v1, train is 3–6 Nov (last cutoff 7 Nov 00:04), valid is 7 Nov and test is 8–9 Nov. There are no duplicate `spike_id`s. Hygiene concerns are in problem 1. |
| Backfill with `since` as far back as retention allows | pass | The lake `history/edits` has 168 contiguous hours, from 2 Nov 22:00 to 9 Nov 21:00 (I checked by listing). The first hour is partial: 2,544 rows against about 5,000 normally, which matches the stated 22:27 retention edge. There are no stray partitions. |
| Report size and label balance | pass | v1 manifest: 794 rows (train 467, valid 111, test 216). Weak positives: 51, 9 and 20. Combined positives: 167, 35 and 79. 02b-labelled rows: 259, 46 and 120. Fingerprint `14586b75…`. These match the learning note. |

## Focus areas

- **Leakage.** Sound by construction. `first_edit_at` is fixed when the spike starts (`detector.py` ~278), so the window is known at prediction time. `rule_fired` uses only `started_at <= cutoff` and an `ended_at` that is earlier than `first_edit_at`. Because `ended_at = last_edit_at` and an active spike absorbs the new burst's edits, that comparison is consistent with what a live system knew. Labels are allowed to use the future and are kept separate from the features.
- **Label provenance.** `evals/labels/spike_labels.jsonl` is append-only: the diff has 0 removed lines and 299 added lines, 155 → 454. All 299 new records have `labeler: "llm"`, `model: "ollama:qwen3:4b"` and `run_id: "07a-candidates"`. The file has no human labels, so none were overwritten. `pick_label` would still prefer human labels.
- **Wikimedia politeness.** The stream backfill uses one connection, sends the configured User-Agent (`LiveSource`), backs off on reconnect, and resumes with `Last-Event-ID`. The revision/creation API calls go through `WikiClient` (min interval, `Retry-After`) and are cached forever per revision and per page. Batches are 20 revids. The note reports 3 HTTP 429s in about 2,000 requests, and each was honoured. That is acceptable.
- **Secrets.** I found no email address or secrets in the diff, and nothing under `data/` or `.env` is committed.

## Problems

1. **medium**, `wikipulse/ml/dataset.py` 318–345: the split is by calendar day with no gap (embargo) between train, valid and test. As a result:
   - 17 same-title spike pairs less than 24 h apart straddle a split boundary in v1 (e.g. "Frozen yogurt" train→valid→test, "Ramayana: Part 1", "Pedro Alliana" at 23:54 train / 02:32 valid).
   - Labels of late-train and late-valid spikes are decided by data inside the next split: 7 valid rows have portal days in test, and the 48 h portal window and the LLM's view to `end_at` cross midnight.

   Together this flatters evaluation on a test set of only 2 days.
2. **medium**, `wikipulse/ml/dataset.py` 245–255 and 296: the combined `label` uses provisional weak negatives in valid and test. In v1, `weak_label_final` is 0 for every valid and test row, and 84 test rows plus 59 valid rows get `label=0` from `not-on-portal`, even though the portal days they depend on were less than 3 days old at build time.
3. **medium**, `wikipulse/ml/dataset.py` 245–255: `combined_label` maps "not on portal and unlabelled" to 0, but the project's own numbers say about half of those rows are news (the LLM calls 201 of 391 weak negatives news). So the column described as "the best available label" is roughly a coin flip for 323 rows. It should be null, or kept out of `label`, instead of being a confident 0.
4. **medium**, `datasets/v1/manifest.json` (written by `dataset.py` 689): the only dataset version was built from a dirty tree (`commit ac27528…, dirty: true`), before this spec's code existed in any commit. The very artifact meant to show traceability can't be tied to a commit, so it should be rebuilt as v2 from a30c203 (or the merge commit) before 07b uses it.
5. **low**, `wikipulse/ml/dataset.py` 230–242: `rule_fired` counts a default-rule spike that started days earlier and is still active as "fired for this burst". This makes the 07b baseline look better than a "the rule fired for this burst" definition would. It is a defensible choice but isn't documented in the docstring.
6. **low**, `wikipulse/ml/dataset.py` 276 / `wikipulse/ml/pages.py` 169, 211: page features become NaN when the revision was later deleted or its text hidden. Revision deletion happens after the cutoff and correlates with vandalism, so the missing value carries future information. It affects only 2 rows in v1 (both in test).
7. **low**, `wikipulse/lake/backfill.py` 71–75: `HourCollector.add` drops nothing before `--since`. Stale messages from the inactive data centre (the note mentions 27 Oct events) that pass `is_wanted` would be merged into otherwise-empty old hour partitions and create tiny fake hours. This didn't happen in this lake (no stray partitions), but nothing prevents it.
8. **low**, `wikipulse/ml/dataset.py` 664: `build --dry-run` still deletes and rewrites the Postgres runs `07a-candidates` and `07a-default` unless `--no-db` is also given. A "report only" mode shouldn't have side effects.
9. **low**, `wikipulse/lake/backfill.py` 173: `--prefix` has a hard-coded default of `"history/edits"` instead of the configured setting (`ml_lake_prefix`, or a lake-history setting).
10. **low**, `wikipulse/ml/dataset.py` 623: `BuildOptions.labels` re-hard-codes `evals/labels/spike_labels.jsonl` instead of reusing `wikipulse.eval.label_store.DEFAULT_LABELS_PATH`.
11. **low**, `wikipulse/ml/features.py` 36: `FEATURE_WINDOW_MIN = 10.0` duplicates the config default (`config.py` `feature_window_min`). The two can drift, and `SpikeWindow` silently uses the module constant whenever a caller forgets `window_min`.
12. **low**, `wikipulse/ml/dataset.py` 638 and 296: `weak_label_final` depends on the wall-clock `built_at`, so rebuilding the same lake, labels and code on a later day yields a different fingerprint. The learning note's question 3 implies the fingerprint depends only on the data.

Verdict: fix

The leakage design, label provenance and backfill politeness are good. What needs fixing:
- rebuild v1 from a committed tree;
- stop presenting provisional and unlabelled weak negatives as confident `label=0` (null them, or exclude them);
- add an optional gap between splits, or at least report cross-boundary same-title rows in the manifest.

## Questions for the human before merging

1. A spike on "Pedro Alliana" starts at 23:54 on 6 Nov (train) and another starts at 02:32 on 7 Nov (valid). Why can this make validation scores look better than live performance, even though the split is "by time", and what would a gap between splits change?
2. Why does `compute_features` receive a `SpikeWindow` instead of the `SpikeRecord`? What does `check_no_leakage` actually prove, and what can it *not* catch (hint: the page snapshot's missingness, and labels)?
3. For a test-day spike that isn't on the portal and has no LLM label, v1 stores `label=0`. Given the weak-vs-LLM table in the learning note, how often is that 0 wrong? Would you train or evaluate 07b on `label`, `weak_label` or `label_02b`, and why?
