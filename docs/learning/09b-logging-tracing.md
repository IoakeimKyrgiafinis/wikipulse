# 09b: Centralized logging and LLM tracing

## What was built

Every WikiPulse process now writes one JSON object per log line, with the same field
names everywhere; a collector in the cluster ships the pods' lines to Loki, and Grafana
searches them. A **correlation id** travels with each story from the edit that started it
to its briefing, so one search shows the whole path. Every LLM call and agent step can be
traced to Langfuse, and nothing happens when no keys are set.

| Piece | File | What it does |
|-------|------|--------------|
| Structured logs | `wikipulse/logging_setup.py` | `setup_logging()` with `LOG_FORMAT=text|json`; `JsonFormatter`, `TextFormatter`; `log_context(...)` binds fields for a block; `service_name()` |
| Settings | `wikipulse/config.py`, `.env.example` | `LOG_FORMAT`, `LOG_SERVICE`, `LOG_EDITS`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` |
| Correlation id | `stream/consumer.py`, `detect/detector.py`, `events/grouper.py`, `events/run.py`, `graph/run.py`, `agent/run.py`, `agent/store.py`, `db/migrations/0009_add_correlation_ids.sql` | Carried edit -> spike -> event -> graph -> briefing; stored in `spikes`, `events` and on the Neo4j `Event` node |
| Tracing | `wikipulse/llm/tracing.py`, `wikipulse/llm/langfuse_tracing.py` | `trace(name)`, `get_tracer()`; `NoopTracer` without keys, `LangfuseTracer` with them; `LLMClient._ask()` records each request as a generation |
| Agent steps | `agent/loop.py`, `agent/tools.py` | One `agent run` trace per briefing; every tool call is a `tool` observation and a log line |
| Chart | `charts/wikipulse/` | `LOG_FORMAT=json`, `LOG_EDITS=true`, `LANGFUSE_HOST` in the ConfigMap; `LOG_SERVICE=<name>` per Deployment; `sessionPods` and the policy `session-egress` (values-replay only); Langfuse keys in the Secret |
| Logs stack | `infra/cluster/logging.tf`, `observability/loki.yaml`, `observability/alloy.yaml`, `observability/alloy/config.alloy.tftpl` | Loki (single binary) and Alloy (one Deployment) from grafana/helm-charts; Grafana's Loki data source in `monitoring.tf` |
| Dashboard | `scripts/dashboards.py` -> `observability/dashboards/event-trail.json` | "WikiPulse: Event trail": a correlation id box, lines per service, the trail |
| Session tools | `scripts/cluster.py run`, `scripts/fake_wikimedia.py` | One-off pods with the deployed image (replay, graph run, agent run); an offline stand-in for the Wikimedia APIs |
| Tests | `tests/test_logging_setup.py`, `tests/llm/test_tracing.py`, `tests/conftest.py`, and additions in `tests/stream`, `detect`, `events`, `graph`, `agent`, `test_chart.py`, `test_cluster.py`, `test_infra.py`, `test_observability.py` | |

How the pieces fit:

```
 laptop                                   kind cluster
 ──────                                   ───────────────────────────────────────────────────────
 recording ──kubectl run -i (stdin)──►   namespace wikipulse                    namespace monitoring
                                          replay pod  "Edit written" cid=E ─┐
 Redis (Compose) ◄── wiki:replay:edits ── │                                 │   Alloy: discovers pods,
                 ──► detector "Spike started" cid=E ────────────────────────┤   tails their logs via the
 Postgres ◄─ spikes.correlation_id=E      grouper "Event replay:1 opened" ──┤   API, JSON -> labels and
          ◄─ events.correlation_id=E      graph pod "graph extraction" ─────┤   structured metadata
 fake_wikimedia :8089 ◄── graph pod        + LLM calls (trace_id=T) ────────┤        │ push
 Ollama :11434 ◄───────── graph, agent    agent pod "Briefing event" ───────┘        ▼
 Neo4j ◄─ Event.correlation_id=E           + tool steps + LLM calls              Loki ◄── Grafana
                                                                                       "Event trail"
                                    (with keys: the same LLM calls and steps ──► Langfuse, session E)
```

## Key concepts, with this project's code

### Structured logging

A log line written for people (`Spike started (short window): Foo (6 edits ...)`) is easy
to read and hard to search: to find every line about one story you need a regular
expression per message format, and each service writes different ones. A **structured**
line is data: `{"time": ..., "level": "INFO", "service": "detector", "logger": ...,
"message": "Spike started ...", "correlation_id": "4f1c...", "title": "Foo"}`. Searching
is then "field = value", whatever service wrote the line.

`wikipulse/logging_setup.py` does this with the standard `logging` module, no new
dependency:

- `JsonFormatter` turns a `LogRecord` into one JSON object. Every attribute on the record
  that isn't one of the standard ones (`record_fields()`) becomes a field, so the code
  adds fields the normal way, `logger.info("...", extra={"correlation_id": cid})`.
- `log_context(correlation_id=..., event_key=...)` binds fields for a whole block. It
  keeps them in a `ContextVar`; `ContextFilter` copies them onto every record. A context
  variable follows the code into `await`s and into `asyncio.to_thread()` (which copies
  the context), so the graph runner binds the id once in `GraphRunner.process()` and the
  LLM client's line, logged in a worker thread four calls deeper, still carries it
  (`test_the_context_follows_into_worker_threads`).
- **Consistent names** are the point: `correlation_id`, `spike_id`, `event_key`, `run_id`,
  `trace_id` mean the same in every service (`FIELDS`). `service` comes from
  `LOG_SERVICE`, which the chart sets to each Deployment's name; without it,
  `service_name()` uses the module that was started (`detect.run`).
- On the laptop `LOG_FORMAT=text` keeps the readable line and appends the fields in
  brackets: `... Event replay:1 opened: Foo [correlation_id=4f1c... event_key=replay:1]`.

### The correlation id

A **correlation id** is one value that every step of one piece of work logs, so the steps
can be joined afterwards. Web services usually create one per request at the edge and
pass it on in a header. A WikiPulse story has no request: it begins with an edit, so the
id is **the `event_id` of the edit that made the spike's start rule hold** (Wikimedia's
`meta.id`, a UUID):

1. `Consumer._write()` (the live consumer and the replay) logs `Edit written` for every
   written edit with `correlation_id = event.event_id` (`log_written()`, with
   `LOG_EDITS=true`, which the cluster sets).
2. `SpikeDetector._maybe_start()` gets the edit's id and stores it on the `Spike`
   (`Spike.correlation_id`, kept in the snapshot), logs `Spike started` with it, and
   writes it to `spikes.correlation_id`.
3. The grouper gives an event the id of the spike that **opened** it, once
   (`Event.refresh()` sets it only while it is still None), and nothing changes it after
   that: not a later join, not a merge, not a restart (`GroupingRunner._restore()` reads
   the stored `events.correlation_id` via `active_correlation_ids()` instead of
   recomputing it). A merge keeps the survivor's id and logs the merged event's id as
   `merged_correlation_id` (see "Fixes after the review"). `GroupingRunner.log_assignment()` logs `Event replay:1 opened`
   or `Spike 5 joined event replay:1` (that line also has `spike_correlation_id`, which
   links a later spike's own trail to the story). Stored in `events.correlation_id`.
4. The graph runner reads it with the event (`db/graph.py`, `EventInput`), binds it in
   `GraphRunner.process()`, and writes it onto the Neo4j `Event` node.
5. The agent's trigger reads it from the node (`BriefingStore.candidates()`, or
   `correlation_id(key)` for `--event`) and binds it in `Trigger.brief()`.

Because the id comes from the data, a replay of the same recording produces the same ids
as the live run did (AGENTS.md's event-time rule, applied to ids). One test had to change
for this: `test_same_input_gives_the_same_spikes` built its edits twice with a helper that
numbers event ids, so the "same input" had different ids; it now builds them once.

### Log aggregation with Loki

The ELK stack (Elasticsearch, Logstash, Kibana) indexes the **full text** of every line:
powerful, and expensive in memory and disk. **Loki** indexes only a few **labels** per
stream (here `service`, `namespace`, `pod`, `container`, `level`, `system`) and stores the
lines compressed in chunks; a query first picks streams by label, then scans their lines
(`|= "text"`, `| json | field="x"`). Same ideas (collect, store, query), a fraction of the
cost, and the query language looks like Prometheus' (LogQL).

The rule that makes Loki work: **labels must have few values.** Each label combination
is a stream with its own index entries; a label per correlation id would make one stream
per story, millions over time, the same mistake as a page title as a Prometheus label
(09a). So the ids are **structured metadata**: stored next to each line, filterable as
`| correlation_id="..."` without parsing, but not indexed
(`test_ids_are_structured_metadata_not_labels`).

The collector is **Grafana Alloy** (Promtail's successor; Promtail is deprecated). Its
pipeline, `observability/alloy/config.alloy.tftpl`:
`discovery.kubernetes` (the pods of the namespace) -> `discovery.relabel` (the labels;
`service` from `app.kubernetes.io/name`, as the metrics have it) ->
`loki.source.kubernetes` (tails each container's log **through the Kubernetes API**, like
`kubectl logs -f`) -> `loki.process` (`stage.json` reads `level` and the ids,
`stage.labels`, `stage.structured_metadata`) -> `loki.write`.

Reading through the API instead of the node's `/var/log/pods` means no hostPath volume
and no privileged DaemonSet: Alloy runs under the monitoring namespace's `baseline` pod
security as one small Deployment. On a big cluster the per-node DaemonSet is the norm (the
API server would become the bottleneck); on one kind node, the API is simpler and safer.

Loki runs as a **single binary** (`deploymentMode: SingleBinary`, one pod, filesystem
storage on a 2 GiB volume, 3-day retention like Prometheus). The chart's defaults install
the scalable mode with object storage and memcached caches of several GB; all off
(`observability/loki.yaml`, `test_loki_is_one_lean_process`).

Grafana got a second data source, `Loki` (uid `loki`), from `monitoring.tf`, and the
dashboard **WikiPulse: Event trail** (`event_trail()` in `scripts/dashboards.py`): a text
box for the correlation id, a bar per service with its line count, and the lines oldest
first. The query is

```
{system="wikipulse"}
  | (correlation_id!="" and correlation_id="$correlation_id")
    or (merged_correlation_id!="" and merged_correlation_id="$correlation_id")
  | json
```

The second line finds a merge line by the merged event's id too; the `!=""` guards make an
empty box show nothing (without them, `correlation_id=""` matches every line that has no
id). `merged_correlation_id` is structured metadata too (Alloy's `stage.json` and
`stage.structured_metadata`).

### Tracing LLM calls and agent steps

Logs say *that* something happened; a **trace** shows one unit of work as a tree of timed
steps with their inputs and outputs: the agent run, each model turn with its prompt,
answer and tokens, each tool call with its arguments and result. For an LLM pipeline that
is where most bugs are found: "why did the briefing say X" is answered by the prompt and
the tool result that preceded it.

`wikipulse/llm/tracing.py` is the only interface the rest of the code sees:

- `trace(name, as_type=...)` opens an observation around a block: the first one without
  a trace around it starts a trace, the ones inside become children. The graph runner
  wraps each extraction (`graph extraction`, a `chain`), the agent each run
  (`AnalystAgent.run`, `agent run`) and each tool call (`ToolBox.call`, `tool get_event`).
- `LLMClient._ask()` wraps every request in a `generation`: model, the system prompt and
  messages, the parameters (temperature, max_tokens, think, tools), the answer or tool
  calls, and the token usage. Errors are recorded (level ERROR) and raised again. So all
  callers are traced in one place, as with the metrics of 09a.
- A new trace's **session** is the bound correlation id, so in Langfuse the extraction
  and every briefing run of one story are one session; the other way round, every log line
  written inside a trace carries `trace_id`, so a line in Loki leads to its trace.

`wikipulse/llm/langfuse_tracing.py` is the only module importing the Langfuse SDK (v5,
built on OpenTelemetry: an observation is an OTel span, and a background thread sends
finished spans in batches). `build_tracer()` imports it only when **both**
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set and not blank; otherwise it
returns `NoopTracer`, whose `observe()` yields one shared do-nothing object.

**Tested both ways** (`tests/llm/test_tracing.py`):

- *Without keys*: every combination of missing or blank keys gives the no-op; with
  `sys.modules["langfuse"] = None` (any import of the SDK would fail) a chat inside a
  trace still works, nothing is bound, nothing is sent.
- *With keys*: the real SDK, with an in-memory OpenTelemetry exporter in place of the
  network (`LangfuseTracer.connect(..., span_exporter=InMemorySpanExporter())`, the host an
  unreachable `127.0.0.1:9`). The tests read the spans the SDK would send: the generation
  with model, input, output and usage; `session.id` = the correlation id on the root and
  its children; the parent links of run -> tool -> chat; ERROR on a failed call; the
  trace id in the log context; the context following `asyncio.to_thread`.
- `tests/conftest.py` installs the no-op for every test, so a developer's `.env` with real
  keys can never make the test suite send traces.

## Design decisions and deviations

- **No Langfuse account, so no traces were sent anywhere.** "Works with keys" is shown
  with the real SDK and an in-memory exporter (above), not against Langfuse cloud or a
  self-hosted Langfuse: the self-hosted server needs Postgres, ClickHouse, Redis and S3
  (v3), too heavy next to the kind cluster in 7.4 GB. The session ran without keys, which
  is the "silent no-op" case in production code.
- **The correlation id is the starting edit's `event_id`**, not a generated UUID: it
  exists before the spike does, the consumer can log it without knowing the future, and a
  replay reproduces it. Its cost: the consumer logs one line per written edit
  (`LOG_EDITS`, about two lines a second live), which is why it is a setting, off on the
  laptop and on in the cluster.
- **An event's id is its opening spike's, set once.** A story with several pages has
  several edit ids; the event keeps one, and the grouper's join line connects the others.
  (The first version used the *earliest* spike's id and recomputed it on every change;
  the review showed why that splits a trail, see below.)
- **Alloy, reading through the Kubernetes API, as a Deployment**, rather than Promtail
  (deprecated) or a DaemonSet reading host files (needs hostPath, which `baseline` pod
  security forbids). Both charts go in the monitoring namespace, from Terraform
  (`logging.tf`, `-var logging=false` to leave them out), like the monitoring stack.
- **Ids as structured metadata, not labels** (see above). `level` is a label: 5 values.
- **The replay, the graph run and the agent run ran as one-off pods in the cluster**
  (`scripts/cluster.py run`, new): their logs must reach Loki through the same collector
  as everything else. In 09a the replay ran on the laptop, where no collector sees it.
  `kubectl run -i` feeds the recording on the pod's stdin (`--file /dev/stdin`); the pod
  gets the chart's settings, security and `LOG_SERVICE`. A network policy for these pods
  (`session-egress`, only with `values-replay.yaml`) allows Redis, Postgres, Neo4j,
  Ollama and the fake Wikimedia port, and **never 443**: the schema refuses it, so a
  session pod can't reach Wikimedia even by mistake. The replay had to accept a pipe
  (`replay.py` checked `is_file()`); a small fix.
- **A fake Wikimedia for the graph run** (`scripts/fake_wikimedia.py`, on the laptop, port
  8089): the graph runner fetches the revisions of each burst and searches Wikidata, and
  nothing may contact Wikimedia. The graph pod got `WIKI_API_URL` and `WIKIDATA_API_URL`
  pointing at it; it answers every request with an empty result. Extraction then sees the
  page summaries already in Postgres (enriched in an earlier stage), not the text added
  during the burst, and no entity gets a Wikidata link. Its answers are cached in
  `api_cache` under the fake URLs, so they never mix with real ones.
- **LLM call lines are INFO now** (`LLM call ollama:qwen3:8b: 1234 tokens in, ...`, with
  the numbers as fields), so the trail shows them; they were DEBUG.
- **Langfuse keys in the cluster**: Terraform variables `langfuse_public_key` and
  `langfuse_secret_key` (sensitive, no default, set by `scripts/infra.py` from your
  settings, "" when unset) go into the Secret; `LANGFUSE_HOST` is in the ConfigMap. Empty
  keys mean no tracing. `cluster.py secret` does the same.
- **The Event trail dashboard** is generated like the others; the data source test now
  accepts the Loki uid for Loki panels, and checks that `monitoring.tf` provisions it.
- **Dependency**: `langfuse` (5.2, with its OpenTelemetry exporter); added to
  `docker/airflow/requirements-ml.txt` too, as its lock test asks.
- **Dependency fix (review)**: `opentelemetry-api` is declared in `pyproject.toml`
  (`langfuse_tracing.py` imports it directly) and `opentelemetry-sdk` in the dev group
  (the tests' in-memory exporter); both were only there through `langfuse` before.
- **Tools**: nothing installed. The grafana Helm repository was added for the chart
  versions (loki 7.6.0, alloy 1.15.0).

## Fixes after the review

`docs/reviews/09b-logging-tracing.md` found two medium problems and some low ones. Fixed:

- **Session pods could get 443 (problem 1).** NetworkPolicies **add up**: a pod gets the
  union of what every policy selecting it allows. A session pod started with
  `--name graph` carried `app.kubernetes.io/name: graph`, so `graph-egress` selected it
  too and opened 443, whatever `session-egress` said. Now every service policy (and
  `migrate-egress`) also has `matchExpressions: wikipulse.io/session DoesNotExist`, so no
  service policy can select a session pod, whatever its name; and `cluster.py run`
  refuses the chart's pod names (`CHART_POD_NAMES`, `check_session_name()`), which also
  keeps a session's logs and metrics apart from the service's. Tests:
  `test_no_service_policy_selects_a_session_pod_whatever_its_name` (evaluates the
  rendered selectors against a session pod's labels, for every service name) and
  `test_a_session_pod_may_not_take_a_chart_pods_name`. The running cluster gets the new
  policies at the next `cluster.py deploy`; it was not rebuilt.
- **The correlation id could change (problems 2 and 3).** It was recomputed as the
  earliest spike's on every join. A spike that *started* earlier but waited for
  enrichment joins later and would take the id over after `Event ... opened` was logged:
  the trail split in two. Now the id is set once (above). For merges, the survivor keeps
  its id and the merge line (logged after the survivor is updated) carries the merged
  event's id as `merged_correlation_id`; the trail dashboard matches that alias, so a
  search for either id finds the line that joins them. Tests: an earlier spike enriched
  later, a merge of an event that started earlier, a restored event, an event opened by a
  pre-09b spike without an id (unit, `tests/events/test_grouper.py`), and a restart after a
  late earlier spike (integration, `tests/events/test_events_db.py`).
- **An empty trail box showed thousands of lines (problem 4)**: the `!=""` guards above.
- **opentelemetry undeclared (problem 6)**: declared (above).
- **The tracer could be built twice (problem 8)**: `get_tracer()` checks again under a
  `threading.Lock` (double-checked locking); `test_threads_racing_for_the_tracer_build_it_once`
  starts eight threads at a barrier against a slow builder.

Not changed (not asked): problem 5 (graph results from fake inputs stay under run id
`replay`), 7 (LLM calls logged at INFO, failures twice), 9 (a replay of a recording also
processed live shares its ids). The new LogQL was checked by the dashboard tests only;
the cluster was down, so it was not run against Loki.

## Results

Session on 2026-11-10 23:54 to 2026-11-11 00:22 UTC, Windows 11, Docker
Desktop (7.4 GB), the Compose services running, Terraform 1.17.0. No pod and no process
contacted Wikimedia: the cluster was `offline=true` (consumer, enricher and graph
Deployments at 0 replicas), the session pods' policy has no 443, and the graph run's four
Wikimedia requests went to `scripts/fake_wikimedia.py` (its log lists them: one revisions
query, one pageprops query, two Wikidata searches). No Langfuse keys were set, so nothing
was traced anywhere: the production no-op path.

**Install.** `scripts/infra.py cluster apply -var offline=true -var replay=true`: 14
resources (Loki 1 min 15 s, Alloy 48 s, in parallel with the rest). The kind node used
2.4 GB of Docker's 7.4 GB with everything running (09a: 2.6 GB without logs; the
difference is noise). A second apply after the replay fix (a new image) planned exactly
"1 to add, 1 to change, 1 to destroy" (the image and the release).

**The path, from a replay of 2026-11-06 20:00 to 20:40** (4,625 wanted edits, filtered
from the 09a session file, at speed 6, as the pod `replay` fed through `kubectl run -i`):

1. edit -> the replay pod logged `Edit written` for every edit (4,625 lines in Loki);
2. spike -> the detector started `Syed Soharwardy` at event time 20:18 (short window, 5
   edits, 4 editors, score 6.4), correlation id `32a1b7db-a8b0-4203-8d9f-2250d4936c49`,
   also in `spikes.correlation_id`;
3. event -> the grouper opened `replay:1` with the same id (`events.correlation_id`);
4. graph -> the pod `graph-session` extracted it with qwen3:4b (3 entities, 1 linked, 1
   relation, 18.5 s); the Neo4j `Event` node has the id;
5. briefing -> the pod `agent-session` (qwen3:8b) wrote a briefing with 2 sources in 24 s
   (2 model turns, 2 tool calls, 5,563 tokens).

**The criterion: one correlation id across services, in Grafana.** Through Grafana's
data source proxy (`/api/datasources/proxy/uid/loki/...`, the query the Event trail
dashboard runs), `{system="wikipulse"} | correlation_id="32a1b7db-a8b0-4203-8d9f-2250d4936c49"`
returned 13 lines, oldest first:

```
replay         Edit written: Syed Soharwardy (revision 1384138940)
detector       Spike started (short window): Syed Soharwardy (5 edits, 4 editors, expected 0.00, score 6.4)
grouper        Event replay:1 opened: Syed Soharwardy
graph-session  No revisions of 'Syed Soharwardy' during its burst
graph-session  LLM call ollama:qwen3:4b: 733 tokens in, 241 out, 14.42 s (load 10.61 s), stop stop
graph-session  Event replay:1: 3 entities (1 linked), 1 relations in 18.5 s
agent-session  Briefing event replay:1
agent-session  Tool get_event: ok in 0.2 s
agent-session  LLM call ollama:qwen3:8b: 1920 tokens in, 25 out, 11.16 s (load 9.31 s), stop stop
agent-session  Tool read_page: ok in 2.1 s
agent-session  LLM call ollama:qwen3:8b: 2112 tokens in, 96 out, 2.36 s (load 0.02 s), stop stop
agent-session  LLM call ollama:qwen3:8b: 1020 tokens in, 390 out, 7.69 s (load 0.01 s), stop stop
agent-session  replay:1: briefing with 2 sources; stop answered, 2 model turns, 2 tool calls, ...
```

and the dashboard's "Lines per service" query gave replay 1, detector 1, grouper 1,
graph-session 3, agent-session 7. The data source `Loki` (uid `loki`) and the dashboard
**WikiPulse: Event trail** were provisioned (Grafana API). Loki's labels were `service`,
`namespace`, `pod`, `container`, `level` (values INFO, WARNING), `system`; the ids stayed
out of the index as structured metadata. In the hour, Loki held 4,628 replay lines, 151
api, 54 grouper, 9 detector, 6 archiver, 9 graph-session, 10 agent-session.

**The debugging story (optional part).** The briefing reads "Pakistani-Canadian Islamic
scholar Syed Soharwardy founded the Islamic Supreme Council of Canada in 1998 [1][2]. He
is also the sheikh of the Suhrawardi Sufi order ..." It is true, and it is the wrong
briefing: it says what the man is, not what happened on 2026-11-06 that made four people
edit his page within ten minutes. The trail shows why without opening any code: the
fourth line, `No revisions of 'Syed Soharwardy' during its burst`, says the extraction got
no text added during the burst, so its only input was the page's summary; the extraction's
title is a biography ("Founding of Islamic Supreme Council of Canada"), and the agent's two
tool calls (`get_event`, `read_page`) read that title and the same summary. Here the cause
is the session's fake Wikimedia; live, the same line would point at a revision query that
returned nothing (a deleted revision, a wrong time window). With Langfuse keys, the
generation of the extraction (via the line's `trace_id`) would show the exact prompt
without the "changes" section. (The extraction model also misspelled the name in the
title, "Syed Soharward"; the LLM call line's 241 output tokens are where to look next.)

**Tracing.** Not exercised against a server (no keys, by the task's constraint); covered
by `tests/llm/test_tracing.py` with the real SDK and an in-memory exporter.

**Cluster afterwards**: `terraform destroy`, 14 resources; `kind get clusters` lists none.
The fake Wikimedia was stopped. Left behind on the host, by design: the rows of the replay
run (spikes, events, `api_cache` entries under the fake URLs) in Postgres, the `replay:1`
event in Neo4j, `data/recordings/session-09b-20261106T2000Z-wanted.jsonl` (not committed).

**Tests**: `uv run pytest`: 1210 passed, 1 skipped (Airflow is only in its image); new: `tests/test_logging_setup.py` (9), `tests/llm/test_tracing.py` (12), and 20 more across stream, detect, events, graph, agent, chart, cluster, infra and observability. `-m integration` for the touched DB tests
(`tests/events/test_events_db.py`, `tests/db`, `tests/graph/test_run_db.py`): 17 passed.
`ruff check`, `ruff format --check`, `terraform fmt -check` and `terraform validate` pass.

## Questions to test your understanding

1. Why is `correlation_id` structured metadata in Loki and not a label, while `service`
   is a label?
2. The grouper runs `save_events()` and the graph runner later processes the event in a
   worker thread (`asyncio.to_thread`). How does the LLM client's log line, written in
   that thread, know the event's correlation id, without any function passing it?
3. A developer adds real Langfuse keys to `.env` and runs `uv run pytest`. What stops the
   tests from sending traces, and what would a test have to do to trace on purpose?

<details>
<summary>Answers</summary>

1. Every distinct set of label values is a separate stream with its own index entries
   and chunks. `service` has a handful of values, so it is a cheap, fast first filter. A
   correlation id has a new value per story: as a label it would create a stream per
   story, an ever-growing index and many tiny chunks (Loki's "high cardinality" problem).
   As structured metadata it is stored with each line and filtered after the label
   selection picked the streams.
2. `GraphRunner.process()` binds it with `log_context(correlation_id=..., event_key=...)`,
   which sets a context variable. `asyncio.to_thread()` runs the function in a copy of
   the current context, so the thread sees the same variable, and `ContextFilter` puts
   its fields on every record that thread logs. The same mechanism carries the
   OpenTelemetry span into the thread, which is why the LLM call nests under the
   extraction's trace.
3. `tests/conftest.py` has an autouse fixture that installs `NoopTracer()` with
   `set_tracer()` before every test (and resets it after), so `get_tracer()` never builds
   a tracer from the settings during tests. A test that wants tracing installs its own,
   as `tests/llm/test_tracing.py` does: a `LangfuseTracer` with an in-memory span
   exporter, so even then nothing leaves the process.

</details>
