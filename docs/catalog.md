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

`python3 scripts/catalog.py validate` checks it (CI runs it on every pull request). The other tools
refuse to use a catalog that fails validation, and say why.

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
| `id` | required | Lowercase letters, digits and dashes. Names the env files on hosts (`<secrets>/<id>/<app>.env`), the `ENVIRONMENT` of the host's `platform.env`, and the `deploy/<id>` status. |
| `name` | required | Display name: Jenkins stages, the console. |
| `mode` | `proxy` | `proxy`: routed by Traefik with TLS on the environment's `DOMAIN`, no host ports (`stacks/<app>.proxy.yml`). `ports`: published on the host's `127.0.0.1`, no Traefik (`stacks/<app>.ports.yml`); for a laptop. |
| `trigger` | `manual` | What deploys here (see [Triggers](#triggers)). |
| `branches` | — | For `trigger: branch`, required. A glob or a list of globs (`*` matches `/` too): `release/*`, `[develop, hotfix/*]`. |
| `agent` | the id | Label of the Jenkins agent that deploys here. The agent runs on the environment's host. |
| `approval` | `false` | `true`: Jenkins waits for someone to click **Deploy** before deploying here, without holding an agent while it waits. |
| `waitTimeout` | `300` | Seconds `deploy.sh` waits for the new containers to be healthy before rolling back. |
| `keepImages` | `3` | Images of each application kept on the host, for rollbacks. |
| `checksTimeout` | `3600` | For `trigger: release`: seconds to wait for the pull request's GitHub checks. |

The list order is the **promotion order**. It is the order release environments deploy in, and
the order the console lists them.

### Triggers

| Trigger | Deploys | Typical use |
|---|---|---|
| `manual` | Only when asked: `scripts/deploy.sh` on the host, or **Build with Parameters → `DEPLOY_TO`** on a branch job in Jenkins. Jenkins creates an agent for a manual environment only when it sets `agent` explicitly; without one, it's deploy-by-hand only. | A laptop; an environment for demos. |
| `branch` | The pushed commit, whenever a branch matching `branches` is pushed. `release/x.y.z` deploys as version `x.y.z`; any other branch as its name (`develop`). | Staging from `release/*`; a dev server from `develop`. |
| `release` | The head of a `release/x.y.z → main` pull request, once every GitHub Actions check on it passes. Release environments deploy one after another in catalog order, each setting `deploy/<id>` on the commit. After the last one succeeds, Jenkins merges the pull request, tags `vx.y.z` and deletes the branch. | Production; a pre-production environment right before it. |

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
| `id` | Lowercase letters, digits and dashes. |
| `name`, `description` | Shown by the console. |
| `applications` | The applications that make up the system. |

### Application fields

| Field | Meaning |
|---|---|
| `id` | Unique across the catalog. It is also the repository name (unless `repository` says otherwise), the Compose project, the Jenkins job, the image name, and the network alias other containers reach it by. |
| `name` | Display name. |
| `kind` | `api`, `web`, `worker` or `platform`, shown by the console. `platform` marks the platform's own components (Traefik, Prometheus...): `scripts/platform.sh` runs them on every host, and Jenkins never deploys them. |
| `repository` | Repository name under `owner`. Default: the id. |
| `health` | URL the status API probes over the Docker networks, e.g. `http://shop-api:8080/healthz`. A 2xx answer is healthy. |
| `metrics` | `host:port` Prometheus scrapes, if the application exposes metrics. |
| `metricsPath` | Path of the metrics endpoint when it isn't `/metrics`. |
| `host` | Public host name under the environment's `DOMAIN` (`shop` → `shop.example.com`). One label only: the wildcard certificate `*.DOMAIN` doesn't cover `api.shop.example.com`. Omit it for applications that aren't public. |
| `checks` | GitHub check names (job names) required on `develop` and `main`, besides `branch-policy`. Only list checks that run on **every** pull request: a path-filtered workflow that doesn't run would leave its required check pending forever. |
| `container` | `{project, service}` to find the application's container, when it isn't the Compose project named after the id. |
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

The Jenkins pipeline resolves options the same way. Its copy of the logic is in
`jenkins/library/vars/yggdrasilPipeline.groovy`; keep the two in step if you change either.

## Recipes

**A dev server that follows `develop`.** Add an environment, then run a host for it (see
[setup.md](setup.md#adding-things)):

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
