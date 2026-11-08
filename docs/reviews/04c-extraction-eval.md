# Review: 04c Extraction evaluation

Branch `spec-04c-extraction-eval`, one commit (`6fd7eed`) on top of `main`. Reviewed against `docs/specs/04c-extraction-eval.md` and `AGENTS.md`.

## Acceptance criteria and requirements

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests with saved Wikidata responses | pass | `tests/fixtures/eval/wikidata.json` holds a saved label query and a saved statement query. `tests/eval/test_wikidata.py` answers from it through an `httpx.MockTransport`, and any other query gets an empty answer. `uv run pytest`: 378 passed, 35 deselected. `uv run pytest -m integration tests/eval`: 2 passed. |
| A run over one day; report the numbers | pass (with a documented caveat) | I re-ran `report --run-id 03b-default-fix --events 2026-11-05 --name 04c-v1` into a scratch `--results-dir`: `233 events: linking accuracy 95.9% (484 names), Wikidata support 31.9% (527 relations), labeled precision 93.9% (49 labeled)`, with 0 requests and 77 cache hits. The v2 run (`--from 04c-v2`) gave 96.0% / 35.2% / 91.8%. Apart from `created_at`, `git` and `note`, both JSON reports are identical to the committed `evals/results/2026-11-08_04c-v1.json` and `_04c-v2.json`. `compare 04c-v1 04c-v2` matches the learning note. The run covers only 16:00 to 23:00 UTC, the part of the day that 04b extracted, and the learning note says so. |
| Req 1: linking accuracy against exact Wikidata labels | pass | `check_links` / `LinkCounts` in `extraction_report.py`. Names carried by exactly one item are scored. Names carried by several items are counted apart. The case-variant and excluded-class rules are documented. |
| Req 2: relation support as a lower bound (any property) | pass | `statements_between` asks for truthy `?a ?direct ?b` in both directions. The report calls it "(lower bound)". |
| Req 3: labeled precision, stating the labeler | pass | `LabelCounts`, `labeler_name`. The report says "labeled by llm (ollama:qwen3:8b, judge prompt v2): 50". `evals/labels/relation_labels.jsonl` has 148 LLM records (74 relations × judge v1/v2), no human records, and it is a new file (not in `main`). |
| Req 4: per entity type; prompt version and model in the config snapshot | pass | The `by_type` section and the "By entity type" table. `config.extraction.models` / `prompt_versions` appear in both reports. |
| Labeling step, `--manual` for a human | pass | I ran `label --sample 2 --labels <scratch file>` with qwen3:8b. It labeled 2 relations, and both labels match the committed v2 labels ("correct"). `label_manually` is covered by unit tests. The repository stayed clean (`git status` empty). |
| Ruff | pass | `ruff check .`: all checks passed. `ruff format --check .`: 207 files already formatted. |
| No real email committed | pass | `git grep -i makiskirgia HEAD` finds only an old review sentence. The new fixtures and results contain no User-Agent. |
| 04b graph not modified | pass | Re-extraction goes to `data/extractions/04c-v2.jsonl` (not committed). Neo4j holds only the 233 `03b-default-fix` events. The integration test leaves no orphan entities (0 entities without `INVOLVED_IN`). |
| LLM calls only through `wikipulse.llm` | pass | `get_client(...)` in `extraction.py`. No imports of provider packages. |

## Problems

1. **medium**, `wikipulse/eval/extraction_data.py:363-368, 409-416`: `load_from_graph` takes each entity's `aliases` and `type` from the global `Entity` node. Those values gather names and type votes from every event and every run that ever touched the node, including stale extractions that 04b replaced. So once the graph holds more than one run (the live run), linking accuracy for an `--events` selection silently includes names from outside the selection. Meanwhile `load_from_file` votes types only within the selection, so v1-vs-v2 comparisons by type use different rules. This does not change today's numbers, because the graph holds only `03b-default-fix`.
2. **medium**, `wikipulse/eval/relation_labels.py:181-216` (and the committed reports): judge prompt v2 was written after reading v1's answers on the same 50 relations, and the author's own reading of those answers puts precision near 80%. Yet the headline "Labeled precision 93.9%" in `evals/results/2026-11-08_04c-v1.md` carries no such caveat. The caveat lives only in the learning note, so the result file overstates how much the number can be trusted.
3. **low**, `wikipulse/eval/extraction.py:101`: `EVAL_CACHE_TTL = timedelta(days=30)` is a hard-coded tunable, while the comparable TTL of the graph runner is a setting (`enrich_cache_ttl_h`). It probably belongs in `config.py`. The batch size `50` in `wikidata.py:215` is a bare literal, unlike `NAMES_PER_QUERY` and `PAIRS_PER_QUERY`.
4. **low**, `wikipulse/eval/extraction.py:145-167`: `source_texts` re-fetches the page but never checks that the newest revision it gets matches the label's `relation.source_revision`. If the event's pages in Postgres changed, the judge would silently see different text than the extraction model saw.
5. **low**, `wikipulse/eval/relation_labels.py:95-108` with `wikipulse/eval/extraction.py:203-204`: the report counts the latest LLM label of any model or judge-prompt version. The config snapshot, though, records `judge_model` as the current default and `JUDGE_PROMPT_VERSION` as the current one, so after a `label --model X` run the snapshot can describe the wrong judge. The per-labeler breakdown in the report partly makes up for this.
6. **low**, `wikipulse/eval/extraction.py:290-306`: `extract_event` copies the steps of `GraphRunner._extract` instead of sharing them (documented as a way to avoid refactoring outside the spec). Any later change to the graph pipeline can make the re-extraction drift from what the graph does.
7. **low**, `wikipulse/eval/extraction.py`: no unit tests cover the orchestration code (`source_texts`, `items_to_label`, `selected_numbers`, `config_snapshot`, `run_compare`). Only the pure modules and the Neo4j reader are tested.
8. **low**, `wikipulse/eval/extraction.py:394` and `:401`: `--model` means "judge model" on `report` and `label`, where on `report` it only changes the config snapshot, but "extraction model" on `extract`. Same flag, different meanings.
9. **low**, `docs/learning/04c-extraction-eval.md`: the spec's "What you'll learn" names entity precision and recall, and neither is measured. The note explains why recall is left out, but it does not address entity precision at all.

Verdict: merge

## Questions for the human before merging

1. *Galápagos Islands*, *Paktia* and *Atlanta, Georgia* count as "wrong" links even though the linker picked what the article meant. What property of the ground-truth rule in `wikidata.items_by_label` causes this, and which direction does it push the 95.9%?
2. Relation support only asks for truthy (`wdt:`) statements. Why can a correct relation like "Santiago Peña was president of Paraguay" go unsupported, and why does a supported relation like Saint Peter "crucified" Rome still not show that the relation phrase is right?
3. Judge prompt v2 was tuned while reading v1's answers on the same 50 relations. What does that do to how far you can trust 93.9% and 91.8% as estimates? And what would you change, for example with `label --manual` or a new sample seed, before using labeled precision to decide between two extraction prompts?
