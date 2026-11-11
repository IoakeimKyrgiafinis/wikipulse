# Spec 09c: Full CI/CD pipeline

Status: done
Stage: 9. Observability and CI/CD
Owner: agent
Depends on: 08c, 09a

## Goal
A complete GitHub Actions pipeline: every pull request is tested and evaluated, and every release produces versioned images and a chart that deploy with one command.

## What you'll learn
- **CI vs CD,** with real stages: test, evaluate, build, publish, deploy.
- **Eval gates:** failing a pull request when detection quality drops, using a fixed recording, so the gate is deterministic and needs no GPU.
- **Container registries and versioning:** images tagged by commit and release version in GitHub Container Registry.
- **Testing infrastructure in CI:** a throwaway Kubernetes cluster spun up inside the pipeline.
- **Supply-chain hygiene:** automated dependency updates and image vulnerability scanning.

## Outputs
- `.github/workflows/ci.yml` (pull requests): lint, unit tests, integration tests with service containers, DAG import tests, Helm lint, Terraform validate, and the detection eval gate on a committed recording, failing if precision or recall drop more than a set margin from the stored baseline
- `.github/workflows/release.yml` (version tags): build and push images to GHCR, scan them, package and publish the Helm chart, create a GitHub Release with notes
- A smoke-test job: create a kind cluster in CI, install the chart, check every pod becomes ready
- `.github/dependabot.yml`
- `scripts/deploy.py`: deploy a given release version to the local cluster (true continuous deployment would need a self-hosted runner on your laptop; document why that's a security decision)

## Acceptance criteria
- A pull request that worsens detection fails the gate.
- Pushing a tag produces images, a chart and a release.
- The smoke-test job passes.

## Look at it yourself (optional, never blocking)
- Open a pull request with a deliberately bad threshold and watch the gate catch it.
