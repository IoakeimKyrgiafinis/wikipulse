# Review: Spec 02b, Detection evaluation

Branch `spec-02b-detection-eval`, one commit (`076160e`) on top of `main`. Reviewed against `docs/specs/02b-detection-eval.md` and `AGENTS.md`.

## How I checked

- Read the spec, AGENTS.md, the full diff (34 files) and the learning note.
- `uv run pytest`: 184 passed, 13 deselected. `uv run pytest -m integration tests/eval`: 1 passed. `uv run ruff check .` and `uv run ruff format --check .`: clean.
- Re-ran `wikipulse.eval.detection` for `02b-baseline`, `02b-strict` and `02b-loose` (2026-11-05 to 2026-11-06) against the existing Postgres runs and the cached portal pages, with the network blocked by a mock transport and the results written to the scratchpad. Every metric and the config snapshot match the committed JSON reports exactly.
- `uv run python -m wikipulse.eval.detection --compare 02b-strict 02b-loose` prints the metric differences and the four changed thresholds.
- Ran `wikipulse.eval.label` in LLM mode (`ollama:qwen3:4b`) for all four runs against a copy of the labels file. All four samples of 50 were already fully labeled, so 0 new labels and 0 requests. Then ran it on `02b-strict` into an empty labels file in the scratchpad: 50 sampled, 48 labeled in about 55 s, 9 Wikimedia requests and 88 cache hits. 40 of the 42 spikes that also appear in the committed file got the same label.
- Checked that no file under `data/` or `.env` is committed and that the real contact address appears nowhere in the diff.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| Unit tests with saved fixture API responses (no network): link extraction, lead-link detection, metric calculations on hand-made data | pass | `tests/eval/test_ground_truth.py` uses the saved `portal-2026-11-06.json` and `redirects-*.json` through `httpx.MockTransport` to test link extraction (107 ns-0 links), lead links and redirect chains. `tests/eval/test_metrics.py` and `tests/eval/test_detection.py` cover recall, portal precision, time to detection, label precision and the report on hand-made spikes. All 48 tests pass and none touch the network. |
| A real run over at least 2 days completes, and you report the numbers | pass | `evals/results/2026-11-07_02b-{strict,baseline,medium,loose}.md/.json` cover 2026-11-05 and 2026-11-06, and the numbers are in the learning note. My offline re-run reproduces the baseline, strict and loose metrics exactly. |
| The label tool runs in LLM mode on 50 spikes, and the report includes label-based precision | pass | The committed labels file has 155 LLM records. Every run's 50-spike sample is fully labeled, and each report shows label precision with its counts, labeler and model, marked as an **estimate**. My fresh LLM run on `02b-strict` finished (48 of 50 newly labeled; problem 3 explains the other 2). |
| Req. 1: recall over all links and over lead links | pass | `metrics.recall_for_day` and `pooled_recall`. Both appear in the summary and per-day tables. |
| Req. 2: portal precision labeled as a lower bound | pass | The Markdown shows "Precision against the portal (**lower bound**)" with an explanation. |
| Req. 3: label precision with the label count and labelers, LLM caveat | pass | "Precision from labels (**estimate**)" with labelers and models listed. |
| Req. 4: time to detection, median and p90, for portal-matched spikes | pass | `metrics.time_to_detection` and `ttd_*` in the report. |
| Req. 5: per-day breakdown, config snapshot, git hash | pass, with caveat | All three are present. The config snapshot comes from the evaluating process plus `--set`, not from the run itself (problem 2), and the committed reports point to a dirty `feea4f8` (problem 7). |
| Req. 6: `--compare A B` | pass | Ran it; output is shown above. |
| MediaWiki API politeness and caching in `data/ground_truth/` | pass | `WikiClient` sends the User-Agent, waits between requests, backs off and caches responses. A repeat evaluation makes 0 requests. |
| Out of scope respected (no detector change, no "In the news") | pass | `wikipulse/detect/` is unchanged. |

## Problems

1. **medium**, `wikipulse/eval/ground_truth.py:249-267` (`extract_entries` / `lead_links`): every bullet at every depth counts as an entry, so a nested description bullet adds its first link as a "lead". That link is very often a country, city or generic topic (the 2026-11-05/06 leads include *Mexico*, *Brooklyn*, *Nairobi*, *Golf*, *Spain*, *United States*, *Australia*). This dilutes the "lead links" recall that the report calls "the more meaningful" metric and that all future detector changes will be judged by. A story-level definition (the top-level `*[[Story]]` bullet, or the deepest bullet's subject when there is no parent link) would match the spec's intent ("usually the main subject") better. The choice is documented, but the metric's denominator is about half noise.
2. **medium**, `wikipulse/eval/detection.py:366-368, 703`: the "detector config snapshot" is the evaluating process's `DETECT_*` settings plus `--set`, not the configuration that actually produced the run. If someone forgets or mistypes `--set`, the report and `--compare` silently show the wrong thresholds. `scripts/detect_recordings.py` already knows each variant's config and could store it (for example in a small JSON next to the run, or in a table) without touching the detector.
3. **low**, `wikipulse/eval/metrics.py:212-218` (`Label.covers`), used by `label.py:250` and `metrics.label_precision`: labels match spikes by title and overlapping period. Within a single run the detector emits overlapping spikes of the same article (2 pairs in `02b-strict`, 7 in baseline, 81 in loose; for example *Stephen Miller* 19:02–19:36 and 19:11–20:32). The second spike is then never labeled on its own and silently inherits the first one's label, which is why my fresh `02b-strict` run labeled 48 spikes, not 50. The effect is probably small (it is nearly the same burst), but it isn't mentioned in the learning note.
4. **low**, `wikipulse/eval/detection.py:318-322`: `PORTAL_MARGIN_DAYS = 2` doesn't always cover `PRECISION_WINDOW_H = 48` at the edges. A spike that starts late on `--to` and ends after midnight can be within 48 h of portal day `--to + 3`, which is never fetched. Symmetrically, a spike whose `first_edit_at` is just before `--from` 00:00 is within 48 h of day `--from - 3`. The edge effect is rare, but the margin should come from the window rather than a separate constant.
5. **low**, `wikipulse/eval/wiki_client.py:137-139` and `label.py:166-172`: only 200 responses are cached. A 404 page summary is requested again on every labeling run. A 200 response carrying a MediaWiki error body (for example `maxlag`) is cached forever under `always_fresh`, so a transient API error would permanently empty that spike's context.
6. **low**, `wikipulse/eval/metrics.py:27-32`, `ground_truth.py:177-178`, `label.py:56-59`, `label_store.py:412`, `detection.py:318`: evaluation parameters (recall and precision windows, sample size, seed, cache TTLs, LLM context and timeout) and the `evals/` paths are module constants, not settings in `wikipulse/config.py`. The `evals/` paths are relative to the current working directory, so running from another folder writes labels and results elsewhere. The windows come from the spec, so this is a consistency nitpick rather than a bug.
7. **low**, `evals/results/2026-11-07_02b-*.md:6`: the committed reports record `feea4f8 ... (with uncommitted changes)`, so the commit hash doesn't identify the code that produced them. My offline re-run on `076160e` gives identical numbers, but a report's own provenance should point to a real commit (re-run the reports after committing, or note the producing commit).
8. **low**, `scripts/detect_recordings.py:74-78` vs `wikipulse/eval/detection.py:355-363`: two parsers for the same `k=v,k=v` override syntax. `parse_variant` crashes with an unhelpful `ValueError` on an item without `=`. `delete_run` and `upsert_spikes` also run without a transaction, so a crash between them leaves the run empty.

Observation, not a problem for this spec: the overlapping same-title spikes in problem 3 (sometimes with identical `first_edit_at`) look like a spec 02a detector quirk worth a look in a later detector spec.

Verdict: merge

The evaluation is correct, reproducible offline and well documented. The two medium problems are about how meaningful the lead-recall and config-snapshot numbers are, not about wrong calculations. They should be fixed before the baseline numbers are used to judge detector changes, and can go into a follow-up.

## Questions for the human before merging

1. Lead-link recall is 0% for the baseline, and the lead list includes *Mexico*, *Spain* and *Golf*. What exactly counts as a "lead link" in `ground_truth.lead_links`, and how would changing that definition change the denominator (52) and the meaning of the number you'll use to judge future detectors?
2. The `02b-loose` report says `min_edits=3`. Where does that number come from, and what would the report and `--compare` show if you ran `wikipulse.eval.detection --name 02b-loose` without the `--set` argument?
3. Label precision is computed only on a fixed random sample of 50 spikes per run, and labels are reused across runs when spikes overlap (`Label.covers`). Why is precision over the run's own random sample unbiased, while "all labeled spikes of the run" would not be? And what does reuse by overlap do when one run has two overlapping spikes of the same article?
