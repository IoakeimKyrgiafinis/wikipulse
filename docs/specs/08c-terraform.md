# Spec 08c: Infrastructure as code with Terraform

Status: done
Stage: 8. Kubernetes, Helm, Terraform
Owner: agent
Depends on: 08b

## Goal
Create the entire local infrastructure from code: cluster, namespace, secrets, storage buckets and the Helm release, so it can be destroyed and rebuilt with one command.

## What you'll learn
- **Infrastructure as code:** declaring what should exist, and letting the tool work out how.
- **Terraform's workflow:** `init`, `plan`, `apply`, `destroy`; state, and why the plan is reviewed before applying.
- **Providers:** the same Terraform skills apply to AWS by switching providers. Explain how in your devlog.

## Outputs
- `infra/` Terraform configuration using the Kubernetes and Helm providers, plus community providers for kind and MinIO (check the Terraform Registry for maintained ones)
- Variables for environment differences; outputs for useful addresses
- Local state, git-ignored, with a note on how teams use remote state

## Requirements
1. `terraform destroy` followed by `terraform apply` rebuilds a working system.
2. No secrets in Terraform files; they come from variables or the environment.

## Acceptance criteria
- `terraform validate` and `terraform fmt -check` pass (also in CI).
- A full destroy and rebuild works; report the time it took.

## Look at it yourself (optional, never blocking)
- Read a `terraform plan` output before every apply until you can predict it.
