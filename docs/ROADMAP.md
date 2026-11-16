# Roadmap: WikiPulse

A live system that detects breaking news from Wikipedia's edit stream, builds a knowledge graph of what's happening, and answers questions about it, with a full MLOps platform around it. Built fully automatically by Claude Code, spec by spec, while you learn from the learning notes in `docs/learning/`. Every stage ends with something working and demoable.

**Free and local:** everything runs on your laptop with Docker. No cloud account needed.
**How it's built:** `/ship-all` ships every spec in order: an implementer subagent builds it, a reviewer subagent checks it once, one round of fixes follows if needed, then it's merged and pushed. Each spec ends with a learning note explaining what was built, the concepts, the results and three self-test questions.

| Stage | What gets built | Done when |
|---|---|---|
| 0. Foundation | Project skeleton, Docker services, CI | CI is green on GitHub |
| 1. Live stream consumer | Crash-safe consumer, recorder, replayer | Live edit rate on screen; restarts lose nothing |
| 2. Spike detection | Spike detector, database, evaluation against Wikipedia's portal | Live trending list plus an evaluation report |
| 3. Event grouping | Cluster related spikes into stories | An event feed of stories, not loose pages |
| 4. Knowledge graph | LLM extraction, Wikidata linking, Neo4j | Browsable graph with accuracy numbers |
| 5. GraphRAG, MCP, agent | MCP server, analyst agent, evals | Questions answered in Claude Desktop |
| 6. Data lake and batch | MinIO archive, event-driven worker, scheduled jobs | Hourly Parquet archive and daily stats |
| 7. Model lifecycle | "Real news" classifier, MLflow, retraining | Versioned model served and retrained on schedule |
| 8. Kubernetes, Helm, Terraform | Cluster deployment, Helm chart, Terraform | Whole system runs on a local cluster |
| 9. Observability and CI/CD | Prometheus, Grafana, Loki, full pipeline | Dashboards live; pipeline goes from commit to deployment |
| 10. Polish | Cleanup, one-command setup | A stranger can run it and understand it |
| 11. Model quality | Skew fix, fresh labels in retraining, vandalism signal | Classifier trained the way it serves; precision up on the gate |
| 12. AI quality | Better Cypher, judge audit, fine-tuned extractor | Numbers checked against human labels; failures down |

Your 16 GB laptop won't run every component at once, and doesn't need to: each stage can be run and demonstrated on its own.

## How to run it

- `/ship-all` ships every remaining spec in order. Run it again any time to resume.
- `/ship-all 05e-api-and-web` stops after that spec (useful for going stage by stage).
- `/ship-spec <name>` ships one spec.

While it runs, read the learning notes as they appear. That's where you learn the concepts.

## Specs

Specs 00 to 10 are done; stages 11 and 12 improve model and AI quality and are ready. Agents adapt later specs to what earlier stages actually built and record any deviations in the learning notes. Each spec has a "What you'll learn" section.

| # | Spec | Status |
|---|---|---|
| 00 | Foundation | done |
| 01 | Live stream consumer | done |
| 02a | Spike detector | done |
| 02b | Detection evaluation | done |
| 03a | Page enrichment | done |
| 03b | Event grouping | done |
| 04a | LLM client | done |
| 04b | Knowledge graph extraction | done |
| 04c | Extraction evaluation | done |
| 05a | GraphRAG retrieval and answering | done |
| 05b | Question-answering evals | done |
| 05c | MCP server | done |
| 05d | Analyst agent | done |
| 05e | API and live web page | done |
| 06a | Data lake archive | done |
| 06b | Event-driven worker | done |
| 06c | Scheduled jobs with Airflow | done |
| 07a | Training dataset | done |
| 07b | News classifier with experiment tracking | done |
| 07c | Serving, monitoring and retraining | done |
| 08a | Kubernetes deployment | done |
| 08b | Helm chart | done |
| 08c | Terraform | done |
| 09a | Metrics, dashboards and alerts | done |
| 09b | Logging and LLM tracing | done |
| 09c | Full CI/CD pipeline | done |
| 10 | Polish and presentation | done |
| 11a | Fix the classifier's training/serving skew | done |
| 11b | Fresh labels for weekly retraining | done |
| 11c | A vandalism signal for spike detection | blocked (rule kept off by owner decision; needs human labels on the news bursts it drops) |
| 12a | Better Cypher generation for question answering | done |
| 12b | Audit the LLM labelers and judges against human labels (needs 50+ human spike labels) | done |
| 12c | Fine-tune a small extraction model | ready |

## Follow-ups

Known problems that were too large for the spec that found them. Each needs its own spec
(or a place in a later one) before it is fixed.

- **Training/serving skew of the news classifier** (found in the 07c review; *partly
  fixed* by spec 11a). Fixed: training and serving compute features through one code
  path (`wikipulse.ml.cutoff`), and datasets are now built from the default detector's
  spikes (`ML_DATASET_MODE=serving`, dataset v5); on v5 as reference, the drift of
  `edits_before_start` is gone (PSI 2.95 -> 0.08 on the 8-10 Nov scores). *Still open:*
  the champion (v3) was trained on the skewed dataset v2; the serving-mode challenger
  (v6) was rejected by the promotion rule (test AP 0.967 vs. 0.971 on the same 72 rows).
  Until a model trained in serving mode is promoted, weekly drift reports (which compare
  with the champion's own training data) keep flagging `edits_before_start`. *Proposed
  fix:* nothing to build; the next weekly retraining trains on serving-mode data, and
  this item can be removed once such a model is champion. *Cost of the serving default:*
  a day becomes a test day only once its portal days are final, about 5 days after it
  ends, so the weekly build's test split lags the end of the lake by about a week (the
  newest ~5 days are `delayed_days` in the manifest), and a lake whose last contiguous
  stretch is shorter than about 10 days makes no dataset at all: `build_dataset` then
  fails with a message saying what to do (fill the gap, wait, or `--mode candidates`),
  and nothing is retrained that week. If spec 11c's vandalism rule is switched on
  (`DETECT_MAX_ANON_SHARE` below 1; it is off by default), the detector holds back
  mostly-anonymous bursts (482 spikes become 367 over the lake at 0.5), so the champion
  would score fewer anonymous-heavy spikes than it was trained on; `anon_share` may then
  look drifted until a model trained on the new spikes is champion. Run the drift check
  after switching it on.
- **The champion overlap guard can't see serving-mode rows** (found in the 11a review).
  `rows_seen_by` (`wikipulse/jobs/retrain.py`) counts the challenger's test rows the
  champion trained or validated on by `spike_id` (`title|started_at`). A candidates-mode
  champion (v3, dataset v2) has ids with the candidate detector's start times, which
  differ from the default detector's, so the guard reports 0 for any serving-mode test
  split even where the champion saw the same burst. *Impact:* a champion that trained
  on the test days isn't flagged as having an unfair advantage. *Proposed fix:* match on
  the page and an overlap of the bursts (`first_edit_at` to the end) instead of the id,
  or ignore it once the champion is a serving-mode model (then the ids agree).
- **The detection eval gate's labels are unreviewed** (found in the 09c review; *checked,
  not fixed* by spec 12b). The CI gate (`wikipulse.eval.gate`) scores against
  `evals/labels/detection_gate_labels.jsonl`: 167 of its 180 labels are `qwen3:8b` labels
  made without the page summary, the other 13 `qwen3:4b` labels copied from 07a, and no
  human has checked them, including the 38 `news` labels that recall rests on (for
  example, "Scene (2026 film)" is `news` because a Reception section was added). Spec 12b
  rebuilt the labels with `scripts/make_gate_fixture.py --labels-only --no-llm` and wrote
  the baseline again: both came out byte for byte the same (precision 0.526, recall 0.211),
  because none of the 87 human spike labels covers the gate's window (7 November, 01:00 to
  13:00 UTC; the human labels end on 6 November). Spec 11c's review rewrote the
  baseline (same numbers; the fixture gained an anonymous flag and the precision margin
  became 5 points, kept between one and two spikes); the labels are unchanged. The 12b
  audit also found the spike
  labeler calls *news* far more often than a human (precision 48% by LLM labels, 27% by
  human labels on the same 02b sample), so the gate's `news` labels are probably too
  many. *Impact:* a blocking CI check measures agreement with an unchecked model.
  *Proposed fix:* a human pass over the gate's bursts, starting with the 38 `news`
  labels. Either label spikes of run `07a-candidates` or `07a-default` in that window with
  `wikipulse.eval.label --manual` (they reach the gate through `spike_labels.jsonl`), or
  give `make_gate_fixture.py` a `--manual` mode over the gate bursts themselves (none
  exists yet). Then `make_gate_fixture.py --labels-only --no-llm`, `--write-baseline`,
  and a reviewed baseline change (CODEOWNERS covers both files).
- **The LLM spike labeler over-calls news** (found by spec 12b). On 49 spikes of run
  `02b-baseline` labeled by both, `qwen3:4b` agrees with the human on 80% (kappa 0.58),
  and all 10 disagreements are LLM `news` against human `not news` (sports seasons and
  players, TV shows). The 07a-candidates sample points the same way (5 of 6, but only 29
  pairs). *Impact:* every LLM-label precision in 02b reads about 20 points too high, and
  the news classifier (07b onward) is trained and tested on these labels. *Proposed fix:*
  a labeler prompt change (out of scope for 12b) measured with `wikipulse.eval.audit`
  against the human labels, then relabeling. Since spec 11b the weekly retraining adds
  `qwen3:4b` labels itself (`label_new_spikes`, `ML_RETRAIN_LABEL_MODEL`), so the biased
  labels grow every week until the prompt is fixed; a new prompt would also need its
  own model setting value or a way to relabel (the task skips any spike some label
  already covers).
- **The gate scores few spikes** (found by spec 11c). The baseline has 19 scored spikes
  (15 with the vandalism rule on), so one spike moves precision by 5 to 7 points. Spec
  11c's review capped the precision margin at 5 points (never less than one spike), which
  makes the 09c demonstration, 02b's medium thresholds and `DETECT_COUNT_REVERTS=true`
  fail again, with the rule on or off; but the gate still can't tell one moved spike
  from noise. *Proposed fix:* a longer fixture (24 to 48 scored hours) so the baseline has
  40 or more spikes, labeled like the current one.
- **The vandalism rule is off: owner decision on the delay trade-off** (found by spec
  11c). Chosen on one half of the lake and checked on the other, every value of
  `DETECT_MAX_ANON_SHARE` below 1 held some spikes back too long on the earlier half
  (p90 delay of held-back spikes 17 to 76 minutes against a 10-minute cap), so the rule
  stays off. In sample, 0.5 raises human-label precision from 26% to 35% and loses none
  of 15 human-labeled news bursts (95% upper bound on the loss rate 18%), but loses 33 of
  245 LLM-labeled news bursts and starts 53 of 482 spikes later (p90 7.5 minutes over
  the whole lake, 17.1 on the earlier half). The delay statistic rests on 19 to 34
  delayed spikes per half, so it is noisy. *Proposed fix:* human labels on the
  LLM-news bursts the rule drops (listed in `evals/results/2026-11-16_vandalism.json`),
  a delay measure on news bursts only (a held-back vandalism burst's delay costs
  nothing), then choose again with `python -m wikipulse.eval.vandalism`. Edit summaries
  that mention vandalism separate news from not news almost as well (AUC 0.71 on human
  labels) and could make the rule sharper, but the gate's fixture keeps no summaries
  (only a revert flag), so it would need a second flag first.
- **No human QA judgements, and no paid-judge step** (found by spec 12b). The audit
  counts 0 human correctness or faithfulness judgements, so the QA numbers are judge-only.
  The spec's optional stronger judge (the `anthropic` backend, allowed only with
  `ANTHROPIC_API_KEY` and `JUDGE_AUDIT_ALLOW_PAID=true`) was skipped because neither was
  set; `wikipulse.eval.audit` reports the condition but has no paid-judge step yet.
  *Proposed fix:* a `--manual` mode for QA judgements, and, if wanted, a paid re-judging
  step over the items where human and LLM disagree.

## Milestones

- **After stage 2:** a live breaking-news detector with a real evaluation. Already a solid portfolio piece.
- **After stage 5:** the complete AI system: live events, knowledge graph, GraphRAG, MCP, agent, web page.
- **After stage 10:** the full platform with model lifecycle, Kubernetes, Terraform, observability and CI/CD.

## Optional extensions

Trading-signal research (event study on companies' pages, research only), a vandalism classifier, comparing events across language editions, fine-tuning a small extraction model, Argo Workflows instead of Airflow.
