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

Your 16 GB laptop won't run every component at once, and doesn't need to: each stage can be run and demonstrated on its own.

## How to run it

- `/ship-all` ships every remaining spec in order. Run it again any time to resume.
- `/ship-all 05e-api-and-web` stops after that spec (useful for going stage by stage).
- `/ship-spec <name>` ships one spec.

While it runs, read the learning notes as they appear. That's where you learn the concepts.

## Specs

All specs are ready. Agents adapt later specs to what earlier stages actually built and record any deviations in the learning notes. Each spec has a "What you'll learn" section.

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

## Follow-ups

Known problems that were too large for the spec that found them. Each needs its own spec
(or a place in a later one) before it is fixed.

- **Training/serving skew of the news classifier** (found in the 07c review). Training
  rows are spikes of the looser *candidate* detector, with the feature window and cutoff
  anchored at the candidate's start; serving scores spikes of the *default* detector,
  which start later. Every window-based feature is measured at a different moment, and
  values like `edits_before_start == 4` (46 of 152 default-rule training rows in dataset
  v2) never occur when serving. *Impact:* the live news scores come from a distribution
  the model wasn't trained on; drift monitoring flags `edits_before_start` every week.
  *Proposed fix:* build training rows from the default detector's spikes (or score
  candidate-detector spikes live), anchored and cut off exactly as `wikipulse.ml.serve`
  does, as a new dataset version; retrain, compare with the champion, and re-check drift.
  Labels for the new rows need the 02b labeler (`wikipulse.eval.label`) run on them.
- **Weekly retraining needs fresh labels** (found while fixing 07c). The retraining DAG
  now extends the history lake every week (`extend_history`), but the newest spikes have
  no LLM labels and their portal days are not final yet, so most new rows have an unknown
  label and the test split (the newest days) shrinks (dataset v4: 68 labeled and 105
  unknown test rows). *Impact:* a weekly run can reject
  for "too few labeled test rows" although the data is new. *Proposed fix:* a labeling
  task in the DAG before `build_dataset` (`wikipulse.eval.label` on the new candidate
  spikes, within an LLM budget), and a label delay: build the test split from days whose
  portal is final.

- **The detection eval gate's labels are unreviewed** (found in the 09c review). The CI
  gate (`wikipulse.eval.gate`) scores against `evals/labels/detection_gate_labels.jsonl`:
  167 of its 180 labels are `qwen3:8b` labels made without the page summary, and no human
  has checked them, including the 38 `news` labels that recall rests on (for example,
  "Scene (2026 film)" is `news` because a Reception section was added). *Impact:* a
  blocking CI check measures agreement with an unchecked model; a change that moves
  precision or recall may be judged against wrong labels. *Proposed fix:* a human pass,
  starting with the 38 `news` labels, written as `labeler: "human"` records (which win
  over LLM ones), then `--write-baseline` and a reviewed baseline change.

## Milestones

- **After stage 2:** a live breaking-news detector with a real evaluation. Already a solid portfolio piece.
- **After stage 5:** the complete AI system: live events, knowledge graph, GraphRAG, MCP, agent, web page.
- **After stage 10:** the full platform with model lifecycle, Kubernetes, Terraform, observability and CI/CD.

## Optional extensions

Trading-signal research (event study on companies' pages, research only), a vandalism classifier, comparing events across language editions, fine-tuning a small extraction model, Argo Workflows instead of Airflow.
