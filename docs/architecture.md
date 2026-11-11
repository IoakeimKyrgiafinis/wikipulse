# Architecture

How WikiPulse fits together, drawn from the code as it is (spec 10). Each box names the
module you run (`uv run python -m <module>`) or the file that defines it; the learning
notes in [`docs/learning/`](learning/) explain each one in depth.

## The idea in one paragraph

Wikipedia publishes every edit as a live stream. When something happens in the world,
the pages about it suddenly get many edits from many people. WikiPulse reads the stream,
notices those **spikes**, groups spikes about the same story into **events**, has a local
LLM turn each event into a small **knowledge graph** (people, organizations, places and
how they relate, each fact citing a Wikipedia revision), and answers questions about it
with **GraphRAG**, through a web page, an API, an MCP server and an analyst agent. Around
that sits an MLOps platform: a data lake, batch jobs, a news classifier with a model
lifecycle, Kubernetes, Helm, Terraform, monitoring and CI/CD.

## The pipeline

Every arrow between processes goes through a store (Redis, Postgres, Neo4j, S3), never a
direct call, so each process can stop, restart or be replaced without the others noticing.

```mermaid
flowchart LR
    WM[("Wikimedia<br/>EventStreams")]
    WAPI[("Wikipedia and<br/>Wikidata APIs")]
    REC[/"recordings<br/>data/recordings/*.jsonl"/]

    subgraph ingest ["1. Ingest"]
        CONSUME["stream.consume<br/>Consumer + LiveSource"]
        RECORD["stream.record"]
        REPLAY["stream.replay<br/>Consumer + FileSource"]
    end

    STREAM[("Redis Streams<br/>wiki:edits<br/>wiki:replay:edits")]

    subgraph understand ["2. Detect and group"]
        DETECT["detect.run<br/>DetectionRunner + SpikeDetector"]
        ENRICH["enrich.run<br/>WikimediaClient"]
        GROUP["events.run<br/>GroupingRunner + Grouper<br/>(bge-small embeddings)"]
    end

    PG[("Postgres<br/>spikes, pages, api_cache,<br/>events, event_pages,<br/>graph_extractions, spike_scores")]

    subgraph graphstage ["3. Knowledge graph"]
        GRAPH["graph.run<br/>inputs, extract, link, store"]
        INDEX["qa.index<br/>full-text + vector indexes"]
    end

    NEO[("Neo4j<br/>Event, Entity, Page,<br/>relations with sources")]

    subgraph serve ["4. Answer"]
        QA["qa.ask<br/>Retriever: graph, Cypher, vector<br/>+ cited answer"]
        MCP["mcp_server.server<br/>Explorer: 5 read-only tools"]
        AGENT["agent.run<br/>agent loop, briefings"]
        API["api<br/>FastAPI + WebSocket + page"]
    end

    LLM["wikipulse.llm<br/>get_client().chat()"]
    OLLAMA[("Ollama<br/>qwen3:4b / qwen3:8b")]
    USER(["browser, Claude Desktop,<br/>any MCP client"])

    WM --> CONSUME --> STREAM
    WM --> RECORD --> REC --> REPLAY --> STREAM
    STREAM --> DETECT --> PG
    PG --> ENRICH --> PG
    WAPI <--> ENRICH
    PG --> GROUP --> PG
    PG --> GRAPH --> NEO
    WAPI <--> GRAPH
    NEO --> INDEX --> NEO
    NEO --> QA
    QA --> MCP
    MCP --> AGENT
    AGENT -->|"briefing"| NEO
    PG --> API
    NEO --> API
    QA --> API
    API --> USER
    MCP --> USER
    GRAPH -.-> LLM
    QA -.-> LLM
    AGENT -.-> LLM
    LLM -.-> OLLAMA
```

| Step | Module | Reads | Writes | Learning note |
|------|--------|-------|--------|---------------|
| Consume | `wikipulse.stream.consume` | EventStreams (SSE, one connection, `Last-Event-ID` resume) | `wiki:edits` (deduplicated, checkpointed atomically by a Lua script) | [01](learning/01-stream-consumer.md) |
| Record / replay | `wikipulse.stream.record`, `.replay` | the stream / a recording | a file / `wiki:replay:edits` through the same `Consumer.process` | [01](learning/01-stream-consumer.md) |
| Detect | `wikipulse.detect.run` | a Redis Stream (consumer group) | `spikes` | [02a](learning/02a-spike-detector.md), [02b](learning/02b-detection-eval.md) |
| Enrich | `wikipulse.enrich.run` | new spikes; Wikipedia Action and REST APIs (cached) | `pages`, `api_cache` | [03a](learning/03a-page-enrichment.md) |
| Group | `wikipulse.events.run` | spikes with their pages | `events`, `event_pages` | [03b](learning/03b-event-grouping.md) |
| Extract | `wikipulse.graph.run` | events, pages, burst text, Wikidata | Neo4j; `graph_extractions` | [04b](learning/04b-graph-extraction.md), [04c](learning/04c-extraction-eval.md) |
| Ask | `wikipulse.qa.ask` (`Retriever`, `answer`) | Neo4j (traversal, checked Cypher, vector search) | an answer with numbered citations | [05a](learning/05a-graph-retrieval.md), [05b](learning/05b-qa-evals.md) |
| MCP | `wikipulse.mcp_server.server` (`Explorer`) | Neo4j, the QA session | MCP tools, resource template, prompts | [05c](learning/05c-mcp-server.md) |
| Agent | `wikipulse.agent.run` (`agent/loop.py`) | the MCP tools, `read_page`, `related_events` | a cited briefing on the `Event` node; transcripts | [05d](learning/05d-analyst-agent.md) |
| Page | `wikipulse.api` (`EventReader`, `EventWatcher`) | Postgres (the list, seconds after detection), Neo4j (headlines, entities, briefings) | REST, WebSocket `/ws/events`, the page | [05e](learning/05e-api-and-web.md) |

All LLM calls go through `wikipulse.llm` ([04a](learning/04a-llm-client.md)): neutral
messages, tools and JSON schemas in, a backend (`ollama_backend.py`, optionally
`anthropic_backend.py`) out. Nothing else imports a provider's package.

## Batch, data lake and the model lifecycle

```mermaid
flowchart LR
    STREAM[("Redis Stream<br/>wiki:edits")]
    WM[("EventStreams<br/>?since= history")]

    ARCHIVE["lake.archive<br/>consumer group lake:archive"]
    BACKFILL["lake.backfill"]
    S3[("S3 object store (RustFS)<br/>edits/ and history/edits/<br/>year=/month=/day=/hour= Parquet")]
    QUERY["lake.query<br/>DuckDB SQL"]

    NOTIFY[("Redis pub/sub<br/>lake:notifications")]
    WORKER["workers.run<br/>NotificationListener + Invoker"]

    subgraph airflow ["Airflow (dags/)"]
        DAILY["daily_stats<br/>daily_eval"]
        RETRAIN["retrain (weekly):<br/>check_drift, extend_history,<br/>build_dataset, train_challenger,<br/>evaluate_against_champion,<br/>promote_or_reject"]
    end

    PG[("Postgres<br/>hourly_stats, daily_stats,<br/>detection_eval_daily,<br/>spike_scores, drift_reports")]

    DATASET["ml.dataset build<br/>features + labels + time split"]
    TRAIN["ml.train<br/>rule, logreg, trees, network"]
    MLFLOW[("MLflow<br/>runs + model registry<br/>@champion / @challenger")]
    SERVE["ml.serve<br/>LiveScorer"]
    DRIFT["ml.drift<br/>PSI + noise floor"]

    STREAM --> ARCHIVE --> S3
    WM --> BACKFILL --> S3
    S3 --> QUERY
    S3 -->|"object created"| NOTIFY --> WORKER --> PG
    PG --> DAILY --> PG
    S3 --> DATASET --> S3
    DATASET --> TRAIN --> MLFLOW
    MLFLOW --> SERVE
    STREAM --> SERVE --> PG
    PG --> DRIFT --> PG
    RETRAIN --> DATASET
    RETRAIN --> TRAIN
    RETRAIN --> MLFLOW
```

Notes [06a](learning/06a-data-lake.md), [06b](learning/06b-event-driven-worker.md),
[06c](learning/06c-scheduled-jobs.md), [07a](learning/07a-training-data.md),
[07b](learning/07b-news-classifier.md), [07c](learning/07c-serving-and-retraining.md).

## Deployment and operations

```mermaid
flowchart TB
    GH["GitHub Actions<br/>ci.yml: test, terraform, dags,<br/>eval-gate, smoke<br/>release.yml: images, Trivy, chart"]
    GHCR[("GHCR<br/>image + Helm chart")]
    TF["Terraform<br/>infra/storage, infra/cluster<br/>(scripts/infra.py)"]
    HELM["Helm chart<br/>charts/wikipulse"]

    subgraph kind ["kind cluster (one Docker container)"]
        PODS["one image, one Deployment per service:<br/>consumer, detector, enricher, grouper,<br/>graph, archiver, api"]
        MON["Prometheus + Grafana dashboards<br/>Loki + Alloy (logs)"]
    end

    COMPOSE[("Docker Compose on the host<br/>Redis, Postgres, Neo4j, RustFS,<br/>MLflow, Airflow")]
    OLLAMA[("Ollama on the host")]
    LANGFUSE[("Langfuse (optional)<br/>LLM traces")]

    GH --> GHCR
    GHCR -->|"scripts/deploy.py"| HELM
    TF --> HELM --> PODS
    TF --> MON
    PODS -->|"host.docker.internal"| COMPOSE
    PODS --> OLLAMA
    PODS -->|"/metrics, JSON logs"| MON
    PODS -.-> LANGFUSE
```

Notes [08a](learning/08a-kubernetes.md), [08b](learning/08b-helm-chart.md),
[08c](learning/08c-terraform.md), [09a](learning/09a-metrics-dashboards.md),
[09b](learning/09b-logging-tracing.md), [09c](learning/09c-cicd-pipeline.md).

The stateful services stay in Docker Compose and Ollama on the host: a 16 GB laptop can't
run them inside the cluster as well, and the cluster can be deleted and recreated without
losing data. The Helm chart's `values-offline.yaml` turns off the three services that call
Wikimedia (consumer, enricher, graph), which is how CI's smoke test and offline sessions
run.

## The quickstart path

`scripts/quickstart.py` runs the smallest useful part of the first diagram, offline:

```mermaid
flowchart LR
    SAMPLE[/"demo/sample/<br/>edits.jsonl.gz, pages.jsonl.gz"/]
    REPLAY["stream.replay<br/>warm-up at full speed,<br/>then the day at 300x"]
    STREAM[("Redis<br/>wiki:quickstart:edits")]
    DETECT["detect.run<br/>run quickstart"]
    PG[("Postgres<br/>spikes, pages, events")]
    GROUP["events.run"]
    API["api"]
    NEO[("Neo4j<br/>(empty, or filled with --graph)")]
    BROWSER(["browser"])

    SAMPLE --> REPLAY --> STREAM --> DETECT --> PG
    SAMPLE -->|"pages loaded directly, no enricher"| PG
    PG --> GROUP --> PG
    PG --> API
    NEO --> API
    API --> BROWSER
```

Every Wikimedia URL of the processes it starts points at a local address, so nothing can
reach Wikimedia even by mistake. With `--graph`, the graph runner extracts each event with
the local LLM against `scripts/fake_wikimedia.py`, an offline stand-in that answers every
Wikimedia request with an empty result.

## Principles that hold everywhere

- **Event time, not wall-clock time.** Windows, baselines, spike lifecycles, event
  closing and "today" in questions all use the edits' own timestamps. A replayed
  recording gives exactly the same spikes and events as the live run (checked by
  `scripts/determinism_check.py` and the 03b replays).
- **At-least-once delivery, idempotent writes.** Consumer groups acknowledge only after
  the write; spikes, events, lake hours and graph extractions are upserts keyed on their
  identity, so a crash and restart repeats work but never doubles it.
- **One polite Wikimedia client.** One stream connection, a descriptive User-Agent,
  backoff, and caches (`api_cache`, `data/cache/`) in front of every API.
- **Every number has an eval.** Detection, grouping, extraction, question answering,
  prompt injection and the classifier each have a committed evaluation in
  `evals/results/`, and CI blocks a pull request that makes detection worse.
