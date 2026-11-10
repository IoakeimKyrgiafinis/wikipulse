# 09a: Metrics, dashboards and alerts

## What was built

Every WikiPulse service now counts what it does and serves the numbers on `/metrics`;
Prometheus in the kind cluster collects them every 15 seconds, Grafana shows them on four
dashboards that live in the repository, and alert rules fire when something users would
notice goes wrong.

| Piece | File | What it does |
|-------|------|--------------|
| Metric catalog | `wikipulse/metrics/catalog.py` | Every metric in one place: name, type, labels, why it exists; `start_at_zero()` |
| Metrics server | `wikipulse/metrics/server.py` | `start_metrics_server(settings)`: `/metrics` on `METRICS_PORT`, in a background thread |
| Model health | `wikipulse/metrics/models.py` | `ModelHealthCollector`: drift reports and stored scores read from Postgres at scrape time (in the API) |
| Lag clock | `wikipulse/stream/lag.py` | `WallClockLag` (live consumer), `ReplayLag` (a replay's own event-time clock) |
| Instrumentation | `stream/consumer.py`, `detect/runner.py`, `detect/detector.py`, `llm/client.py`, `graph/run.py`, `enrich/client.py`, `events/run.py`, `lake/archiver.py`, `ml/serve.py`, `api/app.py` | Counters, gauges and histograms updated where the work happens; every `main()` calls `start_metrics_server()` |
| Settings | `wikipulse/config.py`, `.env.example` | `METRICS_PORT` (unset = no server), `METRICS_HOST` (127.0.0.1) |
| Chart | `charts/wikipulse/` | A `metrics` port (9400) on every pod, `METRICS_PORT`/`METRICS_HOST` in the environment, the network policy `metrics-ingress`; `values-replay.yaml` |
| Monitoring stack | `observability/kube-prometheus-stack.yaml`, `infra/cluster/monitoring.tf` | The community chart kube-prometheus-stack (Prometheus, Grafana, the operator), lean; the PodMonitor, rules and dashboard ConfigMaps |
| Dashboards | `scripts/dashboards.py` -> `observability/dashboards/*.json` | Pipeline health, Detection, LLM usage, Model health; generated, committed, loaded by Grafana's sidecar |
| Alerts | `observability/alerts/wikipulse.rules.yaml`, `wikipulse.rules.test.yaml` | Six rules, with `promtool` unit tests |
| Tests | `tests/metrics/`, `tests/test_observability.py`, `tests/test_chart.py`, `tests/test_infra.py` | Metrics move when they should, every metric a dashboard or rule uses exists, the chart's ports and policy |

How the pieces fit:

```
 laptop (Windows)                         kind cluster
 ────────────────                         ───────────────────────────────────────────────────
 replay  :9464/metrics ◄──────┐           namespace wikipulse            namespace monitoring
  (METRICS_PORT=9464)         │            detector  :9400/metrics ◄──┐   Prometheus
  writes wiki:replay:edits    │            grouper   :9400/metrics ◄──┤    scrapes every 15 s:
        │                     │            archiver  :9400/metrics ◄──┤    - PodMonitor "wikipulse"
        ▼                     │            api       :9400/metrics ◄──┤      (port "metrics")
 Redis (Compose) ──► detector │            (consumer, enricher, graph:│    - host target "replay"
                              │             0 replicas offline)       │    evaluates the rules
                              └── host.docker.internal:9464 ──────────┴──  (wikipulse.rules.yaml)
                                                                          Grafana
                                     ConfigMaps dashboard-*.json ──sidecar──► folder "WikiPulse"
```

Terraform installs everything (`uv run python scripts/infra.py cluster apply ...`): the
WikiPulse release as before, plus the monitoring namespace, the kube-prometheus-stack
release and one ConfigMap per dashboard file (`infra/README.md`, section Monitoring).

## Key concepts, with this project's code

### Pull, scrape, and a server thread per process

Prometheus *pulls*. Each service only keeps its numbers in memory; every 15 seconds
Prometheus asks `GET /metrics` and stores what it reads with a timestamp (a *sample*).
`start_metrics_server()` starts the client library's small HTTP server in a daemon
thread, on its own port, so it answers even when the service's main loop is stuck (its
frozen counters then *show* the problem), and the API's Host-header check doesn't stand
in Prometheus' way. Each process has its own numbers: there is no shared state between
the detector and the consumer, which is why each pod is scraped separately.

Prometheus finds the pods through a **PodMonitor** (a resource type of the Prometheus
Operator): "pods labeled `app.kubernetes.io/part-of: wikipulse` in namespace
`wikipulse`, port named `metrics`". Each target gets labels `namespace`, `pod` and,
through a relabeling, `service` (the pod's `app.kubernetes.io/name`), so dashboards can
say "memory per service". Processes outside the cluster (the replay on the laptop) are
listed as static targets (`host_metrics_targets`). The pods' default-deny network policy
got one hole: `metrics-ingress` lets pods of the namespace `monitoring` reach port 9400,
nothing else.

### The metric types, and when each fits

All defined in `wikipulse/metrics/catalog.py`:

- **Counter**, only goes up: `wikipulse_consumer_messages_total{outcome}`,
  `wikipulse_spikes_started_total{window}`, `wikipulse_llm_tokens_total{direction}`,
  `wikipulse_extractions_total{outcome}`. The raw total is meaningless (it restarts at 0
  with the process); what you query is `rate(x[1m])` (per second) or `increase(x[1h])`,
  which understand restarts. "Events per second" on the dashboard is
  `sum(rate(wikipulse_consumer_messages_total{outcome="kept"}[1m]))`.
- **Gauge**, goes up and down: `wikipulse_consumer_lag_seconds`, `wikipulse_spikes_active`,
  `wikipulse_detector_stream_lag_entries` (the queue), `wikipulse_drift_score_psi`. You
  query its value directly, or `max`/`avg` over pods.
- **Histogram**, a distribution: `wikipulse_llm_request_duration_seconds` counts each
  request into buckets (`le="5"`: at most 5 s), so
  `histogram_quantile(0.95, sum by (le) (rate(..._bucket[5m])))` estimates the 95th
  percentile *across all pods*. An average would hide the slow requests; a percentile
  computed inside one process (a "summary") can't be combined across processes. Buckets
  are fixed in advance from what the values usually are (0.5 s to 300 s for an LLM call).

Two rules that came up while building:

- **Labels are dimensions, and every combination is a series.** `outcome`, `window`,
  `model`, `direction` have a handful of values; a page title or a user name as a label
  would create millions of series and bring Prometheus down.
- **Start labeled counters at 0** (`start_at_zero()`). A labeled series only exists after
  its first `.inc()`, so it *appears* at 1, and `increase()` needs two samples: the first
  spike of a run was invisible, and the panel said "No data" instead of 0. Found in the
  session (see deviations); the owner of each counter now creates its label values at 0
  when it starts.

### What to watch in a streaming system

- **Throughput**: edits written per second, entries the detector processed per second.
  Two lines on one panel ("Edits in and out of the detector"); apart for long means a
  backlog.
- **Lag**, two kinds:
  - *the writer's freshness*: `wikipulse_consumer_lag_seconds` = "now" minus the event
    time of the newest written edit. It is a gauge computed at scrape time
    (`Gauge.set_function(WallClockLag.seconds)`), so a consumer that stopped receiving
    reports a lag that keeps growing, instead of the last good value.
  - *the reader's distance*: the detector's watermark against the writer's,
    `max(wikipulse_consumer_last_event_timestamp_seconds) -
    max(wikipulse_detector_last_event_timestamp_seconds)`, in event time.
- **Queue length**: `wikipulse_detector_stream_lag_entries`, Redis' own count of entries
  not yet delivered to the detector's consumer group (`XINFO GROUPS`, field `lag`), and
  `wikipulse_detector_pending_entries`, delivered but not yet acknowledged.
- **Error rates**: messages by outcome (malformed), Wikimedia API answers by status
  (429 = slow down), LLM requests by outcome, extractions by outcome.

**Event time vs. the wall clock.** AGENTS.md says processing logic uses event time. The
lag metric is not processing logic: nothing in the pipeline reads it. Live, "now" is the
wall clock. A replay of a recording from last week is not "a week behind", though, so
`ReplayLag` measures against the replay's own clock: the first edit's event time plus the
elapsed wall time multiplied by `--speed`, exactly how `FileSource` paces messages.

### Dashboards as code

The four dashboards are JSON files in `observability/dashboards/`, generated by
`scripts/dashboards.py`: a panel is one call (`stat("Active spikes", "sum(...)", ...)`),
and the data source, layout and defaults are written once. Terraform puts each file into
a ConfigMap labeled `grafana_dashboard=1` with the annotation `grafana_folder: WikiPulse`;
Grafana's sidecar container watches for that label and loads them. Dashboards can't be
saved from the UI (`allowUiUpdates: false`): change the code, regenerate, apply.
`tests/test_observability.py` fails when the JSON and the generator disagree, when a
panel queries a metric that no service exports, or when a panel uses another data
source than the provisioned `prometheus`.

| Dashboard | Shows |
|-----------|-------|
| Pipeline health (Grafana's home) | edits/s, writer lag, detector lag, alerts firing; messages by outcome, lag over time, the detector's queue, Wikimedia answers by status, services up, reconnects, memory and CPU per service |
| Detection | spikes started (1 h), active spikes, the detector's event time, open events; spikes per 10 min by window, edits in and out of the detector, spikes grouped into events |
| LLM usage | requests, p95 latency, tokens, extraction failure rate; requests by model and outcome, p50/p95 by model, tokens per minute, extractions by outcome, questions by outcome |
| Model health | drift status per model and run, report period, score PSI against its noise floor, the most shifted features, drifted features, stored scores and news share, live scores |

### Alert design

Alerts wake people up, so each should mean "a user would notice", and be rare enough to
be taken seriously. **Alert fatigue** is what happens otherwise: a channel full of alerts
that resolve themselves teaches everyone to ignore it, including the one that matters.
So the rules alert on *symptoms* (no news coming in, news late, extraction broken), not
on *causes* (one reconnect, one 429, CPU at 90%), which the dashboards show instead; each
has a duration that rides out blips, a severity, and an annotation saying what to check.

| Alert | Expression (short) | Fires after | Severity |
|-------|--------------------|-------------|----------|
| `WikiPulseNoEvents` | `increase(kept[2m]) == 0` or `absent_over_time(kept[2m])` | 2 min without an edit (the window is the debounce) | critical |
| `WikiPulseConsumerLagHigh` | `max(wikipulse_consumer_lag_seconds) > 300` | `for: 2m` | warning |
| `WikiPulseDetectorLagHigh` | writer's event time - detector's > 300 s | `for: 5m` | warning |
| `WikiPulseServiceDown` | `up{system="wikipulse"} == 0` | `for: 5m` | warning |
| `WikiPulseExtractionFailuresHigh` | failed / all extractions over 30 min > 25 %, with at least 4 | `for: 10m` | warning |
| `WikiPulseDriftDetected` | newest drift report says drift, period ended within 8 days | at once | info |

`WikiPulseNoEvents` needs two halves because a dead writer takes its metrics with it:
`increase(...) == 0` catches a consumer that runs but writes nothing, and
`absent_over_time(...[2m])` catches one that is gone (its series stop with its last
scrape). A plain `rate(...) == 0` would never fire when the consumer crashes: no series,
no result, no alert. That is the most common mistake with "no data" alerts.

`observability/alerts/wikipulse.rules.test.yaml` is a unit test for the rules: made-up
series ("a counter that grows for 5 minutes, then goes stale") and the alerts expected at
given times. `promtool test rules` runs it (in `tests/test_observability.py` when promtool
or the Prometheus image is available; CI pulls the image, and there a missing promtool
fails the test instead of skipping it). Every rule has at least one test
(`test_every_rule_has_a_promtool_test`), and the series other processes export are fed as
`NaN`, as they really are (see "Event-time gauges start as NaN" below).

## Design decisions and deviations

- **No live stream: the session ran on a replay** (the task's constraint: `.env` still has
  the placeholder contact, so nothing may connect to Wikimedia). The cluster ran with
  `offline=true` (consumer, enricher and graph runner at 0 replicas) and the new overlay
  `values-replay.yaml` (`-var replay=true`): the detector reads `wiki:replay:edits` as run
  `replay`, the grouper groups that run, the API shows it. The replay ran **on the laptop**
  (`METRICS_PORT=9464 uv run python -m wikipulse.stream.replay ...`), and Prometheus
  scraped it as the static target `host.docker.internal:9464` (`host_metrics_targets`). The
  replay uses the consumer's own class (`Consumer.process`), so it exports exactly the
  consumer's metrics; no separate replay metrics were needed. It writes its own stream,
  not `wiki:edits`, so the live run's detector state and the data lake (the archiver reads
  `wiki:edits`) stay untouched. "Stopping the consumer" was done by stopping the replay.
  No alert duration had to be shortened.
- **The recording had to be sorted.** `history-20261106T2000Z.jsonl` was recorded from
  Wikimedia's history in the order the server sent it, partition by partition: its first
  thousands of lines are sparse events spread over the whole hour. Paced by event time,
  the replay trickled one message every few seconds. A copy sorted by event time and cut
  to 20:00 to 21:00 (`data/recordings/session-09a-20261106T2000Z-sorted.jsonl`, not
  committed) replays at the stream's real density: about 1.5 kept edits per second at
  speed 2.
- **Terraform installs the monitoring stack**, not `scripts/cluster.py`. Terraform can
  read files anywhere (the dashboards and rules live in `observability/`, outside the
  chart, where Helm can't read them) and already owns the cluster's other pieces.
  `cluster.py deploy` still works: the pods then serve `/metrics` but nothing scrapes them.
- **PodMonitor and rules through the monitoring chart's values**, not as templates of the
  WikiPulse chart. A PodMonitor template would need the operator's resource types to
  exist before the WikiPulse chart is installed (or `helm template` in CI would need them).
  `prometheus.additionalPodMonitors` and `additionalPrometheusRulesMap` let the
  kube-prometheus-stack release create them, from Terraform (`monitoring.tf`), with the
  namespace taken from a variable.
- **Model health is read from Postgres by the API** (`ModelHealthCollector`). The drift
  check is a weekly batch job; it is not running when Prometheus scrapes. The usual
  alternative, a Pushgateway the job pushes to, is one more service to run and keeps a
  dead job's last values forever. The reports are already in Postgres, so a collector in a
  service that runs anyway reads them at scrape time (two small queries). The live scorer
  (`wikipulse.ml.serve`) has its own histogram of scores too, but it isn't deployed (it
  needs Wikipedia page revisions).
- **Extra alerts**: `WikiPulseDetectorLagHigh` (the spec's "consumer lag" read as the
  stream's *reading* side, the Redis consumer group, as Kafka users mean it) and
  `WikiPulseServiceDown`.
- **Dashboards are generated** from `scripts/dashboards.py` instead of written as JSON by
  hand (2,500 lines of JSON for 39 panels). Tools like grafanalib or Jsonnet do the same;
  plain Python needed no new dependency.
- **Lean stack**: Alertmanager, node-exporter, kube-state-metrics, the default rules and
  the Kubernetes dashboards are off (`observability/kube-prometheus-stack.yaml`); alerts
  still fire in Prometheus, they just aren't sent anywhere. Admission webhooks are off
  too (they need an extra certificate Job). Memory limits: Prometheus 768 Mi, Grafana
  384 Mi, its sidecar 128 Mi, the operator 192 Mi.
- **Grafana's admin password** is generated by the Grafana chart and kept in the Secret
  `kube-prometheus-stack-grafana`; nothing is written into the values.
- **Labeled counters start at 0** (`start_at_zero()`): added during the session, when the
  first spike of the replay run didn't show on the "spikes started" panel. The image was
  rebuilt and rolled out mid-session with `terraform apply`.
- **A `system="wikipulse"` label** on every WikiPulse target (PodMonitor relabeling, and
  the host targets' static labels), added during the session: "Services", "Memory" and
  "CPU per service" listed Prometheus and Grafana too.
- **Event-time gauges start as NaN** (end of `catalog.py`), added after the session; see
  Results.
- **`docker/airflow/requirements-ml.txt`** gained `prometheus-client` (the test that
  checks it against `uv.lock` asked for it).
- **Tools**: nothing installed. Helm 4.3.1, Terraform 1.17.0 and kind were there; promtool
  runs from the Prometheus image (`quay.io/prometheus/prometheus:v3.12.0`).

- **Review fixes** (after `docs/reviews/09a-metrics-dashboards.md`):
  - `WikiPulseServiceDown` selects `up{system="wikipulse"}` instead of
    `up{namespace="wikipulse"}`. The namespace is a Terraform variable, and host targets
    (the replay) have no namespace at all; the `system` label is on every WikiPulse
    target. Rendering the namespace from Terraform would have worked for the pods only.
  - New promtool tests: `WikiPulseDetectorLagHigh` (a stuck detector fires after 5 min of
    `for`; a detector that hasn't read anything yet, NaN everywhere, never fires, while
    the same test with `0` instead of `NaN` fails, which is the bug the NaN fix removed),
    `WikiPulseConsumerLagHigh` with only NaN series, and `WikiPulseServiceDown` (a pod in
    another namespace and a host target fire; Prometheus' own target doesn't).
  - CI pulls `quay.io/prometheus/prometheus:v3.12.0` before the unit tests, so the
    promtool test runs there (`test_ci_pulls_the_same_prometheus_image` keeps the two
    versions equal). The Helm lint in CI also lints dev + offline + `values-replay.yaml`.
  - `ModelHealthCollector`: a malformed drift report or score row (a feature without a
    name, a PSI that isn't a number) is logged and skipped; the other rows and
    `wikipulse_model_health_up 1` are still exported. Each row's values are computed
    before any is added (`_drift_values`), so a bad row adds nothing half-way. Before,
    one bad row failed the API's whole `/metrics` scrape.
  - The collector's Postgres connect timeout is the setting
    `METRICS_DB_CONNECT_TIMEOUT_S` (default 5 s, whole seconds as libpq wants).
  - Not changed (not asked): the extraction alert counting retries (review problem 6),
    the counters without unit tests (7), `exported_names()` using private attributes (9).

## Results

Session on 2026-11-10, 20:51 to 21:26 UTC, Windows 11, Docker Desktop (7.4 GB), the
Compose services running, Terraform 1.17.0, kube-prometheus-stack 93.1.2 (Prometheus
v3.16, Grafana with its sidecar). No pod and no process contacted Wikimedia: the cluster
was `offline=true` (consumer, enricher, graph: 0 replicas), and the data came from the
replay on the laptop.

**Install.** `scripts/infra.py cluster apply -var offline=true -var replay=true -var
'host_metrics_targets={replay="host.docker.internal:9464"}'`: plan "11 to add", apply in
2 min 53 s (kind 52 s, `kind load` 1 min 31 s, kube-prometheus-stack 1 min 53 s in
parallel, the WikiPulse release 25 s). Pods: Grafana (3 containers), the operator,
Prometheus (2), and api, archiver, detector, grouper. The kind node used 1.9 GB right after
the install and 2.6 GB at the end, of Docker's 7.4 GB; nothing in Compose had to stop.

**Targets** (Prometheus, Status > Targets): `monitoring/wikipulse` found the four pods on
`:9400/metrics` (detector, grouper, api, archiver: up), plus `wikipulse-host-replay` on
`host.docker.internal:9464`, down until the replay started, then up.

**Live data.** The replay of 20:00 to 21:00 on 2026-11-06 at speed 2 ran from 21:02 to
21:22 (4,800 kept edits, 93,146 discarded). From Prometheus, every two minutes:

| | 21:04 | 21:08 | 21:12 | 21:16 | 21:20 | 21:22 |
|---|---|---|---|---|---|---|
| edits written / s | 3.22 | 3.78 | 4.95 | 3.77 | 4.31 | 3.65 |
| edits the detector processed / s | 3.36 | 3.69 | 4.38 | 2.20 | 4.25 | 4.37 |
| writer lag (s, replay clock) | 1.57 | 0.43 | 0.00 | 0.84 | 0.58 | 0.16 |

(The dip at 21:16 is the detector's rollout, see below: it caught up from its snapshot and
the pending list within a minute.) The detector found its first spike of the run at
event time 20:18 (`Syed Soharwardy`), the grouper opened an event for it. Four questions
through `POST /ask` (qwen3:8b on the host's Ollama) made the LLM metrics.

**Panels** (a throwaway script ran every query of every panel through Grafana's own
data source proxy, at 21:22 while the replay ran): all four dashboards were in the folder
WikiPulse, Pipeline health was Grafana's home page, and **41 of 46 panel queries returned
data**. Some values: edits written 3.64/s; messages by outcome discarded 112.8/s, kept
3.6/s; writer lag 0.3 s; the detector's queue 0; services up: api, archiver, detector,
grouper, replay; memory api 475 MB, grouper 404 MB, archiver 90 MB, detector 48 MB;
active spikes 1, open events 1; LLM requests (1 h) 6.3, p50 2.2 s, p95 11 s, 7,537 tokens
in the hour, questions ok 2 (p95 29 s); drift status 02b-baseline ok and 07a-default
drift, score PSI 0.088 (floor 0.230) and 0.162 (floor 0.351), top feature
`edits_before_start` 2.93, 202 + 89 + 1 stored scores. The five empty ones are expected
offline: Wikimedia API answers (no enricher or graph runner), the two extraction queries
(no graph runner), and the live scorer's two (not deployed).

**Stopping the writer fires the "no events" alert.** The replay was stopped at 21:22:42.
Polling Prometheus' `/api/v1/alerts` every 10 s:

```
21:22:48 up(replay)=0 | WikiPulseDriftDetected=firing (since 21:21:16)
...      (no change for 1 min 45 s)
21:24:55 up(replay)=0 | WikiPulseDriftDetected=firing (since 21:21:16); WikiPulseNoEvents=firing (since 21:24:35)
```

`WikiPulseNoEvents` fired 1 min 53 s after the stop, with the rule's real 2-minute window
(no shortened durations). Before the replay started it was firing too (since 20:54:05,
nothing had been written yet) and it resolved within a minute of the first edits.
`WikiPulseDriftDetected` fired from the start for 07a-default (its newest report says
drift, period ending 2026-11-10); its "since" moved to 21:21:16 because the monitoring
upgrade reloaded the rules. `WikiPulseDetectorLagHigh` was *pending* for a few minutes at
the start (the detector's watermark gauge read 0 before its first edit, see below) and,
thanks to its `for: 5m`, never fired.

**Infrastructure as code during the session.** Two changes were applied while the replay
ran, and Terraform planned exactly them: the new image (`terraform_data.image` replaced,
`helm_release.wikipulse` updated: 1 add, 1 change, 1 destroy; the pods rolled), and the
`system` label with three panel queries (`helm_release.monitoring` and one dashboard
ConfigMap updated in place: 0 add, 2 change). Grafana's pod was replaced by the upgrade
and loaded the dashboards again by itself.

**After the session**, two small fixes (unit-tested; not run in a cluster): the
event-time gauges start as NaN, not 0 (every process imports the catalog, so `max()` of
the lag read 0 while no writer ran, and the detector's 0 made `WikiPulseDetectorLagHigh`
pending at its start), and the "last event" gauges keep the newest event time rather
than the last one written (the detector's distance showed -3 s when edits came slightly
out of order).

**Cluster afterwards**: `terraform destroy`, 11 resources in 38 s; `kind get clusters`
lists none.

**Tests**: `uv run pytest` 1171 passed, 1 skipped (Airflow is only in its image) after
the review fixes; new: `tests/metrics/` (18, one of them `integration`, which passes with
`-m integration`), `tests/test_observability.py` (12, including `promtool check rules` and `promtool test
rules`, run through the local Prometheus image), 4 more in `tests/test_chart.py`.
`ruff check`, `ruff format --check`, `terraform fmt -check` and `terraform validate` pass.

## Questions to test your understanding

1. The consumer pod is OOM-killed and doesn't come back. Why would an alert written as
   `sum(rate(wikipulse_consumer_messages_total{outcome="kept"}[2m])) == 0` stay silent,
   and what does `WikiPulseNoEvents` do instead?
2. Why is LLM latency a histogram and not a gauge holding the last request's duration,
   and why not an average?
3. A replay of last week's recording runs at speed 2. Why doesn't
   `WikiPulseConsumerLagHigh` fire, although every edit it writes is a week old?

<details>
<summary>Answers</summary>

1. When the pod is gone, Prometheus' scrapes fail and the counter's series ends (a stale
   marker). `rate()` over a series that doesn't exist returns nothing, `sum` of nothing is
   nothing, and "nothing == 0" is not true: no result, no alert. `WikiPulseNoEvents`
   adds `absent_over_time(...[2m])`, which returns 1 exactly when there was no sample in
   the last 2 minutes, so a vanished writer fires too.
2. A gauge holds one value per scrape: requests between two scrapes would be lost, and
   one slow request among a hundred fast ones would show only if it happened to be the
   last. A histogram counts every request into buckets, so rates of buckets can be added
   across pods and turned into percentiles over any window. An average hides the tail:
   ninety 2-second answers and ten 100-second answers average 11.8 s, while the p95
   (about 100 s) says what the slowest users wait.
3. The replay's lag is measured by `ReplayLag` against the replay's own clock: the first
   edit's event time plus the elapsed wall time multiplied by the speed. As long as the
   replay writes as fast as its pacing asks, the newest written edit is close to that
   clock, so the lag stays near zero. It grows only if the replay falls behind its own
   pace (or stops), which is the condition the alert is about.

</details>
