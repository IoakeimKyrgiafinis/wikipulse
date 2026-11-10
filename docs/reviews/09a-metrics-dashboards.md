# Review: 09a Metrics, dashboards and alerts

Branch `spec-09a-metrics-dashboards` (6 commits, fa639b4..18a53ac) compared with `main`.
Reviewed on 2026-11-10. The review was mostly static. I did not rebuild the cluster, so
the two acceptance criteria are judged from the session evidence in the learning note,
plus my own runs of promtool, Terraform, the tests and CI. No kind cluster is running
(`kind get clusters`: none).

## What I ran

| Check | Result |
|-------|--------|
| `uv run pytest tests/metrics tests/test_observability.py tests/test_chart.py tests/test_infra.py -rs` | 114 passed, 0 skipped. The promtool test ran through the local `quay.io/prometheus/prometheus:v3.12.0` image |
| `uv run pytest` (full unit suite) | 1167 passed, 1 skipped (Airflow) |
| `promtool check rules wikipulse.rules.yaml` (Docker, `--network none`) | SUCCESS: 6 rules found |
| `promtool test rules wikipulse.rules.test.yaml` (Docker) | SUCCESS |
| `terraform fmt -check -recursive` and `terraform validate` in `infra/cluster` | pass, "The configuration is valid" |
| `uv run python scripts/dashboards.py --check` | exit 0 (the JSON files match the generator) |
| `uv run ruff check .` / `ruff format --check .` | pass |
| CI (`gh run list`, run 42906283797 on 18a53ac) | success. But `tests/test_observability.py .......s.`: **the promtool test is skipped in CI** (unit job: 1165 passed, 3 skipped) |
| `git check-ignore infra/cluster/terraform.tfstate` | ignored (`*.tfstate`), so no state is committed |

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| Dashboards show live data during a session. | pass (from the documented session; I did not re-run it) | Learning note, Results: a session from 21:02 to 21:22 UTC used a replay at speed 2 fed into an offline kind cluster. A script ran every panel query through Grafana's data source proxy, and 41 of 46 queries returned data. The 5 empty ones depend on services that are off offline (enricher, graph runner, live scorer). It lists real values per panel and a rate table over time. `scripts/dashboards.py --check` and `test_every_metric_a_dashboard_or_rule_uses_exists` pass, so the committed dashboards are the ones that were tested and only use exported metrics. Caveat: the data was a replay, not the live Wikimedia stream (see the judgment below). |
| Stopping the consumer fires the "no events" alert. | pass (session evidence, plus promtool, which I ran myself) | In the session the replay (the writer) stopped at 21:22:42. `WikiPulseNoEvents` was firing at 21:24:55 ("since 21:24:35"), with the real 2-minute window. Both halves of the rule (writer gone: `absent_over_time`; writer alive but idle: `increase == 0`) are covered by `wikipulse.rules.test.yaml`, which passes in promtool. The in-cluster consumer pod was never the one stopped (it runs at 0 replicas offline). The rule doesn't depend on which process writes, so this is acceptable. |

## Judgment on the choices the caller asked about

- **Replay instead of live data.** Correct. AGENTS.md forbids contacting Wikimedia with
  the placeholder User-Agent contact, and the replay goes through the consumer's own
  class (`Consumer.process`), so it exports exactly the consumer's metrics. Event-time
  pacing (`ReplayLag`) keeps the lag metric meaningful. Using a separate stream
  (`wiki:replay:edits`) and run `replay`, so neither the live state nor the data lake is
  touched, is careful work. One limitation is not stated in the criteria table: "live
  data" here means data flowing live through the pipeline, not the live Wikimedia
  stream. The criterion should be re-checked once a real contact is configured.
- **Monitoring installed via Terraform.** Good choice. The dashboards and rules live
  outside the chart, where Helm can't read them. Terraform already owns the cluster, and
  passing the PodMonitor and rules through the kube-prometheus-stack values avoids a CRD
  dependency in `helm template`/CI. The session showed Terraform planning exactly the
  incremental changes. One drawback: `scripts/cluster.py deploy` users get pods that
  nothing scrapes. This is documented.
- **The mid-session label and NaN fixes.**
  - `start_at_zero` is the right fix (a counter's first increment is invisible to
    `increase()`). Each owner pre-creates only its own labels.
  - The `system="wikipulse"` relabel is sound.
  - The NaN fix is correct in principle: PromQL `max` skips NaN unless every series is
    NaN, and `NaN > 300` is false. But it was only unit-tested in Python and never ran
    in a cluster or in promtool. The promtool lag test still feeds the API's series as
    `0`, not `NaN`, so the scenario the fix is about isn't covered (problem 3).
- **The promtool test skipped in CI.** It should run in CI. It is the only test of the
  rules' *logic*: the Python tests only check names, labels and the strings "> 300" and
  "[2m]". As it stands, a broken alert expression passes CI green. GitHub's Ubuntu
  runners have Docker. A `docker pull quay.io/prometheus/prometheus:v3.12.0` step before
  `uv run pytest` would make the existing test run with no code change. The cleaner
  alternative is a dedicated step that runs `promtool check rules` and `promtool test
  rules`. This is cheap and can't wait for spec 09c (problem 1).

## Problems

1. **medium**, `.github/workflows/ci.yml` (not changed by this branch) and `tests/test_observability.py:165-189`: CI never pulls the Prometheus image, so `test_promtool_checks_and_unit_tests_pass` silently skips there (`.......s.` in run 42906283797), and nothing in CI checks the alert rules' logic.
2. **medium**, `observability/alerts/wikipulse.rules.yaml:58`: `WikiPulseServiceDown` hard-codes `up{namespace="wikipulse"}`, but the namespace is `var.namespace` in Terraform. With another namespace the rule silently matches nothing. It also ignores host targets, while the dashboards select on `system='wikipulse'`.
3. **medium**, `observability/alerts/wikipulse.rules.test.yaml:62-66`: the post-session NaN fix (`catalog.py:189-194`) is not covered by the rule tests. The non-writer series is fed as `0`, not `NaN`, and `WikiPulseDetectorLagHigh` (whose false pending state motivated the fix) has no promtool test at all. The fix was also never run in a cluster.
4. **low**, `observability/alerts/wikipulse.rules.test.yaml`: `WikiPulseDetectorLagHigh` and `WikiPulseServiceDown` have no unit tests. Only 4 of the 6 rules are tested.
5. **low**, `wikipulse/metrics/models.py:526-540`: `collect()` only catches `psycopg.Error`. A malformed report (a feature without `name`, a non-numeric PSI) raises from the collector and fails the API's whole `/metrics` scrape, which would show up as `WikiPulseServiceDown` instead of `wikipulse_model_health_up 0`.
6. **low**, `observability/alerts/wikipulse.rules.yaml:71-77`: `wikipulse_extractions_total{outcome="failed"}` counts retries, so a single poison event retried 4 times in a quiet 30-minute window meets the "at least 4" floor and fires the alert. The comment says that two failures out of three is "not yet a trend", which doesn't match this case.
7. **low**, tests: the API question outcomes (`app.py:226-248`), grouper, archiver, graph extraction and Wikimedia-status counters have no unit tests. The ones that are tested (consumer, detector, LLM, lag, server) are good: they use delta-based assertions, run in a subprocess for the NaN check, and hit a real HTTP scrape.
8. **low**, `.github/workflows/ci.yml:93-101`: the Helm lint loop doesn't include `values-replay.yaml`. `tests/test_chart.py` does render it, so this is only redundancy that's missing.
9. **low**, `tests/test_observability.py:42-50`: `exported_names()` relies on prometheus_client's private `_name`/`_type` attributes, which can break on a library upgrade (the dependency is pinned only as `>=0.21`).
10. **low**, `wikipulse/metrics/models.py:509`: the Postgres `connect_timeout=5` is a literal. It's harmless, but it doesn't follow the AGENTS.md convention that tunables live in `config.py`.

Nothing is out of scope. The replay overlay, the `replay`/`host_metrics_targets`
variables and the Airflow requirements line all serve the acceptance criteria or a
failing lockfile test. The lag's wall-clock use is monitoring, not processing logic,
and this is justified in `stream/lag.py` and the note. No tests were weakened: the
change to `test_chart.py:181` widens an exact-list assertion to include the new policy
and checks that policy tightly. No LLM provider imports were added outside
`wikipulse/llm/`. No dependency was added beyond `prometheus-client`, which the spec
allows.

Verdict: fix

## Questions for the human before merging

1. Every WikiPulse process imports `catalog.py` and so exports `wikipulse_consumer_lag_seconds`. What would `max(wikipulse_consumer_lag_seconds) > 300` have done with the old default of `0` in the API and detector pods, compared with `NaN` now? And which alert takes over when the consumer pod itself is gone, so its lag series no longer exists?
2. `WikiPulseNoEvents` is `increase(...) == 0 or absent_over_time(...)`. Describe one failure that only the first half catches and one that only the second half catches. Why would `rate(...) == 0` alone never fire for a crashed consumer?
3. The drift and score metrics are produced by a collector inside the API, reading Postgres at scrape time, rather than by the weekly drift job pushing them. What happens to these metrics (and to the API's other metrics) when Postgres is down, and what would change if you used a Pushgateway instead?
