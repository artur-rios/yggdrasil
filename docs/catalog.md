# The catalog

`catalog.yaml`, at the root of the repository, is the single description of an installation: the
environments it deploys to, the systems it manages, and the applications each system is made of.
Every part of yggdrasil reads it:

| Reader | What it takes from the catalog |
|---|---|
| Jenkins: `casc.yaml` Job DSL | One multibranch deploy job per application, and the branches each job builds |
| Jenkins: `init.groovy.d/agents.groovy` | One agent per environment that Jenkins deploys to |
| Jenkins: the shared pipeline | What each push or pull request deploys, where, and with which options |
| `scripts/deploy.sh` | Whether the application deploys to that environment, its mode, timeouts and rollback depth |
| `scripts/platform.sh` | That the host's `ENVIRONMENT` exists; the GitHub owner and repository for Jenkins |
| `github/rulesets.py` | Each repository's required checks, and the `deploy/<environment>` statuses on `main` |
| Status API | Which applications run in its environment, how to probe them, how they group into systems |
| Prometheus | Scrape targets, through the status API |
| Console | Everything it shows, through the status API |

Check it after every edit:

```bash
python3 scripts/catalog.py validate
```

- `validate` prints `catalog.yaml: N environments, N systems, N applications`, or every problem it
  found. CI runs it on every pull request. `deploy.sh`, `platform.sh` and `github/rulesets.py`
  refuse a catalog it rejects.
- The **status API** validates the catalog again when it starts, with a few stricter rules (marked
  *status API* in the tables below). A catalog that fails them stops the status API: it lists every
  problem in its log (`docker logs yggdrasil-status-1`) and exits.
- **Jenkins** doesn't validate it: a broken catalog shows up as a Job DSL or pipeline error.

The catalog is read from each host's checkout when the containers start. After changing it, see
[setup.md](setup.md#where-jenkins-and-the-hosts-read-the-catalog) for how to apply it.

## Top level

```yaml
owner: acme            # GitHub user or organisation of the application repositories and of this one
repository: yggdrasil  # this repository's name under the owner (default yggdrasil)
environments: [...]    # at least one, in promotion order
systems: [...]
```

## Environments

An environment is a place applications run: a laptop, a staging host, production. **One
environment per Docker engine**: each application's Compose project is named after its id, so two
environments on one engine would replace each other's containers. Each environment runs its own
platform stack (Traefik, status API, monitoring) and, if Jenkins deploys to it, its own agent. A
"host" can be a VM or a WSL distribution with its own engine as well as a machine.

```yaml
environments:
  - id: development
    name: Development
    mode: ports
    trigger: manual

  - id: staging
    name: Staging
    trigger: branch
    branches: [release/*, develop]

  - id: production
    name: Production
    trigger: release
    approval: true
    waitTimeout: 600
```

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Lowercase letters, digits and single dashes, starting and ending with a letter or digit (`dev`, `pre-prod`). Names the env files on hosts (`<secrets>/<id>/<app>.env`), the `ENVIRONMENT` of the host's `platform.env`, and the `deploy/<id>` status. |
| `name` | required | Display name: Jenkins stages, the console. |
| `mode` | `proxy` | `proxy`: routed by Traefik with TLS on the environment's `DOMAIN`, no host ports (`stacks/<app>.proxy.yml`). `ports`: published on the host's `127.0.0.1`, no Traefik (`stacks/<app>.ports.yml`); for a laptop. |
| `trigger` | `manual` | What deploys here (see [Triggers](#triggers)). |
| `branches` | — | For `trigger: branch`, required. A glob or a list of globs: `release/*`, `[develop, hotfix/*]`. Use only `*`, which matches any characters, `/` included: `?` and `[...]` aren't matched the same way by every reader. Case-sensitive. Besides `main` and pull requests, these are the only branches Jenkins discovers for the application. |
| `agent` | the id | Label of the Jenkins agent that deploys here. The agent runs on the environment's host. |
| `approval` | `false` | `true`: Jenkins waits for someone to click **Deploy** before deploying here, without holding an agent while it waits. Applies to every deploy to this environment: branch, manual and release. Any signed-in Jenkins user can approve. A build still waiting after 4 hours, the pipeline's overall limit, is aborted. |
| `waitTimeout` | `300` | Seconds `deploy.sh` waits for the new containers to be healthy before rolling back to the image that was running, if there is one. `DEPLOY_WAIT_TIMEOUT` overrides it for one run by hand. |
| `keepImages` | `3` | Images of each application kept on the host after a successful deploy, the running one included: what rollbacks use. `DEPLOY_KEEP_IMAGES` overrides it for one run by hand. |
| `checksTimeout` | `3600` | For `trigger: release`: seconds Jenkins waits for every GitHub Actions check on the release pull request before failing the build. Only the value on the application's **first** release environment counts. |

Any other key is an error. Numbers must be whole and at least 1; `approval` is `true` or `false`.

The list order is the **promotion order**: the order release environments deploy in on a release
pull request, and the order a push deploys to several matching `branch` environments.

### Triggers

| Trigger | Deploys | Typical use |
|---|---|---|
| `manual` | Only when asked: `scripts/deploy.sh` on the host, or **Build with Parameters → `DEPLOY_TO`** in Jenkins. `DEPLOY_TO` exists on the branches Jenkins discovers (`main`, and those matching a `branch` environment's globs), once the branch has been built once. Jenkins creates an agent for a manual environment only when it sets `agent`; without one, it's deploy-by-hand only, and a `DEPLOY_TO` build would wait forever for a node. | A laptop; an environment for demos. |
| `branch` | The pushed commit, whenever a branch matching `branches` is pushed. `release/x.y.z` deploys as version `x.y.z`; any other branch as its name, with characters other than letters, digits, `_`, `.` and `-` replaced by `-` (`feature/x` → `feature-x`). The image tag is `<version>-<7-character commit>`. | Staging from `release/*`; a dev server from `develop`. |
| `release` | The head of a `release/x.y.z → main` pull request, once every GitHub Actions check on it has passed (`branch-policy` included). Jenkins marks every `deploy/<id>` pending, then deploys the release environments one after another in catalog order, setting `deploy/<id>` after each. After the last one succeeds, it merges the pull request with a merge commit, creates the GitHub release and tag `vx.y.z`, and deletes the branch. Pushing to the branch meanwhile fails the build. | Production; a pre-production environment right before it. |

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
          staging: {}
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

| Field | Meaning |
|---|---|
| `id` | Required. Unique across the catalog. It is also the repository name (unless `repository` says otherwise), the Compose project (`deploy.sh` names it so) and the Jenkins job. Its Compose files must name the image `<id>` (old images are pruned by that name) and give the service the network alias `<id>` on `edge` and `telemetry`, which `health` and `metrics` use. |
| `name` | Required (*status API*). Display name. |
| `kind` | Required. `api`, `web`, `worker` or `platform`, shown by the console. `platform` marks yggdrasil's own components (Traefik, Prometheus...). They are started by `platform/compose.yml` through `scripts/platform.sh`, not by the catalog: their entries only let the status API probe them and Prometheus scrape them. Jenkins creates no job, agent or ruleset for them. |
| `repository` | Repository name under `owner`. Default: the id; none for `platform` components. |
| `health` | Required. Absolute URL the status API probes over the Docker networks, e.g. `http://shop-api:8080/healthz`. A 2xx answer is healthy. |
| `metrics` | `host:port` Prometheus scrapes, if the application exposes metrics (*status API*: must be `host:port`). |
| `metricsPath` | Path of the metrics endpoint when it isn't `/metrics`. Starts with `/`, and needs `metrics` (*status API*). |
| `host` | The link the console shows: `https://<host>.<DOMAIN>`. It does **not** route anything: the Traefik router is in `stacks/<id>.proxy.yml`, usually from `PUBLIC_HOST` in the application's env file. Keep the two in step. Use one label (`shop`, not `api.shop`): the wildcard certificate `*.DOMAIN` doesn't cover `api.shop.example.com`. Omit it for applications that aren't public. |
| `checks` | GitHub Actions job names required on `develop` and `main`, besides `branch-policy`: the job part of `<workflow> / <job>` in a pull request's Checks tab. Only checks from GitHub Actions satisfy them. Only list checks that run on **every** pull request: a path-filtered workflow that doesn't run would leave its required check pending forever. No empty entries (*status API*). |
| `container` | `{project, service}`: the Compose labels that find the application's container. `project` defaults to the id, which is what `deploy.sh` uses. Without `service`, **any** container of the project matches (a running one first, then the newest), so set `service` when the stack has more than one service, e.g. `{ service: api }` next to a database. |
| `environments` | See below. |

### Per-application environments

Without `environments`, an application deploys to every environment with that environment's
options. With it, the application deploys **only** to the environments listed, and each entry
may override any option except `id` and `name`:

```yaml
environments:
  staging: {}                                 # staging, as configured
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
discovers) and `platform/jenkins/controller/init.groovy.d/agents.groovy` (which agents exist). The
status API re-validates `mode`, `trigger` and `branches`. Change all of them together.

## Command line

`scripts/catalog.py` answers questions about the catalog. It needs PyYAML (`pip install pyyaml`;
Ubuntu: `sudo apt install python3-yaml`). Run it from the repository's root:

| Command | Prints |
|---|---|
| `python3 scripts/catalog.py validate` | `catalog.yaml: N environments, N systems, N applications`, or every problem |
| `python3 scripts/catalog.py environments [<app>]` | Environment ids, one per line, in promotion order; with an application id, only those it deploys to |
| `python3 scripts/catalog.py applications [--deployable]` | Application ids; `--deployable` leaves out `kind: platform` |
| `python3 scripts/catalog.py plan <app>` | JSON: each environment the application deploys to, with every option resolved |
| `python3 scripts/catalog.py get <app> <environment> <option>` | One resolved option. Lists are space-separated, booleans `true`/`false` |
| `python3 scripts/catalog.py owner` / `repository` | The GitHub owner; this repository's name (default `yggdrasil`) |

It exits with `0` on success, `1` for an invalid catalog, an unknown application or an application
that doesn't deploy to that environment, and `2` for a wrong command.

## Recipes

**A dev server that follows `develop`.** Add the environment below and push it to `main`. Then pull
and restart Jenkins on the controller host (it creates the new agent only when it starts), and bring
up a host for it with that agent's secret ([setup.md](setup.md#an-environment)):

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

**Per-environment Compose settings** (replicas, resource limits, an extra volume) go in `stacks/<app>.<environment>.yml`. `deploy.sh` applies it last, when it exists.
