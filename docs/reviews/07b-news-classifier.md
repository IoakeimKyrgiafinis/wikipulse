# Review: 07b-news-classifier

Reviewer: independent agent. Branch `spec-07b-news-classifier` (commit `a33eeca`) against `main`.

## Acceptance criteria

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | Tests on a tiny synthetic dataset (training runs, metrics computed, registration works) | pass | `uv run pytest`: 941 passed, 1 skipped, 71 deselected (80 s). `tests/ml/test_train.py` trains all four models on `tests/ml/synthetic.py` into a SQLite MLflow store, checks the nested runs, metrics, artifacts, the registered version's tags, and that every logged model loads and scores a table. `ruff check .` and `ruff format --check .` are clean. |
| 2 | All four models trained on the real dataset and visible in MLflow; comparison table reported | pass | On the server (`http://127.0.0.1:5000`), experiment `news-classifier` (id 2) has parent run `01d64af9` ("train v2") with four `role=model` children (rule, logreg, gbt, torch), all FINISHED. Their test AP values are 0.6136, 0.9378, 0.9579 and 0.9485, which match the table in the learning note. Registry: `news-classifier` v2 = logreg (tags `dataset_version=v2`, fingerprint `fb6b5a57…`, commit `c498214…`) and v1 = gbt from the earlier session `2ac8eb5b`. |
| – | Registered model loads and reproduces its metrics (extra check) | pass | I loaded `models:/news-classifier/2` through pyfunc and scored the v2 known-label rows. Test AP was 0.9378, 81 of 132 flagged, precision 0.864, recall 0.886, threshold 0.4381. All of these match the logged metrics and tags. v1 (gbt) also reproduced: AP 0.9579, precision 0.791. |
| – | Integration test against the server | not verified by me | `pytest -m integration tests/ml/test_train_server.py` skipped because no server answered at `localhost:5000`. Docker Desktop's engine had stopped during the review (`docker ps` could not reach the daemon), so the skip came from the environment, not the code. The learning note says the test passed for the author. |

ML-correctness checks:

- **Summary leakage:** clean. `page_revision_id` comes from `dataset.revision_at(edits, window.cutoff)`, which is the newest `rev_new` with `time <= cutoff`. `summaries.fetch_leads` fetches exactly those revision ids, so the embedded text is the text at the cutoff, not today's stage-3 summary. The embedding model is pretrained and not fitted on any split.
- **Validation-only choices:** clean, apart from Problem 1. The logreg C and the GBT grid are picked by validation log loss. Torch early-stopping uses the validation loss. Thresholds come from `choose_threshold` on validation. Preprocessing is fitted on training data only. The test split is used only for reporting and for the selection step, which the spec requires.
- **Honest evaluation:** mostly good. There is a bootstrap AP interval and a paired-bootstrap gap, and Brier, ECE and a reliability plot are reported. The rule's ECE equals its Brier score (0.477), which is correct for 0/1 scores.
- **Selection rule:** implemented as documented and covered by a unit test. See Problem 2 for how the rule was arrived at.

## Problems

1. **medium**, `wikipulse/ml/evaluate.py:113-124` (`choose_threshold`): when several thresholds give the same recall, `max((recall, -threshold, threshold))` picks the *lowest* one, so the threshold drops until precision only just meets the target, adding false positives with no recall gain. I measured this on v2. For logreg, the chosen 0.438 lets in 6 validation negatives, while 0.553 has the same validation recall (0.963) at precision 0.839 instead of 0.813. For gbt, 0.136 gives test precision 0.791, below the 0.8 target, while 0.167 has the same validation recall and gives test precision 0.818 with unchanged test recall. The learning note blames the trees' low threshold on overfitting. The main cause is this tie-break, and it also sets the registered model's threshold. The unit test `test_threshold_reaches_the_target_precision_with_the_most_recall` has no equal-recall case, so it doesn't catch this.
2. **medium**, `wikipulse/ml/train.py:249-269` (`select_model`), with learning note lines 243-248: the selection rule changed after the test results were seen. The first rule (fixed margin 0.02) registered gbt as v1. Once the logreg gap turned out to be 0.0201, the rule was replaced and logreg was registered as v2. The new rule is reasonable, but changing a test-set decision rule after seeing the outcome is a forking-paths choice. The note mentions it but doesn't present it as a methodological caveat, and v1 stays in the registry with no marker saying it was superseded.
3. **low**, `docs/learning/07b-news-classifier.md:92-93`: the parameter example `llm:231,portal:48,unknown:179` is wrong. `data_params` counts sources over known rows only, and the logged value is `llm:231,portal:48`.
4. **low**, `wikipulse/ml/models.py:128-130`: the `NewsNet` docstring says "~90 feature columns". There are 43 features, and v2 training adds no missing-value indicators. The learning note itself says 43.
5. **low**, `wikipulse/ml/train.py:381-392` (`log_model`): every model's signature requires `summary_text`, and every model's pip requirements include `torch` and `sentence-transformers`. That holds even for the registered logreg, which reads neither, so serving it in 07c brings in a heavy environment and a column it doesn't use.
6. **low**, `wikipulse/ml/train.py:570-571` (`add_summary_texts`): with `--offline`, revisions missing from the cache silently fall back to title-only text, and only an info-level count is logged. The network can then be trained or evaluated on different inputs than a full run without a warning.
7. **low**, `wikipulse/ml/train.py:82-87` and `wikipulse/ml/models.py:147-156`: the hyperparameter grids and `NetConfig` defaults are hard-coded in modules, not in config. That's defensible for model code, but the network's settings can't be changed per run without editing code.
8. **low**, `tests/ml/test_train_server.py:28-31`: the integration test skips instead of failing when the server is unreachable. A broken MLflow service therefore shows up as "1 skipped" under `-m integration`, which is easy to miss.

No changes outside the spec's scope were found. RustFS instead of MinIO is a documented, justified deviation. No tests were weakened. No real email address or secret appears in the diff (only the existing Compose default credentials). The new dependencies (`mlflow`, `scikit-learn`, `matplotlib`) are explained.

Verdict: fix

## Questions for the human before merging

1. On validation, logreg reaches recall 0.963 at both 0.438 and 0.553. Which threshold should a precision-first news feed use, and what does `choose_threshold` currently do when two thresholds have the same recall?
2. The registered model (logreg, v2) was chosen by a rule adopted after the first rule had picked gbt on the same test split. How much would you trust "logreg is not clearly worse than gbt" here, and what data would let you settle it honestly?
3. All labels are from `qwen3:4b` or portal links. If human labels on the test days disagreed with the LLM on 15 % of spikes, which numbers in the comparison table would still mean something, and which would not?
