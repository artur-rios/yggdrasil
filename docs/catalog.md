# The catalog

`catalog.yaml`, at the root of the repository, is the single description of an installation: the
environments it deploys to, the systems it manages, and the applications each system is made of.
Every part of yggdrasil reads it:

| Reader | What it takes from the catalog |
|---|---|
| Jenkins: `casc.yaml` Job DSL | One multibranch job per application and per [`repositories`](#repositories) entry (deploys, checks, releases), the branches each builds and, for an `owner-tag` entry, its tags; the `actions-release` and `release-resume` jobs, and the daily `tag-check` job, which checks the release tags of every entry that releases ([setup.md](setup.md#version-tags-tag-check-and-repairs)) |
| Jenkins: `init.groovy.d/agents.groovy` | One agent per distinct `agent` of the environments Jenkins deploys to: one per host; and the `ci` and `release` build agents, always (so no environment's `agent` may be called either) |
| Jenkins: the shared pipeline | What each push or pull request deploys, where, and with which options; with the repository's `yggdrasil.yml`, which checks and releases it gets ([The pipeline](setup.md#the-pipeline)) |
| `scripts/pipeline_config.py` | Reads each repository's `yggdrasil.yml` and checks it against its catalog entry: credentials, checks, release, publish ([Repository pipeline file](#repository-pipeline-file)) |
| `scripts/deploy.sh` | Whether the application deploys to that environment, its mode, timeouts, rollback depth and whether it runs on demand |
| `scripts/platform.sh` | That every environment in the host's `ENVIRONMENTS` exists; the GitHub owner and repository for Jenkins |
| `ygg` (`scripts/host.sh`) | The applications of each of the host's environments; which environments it may stop |
| `scripts/rules.py` (`github/rulesets.py` runs it) | The git server (`forge`), the `repositories`, each repository's required checks and the `deploy/<environment>` statuses on `main`, the users who may merge (`forge.jenkinsUser`, `forge.developers`) and the tag pattern. Applies them on GitHub or Forgejo ([setup.md, step 12](setup.md#12-apply-the-branch-rules)), and with a `mirror` the `mirror tags` ruleset on each repository's GitHub copy under `mirror.github` |
| `scripts/forge.sh` | The git server's type, API address and the owner, when the environment doesn't give them: the commands Jenkins and the scripts use to set statuses, merge, tag, release and delete branches ([setup.md](setup.md#the-git-server-commands)) |
| `scripts/renovate.sh` | The names of the repositories to run Renovate against (`repository-names`) and Forgejo's API address |
| Status API | Which applications run in each of its host's environments, how to probe them, how they group into systems |
| Prometheus | Scrape targets, through the status API |
| Console | Everything it shows, through the status API |

Check it after every edit:

```bash
python3 scripts/catalog.py validate
```

- `validate` prints `catalog.yaml: N environments, N systems, N applications`, or every problem it
  found. CI runs it on every pull request. `deploy.sh`, `platform.sh` and `scripts/rules.py`
  refuse a catalog it rejects.
- The **status API** validates the catalog again when it starts. A catalog that fails its rules
  stops the status API: it lists every problem in its log (`docker logs yggdrasil-status-1`) and
  exits. `validate` checks the same rules (marked *status API* in the tables below), so CI rejects
  such a catalog before any host loads it.
- **Jenkins** doesn't validate it: a broken catalog shows up as a Job DSL or pipeline error.

The catalog is read from each host's checkout when the containers start. After changing it, see
[setup.md](setup.md#where-jenkins-and-the-hosts-read-the-catalog) for how to apply it.

## Top level

```yaml
owner: acme            # GitHub user or organisation of the application repositories and of this one
repository: yggdrasil  # this repository's name under the owner (default yggdrasil)
forge: {...}           # the git server: forgejo or github (default: github, see below)
mirror: {...}          # optional, with forgejo: mirror the repositories to GitHub
repositories: [...]    # optional: repositories that are not applications (libraries, CI-only ones, yggdrasil)
environments: [...]    # at least one, in promotion order
systems: [...]
```

## Git server

The repositories live on one git server: **Forgejo** (the default for new installations) or
**GitHub**. A catalog **without a `forge` block is GitHub**, exactly as before the git server
became configurable, so an existing catalog needs no change.

```yaml
# Forgejo, self-hosted
forge:
  type: forgejo
  url: https://git.example.com   # the server's address
  jenkinsUser: jenkins           # default jenkins
  developers: [claude]           # default []
  publishAgent: vps              # default: the agent of the last release environment
mirror:                          # optional
  github: acme                   # the GitHub user or organisation to mirror to
  releases: true                 # default true
```

```yaml
# GitHub (the same as having no forge block)
forge:
  type: github
```

`jenkinsUser` and `developers` are read on Forgejo only: GitHub's rulesets do not restrict who merges,
so with `type: github` they have no effect.

| Field | Default | Meaning |
|---|---|---|
| `forge.type` | `github` without a `forge` block; required inside one | `forgejo` or `github`. |
| `forge.url` | required for `forgejo` | An `https://` address with a host and no path (`https://git.example.com`, a port is fine). A trailing `/` is dropped; the API is `<url>/api/v1`. Not allowed for `github`: GitHub Enterprise Server is not supported, `github` is always `https://github.com` (API `https://api.github.com`). |
| `forge.jenkinsUser` | `jenkins` | The git server user Jenkins acts as. On Forgejo it is the only one that merges `main` and creates the `v*` version tags of applications (no effect on GitHub). A user name: letters, digits, `.`, `_` and `-`; on GitHub it may end in `[bot]` (a GitHub App's bot login, `my-app[bot]`). It is also whose check results the pipeline reuses (see [Which checks run](#which-checks-run)): on GitHub the statuses of a GitHub App are created by its bot login (`my-app[bot]`), so a GitHub installation reuses results only when `jenkinsUser` is set to that login; otherwise every check runs again. |
| `forge.developers` | `[]` | On Forgejo, users, besides the owner, who may merge pull requests into `develop` (no effect on GitHub). A list of user names. |
| `forge.publishAgent` | the `agent` of the last `trigger: release` environment, over the applications in catalog order (what merges and releases today); none when no environment releases | The Jenkins agent that merges, tags, creates releases and uploads their files: the only one holding the git server's write token (and, for GitHub Actions and a release's GitHub copy, the GitHub App's). An agent name: lowercase letters, digits and dashes, and one Jenkins makes for an environment (an environment's `agent`, or the id of one that deploys by branch or release); never `ci` or `release`, the pipeline's own agents. |
| `mirror.github` | required with a `mirror` | The GitHub user or organisation the repositories are mirrored to. |
| `mirror.releases` | `true` | Whether releases are mirrored as well. `true` or `false`. |

`mirror` is only allowed with `forge.type: forgejo`; any other key in `forge` or `mirror` is an
error. Read them with `python3 scripts/catalog.py forge` / `mirror` (see
[Command line](#command-line)).

## Repositories

`repositories` lists the git repositories under `owner` that are **not applications**: libraries that
publish a package, repositories that only run CI, yggdrasil itself. They get the git server's branch and
tag rules and a Jenkins job like an application's repository does, but nothing is deployed from them.
An application's repository (its `repository`, else its `id`) is listed here only when it is also
a library or a CI repository in its own right, as below.

### One repository, several entries

A repository can back more than one catalog entry, one Jenkins job each: a library (a `repositories`
entry with its checks and NuGet packages) and its docs site (an application with `repository: <the
library>`), or an application and its docs site (two applications with one `repository`).

- **At most one of them releases.** A releasing entry is an application with a `trigger: release`
  environment, or a `repositories` entry whose `release` is not `none`. Two on one repository are an error
  naming both (`repository <repo>: application <a> and repositories entry <b> both release; ...`): who
  merges `main` and who creates the version tags must have one answer.
- **One set of branch rules** ([`scripts/rules.py`](setup.md#12-apply-the-branch-rules)). The deciding entry
  is the releasing one, else the `repositories` entry, else the first application in catalog order. It
  supplies the model (`rules`), who merges `main`, who pushes release branches and the tag pattern. An application
  that decides gets the release model only when it releases: one with no `trigger: release` environment (a site
  deployed on pushes to `main`) gets `basic`, since under the release model only Jenkins merges `main`, on a
  release. Every
  entry's `checks` and `actionsChecks` are required (the deciding entry's first), so each job's statuses
  are required.
- **One `yggdrasil.yml`**, read by every job of the repository; each job runs only its own entry's
  `checks` ([below](#repository-pipeline-file)).

```yaml
repositories:
  - id: dotnet-util               # the library: tests, coverage, NuGet on owner tags
    release: owner-tag
    tags: "[0-9]*"
    checks: [test, coverage]
systems:
  - id: docs
    name: Docs
    applications:
      - id: dotnet-util-docs      # its docs site, built from the coverage report
        repository: dotnet-util
        checks: []
        credentials: [reportgenerator-license]   # its deploy runs coverage, which binds it
        # ...
```

```yaml
repositories:
  # A library: the owner publishes versions by creating tags like 1.4.0 (no leading v).
  - id: dotnet-util
    release: owner-tag
    tags: "[0-9]*"
    checks: [Build and test]
    actionsChecks: [test]
  # A repository that only runs CI: develop and main are protected, nothing is released.
  - id: shop-specs
    checks: [lint]
  # yggdrasil itself: the full branching model, and the owner creates its version tags (v*).
  - id: yggdrasil
    release: owner-tag
```

| Field | Default | Meaning |
|---|---|---|
| `id` | required | The repository name under `owner`: letters, digits, `.`, `_` and `-` (`reveal.js` is fine). Unique, and not the id of any application (`<id> is already an application's id`: each names a Jenkins job). It may be an application's repository: see [One repository, several entries](#one-repository-several-entries). |
| `checks` | `[]` | The checks Jenkins reports on pull requests to `develop` and `main` (and `master`, under `rules: basic`), as for an application's `checks`. No empty entries. |
| `actionsChecks` | `[]` | GitHub Actions job names required besides them on `develop` (not on `main`), as for an application's `actionsChecks`. On Forgejo with a `mirror`, such a job must run on `push` to every branch (`push: branches: ['**']`), without `pull_request` or `paths`: the pull requests are on Forgejo, the mirror pushes every branch, and Jenkins copies the job's result for the pull request's head commit by its SHA ([setup.md](setup.md#github-actions-and-the-skip)). No empty entries. |
| `credentials` | `[]` | The only Jenkins credentials the repository's checks may bind. Jenkins credential ids: letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `releaseCredentials` | `[]` | The only Jenkins credentials its release build and finalize step (`release.build.credentials` and `release.finalize.credentials` in `yggdrasil.yml`) may bind: signing keys and the like. Kept apart from `credentials` because a check runs any branch's code: an id listed here is never bindable by a check, and an id in both lists is an error. Same id format. |
| `release` | `none` | `none`: no versions. `owner-tag`: the owner creates the version tags by hand, matching `tags`. (Applications are different: Jenkins creates their `v*` tags.) |
| `tags` | `v*` | The tag pattern, a glob of letters, digits, `.`, `_`, `-`, `*`, `[...]` classes and `@` (not first): `[0-9]*` for versions without a `v`, `*@*` for a library that versions each package apart (`ArturRios.Data.Sqlite@3.1.0`). Only meaningful with `release: owner-tag`. The release's version (`RELEASE_VERSION`) is the tag without a leading `v`, or what follows the last `@` of an `@` tag; such a release's notes start from the same package's previous tag (`<package>@*`). |
| `rules` | `release` | `release`: the full branching model (protected `develop`, `main` and `release/*`). `basic`: `develop`, `main` and `master` only, without force pushes or deletion; the repository's default branch is left as it is, and the pipeline's `branch-policy` passes for any pull request (`basic: nothing to check`). `master`, a fork's default branch, is treated like `main` everywhere: its job discovers it, a pull request into it gets the branch policy and the checks, a push to it the checks. The release model has no `master`. |

Any other key is an error. Read the list with `python3 scripts/catalog.py repositories` (ids) and one
entry, defaults applied, with `python3 scripts/catalog.py repository-entry <id>`. The git server's rules
(`scripts/rules.py`) and the Jenkins pipeline consume them.

## Repository pipeline file

Each repository describes its own checks and releases in a `yggdrasil.yml` at its root. The file is
**data**: images, commands (usually a script under `ci/`), globs and the *names* of Jenkins credentials.
Jenkins does not read the repository's `Jenkinsfile`: every job runs the one in
[`jenkins/Jenkinsfile.default`](../jenkins/Jenkinsfile.default), kept in Jenkins, and the Jenkins library reads
this file instead. It runs the commands only on the isolated CI agent (checks) or the release agent
(releases). Why, and what happens on each push, pull request and tag: [The pipeline](setup.md#the-pipeline).
On Forgejo the file is a protected file on `develop`: only the owner merges a change to it (Renovate's image
bumps there included), with **Merge anyway** ([step 12](setup.md#12-apply-the-branch-rules)).
A repository **without** a `yggdrasil.yml` is not an error: it behaves as before, it deploys and nothing
else (on GitHub, its release also waits for every GitHub Actions check, as it always did).

**One file per repository, each job its own checks.** When a repository backs several catalog entries
([One repository, several entries](#one-repository-several-entries)), they share its one `yggdrasil.yml`,
and each entry's Jenkins job runs only:

- the checks its catalog entry names in `checks`, in that order;
- when the build deploys (a push that deploys, a release pull request of an application with a release
  environment), the checks `deploy.inputs` names as well, after them ([below](#check-results-in-the-image-build-deployinputs)).

The file's other checks belong to another entry: this job neither runs them nor posts their status. A
library's job runs `test` and `coverage`; its docs site's job (`checks: []`, `deploy.inputs: [coverage]`)
runs only `coverage`, and only when it deploys. The one exception is a **release pull request** of the
entry that releases (an application with a `trigger: release` environment): it deploys production before
it merges, so it runs every check `main` requires of the repository, each entry's `checks` (`describe`'s
`repositoryChecks`, the same list [`scripts/rules.py`](setup.md#12-apply-the-branch-rules) requires), then
its `deploy.inputs`.

A check that no job of the repository ever runs (named by no entry's `checks`, and not a deploy input of an
application on it) is a **warning** in `describe`'s `warnings`, not an error: `checks.<name> is not in any
catalog entry of <repo>: it never runs`. The build shows it in its log and is marked **unstable**; it
checks and deploys as usual.

```yaml
checks:                         # name -> a command run in a container; the name is the status context
  unit:
    image: mcr.microsoft.com/dotnet/sdk:10.0
    run: ci/test.sh             # a command line, run with sh -c in the checkout
    docker: true                # give it the Docker engine (default false)
    credentials:                # ENV_VAR -> Jenkins credential id
      REPORTGENERATOR_LICENSE: reportgenerator-license
    paths: ["src/**", "tests/**"]   # run only when a changed file matches (default: always; never empty)
    junit: "**/TestResults/*.xml"   # test results Jenkins collects
    archive: "out/**"               # files kept as the build's artifacts
branchPolicy:                   # the branch policy check
  image: alpine/git
  run: scripts/branch-policy.sh
deploy:
  inputs: [unit]                # checks whose archives go into the image build
release:                        # applications with a release environment, owner-tagged repositories
  build:   {image: debian:12, run: ci/release.sh, files: ["dist/*.zip"], credentials: {SIGN_KEY: sign-key}}
  windows: {workflow: windows.yml, files: ["Setup.exe"]}   # built by a GitHub Actions workflow
  validate: {image: debian:12, run: ci/validate.sh}
  finalize: {image: debian:12, run: ci/sign.sh, files: ["signed/*"], credentials: {SIGN_KEY: sign-key}}
publish:                        # owner-tagged repositories only
  nuget: {image: mcr.microsoft.com/dotnet/sdk:10.0, run: ci/pack.sh, packages: "out/*.nupkg"}
```

Every section is optional. Unknown keys at any level, and values of the wrong type, are errors that name
the field (`checks.unit.dockr: unknown field`). `image` and `run` are required wherever they appear,
and so is `release.windows.workflow`. `credentials` keys are environment variable names. A check's name is
lowercase letters, digits, `.`, `_` and `-`, starting with a letter or digit, and may not be `branch-policy`;
every `image` is a Docker image reference (`registry:port/name:tag` or `name@sha256:...`: no spaces, and it
may not start with `-`).

`release`'s steps run in this order. `build`, `validate` and `finalize` run on the release agent, each from
the tag's checkout with `RELEASE_TAG` and `RELEASE_VERSION` in its environment; `windows` runs on GitHub Actions
([Releases](setup.md#releases) has the whole flow):

- `build` makes the release's own files; its `files` globs, matched in the checkout, are what it adds to the
  release (each must match a file).
- `windows` is a GitHub Actions workflow, dispatched on the tag on GitHub (the mirror); its `files` (globs over
  the artifacts the release agent gets, default every file) are added too.
- `validate` gets the whole set in `RELEASE_FILES` (`build/` and `windows/<artifact>/` under it) and passes or
  fails: it cannot change what is released.
- `finalize` (optional) runs after `validate` has passed, over the same layout, and makes what must cover the
  whole set: a manifest of every file and its checksum, its signature. Its `files` globs, matched in the
  checkout and validated like `build`'s, are added to the release; its `credentials`, like `build`'s, may only
  name `releaseCredentials`. It may **only add**: the set is checksummed before and after it runs, and a file
  of the set that it changes or removes (or one it adds there instead of in `files`) fails the release. A
  failure stops the release before anything is published; `release-resume` runs it again. It never covers a
  partial set: with `windows` declared, Windows files that are missing (a run that failed or timed out) stop
  the release the same way, until `release-resume` runs all of it again, and the release pull request never
  offers the skip past the GitHub Actions checks ([the skip](setup.md#github-actions-and-the-skip)), nor does
  `actions-release` attach Windows files to it afterwards. It runs in a workspace of its own, from a fresh
  checkout of the tag and of yggdrasil's tools, so nothing `validate` left behind reaches it. A resumed release
  with `finalize` uploads its draft's files again instead of reusing them by name and size, and deletes the
  draft's files that are not in its set. Only `files` reach the release: anything `finalize` writes elsewhere in
  the workspace (the upload set's own directory included) is left out.

Every file the release uploads, from any step, must have its own name (a release shows one file per name):
two files named alike fail the release before anything irreversible, a library's NuGet push included.

The file is also checked against the catalog entry of the repository (the application, or its
[`repositories`](#repositories) entry):

- every credential of a check **this job runs** must be in the entry's `credentials`: its catalog `checks`, an
  application's `deploy.inputs` (a deploy runs them), and for an application that releases, every entry's checks (its
  release pull request runs them). A check only another entry of the repository runs is that entry's to allow: a
  library's docs job is not judged on `test`'s credentials, but is on `coverage`'s, its deploy input. Every
  credential in `release.build.credentials` and `release.finalize.credentials` must be in `releaseCredentials`
  (never the other list: a check runs any branch's code, so a release build's signing key is never a check's);
  none is bound on `renovate/*` builds;
- every name in the entry's `checks` must be defined under `checks`;
- `deploy.inputs` may only name checks defined in the file;
- `publish` is allowed only for a `repositories` entry with `release: owner-tag`. Its packages are pushed
  with the Jenkins credential `nuget-api-key` (a fixed id, configured by `NUGET_API_KEY_FILE`), which the
  pipeline binds only on the release agent around `scripts/nuget-push.sh`: it is never a check's or a
  release build's, whatever `credentials` or `releaseCredentials` say ([Owner tags and NuGet](setup.md#owner-tags-and-nuget));
- `release` is allowed for an application with a release environment and for `release: owner-tag`.
- `release`, `publish` and the release's credentials are judged only for the repository's **releasing** entry; the
  other entries sharing the file (a library's docs site) skip them. When no entry of the repository releases, every
  one is judged on them, so the file cannot declare a release nobody makes.

Read the entry and check a checkout with `python3 scripts/pipeline_config.py describe <job-id> <repo-dir>`
(`job-id`: the application's id or the `repositories` entry's id). It prints one JSON document,
`{"entry", "config", "errors", "warnings", "repositoryChecks"}`, where `config` is `null` when there is no
file, `repositoryChecks` lists every check `main` requires of the repository (each catalog entry's), and it
exits `1` when `errors` is not empty (`warnings` never changes the exit status). The entry has `kind` (`application`
or `repository`), `id`, `repository`, `credentials`, `releaseCredentials`, `checks`, `actionsChecks`, `release` (`deploy` for an application with a release
environment, `owner-tag`, or `none`) and `tags` (`v*` for applications).

### Which checks run

For a build of a branch the pipeline asks `python3 scripts/pipeline_config.py plan <job-id> <repo-dir>
--statuses <file> --changed <file|-> --branch <name> [--deploying] [--release]` what to do with each check
the job needs, and gets a JSON list: `{name, action, reason, image, run, docker, credentials, junit,
archive}`. The checks are the entry's `checks` in catalog order (with `--release`, the releasing job's
release pull request: `repositoryChecks` instead), then, with `--deploying`, the `deploy.inputs` not among
them; each once, and nothing else of the file. `--release` for a job that does not release is an error.

- `--statuses` is the output of `forge.sh get-statuses` for the commit (`<context>`, `<state>`, `<creator>`,
  tab separated); `--changed` lists the changed files, one path per line, or is `-` when they are unknown;
  `--deploying` says the build deploys afterwards; the flags come last, in any order.
- **reuse**: the commit already has a `success` for the check created by `forge.jenkinsUser`. A success
  created by anyone else is not trusted, and neither is a failure or a pending: those checks `run`.
- **skip**: the check has `paths`, the changed files are known and none matches. `*` stays in one directory,
  `**` crosses directories (`src/**` matches `src/a/b.cs`, `docs/*.md` does not match `docs/a/b.md`, `**/*.md`
  matches `README.md`). When the changes are unknown (`-`) the check runs, and so it does when `yggdrasil.yml`
  itself changed: an image bump or a new command must run before it is merged.
- **run**: everything else. On a `renovate/*` branch `credentials` is always `{}`.

Reuse works across the jobs of one repository: a `coverage` success posted by the library's job is reused by
its docs site's job on the same commit. A deploy input is the exception when the build deploys: it runs
anyway, since its archive goes into the image.

The branch policy is not a check of the file and is never reused. The command exits `1`, naming the
problems, when the file is invalid or breaks the catalog rules above.

Only a status created by `forge.jenkinsUser` counts for **reuse**. On GitHub the statuses Jenkins posts are
created by the GitHub App's bot login (`<app>[bot]`, the App's slug and `[bot]`), so a GitHub installation
reuses results only when `forge.jenkinsUser` is set to that login; otherwise every check runs again, which is
the safe way to be wrong.

### Check results in the image build (`deploy.inputs`)

A site's image sometimes needs what a check produced (a rendered documentation tree, say). List the check's
name under `deploy.inputs`; the check needs an `archive`. On a push that deploys (`develop`, `main` or
`release/*`) and on a release pull request (its deploys to the release environments), that check runs even if
its result could be reused or its `paths` skipped it, its archive is
handed to the deploy agent, and `scripts/deploy.sh` copies the files into the application's checkout, the build
context of every stack, keeping their names (`DEPLOY_INPUTS_DIR`). A name that already exists in the
repository stops the deploy before anything is copied: inputs never overwrite repository files (a directory
may merge into one). Only `DEPLOY_TO` builds ("Build with Parameters", which run no check) deploy with none,
so a stack that needs them cannot build there.

## Templates for repositories

`templates/` has a starting point for each kind of repository a catalog entry can back. Copy the files named in the
template's `README.md` to the repository, and the stack files from `templates/stacks/` to this repository's
`stacks/`. Every image in them is pinned by version and digest (`name:version@sha256:...`), and `renovate/default.json`
(below) keeps the pins up to date.

| Template | For | Files |
|---|---|---|
| `templates/application/` | a deployed application: `yggdrasil.yml` and the `CONTRIBUTING.md` of the release model | `yggdrasil.yml`, `CONTRIBUTING.md` |
| `templates/site-static/` | a site that is already built (a directory of files) | `Dockerfile`, `yggdrasil.yml`, `README.md` |
| `templates/site-hugo/` | a Hugo site | the same |
| `templates/site-vite/` | a Vite single-page app | the same |
| `templates/site-next-static/` | a Next.js static export | the same |
| `templates/site-next-server/` | a server-rendered Next.js site | the same, and `app/healthz/route.js` |
| `templates/site-node-api/` | a Node.js service (github-readme-stats) | the same |
| `templates/library/` | a .NET library and its docs site | `ci/test.sh`, `ci/coverage.sh`, `ci/pack.sh`, `ci/branch-policy.sh` (the release version gate, for a single-package library), `yggdrasil.yml`, the docs site's `Dockerfile`, `.dockerignore`, `.gitattributes`, `README.md` |
| `templates/stacks/` | the site stack files, copied to `stacks/<application-id>.yml` and `.proxy.yml` | `site.yml`, `site.proxy.yml`, `site-path.proxy.yml`, `site-apex-redirect.proxy.yml` |

**What every site image does.** It listens on port 8080 as a non-root user and answers `/healthz` with 200, which
is the catalog's `health` (`http://<id>:8080/healthz`). The static ones (static, Hugo, Vite, Next static, the
library's docs) are the [static-web-server](https://static-web-server.net/) image, pinned, with no shell: the site
is built in a pinned builder stage (Hugo, Node) and only copied into it, under the application's `path`, since the
proxy does not strip the prefix; `/healthz` is a file of its own. Server-rendered Next.js and the Node service run
`node` as the image's `node` user, with a `HEALTHCHECK`.

**Build arguments.** A site's Dockerfile takes the values Vercel gave its build as build arguments, which the
stack file `site.yml` passes in `build.args` from what `scripts/deploy.sh` exports:

| Build argument | Value | Replaces |
|---|---|---|
| `GIT_COMMIT` | the whole SHA of the checkout being deployed (empty when it is not a git checkout) | `VERCEL_GIT_COMMIT_SHA` |
| `YGG_ENVIRONMENT` | the environment being deployed to | `VERCEL_ENV` |
| `PUBLIC_HOST`, `PUBLIC_PATH` | `YGG_PUBLIC_HOST` and `YGG_PUBLIC_PATH`, the catalog's `host`/`domain` and `path` (empty without them) | the site's URL: Hugo's `baseURL` is `https://<host><path>/`, Vite's `base`, Next's `basePath` |

They are public values, compiled into the image: a secret is the application's variable, never a build argument.

**Stack templates.** In `site.yml` (the service, `web`) and one overlay, copied with `APPLICATION_ID` replaced by the
application's id:

- `site.proxy.yml`: the router `Host(${YGG_PUBLIC_HOST})`;
- `site-path.proxy.yml`: `Host(...) && (Path(${YGG_PUBLIC_PATH}) || PathPrefix(${YGG_PUBLIC_PATH}/))`, without stripping the
  prefix, since the site is built for it;
- `site-apex-redirect.proxy.yml`: the same router on `www.`, and one more on `YGG_APEX_HOST` with a permanent
  `redirectRegex` to the `www.` address. `scripts/deploy.sh` exports it from the catalog (`python3 scripts/catalog.py
  apex-host <id> <environment> <default-domain>`): the bare domain in an environment without a `hostSuffix`,
  `apex<hostSuffix>.<domain>` in the others (`apex-dev.artur-rios.tech`, to `www-dev.`), since the environments of a
  host share one bare domain.

**A library.** `templates/library/` is the file for two catalog entries ([One repository, several
entries](#one-repository-several-entries)): the library's `test` and `coverage` checks, its `publish.nuget`, and the
docs application's `deploy.inputs: [coverage]`. `ci/coverage.sh` makes ReportGenerator's PRO report when
`REPORTGENERATOR_LICENSE` is bound, and the free report, saying so, when it is not (a `renovate/*` build binds no
credential).

**Renovate.** `renovate/default.json` is the shared preset, which a repository's `renovate.json` extends with
`{"extends": ["local>artur-rios/yggdrasil//renovate/default"]}`. It extends `config:recommended`, targets
`develop` (a repository on `main` or `master`, with no `develop`, overrides that with
`"baseBranchPatterns": ["main"]` in its own `renovate.json`), covers nuget, npm, github-actions and docker
(Dockerfiles and Compose files), groups the NuGet packages the way the repositories' `dependabot.yml` did, and
adds a regex manager for the `image: <name>:<tag>@sha256:<digest>` lines of `yggdrasil.yml`. Those image
bumps wait for the owner to merge them: `yggdrasil.yml` is a protected file on `develop`
([step 12](setup.md#12-apply-the-branch-rules)).

`scripts/test_templates.py` keeps all of it honest: every template's `yggdrasil.yml` validates, every image is pinned,
the stack templates render under `docker compose config`, the library scripts run against a fake `dotnet`, and the
preset's regex matches a pinned image line. With `YGG_TEMPLATE_TESTS=1` (and Docker) it also builds each site
template around a tiny sample site and checks `/healthz` and a deep link, and runs Renovate's own config validator
on the preset.

## Environments

An environment is a place applications run: a laptop, a staging server, production. A host (a
machine, a VM, a WSL distribution: anything with its own Docker engine) runs one environment or
several. The shipped catalog has both: `local` on a developer's Docker Desktop, and `development`,
`homologation` and `production` together on one VPS
([example](examples/docker-desktop-and-vps/README.md)).

```yaml
environments:
  - id: local
    name: Local
    mode: ports
    trigger: manual

  - id: development
    name: Development
    trigger: branch
    branches: develop
    agent: vps
    hostSuffix: -dev
    onDemand: true

  - id: homologation
    name: Homologation
    trigger: branch
    branches: release/*
    agent: vps
    hostSuffix: -hml
    onDemand: true

  - id: production
    name: Production
    trigger: release
    agent: vps
```

### Several environments on one host

Every part of yggdrasil names what it creates after the environment as well as the application,
so the environments of one Docker engine never touch each other:

| What | Name | Example |
|---|---|---|
| Compose project | `<application>-<environment>`, set by `deploy.sh` | `heimdall-api-production` |
| Network alias on `edge` and `telemetry` | `<application>.<environment>`, set by the stacks' proxy overlays from `YGG_ENVIRONMENT`, which `deploy.sh` exports. There is no plain `<application>` alias | `heimdall-api.production` |
| Health and metrics address | The catalog's `health` and `metrics` keep the plain id as host; the status API and Prometheus qualify it with the environment for every application that isn't `kind: platform` | `http://heimdall-api.production:8080/healthcheck`, `heimdall-api.production:9464` |
| Traefik routers and services | `<application>-<environment>`, in the stacks' proxy overlays | `heimdall-api-production`, `heimdall-api-production-same-origin`, `heimdall-api-production-auth` |
| Image tag | `<environment>-<version>`. The same commit is built once per environment (a web front end compiles environment values into its image); rollback and pruning only look at this environment's tags. The `yggdrasil.version` label stays the version alone | `heimdall-ui:production-1.4.0-3f2a9c1` |
| Container label | `yggdrasil.environment`, on every container `deploy.sh` starts; Alloy makes it the `environment` label of the logs in Loki | `yggdrasil.environment=production` |
| Env file | `<secrets>/<environment>/<application>.env` on a host without a [variables store](variables.md); with one, the `<application>@<environment>` scope | `/etc/yggdrasil/production/heimdall-api.env` |
| Public host name | The application's `host` plus the environment's `hostSuffix`, under the host's `DOMAIN` | `heimdall-dev.example.com` |

Per host, not per environment:

- **One platform stack** (Traefik, status API, console, Prometheus, Loki, Grafana), Compose project
  `yggdrasil`. `ENVIRONMENTS` in its `platform.env` lists the environments the host runs,
  comma-separated (`development,homologation,production`). A `platform.env` from before 0.5 with
  only `ENVIRONMENT` still works, as a list of one; `ENVIRONMENTS` wins when both are set. The
  status API reports every environment of the list; platform components appear once, in every
  environment's report.
- **One `DOMAIN`**, one wildcard certificate and one DNS record, `*.DOMAIN`. The environments of a
  host differ by `hostSuffix`: `heimdall-dev.example.com`, `heimdall-hml.example.com`,
  `heimdall.example.com`. The suffix is part of the one label under `DOMAIN` because the wildcard
  certificate doesn't cover a second level (`heimdall.dev.example.com`).
- **One Jenkins agent**: give the environments of the host the same `agent` (`vps` above). The
  agent has two executors; deploys of one application never overlap, whatever the environment.

One environment per host still works the same way: leave `hostSuffix` empty and give each host its
own `DOMAIN` (`example.com`, `staging.example.com`).

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Lowercase letters, digits and single dashes, starting and ending with a letter or digit (`dev`, `pre-prod`). Names the env files on hosts (`<secrets>/<id>/<app>.env`), the entries of the host's `ENVIRONMENTS` in `platform.env`, the Compose projects (`<app>-<id>`), the image tags (`<id>-<version>`) and the `deploy/<id>` status. |
| `name` | required | Display name: Jenkins stages, the console. |
| `mode` | `proxy` | `proxy`: routed by Traefik with TLS on the host's `DOMAIN`, no host ports (`stacks/<app>.proxy.yml`). `ports`: published on the host's `127.0.0.1`, no Traefik (`stacks/<app>.ports.yml`); for a laptop. Two `ports` environments on one host would publish the same ports. |
| `trigger` | `manual` | What deploys here (see [Triggers](#triggers)). |
| `branches` | — | For `trigger: branch`, required. A glob or a list of globs: `release/*`, `[develop, hotfix/*]`. Use only `*`, which matches any characters, `/` included: `?` and `[...]` aren't matched the same way by every reader. Case-sensitive. Besides `main` and pull requests, these are the only branches Jenkins discovers for the application. |
| `agent` | the id | Label of the Jenkins agent that deploys here. The agent runs on the environment's host: environments that share a host share its agent, so give them the same `agent` (`vps`). |
| `approval` | `false` | `true`: Jenkins waits for someone to click **Deploy** before deploying here, without holding an agent while it waits. Applies to every deploy to this environment: branch, manual and release. Any signed-in Jenkins user can approve. A build still waiting after 4 hours, the pipeline's overall limit, is aborted. |
| `waitTimeout` | `300` | Seconds `deploy.sh` waits for the new containers to be healthy before rolling back to the image that was running, if there is one. `DEPLOY_WAIT_TIMEOUT` overrides it for one run by hand. |
| `keepImages` | `3` | Images of each application kept on the host after a successful deploy, the running one included: what rollbacks use. `DEPLOY_KEEP_IMAGES` overrides it for one run by hand. |
| `checksTimeout` | `3600` | For `trigger: release`: seconds Jenkins waits for the GitHub Actions checks of the release pull request (without a `yggdrasil.yml` on GitHub, every one; with it, the `actionsChecks`) before it fails the build or, with a `yggdrasil.yml`, offers to deploy without them ([the skip](setup.md#github-actions-and-the-skip)). It is also how long a Windows run of `release.windows` may take. Only the value on the application's **first** release environment counts. |
| `hostSuffix` | `""` | Appended to every application's `host` in this environment: `heimdall` with `-dev` is `https://heimdall-dev.<DOMAIN>`. How several environments share one host, one `DOMAIN`, one `*.DOMAIN` certificate and one `*.DOMAIN` DNS record. Lowercase letters, digits and dashes, not ending with a dash (*status API*); `<host><hostSuffix>` must stay one DNS label of at most 63 characters (*status API*). It only changes the console's link: the router is `PUBLIC_HOST` (or `UI_HOST`) in the env file, which `ygg` writes with the suffix. |
| `onDemand` | `false` | `true`: the environment runs only while it is used. `ygg env start <id>` and `env stop <id>` turn it on and off. The status API reports its stopped applications as `stopped`, which is not a problem, instead of `down`. A deploy of an application that wasn't running (switched off, or never deployed) starts it, waits for it to be healthy (rolling back as usual if it isn't), and stops it again, so a push to `develop` doesn't switch development on. A Jenkins **`DEPLOY_TO`** build, or `DEPLOY_START=1 scripts/deploy.sh ...`, leaves it running. |

Any other key is an error. Numbers must be whole and at least 1; `approval` and `onDemand` are
`true` or `false`.
An application that switches an environment to `trigger: branch` needs `branches` from its override
or from the environment.

The list order is the **promotion order**: the order release environments deploy in on a release
pull request, and the order a push deploys to several matching `branch` environments.

Jenkins serializes the complete release flow per application across all `release/*` branch jobs:
checks, promotion, merge, artifact publication and cleanup. A second release for the same
application waits instead of replacing production while the first release is still publishing.

### Triggers

| Trigger | Deploys | Typical use |
|---|---|---|
| `manual` | Only when asked: `scripts/deploy.sh` on the host, or **Build with Parameters → `DEPLOY_TO`** in Jenkins, which leaves an `onDemand` environment running (`DEPLOY_TO` also offers `branch` environments, to deploy any discovered branch there and switch it on). `DEPLOY_TO` exists on the branches Jenkins discovers (`main`, and those matching a `branch` environment's globs), once the branch has been built once. Jenkins creates an agent for a manual environment only when it sets `agent`; without one, it's deploy-by-hand only, and a `DEPLOY_TO` build would wait forever for a node. | A laptop; an environment for demos. |
| `branch` | The pushed commit, whenever a branch matching `branches` is pushed. `release/x.y.z` deploys as version `x.y.z`; any other branch as its name, with characters other than letters, digits, `_`, `.` and `-` replaced by `-` (`feature/x` → `feature-x`). The image tag is `<environment>-<version>-<7-character commit>`. In an `onDemand` environment, an application that wasn't running is stopped again after the deploy. **Build with Parameters → `DEPLOY_TO`** deploys any discovered branch here by hand, and leaves it running. | Staging from `release/*`; a dev server from `develop`. |
| `release` | The head of a `release/x.y.z → main` pull request, once its `branch-policy` and checks have passed and its `actionsChecks` too (or the owner chose to go on without them). Jenkins marks every `deploy/<id>` pending, then deploys the release environments one after another in catalog order, setting `deploy/<id>` after each. After the last one succeeds, it merges the pull request, tags `vx.y.z`, builds, validates and uploads the release files (`release` in `yggdrasil.yml`) and publishes the release on the git server and, with a `mirror`, on GitHub. Only then does Jenkins complete cleanup. Pushing to the branch meanwhile fails the build. On GitHub, a repository without a `yggdrasil.yml` waits for every GitHub Actions check instead and, with `releaseArtifacts: true`, has its release-artifacts workflow publish the release. The whole flow: [The pipeline](setup.md#releases). | Production; a pre-production environment right before it. |

An application with no `release` environment is never deployed or merged by Jenkins on a release
pull request. Its release pull requests are merged by hand, and its `main` ruleset requires no
`deploy/` status.

## Systems and applications

```yaml
systems:
  - id: shop
    name: Shop
    description: The online store
    applications:
      - id: shop-api
        name: Shop API
        kind: api
        health: http://shop-api:8080/healthz
        metrics: shop-api:9464
        host: shop-api
        checks: [test, docker]
      - id: shop-web
        name: Shop web
        kind: web
        health: http://shop-web:8080/healthz
        host: shop
        checks: [build]
        environments:
          homologation: {}
          production: { approval: true }
```

### System fields

| Field | Meaning |
|---|---|
| `id` | Required. Lowercase letters, digits and dashes. Unique (*status API*). |
| `name` | Required (*status API*). Shown by the console. |
| `description` | Optional. Shown by the console. |
| `applications` | The applications that make up the system: at least one (*status API*). |

### Application fields

An application's environment variables are not catalog fields: they come from the host's [variables store](variables.md) (`<application>@<environment>`, `<application>` and `@<environment>` scopes), or from `<secrets>/<environment>/<application>.env` on a host without one.

| Field | Meaning |
|---|---|
| `id` | Required. Unique across the catalog. It is also the repository name (unless `repository` says otherwise), the Jenkins job, and with the environment the Compose project (`<id>-<environment>`, `deploy.sh` names it so). Its Compose files must tag the images they build with the tag `deploy.sh` passes (`<id>:${IMAGE_TAG}`, which is `<environment>-<version>`; `API_IMAGE_TAG` works too): those are the images it rolls back and prunes, while other services' images (a database's) are left alone. Its proxy overlay gives the service the network alias `<id>.${YGG_ENVIRONMENT}` on `edge` and `telemetry`, which the qualified `health` and `metrics` use. |
| `name` | Required (*status API*). Display name. |
| `kind` | Required. `api`, `web`, `worker` or `platform`, shown by the console. `platform` marks yggdrasil's own components (Traefik, Prometheus...). They are started by `platform/compose.yml` through `scripts/platform.sh`, not by the catalog: their entries only let the status API probe them and Prometheus scrape them. Jenkins creates no job, agent or ruleset for them. |
| `repository` | Repository name under `owner`: letters, digits, `.`, `_` and `-` (*status API*). Default: the id; none for `platform` components. |
| `health` | Required. Absolute URL the status API probes over the Docker networks, e.g. `http://shop-api:8080/healthz`, written with the plain id as host. A 2xx answer is healthy. For an application that isn't `kind: platform`, the status API probes it in each environment at `<host>.<environment>` (`http://shop-api.production:8080/healthz`). |
| `metrics` | `host:port` Prometheus scrapes, if the application exposes metrics (*status API*: must be `host:port`). Qualified like `health`: `shop-api.production:9464`, one target per environment, with an `environment` label. |
| `metricsPath` | Path of the metrics endpoint when it isn't `/metrics`. Starts with `/`, and needs `metrics` (*status API*). |
| `host` | A lowercase host name: letters, digits and dashes (*status API*). The link the console shows: `https://<host><hostSuffix>.<domain><path>`, with the environment's `hostSuffix`, the application's `domain` (else the host's `DOMAIN`) and `path`. It does **not** route anything: the Traefik router is in `stacks/<id>.proxy.yml`, usually from `PUBLIC_HOST` in the application's env file. Keep the two in step. Use one label (`shop`, not `api.shop`): the wildcard certificate `*.DOMAIN` doesn't cover `api.shop.example.com`. Omit it for applications that aren't public. |
| `checks` | The checks Jenkins reports on the pull requests to `develop` and `main`, besides `branch-policy`. They are required there. Only list checks that run on **every** pull request: one that doesn't run would leave its required check pending forever (a `paths` filter in `yggdrasil.yml` makes Jenkins report such a check as skipped, which satisfies it). No empty entries (*status API*). Each name is defined under `checks` in the repository's `yggdrasil.yml`. On a GitHub installation whose repository has no `yggdrasil.yml` yet they are still the job part of `<workflow> / <job>` in a pull request's Checks tab, and `scripts/rules.py` would bind them to no app: [adopt `yggdrasil.yml` first](setup.md#an-existing-github-installation). |
| `actionsChecks` | Optional list, default `[]`. GitHub Actions job names that are required too, in addition to `checks`, on pull requests into `develop` only (`main` requires only what Jenkins reports): the job part of `<workflow> / <job>` in a pull request's Checks tab. A job moved here from `checks` is no longer required on `main`. On GitHub the required check is bound to GitHub Actions, so only checks from it satisfy them (`checks`, Jenkins', are bound to no app); on Forgejo they are required by name, like the others. The same rule applies: only checks that run on **every** pull request. On Forgejo with a `mirror`, such a job must run on `push` to every branch (`push: branches: ['**']`), without `pull_request` or `paths`: the pull requests are on Forgejo, the mirror pushes every branch, and Jenkins copies the job's result for the pull request's head commit by its SHA ([setup.md](setup.md#github-actions-and-the-skip)). No empty entries. |
| `credentials` | Optional list, default `[]`. The only Jenkins credentials the application's checks may bind: Jenkins credential ids (letters, digits, `.`, `_` and `-`, starting with a letter or digit). Anything else is refused by the pipeline. |
| `releaseCredentials` | Optional list, default `[]`. The only Jenkins credentials the application's release build and finalize step (`release.build.credentials` and `release.finalize.credentials` in its `yggdrasil.yml`) may bind, on the release agent: signing keys and the like. Never bindable by a check, which runs any branch's code; an id in both this and `credentials` is an error. Same id format. |
| `domain` | Optional. The domain a site is served on: a lowercase host name with at least one dot (`docs.example.com`). For applications that are published sites. Without it the host's `DOMAIN` is used (the deploy agent gets it as `YGG_DOMAIN`). `scripts/deploy.sh` exports the full host name as `YGG_PUBLIC_HOST` (`<host><hostSuffix>.<domain>`; `python3 scripts/catalog.py public-host <id> <environment> <default-domain>` prints it), which a stack file may use instead of `PUBLIC_HOST`. It does not route anything by itself. |
| `path` | Optional. Where under the domain the site lives: starts with `/`, does not end with one, no `..` (`/docs`). `scripts/deploy.sh` exports it as `YGG_PUBLIC_PATH` (empty without one; `python3 scripts/catalog.py public-path <id>`). |
| `apex` | Optional boolean. With `true` the site is also served on the bare domain, not only under its host name. With `apex: true` and no `host`, `YGG_PUBLIC_HOST` is the bare domain; with both, it is the `host` name, and `YGG_APEX_HOST` is the name the stack file redirects from it: the bare domain in an environment without a `hostSuffix`, `apex<hostSuffix>.<domain>` in the others (`catalog.py apex-host`). An apex-only site (no `host`) has the bare domain in **every** environment, whatever the `hostSuffix`: deploy it to one environment only. |
| `releaseArtifacts` | Optional boolean, default `false`. GitHub's path for a repository **without** a `yggdrasil.yml`: with `true`, Jenkins creates the immutable tag and a private draft after deployment, dispatches `.github/workflows/release-artifacts.yml` with `tag`, `publish` and `release_id` inputs, and waits for that workflow to publish the verified draft. Without it, Jenkins creates the tag and public GitHub release directly. On Forgejo, and for a GitHub repository that has a `yggdrasil.yml`, its `release` section says what is published instead: `true` with a `release` section is ignored (the build log says so), and `true` without one fails the release before anything is deployed. |
| `container` | `{project, service}`: the Compose labels that find the application's container. `project` defaults to the id; the status API looks for `<project>-<environment>`, which is what `deploy.sh` uses (platform components: `project` as it is). Without `service`, **any** container of the project matches (a running one first, then the newest), so set `service` when the stack has more than one service, e.g. `{ service: api }` next to a database. |
| `environments` | See below. |

### Per-application environments

Without `environments`, an application deploys to every environment with that environment's
options. With it, the application deploys **only** to the environments listed, and each entry
may override any option except `id` and `name`:

```yaml
environments:
  homologation: {}                            # homologation, as configured
  production: { approval: true, waitTimeout: 900 }
```

An option resolves in three steps: its default, then the environment's value, then the
application's override. See the result with:

```bash
python3 scripts/catalog.py plan shop-web
python3 scripts/catalog.py get shop-web production approval
```

The same resolution is repeated in `jenkins/library/vars/yggdrasilPipeline.groovy` (what a build
deploys), the Job DSL in `platform/jenkins/controller/casc.yaml` (which branches each job
discovers), `platform/jenkins/controller/init.groovy.d/agents.groovy` (which agents exist) and the
status API (each application's `hostSuffix` and `onDemand`). The status API re-validates `mode`,
`trigger`, `branches`, `hostSuffix` and `onDemand`. Change all of them together.

## Command line

`scripts/catalog.py` answers questions about the catalog. It needs PyYAML (`pip install pyyaml`;
Ubuntu: `sudo apt install python3-yaml`). Run it from the repository's root:

| Command | Prints |
|---|---|
| `python3 scripts/catalog.py validate` | `catalog.yaml: N environments, N systems, N applications`, or every problem |
| `python3 scripts/catalog.py environments [<app>]` | Environment ids, one per line, in promotion order; with an application id, only those it deploys to |
| `python3 scripts/catalog.py applications [--deployable]` | Application ids; `--deployable` leaves out `kind: platform` |
| `python3 scripts/catalog.py plan <app>` | JSON: each environment the application deploys to, with every option resolved |
| `python3 scripts/catalog.py get <app> <environment> <option>` | One resolved option. Lists are space-separated, booleans `true`/`false`: `get heimdall-api development onDemand` prints `true` |
| `python3 scripts/catalog.py environment <environment> [<option>]` | JSON: the environment's own options, defaults applied but no application's override; with an option, only its value, printed as `get` does |
| `python3 scripts/catalog.py owner` / `repository` | The GitHub owner; this repository's name (default `yggdrasil`) |
| `python3 scripts/catalog.py forge [type\|url\|api\|jenkins-user\|publish-agent]` | The git server: JSON of `type`, `url`, `api`, `jenkinsUser`, `developers` and `publishAgent` (`null` when nothing releases) with defaults applied (GitHub when there is no `forge` block), or one value; `publish-agent` exits `1` when there is none |
| `python3 scripts/catalog.py mirror [github\|releases]` | The mirror: JSON, or one value (booleans `true`/`false`). Exits `1` with `catalog.yaml has no mirror` when there is none |
| `python3 scripts/catalog.py repositories` | The ids of the [`repositories`](#repositories) that are not applications, one per line (nothing when there are none) |
| `python3 scripts/catalog.py repository-names` | Every git repository under the owner, once each and one per line: the deployable applications' (their `repository`, else their `id`) and then the `repositories` entries. What `scripts/renovate.sh` visits |
| `python3 scripts/catalog.py repository-entry <id>` | JSON of one such repository with defaults applied (`id`, `checks`, `actionsChecks`, `credentials`, `releaseCredentials`, `release`, `tags`, `rules`). Exits `1` when the id is not listed |
| `python3 scripts/catalog.py public-host <app> <environment> <default-domain>` / `public-path <app>` | A site's full host name in an environment (`<host><hostSuffix>.<domain>`, or the bare domain for an apex-only site; empty when the application has none or there is no domain) and its path: what `deploy.sh` exports as `YGG_PUBLIC_HOST` and `YGG_PUBLIC_PATH` |
| `python3 scripts/catalog.py apex-host <app> <environment> <default-domain>` | For an `apex: true` site with a `host`, the name its bare-domain router answers on and redirects to the host: the bare domain where the environment has no `hostSuffix`, else `apex<hostSuffix>.<domain>`; empty for any other application. What `deploy.sh` exports as `YGG_APEX_HOST` |
| `python3 scripts/catalog.py systems` | `<id><TAB><name>`, one system per line |
| `python3 scripts/catalog.py show <app>` | JSON: the application's fields, plus `system`, its system's id |
| `python3 scripts/catalog.py add-application < new.json` | Adds an application, keeping every comment of `catalog.yaml`. Reads `{"system": {"id", "name", "description"}, "application": {...}}`: the application goes at the end of that system, or of a new one (then `name` and `description` count), placed before the `yggdrasil` system. Refuses anything that would make the catalog invalid. [`ygg add`](cli.md) writes the JSON for you |

It exits with `0` on success, `1` for an invalid catalog, an unknown application or environment,
an unknown option, or an application that doesn't deploy to that environment, and `2` for a wrong
command.

## Recipes

**Several environments on one host.** The shipped catalog's layout: development, homologation and
production on one VPS. Give the environments the same `agent`, a distinct `hostSuffix` (production
can keep none), and `onDemand: true` to those that should run only while used:

```yaml
  - id: development
    name: Development
    trigger: branch
    branches: develop
    agent: vps
    hostSuffix: -dev
    onDemand: true
  - id: production
    name: Production
    trigger: release
    agent: vps
```

On the host, list them all in `platform.env` (`ENVIRONMENTS=development,production`) and create an
env file per environment and application (`ygg config <app> <environment>`), with the
suffixed host names: `PUBLIC_HOST=heimdall-api-dev.example.com`. One `*.example.com` DNS record and
certificate covers them all. Then `scripts/platform.sh up`, and run the agent named `vps`.
`ygg env start development` switches development on, `env stop development` off again.

**A dev server that follows `develop`, on a host of its own.** Add the environment below and push
it to `main`. Then pull and restart Jenkins on the controller host (it creates the new agent only
when it starts), and bring up a host for it with that agent's secret
([setup.md](setup.md#an-environment)):

```yaml
  - id: dev
    name: Dev
    trigger: branch
    branches: develop
```

**A pre-production environment with approval before production.** Two release environments. They deploy in order, and the pull request merges only after both succeed:

```yaml
  - id: preprod
    name: Pre-production
    trigger: release
  - id: production
    name: Production
    trigger: release
    approval: true
```

**An application that only exists in production.** `environments: { production: {} }`.

**An application that must stay up in an on-demand environment** (a service the others depend
on): `environments: { development: { onDemand: false } }` and so on for the others it deploys to.
`ygg env stop development` then leaves it running.

**Per-environment Compose settings** (replicas, resource limits, an extra volume) go in `stacks/<app>.<environment>.yml`. `deploy.sh` applies it last, when it exists.
