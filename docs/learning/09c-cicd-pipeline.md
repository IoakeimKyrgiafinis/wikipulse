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
| Release workflow | `.github/workflows/release.yml` | On `v*` tags: check the version, run all of CI, build + scan + push three images, package + push the chart, create the Release |
| Release helper | `scripts/release.py` | `version` (tag must match `pyproject.toml`), `package-chart` (versioned chart pointing at the release's image) |
| Deploy | `scripts/deploy.py` | `deploy.py 0.2.0`: pull the release's image and chart, install them on the local kind cluster |
| Dependency updates | `.github/dependabot.yml` | Weekly pull requests for uv, GitHub Actions, base images, Terraform providers |
| Tests | `tests/eval/test_gate.py`, `tests/test_cicd.py` | The gate's scoring and its committed inputs; the workflows' structure and permissions; the scripts' logic |

How the pieces fit:

```
 pull request / push to a branch                       tag v0.2.0
 ───────────────────────────────                       ──────────
 ci.yml                                                release.yml
   test ─────── lint, unit, integration                  version  tag == pyproject version?
   terraform ── fmt, validate                            ci ───── calls ci.yml (all of it)
   dags ─────── DAG import in the Airflow image          images ─ build -> Trivy scan -> push
   eval-gate ── detector on the fixture,                 │         ghcr.io/<owner>/wikipulse:0.2.0, :0.2, :sha-1a2b3c4
                precision/recall vs baseline             │         (+ wikipulse-airflow, wikipulse-mlflow)
   smoke ────── build image, kind cluster,               chart ── package 0.2.0 -> oci://ghcr.io/<owner>/charts
                helm install offline, pods ready         release  GitHub Release: digests, chart, notes
                                                                    │
 laptop:  uv run python scripts/deploy.py 0.2.0  ◄──────────────────┘  (pull, not push: see "CD")
```

## Key concepts, with this project's code

### CI vs CD, and the stages

- **Continuous integration (CI)**: every change is merged only after an automated
  pipeline says it works. Here that is `ci.yml`: five jobs that run in parallel on every
  push and pull request, each on a fresh Linux machine. A red job blocks the merge (once
  branch protection asks for it, see "Still to do on GitHub").
- **Continuous delivery**: every release is *built and published automatically* and can
  be deployed with one command. That is `release.yml` plus `scripts/deploy.py`.
- **Continuous deployment**: every release is also *deployed* automatically. Not done
  here, on purpose; see "CD to a laptop" below.

The stages the spec names map onto jobs like this:

| Stage | Where | Fails when |
|-------|-------|------------|
| test | `ci.yml` `test`, `terraform`, `dags` | ruff, a unit or integration test, Helm lint, Terraform validate, a DAG import |
| evaluate | `ci.yml` `eval-gate` | detection precision or recall drop more than 0.05 below the baseline |
| build | `ci.yml` `smoke` (image + install), `release.yml` `images` | the image doesn't build, a pod never gets ready, a fixable critical CVE |
| publish | `release.yml` `images`, `chart`, `release` | the tag doesn't match the version; a push fails |
| deploy | `scripts/deploy.py` on the laptop | the release doesn't install or its pods don't get ready |

`release.yml` calls `ci.yml` as a **reusable workflow** (`on: workflow_call`, then
`uses: ./.github/workflows/ci.yml`), so a release is built only from a commit that passes
everything CI checks, without a second copy of those jobs. For the same reason `ci.yml`'s
`push` trigger lists branches only: a tag would otherwise run CI twice.

### Eval gates: failing a pull request on quality

Unit tests check that code does what it says; they don't notice that a "harmless" change
to a threshold halves the share of real news among the spikes. An **eval gate** runs the
model (here: the rule-based spike detector) on fixed data in CI and fails when its
quality metric drops. Three things make it usable in CI:

1. **Deterministic.** Same input, same numbers, every run. The detector works on event
   time (AGENTS.md's rule), so a recording gives exactly the same spikes on any machine.
   No network, no database, no LLM, no GPU: `gate.detect()` feeds the fixture's edits to
   `SpikeDetector` in memory. It takes about 2 seconds.
2. **A stored baseline with a margin.** `evals/baselines/detection_gate.json` holds the
   numbers of the code on `main`: precision 0.526, recall 0.211, margin 0.05.
   `gate.check()` fails when either is more than the margin below. A margin is needed
   because with 19 scored spikes one spike changing label moves precision by 0.05; the
   gate is for real regressions, not for every wobble. Improvements pass; when a change
   is *meant* to move the numbers, `--write-baseline` stores the new ones and the
   reviewer sees the baseline change in the pull request's diff.
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
from; answers "what code is this?"). Tags can be moved; a **digest**
(`@sha256:...`) can't, so the release notes list each image by digest. The chart is
versioned the same way: `scripts/release.py package-chart` sets the chart's `version` and
`appVersion` to the release version and its default image to
`ghcr.io/<owner>/wikipulse:0.2.0`, so chart 0.2.0 always runs image 0.2.0. The tag must
equal `version` in `pyproject.toml` (`release.py version`); a release is cut by bumping
that, merging, and tagging the merge commit.

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
- **Image scanning**: each release image is built into the runner's Docker first and
  scanned with **Trivy** (`aquasec/trivy:0.76.2`) for known vulnerabilities (CVEs) in its
  OS and Python packages. The job fails on a CRITICAL vulnerability that has a fix
  (`--ignore-unfixed`: one without a fix can't be acted on, and would block every release);
  the full table of HIGH and CRITICAL findings goes to the job summary. Only after the
  scan does the same build (from the cache) get pushed: nothing vulnerable is published.
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

The registry comes from `--registry`, `WIKIPULSE_REGISTRY`, or the git remote `origin`
(lowercased, as GHCR wants). Like `cluster.py deploy`, it refuses the placeholder contact
unless `--allow-placeholder-contact`, which deploys offline.

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
- **Three images, not one**: `wikipulse` (the chart's), plus `wikipulse-airflow` and
  `wikipulse-mlflow` from `docker/`, which Compose builds today. Releasing them means a
  release pins every image the system runs.
- **Trivy runs from its container image**, pinned by version, instead of through the
  `aquasecurity/trivy-action` wrapper: one less third-party action with access to the job,
  and the same command works on the laptop.
- **The smoke test is its own script** rather than `cluster.py up`: a separate cluster name
  (`wikipulse-smoke`, so it never touches the day-to-day cluster), the CoreDNS change for
  Linux, the readiness and health checks, diagnostics, and deletion at the end. It reuses
  `cluster.py`'s namespace and Secret builders.
- **The smoke job runs on pull requests, `main` and release tags**, not on every branch
  push: it takes several minutes (image build, kind, model download).
- **Actions are pinned to major versions** (`@v4`), as the existing CI did, and Dependabot
  keeps them current. Pinning to commit SHAs is stricter (a moved tag can't change what
  runs) and worth doing for third-party actions; left as a follow-up.
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

**A mistake in that run, and its side effects.** I expected `.env` to still hold the
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
data). The lesson: when a deploy must stay offline, pass `--offline`;
`--allow-placeholder-contact` only forces it while the contact really is the placeholder.

**Tests**: `uv run pytest`: 1264 passed, 1 skipped (Airflow is only in its image); new:
`tests/eval/test_gate.py` (18) and `tests/test_cicd.py` (22). `ruff check` and
`ruff format --check` are clean.

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
4. **Merge, then cut the first release**: `pyproject.toml` says 0.1.0, so on the merged
   `main`: `git tag v0.1.0` and `git push origin v0.1.0`. Watch the `Release` workflow:
   - *"Pushing a tag produces images, a chart and a release"*: three packages on the
     repository's Packages page (`wikipulse`, `wikipulse-airflow`, `wikipulse-mlflow`,
     each tagged `0.1.0`, `0.1` and `sha-...`), the chart package `charts/wikipulse`
     version 0.1.0, and the GitHub Release `v0.1.0` with the image digests, the chart file
     and the generated notes.
   - If the image push is refused: a package that already exists must grant this
     repository write access (the package's settings -> Manage Actions access). The
     repository's default workflow permissions can stay read-only; each job asks for its own.
5. **Deploy it on the laptop** (optional): `docker login ghcr.io` with a token that has
   `read:packages` (new packages are private), `uv run python scripts/cluster.py create`,
   then `uv run python scripts/deploy.py 0.1.0` (add `--offline` to keep the cluster away
   from Wikimedia).
6. **Dependabot** starts once `dependabot.yml` is on `main`: Insights -> Dependency graph ->
   Dependabot shows its runs. Turning on "Dependabot security updates" in Settings -> Code
   security adds the alert-driven pull requests.

## Questions to test your understanding

1. A pull request lowers `detect_min_edits` from 5 to 3 and adds a test for it. All unit
   tests pass. What does the gate report, and why is "precision" the number that fails?
2. Why does the gate count a spike without a label as "not news" instead of leaving it
   out, as 02b's label precision does?
3. Why does `release.yml` build each image, scan it, and only then build it again to push,
   instead of pushing first and scanning the pushed image?

<details>
<summary>Answers</summary>

1. The detector raises 39 spikes in the scored hours instead of 19. It finds two more of
   the news bursts (recall 0.211 -> 0.263), but most new spikes are labeled not news, so
   precision falls from 0.526 to 0.324, more than the 0.05 margin: the gate fails and
   lists the changed setting. Unit tests check that the code works; the gate checks that
   it still works *well*.
2. Leaving them out would reward a change for raising spikes nobody has judged: a new
   rule could add 100 unknown spikes and keep its precision, because only the old,
   labeled ones are counted. Counting them as not news makes the safe assumption; the
   author can label them (`make_gate_fixture.py`) and, if they are news, precision
   recovers. 02b measured a fixed random sample of one run, where every sampled spike was
   labeled, so the question didn't arise there.
3. Once an image is pushed under `0.2.0`, people and `deploy.py` can pull it; a scan
   afterwards can only report. Scanning the locally loaded build first means a release
   with a fixable critical vulnerability is never published. The second build costs
   seconds because every layer comes from the build cache.

</details>
