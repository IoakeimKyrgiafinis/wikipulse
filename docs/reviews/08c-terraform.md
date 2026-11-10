# Review: 08c-terraform

Branch `spec-08c-terraform` against `main` (6 commits, 18 files). I reviewed the code by reading it and ran the checks below. I did not re-run the destroy-and-rebuild, as instructed. The storage stack was not touched.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| `terraform fmt -check` passes | pass | `terraform fmt -check -recursive infra` exits 0 (Terraform 1.17.0) |
| `terraform validate` passes for each stack | pass | `init -backend=false -lockfile=readonly` then `validate` in `infra/storage` and `infra/cluster`: both "Success! The configuration is valid." |
| The same checks run in CI | pass | Job `terraform` in `.github/workflows/ci.yml:119-142`; `gh run view 42892984250`: "terraform in 13s" succeeded (the `test` and `dags` jobs were still running) |
| A full destroy and rebuild works, with its time reported | pass (not re-run) | The learning note reports destroy in 25 s and apply in 156 s (181 s total), followed by "No changes". `infra/cluster/terraform.tfstate.backup` contains the 5 resources and the current `terraform.tfstate` is empty, which matches an apply followed by the final destroy |
| Req. 2: no secrets in Terraform files | pass | Every secret variable has `sensitive = true` and no default; `tests/test_infra.py` checks this; a grep of `infra/*.tf` finds no literals |
| State, `.terraform/`, kubeconfig and tfvars are not committed | pass | `git ls-files` lists only `.tf`, `.terraform.lock.hcl` and `README.md` under `infra/`; `git status` is clean even though the state files and `.terraform/` exist locally (they are git-ignored) |
| Tests and lint | pass | `uv run pytest tests/test_infra.py`: 12 passed; `ruff check .` and `ruff format --check .` are clean |

## Judging the deviations

- **Two stacks instead of one: accepted.** The buckets hold real data, and in one stack a destroy would either delete them or fail halfway. Splitting the state by lifetime and adding `prevent_destroy` is the right design. The learning note explains it well.
- **The image is built outside Terraform: accepted.** Building is not infrastructure. `terraform_data.image` reloads the image whenever the tag or the cluster changes, and the tag is the commit hash. The only gap is that an image rebuilt under the same tag is not reloaded (problem 6).
- **The provider-unknown trap was documented, not fixed: accepted for a laptop cluster.** The note in `main.tf` and the learning note are accurate. However, the variables that trigger it (`cluster_name`, `node_image`, `kubeconfig_path`) are presented as ordinary "environment" knobs, and the README, which users actually read, doesn't mention the recovery command (problem 3).

## Problems

1. **medium**, `infra/cluster/main.tf:108-124`: `helm_release` uses a local chart path without `experiments { manifest = true }`, so if you edit a template in `charts/wikipulse` without bumping the chart version or changing a value, `terraform plan` reports "No changes" and the edit is never deployed. The learning note's "Terraform compares that with what exists" suggests otherwise.
2. **low**, `infra/cluster/main.tf:87-101`: if a secret value changes (for example a password rotation), only the Secret object is updated. The release and its pod templates stay the same, so running pods keep the old environment until someone restarts them. Neither the docs nor the code mention this.
3. **low**, `infra/cluster/variables.tf:3-19` and `infra/README.md`: changing `cluster_name`, `node_image` or `kubeconfig_path` triggers the documented plan failure ("localhost"), but these are offered as everyday variables, and the README doesn't mention the `destroy -refresh=false` recovery. Because `pathexpand` stores an absolute, user-specific path, running the same state from another machine or user would also force a replacement.
4. **low**, `infra/storage/variables.tf:25-29`: the bucket names are copied as defaults instead of being passed from settings (`Settings.s3_bucket`, `MLFLOW_BUCKET`) by `scripts/infra.py`, so the stack and the app can drift apart if `.env` changes a bucket name.
5. **low**, `infra/storage/main.tf:6` and `scripts/infra.py:51-54`: `minio_ssl = false` is hard-coded, while `s3_server()` strips the scheme from `S3_ENDPOINT`, so an `https://` endpoint would be used over plain HTTP (the test even uses an https AWS example).
6. **low**, `infra/cluster/main.tf:62-68`: `triggers_replace` only uses the tag, so an image rebuilt under the same tag (for example a build from a dirty working tree) is not reloaded into the node.
7. **low**, `scripts/infra.py:46-48`: the comment "Terraform commands that read the variables" sits above `TF_VAR` instead of `COMMANDS_WITH_VARIABLES`, and `output` does not read input variables. It is harmless but misleading.
8. **low**, `tests/test_infra.py:96-103`: the "no literal secret" test is a single-line regex (`name = "`), so it misses heredocs, other attribute names and `.tfvars` files. It is a useful guard, not a proof.

Verdict: merge

## Questions for the human before merging

1. You change `node_image` to pin a newer Kubernetes version and run `scripts/infra.py cluster plan -var offline=true`. Why does the plan fail with an error about `http://localhost`, and what exact sequence of commands gets you to the new cluster?
2. You change the `POSTGRES_DSN` password in `.env` and run `apply`. Which resource changes, and why do the running pods still use the old password afterwards?
3. Why does `terraform destroy` in `infra/storage` refuse to run, and what would happen to the data lake if the buckets were in the cluster stack with `force_destroy = true`?
