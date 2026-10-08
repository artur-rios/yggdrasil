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
| `scripts/ygg.sh` | The applications of each of the host's environments; which environments it may stop |
| `github/rulesets.py` | Each repository's required checks, and the `deploy/<environment>` statuses on `main` |
| Status API | Which applications run in each of its host's environments, how to probe them, how they group into systems |
| Prometheus | Scrape targets, through the status API |
| Console | Everything it shows, through the status API |

Check it after every edit:

```bash
python3 scripts/catalog.py validate
```

- `validate` prints `catalog.yaml: N environments, N systems, N applications`, or every problem it
  found. CI runs it on every pull request. `deploy.sh`, `platform.sh` and `github/rulesets.py`
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
environments: [...]    # at least one, in promotion order
systems: [...]
```

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
| Traefik routers and services | `<application>-<environment>`, in the stacks' proxy overlays | `heimdall-api-production`, `heimdall-api-production-same-origin` |
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
| `hostSuffix` | `""` | Appended to every application's `host` in this environment: `heimdall` with `-dev` is `https://heimdall-dev.<DOMAIN>`. How several environments share one host, one `DOMAIN`, one `*.DOMAIN` certificate and one `*.DOMAIN` DNS record. Lowercase letters, digits and dashes, not ending with a dash (*status API*); `<host><hostSuffix>` must stay one DNS label of at most 63 characters (*status API*). It only changes the console's link: the router is `PUBLIC_HOST` (or `UI_HOST`) in the env file, which `scripts/ygg.sh` writes with the suffix. |
| `onDemand` | `false` | `true`: the environment runs only while it is used. `scripts/ygg.sh env start <id>` and `env stop <id>` turn it on and off. The status API reports its stopped applications as `stopped`, which is not a problem, instead of `down`. A deploy of an application that wasn't running (switched off, or never deployed) starts it, waits for it to be healthy (rolling back as usual if it isn't), and stops it again, so a push to `develop` doesn't switch development on. A Jenkins **`DEPLOY_TO`** build, or `DEPLOY_START=1 scripts/deploy.sh ...`, leaves it running. |

Any other key is an error. Numbers must be whole and at least 1; `approval` and `onDemand` are
`true` or `false`.
An application that switches an environment to `trigger: branch` needs `branches` from its override
or from the environment.

The list order is the **promotion order**: the order release environments deploy in on a release
pull request, and the order a push deploys to several matching `branch` environments.

### Triggers

| Trigger | Deploys | Typical use |
|---|---|---|
| `manual` | Only when asked: `scripts/deploy.sh` on the host, or **Build with Parameters → `DEPLOY_TO`** in Jenkins, which leaves an `onDemand` environment running (`DEPLOY_TO` also offers `branch` environments, to deploy any discovered branch there and switch it on). `DEPLOY_TO` exists on the branches Jenkins discovers (`main`, and those matching a `branch` environment's globs), once the branch has been built once. Jenkins creates an agent for a manual environment only when it sets `agent`; without one, it's deploy-by-hand only, and a `DEPLOY_TO` build would wait forever for a node. | A laptop; an environment for demos. |
| `branch` | The pushed commit, whenever a branch matching `branches` is pushed. `release/x.y.z` deploys as version `x.y.z`; any other branch as its name, with characters other than letters, digits, `_`, `.` and `-` replaced by `-` (`feature/x` → `feature-x`). The image tag is `<environment>-<version>-<7-character commit>`. In an `onDemand` environment, an application that wasn't running is stopped again after the deploy. **Build with Parameters → `DEPLOY_TO`** deploys any discovered branch here by hand, and leaves it running. | Staging from `release/*`; a dev server from `develop`. |
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
| `checks` | GitHub Actions job names required on `develop` and `main`, besides `branch-policy`: the job part of `<workflow> / <job>` in a pull request's Checks tab. Only checks from GitHub Actions satisfy them. Only list checks that run on **every** pull request: a path-filtered workflow that doesn't run would leave its required check pending forever. No empty entries (*status API*). |
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
| `python3 scripts/catalog.py systems` | `<id><TAB><name>`, one system per line |
| `python3 scripts/catalog.py show <app>` | JSON: the application's fields, plus `system`, its system's id |
| `python3 scripts/catalog.py add-application < new.json` | Adds an application, keeping every comment of `catalog.yaml`. Reads `{"system": {"id", "name", "description"}, "application": {...}}`: the application goes at the end of that system, or of a new one (then `name` and `description` count), placed before the `yggdrasil` system. Refuses anything that would make the catalog invalid. [`scripts/ygg.sh add`](cli.md) writes the JSON for you |

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
env file per environment and application (`scripts/ygg.sh config <app> <environment>`), with the
suffixed host names: `PUBLIC_HOST=heimdall-api-dev.example.com`. One `*.example.com` DNS record and
certificate covers them all. Then `scripts/platform.sh up`, and run the agent named `vps`.
`scripts/ygg.sh env start development` switches development on, `env stop development` off again.

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
`scripts/ygg.sh env stop development` then leaves it running.

**Per-environment Compose settings** (replicas, resource limits, an extra volume) go in `stacks/<app>.<environment>.yml`. `deploy.sh` applies it last, when it exists.
