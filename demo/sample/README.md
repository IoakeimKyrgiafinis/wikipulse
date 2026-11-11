# The quickstart's sample

What `scripts/quickstart.py` replays, so WikiPulse can be tried without a connection to
Wikimedia. Made once by `scripts/make_quickstart_sample.py` from the history lake
(spec 07a) and the `pages` table (spec 03a).

| File | What it holds |
|------|---------------|
| `edits.jsonl.gz` | 58,882 human edits of English Wikipedia articles, 4 November 2026 12:00 UTC to 6 November 00:00 UTC, in event-time order. The first 12 hours are the detector's warm-up; 5 November is the day the quickstart shows. Same compact format as the detection gate's fixture (`wikipulse.eval.gate`): event time, title, a numbered user (no user names or IP addresses) and whether the edit was a revert (no edit summaries). Pages with fewer than three edits in the period are left out. |
| `pages.jsonl.gz` | 134 enriched pages (title, Wikidata id, short description, lead summary, categories, outgoing links): every page a spike of the default or the "medium" thresholds starts on, where the `pages` table had it. All 74 spikes the default thresholds raise on 5 November are covered. |

The page texts are from English Wikipedia and Wikidata, available under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) and CC0 respectively.
