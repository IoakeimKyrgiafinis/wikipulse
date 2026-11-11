# Measuring a breaking-news detector when nobody knows what the news was

*Draft. Built from the learning notes of specs 02a, 02b, 03b, 07b and 09c. Every number
is from one of those notes; the detection and grouping ones are also committed reports in
`evals/results/`.*

WikiPulse watches every edit to English Wikipedia and raises a "spike" when a page
suddenly gets unusual attention: for example 5 edits from 3 different people within 10
minutes, at least five times what that page normally sees at that hour. Spikes are
usually news. The question this post is about is the word "usually". How do you
measure precision and recall of a news detector when there is no list of all the news?

## Two imperfect truths

There are two obvious candidates for ground truth, and both are wrong in useful ways.

**Wikipedia's own "Current events" portal.** Editors list the day's notable stories,
each with links to articles. If a spike lands on a portal article, it is news, no
argument. But the portal is selective: it covers international politics, disasters and
courts, and ignores most of what actually moves Wikipedia's editors: US election results
by state, deaths, game releases, reality-TV finals, football. So precision measured
against the portal is a *floor*, not an estimate.

**Labels.** Show a spike (the page, its summary, the edit comments of the burst) to a
labeler and ask "news, not news, or unsure?". WikiPulse uses a local LLM (qwen3:4b) for
this, with every record saying who labeled it (`llm` or `human`) and human labels always
winning. Labels can say "this is news the portal never listed", but they are noisy: a
sample of 50 has a 95% margin of about ±14 percentage points, and the labeler has its
own blind spots.

So the evaluation (`wikipulse.eval.detection`) reports both, side by side, on two full
recorded days and four threshold settings:

| Thresholds | Spikes | Recall of portal lead stories | Portal precision (floor) | Labeled precision (n = 50) |
|---|---:|---:|---:|---:|
| strict | 53 | 0.0% | 7.5% | 54% |
| default | 135 | 0.0% | 7.4% | 48% |
| medium | 268 | 3.8% | 5.2% | 58% |
| loose | 1,264 | 13.5% | 3.5% | 50% |

Read naively, the portal column says the detector is terrible (93% false positives) and
the label column says it is a coin flip. Both readings are wrong.

## What the gap between them tells you

Portal precision of 3.5 to 7.5% against label precision of about 50% means that most of
the news the detector finds is news the portal doesn't list. Reading the labels confirms
it: US election results by state, *Susie Wiles* leaving as White House chief of staff,
deaths, a game remake release.

The label column, on the other hand, barely moves between settings that differ by a
factor of 24 in spike count. With 50 labels per setting, 48% and 58% are statistically
indistinguishable. The honest summary is "about half", and anything finer needs more
labels, or a paired design that only labels the spikes where two settings disagree.

The labels were also the best diagnostic tool. 21 of the 26 "not news" labels in the
default setting's sample mention vandalism or reverts: a vandal plus several people
fixing the page by hand looks exactly like a news burst to an edit counter. That
pointed at the next feature to build (the share of reverted edits) better than any
threshold sweep could.

## Recall has a ceiling, and you can measure it

Recall of the portal's lead stories is between 0% and 13.5%, which looks alarming until
you ask how much editing those stories caused at all. Of the 52 lead articles on the two
days, only 21 received *any* human edit in the window around their day, and only 11
received at least 5 edits from 3 people *in total over three days*. Portal stories are
mostly international politics, which English Wikipedia reacts to slowly and with few
editors. No detector of editing bursts can see a story nobody edits, so the ceiling for
this approach on these days is roughly 11 of 52. Measuring the ceiling turned a "bad"
number into a statement about the signal itself.

## Turning a fragile evaluation into a CI gate

A one-off evaluation tells you where you are; a gate keeps you there. WikiPulse's CI runs
the detector on a committed 16-hour recording (240 KB, users replaced by numbers, edit
summaries reduced to a revert flag) and scores it against 180 frozen burst labels. It
needs no network, database or GPU and takes about two seconds. The baseline is precision
0.526 (10 news of 19 spikes) and recall 0.211 (8 of 38 news bursts).

Two lessons from building it:

- **Margins in counts, not percentages.** With 19 spikes, a fixed 5-point margin fails as
  soon as a single spike changes its label. The gate allows a drop worth 2 spikes of
  precision and 1 news burst of recall, which is "small" in a way the data can actually
  express.
- **A spike without a label counts as not news.** A change that makes the detector raise
  new spikes has to earn them by labeling them; the gate lists them.

Lowering `min_edits` from 5 to 3 fails the gate (precision 0.526 to 0.324); raising it to
8 fails it the other way (recall 0.211 to 0.132, in a manual run). Both directions are
kept as tests: `min_edits=3` failing on precision, and spec 02b's strict variant (6 edits
from 4 people; 12 from 5 over an hour) failing on recall.

## The caveat that stays

All of these labels are an LLM's. A downstream news classifier trained on them reaches an
average precision of 0.938, which means "agrees with qwen3:4b very well", not "finds news
very well". The CI gate, too, measures agreement with an unreviewed model. The roadmap
lists a human pass over the 38 `news` labels the gate's recall rests on as the next step.
Imperfect ground truth isn't a reason not to measure; it is a reason to measure from more
than one side, report the margins, and write down exactly what each number means.
