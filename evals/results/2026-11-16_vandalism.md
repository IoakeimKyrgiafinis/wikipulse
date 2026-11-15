# Vandalism signals (spec 11c)

Lake `history/edits`, 2026-11-02 22:00 to 2026-11-10 23:00 UTC (hour partitions). Made by `python -m wikipulse.eval.vandalism`.

## Labels

| Label set | File | Labeler (models) | News | Not news | Unsure, left out | With edits in the lake (news) |
|---|---|---|---:|---:|---:|---:|
| human | `spike_labels.jsonl` | human (-) | 18 | 68 | 1 | 86 (18) |
| llm | `spike_labels.jsonl` | llm (ollama:qwen3:4b) | 361 | 337 | 1 | 698 (361) |
| gate (llm) | `detection_gate_labels.jsonl` | llm (ollama:qwen3:4b, ollama:qwen3:8b) | 38 | 130 | 12 | 168 (38) |

A burst is one article's overlapping labeled spikes; each set uses that labeler's latest verdict. Human and LLM numbers are kept apart (spec 12b: the LLM over-calls news).

## Signals

- `revert_share`: reverts (by edit summary, humans and bots) among all the period's edits
- `reverted_share`: share of the period's human non-revert edits already reverted
- `revert_minutes`: median minutes from a reverted edit to its revert (bursts with one)
- `anon_share`: share of the period's human non-revert edits by anonymous editors
- `anon_editors`: distinct anonymous editors (a count)
- `registered_editors`: distinct registered editors (a count)
- `top_editor_share`: share of the period's human non-revert edits by its busiest editor
- `vandal_word_share`: share of human edit summaries mentioning vandalism or similar

Moments: `start` = the window the start rule saw (10 or 60 minutes up to the start); `cutoff` = first edit to 10 minutes after the start (the classifier's view); `burst` = the whole burst (hindsight).

AUC: the chance that a random not-news burst has a higher value than a random news burst (0.5 = no separation, 1 = perfect). Intervals: 95% bootstrap over bursts. Counts are of editors, never names.

### human

| Signal | Moment | n (news / not) | Mean news | Mean not news | AUC [95%] | Reading |
|---|---|---|---:|---:|---|---|
| revert_share | start | 18 / 68 | 0.03 | 0.05 | 0.53 [0.42, 0.63] | no clear separation |
| revert_share | cutoff | 18 / 68 | 0.03 | 0.07 | 0.56 [0.45, 0.66] | no clear separation |
| revert_share | burst | 18 / 68 | 0.03 | 0.05 | 0.52 [0.40, 0.62] | no clear separation |
| reverted_share | start | 18 / 68 | 0.03 | 0.07 | 0.54 [0.43, 0.63] | no clear separation |
| reverted_share | cutoff | 18 / 68 | 0.05 | 0.10 | 0.56 [0.44, 0.66] | no clear separation |
| reverted_share | burst | 18 / 68 | 0.05 | 0.11 | 0.57 [0.46, 0.67] | no clear separation |
| revert_minutes | start | 4 / 18 | 1.57 | 1.49 | 0.42 [0.10, 0.85] | no clear separation |
| revert_minutes | cutoff | 5 / 24 | 1.87 | 1.21 | 0.27 [0.06, 0.52] | no clear separation |
| revert_minutes | burst | 6 / 27 | 1.78 | 2.89 | 0.33 [0.14, 0.54] | no clear separation |
| anon_share | start | 18 / 68 | 0.14 | 0.38 | 0.75 [0.64, 0.85] | higher in not news |
| anon_share | cutoff | 18 / 68 | 0.13 | 0.37 | 0.75 [0.62, 0.84] | higher in not news |
| anon_share | burst | 18 / 68 | 0.12 | 0.37 | 0.75 [0.63, 0.85] | higher in not news |
| anon_editors | start | 18 / 68 | 0.61 | 1.04 | 0.67 [0.52, 0.79] | higher in not news |
| anon_editors | cutoff | 18 / 68 | 0.67 | 1.26 | 0.68 [0.54, 0.81] | higher in not news |
| anon_editors | burst | 18 / 68 | 0.78 | 1.38 | 0.67 [0.52, 0.79] | higher in not news |
| registered_editors | start | 18 / 68 | 3.00 | 2.28 | 0.29 [0.17, 0.42] | higher in news |
| registered_editors | cutoff | 18 / 68 | 3.50 | 2.49 | 0.27 [0.15, 0.41] | higher in news |
| registered_editors | burst | 18 / 68 | 5.28 | 2.72 | 0.21 [0.11, 0.33] | higher in news |
| top_editor_share | start | 18 / 68 | 0.55 | 0.51 | 0.40 [0.25, 0.55] | no clear separation |
| top_editor_share | cutoff | 18 / 68 | 0.59 | 0.49 | 0.31 [0.18, 0.45] | higher in news |
| top_editor_share | burst | 18 / 68 | 0.53 | 0.48 | 0.41 [0.25, 0.58] | no clear separation |
| vandal_word_share | start | 18 / 68 | 0.03 | 0.13 | 0.71 [0.61, 0.80] | higher in not news |
| vandal_word_share | cutoff | 18 / 68 | 0.03 | 0.13 | 0.74 [0.65, 0.83] | higher in not news |
| vandal_word_share | burst | 18 / 68 | 0.02 | 0.13 | 0.76 [0.67, 0.84] | higher in not news |

### llm

| Signal | Moment | n (news / not) | Mean news | Mean not news | AUC [95%] | Reading |
|---|---|---|---:|---:|---|---|
| revert_share | start | 361 / 337 | 0.02 | 0.06 | 0.58 [0.55, 0.61] | higher in not news |
| revert_share | cutoff | 361 / 337 | 0.03 | 0.08 | 0.62 [0.59, 0.66] | higher in not news |
| revert_share | burst | 361 / 337 | 0.02 | 0.07 | 0.58 [0.54, 0.61] | higher in not news |
| reverted_share | start | 361 / 337 | 0.03 | 0.08 | 0.58 [0.55, 0.61] | higher in not news |
| reverted_share | cutoff | 361 / 337 | 0.04 | 0.12 | 0.62 [0.59, 0.65] | higher in not news |
| reverted_share | burst | 361 / 337 | 0.05 | 0.13 | 0.61 [0.58, 0.65] | higher in not news |
| revert_minutes | start | 46 / 92 | 3.63 | 1.10 | 0.27 [0.19, 0.37] | higher in news |
| revert_minutes | cutoff | 65 / 132 | 3.48 | 1.21 | 0.28 [0.21, 0.36] | higher in news |
| revert_minutes | burst | 95 / 148 | 5.63 | 2.68 | 0.30 [0.24, 0.36] | higher in news |
| anon_share | start | 361 / 337 | 0.24 | 0.39 | 0.65 [0.61, 0.69] | higher in not news |
| anon_share | cutoff | 361 / 337 | 0.23 | 0.38 | 0.65 [0.61, 0.69] | higher in not news |
| anon_share | burst | 361 / 337 | 0.23 | 0.38 | 0.66 [0.62, 0.70] | higher in not news |
| anon_editors | start | 361 / 337 | 0.87 | 1.01 | 0.58 [0.54, 0.62] | higher in not news |
| anon_editors | cutoff | 361 / 337 | 0.99 | 1.07 | 0.56 [0.52, 0.60] | higher in not news |
| anon_editors | burst | 361 / 337 | 1.40 | 1.23 | 0.54 [0.50, 0.58] | no clear separation |
| registered_editors | start | 361 / 337 | 2.63 | 2.19 | 0.38 [0.34, 0.42] | higher in news |
| registered_editors | cutoff | 361 / 337 | 3.00 | 2.42 | 0.39 [0.34, 0.43] | higher in news |
| registered_editors | burst | 361 / 337 | 4.54 | 2.75 | 0.34 [0.30, 0.38] | higher in news |
| top_editor_share | start | 361 / 337 | 0.52 | 0.52 | 0.51 [0.47, 0.56] | no clear separation |
| top_editor_share | cutoff | 361 / 337 | 0.50 | 0.51 | 0.51 [0.47, 0.56] | no clear separation |
| top_editor_share | burst | 361 / 337 | 0.47 | 0.49 | 0.55 [0.50, 0.59] | higher in not news |
| vandal_word_share | start | 361 / 337 | 0.03 | 0.13 | 0.71 [0.68, 0.74] | higher in not news |
| vandal_word_share | cutoff | 361 / 337 | 0.03 | 0.13 | 0.73 [0.70, 0.77] | higher in not news |
| vandal_word_share | burst | 361 / 337 | 0.03 | 0.13 | 0.73 [0.70, 0.76] | higher in not news |

### gate (llm)

| Signal | Moment | n (news / not) | Mean news | Mean not news | AUC [95%] | Reading |
|---|---|---|---:|---:|---|---|
| revert_share | start | 38 / 130 | 0.02 | 0.05 | 0.54 [0.49, 0.59] | no clear separation |
| revert_share | cutoff | 38 / 130 | 0.03 | 0.06 | 0.54 [0.48, 0.60] | no clear separation |
| revert_share | burst | 38 / 130 | 0.02 | 0.05 | 0.53 [0.47, 0.59] | no clear separation |
| reverted_share | start | 38 / 130 | 0.02 | 0.07 | 0.55 [0.50, 0.59] | higher in not news |
| reverted_share | cutoff | 38 / 130 | 0.04 | 0.10 | 0.54 [0.48, 0.60] | no clear separation |
| reverted_share | burst | 38 / 130 | 0.05 | 0.11 | 0.55 [0.47, 0.61] | no clear separation |
| revert_minutes | start | 2 / 19 | 1.84 | 2.39 | 0.47 [0.15, 0.80] | no clear separation |
| revert_minutes | cutoff | 5 / 27 | 2.70 | 2.69 | 0.53 [0.21, 0.87] | no clear separation |
| revert_minutes | burst | 7 / 34 | 6.48 | 5.48 | 0.54 [0.27, 0.79] | no clear separation |
| anon_share | start | 38 / 130 | 0.14 | 0.32 | 0.64 [0.56, 0.73] | higher in not news |
| anon_share | cutoff | 38 / 130 | 0.14 | 0.31 | 0.64 [0.56, 0.73] | higher in not news |
| anon_share | burst | 38 / 130 | 0.12 | 0.30 | 0.65 [0.57, 0.73] | higher in not news |
| anon_editors | start | 38 / 130 | 0.29 | 0.63 | 0.64 [0.56, 0.72] | higher in not news |
| anon_editors | cutoff | 38 / 130 | 0.32 | 0.65 | 0.64 [0.56, 0.72] | higher in not news |
| anon_editors | burst | 38 / 130 | 0.34 | 0.69 | 0.64 [0.56, 0.72] | higher in not news |
| registered_editors | start | 38 / 130 | 1.97 | 1.62 | 0.35 [0.27, 0.42] | higher in news |
| registered_editors | cutoff | 38 / 130 | 2.00 | 1.71 | 0.37 [0.29, 0.45] | higher in news |
| registered_editors | burst | 38 / 130 | 2.42 | 1.78 | 0.31 [0.23, 0.40] | higher in news |
| top_editor_share | start | 38 / 130 | 0.69 | 0.66 | 0.45 [0.36, 0.53] | no clear separation |
| top_editor_share | cutoff | 38 / 130 | 0.66 | 0.64 | 0.48 [0.37, 0.58] | no clear separation |
| top_editor_share | burst | 38 / 130 | 0.63 | 0.64 | 0.52 [0.40, 0.62] | no clear separation |
| vandal_word_share | start | 38 / 130 | 0.03 | 0.11 | 0.62 [0.55, 0.68] | higher in not news |
| vandal_word_share | cutoff | 38 / 130 | 0.03 | 0.11 | 0.63 [0.56, 0.69] | higher in not news |
| vandal_word_share | burst | 38 / 130 | 0.03 | 0.10 | 0.62 [0.55, 0.68] | higher in not news |

## The news classifier

Signals the classifier already has as features (measured at the `cutoff` moment, over the burst's human edits): `revert_share` (feature `revert_share`), `anon_share` (feature `anon_share`), `top_editor_share` (feature `top_editor_share`), `vandal_word_share` (feature `vandal_word_share`). Not among its features: `reverted_share`, `revert_minutes`, `anon_editors`, `registered_editors`.

## Candidate rule: `max_anon_share` in the detector

Spikes scored: first edit from 2026-11-03 22:00 UTC on.

| `max_anon_share` | Spikes | Precision, human (n) | News bursts found, human | Not news found, human | Precision, llm (n) | News bursts found, llm | Not news found, llm |
|---|---:|---|---:|---:|---|---:|---:|
| 1.0 (off) | 482 | 26% [17%, 38%] (68) | 15 of 15 | 48 of 48 | 56% [52%, 61%] (480) | 245 of 245 | 210 of 210 |
| 0.75 | 454 | 29% [19%, 41%] (62) | 15 of 15 | 43 of 48 | 56% [51%, 60%] (452) | 229 of 245 | 201 of 210 |
| 0.6 | 437 | 28% [19%, 41%] (60) | 15 of 15 | 42 of 48 | 56% [52%, 61%] (435) | 224 of 245 | 190 of 210 |
| 0.5 | 367 | 35% [23%, 49%] (49) | 15 of 15 | 31 of 48 | 64% [59%, 69%] (365) | 212 of 245 | 132 of 210 |
| 0.45 (chosen) | 327 | 41% [28%, 57%] (41) | 15 of 15 | 23 of 48 | 69% [63%, 73%] (325) | 202 of 245 | 102 of 210 |
| 0.4 | 324 | 41% [28%, 57%] (41) | 15 of 15 | 23 of 48 | 69% [64%, 74%] (322) | 201 of 245 | 100 of 210 |

What each value does to the spikes raised without the rule: kept with the same start, kept but started later (held back until registered editors joined), or dropped.

| `max_anon_share` | Same start | Later start | Delay, median / p90 (min) | Dropped |
|---|---:|---:|---|---:|
| 1.0 | 482 | 0 | n/a / n/a | 0 |
| 0.75 | 440 | 14 | 2.9 / 19.3 | 28 |
| 0.6 | 420 | 18 | 3.1 / 24.7 | 44 |
| 0.5 | 315 | 53 | 0.4 / 7.5 | 114 |
| 0.45 | 303 | 25 | 4.6 / 55.1 | 154 |
| 0.4 | 302 | 23 | 4.6 / 64.4 | 157 |

On the CI gate's fixture (labels: LLM, see the set `gate (llm)`):

| `max_anon_share` | Spikes scored | Precision | Recall (news bursts found) |
|---|---:|---:|---|
| 1.0 | 19 | 0.526 | 0.211 (8 of 38) |
| 0.75 | 17 | 0.529 | 0.184 (7 of 38) |
| 0.6 | 17 | 0.529 | 0.184 (7 of 38) |
| 0.5 | 16 | 0.562 | 0.184 (7 of 38) |
| 0.45 | 15 | 0.600 | 0.184 (7 of 38) |
| 0.4 | 15 | 0.600 | 0.184 (7 of 38) |

### Labeled bursts the rule drops (`max_anon_share` = 0.45)

human: 25 bursts, 0 of them labeled news.

- Rise and Fall (Indian TV series) (2026-11-05 05:27 UTC): not news; llm: news
- Sefton High School (2026-11-05 09:12 UTC): not news; llm: not news
- Gameoverse (2026-11-05 14:24 UTC): not news; llm: not news
- Tanimbar corella (2026-11-05 15:30 UTC): not news; llm: not news
- Ellen Ochoa (2026-11-05 16:59 UTC): not news; llm: not news
- Rolijah Hardy (2026-11-05 17:18 UTC): not news; llm: not news
- History of photography (2026-11-05 19:52 UTC): not news; llm: not news
- RuPaul's Drag Race UK series 8 (2026-11-05 20:37 UTC): not news; llm: news
- Neha Bora (2026-11-05 21:08 UTC): not news; llm: not news
- Alamo Heights High School (2026-11-05 23:28 UTC): not news; llm: not news
- Foundational Black Americans (2026-11-05 23:55 UTC): not news; llm: not news
- University of Warwick Conservative Association (2026-11-06 00:00 UTC): not news; llm: not news
- Leonid Sedov (2026-11-06 00:08 UTC): not news; llm: not news
- 1946: The Mistranslation That Shifted Culture (2026-11-06 03:24 UTC): not news; llm: not news
- Headnoise (2026-11-06 04:30 UTC): not news; llm: news
- Turbofan (2026-11-06 09:01 UTC): not news; llm: not news
- List of Jewish messiah claimants (2026-11-06 09:25 UTC): not news; llm: not news
- Ostrom (2026-11-06 09:33 UTC): not news; llm: not news
- Shakeela (2026-11-06 10:03 UTC): not news; llm: not news
- Anuradha T. K. (2026-11-06 14:26 UTC): not news; llm: not news
- Tidal power (2026-11-06 14:38 UTC): not news; llm: not news
- Leonid Sedov (2026-11-06 15:15 UTC): not news; llm: not news
- Jim McDonald (Coronation Street) (2026-11-06 18:28 UTC): not news; llm: not news
- Dingo (2026-11-06 21:13 UTC): not news; llm: not news
- 2026–27 Everton F.C. season (2026-11-06 21:54 UTC): not news; llm: news

llm: 151 bursts, 43 of them labeled news.
Of those 43 news bursts, 4 also have a `human` label: 0 news, 4 not news.

- 2026 United States House of Representatives elections in Florida (2026-11-04 00:15 UTC): news
- Rick Jackson (businessman) (2026-11-04 02:19 UTC): news
- Darline Graham (2026-11-04 03:40 UTC): news
- David Crowley (Wisconsin politician) (2026-11-04 03:43 UTC): news
- Bigg Boss (Hindi TV series) season 20 (2026-11-04 04:08 UTC): news
- Bigg Boss (Hindi TV series) season 20 (2026-11-04 10:24 UTC): news
- 2026–27 AFC Women's Champions League (2026-11-04 10:53 UTC): news
- Bigg Boss (Tamil TV series) season 10 (2026-11-04 12:29 UTC): news
- 2026 FIFA U-17 Women's World Cup (2026-11-04 16:41 UTC): news
- 2026–27 AFC Women's Champions League (2026-11-04 16:42 UTC): news
- Rise and Fall (Indian TV series) (2026-11-04 18:02 UTC): news
- Jacoby Brissett (2026-11-04 19:49 UTC): news
- Swing state (2026-11-04 20:38 UTC): news
- 2026–27 Arsenal F.C. season (2026-11-04 21:52 UTC): news
- Rise and Fall (Indian TV series) (2026-11-05 05:27 UTC): news; human: not news
- Rodil Zalameda (2026-11-05 05:29 UTC): news
- Greg James (2026-11-05 07:58 UTC): news
- Rise and Fall (Indian TV series) (2026-11-05 13:13 UTC): news
- List of Looney Tunes feature films (2026-11-05 18:11 UTC): news
- RuPaul's Drag Race UK series 8 (2026-11-05 20:37 UTC): news; human: not news
- Headnoise (2026-11-06 04:30 UTC): news; human: not news
- Shipley (surname) (2026-11-06 09:35 UTC): news
- Patriot Platform (2026-11-06 14:11 UTC): news
- Syed Soharwardy (2026-11-06 20:10 UTC): news
- 2026–27 Everton F.C. season (2026-11-06 21:54 UTC): news; human: not news
- Bigg Boss (Tamil TV series) season 10 (2026-11-07 07:56 UTC): news
- 2026–27 Tottenham Hotspur F.C. season (2026-11-07 14:10 UTC): news
- Bigg Boss (Tamil TV series) season 10 (2026-11-07 16:51 UTC): news
- Tiffany Stratton (2026-11-07 18:55 UTC): news
- 2026–27 Manchester City F.C. season (2026-11-07 19:24 UTC): news
- 2026–27 Liverpool F.C. season (2026-11-08 15:52 UTC): news
- Carlos Alcaraz career statistics (2026-11-08 16:20 UTC): news
- List of Formula One World Constructors' Champions (2026-11-08 18:53 UTC): news
- Aramco Tower (2026-11-08 20:47 UTC): news
- Dževad Prekazi (2026-11-08 21:34 UTC): news
- Christopher Bell (racing driver) (2026-11-08 23:25 UTC): news
- Big Brother (Australian TV series) season 17 (2026-11-09 09:48 UTC): news
- James Gunn (2026-11-10 01:43 UTC): news
- Survivor Series: WarGames (2026) (2026-11-10 03:09 UTC): news
- Sheikh Hasina (2026-11-10 20:26 UTC): news
- I'm a Celebrity...Get Me Out of Here! (British TV series) series 26 (2026-11-10 21:32 UTC): news
- Lachie Neale (2026-11-10 22:35 UTC): news
- Harley Reid (2026-11-10 22:43 UTC): news
- Wrinkles the Clown (2026-11-04 01:35 UTC): not news
- Josh Shapiro (2026-11-04 01:53 UTC): not news
- Dan Osborn (2026-11-04 02:36 UTC): not news
- Harris Wofford (2026-11-04 03:28 UTC): not news
- Harpsichord (2026-11-04 03:54 UTC): not news
- Bodybuilding (2026-11-04 07:40 UTC): not news
- Jean de La Fontaine (2026-11-04 14:55 UTC): not news
- ... and 101 more (in the JSON report)
