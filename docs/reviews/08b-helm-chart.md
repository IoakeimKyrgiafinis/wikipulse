# Review: 08b Helm chart

Branch `spec-08b-helm-chart` vs `main` (6 commits, 28b9c51..b09404d). Reviewed statically, by
running the tests, lint and `helm template`, and by redoing install, upgrade and rollback on a
fresh kind cluster with offline values only. The cluster was deleted afterwards.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| `helm install` works on the kind cluster | pass | `scripts/cluster.py up --allow-placeholder-contact --set detector.replicas=0 --set archiver.replicas=0 --set grouper.replicas=0` built `wikipulse:b09404d`, created the namespace and Secret, ran the `migrate` pre-install hook (`Database is up to date; nothing to apply.`), and installed revision 1. Ready: `api 1/1`. The offline overlay was applied automatically (consumer, enricher and graph at 0). I also set detector, archiver and grouper to 0 so the review did not change the shared Redis group or the lake. |
| An upgrade changing replicas works | pass | `cluster.py deploy --values values-prod.yaml --allow-placeholder-contact ...` gave revision 2. The pre-upgrade hook ran with the network policies in place, then `api 2/2` with a rolling update, and PDB `api` (minAvailable 1) was created. |
| `helm rollback` works | pass | `cluster.py rollback` gave revision 3, "Rollback to 1". Afterwards: `api 1/1`, the PDB was deleted, and the pod template hash was the same as in revision 1 (`api-69c7545cff`). The history showed `1 Install complete / 2 Upgrade complete / 3 Rollback to 1`. The only ReplicaSets that created pods were api and migrate, so nothing in the cluster contacted Wikimedia. |
| Lint and template tests pass | pass | `helm lint --strict` passed for default, dev, prod, dev+offline and prod+offline. `helm template` rendered dev and prod. `uv run pytest tests/test_chart.py tests/test_cluster.py tests/db/test_migrate.py`: 78 passed. Full `uv run pytest`: 1110 passed, 1 skipped. `ruff check` and `ruff format --check` were clean. CI installs Helm v4.3.1 and runs the same lint and template commands. |

## Judgement of the deviations

- **Namespace and Secret outside the chart:** sound. Helm stores release records in the namespace, so the namespace has to exist first. `--create-namespace` would not add the `restricted` Pod Security label. A chart in git must not hold secrets. Pods read the Secret by name through `existingSecret`. This is documented in NOTES.txt, values.yaml and secret.example.yaml.
- **Migration hook with `--host`:** sound. A pre-install hook runs before the ExternalName Service `postgres` exists. `with_host()` uses psycopg's `make_conninfo`, so it replaces only the host, and a test covers it. The alternatives the author rejected (Services as hooks, init containers) were rejected for valid reasons. The cost is a second copy of the host value (problem 1).
- **Prod "more replicas" only for the API:** correct. The workers are singletons by design (one Wikimedia connection, per-page state, the lake lock), and `values.schema.json` now enforces this with `maximum: 1`. Prod has 2 API replicas, a PDB, and lower limits for every service. A test checks that the prod limits are below the dev limits.
- **Removal of `k8s/` manifests:** correct. Keeping two sources of truth would let them drift apart. `kind-config.yaml` (which describes the cluster) stays. The manifest tests of `tests/test_k8s.py` were carried over to `tests/test_chart.py` and run against the rendered chart for every environment. Only the kustomization-listing test and the two `with_image_tag` tests were dropped, along with the code they tested. `test_the_image_tag_reaches_every_container` replaces the image-tag tests. No tests were weakened.
- **Dockerfile `--reinstall-package wikipulse`:** a justified fix outside the chart itself. Without it, the uv cache mount could install a stale wheel when only code changed. The author reproduced this (`unrecognized arguments: --host`). The change is one flag, and the learning note explains it.

## Problems

1. **low**, `charts/wikipulse/values.yaml:62`: `migrations.databaseHost` repeats `externalServices.host` (line 47), so if one changes without the other, migrations run against a different Postgres than the services use, with no warning. It could default to empty and fall back to `externalServices.host` in the template.
2. **low**, `charts/wikipulse/templates/network-policies.yaml:672`: the `migrate-egress` port 5432 is hard-coded, while every other service takes its egress ports from values (`<service>.egressPorts`).
3. **low**, `charts/wikipulse/values-dev.yaml:1-6`: the comment talks about "generous limits" and says the defaults "are written out here so the difference to values-prod.yaml is visible", but the file only sets `api.replicas: 1`.
4. **low**, `scripts/cluster.py:306`: `deploy --offline` still refuses a placeholder contact unless `--allow-placeholder-contact` is also given, even though an offline deploy is exactly the safe case. This is surprising, but it errs on the safe side.
5. **low**, `scripts/cluster.py:241-245`: `rollback` does not check the contact or the offline state. Rolling back to an online revision (consumer at 1 replica) after the Secret was re-created with the placeholder would start a consumer with the placeholder User-Agent. This is unlikely with the current workflow.
6. **low**, `charts/wikipulse/values.schema.json:882-885`: `api.podDisruptionBudget` has no `additionalProperties: false` and no check against `replicas`. A typo there is silently ignored, and enabling the PDB with `replicas: 1` and `minAvailable: 1` would block node drains.

No high or medium problems. Nothing listed as out of scope was touched (the spec has no "Out of scope" section). The changes to `wikipulse/db/migrate.py` and the Dockerfile were needed to meet the acceptance criteria, and both are recorded in the learning note. No hard-coded values belong in `wikipulse/config.py`: the chart's settings correctly live in `values.yaml`. No AGENTS.md violations were found: no network access in tests, no secrets committed, no new Python dependencies, and the learning note and spec status are present.

Verdict: merge

## Questions for the human before merging

1. The migration Job is a `pre-install,pre-upgrade` hook. On the very first install, which objects exist when it runs? Why does it need both inline `env:` entries and `--host`, and why don't the regular services need them?
2. You run `cluster.py deploy --values values-prod.yaml --set api.replicas=1`. What happens to the PodDisruptionBudget, and what would `kubectl drain` on kind's single node then do?
3. Why can't `values-prod.yaml` simply set `detector.replicas: 2` for more throughput, and which part of the chart stops you if you try?
