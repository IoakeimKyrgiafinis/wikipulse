# Review: 09c-cicd-pipeline

Branch `spec-09c-cicd-pipeline` (7 commits, 5dcd0bf..650e54b) compared with `main`. Reviewed on 2026-11-11.

## What I ran

- `uv run python -m wikipulse.eval.gate` passed with precision 0.526, recall 0.211, 19 spikes, 8 of 38 news bursts, and no unlabeled spikes.
- `uv run python -m wikipulse.eval.gate --set min_edits=3` failed with `precision dropped from 0.526 to 0.324`.
- `uv run pytest tests/eval/test_gate.py tests/test_cicd.py -q`: 45 passed.
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- `gh run view 42939140084` (branch push) passed `test`, `terraform`, `dags` and `eval-gate`. `smoke` was skipped, as designed for branch pushes.
- `gh pr view 1` (closed demo PR, `detect_min_edits` 5 to 3) failed `eval-gate` in both runs (42939157911 push and 42939176309 pull_request). `test` also failed there.
- Run 42939176309: its `smoke` job (the first `smoke` run on GitHub ever) was still `in_progress` at 01:36 UTC, 8 minutes after it started. The image build step had succeeded; the smoke-test step had not finished.
- I did not run the smoke test or `deploy.py` locally. Both write to the Compose services and the user's real data, and the learning note already records a local smoke PASS twice.

## Acceptance criteria

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | A pull request that worsens detection fails the gate | pass | PR #1 (`detect_min_edits` 5 to 3): `eval-gate` FAILURE on GitHub in runs 42939157911 and 42939176309, precision 0.324 against a 0.526 baseline. I reproduced it locally with `--set min_edits=3` (exit 1, same numbers). |
| 2 | Pushing a tag produces images, a chart and a release | not verified (fail for now) | No tag has been pushed, so `release.yml` has never run. The workflow reads correctly (version, then ci, then images, chart, release), but nothing has been observed. It must be checked with `v0.1.0-rc.1` after merge. |
| 3 | The smoke-test job passes | not verified on GitHub (fail for now) | The job is skipped on branch pushes. Its only GitHub run (PR #1, run 42939176309) was still in progress when this review was written. The only evidence of a pass is the learning note's local run on Windows/Docker Desktop. It must pass on a Linux runner before the tag is pushed. |

## Problems

1. **high**, `scripts/deploy.py:105-110,124`: the release-deploy script goes online by default whenever `.env` has a real contact, and the `--allow-placeholder-contact` help text says it "implies --offline", which is false when the contact is real. That wrong belief is exactly what started the full live pipeline during testing: a stream connection, 722 edits written to `wiki:edits`, Neo4j and `graph_extractions` writes, and 6 Wikimedia API calls. Offline should be the default, with an explicit `--online` opt-in that also requires a non-placeholder contact.
2. **medium**, `scripts/deploy.py` / `tests/test_cicd.py:343`: no test covers the dangerous combination (real contact plus `--allow-placeholder-contact` without `--offline`). Only the placeholder refusal is tested, so the incident in problem 1 could happen again without any test failing.
3. **medium**, `.github/workflows/release.yml:118-130`: Trivy runs as `aquasec/trivy:0.76.2`, pinned by a mutable tag and not by digest. It gets `/var/run/docker.sock`, which means root on the runner. It runs in a job with `packages: write`, after `actions/checkout`, which by default leaves the job token in `.git/config`. A compromised scanner image could therefore publish packages. Pin the digest, set `persist-credentials: false`, or scan a `docker save` tarball without the socket.
4. **medium**, `.github/workflows/release.yml` (all `uses:` lines): every action in jobs with `packages: write` or `contents: write` is pinned to a moving major tag (`docker/*@v3/v5/v6`, `azure/setup-helm@v4`, `actions/*@v4`), not a commit SHA. The learning note defers this, but it is the main supply-chain exposure of the release path. Dependabot can keep SHA pins current.
5. **medium**, `.github/workflows/release.yml:107-115,138-148`: the image that gets pushed is a second build ("entirely from the cache"), not the image Trivy scanned. A gha cache eviction (likely with the roughly 6 GB `wikipulse-airflow` image and `mode=max`) or a moved base-image tag between the two steps would publish an unscanned image. Push the scanned image itself, or scan the pushed digest before tagging.
6. **medium**, `.github/workflows/release.yml:66-79`: the Airflow image (about 6 GB) has never been built, scanned or pushed by this workflow, locally or in CI. Two builds plus a Trivy scan on a standard runner may run out of disk or cache space and fail the first release.
7. **medium**, `evals/labels/detection_gate_labels.jsonl`: the gate's ground truth is 167 new `qwen3:8b` labels made without the page summary, and no human has checked them, including the 38 `news` labels that recall is built on (for example, "Scene (2026 film)" was labeled news because a Reception section was added). A blocking CI gate then measures agreement with an unchecked LLM.
8. **medium**, `wikipulse/eval/gate.py:55` / `evals/baselines/detection_gate.json`: precision is 10/19, so a single label change moves it by 0.053, more than the 0.05 margin. The gate fails as soon as one spike moves the wrong way (for example, one news spike lost and one noise spike gained). That makes it close to an "exactly equal" check that will produce false alarms. Base the margin on the counts, or give a reason for 0.05.
9. **low**, `.github/workflows/ci.yml:13-16`: `push: branches: ["**"]` plus `pull_request` runs the whole pipeline twice for every PR branch (visible on PR #1: two `test`, `eval-gate` and `dags` runs).
10. **low**, `.github/workflows/release.yml:3-11`: nothing checks that the tagged commit is on `main`, and there is no protected environment, so any commit someone can tag gets released. CI still runs on it, but unreviewed code can ship.
11. **low**, `.github/workflows/release.yml:97-104`: `docker/metadata-action` adds a `latest` tag by default (`flavor: latest=auto`) for non-pre-release semver tags. The header comment and the learning note list only the version, `major.minor` and `sha-` tags.
12. **low**, `scripts/deploy.py:61-62`: `WIKIPULSE_REGISTRY` is a new setting read with `os.environ`, not through `wikipulse/config.py`, and it is not documented in `.env.example`, which AGENTS.md requires.
13. **low**, `scripts/deploy.py:128-131`: `deploy.py` deploys by the mutable tag `<version>`, even though the release records digests. Pulling `@sha256:` would guarantee the deployed image is the released one.
14. **low**, `scripts/deploy.py:86`: `--values` only accepts file names inside the pulled chart (`chart / name`). `cluster.values_file` also accepts any path, so `--values my-overrides.yaml` behaves differently in the two scripts.
15. **low**, `scripts/deploy.py:124,135`: `check_contact` runs twice (once directly, once inside `cluster.secret`), so the placeholder warning is logged twice.
16. **low**, `wikipulse/eval/gate.py:50-52,386`: the default fixture, labels and baseline paths depend on the working directory, and the gate reads `DETECT_*` from the local `.env`. Running it from another directory, or with a tuned `.env`, gives a different verdict than CI does.
17. **low**, `docs/specs/09c-cicd-pipeline.md:3`: the status is still `ready`. AGENTS.md asks for `done` or `blocked` with a reason; until criteria 2 and 3 are seen on GitHub, `blocked` (waiting for the tag and the smoke run) would be accurate.

Verdict: fix

The gate design is sound for its purpose: deterministic, event-time based, no network, stale-input hashes, and unlabeled spikes counted as not news. The release workflow's permissions are well scoped: `permissions: {}` at the top, per-job grants, and only `GITHUB_TOKEN`. The smoke test is correctly wired before release: `release.yml` runs `ci` through `workflow_call`, and `smoke`'s condition is true for `refs/tags/v*`, so `images` cannot start until `smoke` passes. Before merging, fix problems 1 and 2, pin Trivy by digest (3), and either push the scanned image or accept the risk in problem 5 in writing. Then wait for `smoke` to pass on a Linux runner before pushing `v0.1.0-rc.1`.

## Questions for the human before merging

1. A pull request lowers `detect_min_edits` and, in the same commit, runs `--write-baseline` and commits the new `detection_gate.json`. The eval gate is green. What in this pipeline stops that change, and what should you look for in the diff when you review it?
2. With `.env` holding your real contact, which services does `uv run python scripts/deploy.py 0.1.0-rc.1 --allow-placeholder-contact` start, and what will they read from and write to? What flag, or what code change, makes it stay offline?
3. Between Trivy scanning `wikipulse:scan` and the `Push` step, what could make the image published to GHCR differ from the one that was scanned, and how would you prove that a released digest is the one that was scanned?
