# 08a: Kubernetes deployment

## What was built

WikiPulse's live pipeline now runs on a local Kubernetes cluster made with **kind**
("Kubernetes in Docker": the whole cluster is one Docker container, `wikipulse-control-plane`).

| Piece | File | What it does |
|-------|------|--------------|
| Image | `Dockerfile`, `.dockerignore` | One image for every service, built in two stages with uv, running as user 10001 |
| Manifests | `k8s/` | Namespace, ConfigMap, Secret template, Services, a volume claim, a migration Job, seven Deployments, network policies |
| Cluster script | `scripts/cluster.py` | `up` = create the cluster, build, load, Secret, migrate, apply, wait; `down` deletes it |
| Probes | `wikipulse/health.py` | Workers touch a heartbeat file; `python -m wikipulse.health` is the probe |
| Pod-kill test | `scripts/k8s_crash_test.py` | Deletes the consumer pod mid-stream twice and checks nothing was lost or doubled |

How the pieces fit:

```
 host (Windows)                                   kind node (one Docker container)
 ───────────────                                  ─────────────────────────────────
 Docker Compose:  redis :6379  ◄──────────┐       namespace wikipulse
                  postgres :5432 ◄────────┤        consumer  ─► Wikimedia (HTTPS)
                  neo4j :7687 ◄───────────┤        detector, archiver, enricher,
                  rustfs (S3) :9000 ◄─────┤        grouper, graph, api
 Ollama :11434 ◄──────────────────────────┘        Services redis, postgres, neo4j, s3
                 host.docker.internal               = DNS aliases of host.docker.internal
```

The services in the cluster are the pipeline's long-running processes: the consumer
(stage 1), the detector (2), the enricher and the grouper (3), the graph runner (4), the
API with the live page (5) and the lake archiver (6). Each is a **Deployment** running
the same image with a different `command`. Redis, Postgres, Neo4j and RustFS stay in
Docker Compose (memory: the laptop has 16 GB and Docker gets 7.4 GB), and Ollama stays on
the host where it has the GPU.

## Key concepts, with this project's code

### Core objects

- **Namespace** (`k8s/namespace.yaml`): a folder for everything WikiPulse puts in the
  cluster. `kubectl delete namespace wikipulse` would remove it all. Its label
  `pod-security.kubernetes.io/enforce: restricted` makes the API server *refuse* pods
  that could run as root or gain privileges, so the security settings below are
  enforced, not just written down.
- **Deployment** (`k8s/consumer.yaml` and the others): "keep one pod like this running".
  When a pod dies or is deleted, the Deployment's ReplicaSet starts a new one. That is
  what the pod-kill test relies on.
- **ConfigMap** (`k8s/configmap.yaml`): the settings, as environment variables
  (`envFrom`). `wikipulse/config.py` reads environment variables before `.env`, so no
  code changed: `REDIS_URL=redis://redis:6379/0` simply wins over the default.
- **Secret** (`k8s/secret.example.yaml` is only a template): passwords, the Postgres
  DSN and the User-Agent (its contact address is personal). `cluster.py secret` builds
  it in memory from your settings (`secret_manifest()`), with the DSN's host replaced
  by `postgres` (`cluster_dsn()`), and pipes it to `kubectl apply -f -`, so it never
  touches the disk. Secrets are only base64-encoded in etcd, not encrypted: they keep
  values out of git and out of `kubectl get configmap`, nothing more.
- **Service**: a stable name for something. `api` (in `k8s/api.yaml`) is a ClusterIP
  Service in front of the API pods. `redis`, `postgres`, `neo4j` and `s3`
  (`k8s/external-services.yaml`) are **ExternalName** Services: DNS aliases of
  `host.docker.internal`, Docker Desktop's name for the host. The applications only
  know `redis:6379`; moving Redis into the cluster later replaces the Service, not the
  settings. Ollama is the exception: it is called as `host.docker.internal:11434`
  directly, because it refuses requests whose `Host` header isn't a local name
  (`localhost`, `*.local`, `*.internal`). Through an `ollama` alias every call got
  HTTP 403 (found in the live session; see deviations).
- **PersistentVolumeClaim** (`k8s/model-cache.yaml`): the grouper and the API need the
  embedding model (`BAAI/bge-small-en-v1.5`). Instead of baking it into the image, it
  is downloaded once into a 1 GiB volume mounted at `/models` (`HF_HOME`). kind's
  `local-path` provisioner creates the volume as a directory on the node; it survives
  pod restarts and image rebuilds.
- **Job** (`k8s/migrate-job.yaml`): runs `python -m wikipulse.db.migrate` to completion
  before the services start (`cluster.py migrate`).
- **StatefulSet**: not used. It is the object for databases (stable names, one volume
  per replica); every stateful service stayed in Compose, as the laptop's memory asked.

### Health: probes

Kubernetes asks each container three questions:

- **startupProbe**: "have you started?" Other probes wait until it passes, so a slow
  start (the grouper's first start downloads the model) isn't mistaken for a hang.
- **readinessProbe**: "may I send you traffic?" Only matters for the API (the Service
  routes only to ready pods), but it also shows `READY 1/1` in `kubectl get pods`.
- **livenessProbe**: "are you still alive?" Fails three times in a row, and the
  container is restarted.

The API answers `GET /health` (`httpGet` probe). The probe sends `Host: localhost`,
because the API refuses other Host headers (DNS rebinding protection from spec 05e).

The workers have no port, so they got a **heartbeat** (`wikipulse/health.py`):
`Heartbeat.beat()` touches `HEALTH_FILE` (`/tmp/heartbeat`, at most once a second), and
the probe command `python -m wikipulse.health --max-age 300` exits 1 when the file is
older than that. Where each worker beats:

- the consumer: at every connection attempt and after every message
  (`Consumer.run_live(..., heartbeat=...)`);
- the detector and the archiver: every time their loop asks "should I stop?", which
  happens per entry and per 2-second read (`beat_while(stop, heartbeat)` in their
  `main()`);
- the enricher, the grouper and the graph runner: once per poll, plus after every page
  (`enrich_titles(..., heartbeat)`) or every event (`GraphRunner.step(..., heartbeat)`).

The allowed age is set per service from its slowest normal iteration: 300 s for the
stream workers (the consumer's longest reconnect backoff is 60 s), 600 s for the poll
loops, 900 s for the graph runner (one event can take several LLM calls of up to 120 s).
A heartbeat measures *wall-clock* time on purpose: the question is whether the process
is making progress now, which has nothing to do with event time.

### Health: resources

Every container has `requests` (what the scheduler reserves) and `limits` (the ceiling).
CPU over the limit is throttled; memory over the limit gets the container killed
(`OOMKilled`). The torch services (grouper, API) may use 1.5 GiB, the consumer 256 MiB.
With everything running, the kind node used about 2 GiB.

### Scaling, and why the workers run once

The spec suggested running three stream workers that share the load through a consumer
group. None of WikiPulse's stream workers can do that correctly, so all run with
`replicas: 1` and `strategy: Recreate` (the old pod stops before the new one starts):

- the **consumer** holds the one Wikimedia connection (their policy; and two consumers
  would write every edit twice);
- the **detector** keeps per-page state (windows, baselines). A consumer group hands
  each entry to *one* consumer of the group, round-robin, not by page, so three
  detectors would each see a third of every page's edits and detect nothing;
- the **archiver** takes a Redis lock: one writer per lake.

Sharing load through a group works for *stateless* per-entry work. Scaling the detector
would need partitioning by page (one stream per hash bucket, a detector per bucket),
which is what Kafka's partitions do. The API is stateless and could run several
replicas behind its Service (it uses `RollingUpdate`), but each replica loads its own
embedding model (~500 MB), so it runs once here. `kubectl -n wikipulse scale
deployment/api --replicas=2` tries it.

### Container images

`Dockerfile` has two stages. The **build** stage copies uv in, installs the locked
dependencies (`uv sync --locked --no-dev --no-install-project`) in a layer of their own,
then WikiPulse itself; the **runtime** stage copies only `/opt/venv` onto a clean
`python:3.12-slim-bookworm`. Changing code rebuilds only the last layer.

Tags: `cluster.py build` tags the image with the git commit (`wikipulse:ad3dd15`), plus
`-dirty-<time>` with uncommitted changes (`image_tag()`), so a new build is always a
new tag and a rollout always means a new image. kind has no registry, so `kind load
docker-image` copies the image into the node; `imagePullPolicy: IfNotPresent` keeps
Kubernetes from trying to pull it. The manifests say `wikipulse:dev`;
`with_image_tag()` points them at the tag just built.

### Security basics

- **Non-root**: the image's user is 10001; the pods set `runAsNonRoot`, drop all Linux
  capabilities, forbid privilege escalation and use the default seccomp profile.
- **Read-only filesystem**: containers can't write to their image; `/tmp` is an
  `emptyDir` (the heartbeat, `HOME`, `DATA_DIR`), `/models` the model volume.
- **No API credentials**: `automountServiceAccountToken: false`; no pod talks to the
  Kubernetes API.
- **Network policies** (`k8s/network-policies.yaml`): `default-deny-all` blocks
  everything in and out, then each service gets exactly its ports (consumer: Redis and
  HTTPS; detector: Redis and Postgres; graph: Postgres, Neo4j, Ollama, HTTPS; ...). Only
  the API accepts connections. kind's network plugin (kindnet) enforces them; checked
  from inside the pods (see Results).

## Design decisions and deviations

- **Stateful services stay in Compose**, reached through ExternalName Services and
  `host.docker.internal` (the spec allowed either). Ollama stays on the host, reached by
  the name `host.docker.internal` itself: the first version had an `ollama` ExternalName
  Service, and the graph runner's first extractions in the cluster failed with
  `Ollama refused the request` (HTTP 403 for the Host header `ollama`). Changing the
  ConfigMap was not enough by itself: environment variables are read when a container
  starts, so `kubectl rollout restart deployment/graph deployment/api` was needed.
- **The archiver is deployed too** (stage 6 is part of the live pipeline), and the
  **enricher** (the grouper waits for it). The scorer (`wikipulse.ml.serve`), the
  agent and the hourly-stats worker are not deployed: the scorer keeps 26 hours of edits
  in memory, which the laptop can't spare next to everything else. They can be added
  the same way.
- **No three-worker scaling demo** (see "Scaling"): no WikiPulse stream worker is
  stateless, and demonstrating it with the detector would produce wrong spikes.
- **One image includes torch** (CPU-only, ~800 MB of the 2.9 GB image): the grouper and
  the API's question answering run the embedding model, so serving needs it. The image
  is 642 MB compressed. Making the smaller workers lean would mean a second image
  without torch, mlflow, scikit-learn and matplotlib, which needs optional dependency
  groups in `pyproject.toml`; left for later.
- **Bytecode is compiled into the image** (`UV_COMPILE_BYTECODE=1`): with a read-only
  filesystem Python can't cache `.pyc` files, so every start would compile torch again.
- **The model is cached on a volume**, not baked into the image (keeps the image lean;
  egress to HTTPS is allowed for the grouper and the API for this).
- **The cluster's consumer has its own checkpoint** (`--namespace wiki:k8s:consumer`).
  The laptop consumer's checkpoint was three days old; resuming it would have pulled
  three days of the stream (millions of messages) from Wikimedia. The cluster consumer
  starts at "now" and writes the shared `wiki:edits`, so the detector and archiver
  groups simply continue. Don't run the laptop consumer at the same time.
- **The pod-kill test reads its reference afterwards** (`scripts/k8s_crash_test.py`).
  `scripts/crash_test.py` records the stream next to the consumer: two connections. Here
  the consumer is scaled to 0 first, then the reference is read from the consumer's
  starting checkpoint (Last-Event-ID): one connection at a time. Events from before the
  start that the server repeats are excluded by id.
- **API: `API_LISTEN_ALL`** (new setting). The API refused any non-loopback address;
  in a pod it must listen on `0.0.0.0` for the Service and the probes. The switch allows
  exactly that; requests must still name a loopback Host (as `kubectl port-forward`
  sends), and the network policy decides who may connect.
- **`HEALTH_FILE`** (new setting): unset outside a container, so nothing changes on the
  laptop.
- **`enableServiceLinks: false`** on every pod. Found on the first deploy: Kubernetes
  sets `API_PORT=tcp://10.96.x.x:8000` in every pod for the Service `api`, and
  `API_PORT` is a WikiPulse setting; every service crashed on start with a validation
  error. Service links are a leftover from Docker links; DNS names replace them.
- **Migrations run before the services**, as a Job, after the ConfigMap and the
  network policies (`apply_base()`); the first attempt failed because the ConfigMap
  didn't exist yet.
- **PyYAML is declared as a dev dependency** for `tests/test_k8s.py` (it was already
  installed through other packages).
- **Tools**: kind 0.33.0 was already installed through winget, kubectl 1.34 comes with
  Docker Desktop; nothing was installed. The node runs Kubernetes 1.37.

## Results

Run on 2026-11-10 against the real Wikimedia stream (one connection, the User-Agent from
settings, which still carries the placeholder contact `you@example.com`; `cluster.py`
and the consumer both warn about it).

**Image**: `wikipulse:ad3dd15`, 2.9 GB on disk, 642 MB compressed (torch CPU 809 MB,
pyarrow 170 MB, transformers 120 MB, scipy 109 MB). First build about 3 minutes, a code
change rebuilds only the last layers; `kind load` took about a minute.

**Live session** (about 30 minutes, 15:52 to 16:22 UTC). `kubectl -n wikipulse get pods`
at the end:

```
NAME                        READY   STATUS      RESTARTS   AGE
api-7b7b647688-v85j9        1/1     Running     0          3m49s
archiver-96fd4dcb4-g5rlp    1/1     Running     0          20m
consumer-8c9cccd45-6pf45    1/1     Running     0          20m
detector-78cd757b94-hk95m   1/1     Running     0          20m
enricher-67b566bc7d-5qxhp   1/1     Running     0          20m
graph-59cd68fdbf-nkg64      1/1     Running     0          3m48s
grouper-cbd68b6f7-qmspm     1/1     Running     0          20m
migrate-sgxt8               0/1     Completed   0          28m
```

(The 20-minute ages: every worker was hard-killed once on purpose, see below; graph
and api were restarted for the Ollama fix.) The whole chain worked inside the cluster:
the consumer read 58,745 messages and kept 1,986 English article edits; the detector
found a spike (`Bounty (chocolate bar)`, 16:08 UTC); the enricher fetched its page (two
HTTP 429 answers from Wikipedia on the first requests, retried politely after 30 s); the
grouper downloaded the embedding model into the volume and made event `live:4`; the
graph runner extracted it with `qwen3:4b` on the host's Ollama (2 entities, 19.4 s); the
API served it through `kubectl port-forward` (`GET /events/live:4` with its headline);
the archiver wrote hour 16 to the lake every 5 minutes (1,496 rows by 16:20). Only one
spike in half an hour, which is little but not unusual for a short window (the
detector also resumed after a three-day gap in its state); the point here was the
whole chain running in the cluster, not the volume. The kind node used about 2.0 GB of Docker's
7.4 GB; no Compose service had to be stopped.

**Network policies**, checked from inside the pods with a socket connect:

| from / to | redis 6379 | postgres 5432 | neo4j 7687 | ollama 11434 | api 8000 |
|-----------|-----------|---------------|------------|--------------|----------|
| consumer  | open      | blocked       | blocked    | blocked      | blocked  |
| detector  | open      | open          | blocked    | blocked      | blocked  |
| graph     | blocked   | open          | open       | open         | blocked  |

**Consumer pod-kill test** (`scripts/k8s_crash_test.py`, full length, 6 minutes plus
the reference read):

```
Hard kill: ['pod/consumer-8c9cccd45-jn9l6'] replaced in 7 s
Graceful delete: replaced in 7 s
Stopped: 399 entries written since the start
Reached the final checkpoint after 12046 messages
Kept events in the recording (up to the consumer's last): 399
Entries in Redis:                                         399
Missing from Redis:                                       0
In Redis but not in the recording:                        0
Duplicate entries in Redis:                               0
Events that arrived while the consumer was dead:          7 (7 of them recovered after the restart)
PASS
Events that arrived around the graceful delete:           9 (9 of them in Redis)
```

**The other workers** were then hard-killed together (`kubectl delete pod --force
--grace-period=0` on detector, archiver, enricher, grouper, graph). All came back
within seconds. The detector restored its snapshot and re-read its pending entry; the
archiver found the dead pod's lock, waited until it expired (60 s), then re-read the 199
entries it had read but not yet written to the lake, so nothing was lost there either.

**Tests**: `uv run pytest` 1070 passed (new: `tests/test_health.py`, `tests/test_k8s.py`
with 44 checks of the manifests and `cluster.py`, a heartbeat test in
`tests/stream/test_consumer.py`); `ruff check` and `ruff format --check` clean. One
earlier run stopped with two collection errors that didn't come back in two reruns;
the cause is unknown.

**Cluster afterwards**: deleted (`scripts/cluster.py down`), to give the laptop its
memory back and close the Wikimedia connection. `scripts/cluster.py up` recreates it in
about five minutes.

## Questions to test your understanding

1. The detector is killed while it holds 300 entries it has read but not acknowledged.
   Why does the new pod not lose them, and why would three detector replicas be wrong
   even though the consumer group would happily spread entries over them?
2. Why does every pod set `enableServiceLinks: false`, and what would happen without it?
3. The consumer's liveness probe allows a heartbeat age of 300 s, its readiness probe
   120 s. What would go wrong with a liveness limit of 30 s during a Wikimedia outage?

<details>
<summary>Answers</summary>

1. Entries a consumer of a group has read but not acknowledged stay in the group's
   pending list (PEL) under the consumer's name. The new pod uses the same consumer name
   (`DEFAULT_CONSUMER`), re-reads its PEL first (`DetectionRunner._replay_pending`) and
   skips what its last snapshot already covers. With three replicas, the group would
   give each entry to one of them, regardless of the page, so each detector would see
   about a third of a page's edits: its windows and baselines would be wrong and most
   spikes would never reach the thresholds.
2. With service links, Kubernetes puts variables like `API_PORT=tcp://10.96.132.95:8000`
   into every pod for every Service in the namespace. WikiPulse's settings read
   environment variables, and `API_PORT` must be an integer, so every service failed at
   start with a pydantic validation error (it happened on the first deploy).
3. During an outage the consumer waits with backoff (up to 60 s) between connection
   attempts and gets no messages, so its heartbeat ages. With 30 s, Kubernetes would
   restart the container again and again; each restart resets the backoff, so the
   consumer would knock on Wikimedia's door more often, exactly when it should wait.
   300 s restarts only a consumer that is really stuck.

</details>
