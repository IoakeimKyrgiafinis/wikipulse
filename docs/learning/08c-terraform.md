# 08c: Infrastructure as code with Terraform

## What was built

Everything WikiPulse's Kubernetes deployment needs is now declared in Terraform files in
`infra/`, and one command creates it or deletes it again:

| Piece | File | What it does |
|-------|------|--------------|
| Storage stack | `infra/storage/` | The buckets `wikipulse-lake` and `wikipulse-mlflow` in RustFS (provider `aminueza/minio`); protected against `destroy` |
| Cluster stack | `infra/cluster/` | The kind cluster (`tehcyx/kind`), the namespace and Secret (`hashicorp/kubernetes`), the image loaded into the node (`terraform_data` + `kind load`), the Helm release of `charts/wikipulse` (`hashicorp/helm`) |
| Variables | `infra/*/variables.tf` | Environment differences (`environment`, `offline`, `cluster_name`, `node_image`, `image_tag`, ...) and the secrets (no defaults) |
| Outputs | `infra/*/outputs.tf` | kube context, API server, namespace, release revision and status, the API's port-forward command; the S3 endpoint and buckets |
| Lock files | `infra/*/.terraform.lock.hcl` | The exact provider versions and their checksums (Windows, Linux, macOS), committed |
| Wrapper | `scripts/infra.py` | Runs `terraform` on a stack with your settings as `TF_VAR_*` environment variables |
| Tests | `tests/test_infra.py` | The wrapper's logic, no secret defaults or literals in `.tf` files, state git-ignored, `terraform fmt` |
| CI | `.github/workflows/ci.yml`, job `terraform` | `hashicorp/setup-terraform@v4`, then `fmt -check` and `init -backend=false` + `validate` for both stacks |
| Guide | `infra/README.md` | The commands |

How the pieces fit:

```
 scripts/infra.py cluster apply -var offline=true
   │  .env / config.py ──► TF_VAR_postgres_dsn, TF_VAR_neo4j_password, TF_VAR_s3_*,
   │                       TF_VAR_user_agent, TF_VAR_image_tag (newest wikipulse image)
   ▼
 terraform (infra/cluster)                      state: infra/cluster/terraform.tfstate
   kind_cluster.this ──────────────► kind cluster "wikipulse" (Docker container)
     │ endpoint + certificates
     ├─► provider "kubernetes" ─► kubernetes_namespace_v1.wikipulse (Pod Security label)
     │                            kubernetes_secret_v1.wikipulse   (from the variables)
     ├─► terraform_data.image ──► local-exec: kind load docker-image wikipulse:<tag>
     └─► provider "helm" ───────► helm_release.wikipulse = charts/wikipulse
                                   + values-<environment>.yaml (+ values-offline.yaml)
                                   + image.tag; migration hook, then waits for the pods

 terraform (infra/storage)                      state: infra/storage/terraform.tfstate
   minio_s3_bucket.this["wikipulse-lake"], ["wikipulse-mlflow"] ──► RustFS localhost:9000
```

Terraform works out the order from the references: the namespace refers to nothing, but
its *provider* is configured from `kind_cluster.this`, so the cluster comes first; the
Secret refers to the namespace; the release names the namespace and `depends_on` the
Secret and the image. `destroy` goes the opposite way.

## Key concepts, with this project's code

### Infrastructure as code: say *what*, not *how*

Before this spec the cluster was built by `scripts/cluster.py up`: a list of steps
(create the cluster, load the image, apply the namespace, apply the Secret, run helm).
A script like that says *how*; run it twice and it has to check every step itself
(`cluster_exists()`, `kubectl apply`, `helm upgrade --install`).

The Terraform files say *what* should exist: a kind cluster named `wikipulse`, a namespace
with these labels, a Secret with these keys, a release of this chart with these values.
Terraform compares that with what exists and works out the steps. Run `apply` again and
nothing happens ("No changes"); change `environment = "prod"` and it upgrades only the
release; delete the cluster by hand and the next plan says "create" again.

What Terraform compares is limited to what each provider stores, though. The Helm
provider stores the release's *inputs* (chart version, values), not the rendered
templates, so an edited template in `charts/wikipulse` with the same chart version
looked like "No changes". And a changed password updates the Secret, but the pods read
it only at start. Both are closed with checksums (see "Review fixes" below).

A **resource** block (`resource "kubernetes_namespace_v1" "wikipulse" { ... }`) is one
thing that should exist. Its address, `kubernetes_namespace_v1.wikipulse`, is how
Terraform tracks it. `for_each` makes one resource per element:
`minio_s3_bucket.this["wikipulse-lake"]` and `["wikipulse-mlflow"]` in
`infra/storage/main.tf`. **Variables** (`var.environment`) are the inputs, **outputs**
(`kube_context`, `release`, `api_port_forward`) are what a run reports back, and
**locals** are named expressions (`local.values_files` picks the values files).

### The workflow: init, plan, apply, destroy

- `terraform init` downloads the providers named in `versions.tf` into `.terraform/`
  and records their versions and checksums in `.terraform.lock.hcl`. The lock file is
  committed (so CI and every laptop use exactly the same provider builds); `.terraform/`
  is not. `terraform providers lock -platform=...` added the Linux and macOS checksums,
  so CI on Linux verifies against the same file.
- `terraform plan` reads the state, asks the providers what really exists ("refresh"),
  and prints the difference: `+` create, `~` update in place, `-/+` destroy and
  re-create, `-` destroy, ending in `Plan: 1 to add, 1 to change, 0 to destroy`.
  For example, changing the image tag from `fe87eff` to `b6f3e64` on a running cluster
  planned `-/+ terraform_data.image` (its `triggers_replace` lists the tag, so a new tag
  loads the new image) and `~ helm_release.wikipulse` with
  `~ value = "fe87eff" -> "b6f3e64"`: a Helm upgrade, nothing else.
- `terraform apply` makes the same plan again, shows it, and asks before doing it
  (`-auto-approve` skips the question; only use it once you've read the plan).
- `terraform destroy` plans the deletion of everything in the state, in reverse
  dependency order, and asks too.

**Why the plan is reviewed before applying.** The plan is the only moment you see what
will happen before it happens. A `-/+` where you expected a `~` means data loss for a
database or a bucket; a "1 to destroy" you didn't expect means something was renamed.
In teams the plan is posted to the pull request and reviewed like code, and
`terraform plan -out=x.tfplan` followed by `terraform apply x.tfplan` applies exactly
the plan that was reviewed, nothing that changed in between (`*.tfplan` is git-ignored:
a saved plan contains the variables, secrets included).

### State

`terraform.tfstate` (one per stack) is Terraform's memory: which real object belongs to
which resource address, with all its attributes. Without it Terraform couldn't know that
the namespace `wikipulse` is "its" namespace, and would try to create it again.

- **It holds secrets in plain text.** The Secret's values, the kind cluster's client key
  and the S3 keys are all in it, `sensitive = true` or not (that only hides them in
  plan output). That is why the state is git-ignored (`*.tfstate`, `*.tfstate.*`) and
  must be treated like `.env`.
- **Local state** (no `backend` block) is right for one person on one laptop.
- **How teams use remote state.** With several people or a CI pipeline, the state lives
  in a shared **backend**: an S3 bucket (versioned, encrypted, with a lock file),
  Azure Blob Storage, Google Cloud Storage, or HCP Terraform. A backend gives three
  things a local file can't: everyone works on the *same* state; a **lock**, so two
  `apply`s can't run at the same time and corrupt it; and history (versioning) to
  recover a broken state. It would be one block in `versions.tf`:

  ```hcl
  backend "s3" {
    bucket       = "wikipulse-tfstate"
    key          = "cluster/terraform.tfstate"
    region       = "eu-central-1"
    use_lockfile = true
  }
  ```

  Credentials for the backend come from the environment, never from the block.
  `terraform init -migrate-state` moves an existing local state into it.
- **Importing.** The buckets existed before Terraform (the app and MLflow create theirs).
  `terraform import 'minio_s3_bucket.this["wikipulse-lake"]' wikipulse-lake` wrote them
  into the state without creating anything; the next plan said "No changes", which
  proves the configuration matches reality.

### Providers, and how the same skills apply to AWS

A **provider** is a plugin that translates resources into API calls. Terraform's core
(plan, state, dependency graph, variables) knows nothing about kind, Kubernetes or S3;
the providers do:

| Provider | Talks to | Resources used here |
|----------|----------|---------------------|
| `tehcyx/kind` (community) | the kind library, i.e. Docker | `kind_cluster` |
| `hashicorp/kubernetes` | the cluster's API server | `kubernetes_namespace_v1`, `kubernetes_secret_v1` |
| `hashicorp/helm` | Helm's library, then the API server | `helm_release` |
| `aminueza/minio` (community) | the S3 API (and MinIO's admin API) | `minio_s3_bucket` |
| built in (`terraform`) | nothing | `terraform_data` |

Providers are configured in `provider` blocks. Here the Kubernetes and Helm providers
get the address and certificates from `kind_cluster.this`: the cluster is created first,
then the providers connect to it.

**Moving to AWS changes the providers and resources, not the workflow.** The same
`init`, `plan`, `apply`, `destroy`, the same variables, outputs, state and lock file:

| Here (laptop) | On AWS | Provider |
|---------------|--------|----------|
| `kind_cluster` | `aws_eks_cluster` + `aws_eks_node_group` (or the `terraform-aws-modules/eks` module), plus a VPC (`aws_vpc`, subnets) | `hashicorp/aws` |
| `minio_s3_bucket` on RustFS | `aws_s3_bucket` (+ `aws_s3_bucket_versioning`, encryption) | `hashicorp/aws` |
| `kind load docker-image` | push to an `aws_ecr_repository`; the nodes pull from it | `hashicorp/aws` |
| Compose's Postgres and Redis | `aws_db_instance` (RDS), `aws_elasticache_replication_group` | `hashicorp/aws` |
| namespace, Secret, Helm release | unchanged: the same `kubernetes` and `helm` resources, configured from `aws_eks_cluster`'s endpoint and a token (`data "aws_eks_cluster_auth"`) | `kubernetes`, `helm` |
| Secret values from `TF_VAR_*` | AWS Secrets Manager (`aws_secretsmanager_secret`), or External Secrets in the cluster | `hashicorp/aws` |
| local state | `backend "s3"` | |

The `aws` provider block takes a `region`; its credentials come from the environment
(`AWS_PROFILE`, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, or a CI role through
OIDC), never from the files, just like the secrets here. The application needs only
settings: `S3_ENDPOINT=https://s3.<region>.amazonaws.com` (`wikipulse/config.py`
already says so) and the database addresses in the ConfigMap and the Secret.

### Secrets: variables and the environment only

Every secret is a variable with `sensitive = true` and **no default**
(`infra/cluster/variables.tf`: `postgres_dsn`, `neo4j_password`, `s3_access_key`,
`s3_secret_key`, `user_agent`; `infra/storage/variables.tf`: the two S3 keys).
Terraform reads `TF_VAR_<name>` environment variables, so `scripts/infra.py` builds them
from your settings (`stack_variables()`, `terraform_env()`) for the one Terraform
process it starts: nothing is written to a file or put on a command line (where other
processes could read it). `tests/test_infra.py` checks that no secret variable has a
default and no `.tf` file assigns a literal password, key, DSN or user.

## Design decisions and deviations

- **Two stacks with separate state, not one.** The spec lists the buckets with the
  cluster, but the buckets hold the data lake (417 objects) and MLflow's models (454),
  and `terraform destroy` followed by `apply` must not delete them. In one stack the
  destroy would either delete the data (`force_destroy = true`) or fail halfway
  (S3 refuses to delete a non-empty bucket), leaving a half-destroyed cluster. So
  `infra/storage` holds the buckets with `prevent_destroy` (a destroy there fails
  before touching anything: "Resource ... has lifecycle.prevent_destroy set"), and
  `infra/cluster` holds everything that can be rebuilt. Splitting state by lifetime
  ("blast radius") is what teams do too: network and databases in one state, clusters
  and apps in others. The cluster stack doesn't read the storage stack's outputs: the
  bucket names are the app's settings (`S3_BUCKET`, `MLFLOW_BUCKET`), so there was
  nothing to pass.
- **`aminueza/minio` against RustFS.** The most used maintained S3-storage provider
  (3.46.3, released the day of this run). Only `minio_s3_bucket` is used, which is plain
  S3 (create, head, delete bucket), so RustFS's S3 compatibility is enough: import,
  refresh and plan worked against it. The MinIO-only admin resources (users, policies)
  are not used. The alternative, `hashicorp/aws` with a custom endpoint, works too but
  needs several `skip_*` settings to stop it calling AWS's account and region APIs,
  which is more confusing to learn from.
- **`tehcyx/kind` for the cluster** (0.13, maintained). Its `kind_config` block repeats
  `k8s/kind-config.yaml` (one control-plane node); both stay, since `cluster.py` still
  uses the YAML file. kind adds the context `kind-wikipulse` to your kubeconfig, so
  `kubectl`, `helm` and `cluster.py status/history/rollback` work on the cluster
  Terraform made.
- **Terraform doesn't build the image.** Building is not infrastructure, and Terraform
  would have to rebuild on every plan to know whether the code changed. `cluster.py
  build` builds; the cluster stack only loads the tag it is given (`terraform_data.image`
  with a `local-exec` running `kind load docker-image`, re-run when the tag or the
  cluster changes). `kind load` is the same command on Windows (`cmd /C`) and Linux
  (`/bin/sh -c`).
- **The `offline` variable.** `.env` still has the placeholder contact, so the
  acceptance runs used `offline = true`: `values-offline.yaml` is added and the
  consumer, enricher and graph runner have 0 replicas; no pod contacted Wikimedia. The
  default is `false`, as for a real deployment, and a **validation rule** on
  `user_agent` refuses the placeholder unless `offline` is true (the rule of
  `cluster.py check_contact()`, now enforced by Terraform itself before anything
  happens): `Error: Invalid value for variable ... user_agent still holds the
  placeholder contact`.
- **`environment` selects the values file** (`dev` or `prod`, validated): the
  "variables for environment differences" of the spec. Other variables: `cluster_name`,
  `node_image` (pins a Kubernetes version), `namespace`, `image_tag`, `helm_timeout`.
- **The wrapper `scripts/infra.py`** wasn't asked for. Without it you would export five
  `TF_VAR_` variables by hand, copying passwords from `.env` into your shell history.
  It also picks the image tag (the current commit's image if built, else the newest).
  It found a Windows-only trap: Python upper-cases environment names on Windows, so a
  `TF_VAR_image_tag` you set yourself reached Terraform as `TF_VAR_IMAGE_TAG`, which
  Terraform (case-sensitive) ignores. The first apply deployed the wrong tag because of
  it; `terraform_env()` now lower-cases the names after `TF_VAR_`.
- **CI**: a separate `terraform` job: `hashicorp/setup-terraform@v4` (Terraform 1.17.0,
  the version used here; `terraform_wrapper: false` so the output is plain),
  `terraform fmt -check -recursive -diff infra`, and for each stack
  `init -backend=false -lockfile=readonly` (downloads the locked providers, fails if the
  lock file doesn't match) and `validate`. Nothing is planned in CI: that would need a
  cluster and the secrets.
- **Providers configured from a resource: one known trap, documented, not designed
  away.** The Kubernetes and Helm providers take their address and certificates from
  `kind_cluster.this`. That works as long as the cluster is created or kept. A change
  that *replaces* it (the provider marks `name`, `node_image`, `kubeconfig_path` and
  `kind_config` as "forces replacement") makes those values unknown during the plan,
  the Kubernetes provider falls back to its default `http://localhost`, and the plan
  fails: `Failed to read namespace "wikipulse": Get "http://localhost/api/v1/..."`.
  It happened here when `kubeconfig_path` was added (see below): even `terraform
  destroy` failed, because destroy first refreshes with a normal plan. The fix:
  `terraform destroy -refresh=false` (the destroy plan uses the stored values, which
  are known), then `apply`. HashiCorp's guidance for clusters that change is to keep
  the cluster and the things inside it in separate stacks; for a laptop cluster that
  is rebuilt in under three minutes, one stack and one command were judged simpler,
  and the rule is written next to the provider blocks in `infra/cluster/main.tf`.
- **`kubeconfig_path` is set.** Without it the kind provider wrote a second kubeconfig,
  `infra/cluster/wikipulse-config`, with the cluster's client key, into the working
  folder (kind merges the context into `~/.kube/config` as well). Now the provider
  writes only into `~/.kube/config` (variable `kubeconfig_path`), and the pattern
  `infra/cluster/*-config` is git-ignored as a safety net.
- **Tools**: Terraform 1.17.0 was already installed through winget
  (`Hashicorp.Terraform`); nothing was installed for this spec.

### Review fixes (docs/reviews/08c-terraform.md)

- **Template edits are deployed (problem 1).** `chart_checksum()` in `scripts/infra.py`
  hashes every file of `charts/wikipulse` (relative paths and contents, line endings
  normalized) and passes it as the variable `chart_checksum`; `helm_release` sets it as
  the value `checksums.chart`. No template reads that value: it only changes the
  release's inputs, so `apply` runs `helm upgrade`, which renders the edited templates and
  changes only the objects whose rendered form changed. Chosen over the provider's
  `experiments { manifest = true }`, which renders the chart during every plan and stores
  the full manifest in the state; the checksum is small and testable without a cluster
  (`tests/test_infra.py::test_a_template_edit_changes_the_chart_checksum`). The cost:
  plain `terraform` must be given `chart_checksum` too (it has no default, like
  `image_tag`), and any file change in the chart, even a comment, makes a new release
  revision (without restarting pods whose objects didn't change).
- **A changed secret restarts the pods (problem 2).** The Secret's values live in
  `local.secret_data`; the release gets `sha256(jsonencode(local.secret_data))` as
  `checksums.secret` (through `set_sensitive`, so not even the hash shows in the plan).
  The chart puts it on every Deployment's pod template as the annotation
  `checksum/secret` (`templates/_deployment.tpl`), the same trick as `checksum/config`,
  so a new value rolls the pods. `scripts/cluster.py` doesn't set it; the annotation is
  then left out. The migration Job needs nothing: a hook runs anew on every upgrade.
- **The replacement trap is marked (problem 3).** `cluster/variables.tf` flags
  `cluster_name`, `node_image` and `kubeconfig_path`, and `infra/README.md` has a
  "Replacing the cluster" section with the `destroy -refresh=false` recovery, including
  the case of the expanded `kubeconfig_path` from another user or machine.
- **`s3_ssl` (problem 5).** The storage stack's provider uses `var.s3_ssl`;
  `scripts/infra.py` sets it from `S3_ENDPOINT`'s scheme (`https` means true).
- **Problem 7.** The comment now sits above `COMMANDS_WITH_VARIABLES`, and `output`
  (which reads no input variables) is no longer in it.
- Not changed: problem 4 (bucket names as defaults), 6 (same-tag image rebuilds) and 8
  (the literal-secret test is a guard, not a proof) were not in the fix list.
- Verified with `terraform fmt -check`, `validate` for both stacks, the full test suite,
  ruff, `storage plan` ("No changes" with `s3_ssl = false`) and a `cluster plan`
  (no cluster running: 5 to add, with both checksums in the release). No apply or
  rebuild was run for these fixes.

## Results

Run on 2026-11-10 with Terraform 1.17.0 on Windows 11, Docker Desktop, the Compose
services running, image `wikipulse:b6f3e64`, `offline = true` (the placeholder contact
is still in `.env`). In every run the consumer, enricher and graph Deployments had
`0/0` replicas and never a pod, so no pod contacted Wikimedia.

**Destroy and rebuild** (the acceptance run, on the final configuration):

| Step | Time |
|------|------|
| `terraform destroy`: release, image record, Secret, namespace (12 s, it waits for the namespace to be gone), cluster (6 s) | 25 s |
| `terraform apply`: kind cluster (49 s), namespace and Secret (0 s), `kind load` of the image (1 min 10 s), Helm release with the migration hook and `--wait` (33 s) | 156 s |
| **Total, destroy + rebuild** | **181 s (about 3 minutes)** |

After the apply: `Apply complete! Resources: 5 added`; api, archiver, detector and
grouper `1/1` with `wikipulse:b6f3e64`, consumer, enricher and graph `0/0`; the
migration Job `Completed`; `helm history`: revision 1, "Install complete"; through
`kubectl port-forward svc/api 8000:8000`, `GET /health` answered 200 with
`"postgres":true,"neo4j":true`. A second `terraform plan` said **"No changes. Your
infrastructure matches the configuration."**: the apply is repeatable.

Earlier runs gave the same picture: apply 159 s and 160 s, destroy 25 s and 22 s. The
largest step is `kind load` (copying the 2.9 GB image into the node); a local registry
would remove it. The model volume goes with the cluster, so every rebuild downloads the
embedding model again; the grouper was still ready within the release's 33 s, and Helm's
`--wait` (`wait = true`) only reports success once every Deployment is ready.

**Storage stack**: both buckets imported, then `plan`: "No changes". `plan -destroy`
was refused by `prevent_destroy` for both buckets, as intended; nothing was deleted.

**Variables**: `plan` without `offline = true` was refused by the validation rule on
`user_agent` (placeholder contact) before anything was created.

**Checks**: `terraform fmt -check -recursive infra` and `terraform validate` for both
stacks pass locally, and the same commands are the CI job `terraform`.
`uv run pytest`: 1122 passed, 1 skipped (`tests/test_infra.py` adds 12 tests).
`ruff check` and `ruff format --check` are clean.

**Cluster afterwards**: deleted with `terraform destroy`; `kind get clusters` lists none
and the context `kind-wikipulse` is gone from `~/.kube/config`. The buckets and the
data in Compose are untouched.

## Questions to test your understanding

1. You change `environment` from `dev` to `prod` and run `terraform plan`. Which
   resources does the plan touch, and with which symbol?
2. Someone commits `infra/cluster/terraform.tfstate` "so the team has it". Name two
   problems, and what to do instead.
3. Why are the buckets in a separate stack, and what happens if you run
   `terraform destroy` in `infra/storage`?

<details>
<summary>Answers</summary>

1. Only `helm_release.wikipulse`, with `~` (update in place): its `values` list now holds
   `values-prod.yaml` instead of `values-dev.yaml`. Terraform runs a Helm upgrade (a new
   revision: two API replicas, the disruption budget, stricter limits). The cluster,
   namespace, Secret and image are unchanged because nothing they depend on changed.
2. The state holds every secret in plain text (the Secret's values, the cluster's
   client key, the S3 keys), so committing it leaks them; and a file in git has no
   lock, so two people applying at once would overwrite each other's state (and a merge
   of two states is not a thing). Instead: a remote backend (`backend "s3"` with
   `use_lockfile = true`, or HCP Terraform), which shares one locked, versioned and
   encrypted state.
3. They hold data (the lake, MLflow's models) with a different lifetime than the
   cluster: the cluster stack is destroyed and rebuilt at will, and a destroy must
   never delete the data. In one stack a destroy would either delete the buckets or
   fail halfway on a non-empty bucket. `terraform destroy` in `infra/storage` fails at
   plan time ("Instance cannot be destroyed": `prevent_destroy`) before deleting
   anything; to really delete a bucket you remove that line on purpose first.

</details>
