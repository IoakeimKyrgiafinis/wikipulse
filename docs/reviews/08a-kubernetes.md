# Review: Spec 08a, Kubernetes deployment

Branch `spec-08a-kubernetes` against `main` (4 commits, e242ebc..c671ae1). I reviewed it statically: the Dockerfile, `k8s/`, `scripts/cluster.py`, `scripts/k8s_crash_test.py`, `wikipulse/health.py` and the heartbeat changes, plus the tests. I did not recreate the kind cluster. The two live criteria were judged from the commands in the code and the output recorded in `docs/learning/08a-kubernetes.md`. Afterwards `kind get clusters` showed that no cluster was left running.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| The full pipeline runs in the cluster during a live session; report `kubectl get pods` | pass (implementer evidence, not re-run) | The learning note records a 30-minute live session with `kubectl get pods`: 7 Deployments `1/1 Running` and `migrate` `Completed`. It also traces one spike through every stage: detector, enricher, grouper (`live:4`), graph (qwen3:4b) and the API through port-forward. The manifests match: `kustomization.yaml` lists all 7 Deployments and `cluster.py apply` waits for each rollout. |
| Deleting the consumer pod mid-stream loses no events (rerun the crash-test logic) | pass (implementer evidence, not re-run) | `scripts/k8s_crash_test.py` reuses `compare`/`report` from `scripts/crash_test.py`. It does a hard kill (`--force --grace-period=0`), then a graceful delete, then scales to 0. Only after that does it read the reference stream, so there is only ever one Wikimedia connection. Recorded result: 399/399, 0 missing, 0 extra, 0 duplicates, PASS. The logic is sound: `position()` reads the checkpoint and the last entry in one MULTI. |
| Requirement 1: every service has a health endpoint or command for probes | pass | The API uses `GET /health` with a `Host: localhost` header. The workers use `python -m wikipulse.health` against `HEALTH_FILE`. `tests/test_k8s.py::test_deployments_have_all_three_probes` checks this. |
| Requirement 2: killing any pod causes no data loss | pass (implementer evidence) | For the consumer this is the crash test above. The detector and archiver re-read their pending group entries, and the note records that the archiver re-read 199 entries after its lock expired. The enricher, grouper and graph poll Postgres. |
| Tests pass | pass | `uv run pytest`: 1070 passed, 1 skipped, 75 deselected |
| Ruff passes | pass | `ruff check .`: all checks passed. `ruff format --check .`: 411 files already formatted |
| Learning note and spec status | pass | `docs/learning/08a-kubernetes.md` exists and has the three questions. The spec has `Status: done`. |

## Judging the deviations

- **No 3-worker scaling exercise: accepted.** The reasoning is correct. A Redis consumer group hands out entries round-robin, but the detector keeps state per page, so three detectors would each see a third of every page's edits and miss spikes. The consumer must keep a single Wikimedia connection, and the archiver holds a lock. Running the exercise anyway would have shown wrong behaviour. The note explains how to partition the work and suggests `scale deployment/api` for a hands-on try.
- **Scorer and agent not deployed: accepted.** The spec's Context lists the services to deploy as "consumer, detector, events, graph, API", and the implementation goes further by adding the archiver and the enricher. The memory reason given for the scorer is reasonable.
- **Separate consumer checkpoint namespace: accepted, with a risk (problem 2).** Resuming from a checkpoint three days old would have pulled millions of messages from Wikimedia, so starting from "now" is the polite choice. The cost is that duplicate protection is now split between two namespaces, and nothing enforces that only one consumer runs.
- **One fat image with torch (2.9 GB, 642 MB compressed): accepted for now.** The spec requires one image for all services. The grouper and the API need torch. Splitting the image needs optional dependency groups, which is reasonably left for later. Using the CPU-only torch index and compiling bytecode at build time (needed because the root filesystem is read-only) are good choices.
- **Other deviations are good fixes found while running the live session:** `enableServiceLinks: false` (stops the `API_PORT=tcp://...` collision), the migration Job running after the base manifests, and `API_LISTEN_ALL` (which still enforces a loopback Host header).

## Problems

1. **medium**: `scripts/cluster.py:185-186`. The live session and the pod-kill test ran against Wikimedia with the placeholder User-Agent `you@example.com` (recorded in the learning note). This breaks the AGENTS.md rule that every request sends the human's contact address, and `secret()` only logs a warning instead of refusing to deploy a consumer that contacts Wikimedia.
2. **medium**: `k8s/consumer.yaml:47`. With `--namespace wiki:k8s:consumer`, both the checkpoint and the `seen:` dedup keys are separate from the laptop consumer's, yet both consumers write to the same `wiki:edits` stream. If the laptop consumer is started again, it silently re-writes events the cluster already wrote, and the detector and archiver count those edits twice. The only safeguard is a sentence in the learning note.
3. **low**: `k8s/graph.yaml:2`. The header comment still says Ollama is reached "through the `ollama` Service", but commit c671ae1 removed that Service; it now goes to `host.docker.internal`.
4. **low**: `k8s/network-policies.yaml:159-161`. `api-ingress` has no `from` selector, so any pod in any namespace can reach port 8000. The comment says no other pod connects, and the policy could say so too (the only remaining protection is the Host-header check, which a client can fake).
5. **low**: `k8s/network-policies.yaml` (all egress rules). Egress is allowed by port to any address, so for example the consumer can open HTTPS to any host on the internet. The file comment explains this, but it is a weaker policy than "limiting who talks to whom" suggests.
6. **low**: `wikipulse/health.py:119`. Each exec probe starts a full Python interpreter and, without `--file`, imports pydantic-settings and builds `Settings`. That happens every 15–30 s in each of six pods, and the probe's memory counts against tight limits (the consumer's is 256Mi). A stdlib-only path, or passing `--file` in the manifests, would be cheaper.
7. **low**: `scripts/cluster.py:90`. `newest_image_tag` sorts Docker's `CreatedAt` strings as text. This works only while every image has the same UTC offset, so across a daylight-saving change it can pick the wrong image.
8. **low**: `tests/test_k8s.py:151`. `Settings()` here reads the developer's local `.env` (other tests in the file pass `_env_file=None`). This doesn't change the result, but the unit test depends on local files.
9. **low**: `k8s/kustomization.yaml` and `scripts/cluster.py:93`. The image tag is set twice: by the kustomize `images:` block, then by a line-based text rewrite in `with_image_tag`. Using `kustomize edit set image`, or dropping one of the two, would be clearer.

Verdict: merge

## Questions for the human before merging

1. Why does running three detector replicas in the `detect:live` consumer group give *wrong* results rather than just faster ones, and what would you need to change in the pipeline to make the detector scale horizontally?
2. The cluster consumer uses its own checkpoint namespace but writes the same `wiki:edits` stream as the laptop consumer. What exactly happens to `wiki:edits` and to the spikes if you start `python -m wikipulse.stream.consume` on the laptop while the cluster is running, and why doesn't the duplicate protection catch it?
3. For a worker such as the grouper, explain what each of the startup, readiness and liveness probes does with `python -m wikipulse.health`, why the liveness `--max-age` (600 s) is larger than the readiness one (120 s), and what Kubernetes does when each one fails.
