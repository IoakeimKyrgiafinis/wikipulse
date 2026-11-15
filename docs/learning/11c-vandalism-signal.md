# Learning note: Spec 11c, A vandalism signal for spike detection

## What was built

Spec 02b found that most "not news" spikes were vandalism bursts: someone defaces a page,
a few people fix it, and the burst of editing looks like breaking news to a detector that
only counts edits and editors. This spec first measured which properties of a burst tell
news from not news, and then added the one that helped most to the detector.

```
uv run python -m wikipulse.eval.vandalism                 # the analysis (~8 minutes)
uv run python -m wikipulse.eval.vandalism --no-rules      # the signals only (seconds)
uv run python -m wikipulse.eval.gate                      # the CI gate, with the rule on
```

| File | Role |
|------|------|
| `wikipulse/eval/vandalism.py` (new) | The analysis: labeled bursts per labeler (`labeled_bursts`), revert matching (`reverted_at`), eight signals at three moments (`period_signals`, `moments_of`), the AUC with a bootstrap interval (`auc`, `auc_interval`, `separation`), the detector over the whole lake at six values of the rule (`rule_results`, `start_changes`), the gate fixture at the same values (`gate_results`), and the report. |
| `evals/results/2026-11-16_vandalism.md` / `.json` | The analysis report. |
| `wikipulse/detect/detector.py` | The rule: `SpikeDetector._too_anonymous()`, called in `_maybe_start()`. `is_anonymous()` moved here from `ml/features.py`, so the detector and the classifier share one definition. |
| `wikipulse/detect/windows.py` | `PageWindows.users()`: the users of a page's edits in a window. |
| `wikipulse/config.py`, `.env.example` | `DETECT_MAX_ANON_SHARE` (`DetectorConfig.max_anon_share`), default 0.45; 1 switches the rule off. |
| `wikipulse/eval/gate.py`, `scripts/make_gate_fixture.py` | The gate fixture keeps a flag per edit for anonymous editors (`"a"`, never the name); `fixture_user()` turns it back into a name `is_anonymous` recognises. The label variants keep the rule off, so the labels still cover what the rule drops. |
| `tests/fixtures/detection_gate/edits.jsonl.gz`, `evals/baselines/detection_gate.json` | The fixture with the new flag, and the new baseline. |
| `tests/detect/test_detector.py`, `tests/eval/test_gate.py`, `tests/eval/test_vandalism.py` | The rule (held back, the limit itself, starting later, only the start, old snapshots), the fixture flag, and the analysis (18 tests). |

How the pieces fit:

```
evals/labels/spike_labels.jsonl ──┐      history lake (8 days of edits)
detection_gate_labels.jsonl ──────┤               │
                                  ▼               ▼
            bursts per labeler: human | llm | gate (llm)
                                  │
         signals at start / cutoff / burst ── AUC news vs not news  ─┐
                                  │                                  │  report
   SpikeDetector(max_anon_share = 1.0, 0.75, 0.6, 0.5, 0.45, 0.4)    │  2026-11-16_vandalism.md
     over the lake, and over the gate fixture ── precision, recall ──┘
                                  │
             chosen: 0.45 ──► DetectorConfig default, gate baseline
```

## Key concepts

### Feature analysis before modelling

The obvious plan, from 02b's note, was "count reverts": vandalism gets reverted, so a
burst full of reverts must be vandalism. The spec asked to look first, and the data said
otherwise.

For every labeled burst, `period_signals()` computes eight numbers from the page's edits
in the lake:

- **Reverts.** `revert_share` (reverts among all edits, by edit summary: `is_revert`),
  `reverted_share` (share of the burst's edits that a later revert undid, known at the
  decision moment) and `revert_minutes` (how fast they were undone). `reverted_at()`
  finds what a revert undid: "Undid revision N" names the revision; "Reverted edits by
  X" undoes the last editor's latest run of edits.
- **Who edits.** `anon_share` (share of edits by anonymous editors: IP addresses and
  temporary accounts), `anon_editors` and `registered_editors` (counts) and
  `top_editor_share`. Users are compared in memory only; the report holds counts and
  shares, never a name or an address.
- **What they say.** `vandal_word_share`: edit summaries that mention vandalism,
  unsourced edits, spam and similar (the classifier's `VANDAL_WORDS`).

Each is measured at three *moments* (`moments_of()`): `start`, the window the start rule
saw (only this one can drive a detector rule); `cutoff`, what the classifier sees 10
minutes later; `burst`, the whole burst (hindsight, to describe the data only).

To compare signals with one number, the analysis uses the **AUC**: the chance that a
random not-news burst has a higher value than a random news burst. 0.5 means the signal
says nothing; 1.0 means it separates perfectly; below 0.5 means it is higher in news.
`auc()` computes it from ranks (the Mann-Whitney U statistic), which is the same as
comparing every pair (a test checks that) but fast enough to bootstrap 1,000 times.

The results at the `start` moment (full tables in the report):

| Signal | Human labels (18 news, 68 not news) | LLM labels (361 / 337) |
|---|---|---|
| `revert_share` | 0.53 [0.42, 0.63], no clear separation | 0.58 [0.55, 0.61] |
| `reverted_share` | 0.54 [0.43, 0.63], no clear separation | 0.58 [0.55, 0.61] |
| `revert_minutes` | n = 4 / 18, too few | 0.27 [0.19, 0.37] (faster in not news) |
| **`anon_share`** | **0.75 [0.64, 0.85]** (means 0.14 vs 0.38) | 0.65 [0.61, 0.69] |
| `anon_editors` | 0.67 [0.52, 0.79] | 0.58 [0.54, 0.62] |
| `registered_editors` | 0.29 [0.17, 0.42] (more in news) | 0.38 [0.34, 0.42] |
| `top_editor_share` | 0.40 [0.25, 0.55], no clear separation | 0.51 [0.47, 0.56] |
| `vandal_word_share` | 0.71 [0.61, 0.80] | 0.71 [0.68, 0.74] |

What it says:

- **Reverts barely separate.** Not-news bursts have more of them (5% of edits against 3%),
  but most vandalism bursts have none at all at the start: the vandal's own edits and the
  manual fixes are ordinary edits, and the few tool reverts come later or not at all.
  News bursts get reverts too (edit wars over a breaking story). With the LLM's 700
  labels the difference is "significant" (0.58), and still too small to build a rule on:
  the rule prototype below confirms it.
- **Who edits separates best.** News bursts are carried by registered editors (3.0 on
  average at the start, against 2.3); not-news bursts by logged-out ones (38% of edits
  against 14%).
- **The words in edit summaries** separate almost as well (0.71), and a test of the
  signal and 02b's story at once: "21 of 26 mention vandalism or reverts" was about the
  summaries the LLM labeler read, not about how many reverts there are.

### The rule, and why anonymous share

The detector already sees each edit's user, so the rule is cheap: `_too_anonymous()`
counts the anonymous users among the window's edits (`PageWindows.users()`), and a start
rule only counts while that share is at most `max_anon_share`. It decides only the
*start*: the rule is checked again on every edit, so a burst that registered editors
join can still start later, and a running spike keeps counting every edit. Everything
comes from the events themselves, so replays stay deterministic.

Why this signal and not the summaries: `anon_share` separates at least as well, needs no
word list, and the gate fixture could carry it as a single flag (the fixture keeps no
summaries, only a revert flag). A revert-share rule was prototyped first, outside the
committed code (a subclass of `SpikeDetector` with the same lake runs): holding a start
back while 20%, 34% or 50% of the window's edits were reverts removed at most 1 of the 48
human-labeled not-news bursts. That matches the AUC table.

### Precision/recall trade-offs, and choosing the value

`rule_results()` runs the detector over the whole lake (8 days, 988,885 edits) once per
value and scores the spikes from 3 November 22:00 on (after a day of warm-up):

| `max_anon_share` | Spikes | Precision, human (n) | Human news bursts kept | Precision, LLM (n) | LLM news bursts kept | Gate precision / recall |
|---|---:|---|---:|---|---:|---|
| 1.0 (off) | 482 | 26% [17%, 38%] (68) | 15 of 15 | 56% [52%, 61%] (480) | 245 of 245 | 0.526 / 0.211 |
| 0.75 | 454 | 29% [19%, 41%] (62) | 15 of 15 | 56% (452) | 229 | 0.529 / 0.184 |
| 0.6 | 437 | 28% [19%, 41%] (60) | 15 of 15 | 56% (435) | 224 | 0.529 / 0.184 |
| 0.5 | 367 | 35% [23%, 49%] (49) | 15 of 15 | 64% (365) | 212 | 0.562 / 0.184 |
| **0.45** | **327** | **41% [28%, 57%] (41)** | **15 of 15** | **69% [63%, 73%] (325)** | **202** | **0.600 / 0.184** |
| 0.4 | 324 | 41% [28%, 57%] (41) | 15 of 15 | 69% (322) | 201 | 0.600 / 0.184 |

The choosing rule, written down before picking: **the highest precision by human labels
that loses no human-labeled news burst and at most one gate news burst (the gate's
margin); on a tie, the looser value.** 0.45 and 0.4 tie on human precision; 0.45 keeps one
more LLM-news burst. LLM-label recall is reported but not used to choose, because 12b
showed the LLM calls exactly these bursts (sports, TV) news when a human doesn't. In
practice 0.45 means "anonymous editors made less than about half of the window's edits":
in a 5-edit window, 2 anonymous edits pass and 3 don't.

Reading the numbers honestly:

- **Precision.** By human labels 26% rises to 41%, but the two intervals overlap. The
  stronger evidence is paired: the rule drops 25 human-labeled bursts and *all 25* are
  not news. If it dropped bursts at random from the 63 the run found (15 news), the
  chance of 25 drops without a single news burst would be about 0.0001
  (hypergeometric).
- **Recall.** No human news burst is lost, but there are only 15: with 0 of 15 lost, the
  one-sided 95% upper bound on the loss rate is still 18%. By LLM labels 43 of 245 news
  bursts are lost (18%). Of those 43, 4 also have a human label, all `not news`; the
  rest, by their titles, are mostly football seasons, reality TV and wrestling, but also
  stories with many logged-out editors: US election pages on election night (*Rick
  Jackson* winning a governorship, *2026 United States House of Representatives elections
  in Florida*), *Aramco Tower* after a missile strike, *James Gunn*. Those are real
  losses, and the ROADMAP follow-up asks for human labels on these 43.
- **Time.** 25 spikes start later than without the rule (median 4.6 minutes, p90 55):
  bursts that were held back until registered editors joined. 154 of 482 disappear.

### How the CI gate keeps you honest

The gate scores the default detector on 12 hours of 7 November against 180 frozen labels.
With the rule:

| | Old baseline (rule off) | New baseline (`max_anon_share` 0.45) |
|---|---:|---:|
| precision | 0.526 (10 news of 19 spikes) | **0.600** (9 news of 15 spikes) |
| recall | 0.211 (8 of 38 news bursts) | **0.184** (7 of 38) |
| spikes dropped | | Kusmara, Pathans in India, Emperor of the French (not news), Bigg Boss (Tamil TV series) season 10 (news) |
| `fixture_sha256` | `93b49d71...` | `6ae2e220...` (the new `"a"` flag) |
| `labels_sha256` | `5a0591ca...` | `5a0591ca...` (unchanged) |

Recall drops by exactly one news burst, the most the gate allows (requirement 5), and the
burst is a reality-TV season, the kind 12b's human labels call not news. The gate passes
with the new baseline, and it would also have passed against the old one (precision up,
recall down by the 1-burst margin). **Owner: please review the baseline change**
(`evals/baselines/detection_gate.json` and the fixture are under CODEOWNERS).

The gate kept me honest in a second, less pleasant way: with fewer spikes, its margins
got coarser. The precision margin is two spikes, so 2/15 = 0.133 now instead of 2/19 =
0.105. Changes it used to catch now pass:

| Change (with the rule on) | Precision | Recall | Before 11c |
|---|---:|---:|---|
| `max_anon_share=1.0` (rule off) | 0.526 | 0.211 | (the old baseline) |
| `min_edits=3` (09c's demonstration) | 0.478 | 0.237 | failed |
| 02b's medium thresholds | 0.478 | 0.237 | failed |
| `count_reverts=true` | 0.500 | 0.184 | failed |

Three tests of `tests/eval/test_gate.py` asserted that these changes fail. I kept every
assertion and changed the scenario: "a looser threshold fails on precision" now uses
02b's loose thresholds (precision 0.291, recall 0.842); the 09c demonstration runs on the
detector it was made for (`min_edits=3, max_anon_share=1.0`: 0.324); the command-line
test uses `min_edits=3,min_editors=2` (0.325). This is a real loss of sensitivity, not a
detail, so it is a ROADMAP follow-up (a longer fixture with 40 or more spikes) and part
of the owner's review.

### The gate fixture's new flag

The rule needs to know which edits were anonymous, and the fixture had numbered users
only. `scripts/make_gate_fixture.py` cut the fixture again from the same recording
(`data/recordings/history-20261106T2100Z.jsonl`): every one of the 19,171 lines is
unchanged except for the new `"a"` flag (2,667 edits anonymous), and the note on the info
line. `gate.fixture_user()` names an anonymous fixture user like a temporary account
(`~0000-17-0`), so the detector's own `is_anonymous()` recognises it; a line without the
flag (an older fixture) is a registered user. No IP address or account name is in the
file (a test checks it). The labels were rebuilt with `--labels-only --no-llm` and came
out byte for byte the same; `VARIANTS` now pins the rule off for spec 02b's four
variants and adds the current defaults, so a later rebuild keeps the labels of the bursts
the rule drops (without that, recall's denominator would quietly shrink).

## Decisions and deviations

- **Detector, not classifier.** The classifier already has `anon_share`, `revert_share`,
  `vandal_word_share` and `top_editor_share` among its 43 features (the report lists the
  overlap), so a new feature would add little. The detector had none of them, and the
  gate measures the detector. The classifier is unchanged, so no challenger was trained
  (acceptance criterion 3 doesn't apply). One consequence: the default detector now
  raises about a third fewer spikes, all of them the anonymous-heavy kind, so the
  classifier's live inputs shift; the next weekly retraining (serving mode) trains on the
  new spikes. Noted in the skew follow-up.
- **"Doesn't start" rather than "marked `vandalism`".** A mark would need a new column,
  API and page changes, and would leave the gate's numbers unchanged. Holding the start
  back is one condition in `_maybe_start()`.
- **`max_anon_share` is inclusive** (a share equal to the limit passes) so that 1.0 can
  mean "off". The analysis grid used inclusive values; 0.45 sits between 2/5 and 1/2.
- **`stats["anon_held"]`** counts start checks the rule held back (a burst is checked on
  every edit, so one burst can count several times). Old snapshots don't have the key;
  `_too_anonymous()` uses `stats.get`, and a test loads such a snapshot.
- **Labels.** Human labels first: every number exists for human and LLM labels apart, and
  the value was chosen on the human ones. The human set is 86 bursts (18 news), from
  `02b-baseline` and `07a-candidates`; the LLM set 698 bursts (all `qwen3:4b`); the gate
  set 168 bursts (`qwen3:8b` and copied `qwen3:4b`). One human and one LLM `unsure` were
  left out, and 12 gate `unsure`. No label file was changed; no LLM was called.
- **Edits come from the history lake** (`ML_LAKE_PREFIX`, 2 November 22:00 to 10
  November 23:00), not from the recordings: it covers every labeled burst and reads in a
  second per query. No Wikimedia request was made.
- **The determinism check** (02a) was run twice on the one-hour recording 02a used:
  with the defaults (now 0 spikes, 1 start held back; 02a found 1 spike) and with 02a's
  looser thresholds (17 spikes, 12 still active at the end; 02a found 28 without the
  rule). Both PASS: run B, stopped half way and restarted from its snapshot, gave
  identical spikes in every field.
- **Report date.** The analysis ran shortly after midnight local time; `--date
  2026-11-16` names the report after the local day (the default is the UTC day).
- **README** updated where it stated the old gate numbers and "the share of reverted
  edits is the most promising detection feature", which the analysis contradicts.

## Results

- Analysis: `evals/results/2026-11-16_vandalism.md` (signals for 86 human, 698 LLM and 168
  gate bursts; six rule values over the lake and the gate). About 7 minutes.
- Chosen: `DETECT_MAX_ANON_SHARE=0.45`. Over the lake: 482 spikes become 327; precision
  by human labels 26% [17%, 38%] (n = 68) to 41% [28%, 57%] (n = 41), by LLM labels 56% to
  69%; human news bursts kept 15 of 15, LLM news bursts 202 of 245.
- Gate: precision 0.526 to 0.600, recall 0.211 to 0.184 (one news burst, the margin).
- Checks: `uv run pytest` 1,398 passed, 1 skipped, 81 deselected; `uv run pytest -m
  integration tests/detect tests/eval` 4 passed; `ruff check .` and `ruff format --check
  .` pass; `python -m wikipulse.eval.gate` passes (exit code 0); the determinism check
  passes with the defaults and with looser thresholds.

## Look at it yourself

- The report lists the 25 human-labeled bursts the rule drops (all not news: schools,
  towns, TV, a few articles hit by vandals) and the 151 LLM-labeled ones, news first.
  Read ten of the 43 LLM-news ones: would you call them news?
- Run `python -m wikipulse.eval.gate --set max_anon_share=1.0` and see it pass: what
  does that tell you about the gate after this spec?

## Questions to test understanding

1. 02b's note said "21 of 26 not-news labels mention vandalism or reverts", yet reverts
   turned out to be one of the weakest signals. How can both be true?
2. By LLM labels the rule loses 18% of the news; by human labels none. Which number
   would you quote, and what would you do before trusting either?
3. The rule raised the gate's precision, and also made the gate unable to notice the
   rule being switched off. Why, and what would fix it?

<details>
<summary>Answers</summary>

1. The 21 were about what the LLM labeler *read*: the edit summaries ("rv vandalism",
   "unsourced", "Reverted edits by ..."). A summary that mentions vandalism is indeed a
   strong signal (AUC 0.71). But the *share* of revert edits stays small: a vandalism
   burst is mostly the vandals' own edits and people fixing them by hand, with one or
   two tool reverts that often come after the start. And news bursts have reverts too.
   So "many bursts mention reverts" and "the revert share barely differs" are both true.
2. Neither alone. The human number is the trustworthy labeler on a tiny sample (0 of 15;
   the loss could still be up to 18%). The LLM number is a large sample from a labeler
   known to call sports and TV news, which are just what the rule drops. I would quote
   both, as the report does, and get human labels on the 43 LLM-news bursts the rule
   drops: that turns the uncertain part into a measurement.
3. The gate's precision margin is counted in spikes (2), and the rule left only 15
   scored spikes, so one spike is now 6.7 points and the margin 13.3. Switching the rule
   off lowers precision by 7.4 points, within the margin, and raises recall. A larger
   fixture (more hours, more scored spikes) makes each spike weigh less, so the same
   2-spike margin becomes a finer check; a margin with a floor in percentage points is
   the other option.

</details>
