# 09c: The full CI/CD pipeline

## What was built

GitHub Actions now does everything between "I pushed a commit" and "a versioned release
is ready to deploy": every pull request is tested, *evaluated* and installed on a
throwaway Kubernetes cluster; every version tag builds, scans and publishes the images and
the Helm chart and writes a GitHub Release.

| Piece | File | What it does |
|-------|------|--------------|
| CI workflow | `.github/workflows/ci.yml` | Jobs `test` (lint, format, Helm lint, unit and integration tests with service containers), `terraform`, `dags`, and new: `eval-gate`, `smoke` |
| Eval gate | `wikipulse/eval/gate.py` | `python -m wikipulse.eval.gate`: the detector on a committed recording, scored against frozen labels, compared with the stored baseline |
| Gate inputs | `tests/fixtures/detection_gate/edits.jsonl.gz`, `evals/labels/detection_gate_labels.jsonl`, `evals/baselines/detection_gate.json` | 16 hours of edits (240 KB), 180 burst labels, the baseline numbers |
| Gate maker | `scripts/make_gate_fixture.py` | How the recording and labels were made (runs once, on the laptop) |
| Smoke test | `scripts/smoke_test.py` | kind cluster `wikipulse-smoke`, the chart installed offline, every pod ready, the API reaching its databases |
| Release workflow | `.github/workflows/release.yml` | On `v*` tags of commits on `main`: check the version, run all of CI, build + scan + push two images, package + push the chart, create the Release |
| Release helper | `scripts/release.py` | `version` (tag must match `pyproject.toml`), `package-chart` (versioned chart pointing at the release's image) |
| Deploy | `scripts/deploy.py` | `deploy.py 0.2.0`: pull the release's image and chart, install them on the local kind cluster, offline unless `--online` |
| Code owners | `.github/CODEOWNERS` | Changes to the gate's baseline, labels, recording or code ask for the owner's review |
| Dependency updates | `.github/dependabot.yml` | Weekly pull requests for uv, GitHub Actions, base images, Terraform providers |
| Tests | `tests/eval/test_gate.py`, `tests/test_cicd.py` | The gate's scoring and its committed inputs; the workflows' structure and permissions; the scripts' logic |

How the pieces fit:

```
 pull request / push to main                           tag v0.2.0 (on main)
 ───────────────────────────                           ────────────────────
 ci.yml                                                release.yml
   test ─────── lint, unit, integration                  version  on main? tag == pyproject version?
   terraform ── fmt, validate                            ci ───── calls ci.yml (all of it)
   dags ─────── DAG import in the Airflow image          images ─ build once -> Trivy scan -> push that image
   eval-gate ── detector on the fixture,                 │         ghcr.io/<owner>/wikipulse:0.2.0, :0.2, :sha-1a2b3c4
                precision/recall vs baseline             │         (+ wikipulse-mlflow)
   smoke ────── build image, kind cluster,               chart ── package 0.2.0 -> oci://ghcr.io/<owner>/charts
                helm install offline, pods ready         release  GitHub Release: digests, chart, notes
                                                                    │
 laptop:  uv run python scripts/deploy.py 0.2.0  ◄──────────────────┘  (pull, not push: see "CD")
```

## Key concepts, with this project's code

### CI vs CD, and the stages

- **Continuous integration (CI)**: every change is merged only after an automated
  pipeline says it works. Here that is `ci.yml`: five jobs that run in parallel on every
  pull request and every push to `main`, each on a fresh Linux machine. A red job blocks the merge (once
  branch protection asks for it, see "Still to do on GitHub").
- **Continuous delivery**: every release is *built and published automatically* and can
  be deployed with one command. That is `release.yml` plus `scripts/deploy.py`.
- **Continuous deployment**: every release is also *deployed* automatically. Not done
  here, on purpose; see "CD to a laptop" below.

The stages the spec names map onto jobs like this:

| Stage | Where | Fails when |
|-------|-------|------------|
| test | `ci.yml` `test`, `terraform`, `dags` | ruff, a unit or integration test, Helm lint, Terraform validate, a DAG import |
| evaluate | `ci.yml` `eval-gate` | detection precision drops by more than 2 spikes' worth, or recall by more than 1 news burst's worth, below the baseline |
| build | `ci.yml` `smoke` (image + install), `release.yml` `images` | the image doesn't build, a pod never gets ready, a fixable critical CVE |
| publish | `release.yml` `images`, `chart`, `release` | the tag doesn't match the version; a push fails |
| deploy | `scripts/deploy.py` on the laptop | the release doesn't install or its pods don't get ready |

`release.yml` calls `ci.yml` as a **reusable workflow** (`on: workflow_call`, then
`uses: ./.github/workflows/ci.yml`), so a release is built only from a commit that passes
everything CI checks, without a second copy of those jobs. For the same reason `ci.yml`'s
`push` trigger lists `main` only: a tag would otherwise run CI twice, and so would a pull
request's branch (once for the push, once for the pull request). A branch without a pull
request can still be checked by hand (`workflow_dispatch`: Actions -> CI -> Run workflow).

### Eval gates: failing a pull request on quality

Unit tests check that code does what it says; they don't notice that a "harmless" change
to a threshold halves the share of real news among the spikes. An **eval gate** runs the
model (here: the rule-based spike detector) on fixed data in CI and fails when its
quality metric drops. Three things make it usable in CI:

1. **Deterministic.** Same input, same numbers, every run. The detector works on event
   time (AGENTS.md's rule), so a recording gives exactly the same spikes on any machine.
   No network, no database, no LLM, no GPU: `gate.detect()` feeds the fixture's edits to
   `SpikeDetector` in memory. It takes about 2 seconds.
2. **A stored baseline with a margin counted in items.** `evals/baselines/detection_gate.json`
   holds the numbers of the code on `main`: precision 0.526 (10 of 19 spikes), recall
   0.211 (8 of 38 news bursts), `margin_spikes: 2`, `margin_bursts: 1`.
   `gate.allowed_drops()` turns the counts into fractions of the baseline's own totals:
   precision may fall by 2/19 = 0.105 (two spikes turning from news to not news), recall
   by 1/38 = 0.026 (one news burst lost); `gate.check()` fails beyond that. Why counts:
   with 19 spikes, a single spike moves precision by 0.053, so the first version's fixed
   0.05 failed as soon as one label flipped, close to an "exactly equal" check (review
   finding). Two spikes absorb one label flip plus one extra noise spike; three fail.
   Recall keeps its old strictness (0.05 also allowed exactly one lost burst), because the
   found news bursts are few (8) and each one matters. Improvements pass; when a change
   is *meant* to move the numbers, `--write-baseline` stores the new ones.
   **That makes the baseline the weak point**: a pull request that lowers a threshold
   *and* rewrites the baseline passes by construction. Two guards: `.github/CODEOWNERS`
   asks for the owner's review of `evals/baselines/`, the gate labels, the recording and
   `gate.py` (enforced once branch protection requires code-owner review), and the
   `eval-gate` job's step "Flag changes to what the gate measures against" puts a
   warning on any pull request that touches them (`git diff HEAD^1 HEAD`: the checked-out
   merge commit's first parent is the base branch). The reviewer then reads the
   baseline's diff: which `config` values changed, and why precision or recall moved.
3. **Inputs that can't change behind its back.** The baseline records the sha256 of the
   fixture and the labels file (`stale_inputs()`); editing either without writing a new
   baseline fails the gate with "the baseline is stale".

**The fixture** (`tests/fixtures/detection_gate/edits.jsonl.gz`) is 16 hours of English
Wikipedia from the 02b recordings, 2026-11-06 21:00 to 2026-11-07 13:00 UTC. The first 4
hours are warm-up (the baselines fill); spikes whose burst begins from 01:00 on are scored.
To make it small and free of personal data, `make_gate_fixture.py` kept only what the
detector reads: event time, title, a *numbered* user (`user-17`; anonymous editors' names
are IP addresses, which must not go into git) and a revert flag instead of the edit
summary. Bot edits are left out (the detector only uses them to move its clock), and so
are pages with fewer than 3 human edits in the 16 hours (no start rule worth testing can
fire on them; they only feed the overall edit rate, and the spikes of all four variants
below were the same with and without them). 49,834 human edits became 19,171 lines,
240 KB.

**The labels** (`evals/labels/detection_gate_labels.jsonl`) are the ground truth. Four
detector variants, spec 02b's strict, default, medium and loose, ran on the fixture;
every spike they raised in the scored hours was grouped into *bursts* (same article,
overlapping periods: `bursts_of()`), 180 in all, and each burst got one label:

- 13 reuse a label from `evals/labels/spike_labels.jsonl` (specs 02b and 07a, made with
  the page summary; `existing_label()`, a human label would win);
- 167 are new LLM labels, from spec 02b's own labeler (`LLMLabeler`, same prompt, JSON
  answer, temperature 0) with `ollama:qwen3:8b`, seeing the burst's edit comments from
  the recording but no page summary, because fetching one would contact Wikimedia.

Every record says `labeler: "llm"` and its model, as AGENTS.md asks. Result: 38 news, 130
not news, 12 unsure.

**No human has reviewed these labels.** Some are plainly doubtful: "Scene (2026 film)"
is `news` because a Reception section was added. So today the gate measures *agreement
with qwen3:8b*, not detection quality: it reliably catches a change that moves the
detector's output, but whether a moved number is better or worse in reality depends on
labels nobody checked, and recall rests entirely on the 38 `news` ones. A human review
(starting with those 38) is a follow-up in `docs/ROADMAP.md`; human lines
(`labeler: "human"`) win over LLM ones, and `--write-baseline` then records the new
numbers.

**The two numbers** (`gate.score()`):

- *Precision* = news / (news + not news + **unlabeled**) over the run's scored spikes.
  An unlabeled spike counts as not news. That is pessimistic on purpose: a change that
  makes the detector raise spikes nobody has judged must label them before it can
  claim they are news; the report lists them.
- *Recall* = news bursts some spike of the run covers / all news bursts. The news bursts
  are the *pool* of what the four variants found, the way search engines are evaluated
  (TREC "pooling"): recall is relative to that pool, not to all news of the night. That is
  why the loose variant reaches recall 1.0 by construction, and why the default's 0.211
  is not comparable with 02b's portal recall.

| Variant (start rule short; long) | Spikes scored | Precision | Recall | Gate |
|---|---:|---:|---:|---|
| strict (6 edits, 4 editors; 12, 5) | 7 | 0.429 | 0.079 | fails (recall) |
| **default (5, 3; 10, 4)** = baseline | 19 | **0.526** | **0.211** | passes |
| medium (4, 3; 8, 3) | 37 | 0.343 | 0.263 | fails (precision) |
| loose (3, 2; 6, 2) | 190 | 0.236 | 1.000 | fails (precision) |
| `quiet_period_min=10` | 22 | 0.500 | 0.211 | passes (within the margin) |
| `min_edits=3` (the demo pull request) | 39 | 0.324 | 0.263 | fails (precision) |
| `min_edits=6` | 15 | 0.533 | 0.158 | fails (recall: 2 news bursts lost) |
| `count_reverts=true` | 30 | 0.333 | 0.211 | fails (precision) |

The trade-off from 02b shows again: looser rules find more of the news (recall up) but
raise mostly noise (precision down), stricter ones the opposite. The gate holds the
current balance unless someone changes it on purpose.

### Container registries and versioning

A **registry** stores images (and, with OCI, Helm charts) under names like
`ghcr.io/<owner>/wikipulse`. GitHub Container Registry (GHCR) belongs to the repository's
owner and accepts pushes from a workflow with the job's own `GITHUB_TOKEN` and
`packages: write`; no password is stored anywhere.

Each image gets several **tags** (`docker/metadata-action`): `0.2.0` (the release; never
moved), `0.2` (the newest patch of 0.2; moves), `sha-1a2b3c4` (the commit it was built
from; answers "what code is this?"). No `latest`: metadata-action adds it to full
releases by default, and `flavor: latest=false` turns that off, because "whatever is
newest" is not a version anyone should deploy. Tags can be moved; a **digest**
(`@sha256:...`) can't, so the release notes list each image by digest. The chart is
versioned the same way: `scripts/release.py package-chart` sets the chart's `version` and
`appVersion` to the release version and its default image to
`ghcr.io/<owner>/wikipulse:0.2.0`, so chart 0.2.0 always runs image 0.2.0. The tag must
equal `version` in `pyproject.toml` (`release.py version`); a release is cut by bumping
that, merging, and tagging the merge commit. The `version` job also checks that the
tagged commit is on `main` (`git merge-base --is-ancestor "$GITHUB_SHA" origin/main`):
otherwise anyone able to push a tag could release a commit that no pull request
reviewed. (A protected GitHub *environment* with required approval would be the
stronger, settings-side version of that check; not set up.)

**Pre-releases** (added after the first review, for the approved first tag
`v0.1.0-rc.1`): a tag may add a pre-release part to the project version. Its *release
part* (before the first `-`) must still equal `pyproject.toml`'s version, while the full
`0.1.0-rc.1` names the images, the chart (`version: 0.1.0-rc.1` is valid SemVer for Helm)
and the GitHub Release. `release.py version --prerelease` prints `true`, and the workflow
then (a) leaves out the floating `0.1` image tag (`enable=` on that metadata-action line):
a release candidate must never become "the newest 0.1", and (b) marks the GitHub Release
as a pre-release. The `0.1.0-rc.1` and `sha-...` tags are still pushed.

### Testing infrastructure in CI: a throwaway cluster

The `smoke` job builds the image and runs `scripts/smoke_test.py`: it creates a kind
cluster *inside the job*, loads the image, installs the chart exactly as the laptop does
(`values-dev.yaml` + `values-offline.yaml`), and checks the result: every Deployment has
its replicas ready, every pod is Ready (the migration Job's pod has Succeeded), and the
API's `/health`, called inside its pod, says it reaches Postgres and Neo4j. Then the
cluster is deleted. Static checks (Helm lint, `helm template`) can't catch a pod that
crashes at start, a probe that never passes, a migration that fails or a network policy
that blocks the database; this does. On failure the script prints the pods, their events
and their logs, because the cluster is gone when you look.

The one thing that differs from the laptop is how the cluster reaches the databases. The
chart points at `host.docker.internal`, which Docker Desktop provides and Linux doesn't.
In CI the databases are the job's **service containers** (the same images as Compose),
published on the runner's ports. `--host-gateway` adds a `hosts` block to CoreDNS
(`corefile_with_host()`) that answers `host.docker.internal` with the gateway of the
Docker network kind uses (`gateway_ip()`), which is the runner itself. Chart and
settings stay unchanged.

Offline: the consumer, enricher and graph runner have 0 replicas, so nothing in the
cluster contacts Wikimedia, and `USER_AGENT` keeps its placeholder. The grouper and the
API download the embedding model from Hugging Face, as on the laptop.

### Supply-chain hygiene

Most code in the images isn't ours: ~200 Python packages, a Debian base, Airflow and
MLflow images. Two habits keep that under control:

- **Automated dependency updates** (`.github/dependabot.yml`): every Monday Dependabot
  opens pull requests for new versions of the Python packages (`uv`, reads `uv.lock`),
  the actions in the workflows, the Dockerfiles' base images and the Terraform
  providers. Minor and patch updates come grouped, majors one at a time. Each one runs
  the whole CI, gate and smoke test included, so "does the update break anything" is
  answered before anyone merges. Left out on purpose: the Airflow requirement files
  (generated from `uv.lock`, a test checks they match) and the service images, pinned to
  what the tests ran against.
- **Image scanning**: each release image is built *once*, into the runner's Docker, with
  all its release tags, and scanned with **Trivy** for known vulnerabilities (CVEs) in
  its OS and Python packages. The job fails on a CRITICAL vulnerability that has a fix
  (`--ignore-unfixed`: one without a fix can't be acted on, and would block every release);
  the full table of HIGH and CRITICAL findings goes to the job summary. Then `docker push
  --all-tags` publishes *that* image, and the digest the registry reports for it goes
  into the release notes. The first version scanned one build and pushed a second build
  "from the cache"; a cache eviction or a base-image tag that moved in between would have
  published an image nobody scanned (review finding).
- **Pin what runs with your token.** A tag like `actions/checkout@v4` can be moved by
  whoever controls that repository, and the next run executes the new code with the
  job's token (here: `packages: write`, `contents: write`). So every action in both
  workflows is pinned to a full commit SHA, e.g.
  `actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0`; Dependabot's
  `github-actions` updates understand that form and move SHA and comment together.
  Trivy is pinned by image digest (`aquasec/trivy:0.76.2@sha256:8f6b...`) for the same
  reason. `tests/test_cicd.py` (`test_every_action_is_pinned_to_a_commit`) fails on an
  unpinned `uses:`.
- **Give tools no more than they need.** Trivy reads a `docker save` file of the image
  instead of the Docker socket (access to the socket is root on the runner) and runs
  before the job logs in to GHCR. No checkout keeps the token in `.git/config`
  (`persist-credentials: false`): several jobs run containers that mount the working
  directory or use Docker, and nothing in either workflow pushes to git.
- **Least privilege for the pipeline itself**: `ci.yml`'s token can only read the
  repository; `release.yml` starts with no permissions and gives each job only what it
  needs (`packages: write` for the two jobs that push, `contents: write` for the one that
  creates the Release). No stored secrets: the automatic `GITHUB_TOKEN` expires with the
  run. `tests/test_cicd.py` checks all of this, and that no workflow contains an e-mail
  address.

### CD to a laptop, and why a self-hosted runner is a security decision

True continuous deployment would have GitHub deploy each release onto the laptop's kind
cluster by itself. GitHub's own runners can't reach a cluster on a home network, so the
job would need a **self-hosted runner**: a program on the laptop that asks GitHub for jobs
and runs them as your user. That is a security decision, not just a setup step:

- **It runs code from the repository on your machine.** Whatever a workflow says, the
  runner executes, with your user's rights: your files, your Docker (which is root-equivalent
  on its VM), your kubeconfig, `.env` with the database passwords and your contact
  address, your browser profile. On GitHub's runners the same code runs in a fresh VM that
  is thrown away after the job.
- **Who can make it run code?** Anyone who can change a workflow that targets the runner:
  every collaborator, a compromised account, and, if the repository is public, anyone
  opening a pull request, unless the workflow is restricted carefully. GitHub's own advice
  is to never use self-hosted runners with public repositories.
- **Dependencies run there too.** A compromised action or Python package (the supply-chain
  attacks Dependabot and Trivy exist for) would run on the laptop instead of a disposable
  VM.
- **It is persistent.** Nothing is wiped between jobs: a job can leave something behind
  that the next one, or you, run later.
- **It holds a credential** that can ask GitHub for jobs, and it keeps a connection open
  to GitHub whenever the laptop is on.

If it were wanted anyway: a separate, unprivileged user or a VM for the runner, only the
`deploy` job on it (`runs-on: [self-hosted, laptop]`), triggered only by releases from
the protected `main` branch, behind a GitHub **environment** with required approval, with
a kubeconfig that can only touch the `wikipulse` namespace. For one person's laptop, the
safer equivalent is what was built: the pipeline publishes, and the laptop *pulls* when
you choose to (`scripts/deploy.py 0.2.0`). Nothing from outside can make the laptop run
anything; deploying stays a deliberate act. GitOps tools (Argo CD, Flux) use the same pull
idea for real clusters: an agent inside the cluster pulls approved versions, and CI never
holds the cluster's credentials.

### Deploying a release: `scripts/deploy.py`

`uv run python scripts/deploy.py 0.2.0` (cluster from `scripts/cluster.py create`):

1. `docker pull ghcr.io/<owner>/wikipulse:0.2.0` with your Docker login (needed while
   the package is private), then `kind load docker-image`, as `cluster.py load` does;
2. `helm pull oci://ghcr.io/<owner>/charts/wikipulse --version 0.2.0 --untar`;
3. the namespace and Secret from your settings (`cluster.secret()`);
4. `helm upgrade --install` of that chart with its `values-dev.yaml` (or `--values`) and the
   release's image, `--wait`: a new revision of the release, so `cluster.py rollback` goes
   back to what ran before.

The registry comes from `--registry`, the `WIKIPULSE_REGISTRY` setting
(`wikipulse/config.py`, documented in `.env.example`), or the git remote `origin`
(lowercased, as GHCR wants).

**Offline is the default.** Without flags, `deploy.py` adds `values-offline.yaml`: the
consumer, enricher and graph runner (the services that contact Wikimedia) get 0
replicas, whatever `USER_AGENT` holds. Only `--online` starts them, and `--online`
refuses a placeholder contact before running any command (`ONLINE_NEEDS_CONTACT`). The
first version did the opposite: it went online whenever `.env` had a real contact, and
its `--allow-placeholder-contact` help said "implies --offline", which was only true
with the placeholder. That is how the incident under "Results" happened. The flag is
gone; tests cover offline-by-default with a real and a placeholder contact, and
`--online` with both.

## Design decisions and deviations

- **The gate's ground truth is labels, not the Current events portal.** 02b's portal
  metrics are a lower bound with tiny counts (portal recall 1.4%, precision 7.4% over two
  whole days); on 12 hours they would be zero or one match and say nothing about a
  threshold change. Labels of every burst four variants raise make both directions
  visible.
- **A compact fixture instead of the raw recording format.** Raw messages are about 1.5 KB
  each (the 290-line `stream_sample.jsonl` is 444 KB); 16 hours would be hundreds of MB
  and full of user names and IP addresses. The compact format keeps exactly what the
  detector reads, so the gate skips the stage-1 filter and parser (which have their own
  tests) and runs `SpikeDetector` directly.
- **Unlabeled spikes count as not news** (see above), so a new rule can't pass the gate by
  raising spikes nobody judged.
- **New labels were made without the page summary**, with `qwen3:8b` instead of 02b's
  `qwen3:4b` (the stronger model makes up for less context). They are LLM labels with
  the usual caveats (02b: about ±14 points on 50 labels); for a gate that compares a
  change with the baseline *on the same labels*, consistency matters more than accuracy.
  A human pass over the 38 news labels (`labeler: "human"` lines, then
  `--write-baseline`) would make the numbers more meaningful.
- **Two images, not three**: `wikipulse` (the chart's) and `wikipulse-mlflow`. The
  first version also released `wikipulse-airflow`; the review pointed out that this
  6 GB image had never gone through the release job, and a build plus a scan (plus, now,
  a `docker save` file for Trivy) on a standard runner's roughly 14 GB of free disk could
  fail the first release. Freeing disk on the runner would work around that, but the
  image isn't worth releasing: nothing deploys it (the chart and `deploy.py` use only
  `wikipulse`; Airflow runs from Compose, which builds it locally), and it only works
  with the repository mounted at `/opt/wikipulse`, so a published copy is not a
  self-contained release. CI's `dags` job still builds it on every run, so a broken
  Dockerfile is caught.
- **Trivy runs from its container image**, pinned by digest, instead of through the
  `aquasecurity/trivy-action` wrapper: one less third-party action with access to the job,
  and the same command works on the laptop.
- **The smoke test is its own script** rather than `cluster.py up`: a separate cluster name
  (`wikipulse-smoke`, so it never touches the day-to-day cluster), the CoreDNS change for
  Linux, the readiness and health checks, diagnostics, and deletion at the end. It reuses
  `cluster.py`'s namespace and Secret builders.
- **CI runs on pull requests, `main`, release tags (through `release.yml`) and by hand**,
  not on every branch push; the smoke job no longer needs its own condition.
- **Actions are pinned to commit SHAs** within the major versions the first version used
  (newest release of each: e.g. checkout v4.4.0, setup-uv v6.8.0, build-push-action
  v6.19.2). Moving to newer majors (checkout v7, build-push-action v7, ...) is left to
  Dependabot's pull requests, where CI checks each one.
- **The gate margin is counted in spikes and bursts** (`margin_spikes: 2`,
  `margin_bursts: 1`), see "Eval gates" above. `--write-baseline` keeps the stored
  margins unless `--margin-spikes` / `--margin-bursts` say otherwise.
- **No status checks are required yet.** Making `eval-gate` and `smoke` required for
  merging is a repository setting (branch protection or a ruleset), not a file.

## Results

All verified locally on 2026-11-11, Windows 11, Docker Desktop. Nothing was pushed, no tag
or release was created, nothing was published to GitHub or GHCR.

**Eval gate** (`uv run python -m wikipulse.eval.gate`, about 2 seconds):

- On the current code: **PASS**, exit code 0: precision 0.526 (10 news of 19 spikes),
  recall 0.211 (8 of 38 news bursts), no spike without a label.
- With a deliberately worse threshold, as a local uncommitted change: `wikipulse/config.py`
  `detect_min_edits` 5 -> 3, then the same command: **FAIL**, exit code 1:

  ```
  | precision | 0.324 | 0.526 |
  | recall | 0.263 | 0.211 |
  | spikes scored | 39 | 19 |
  Detector settings that differ from the baseline's:
  - `min_edits`: 5 -> 3
  **Fails:** precision dropped from 0.526 to 0.324 (more than the allowed 0.050)
  ```

  After the review fixes (margins in counts): `--set min_edits=3` still **FAILS**, exit
  code 1: "precision dropped from 0.526 to 0.324 (more than the allowed 0.105)"; a test
  (`test_lowering_min_edits_to_3_fails_on_precision`) keeps it that way.

  The change was reverted (`git diff wikipulse/config.py` empty) and the gate passed
  again. The other direction, `--set min_edits=8`: **FAIL** on recall (0.211 -> 0.132).
  `tests/eval/test_gate.py` keeps both directions as tests (medium thresholds fail on
  precision, strict ones on recall).

**Workflow lint**: actionlint 1.7.12 (its Docker image, `rhysd/actionlint`, with
shellcheck 0.11.0 and pyflakes, so the `run:` scripts are checked too): 0 errors in
`ci.yml` and `release.yml`. Nothing was installed on the laptop; the image was only pulled.

**Smoke test, locally with kind** (`uv run python scripts/smoke_test.py --image
wikipulse:ci --host-gateway`, image built from this branch): **PASS**, twice (3 min 24 s
and 3 min 30 s from cluster creation to deletion). The Linux path was used on purpose:
CoreDNS answered `host.docker.internal` with `172.21.0.1`, the kind network's gateway, and
the pods reached Compose's Postgres and Neo4j through it, across Docker networks, as they
will reach the service containers in CI. Helm's install with the migration hook took
about 70 s; api, archiver, detector and grouper `1/1`, consumer, enricher and graph `0/0`;
the API's `/health`: `"postgres":true,"neo4j":true`. Nothing in that cluster contacted
Wikimedia (the smoke test always applies the offline overlay). It used the Compose
services, so its detector and archiver read the `wiki:edits` stream groups, as the 08b
and 09a acceptance runs did; in CI it gets fresh service containers.

**Image scan, locally** (the release job's two Trivy commands, `aquasec/trivy:0.76.2`):

| Image | HIGH | CRITICAL | critical with a fix | Gate |
|-------|-----:|---------:|--------------------:|------|
| `wikipulse:ci` (Debian 12) | 96 | 2 | 0 | passes |
| `wikipulse-mlflow:3.18.0` (Debian 13) | 51 | 0 | 0 | passes |

The two critical findings in `wikipulse` are in the base image's `libsqlite3-0`
(CVE-2025-7458, no fixed version yet) and `zlib1g` (CVE-2023-45853, "will not fix" in
Debian 12); no Python package had a HIGH or CRITICAL finding. A newer base image
(Dependabot will offer it) is the way to bring the HIGH count down. The Airflow image
(6 GB) was not scanned locally.

**Chart packaging and `deploy.py`, end to end against a local registry** (a throwaway
`registry:2` container on `localhost:5001`; nothing published anywhere else):
`release.py package-chart --version 0.1.0 --image-repository localhost:5001/test/wikipulse`
(lint and package), `helm push ... --plain-http`, the image pushed there too, then on a new
`wikipulse` kind cluster `uv run python scripts/deploy.py 0.1.0 --registry
localhost:5001/test --plain-http --allow-placeholder-contact`: image pulled and loaded,
chart pulled, Secret applied, revision 1 installed in 1 min 58 s, every pod ready, running
`localhost:5001/test/wikipulse:0.1.0`. Registry, test image and cluster were removed
afterwards.

**A mistake in that run, and its side effects** (the cause, `deploy.py` going online by
default, was fixed after the review; see "Deploying a release"). I expected `.env` to still hold the
placeholder contact, as in 08a to 09b, and relied on `--allow-placeholder-contact` to make
the deploy offline. It holds a real contact now, so `deploy.py` (correctly, like
`cluster.py deploy`) deployed the *whole* pipeline. The consumer, enricher and graph runner
ran for about one minute (about 01:10:20 to 01:11:30 UTC) before I deleted the cluster:

- the consumer opened one stream connection with your User-Agent and resumed from 08a's
  checkpoint `wiki:k8s:consumer` (2026-11-10 16:22 UTC). It wrote 722 English article
  edits (event time 10 November, 16:22 to about 16:31 UTC) to `wiki:edits`; the
  `detect:live` group processed them (no new spike), and the `lake:archive` group holds
  them as pending, so the archiver writes them to the lake when it next runs;
- the graph runner re-extracted the live events `live:1` and `live:2` with `qwen3:4b`
  (17 and 6 entities, written to Neo4j and `graph_extractions`) and made 6 Wikipedia and
  Wikidata API requests (cached in `api_cache`). The enricher had nothing to do.

Nothing was deleted or rolled back (that would have meant more unrequested writes to your
data). The lesson, now built into `deploy.py`: the safe mode must be the default, and the
risky one must need an explicit flag that says what it does (`--online`).

**Tests**: `uv run pytest`: 1269 passed, 1 skipped (Airflow is only in its image); new:
`tests/eval/test_gate.py` (18) and `tests/test_cicd.py` (27). `ruff check` and
`ruff format --check` are clean.

**After the review fixes** (2026-11-11, still nothing pushed, tagged or deployed):
`uv run pytest`: 1286 passed, 1 skipped; `tests/eval/test_gate.py` 22 tests,
`tests/test_cicd.py` 40. `ruff check` and `ruff format --check` clean; actionlint (Docker
image, with shellcheck) 0 errors in both workflows. The gate passes on the committed
baseline and fails on `--set min_edits=3`. The new Trivy form (digest-pinned image,
`image --input` on a `docker save` file, no socket) was tried locally on `alpine:3.20`:
it scans and exits 0. `deploy.py` was not run against a cluster: its new tests mock every
command, and the offline default is checked through the `helm upgrade` arguments.

## Still to do on GitHub (acceptance steps that can only run there)

Nothing was pushed, tagged or published. To finish the three acceptance criteria:

1. **Push the branch and open the pull request** (`git push -u origin
   spec-09c-cicd-pipeline`, then `gh pr create` from PowerShell). Watch the run: `test`,
   `terraform`, `dags`, `eval-gate` and `smoke` must all be green.
   - *"The smoke-test job passes"* is checked here, on GitHub's Linux runner with service
     containers; locally it ran on Docker Desktop. The first run builds the image without
     a cache (several minutes); later runs reuse the GitHub Actions cache.
   - The `eval-gate` job's summary page shows the gate's table.
2. **Show that a worse pull request fails the gate**: a throwaway branch from this one
   with `detect_min_edits: int = Field(default=3, ge=1)` in `wikipulse/config.py`, pushed
   and opened as a draft pull request. `eval-gate` must turn red with "precision dropped
   from 0.526 to 0.324". Then close it without merging and delete the branch.
3. **Make the checks required** (a repository setting, not a file): Settings -> Rules (or
   Branches) -> a rule for `main` that requires the status checks `test`, `terraform`,
   `dags`, `eval-gate` and `smoke`. Without it, a red gate is visible but doesn't block a
   merge.
4. **Merge, then cut the first (pre-)release**: `pyproject.toml` says 0.1.0, and the
   approved tag is `v0.1.0-rc.1`, so on the merged `main`: `git tag v0.1.0-rc.1` and
   `git push origin v0.1.0-rc.1`. Watch the `Release` workflow:
   - *"Pushing a tag produces images, a chart and a release"*: two image packages on the
     repository's Packages page (`wikipulse`, `wikipulse-mlflow`, each tagged
     `0.1.0-rc.1` and `sha-...`, and *no* `0.1` or `latest`), the chart package
     `charts/wikipulse` version 0.1.0-rc.1, and the GitHub Release `v0.1.0-rc.1`, marked
     as a pre-release, with the image digests, the chart file and the generated notes.
   - If the image push is refused: a package that already exists must grant this
     repository write access (the package's settings -> Manage Actions access). The
     repository's default workflow permissions can stay read-only; each job asks for its own.
5. **Deploy it on the laptop** (optional): `docker login ghcr.io` with a token that has
   `read:packages` (new packages are private), `uv run python scripts/cluster.py create`,
   then `uv run python scripts/deploy.py 0.1.0-rc.1` (offline; `--online` starts the
   services that contact Wikimedia).
7. **Code-owner review** (optional, a setting): in the rule for `main`, "Require review
   from Code Owners" makes `.github/CODEOWNERS` binding. GitHub doesn't let you approve
   your own pull request, so in a one-person repository it mostly documents the rule.
6. **Dependabot** starts once `dependabot.yml` is on `main`: Insights -> Dependency graph ->
   Dependabot shows its runs. Turning on "Dependabot security updates" in Settings -> Code
   security adds the alert-driven pull requests.

## Questions to test your understanding

1. A pull request lowers `detect_min_edits` from 5 to 3 and adds a test for it. All unit
   tests pass. What does the gate report, and why is "precision" the number that fails?
2. Why does the gate count a spike without a label as "not news" instead of leaving it
   out, as 02b's label precision does?
3. Why does `release.yml` scan each image before logging in to GHCR and then push that
   same local image, instead of pushing first and scanning the pushed image, or
   building again for the push?

<details>
<summary>Answers</summary>

1. The detector raises 39 spikes in the scored hours instead of 19. It finds two more of
   the news bursts (recall 0.211 -> 0.263), but most new spikes are labeled not news, so
   precision falls from 0.526 to 0.324, far more than the allowed 0.105 (two spikes'
   worth of 19): the gate fails and lists the changed setting. Unit tests check that the code works; the gate checks that
   it still works *well*.
2. Leaving them out would reward a change for raising spikes nobody has judged: a new
   rule could add 100 unknown spikes and keep its precision, because only the old,
   labeled ones are counted. Counting them as not news makes the safe assumption; the
   author can label them (`make_gate_fixture.py`) and, if they are news, precision
   recovers. 02b measured a fixed random sample of one run, where every sampled spike was
   labeled, so the question didn't arise there.
3. Once an image is pushed under `0.2.0`, people and `deploy.py` can pull it; a scan
   afterwards can only report. Scanning the locally loaded build first means a release
   with a fixable critical vulnerability is never published. Pushing that very image
   (not a rebuild) guarantees the published digest is the scanned one: a rebuild could
   differ if the build cache was evicted or a base-image tag moved. And scanning before
   the login means a compromised scanner finds no registry credential to steal.

</details>
