# Review: 09b-logging-tracing

Branch `spec-09b-logging-tracing` (7 commits, `79e243f..faaf83f`) against `main`.
Reviewed on 2026-11-11 by an independent reviewer. The review was static, plus local runs of the
test suite, ruff, helm lint and terraform validate/fmt, and a check of the branch's CI run. The
kind cluster was **not** rebuilt, so nothing contacted Wikimedia or Langfuse.

## Acceptance criteria

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | In Grafana, searching one event's correlation ID shows its whole path across services. | pass (from the agent's session evidence; not re-run) | The wiring checks out statically. The id is the starting edit's `event_id`. `detector._maybe_start` puts it on the spike, then it goes to `spikes.correlation_id`, `Event.refresh`, `events.correlation_id`, `EventInput` / the Neo4j `Event` node, and finally `Trigger.brief`'s `log_context`. Alloy sends it as structured metadata, and the `Event trail` dashboard uses `{system="wikipulse"} \| correlation_id="$correlation_id"`. The learning note shows 13 lines for one id across replay, detector, grouper, graph-session and agent-session, fetched through Grafana's Loki data-source proxy. Unit tests cover each hop (`test_a_spike_carries_the_id...`, `test_an_event_carries_the_correlation_id...`, `test_an_event_is_extracted_under_its_correlation_id`, `test_a_run_logs_and_traces_under_the_events_correlation_id`). The integration test `test_correlation_ids_go_from_spikes_to_events_to_the_graph_runner` passed locally. |
| 2 | Tracing works with keys and does nothing without them (tested). | pass | `tests/llm/test_tracing.py` has 12 tests, all passing. **Without keys:** every combination of missing or blank keys gives `NoopTracer`. With `sys.modules["langfuse"]=None` a traced chat still works and no `trace_id` is bound. Errors pass through. **With keys:** the real Langfuse SDK runs with an `InMemorySpanExporter`. The tests check the generation's model, input, output and usage, `session.id` from the correlation id, parent links run -> tool -> chat, ERROR level on failure, the `trace_id` in the log context, and that the context survives `asyncio.to_thread`. |
| - | `uv run pytest` | pass | 1210 passed, 1 skipped, 78 deselected |
| - | `uv run pytest -m integration` on the touched DB tests | pass | `tests/events/test_events_db.py tests/db tests/graph/test_run_db.py`: 17 passed |
| - | `ruff check .` / `ruff format --check .` | pass | "All checks passed!" / "443 files already formatted" |
| - | `helm lint` (default, and dev+offline+replay) | pass | 0 charts failed |
| - | `terraform validate` / `fmt -check` (infra/cluster) | pass | "Success! The configuration is valid." |
| - | CI on the branch | pass | Run 42930481396: terraform, test, dags all green |

## Judgement on the specific choices

- **"Works with keys" test with the real SDK and an in-memory exporter.** This is a good choice. It exercises our adapter on top of the real SDK and checks the OTel attributes Langfuse would send, with no network. It does not prove that the cloud accepts the payload (auth, region, OTLP endpoint), and the note says so honestly. The tests depend on the private attribute names `langfuse.observation.*`, so an SDK upgrade can break them. That is acceptable, because the tests would catch the breakage.
- **The fake Wikimedia for graph extraction.** This is fine as a script for offline sessions only (`scripts/`, not the package), and it never forwards anything. It does make the session's extraction and briefing worse: no burst revisions and no Wikidata links. The note documents this and even uses it as the debugging story.
- **Alloy reading logs through the Kubernetes API.** This is the right trade-off for one kind node under `baseline` PSS: no hostPath and no privileged DaemonSet. The note correctly says that a DaemonSet is the norm at scale.
- **Correlation ids as per-line structured metadata, not labels.** This is correct (cardinality). Loki schema v13 and `allow_structured_metadata` are set, and a test enforces it.
- **The two adapted tests.** Neither was weakened. In `test_same_input_gives_the_same_spikes`, the old version built the input twice with a counter-numbered `event_id`, so the inputs were no longer the same once ids mattered. Building the list once and running two fresh detectors on it still tests determinism. In `test_every_pod_has_an_egress_policy...`, `session-egress` is popped, but the test still asserts that it exists exactly in the replay environment, and the rest of the assertions are unchanged. The datasource test (`test_dashboards_use_the_provisioned_data_sources...`) was generalized: it is still strict per type, and it now also checks that `monitoring.tf` provisions the Loki uid.
- **Leftover `api_cache` rows under the fake URLs.** These can't poison real runs. `request_key` is the sha256 of `{url, params}` and the URL includes the fake host, so real requests never hit those rows, and the enricher purges them once they expire. The derived data is a different matter (see problem 5).
- **The one-off pod network policy.** The idea is good: its own label, ports only, the schema refuses 443, and it is rendered only with `values-replay.yaml`. The "never 443, even by mistake" guarantee has a hole, though (problem 1).

## Problems

1. **medium**: `scripts/cluster.py:344` (with `charts/wikipulse/templates/network-policies.yaml:36-48`). A session pod gets `app.kubernetes.io/name: <--name>`. NetworkPolicies add up, so `run --name graph` (or `consumer`, `enricher`, `grouper`, `api`) also matches that service's `<name>-egress` policy, which allows 443. The claim that a session pod can never reach Wikimedia "even by mistake" is therefore false. `run_pod` should refuse chart service names, or the pod should not reuse that label.
2. **medium**: `wikipulse/events/grouper.py:79`. `Event.refresh()` recomputes the event's correlation id as the earliest spike's on every join and merge. A spike that started earlier but was enriched later (the grouper waits for enrichment), or a merged event that started earlier than the lower-numbered survivor, changes the id after `Event ... opened` was logged. The trail then splits across two ids and the "one search" promise breaks. The docstring's "a merge keeps the survivor's" (lines 22-23) is also not what the code does. Keeping the first-assigned id would make it stable.
3. **low**: `wikipulse/events/grouper.py` (`_merge`, the log call before `survivor.refresh()`). The merge line logs `survivor.correlation_id` before the refresh, so in the case above it records the id the event is about to lose.
4. **low**: `scripts/dashboards.py:591`. With the textbox empty (the default), `| correlation_id=""` matches every line that has no correlation id. The trail panel then shows up to 5000 unrelated lines instead of nothing.
5. **low**: `infra/README.md` / the session design. The graph run's results from fake inputs (extraction fingerprint in Postgres, the `replay:1` Neo4j node with its briefing) stay under run id `replay`. A later real graph run on `replay` would consider them up to date and keep the degraded extraction. The `api_cache` rows themselves are harmless (see above).
6. **low**: `wikipulse/llm/langfuse_tracing.py:15` and `tests/llm/test_tracing.py:15-16`. These import `opentelemetry` (api and sdk) directly, but `pyproject.toml` declares only `langfuse`, so they depend on a transitive dependency.
7. **low**: `wikipulse/llm/client.py:195,206`. Every LLM call is now an INFO line, and every failure is a WARNING here as well as in the callers. Evaluation runs with thousands of calls get much noisier, and failures are logged twice.
8. **low**: `wikipulse/llm/tracing.py:126-133`. `get_tracer()` builds the global tracer lazily without a lock. In a threaded server the first concurrent calls can build it twice (the SDK's per-key singleton makes this mostly harmless).
9. **low**: `scripts/dashboards.py:591` / the correlation-id design. Because ids come from the data, replaying a recording that was also processed live gives identical ids. The trail then mixes the live and replay runs unless the reader filters by `event_key` or `service`. This is documented as a feature, but the dashboard doesn't help separate the two.

Verdict: fix

## Questions for the human before merging

1. A spike on page A starts at 10:00 but is enriched only at 10:20. Meanwhile, page B spiked at 10:05 and opened event 1. A then joins event 1. Which correlation id does event 1 end with, which lines does a search for each id find, and how would you change `Event.refresh()` so that the id never changes?
2. Why does `kubectl run` with `--name graph` give the session pod port 443, although `session-egress` has no 443 and the schema refuses it? What does that tell you about how Kubernetes combines several NetworkPolicies that select the same pod?
3. In `LangfuseTracer.observe`, how does a generation created inside `asyncio.to_thread(client.chat, ...)` end up as a child of the `agent run` span, and why would the same code produce separate traces if the call ran in a thread started with `threading.Thread` instead?
