# infra/: the local infrastructure as code (spec 08c)

Two Terraform stacks, each with its own state:

| Stack | Creates | Lifetime |
|-------|---------|----------|
| `storage/` | the buckets `wikipulse-lake` and `wikipulse-mlflow` in the S3 store (RustFS in Compose) | long: the data. `prevent_destroy` refuses `terraform destroy` |
| `cluster/` | the kind cluster, the namespace, the Secret, the image in the node, the Helm release | short: destroy and rebuild at will |

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

The state (`terraform.tfstate`) is local and git-ignored: it contains the secrets in plain
text. Explanation, decisions and results: `docs/learning/08c-terraform.md`.
