# Learning note: Spec 12b, Audit the LLM labelers and judges against human labels

## What was built

Until now every number in the README rested on labels written by local LLMs: `qwen3:4b`
said which spikes were news (02b, 07a) and which page pairs were the same story (03b),
`qwen3:8b` judged relations (04c) and QA answers (05b) and labeled the CI gate (09c). The
human has since labeled some of the same items by hand with the `--manual` tools. This
spec measures how far the machines agree with the human, and shows the human-checked
numbers next to the LLM ones.

```
uv run python -m wikipulse.eval.audit                              # the audit (a few seconds)
uv run python scripts/make_gate_fixture.py --labels-only --no-llm  # gate labels, rebuilt
uv run python -m wikipulse.eval.gate --write-baseline              # gate baseline, rewritten
```

| File | Role |
|------|------|
| `wikipulse/eval/audit.py` (new) | The audit. Loads every label file (`LabelFiles.load`), pairs human with LLM labels per family (`spike_comparisons`, `gate_comparisons`, `keyed_comparisons`, `qa_comparisons`), computes agreement (`agreement`: accuracy, `kappa`, `confusion`, `bootstrap` intervals), recomputes the headline numbers from the committed reports (`detection_headline`, `grouping_headline`, `relation_headline`, `qa_headline`), and writes `evals/results/<date>_audit.md` and `.json`. |
| `scripts/make_gate_fixture.py` | `--labels-only` (rebuild the gate's labels from the committed fixture, no 3 GB recording needed), a new label order that keeps the gate's own LLM labels unless a human labeled (`kept_llm_label`), and `--relabel` to ask the LLM again. |
| `wikipulse/config.py`, `.env.example` | `JUDGE_AUDIT_ALLOW_PAID` (default `false`): the opt-in the spec requires for a paid judge. |
| `tests/eval/test_audit.py`, `tests/eval/test_make_gate_fixture.py` (new) | Kappa, intervals, `unsure` handling, pairing rules, the headline recomputation, read-only on labels, every family reported; the gate script's label order. |
| `evals/results/2026-11-15_audit.md` / `.json` | The first audit. |
| `README.md`, `docs/usage.md`, `docs/ROADMAP.md` | Human numbers next to LLM numbers; the commands; follow-ups. |

How the pieces fit:

```
evals/labels/*.jsonl  (read only)            evals/results/*_02b|03b|04c|05b*.json
        │                                                │
  pair each human label with                  rebuild each report's sample, then
  the LLM label of the same item              score it twice: LLM labels only,
  (per model and prompt version)              human labels only
        │                                                │
  accuracy, kappa, confusion,                 precision / recall / correctness,
  bootstrap 95% intervals                     with n and 95% intervals
        └───────────────── audit.md / audit.json ────────┘
```

The audit is pure reading: no network, no database, no LLM. Every LLM number it
recomputes from the label files matches the committed report exactly (the
`llm_matches_report` check), which is how we know the pairing found the right items.

## Key concepts

### Measuring your measuring instrument

An LLM judge is an instrument, like a thermometer. Before you trust what it says about
the system, you check it against a reference you trust more, here the human. The audit
does that per *family* of labels, and per *labeler* (model and prompt version), because
each is a different instrument.

Pairing is the hard part: the human and the LLM must have judged the *same item*.
- Spikes (`spike_comparisons`): same article and overlapping active periods (the rule of
  `Label.covers`), preferring the LLM label from the same run.
- The gate (`gate_comparisons`): a gate burst and any human spike label that overlaps it.
- Page pairs and relations (`keyed_comparisons`): the exact record key
  (`PairLabelRecord.key`, `RelationLabelRecord.key`).
- QA (`qa_comparisons`): run, question and answer hash; faithfulness claim by claim.

### Inter-rater agreement and Cohen's kappa

*Accuracy* (raw agreement) is the share of items where both said the same thing. It
flatters a rater when one answer is common: if 90% of relations are correct, a judge that
says "correct" to everything agrees 90% of the time while knowing nothing.

*Cohen's kappa* corrects for that. It compares the observed agreement `po` with the
agreement two raters would reach by chance with the same habits `pe` (how often each says
each label): `kappa = (po - pe) / (1 - pe)`. 1 is perfect, 0 is chance, below 0 is worse
than chance. The project already had `cohen_kappa` in `wikipulse/eval/qa_metrics.py`
(written for judge-vs-judge agreement in 05b); the audit reuses it, with linear weights
for QA correctness (0, 1, 2 are ordered: 0 vs 2 is a worse miss than 0 vs 1).

Two edge cases the audit handles in `kappa` and `rare_category`:
- If both sides give one and the same label to every item, `pe = 1` and kappa is 0/0.
  `cohen_kappa` returns 1.0 there; the audit says *undefined* instead, and counts such
  bootstrap resamples apart.
- The **kappa paradox**: when one label is rare, kappa swings on one or two items. The
  relations are the textbook case: the human calls 48 of 49 relations correct, the judge
  agrees on 46 of them, and kappa is -0.03. The judge isn't worse than chance; there is
  just almost nothing to tell apart. The report flags it ("one category is rare, so read
  accuracy and the matrix, not kappa").

The *confusion matrix* (rows human, columns LLM) shows the direction of the errors,
which kappa hides. For spikes it is the most useful table of the audit: all 10
disagreements on `02b-baseline` are in one cell, LLM `news` against human `not news`.

### Reporting with small samples

Every number carries its sample size and a 95% interval, and anything under 30 decisive
pairs says "too few to conclude" (`conclusion`, `MIN_CONCLUSIVE`).
- **Bootstrap** (`bootstrap`): resample the paired items with replacement 2,000 times,
  recompute the statistic each time, take the 2.5th and 97.5th percentiles. It works for
  any statistic (accuracy, kappa, mean correctness), which is why the spec asks for it.
  For the grouping recall, which is a stratified estimate, `recall_interval` resamples
  within each stratum.
- **Wilson interval** (`wilson`) for a single proportion such as a precision. The usual
  `p ± 1.96·se` collapses to `[1, 1]` at 49 of 49 and can stretch past 0 or 1; Wilson stays
  sensible at the edges, which matters for 98% precision on 49 items.
- **`unsure`** is treated the same for both sides: a pair where either said `unsure` (or
  a QA judgement without a score, a claim without a verdict) is left out of accuracy and
  kappa and counted in its own column, and shows in the confusion matrix's `unsure` row
  and column.
- A family without any pair says so: "Families without any human-LLM pair: gate, qa.
  Their agreement is unknown, not good or bad."

## Results

Human labels at the time of the audit: 87 spike labels (50 on `02b-baseline`, 37 on
`07a-candidates`), 125 page-pair labels, 49 relation labels, 0 QA judgements, 0 gate
labels, 0 human-written QA questions.

### Agreement (from `evals/results/2026-11-15_audit.md`)

| Family | Scope | LLM labeler | Decisive pairs | Accuracy [95%] | Kappa [95%] | Says positive: human / LLM |
|---|---|---|---:|---|---|---|
| spikes | 02b-baseline | qwen3:4b | 49 (1 human `unsure`) | 80% [67%, 90%] | 0.58 [0.37, 0.79] | news 27% / 47% |
| spikes | 07a-candidates | qwen3:4b | 29 (1 LLM `unsure`, 7 unpaired), too few | 79% [62%, 93%] | 0.39 [-0.06, 0.76] | news 14% / 28% |
| gate | 09c-gate | qwen3:8b and qwen3:4b | 0 | no human labels yet | | |
| grouping | 03b-default-fix | qwen3:4b, prompt 1 | 123 | 68% [60%, 76%] | 0.41 [0.28, 0.53] | same 58% / 28% |
| grouping | 03b-default-fix | qwen3:4b, prompt 2 | 124 | 85% [79%, 91%] | 0.71 [0.59, 0.82] | same 59% / 49% |
| relations | 04c-v1 | qwen3:8b, prompt 1 | 49 | 94% [86%, 100%] | -0.03 (rare category) | correct 98% / 96% |
| relations | 04c-v1 | qwen3:8b, prompt 2 | 48 | 92% [83%, 98%] | -0.03 (rare category) | correct 98% / 94% |
| qa | 05b-v1 | qwen3:8b, qwen3:4b | 0 | no human judgements yet | | |

### Headline numbers, LLM labels next to human labels

| Number | LLM labels | Human labels |
|---|---|---|
| Detection precision, default thresholds (02b-baseline) | 48% [35%, 61%], n = 50 | **27%** [16%, 40%], n = 49 |
| Detection precision, strict / medium / loose | 54% / 58% / 50%, n = 50 each | 27% (n = 22) / 26% (n = 27) / n = 1: too few |
| Grouping precision (03b-default-fix) | 78% [65%, 87%], n = 50 | **98%** [90%, 100%], n = 50 |
| Grouping recall (03b-default-fix) | 23% [18%, 31%] | 25% [21%, 32%] |
| Relation precision (04c-v1, judge prompt 2) | 94% [83%, 98%], n = 49 | **98%** [89%, 100%], n = 49 |
| Relation precision (04c-v2) | 92% [81%, 97%], n = 49 | 100% [87%, 100%], n = 25: too few |
| QA correctness (05b-v1, 0 to 2) | 1.20 [0.88, 1.52], n = 25 | n = 0 |
| QA faithfulness (05b-v1) | 96% [86%, 99%], n = 49 claims | n = 0 |

What it says:
- **The spike labeler over-calls news.** By human labels about a quarter of the default
  detector's spikes are news, not half. The errors all go one way (sports seasons and
  players, TV shows called news), so this is a bias, not noise, and it matters beyond 02b:
  the news classifier of 07b onward was trained and tested on these labels. A prompt
  change is out of scope here; it is a ROADMAP follow-up.
- **The grouping labeler was too strict, and its second prompt is much better.** With
  prompt 1 it called 38 human-`same` pairs `different` (kappa 0.41); prompt 2 reaches
  kappa 0.71. The grouper itself is better than its LLM-measured 78%: 98% of the pairs it
  put together are the same story for the human.
- **The relation judge is close to the human.** 94% vs 98%: the earlier README guess of
  "about 80%" from reading the verdicts didn't hold up against the human labels.
- **QA and the gate are unaudited**: no human labels yet.

### The CI gate: old and new baseline (for the owner's review)

The gate labels were rebuilt with `scripts/make_gate_fixture.py --labels-only --no-llm`
and the baseline with `python -m wikipulse.eval.gate --write-baseline`.

| | Old baseline (09c) | New baseline (12b) |
|---|---:|---:|
| precision | 0.526 (10 news of 19 spikes) | 0.526 (10 news of 19 spikes) |
| recall | 0.211 (8 of 38 news bursts) | 0.211 (8 of 38 news bursts) |
| labels | 180: 38 news, 130 not news, 12 unsure; all LLM | the same file, byte for byte |
| `labels_sha256` | `5a0591ca...` | `5a0591ca...` (unchanged) |

Nothing changed, and that is the honest result: none of the 87 human spike labels covers
a gate burst. The gate evaluates 7 November, 01:00 to 13:00 UTC; the human labels end on
6 November (02b covers 5 and 6 November, and the human's 07a labels stop at 22:15 on the
6th). So the gate still measures agreement with `qwen3:8b`, and the audit suggests its 38
`news` labels are probably too many. **Owner: please review** this (no diff to approve
this time, but the ROADMAP follow-up "The detection eval gate's labels are unreviewed"
now says what a human pass needs). `.github/CODEOWNERS` already covers
`/evals/baselines/`, `/evals/labels/detection_gate_labels.jsonl` and `gate.py`, so any
future change to either file asks for the owner's review.

Before the script change, a plain rebuild *would* have changed the baseline, without any
human involved: 11 gate labels would have been replaced by `qwen3:4b` labels that spec
11a added to `spike_labels.jsonl` for run `07a-default` (precision 0.526 to 0.579, recall
0.211 to 0.225, 38 to 40 news bursts). That is LLM churn, not human judgement, so I
changed the order (see below).

## Decisions and deviations

- **Gate label order changed** (`scripts/make_gate_fixture.py`). Old: kept human label,
  then any `spike_labels.jsonl` label (human or LLM), then a new LLM label, so a rebuild
  re-asked the LLM for 167 bursts. New: kept human label, human `spike_labels` label, the
  gate's own LLM label, LLM `spike_labels` label, new LLM label. A rebuild now changes only
  what humans changed, as the spec's "rebuilt from human judgement" intends; `--relabel`
  restores the old behaviour. Known gap, unchanged from before: a human label already
  copied into the gate file beats a later human correction in `spike_labels.jsonl`.
- **`--labels-only`** reads the committed fixture rather than re-cutting it from the
  3.2 GB recording in `data/`. The fixture is deterministic, so re-cutting it adds time
  and memory and nothing else.
- **No paid judge.** Neither `ANTHROPIC_API_KEY` nor `JUDGE_AUDIT_ALLOW_PAID` is set, so
  that part was skipped, and the report says so ("Paid judge: skipped: ANTHROPIC_API_KEY
  and JUDGE_AUDIT_ALLOW_PAID=true not set"). I added the `JUDGE_AUDIT_ALLOW_PAID` setting
  so the condition is explicit, but no paid-judge step: it could not have been run or
  tested here, and untested code that spends money is worse than none (ROADMAP follow-up).
- **Headline numbers come from the committed reports**, not from the database: the 02b
  sample is rebuilt with the report's seed (`sample_spikes`), the 03b strata and
  populations come from the report, the 04c sample rows are matched back to label keys
  (`relation_row` and `report_row`), and 05b uses the report's primary judge. This keeps
  the audit offline, and the "LLM" column must match the report exactly; it does.
- **Grouping pairs are matched by their two start times**, not by title: the 03b reports
  name pages by canonical title (after redirects) while the labels use the spike title.
- **Kappa per prompt version.** The grouping and relation files hold labels from two
  prompt versions; each is its own instrument, so each gets its own row.
- **`qa questions` is counted only.** A question set isn't a judgement of an item two
  raters could disagree on.
- **Human scope.** Human labels are grouped by the run (spikes) or evaluation
  (`sampled_for`) they were made for; human labels the LLM never judged are counted as
  *unpaired* (7 on `07a-candidates`).
- **30 as the "too few" line** is a convention, not a law; the intervals carry the real
  information.

## Look at it yourself

- The 10 spikes of `02b-baseline` where the LLM said news and you said not news are listed
  in the audit. Is there a pattern the labeler prompt could fix (for example "a new
  episode or match of an ongoing series is not news")?
- The grouping audit has one pair the other way round: you said *Ueno Bank* and *Santiago
  Peña* are different stories, the LLM said the same, and the README still names both as
  one event of the Paraguayan banking crisis. Worth a second look.

## Questions to test understanding

1. The relation judge agrees with the human on 94% of relations, yet its kappa is about
   0. Is the judge useless?
2. Why does the audit compare the LLM's precision *on the human sample* as well as on its
   own full sample?
3. A plain rebuild of the gate labels would have raised its precision from 0.526 to
   0.579. Why was that rebuild rejected, even though the number looks better?

<details>
<summary>Answers</summary>

1. No. Kappa compares agreement with what chance would give *given how often each rater
   uses each label*. The human called 48 of 49 relations correct, so two raters who both
   say "correct" almost always would agree about 94% of the time by chance alone: there
   is almost nothing left for skill to show. This is the kappa paradox; with one rare
   category, read accuracy and the confusion matrix (which shows the 3 or 4 disagreements
   item by item), and get more `wrong` examples before judging the judge.
2. Because the two samples differ. On `02b-medium` the LLM's 58% is over 50 spikes and the
   human's 26% over 27 of them; part of a difference could come from *which* spikes were
   labeled. On exactly the 27 human-labeled spikes the LLM says 52%, so the gap (52% vs
   26%) is the labelers disagreeing, not the samples.
3. Because no human was involved: the 11 changed labels came from `qwen3:4b` labels added
   to `spike_labels.jsonl` in spec 11a. Moving a CI baseline because one LLM's labels
   replaced another's is moving the goalposts with an unchecked instrument, which the
   audit shows over-calls news. A baseline change should follow human judgement, so the
   script now keeps the gate's LLM labels unless a human label replaces them.

</details>
