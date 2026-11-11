# infra/: the local infrastructure as code (spec 08c)

Two Terraform stacks, each with its own state:

| Stack | Creates | Lifetime |
|-------|---------|----------|
| `storage/` | the buckets `wikipulse-lake` and `wikipulse-mlflow` in the S3 store (RustFS in Compose) | long: the data. `prevent_destroy` refuses `terraform destroy` |
| `cluster/` | the kind cluster, the namespace, the Secret, the image in the node, the Helm release, Prometheus and Grafana | short: destroy and rebuild at will |

Docker Compose (`docker compose up -d`) must be running: the cluster's pods use its Redis,
Postgres, Neo4j and RustFS, and the storage stack talks to RustFS on `localhost:9000`.

```
uv run python scripts/cluster.py build                     # the image (Terraform doesn't build)
uv run python scripts/infra.py cluster init
uv run python scripts/infra.py cluster plan  -var offline=true
uv run python scripts/infra.py cluster apply -var offline=true
uv run python scripts/infra.py cluster destroy -var offline=true
```

Buckets that exist already (the app and MLflow create theirs on first use) are adopted
once instead of created:

```
uv run python scripts/infra.py storage init
uv run python scripts/infra.py storage import 'minio_s3_bucket.this["wikipulse-lake"]' wikipulse-lake
uv run python scripts/infra.py storage import 'minio_s3_bucket.this["wikipulse-mlflow"]' wikipulse-mlflow
uv run python scripts/infra.py storage plan                # then: No changes
```

(In Windows PowerShell 5.1, write the inner quotes as `\"`; PowerShell 7.3+ passes them as shown.)

`scripts/infra.py` passes your settings (`.env`) to Terraform as `TF_VAR_*` environment
variables, so no secret is ever written into a `.tf` file. Plain `terraform` works too if
you set those variables yourself (see each stack's `variables.tf`).

`offline=true` runs the consumer, enricher and graph runner with 0 replicas (no pod calls
Wikimedia). Terraform refuses `offline=false` while `USER_AGENT` holds the placeholder
contact.

An `apply` also deploys what Terraform can't compare by itself: `scripts/infra.py` passes
a checksum of `charts/wikipulse`, so an edited template is upgraded without a chart
version bump, and the release carries a checksum of the Secret's values, so a changed
password restarts the pods (they read the Secret only at start).

## Monitoring (spec 09a)

The cluster stack also installs Prometheus and Grafana (`cluster/monitoring.tf`, the
community chart kube-prometheus-stack, namespace `monitoring`), with WikiPulse's alert
rules and dashboards from `observability/`. `-var monitoring=false` leaves them out.

```
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # Grafana
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # Prometheus
kubectl -n monitoring get secret kube-prometheus-stack-grafana -o jsonpath="{.data.admin-password}"   # base64
```

(`terraform output grafana` prints the same commands.) In Grafana, the folder
**WikiPulse** holds the four dashboards; in Prometheus, **Alerts** shows the rules.

A session without the live stream (the placeholder contact in `.env`): deploy the replay
overlay, let Prometheus scrape a replay running on the laptop, then start the replay:

```
uv run python scripts/infra.py cluster apply -var offline=true -var replay=true -var 'host_metrics_targets={replay="host.docker.internal:9464"}'
$env:METRICS_PORT=9464; uv run python -m wikipulse.stream.replay --file data/recordings/<recording>.jsonl --speed 2
```

Stopping the replay (Ctrl+C) fires `WikiPulseNoEvents` about two minutes later.

## Logs (spec 09b)

`logging.tf` installs Loki and the Alloy collector next to Prometheus (`-var logging=false`
leaves them out); Grafana gets the data source **Loki** and the dashboard **WikiPulse:
Event trail**. Every pod logs JSON; search one story with its correlation id (from a log
line, or `spikes.correlation_id` / `events.correlation_id` in Postgres):

```
{system="wikipulse"} | correlation_id="<id>"
```

Only pods' logs reach Loki, so in a replay session the replay, a graph run and an agent run
run as one-off pods (`scripts/cluster.py run`, needs `-var replay=true`). Offline, the graph
run uses `scripts/fake_wikimedia.py` on the laptop instead of the Wikimedia APIs (in
PowerShell, `--env` and `--` work the same; in Git Bash set `MSYS_NO_PATHCONV=1` so
`/dev/stdin` isn't rewritten):

```
uv run python scripts/cluster.py run --name replay --stdin data/recordings/<recording>.jsonl -- python -m wikipulse.stream.replay --file /dev/stdin --speed 6
uv run python scripts/fake_wikimedia.py        # another terminal
uv run python scripts/cluster.py run --name graph-session --env WIKI_API_URL=http://host.docker.internal:8089/w/api.php --env WIKIDATA_API_URL=http://host.docker.internal:8089/wikidata/api.php -- python -m wikipulse.graph.run --run-id replay --once
uv run python scripts/cluster.py run --name agent-session --model-cache -- python -m wikipulse.agent.run --event replay:1 --once
```

LLM tracing: set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in `.env`; `apply` puts
them into the Secret (empty: no tracing).

## Replacing the cluster

Changing `cluster_name`, `node_image` or `kubeconfig_path` (marked in
`cluster/variables.tf`) replaces the kind cluster, and the plan fails with
`Failed to read namespace "wikipulse": Get "http://localhost/..."`: the Kubernetes and
Helm providers would need the new cluster's address before it exists. Destroy the old
cluster first, without refreshing, then apply with the new value:

```
uv run python scripts/infra.py cluster destroy -refresh=false -var offline=true
uv run python scripts/infra.py cluster apply -var offline=true -var node_image=kindest/node:<version>
```

`kubeconfig_path` is stored expanded (for example `C:\Users\<you>\.kube\config`), so
running the same state as another user or on another machine also replaces the cluster:
use the same recovery.

## State

The state (`terraform.tfstate`) is local and git-ignored: it contains the secrets in plain
text. Explanation, decisions and results: `docs/learning/08c-terraform.md`.
