# The catalog

`catalog.yaml`, at the root of the repository, is the single description of an installation: the
environments it deploys to, the systems it manages, and the applications each system is made of.
Every part of yggdrasil reads it:

| Reader | What it takes from the catalog |
|---|---|
| Jenkins: `casc.yaml` Job DSL | One multibranch deploy job per application, and the branches each job builds |
| Jenkins: `init.groovy.d/agents.groovy` | One agent per distinct `agent` of the environments Jenkins deploys to: one per host |
| Jenkins: the shared pipeline | What each push or pull request deploys, where, and with which options |
| `scripts/deploy.sh` | Whether the application deploys to that environment, its mode, timeouts, rollback depth and whether it runs on demand |
| `scripts/platform.sh` | That every environment in the host's `ENVIRONMENTS` exists; the GitHub owner and repository for Jenkins |
| `ygg` (`scripts/host.sh`) | The applications of each of the host's environments; which environments it may stop |
| `scripts/rules.py` (`github/rulesets.py` runs it) | The git server (`forge`), the `repositories`, each repository's required checks and the `deploy/<environment>` statuses on `main`, the users who may merge (`forge.jenkinsUser`, `forge.developers`) and the tag pattern. Applies them on GitHub or Forgejo ([setup.md, step 12](setup.md#12-apply-the-branch-rules)) |
| `scripts/forge.sh` | The git server's type, API address and the owner, when the environment doesn't give them: the commands Jenkins and the scripts use to set statuses, merge, tag, release and delete branches ([setup.md](setup.md#the-git-server-commands)) |
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
| `forge.publishAgent` | the `agent` of the last `trigger: release` environment, over the applications in catalog order (what merges and releases today); none when no environment releases | The Jenkins agent that merges, tags, creates releases and uploads their files: the only one holding the git server's write token. An agent name: lowercase letters, digits and dashes. |
| `mirror.github` | required with a `mirror` | The GitHub user or organisation the repositories are mirrored to. |
| `mirror.releases` | `true` | Whether releases are mirrored as well. `true` or `false`. |

`mirror` is only allowed with `forge.type: forgejo`; any other key in `forge` or `mirror` is an
error. Read them with `python3 scripts/catalog.py forge` / `mirror` (see
[Command line](#command-line)).

## Repositories

`repositories` lists the git repositories under `owner` that are **not applications**: libraries that
publish a package, repositories that only run CI, yggdrasil itself. They get the git server's branch and
tag rules and a Jenkins job like an application's repository does, but nothing is deployed from them.
An application's repository (its `repository`, else its `id`) is never listed here.

```yaml
repositories:
  # A library: the owner publishes versions by creating tags like 1.4.0 (no leading v).
  - id: dotnet-util
    release: owner-tag
    tags: "[0-9]*"
    checks: [Build and test]
    actionsChecks: [test]
    credentials: [nuget-api-key]
  # A repository that only runs CI: develop and main are protected, nothing is released.
  - id: shop-specs
    checks: [lint]
  # yggdrasil itself: the full branching model, and the owner creates its version tags (v*).
  - id: yggdrasil
    release: owner-tag
```

| Field | Default | Meaning |
|---|---|---|
| `id` | required | The repository name under `owner`: letters, digits, `.`, `_` and `-` (`reveal.js` is fine). Unique, and not the repository of any application (`<id> is already an application's repository`). |
| `checks` | `[]` | The checks Jenkins reports on pull requests to `develop` and `main`, as for an application's `checks`. No empty entries. |
| `actionsChecks` | `[]` | GitHub Actions job names required besides them on `develop` (not on `main`), as for an application's `actionsChecks`. No empty entries. |
| `credentials` | `[]` | The only Jenkins credentials the repository's checks may bind. Jenkins credential ids: letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `release` | `none` | `none`: no versions. `owner-tag`: the owner creates the version tags by hand, matching `tags`. (Applications are different: Jenkins creates their `v*` tags.) |
| `tags` | `v*` | The tag pattern, a glob of letters, digits, `.`, `_`, `-`, `*` and `[...]` classes (`[0-9]*` for versions without a `v`). Only meaningful with `release: owner-tag`. |
| `rules` | `release` | `release`: the full branching model (protected `develop`, `main` and `release/*`). `basic`: `develop` and `main` only, without force pushes or deletion. |

Any other key is an error. Read the list with `python3 scripts/catalog.py repositories` (ids) and one
entry, defaults applied, with `python3 scripts/catalog.py repository-entry <id>`. The git server's rules
(`scripts/rules.py`) and the Jenkins pipeline consume them.

## Repository pipeline file

Each repository describes its own checks and releases in a `yggdrasil.yml` at its root. The file is
**data**: images, commands (usually a script under `ci/`), globs and the *names* of Jenkins credentials.
Jenkins does not read the repository's `Jenkinsfile`; the Jenkins library reads this file instead and
runs the commands only on the isolated CI agent (checks) or the release agent (releases). A repository
**without** a `yggdrasil.yml` is not an error: it behaves as before, it deploys and nothing else.

```yaml
checks:                         # name -> a command run in a container; the name is the status context
  unit:
    image: mcr.microsoft.com/dotnet/sdk:10.0
    run: ci/test.sh             # a command line, run with sh -c in the checkout
    docker: true                # give it the Docker engine (default false)
    credentials:                # ENV_VAR -> Jenkins credential id
      NUGET_KEY: nuget-api-key
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
publish:                        # owner-tagged repositories only
  nuget: {image: mcr.microsoft.com/dotnet/sdk:10.0, run: ci/pack.sh, packages: "out/*.nupkg"}
```

Every section is optional. Unknown keys at any level, and values of the wrong type, are errors that name
the field (`checks.unit.dockr: unknown field`). `image` and `run` are required wherever they appear,
and so is `release.windows.workflow`. `credentials` keys are environment variable names. A check's name is
lowercase letters, digits, `.`, `_` and `-`, starting with a letter or digit, and may not be `branch-policy`;
every `image` is a Docker image reference (`registry:port/name:tag` or `name@sha256:...`: no spaces, and it
may not start with `-`).

The file is also checked against the catalog entry of the repository (the application, or its
[`repositories`](#repositories) entry):

- every credential in `checks.*.credentials` and `release.build.credentials` must be in the entry's
  `credentials`; it is never bound on `renovate/*` builds;
- every name in the entry's `checks` must be defined under `checks`;
- `deploy.inputs` may only name checks defined in the file;
- `publish` is allowed only for a `repositories` entry with `release: owner-tag`;
- `release` is allowed for an application with a release environment and for `release: owner-tag`.

Read the entry and check a checkout with `python3 scripts/pipeline_config.py describe <job-id> <repo-dir>`
(`job-id`: the application's id or the `repositories` entry's id). It prints one JSON document,
`{"entry", "config", "errors"}`, where `config` is `null` when there is no file, and exits `1` when
`errors` is not empty. The entry has `kind` (`application` or `repository`), `id`, `repository`,
`credentials`, `checks`, `actionsChecks`, `release` (`deploy` for an application with a release
environment, `owner-tag`, or `none`) and `tags` (`v*` for applications).

### Which checks run

For a build of a branch the pipeline asks `python3 scripts/pipeline_config.py plan <job-id> <repo-dir>
--statuses <file> --changed <file|-> --branch <name>` what to do with each check, and gets a JSON list in the
file's order: `{name, action, reason, image, run, docker, credentials, junit, archive}`.

- `--statuses` is the output of `forge.sh get-statuses` for the commit (`<context>`, `<state>`, `<creator>`,
  tab separated); `--changed` lists the changed files, one path per line, or is `-` when they are unknown.
- **reuse**: the commit already has a `success` for the check created by `forge.jenkinsUser`. A success
  created by anyone else is not trusted, and neither is a failure or a pending: those checks `run`.
- **skip**: the check has `paths`, the changed files are known and none matches. `*` stays in one directory,
  `**` crosses directories (`src/**` matches `src/a/b.cs`, `docs/*.md` does not match `docs/a/b.md`, `**/*.md`
  matches `README.md`). When the changes are unknown (`-`) the check runs.
- **run**: everything else. On a `renovate/*` branch `credentials` is always `{}`.

The branch policy is not a check of the file and is never reused. The command exits `1`, naming the
problems, when the file is invalid or breaks the catalog rules above.

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
| `checksTimeout` | `3600` | For `trigger: release`: seconds Jenkins waits for every GitHub Actions check on the release pull request before failing the build. Only the value on the application's **first** release environment counts. |
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
| `release` | The head of a `release/x.y.z → main` pull request, once every GitHub Actions check on it has passed (`branch-policy` included). Jenkins marks every `deploy/<id>` pending, then deploys the release environments one after another in catalog order, setting `deploy/<id>` after each. After the last one succeeds, it merges the pull request and creates the GitHub release and tag. With `releaseArtifacts: true`, that is an immutable tag plus private draft; Jenkins dispatches the application's release-artifact workflow and waits for it to verify every upload and publish the draft. Only then does Jenkins complete cleanup. Pushing to the branch meanwhile fails the build. | Production; a pre-production environment right before it. |

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
| `host` | A lowercase host name: letters, digits and dashes (*status API*). The link the console shows: `https://<host><hostSuffix>.<DOMAIN>`, with the environment's `hostSuffix`. It does **not** route anything: the Traefik router is in `stacks/<id>.proxy.yml`, usually from `PUBLIC_HOST` in the application's env file. Keep the two in step. Use one label (`shop`, not `api.shop`): the wildcard certificate `*.DOMAIN` doesn't cover `api.shop.example.com`. Omit it for applications that aren't public. |
| `checks` | The checks Jenkins reports on the pull requests to `develop` and `main`, besides `branch-policy`. They are required there. Only list checks that run on **every** pull request: one that doesn't run would leave its required check pending forever. No empty entries (*status API*). With the GitHub git server and the current pipeline they are still the job part of `<workflow> / <job>` in a pull request's Checks tab; `scripts/rules.py` would require them bound to no app, so an existing GitHub installation waits before running it ([setup.md](setup.md#an-existing-github-installation)). |
| `actionsChecks` | Optional list, default `[]`. GitHub Actions job names that are required too, in addition to `checks`, on pull requests into `develop` only (`main` requires only what Jenkins reports): the job part of `<workflow> / <job>` in a pull request's Checks tab. A job moved here from `checks` is no longer required on `main`. On GitHub the required check is bound to GitHub Actions, so only checks from it satisfy them (`checks`, Jenkins', are bound to no app); on Forgejo they are required by name, like the others. The same rule applies: only checks that run on **every** pull request. No empty entries. |
| `credentials` | Optional list, default `[]`. The only Jenkins credentials the application's checks may bind: Jenkins credential ids (letters, digits, `.`, `_` and `-`, starting with a letter or digit). Anything else is refused by the pipeline. |
| `domain` | Optional. The domain a site is served on: a lowercase host name with at least one dot (`docs.example.com`). For applications that are published sites. Without it the host's `DOMAIN` is used. `scripts/deploy.sh` exports the full host name as `YGG_PUBLIC_HOST` (`<host><hostSuffix>.<domain>`; `python3 scripts/catalog.py public-host <id> <environment> <default-domain>` prints it), which a stack file may use instead of `PUBLIC_HOST`. It does not route anything by itself. |
| `path` | Optional. Where under the domain the site lives: starts with `/`, does not end with one, no `..` (`/docs`). `scripts/deploy.sh` exports it as `YGG_PUBLIC_PATH` (empty without one; `python3 scripts/catalog.py public-path <id>`). |
| `apex` | Optional boolean. With `true` the site is also served on the bare domain, not only under its host name. With `apex: true` and no `host`, `YGG_PUBLIC_HOST` is the bare domain; with both, it is the `host` name (the stack file adds the bare domain itself). |
| `releaseArtifacts` | Optional boolean, default `false`. With `true`, Jenkins creates the immutable tag and a private draft after deployment, dispatches `.github/workflows/release-artifacts.yml` with `tag`, `publish` and `release_id` inputs, and waits for that workflow to publish the verified draft. Without it, Jenkins creates the tag and public GitHub release directly. |
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
| `python3 scripts/catalog.py repository-entry <id>` | JSON of one such repository with defaults applied (`id`, `checks`, `actionsChecks`, `credentials`, `release`, `tags`, `rules`). Exits `1` when the id is not listed |
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
