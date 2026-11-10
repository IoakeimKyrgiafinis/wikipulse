# 08b: Helm chart

## What was built

The Kubernetes deployment of 08a (fifteen hand-written YAML files in `k8s/`) is now a
**Helm chart**, `charts/wikipulse/`. One command installs, upgrades or rolls back the
whole pipeline, and the same chart serves different environments through values files.

| Piece | File | What it does |
|-------|------|--------------|
| Chart metadata | `charts/wikipulse/Chart.yaml` | Name, chart version (0.1.0), app version |
| Default values | `values.yaml` | Every setting, documented: image, ConfigMap entries, one block per service (replicas, command, resources, probe ages, egress ports) |
| Environments | `values-dev.yaml`, `values-prod.yaml` | The laptop; "production" with two API replicas, stricter limits and a disruption budget |
| Offline overlay | `values-offline.yaml` | The consumer, enricher and graph runner (the services that call Wikimedia) at 0 replicas |
| Values schema | `values.schema.json` | Refuses wrong values before anything is rendered (a second consumer, a password in the ConfigMap, a typo) |
| Shared templates | `templates/_helpers.tpl`, `templates/_deployment.tpl` | Labels, security settings, probes, and *one* Deployment template for all seven services |
| Service templates | `templates/consumer.yaml` ... `api.yaml` | One file per service, each a one-line call of the shared Deployment template (plus the API's Service) |
| Other templates | `configmap.yaml`, `external-services.yaml`, `model-cache.yaml`, `network-policies.yaml`, `migrate-job.yaml`, `api-pdb.yaml`, `NOTES.txt` | |
| Cluster script | `scripts/cluster.py` | `deploy` = `helm upgrade --install`; new `history` and `rollback` |
| Tests | `tests/test_chart.py`, `tests/test_cluster.py` | Lint and template tests; the script's logic |
| CI | `.github/workflows/ci.yml` | Installs Helm (`azure/setup-helm`), lints and renders every environment, runs the tests |

How the pieces fit:

```
values.yaml ─┐
values-dev.yaml or values-prod.yaml ─┤ merged, in this order        templates/*.yaml
values-offline.yaml (optional) ──────┤ ──► checked against ──► rendered with ──► Kubernetes
--set image.tag=<commit> ────────────┘     values.schema.json     Go templates      objects
                                                                                      │
          helm upgrade --install wikipulse charts/wikipulse ... --wait  ◄─────────────┘
          = revision N of the release "wikipulse", recorded in the namespace
```

`scripts/cluster.py up` still does everything (create the kind cluster, build, load,
Secret), but its last step is now `deploy`: it creates the namespace, then runs

```
helm --kube-context kind-wikipulse --namespace wikipulse upgrade --install wikipulse charts/wikipulse
     --values charts/wikipulse/values-dev.yaml --set image.tag=<commit> --wait --timeout 15m
```

The raw manifests are gone from `k8s/`; only `k8s/kind-config.yaml` (the cluster, not the
application) is left.

## Key concepts, with this project's code

### Templating and values

A Helm **chart** is a folder of templates plus default values. A template is a YAML file
with Go template expressions in `{{ }}`: `{{ .Values.consumer.replicas }}` is replaced
by the value at that path. `helm template` shows the result without touching a cluster.

**Values are merged in layers**: the chart's `values.yaml`, then every `--values` file in
order, then every `--set`. Later layers win, key by key (maps merge; lists are replaced
whole). So `values-prod.yaml` only lists what differs, e.g. `api.replicas: 2` and the
resources of each service, and `values-offline.yaml` only lists three `replicas: 0`. In
`cluster.py`, `deploy_command()` builds exactly that order: the environment file, then
the offline overlay, then `--set image.tag=...`.

**Named templates** (`define` / `include`) keep the chart DRY. In 08a the seven
Deployments were seven copies of the same 80 lines with small differences. Now
`templates/_deployment.tpl` defines `wikipulse.deployment` once, and
`templates/detector.yaml` is just

```
{{ include "wikipulse.deployment" (dict "root" $ "name" "detector") }}
```

The differences live in `values.yaml` under `detector:` (command, grace period,
resources, heartbeat ages, startup allowance, egress ports). The shared template decides
what must never vary: workers always get `strategy: Recreate` and heartbeat probes; only
the call in `api.yaml` passes `"http" true`, which gives the API its port, its HTTP
probes and a rolling update. Files whose names start with `_` are not rendered on their
own; they only hold definitions.

Some template details worth knowing:

- `toYaml` / `nindent`: `{{- toYaml $values.resources | nindent 12 }}` turns a values map
  into YAML indented to fit. `{{-` and `-}}` trim the whitespace next to them, which is
  how a template avoids blank lines (the first version of `wikipulse.labels` left one).
- **Checksum annotation**: every pod template carries
  `checksum/config: <sha256 of the rendered ConfigMap>`. Environment variables are only
  read when a container starts (08a learned that the hard way with the Ollama fix), so
  changing the ConfigMap alone did nothing until a manual restart. Now a config change
  changes the pod template, and Kubernetes rolls the pods by itself.
- **`helm.sh/resource-policy: keep`** on the model volume (`model-cache.yaml`):
  `helm uninstall` leaves it, so the downloaded embedding model survives.
- **Fixed names**: objects are named `consumer`, `api`, `wikipulse-config`, not
  `<release>-consumer` as in most public charts. The settings refer to the Services
  `redis`, `postgres`, ... by name, and two releases in one namespace would mean two
  consumers anyway. One release per namespace is the rule.

### The values schema

`values.schema.json` is a JSON Schema that Helm checks the *merged* values against on
`install`, `upgrade`, `template` and `lint`. It encodes the rules of 08a that were only
comments before:

- every worker's `replicas` is 0 or 1 (`"maximum": 1`): `--set consumer.replicas=2`
  fails with `at '/consumer/replicas': maximum: got 2, want 1` before anything reaches
  the cluster;
- no ConfigMap key may look like a secret (`PASSWORD|SECRET|DSN|KEY|USER_AGENT`);
- `additionalProperties: false` on the service blocks, so a typo like `api.replica=2`
  fails instead of being silently ignored;
- `image.pullPolicy` must be one of the three real values.

### Releases: install, upgrade, history and rollback

A **release** is one installation of a chart, here `wikipulse` in the namespace
`wikipulse`. Every `helm install`, `upgrade` and `rollback` creates a new **revision**:
Helm stores the rendered manifests and the values of each revision as a Secret in the
namespace (`sh.helm.release.v1.wikipulse.v3`). `helm history` lists them.

- `helm upgrade --install` installs the first time and upgrades afterwards, so
  `cluster.py deploy` is the same command for both.
- On an upgrade, Helm compares the new manifests with the current objects and changes
  only what differs (Helm 4 uses server-side apply for this). A Deployment whose pod
  template changed rolls out new pods; one whose `replicas` changed only scales.
- `--wait` waits until the Deployments are ready (or fails after `--timeout`). Without
  it Helm reports success as soon as the objects are accepted, even if the pods then
  crash.
- `helm rollback wikipulse 1` re-applies revision 1's stored manifests **as a new
  revision** (history is never rewritten: a rollback after revision 2 is revision 3,
  with the description "Rollback to 1").

### Hooks: the migrations

The migration Job (`templates/migrate-job.yaml`) is a **hook**:
`helm.sh/hook: pre-install,pre-upgrade`. Helm creates it before the release's other
objects, waits for it to complete, and aborts the install or upgrade if it fails. That
replaces 08a's separate `cluster.py migrate` step, with a stronger guarantee: new
services never start against an old schema. Details:

- `hook-delete-policy: before-hook-creation` deletes the previous Job right before the
  next one is created (a finished Job can't be changed), and keeps the latest for
  `kubectl logs job/migrate`.
- On the first install the hook runs before *any* of the release's objects exist. So
  the Job gets the settings inline as `env:` entries (rendered from the same
  `.Values.config`) instead of from the ConfigMap, and it connects to the database host
  directly (`python -m wikipulse.db.migrate --host host.docker.internal`) instead of
  through the Service `postgres`, a DNS alias that doesn't exist yet either. The first
  install failed exactly there (see the deviations).
- A rollback runs no `pre-upgrade` hook: the migrations only go forward, and an older
  revision's code keeps working on the newer schema (the project's migrations only add).

### Chart quality: lint and template tests

- `helm lint --strict` checks the chart's structure, renders it, validates the values
  against the schema and turns warnings into errors. CI runs it for the defaults, dev,
  prod, and both with the offline overlay.
- `tests/test_chart.py` renders the chart with `helm template` for every environment and
  checks the rendered objects: every pod non-root with a read-only filesystem and no
  service links, requests within limits, three probes everywhere, workers singletons
  with Recreate, a deny-all network policy plus one egress policy per pod, the exact
  egress ports, no Secret in the chart, migrations as a hook with the ConfigMap's
  settings, a config change changing the checksum, selectors matching their pods, prod
  stricter than dev, and offline running nothing that calls Wikimedia. The schema tests
  try six bad values and expect a refusal. These are the 08a manifest tests, now run
  against what Helm actually renders.

## Design decisions and deviations

- **The raw manifests are deleted, not kept next to the chart.** Two sources of truth
  would drift. `k8s/kind-config.yaml` stays (it describes the cluster, not the
  application), and `secret.example.yaml` moved into the chart folder, excluded from the
  packaged chart by `.helmignore`. The 08a manifest tests (`tests/test_k8s.py`) now run
  against the rendered chart (`tests/test_chart.py`), for every environment; the
  `cluster.py` tests moved to `tests/test_cluster.py`. The two `with_image_tag()` tests
  went with the function: Helm sets the tag (`--set image.tag=...`), and
  `test_the_image_tag_reaches_every_container` checks the same thing.
- **The namespace and the Secret stay outside the chart**, created by `cluster.py`
  (`namespace_manifest()`, `secret_manifest()`), both in memory. Helm keeps its release
  records *in* the namespace, so the namespace must exist first (and carry the Pod
  Security label, which `--create-namespace` wouldn't set). A chart in git must never
  hold secret values, so the pods read the Secret by name (`existingSecret`).
- **Migrations: a hook that connects to the database host directly.** The first
  install failed: `failed pre-install: resource Job/wikipulse/migrate not ready`, with
  `failed to resolve host 'postgres'` in the Job's log. A pre-install hook runs before
  *all* of the release's objects, the ExternalName Service `postgres` included. Two
  alternatives were rejected: making the Services hooks as well (Helm recreates hook
  objects on every upgrade and never tracks or deletes them), and running migrations as
  an init container of every Postgres pod (a failed migration would then only show up
  after the Recreate workers were already stopped). Instead `wikipulse.db.migrate` got
  a small `--host` option (`with_host()`, using psycopg's `make_conninfo`: same user,
  password and database, another host), and the chart passes
  `--host {{ .Values.migrations.databaseHost }}` (`host.docker.internal`). The Job also
  gets its settings inline instead of from the ConfigMap, for the same reason.
- **A Dockerfile bug from 08a, found here.** After the `--host` fix the Job still said
  `unrecognized arguments: --host`: the new image held the *old* `migrate.py`. The
  Dockerfile's last step runs `uv sync` with a cache mount, and uv reuses the wheel it
  built for a local project until `pyproject.toml` changes; the version stayed 0.1.0, so
  code-only changes were silently not installed (the build log said "Installed 1
  package" without "Built"). `--reinstall-package wikipulse` makes uv build it from the
  copied source every time. Images built after 08a's first build may have run stale
  code.
- **Prod's "more replicas" means the API only.** The workers can't scale (08a), and the
  schema now enforces it. Prod gets two API replicas, a PodDisruptionBudget
  (`minAvailable: 1`, so draining a node never takes both down) and stricter limits:
  every container's CPU and memory ceiling is lower than in dev, set above the usage
  measured on the cluster (see Results). Requests stay as in dev, except the API's and
  the grouper's, raised closer to what they really use, so the scheduler reserves it.
- **`values-offline.yaml` and the placeholder contact.** `.env` still has the
  placeholder contact, and Wikimedia requires a real one. The acceptance run doesn't
  need Wikimedia, so it ran with `--allow-placeholder-contact`, which now *forces* the
  offline overlay in `cluster.py` (`check_contact()` returns True, `deploy_command()`
  adds `values-offline.yaml`): the consumer, the enricher and the graph runner (it links
  names through Wikipedia and Wikidata) had 0 replicas in every revision. In 08a the
  opt-out only promised "don't let the consumer connect"; now it can't. The API only
  builds Wikipedia links as text, and the grouper and the API reach Hugging Face (not
  Wikimedia) for the embedding model.
- **`cluster.py` commands changed**: `migrate` and `apply` are gone (the hook and
  `deploy` replace them), `deploy`, `history` and `rollback` are new, and `deploy` takes
  `--values`, `--set`, `--offline`. `up` works as before.
- **Additions the spec didn't ask for**: fixed object names instead of
  `<release>-<name>` (see "Templating"), the checksum annotation, the `keep` policy on
  the model volume, the values schema and the disruption budget, each explained above.
- **Tools**: Helm 4.3.1 was already installed through winget (`Helm.Helm`); nothing was
  installed. CI pins the same version with `azure/setup-helm@v4`. Helm 4 differs from
  the Helm 3 most tutorials show in two visible ways: `--wait` takes a strategy
  (`--wait` alone means `watcher`), and `--atomic` became `--rollback-on-failure`.

## Results

Run on 2026-11-10 on a fresh kind cluster (Kubernetes 1.37), image `wikipulse:fe87eff`,
Docker Compose services running on the host. No pod contacted Wikimedia: every revision
had the offline overlay, and the consumer, enricher and graph ReplicaSets never had a
pod (their pod events: 0).

1. **Install** (`cluster.py up --allow-placeholder-contact`, then after the two fixes
   above `cluster.py deploy --allow-placeholder-contact`): the migration hook ran
   (`Database is up to date; nothing to apply.`), then all Deployments were ready in
   about 40 s: api, archiver, detector, grouper `1/1`; consumer, enricher, graph `0/0`.
2. **Upgrade changing replicas** (`cluster.py deploy --values values-prod.yaml
   --allow-placeholder-contact`): the hook ran again (pre-upgrade), the API went from 1
   to 2 replicas with limits `cpu 500m, memory 1Gi` (dev: `1, 1536Mi`), the
   PodDisruptionBudget `api` appeared, and the other workers were replaced (their limits
   changed). About 60 s.
3. **Rollback** (`cluster.py rollback`): back to revision 1's manifests in 17 s: the API
   at 1 replica with `cpu 1, memory 1536Mi`, the budget deleted. The pods got the same
   template hash as in revision 1 (`api-9dff9cdb9-...`): Kubernetes scaled the old
   ReplicaSet back up instead of building a new one.

`helm history wikipulse` at the end:

```
REVISION  UPDATED                   STATUS      CHART            APP VERSION  DESCRIPTION
1         Tue Nov 10 21:30:29 2026  superseded  wikipulse-0.1.0  0.1.0        Install complete
2         Tue Nov 10 21:31:38 2026  superseded  wikipulse-0.1.0  0.1.0        Upgrade complete
3         Tue Nov 10 21:32:18 2026  deployed    wikipulse-0.1.0  0.1.0        Rollback to 1
```

The release records are the Secrets `sh.helm.release.v1.wikipulse.v1` to `v3`. An
upgrade with `--set consumer.replicas=2` was refused by the schema
(`at '/consumer/replicas': maximum: got 2, want 1`) and created no revision. (The two
failed first installs were uninstalled before this run, so the history starts clean.)

**Memory measured** in the pods (cgroup `memory.current` / `memory.peak`, revision 1,
shortly after start): grouper 632 / 699 Mi (embedding model loaded), api 147 / 153 Mi
(it loads the model on the first question, about 500 MB more), archiver 151 / 189 Mi,
detector 42 / 67 Mi. The kind node used 1.6 GB. The prod limits leave room above these
(grouper 1 Gi, api 1 Gi, archiver and detector 768 Mi). The consumer, enricher and
graph runner didn't run, so their prod limits are based on 08a's numbers only; check
them in the next live run.

**Lint and template tests**: `helm lint --strict` passes for the defaults, dev, prod and
both with the offline overlay (the same commands as the CI step). `tests/test_chart.py`
renders every environment and tries six refused values; `tests/test_cluster.py` covers
the script; `tests/db/test_migrate.py` got a `with_host()` test.
`uv run pytest`: 1110 passed, 1 skipped (an older test that needs a running snapshot server; the chart tests run whenever helm is installed). `ruff check` and `ruff format --check` are clean.

**Cluster afterwards**: deleted (`scripts/cluster.py down`).

## Questions to test your understanding

1. You run `cluster.py deploy --values values-prod.yaml`, then `cluster.py rollback`.
   Which revision number does the rollback create, and does the migration Job run again?
2. With `--values values-prod.yaml --set api.resources.limits.cpu=2`, what are the
   API's CPU and memory limits, and why does the memory limit survive?
3. Why did the first install fail with `failed to resolve host 'postgres'`, although
   the chart contains the Service `postgres`?

<details>
<summary>Answers</summary>

1. A new one: after revisions 1 (install) and 2 (upgrade), the rollback is revision 3,
   "Rollback to 1". Helm never rewrites history; it re-applies revision 1's stored
   manifests. The migration Job does not run: it is a `pre-install,pre-upgrade` hook, and
   a rollback only runs `pre-rollback`/`post-rollback` hooks. That's fine because
   migrations only add to the schema, which the older code still works with.
2. CPU `2` (the `--set` is the last layer) and memory `1Gi` (from `values-prod.yaml`).
   Helm merges maps key by key, so setting `limits.cpu` replaces only that key; the
   `memory` key from the earlier layer stays. (Lists, like `command` or `egressPorts`,
   would be replaced whole.)
3. The migration Job is a `pre-install` hook: Helm creates it and waits for it *before*
   creating any of the release's regular objects. On the first install the
   ExternalName Service `postgres` didn't exist yet, so cluster DNS had no such name.
   The fix: the hook connects to the database host directly
   (`wikipulse.db.migrate --host host.docker.internal`) and gets its settings inline
   instead of from the ConfigMap, which doesn't exist yet either.

</details>
