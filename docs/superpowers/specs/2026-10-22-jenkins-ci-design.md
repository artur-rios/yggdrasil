# Jenkins runs CI: branch policy and the Linux checks move off GitHub Actions

Status: draft design, 2026-10-22, awaiting the owner's review. Phase 1 of leaving GitHub; phase 2 (a self-hosted git server, likely
Forgejo) is a separate design.

## Intent

Today GitHub Actions runs every application's branch policy, tests and builds, and Jenkins only deploys,
waiting for Actions before a release. The goal is to self-host the pipeline: Jenkins does what Actions
does today (branch policy, test, build) and then the deploys it already does. Like for like, not
new checks.

Two things can't move to a Linux machine: the Windows jobs and the one macOS job (Safari). They stay on
GitHub Actions. When Actions is unavailable, the owner can tell Jenkins to deploy a release without them.

Phase 2 swaps GitHub for another git server, so everything built here talks to GitHub only through
`scripts/github.sh` and `github/rulesets.py`, never through Actions-only features.

## Goals

1. **Branch policy in Jenkins.** The `branch-policy` check every repository runs today becomes one script in
   yggdrasil, run by the shared pipeline on every pull request into `develop` or `main`.
2. **Linux checks in Jenkins.** Every merge check that runs on Linux today runs in Jenkins, on a dedicated
   CI machine, and reports to GitHub under the same name it has today (with one exception, Chimera's, below).
3. **Windows and macOS stay on Actions**, and Jenkins still waits for them before a release, as today.
4. **A skip.** If the wait for Actions times out or GitHub doesn't answer, Jenkins offers a "Deploy without
   GitHub Actions checks" button instead of failing the release.
5. **Checks before deploys.** On `develop`, `main` and `release/*` pushes, the checks run first and the
   branch environments deploy only if they pass.
6. **yggdrasil's own CI** (Status API, Console, Platform, branch policy) moves to Jenkins too.

## Non-goals

- Leaving GitHub as the git host (phase 2).
- Release-time builds. These stay on Actions unchanged: heimdall-ui and fortuna-ui `build.yml`, yggdrasil
  `release.yml` and Chimera `release-artifacts.yml`. Each has a Windows leg and gathers every platform
  into one GitHub release; splitting their Linux legs into Jenkins would leave neither side able to check
  the whole release. They run on tags, so an Actions outage delays release files, never a deploy.
- heimdall-api's docs site. GitHub Pages only publishes from Actions (the repository deliberately avoids a
  `gh-pages` branch). It stays on Actions until phase 2.
- Dependabot. It is a GitHub feature, not Actions, and keeps working.
- New checks, or changes to what any check verifies.
- Running checks on plain feature-branch pushes. Checks run on pull requests, as today.
- **Pull requests from forks** (owner's decision). Jenkins doesn't discover or build them, so they get
  no checks; the owner merges one with the admin bypass, or pushes its branch into the repository first so
  it is built as the owner's. Today Actions does check them (fortuna-api is public); this is a deliberate
  loss, since an outsider's `ci/` scripts would otherwise run on the CI machine. Documented in setup.md and
  each public repository's CONTRIBUTING.

## Anti-goals

- **CI must not be able to touch production.** Check code (and every dependency it pulls) must never reach
  a Docker engine that runs an environment. No host Docker socket, no `--privileged` Docker-in-Docker on a
  deploy host.
- **The skip must not hide a failure.** It is offered when Actions doesn't answer, never when an Actions
  check reported a failure.
- **No second copy of the release logic.** Releases still go through `release()` in
  `yggdrasilPipeline.groovy`; checks are a stage in front of it, not a parallel flow.

## Decided

These were decided by the owner during design:

- Windows and macOS jobs stay on GitHub Actions. Everything else that is a merge check moves to Jenkins.
- If Actions is unavailable, the owner can choose to skip those jobs and deploy anyway.
- `main`'s ruleset requires only what Jenkins reports, so a skipped release can still merge. `develop`'s
  ruleset still requires the Windows/macOS checks; during an outage the owner merges into `develop` with
  the admin bypass that already exists.
- CI runs on a **separate machine**, not on a deploy host: an Ubuntu VPS (8 vCPU, 32 GB RAM).
- Pull requests from forks get no checks.
- Each application's check steps live **in its own repository**, declared in its Jenkinsfile, and the
  shared pipeline runs them.
- Release-time builds and the docs site stay on Actions (non-goals above).
- Pushes to `develop`/`main` run checks before deploying (a deploy to development waits for them).

## What moves and what stays

| Repository | Moves to Jenkins | Stays on Actions (required on `develop`, waited for on releases) | Stays on Actions (not merge checks) |
|---|---|---|---|
| heimdall-api | branch-policy, `test` (incl. Python helper tests, vulnerability scan, coverage minimum), `docker`, OpenAPI drift (`check`) | none | Build Docs (Pages) |
| heimdall-ui | branch-policy, `Analyze and test`, `Regenerate and compare` | none | `build.yml` (tags) |
| fortuna-api | branch-policy, `test`, `docker`, `check`, `audit`, native core on Linux (`msrv`, `build (ubuntu)`) | none required; native core on Windows is path-filtered, so it is waited for when it runs but never required | none |
| fortuna-ui | branch-policy, `Analyze and test`, the three drift checks | none | `build.yml` (tags) |
| chimera | branch-policy (with its extras, below), `Analyze and test`, browser tests (Linux), `Native (linux)`, `Container`, `Accessibility and performance` | `Safari`, `Native (windows)` | `release-artifacts.yml` |
| yggdrasil | branch-policy, `Status API`, `Console` (Linux and Android), `Platform` | `Console (Windows)` | `release.yml` (tags) |

Check names are today's GitHub check names. **Chimera's `Cross-platform`** is a job that only aggregates
browser, Safari and native results; it splits across the two systems, so it goes away. Its catalog
`checks` list names the Jenkins pieces and its new `actionsChecks` list names the two Actions pieces
(builder's call on exact names; they must match what each side reports).

Path-filtered workflows (fortuna-api's native core, heimdall-api's OpenAPI `check`) keep their filter
in Jenkins, with the same paths as today: the check is skipped
when nothing under its paths changed, and reported as success with a "skipped: no changes under …"
description, so it can stay out of the required list as today.

## Architecture

```
GitHub ──webhook──▶ Jenkins controller (VPS)
                       │
                       ├─ node('ci')  ── CI machine: CI agent + its own Docker engine
                       │      branch-policy, then every check in parallel, each in its own container
                       │      each reports a commit status <check name> (pending → success/failure)
                       │
                       ├─ release PR only: wait for the Actions checks (wait-checks)
                       │      timeout / GitHub unreachable ──▶ input "Deploy without GitHub Actions checks"
                       │
                       └─ node(<environment agent>) ── deploy.sh, as today
```

### The CI machine

- A separate machine with its own Docker Engine (not Docker Desktop), used for nothing else. The owner's:
  an Ubuntu VPS, 8 vCPU, 32 GB RAM, 400 GB NVMe, on its own (not on the production VPS's network); it
  reaches the controller over the internet like any other agent. Assumed x86-64: the check images
  (Flutter, Playwright, the `linux-x64` native builds) need it, so setup.md states it as a requirement.
- Sized for it: **4 executors** to start (designer's call). That's enough for one Chimera build's checks
  side by side, or two applications' builds at once, with room for PostgreSQL containers. The disk holds
  check images and build caches; a scheduled `docker system prune` (builder's call on how and when) keeps
  it bounded.
- It runs the **CI agent**: an inbound WebSocket agent like the deploy agent (it dials out to the controller,
  so the CI machine needs no open ports), named and labeled `ci`. It mounts *that machine's* Docker socket,
  which is acceptable because the machine holds nothing but CI.
- Its image is the deploy agent's minus what only deploys need, plus git: Docker CLI with Buildx, curl,
  jq, Python with PyYAML (branch policy and catalog checks). Toolchains (.NET, Flutter, Rust, Node,
  Playwright) are **not** installed on it: each check names its own image.
- Created like the other agents: `init.groovy.d/agents.groovy` also creates `ci`. How the controller learns the CI agent exists (always, or a catalog setting such as a
  top-level `ci: { agent: ci }`) is the builder's call; it must not depend on an environment.
- Setup: a new section in `docs/setup.md` ("Bring up the CI machine"), a Compose profile (`ci`) in
  `platform/compose.yml` or a small Compose file of its own, and the agent's secret in that machine's env
  file, mirroring section 9.5.
- **No GitHub write access on the CI machine** (designer's call, from the first anti-goal). Check code
  runs with root on this machine, so anything it holds can leak. The `github-app` installation token can
  push branches, merge pull requests and dispatch workflows: pushing a `release/*` branch alone would get
  code deployed to homologation. So the CI machine never sees it:
  - A second GitHub App, **yggdrasil CI** (credential id `github-ci`), with Contents: read, Commit
    statuses: write and Metadata: read, installed on the same repositories. A new step in setup.md's
    section 7. It can't push, merge or dispatch. Statuses alone can't merge into `main`, because only
    Jenkins merges releases, after its own deploys.
  - Builds check out on `node('ci')` with `github-ci`, not with `github-app` (the multibranch source and
    the deploy agents keep `github-app`).
  - Check and `branch-policy` statuses are posted with `github-ci` too. `deploy/<environment>`, merge,
    release and branch deletion stay on the deploy agents with `github-app`, as today.
- **Dependabot pull requests get no credentials.** Actions withholds repository secrets from Dependabot
  today, so Jenkins doesn't pass a check's `credentials` on builds of `dependabot/*` branches. Checks must
  pass without them, as they do on Actions now.
- **Workspace paths.** The agent runs checks with `docker run` against the machine's engine, so a bind mount
  of the workspace must mean the same path on the host and in the agent container. Mount the agent's work
  directory at an identical path on both sides (`/home/jenkins/agent:/home/jenkins/agent`).

### Declaring checks (application repositories)

Each Jenkinsfile passes its checks to the shared pipeline:

```groovy
@Library('yggdrasil') _
yggdrasilPipeline(stack: 'heimdall-api', checks: [
  'test':   [image: 'mcr.microsoft.com/dotnet/sdk:10.0', run: 'ci/test.sh', docker: true,
             credentials: [REPORTGENERATOR_LICENSE: 'reportgenerator-license'],
             junit: 'artifacts/test-results/**/*.trx', archive: 'artifacts/coverage-report/**'],
  'docker': [image: 'docker:cli', run: 'docker build .', docker: true],
  'check':  [image: 'mcr.microsoft.com/dotnet/sdk:10.0', run: 'ci/check-openapi.sh',
             paths: ['src/**', 'tools/ArturRios.Heimdall.OpenApiGen/**', 'scripts/openapi.py',
                     'docs/openapi/**', 'ci/check-openapi.sh']],   // path-filtered today, so it stays filtered
])
```

The key is the check name reported to GitHub. Per check:

| Field | Meaning |
|---|---|
| `image` | Container image the step runs in, pinned by tag (digest pinning is the builder's call, matching how each repository pins its actions today). |
| `run` | Shell command or script, run from the repository root. Steps move out of the workflow YAML into scripts under `ci/` so they can also be run by hand. |
| `docker` | `true` gives the step the CI machine's Docker engine: image builds and Testcontainers (heimdall-api and fortuna-api functional tests start PostgreSQL). Default `false`. |
| `credentials` | Environment variable → Jenkins credential id (secret text), for the few secrets checks use today (the ReportGenerator license). |
| `paths` | Optional globs; the check runs only when the change touches them (fortuna-api native core). Compared against the target branch for pull requests, and against the previous build's commit for branch builds. |
| `junit`, `archive` | Optional: test results for Jenkins' test report, files to keep with the build (coverage reports, built packages). What Actions uploads as artifacts today becomes `archive`. |

Checks of one build run in parallel; the build fails if any fails, after all have reported.

A Jenkinsfile without `checks` keeps today's behavior exactly (deploy only, wait for every Actions check on
releases). This lets the library ship before any application moves, and lets each application move on its
own.

### Branch policy

- One script, `scripts/branch-policy.sh <head-ref> <base-ref> <head-sha>`, with today's rules unchanged
  (the workflow's shell, which is identical across repositories): into `develop` only `feature/*`, `fix/*`
  or `dependabot/*`, not cut from `main`; into `main` only `release/x.y.z`, an unchanged snapshot of
  `develop`, for an untagged version. It needs the full history of `develop` and `main` and the remote tags.
- The shared pipeline runs it on every pull request into `develop` or `main`, on the CI agent, and reports
  `branch-policy`. Pull requests into other branches report nothing, as today.
- **Application extras.** Chimera adds steps to its branch policy (a Dart branch-origin check, release
  readiness, and validation of its own entry in yggdrasil's catalog). The Jenkinsfile can pass
  `branchPolicy: [image: …, run: …]`, run after the common script with `HEAD_REF`, `BASE_REF`, `HEAD_SHA`
  and the yggdrasil checkout's path in its environment; both must pass for `branch-policy` to succeed.
- Today's workflow also reruns when a pull request's base is changed (`edited`). The builder confirms that a
  retargeted pull request gets rebuilt in Jenkins (rescan or webhook); if not, document that a retarget
  needs a push or a "Build Now".

### Reporting

- Each check, and `branch-policy`, reports through `scripts/github.sh set-status` (the commit statuses API,
  which Jenkins already uses for `deploy/<environment>`): `pending` when it starts, then `success` or
  `failure`, with the build URL as target. Contexts are the check names.
- Statuses are reported on the commit the build ran: the pull request's head (pull request discovery already
  uses the head revision) or the pushed commit.
- **Reuse by commit.** A check whose context already has `success` from Jenkins on the same commit is not
  run again (the status is per commit and the commit is immutable). This makes a `release/x.y.z` push and
  its pull request, which are usually exactly the `develop` head already checked, cost nothing.
  `branch-policy` is never reused: it depends on the pull request's refs, not only the commit.

### Pipeline flow

`yggdrasilPipeline` gains a `checks` stage in front of what it does today:

| Build | Today | After |
|---|---|---|
| Pull request into `develop` | nothing | branch-policy + checks |
| Pull request into `main` (not `release/x.y.z`) | nothing | branch-policy + checks (branch policy fails it) |
| Pull request `release/x.y.z` → `main` | wait for Actions, deploy release environments, merge, release | branch-policy + checks, then wait for the **remaining** Actions checks (with the skip), then as today |
| Push to `develop`/`main`/`release/*` | deploy matching branch environments | checks, then deploy only if green |
| "Build with Parameters" `DEPLOY_TO` | deploy | unchanged: no checks (an explicit human action; builder may revisit) |

- Checks run on `node('ci')`; deploys stay on the environment's agent. A build never holds a deploy agent
  while checks run.
- The release lock (`release-${app}`) covers the release pull request's checks too, as it covers
  everything after them today.
- The multibranch jobs must discover `develop` even when no environment's `branches` names it (today the
  heads filter is `main`, `PR-*` and branch globs only). Feature branches stay undiscovered as branches;
  they're built as pull requests.

### Waiting for Actions, and the skip

`scripts/github.sh wait-checks` changes:

- It runs after Jenkins' own checks have passed, so by then Actions has had minutes to queue its workflows.
- Proof that Actions has started: today it is the `branch-policy` check run, which now comes from Jenkins.
  Instead, every name in the application's `actionsChecks` (catalog, passed by the pipeline) must appear
  and complete. Applications with no `actionsChecks` wait only for whatever GitHub Actions runs exist on
  the commit (fortuna-api's path-filtered Windows build, for one). The small race where an Actions workflow
  hasn't registered yet for such an application is accepted (builder may add a short grace period).
- It still considers only GitHub Actions check runs, so Jenkins' own statuses never count.
- **Distinct exit codes**: `0` all passed, `1` an Actions check failed, `2` timed out (`checksTimeout`) or
  GitHub stopped answering.

In `release()`:

- Exit `1` fails the release, as today.
- Exit `2` leaves the agent, then asks: **"GitHub Actions checks didn't finish. Deploy without them?"**
  with a **Deploy without GitHub Actions checks** button, outside `node()` like the approval inputs. Abort
  fails the release.
- After a skip the release proceeds exactly as today. It is recorded in the build log, in the build
  description (`v1.4.0 (deployed without GitHub Actions checks)`), and in the `deploy/<environment>`
  status descriptions (`Deployed to Production without GitHub Actions checks`).
- The skip covers only the release pull request; it isn't offered on other builds, which don't wait for
  Actions.

### Catalog

- `checks` now means **the checks Jenkins reports**, required on `develop` and `main`. Names must match the
  keys of the application's Jenkinsfile `checks`. On a release, the pipeline fails early with a clear
  message if a catalog check isn't declared in the Jenkinsfile (otherwise the ruleset would wait forever).
- New `actionsChecks`: GitHub Actions job names still required on `develop` and waited for on releases
  (Chimera: `Safari`, `Native (windows)`). Only checks that run on every pull request, as `checks` today.
- `checksTimeout` keeps its meaning, applied to the wait for Actions.
- yggdrasil itself: its repository isn't an application. Its checks and `actionsChecks` go in the catalog
  at top level (builder's call on the exact shape) so the Job DSL can create a checks-only `yggdrasil` job
  and `rulesets.py` can apply its rules. Its Jenkinsfile calls the shared pipeline without a `stack` (or a
  sibling entry point, builder's call) and never deploys.
- `scripts/catalog.py` and `yggdrasilPipeline.groovy`'s `resolve` stay in step, as their comments require,
  and `catalog.py validate` checks the new fields.

### Rulesets (`github/rulesets.py`)

- `develop`: `branch-policy` and `checks` (from Jenkins), plus `actionsChecks` bound to GitHub Actions'
  integration id.
- `main`: `branch-policy`, `checks` and `deploy/<environment>`; **no** Actions checks, so a release
  deployed with the skip can merge.
- Jenkins' contexts are **not bound** to an integration, like `deploy/<environment>` today, so the GitHub App
  can be replaced (and so phase 2 doesn't depend on an app id). Anyone with write access could post a
  matching status by hand; with a single maintainer that is accepted.
- Tags and repository settings unchanged.

### Removing the old workflows

In each repository, the workflow files (or jobs) that moved are deleted in the same change that adds the
Jenkinsfile `checks`, so a check is never reported by both systems under the same name. Workflows that
keep a Windows/macOS job keep only that job. Unchanged: `build.yml`, `release.yml`, `release-artifacts.yml`,
`build-docs.yml`, Dependabot.

heimdall-api's `build-docs.yml` today downloads the coverage report from the Tests run, and Tests
calls it (`workflow_call`) on pushes to `main`. Once Tests is gone, `build-docs.yml` runs on every push to
`main` (its `paths` filter goes, since source changes move the coverage numbers) and runs the test suite
itself to produce the report before building the site.

Chimera's own contract tests (and `tool/validate_yggdrasil_contract.py`) name `Cross-platform` and the
workflow files. They change in Chimera's cutover pull request, together with Chimera's catalog entry in
yggdrasil, so the two repositories agree at every point.

## Rollout

Order matters, because the library is shared and the rulesets are strict:

1. **yggdrasil**: library support for `checks`, branch policy, reporting, the new `wait-checks`, the skip,
   the catalog fields, the CI agent and its setup docs, `rulesets.py`. A Jenkinsfile without `checks`
   behaves as today. Released as a normal yggdrasil release; the controller's library version is moved to
   it.
2. **Bring up the CI machine** and confirm the `ci` agent connects.
3. **Per application, one at a time**: a `feature/` pull request that adds `ci/` scripts and the Jenkinsfile
   `checks`, deletes the moved workflows, and the catalog change for that application (in yggdrasil).
   Then run `rulesets.py <repo>`. The cutover pull request itself can't satisfy both the old and the new
   required checks; the owner merges it with the admin bypass after checking Jenkins' results on it.
   Suggested order: heimdall-api, heimdall-ui, fortuna-api, fortuna-ui, chimera (most complex last).
4. **yggdrasil's own CI** last, the same way.
5. The first release of each application after its cutover is the end-to-end test: checks, the wait for any
   Actions checks, deploys, merge.

## Testing

- `scripts/branch-policy.sh`: unit tests over a scratch repository for each rule and each rejection,
  matching today's messages (the repository already tests its shell scripts from Python, e.g.
  `scripts/test_github.py`).
- `wait-checks`: the existing test approach for `github.sh` extended to the new proof and the three exit
  codes, including GitHub not answering.
- `rulesets.py`: `--dry-run` output for an application with and without `actionsChecks`, and for yggdrasil.
- `catalog.py`: validation of `checks`/`actionsChecks` and the top-level yggdrasil entry.
- The pipeline: a build of each kind on one real application before moving the rest (step 3's first
  repository), including a forced Actions timeout to exercise the skip.
- CI isolation: confirm from inside a check that the VPS's Docker engine isn't reachable.

## Documentation

- `README.md`: the diagrams and text that say "Actions: build + test" and "wait for every GitHub Actions
  check".
- `docs/catalog.md`: `checks`, `actionsChecks`, `checksTimeout`, the `release` trigger's description.
- `docs/setup.md`: the CI machine, the CI agent, credentials checks use, the per-repository preparation in
  section 5, and section 12's rulesets.
- The header comments of `yggdrasilPipeline.groovy`, `github.sh` and `rulesets.py`, and each application's
  Jenkinsfile comment ("Build and test stay in GitHub Actions; this file only deploys").
- `CHANGELOG.md`.

## Left to the builder

- Exact check names for Chimera's split checks, and the top-level catalog shape for yggdrasil.
- How checks run in parallel on the CI agent (one executor per check, or parallel steps on one node),
  within the 4 executors.
- How Testcontainers inside a check reaches the containers it starts (socket mount plus host override, or a
  shared network).
- Jenkins plugins the reporting needs (test results, archived files).
- Image pinning style.
