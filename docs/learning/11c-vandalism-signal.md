# Learning note: Spec 11c, A vandalism signal for spike detection

## What was built

Spec 02b found that most "not news" spikes were vandalism bursts: someone defaces a page,
a few people fix it, and the burst of editing looks like breaking news to a detector that
only counts edits and editors. This spec first measured which properties of a burst tell
news from not news, added the one that helped most to the detector as a rule, and chose
its setting on held-out data: the rule ships switched off, for reasons explained below.

```
uv run python -m wikipulse.eval.vandalism                 # the analysis (~8 minutes)
uv run python -m wikipulse.eval.vandalism --no-rules      # the signals only (seconds)
uv run python -m wikipulse.eval.gate                      # the CI gate, with the rule on
```

| File | Role |
|------|------|
| `wikipulse/eval/vandalism.py` (new) | The analysis: labeled bursts per labeler (`labeled_bursts`), revert matching (`reverted_at`), eight signals at three moments (`period_signals`, `moments_of`), the AUC with a bootstrap interval (`auc`, `auc_interval`, `separation`), the detector over the whole lake at six values of the rule (`rule_results`, `start_changes`), the gate fixture at the same values (`gate_results`), the choosing rule and the held-out split (`choose_value`, `folds`, `held_out`), and the report. |
| `evals/results/2026-11-16_vandalism.md` / `.json` | The analysis report. |
| `wikipulse/detect/detector.py` | The rule: `SpikeDetector._too_anonymous()`, called in `_maybe_start()`. `is_anonymous()` moved here from `ml/features.py`, so the detector and the classifier share one definition. |
| `wikipulse/detect/windows.py` | `PageWindows.users()`: the users of a page's edits in a window. |
| `wikipulse/config.py`, `.env.example` | `DETECT_MAX_ANON_SHARE` (`DetectorConfig.max_anon_share`); 1, the default, switches the rule off (chosen on held-out data, see below). |
| `wikipulse/eval/gate.py`, `scripts/make_gate_fixture.py` | The gate fixture keeps a flag per edit for anonymous editors (`"a"`, never the name); `fixture_user()` turns it back into a name `is_anonymous` recognises. The label variants keep the rule off, so the labels still cover what the rule drops. The precision margin is now 5 points, kept between one and two spikes (`allowed_drops`). |
| `tests/fixtures/detection_gate/edits.jsonl.gz`, `evals/baselines/detection_gate.json` | The fixture with the new flag, and the new baseline. |
| `tests/detect/test_detector.py`, `tests/eval/test_gate.py`, `tests/eval/test_vandalism.py` | The rule (held back, the limit itself, starting later, only the start, old snapshots, `anon_held` once per edit), the fixture flag, the margin in points and the restored gate scenarios, and the analysis (26 tests, with the choosing rule and the folds). |

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
   choose_value on one half, judged on the other ──► default 1.0 (off), gate baseline
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
value and scores the spikes from 3 November 22:00 on (after a day of warm-up). These are
**in-sample numbers**: the same data any choice would be made on, so they describe the
values but can't tell you how well a choice holds up.

| `max_anon_share` | Spikes | Precision, human (n) | Human news kept | Precision, LLM (n) | LLM news kept | Held-back spikes: later start / median / p90 delay | Gate precision / recall |
|---|---:|---|---:|---|---:|---|---|
| **1.0 (off, chosen)** | 482 | 26% [17%, 38%] (68) | 15 of 15 | 56% [52%, 61%] (480) | 245 of 245 | 0 | **0.526 / 0.211** |
| 0.75 | 454 | 29% [19%, 41%] (62) | 15 of 15 | 56% (452) | 229 | 14 / 2.9 / 19.3 min | 0.529 / 0.184 |
| 0.6 | 437 | 28% [19%, 41%] (60) | 15 of 15 | 56% (435) | 224 | 18 / 3.1 / 24.7 min | 0.529 / 0.184 |
| 0.5 | 367 | 35% [23%, 49%] (49) | 15 of 15 | 64% (365) | 212 | 53 / 0.4 / 7.5 min | 0.562 / 0.184 |
| 0.45 | 327 | 41% [28%, 57%] (41) | 15 of 15 | 69% [63%, 73%] (325) | 202 | 25 / 4.6 / 55.1 min | 0.600 / 0.184 |
| 0.4 | 324 | 41% [28%, 57%] (41) | 15 of 15 | 69% (322) | 201 | 23 / 4.6 / 64.4 min | 0.600 / 0.184 |

The first version of this spec chose 0.45 from this table. The review found two faults
with that, and they are the two lessons of this section:

1. **Choosing and reporting on the same data.** 0.45 was picked because it had the best
   human precision on these 68 bursts, and then that precision (41%) was reported as the
   result. A value picked as the best of six on a small sample looks better on that
   sample than it will on new data. And the rule for picking it only entered the history
   together with the numbers.
2. **The trade-off that wasn't weighed.** A held-back burst can start later, once
   registered editors join. At 0.45 the spikes that start later do so by 55 minutes at
   the 90th percentile. For a project about catching breaking news early, that is a cost,
   and it was reported but played no part in the choice.

The fix is `choose_value()`, `folds()` and `held_out()` in `wikipulse/eval/vandalism.py`,
committed (9d2f48b) before they were run on the lake:

- **The choosing rule**: of the values that lose none of the period's human-labeled news
  bursts *and* whose held-back spikes start at most `MAX_P90_DELAY_MIN` = 10 minutes late
  (p90 of the delays `start_changes()` measures), the one with the highest human
  precision; on a tie, the looser value. 10 minutes is one short detector window, and
  how long the news classifier waits after a start: a burst held back longer than that
  reaches the page noticeably late. 1.0 (off) is always eligible.
- **Held-out data** (`folds()`): the human labels cover 5 November and 6 November, so the
  lake is cut in two at 6 November 00:00 (`SPLIT_AT`). The rule chooses on one half; the
  value is judged on the other half, which played no part in the choice. The fold that
  chooses on the earlier half and checks on the later one gives the value used (that's
  how a deployment works: choose on the past, run on the future); the reversed fold
  shows whether the other half would agree.

What happened:

| Fold | Chosen | Why | On the held-out half (human labels) |
|---|---|---|---|
| choose on 3-5 Nov, check on 6-10 Nov | **1.0 (off)** | every lower value: p90 delay 17.1 to 76 min on 3-5 Nov | 23% [12%, 41%] (n = 30), 0 of 6 news lost |
| choose on 6-10 Nov, check on 3-5 Nov | 0.5 | 0.75: 11.9 min, 0.45 and 0.4: 33 to 48 min; 0.5: 5.6 min | 38% [22%, 57%] (n = 26), 0 of 9 news lost |

Pooled over both folds (each half judged with the value the *other* half chose): human
precision 30% [20%, 43%] (n = 56), 0 of 15 human-labeled news bursts lost. The rule off
on both halves gives 18 of 67, 27%.

So the procedure picked **1.0: the rule stays off by default**. I kept that, as decided
before running it. It is not a verdict that the rule is useless:

- On held-out data 0.5 helps on both halves: human precision 23% to 30% (n 30 to 23) on
  6-10 Nov and 30% to 38% (n 37 to 26) on 3-5 Nov, with no human news burst lost, at a
  cost of 17 of 151 and 16 of 94 LLM-labeled news bursts. The intervals overlap
  everywhere; the samples are small.
- What stopped it is the delay cap, and the delay statistic is noisy: 0.5 holds back 19
  spikes on the earlier half (p90 17.1 minutes, so about two spikes held more than 10
  minutes) and 34 on the later one (p90 5.6). The p90 also mixes news and vandalism
  bursts; a vandalism burst that starts late costs nothing.
- 0.45, the first version's choice, fails the cap on both halves (76 and 33 minutes).
  Going from 0.5 to 0.45 turns many short holds into long ones.

Turning the rule on is therefore an owner decision about how much delay is acceptable,
not something more analysis of these 15 human news bursts can settle. The ROADMAP
follow-up says what would help: human labels on the news bursts the rule drops, and a
delay measured on news bursts only.

Reading the numbers honestly (all in sample unless the fold table says otherwise):

- **Precision.** At 0.5, 26% rises to 35% by human labels; at 0.45 to 41%. The intervals
  overlap. The paired evidence is stronger: 0.45 drops 25 human-labeled bursts and *all
  25* are not news (0.5: 17 of 17). If 25 drops were random among the 63 found (15
  news), getting no news burst would have a chance of about 0.0001 (hypergeometric).
- **Recall.** No human news burst is lost at any value, but there are only 15: with 0 of
  15 lost, the one-sided 95% upper bound on the loss rate is 18%. By LLM labels 0.5
  loses 33 of 245 news bursts (13%), 0.45 loses 43 (18%), among them US election pages
  on election night (*Rick Jackson*, *2026 United States House of Representatives
  elections in Florida*), *Aramco Tower* after a missile strike, and *James Gunn*.
- **Time.** See the delay column: the median delay is small, the tail is not.

### How the CI gate keeps you honest

The gate scores the default detector on 12 hours of 7 November against 180 frozen
labels, and fails a change whose precision or recall drops more than a margin below the
baseline.

**What the first version did to it.** With the rule at 0.45, the baseline had only 15
scored spikes (precision 0.600, 9 of 15; recall 0.184). The precision margin was counted
as 2 spikes, so it grew to 2/15 = 0.133, and changes the gate used to catch passed: the
09c demonstration (`min_edits=3`, 0.478), 02b's medium thresholds (0.478),
`count_reverts=true` (0.500) and switching the rule off (0.526). Three tests asserted
that such changes fail, and they were rewritten to grosser changes so the suite stayed
green. The review called that what it is, weakening tests, and it was the blocker.

**The fix: a margin in points, kept between one and two spikes** (`allowed_drops()` in
`wikipulse/eval/gate.py`). Precision may drop by `margin_points` = 0.05, but never by
less than one of the baseline's judged spikes (so one moved label is still no false
alarm) and never by more than `margin_spikes` = 2 of them:

```
allowed = max(1 / judged, min(margin_spikes / judged, margin_points))
```

This is where the gate's history pointed: spec 09c started with a fixed 0.05, which
failed as soon as one label flipped (one spike was 0.053), and switched to 2 spikes. The
cap stops the margin from growing when the spike count shrinks; the floor keeps 09c's
fix. A baseline without `margin_points` (an older file) keeps the old rule;
`--write-baseline` adds it, and `--margin-points` sets it.

| | First version's baseline (rule 0.45) | New baseline (rule off) |
|---|---:|---:|
| precision | 0.600 (9 news of 15 spikes) | **0.526** (10 news of 19 spikes) |
| recall | 0.184 (7 of 38 news bursts) | **0.211** (8 of 38) |
| allowed precision drop | 0.133 (2 spikes) | **0.053** (0.05, raised to one spike) |
| allowed recall drop | 0.026 (1 burst) | 0.026 (1 burst) |
| `fixture_sha256` | `6ae2e220...` | `6ae2e220...` (unchanged) |
| `labels_sha256` | `5a0591ca...` | `5a0591ca...` (unchanged) |

Precision and recall equal those on `main` (0.526 / 0.211, where the margin was 0.105):
what changed since `main` is the fixture (the `"a"` flag), the new `max_anon_share`
setting in the stored config, and the margin. **Owner: please review the baseline
change** (`evals/baselines/detection_gate.json` and the fixture are under CODEOWNERS).

The original tests are back, as on `main`, and fail as they should. Every scenario, by
command line (`python -m wikipulse.eval.gate --set ...`):

| Change | Precision | Recall | Against the new baseline | Against a rule-on (0.45) baseline, margin 0.067 |
|---|---:|---:|---|---|
| none (the defaults) | 0.526 | 0.211 | PASS | |
| `min_edits=3` (09c's demonstration) | 0.324 | 0.263 | FAIL | FAIL (0.478) |
| 02b's medium thresholds | 0.343 | 0.263 | FAIL | FAIL (0.478) |
| `count_reverts=true` | 0.333 | 0.211 | FAIL | FAIL (0.500) |
| `min_edits=4` | 0.385 | 0.211 | FAIL | |
| 02b's strict thresholds | 0.429 | 0.079 | FAIL | |
| `max_anon_share=0.45` (rule on) | 0.600 | 0.184 | PASS (recall -1 burst, the margin) | |
| `max_anon_share=1.0` (rule off) | 0.526 | 0.211 | | FAIL |

(Against a rule-on baseline at 0.5, 9 of 16 spikes, margin 0.062, the three original
changes also fail; only switching the rule off passes, 0.562 to 0.526, less than one
spike.)

A new test, `test_with_the_vandalism_rule_on_the_gate_still_sees_small_regressions`,
builds the rule-on baseline in memory and checks that switching the rule off,
`min_edits=3` and `count_reverts=true` all fail it: if the owner turns the rule on, the
gate stays sharp. Two unit tests pin the margin itself. The price of the cap, honestly
stated: on 15 spikes, one moved label plus one extra noise spike now fails the gate
(`test_on_15_spikes_one_moved_spike_passes_and_more_noise_fails`), because that is
numerically the same as `count_reverts=true` (0.600 to 0.500). A gate on 15 spikes can't
tell those apart; a longer fixture could (ROADMAP follow-up).

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
  (acceptance criterion 3 doesn't apply). With the rule off by default, the classifier's
  live inputs don't change either; if the owner turns it on, the detector raises fewer
  anonymous-heavy spikes, and the ROADMAP skew follow-up asks for a drift check then.
- **"Doesn't start" rather than "marked `vandalism`".** A mark would need a new column,
  API and page changes, and would leave the gate's numbers unchanged. Holding the start
  back is one condition in `_maybe_start()`.
- **`max_anon_share` is inclusive** (a share equal to the limit passes) so that 1.0 can
  mean "off". Shares in small windows are coarse: at 0.5, 5 of 10 anonymous edits pass
  and 6 of 11 (0.545) don't. (The first version's comment called 0.45 "less than half",
  which was wrong: 5 of 11, 0.455, is less than half and was held back.)
- **The default is 1.0 (off)**, because the held-out choosing rule picked it (see above).
  This deviates from the spec's expected outcome ("precision improves"): acceptance
  criterion 2 ("on the gate fixture, precision rises") is **not met**, and the spec's
  status is `blocked` on an owner decision. The two options: keep the rule off (the
  analysis, the rule and the sharper gate stay useful), or accept the delay and set
  `DETECT_MAX_ANON_SHARE=0.5` (then write the gate baseline again; the gate's numbers
  would be 0.562 / 0.184, one news burst less, within the margin).
- **Choosing the delay cap.** 10 minutes was set with the whole-lake delay table already
  known (the review quoted it: 7.5 minutes at 0.5, 55 at 0.45), so the cap is not blind;
  what was blind is how the halves would come out, and the rule was committed before
  they were computed. I did not change the cap or the fold after seeing that the earlier
  half rejects 0.5; doing so would repeat the first version's mistake.
- **Grid.** The six values stayed as they were (1.0, 0.75, 0.6, 0.5, 0.45, 0.4), so the
  numbers stay comparable with the first version; 0.45 sits between 2/5 and 1/2. Adding
  values after seeing results would add more chances to fit noise.
- **`stats["anon_held"]`** counts edits on which a start rule passed and the vandalism
  rule held the start back, once per edit (the first version counted inside
  `_too_anonymous()`, so an edit counted twice when both windows were held, and also when
  the long window then started the spike). A burst held back over several edits counts
  on each. Old snapshots don't have the key (`stats.get`), and a test loads one.
- **Bootstrap intervals say how many resamples they rest on.** `auc_interval()` leaves
  out resamples that hold only one class (no AUC); it now returns the count, and the
  report says "(981 of 1000 resamples had both classes)" where some were left out
  (only `revert_minutes`, the signal with 2 to 7 news bursts).
- **Labels.** Human labels first: every number exists for human and LLM labels apart, and
  the value was chosen on the human ones. The human set is 86 bursts (18 news), from
  `02b-baseline` and `07a-candidates`, on 5 and 6 November only; the LLM set 698 bursts
  (all `qwen3:4b`); the gate set 168 bursts (`qwen3:8b` and copied `qwen3:4b`). One human
  and one LLM `unsure` were left out, and 12 gate `unsure`. No label file was changed;
  no LLM was called.
- **Edits come from the history lake** (`ML_LAKE_PREFIX`, 2 November 22:00 to 10
  November 23:00), not from the recordings: it covers every labeled burst and reads in a
  second per query. No Wikimedia request was made.
- **The determinism check** (02a) was run on the one-hour recording 02a used: first with
  the defaults then (0.45: 0 spikes, 1 start held back; 02a found 1 spike) and with 02a's
  looser thresholds (17 spikes); after the review fixes again with the rule on at 0.5 and
  02a's looser thresholds (see Results). All PASS: run B, stopped half way and restarted
  from its snapshot, gave identical spikes in every field.
- **Report date.** The analysis ran shortly after midnight local time; `--date
  2026-11-16` names the report after the local day (the default is the UTC day). The
  review fixes regenerated the same report.
- **Not fixed from the review**: the `# fmt: skip` markers in the analysis tests
  (problem 7) and a drift check for the champion (problem 8; moot while the rule is off,
  kept as a ROADMAP note).

## Results

- Analysis: `evals/results/2026-11-16_vandalism.md` (signals for 86 human, 698 LLM and
  168 gate bursts; six rule values over the lake and the gate; both folds). About 8
  minutes.
- Chosen on held-out data: `DETECT_MAX_ANON_SHARE=1.0`, the rule off.
  - Held out (each half judged with the other half's choice): human precision 30% [20%,
    43%] (n = 56), 0 of 15 human news bursts lost; the rule off on both halves: 27%
    (n = 67).
  - In sample, for the owner's decision: 0.5 gives human precision 35% [23%, 49%] (n =
    49) against 26% [17%, 38%] (n = 68), keeps 15 of 15 human and 212 of 245 LLM news
    bursts, and starts 53 of 482 spikes later (median 0.4, p90 7.5 minutes).
- Gate: baseline 0.526 / 0.211 (as on `main`), allowed precision drop 0.105 to 0.053
  (0.133 in the first version), recall 0.026 unchanged. The 09c demonstration, 02b's
  medium thresholds and `count_reverts=true` fail again (table above).
- Checks: `uv run pytest` 1,410 passed, 1 skipped, 81 deselected; `ruff check .` and
  `ruff format --check .` pass; `python -m wikipulse.eval.gate` passes (exit code 0); the
  determinism check passes (rule on at 0.5 with 02a's looser thresholds: 20 spikes in both
  runs, 13 still active at the end, `anon_held` 16 in both, run B restarted from its
  snapshot half way).

## Look at it yourself

- The report lists the 17 human-labeled bursts 0.5 drops (all not news) and the 111
  LLM-labeled ones, news first. Read ten of the 33 LLM-news ones: would you call them
  news? That is the evidence the decision to turn the rule on needs.
- In the fold tables, compare the "Choose: p90 delay" of 0.5 on the two halves (17.1 and
  5.6 minutes). How many spikes is that p90 computed from, and how much would you trust
  it?
- Run `python -m wikipulse.eval.gate --set count_reverts=true` and see it fail, then
  `--set max_anon_share=0.5` and see it pass.

## Questions to test understanding

1. 02b's note said "21 of 26 not-news labels mention vandalism or reverts", yet reverts
   turned out to be one of the weakest signals. How can both be true?
2. The first version reported 41% precision for 0.45; the held-out procedure picked the
   rule off. Why is the 41% not a fair estimate of what 0.45 would do on new data, and
   what does the held-out split give you instead?
3. Why did the gate's 2-spike precision margin let `count_reverts=true` through with the
   rule on, and how does the cap in points fix that without making a single flipped
   label fail the gate?

<details>
<summary>Answers</summary>

1. The 21 were about what the LLM labeler *read*: the edit summaries ("rv vandalism",
   "unsourced", "Reverted edits by ..."). A summary that mentions vandalism is indeed a
   strong signal (AUC 0.71). But the *share* of revert edits stays small: a vandalism
   burst is mostly the vandals' own edits and people fixing them by hand, with one or
   two tool reverts that often come after the start. And news bursts have reverts too.
   So "many bursts mention reverts" and "the revert share barely differs" are both true.
2. 0.45 was picked *because* it scored best on those 68 human-labeled spikes; part of
   any winner's lead on a small sample is luck, and reporting the winner's score on the
   same sample counts that luck as skill. Choosing on one half and judging on the other
   keeps the luck of the choosing half out of the reported number: the held-out half
   never influenced the choice, so its precision is an honest estimate of the
   procedure. Here it also exposed that the delay cost, which the first choice ignored,
   rules 0.45 out on both halves.
3. The margin was 2 spikes of the baseline's judged spikes; the rule left 15, so it was
   2/15 = 0.133, and `count_reverts=true` cost 0.100 (three more noise spikes). The cap
   limits the margin to 5 points however few spikes there are, but never below one
   spike (0.067 with 15), so one flipped label still passes while the three extra noise
   spikes fail.

</details>
