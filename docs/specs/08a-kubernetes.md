# Spec 08a: Kubernetes deployment

Status: ready
Stage: 8. Kubernetes, Helm, Terraform
Owner: agent
Depends on: 05e

## Goal
Run WikiPulse's services on a local Kubernetes cluster (kind), configured the way production systems are.

## What you'll learn
- **Core Kubernetes objects:** Deployments, Services, ConfigMaps, Secrets, StatefulSets, PersistentVolumes. Do the official "Kubernetes Basics" tutorial before running this spec.
- **Health and resources:** liveness and readiness probes, CPU and memory requests and limits.
- **Scaling:** run three stream workers and watch them share the load through the consumer group.
- **Container images:** building, tagging and loading images into the cluster.
- **Security basics:** non-root containers, Secrets, network policies limiting who talks to whom.

## Context
- Laptop memory is limited: deploy the application services (consumer, detector, events, graph, API) to the cluster, and point them at Redis, Postgres, Neo4j and MinIO. Those can run in the cluster too if memory allows, or stay in Docker Compose.
- Ollama stays on the host, reached from the cluster.

## Outputs
- `Dockerfile` (multi-stage, uv, non-root) building one image used by all services with different commands
- `k8s/` with manifests per service, a namespace, ConfigMap, Secret template, probes, resource limits, and network policies
- `scripts/cluster.py`: create the kind cluster, build and load the image, apply manifests, tear down

## Requirements
1. Every service exposes a health endpoint or command for probes.
2. Killing any pod causes no data loss (Kubernetes restarts it, and checkpoints resume).

## Acceptance criteria
- The full pipeline runs in the cluster during a live session; report `kubectl get pods`.
- Deleting the consumer pod mid-stream loses no events (rerun the crash-test logic).

## Look at it yourself (optional, never blocking)
- Use `kubectl logs`, `describe`, `exec` and `scale` yourself until each feels natural.
