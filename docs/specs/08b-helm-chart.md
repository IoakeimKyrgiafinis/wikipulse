# Spec 08b: Helm chart

Status: done
Stage: 8. Kubernetes, Helm, Terraform
Owner: agent
Depends on: 08a

## Goal
Package the whole deployment as a Helm chart, so it installs, upgrades and rolls back with one command and is configurable per environment.

## What you'll learn
- **Templating and values:** one chart, different settings for development and "production."
- **Releases:** install, upgrade, history and rollback.
- **Chart quality:** linting and template tests.

## Outputs
- `charts/wikipulse/` replacing the raw manifests: templates for every service, `values.yaml`, `values-dev.yaml`, `values-prod.yaml` (more replicas, stricter limits)
- `helm lint` and template-rendering tests runnable locally and in CI

## Acceptance criteria
- `helm install`, an upgrade changing replicas, and `helm rollback` all work on the kind cluster.
- Lint and template tests pass.

## Look at it yourself (optional, never blocking)
- Change a value, upgrade, roll back, and read `helm history`.
