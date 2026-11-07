# Spec 02b: Detection evaluation

Status: ready
Stage: 2. Spike detection
Owner: agent
Depends on: 01, 02a (the agent part, so the spikes table exists)

## Goal
Measure how well spike detection finds real news, using Wikipedia's own "Current events" portal as ground truth, so every change to the detector can be judged by numbers.

## What you'll learn
- **Evaluation design with imperfect ground truth:** why portal precision is only a lower bound, and how a labeled sample fills the gap (and why LLM labels are only an estimate). This reasoning is what senior data scientists are hired for.
- **Precision, recall and their trade-off:** try a stricter and a looser detector, and watch the two numbers move in opposite directions.
- **Working with the MediaWiki API** politely: caching, User-Agent, rate limits.
- **Labeling as a skill:** while labeling 50 spikes, note your own unclear cases. Unclear cases usually mean the definition of "news" needs sharpening.

## Context
- Each day has a page `Portal:Current events/<YYYY> <Month> <D>` (for example, `Portal:Current events/2026 November 6`) listing that day's events, with links to the relevant articles.
- Many links are generic (countries, cities, organizations mentioned in passing), so portal links are an imperfect ground truth. The design below accounts for that.
- Spikes can be produced for past days without waiting: record history with `stream.record --since <timestamp>` (within the stream's retention window), then replay it.

## Outputs
- `wikipulse/eval/ground_truth.py`: fetches each day's portal page through the MediaWiki API (`action=parse`, `prop=links`), caching responses in `data/ground_truth/`
- `wikipulse/eval/detection.py`: `uv run python -m wikipulse.eval.detection --from <date> --to <date> --name <run name>`
- `wikipulse/eval/label.py`: labels a random sample of 50 spikes as `news`, `not news` or `unsure`. By default an LLM labels them (`EVAL_LABEL_MODEL`, default `ollama:qwen3:8b`), given the page summary and recent edit comments; `--manual` offers a terminal tool for a human instead. Saved to `evals/labels/spike_labels.jsonl`, each record stating its labeler (`llm` or `human`)
- Reports in `evals/results/<date>_<run name>.md` (Markdown) and `.json` (details)

## Requirements
1. **Recall:** the share of portal-linked articles that had a spike within 24 hours of their portal date. Report it twice: over all links, and over "lead" links only (the first link in each portal entry, usually the main subject).
2. **Precision against the portal:** the share of spikes whose title appears on the portal within 48 hours. Label it clearly as a *lower bound*, because many genuine spikes (sports, TV, deaths of minor figures) never reach the portal.
3. **Precision from labels:** report precision on the labeled sample, with the number of labels and who labeled them. LLM labels are an estimate; say so in the report.
4. **Time to detection:** for spikes matching a portal item, the distribution (median, 90th percentile) of the time between the first edit of the burst and the spike's start.
5. The report includes a per-day breakdown, the detector's config snapshot (all thresholds), and the git commit hash.
6. `--compare <run A> <run B>` prints the metric differences.

## Acceptance criteria
- Unit tests with saved fixture API responses (no network): link extraction, lead-link detection, metric calculations on hand-made data.
- A real run over at least 2 days completes, and you report the numbers.
- The label tool runs in LLM mode on 50 spikes, and the report includes label-based precision.

## Look at it yourself (optional, never blocking)
- Relabel some spikes yourself with `--manual` and see how often you agree with the LLM.
- Record the baseline numbers in the devlog. From now on, every detector change is judged by them.

## Out of scope
- Changing the detector
- Wikipedia's "In the news" template (possible later addition)
