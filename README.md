# yggdrasil

A self-hosted **deploy manager**. It builds, deploys and watches **systems**: products made of one or
more applications that work together, such as a front end and its back end. It runs them on three
environments, and adding a system or an application is a catalog entry, not a change to the platform.

Today it manages:

| System | Applications |
|---|---|
| **Heimdall**: identity and access management | `heimdall-api` (.NET 10), `heimdall-ui` (Flutter web) |
| **Fortuna**: personal finance | `fortuna-api` (.NET 10), `fortuna-ui` (Flutter web) |
| **Yggdrasil**: the platform itself | Traefik, the status API, the console, Prometheus, Loki, Alloy, Grafana, Jenkins |

What it gives every application:

- **A release flow enforced on GitHub**: `feature/`/`fix/` → `develop` → `release/x.y.z` → `main`, tagged `vx.y.z`.
- **Deploys by a self-hosted Jenkins**:
  - homologation when a release branch is pushed
  - production when the release pull request is green, which is also when the PR gets merged and tagged
- **Rollback** to the previous image when a deploy doesn't become healthy.
- **TLS** from Let's Encrypt through Traefik.
- **Monitoring**: Prometheus metrics and Loki logs, in Grafana.
- **The console**: a responsive web and Android app showing each system's status. Expand a system to see each of its applications: health, version, commit, when it was deployed, and container state.

| | Development | Homologation | Production |
|---|---|---|---|
| Where | Windows, Docker Desktop | Same machine, Docker Engine inside WSL Ubuntu | Ubuntu VPS |
| Reached at | `127.0.0.1:<port>` | `https://*.hml.<domain>` | `https://*.<domain>` |
| Traefik + Let's Encrypt | no | yes (DNS-01, wildcard) | yes (DNS-01, wildcard) |
| Status API + console | no | `https://yggdrasil.hml.<domain>` | `https://yggdrasil.<domain>` |
| Prometheus, Loki, Alloy, Grafana | no | yes | yes |
| Jenkins | no | agent `homologation` | controller + agent `production` |
| PostgreSQL | Windows host | Windows host or WSL (see below) | the VPS |
| Deploys when | you run it | `release/x.y.z` is pushed | the `release/x.y.z → main` PR is green |

```
                         GitHub (code, Actions = build + test, rulesets)
                             │ webhooks                     ▲ status, merge, tag
                             ▼                              │
   VPS ─────────────────────────────────────────────────────┴───────────────  WSL (home)
   Traefik :443 ── jenkins.<domain> ── Jenkins controller                      Traefik :443
      │                                   │  WebSocket                            │
      ├── heimdall.<domain>      ui       ├─────────── agent "production"         ├── *.hml.<domain>
      ├── heimdall-api.<domain>  api      └─────────── agent "homologation" ◄─────┤   (same stacks)
      ├── fortuna.<domain>       ui          (dials out from WSL; no inbound port) │
      ├── fortuna-api.<domain>   api                                               │
      ├── yggdrasil.<domain> ── console + status API ── probes every app, reads containers (read-only)
      └── grafana.<domain> ── Prometheus (targets from the status API) + Loki ◄── Alloy (container logs)
```

## The catalog

[`catalog.yaml`](catalog.yaml) is the single list of what yggdrasil manages. It lists systems, and each system lists its applications: id (also the repository, Compose project and network alias), kind, health endpoint, metrics endpoint, public host name and required GitHub checks. Everything that needs to know which applications exist reads it:

| Reads the catalog | For |
|---|---|
| Jenkins (`casc.yaml` Job DSL) | One multibranch deploy job per application (except `kind: platform`) |
| `github/rulesets.py` | Rulesets, required checks and settings of each repository |
| Status API | What to probe, how applications group into systems, and Prometheus' scrape targets |
| Console | Everything it shows, through the status API |
| `scripts/deploy.sh` | Refuses stacks that are not in it |

### Adding an application

To add, for example, a `hermes-api` and a `hermes-ui` as a new **Hermes** system:

1. **Catalog**: add the system and its two applications to `catalog.yaml`, with `health` (a URL the status API can reach over the `edge` network, e.g. `http://hermes-api:8080/healthz`), `metrics` if the app exposes Prometheus metrics, `host` for public ones, and the `checks` its CI runs on every pull request.
2. **Stack files** in `stacks/`:
   - An API repository that has its own `docker-compose.yml` needs only `hermes-api.proxy.yml`: Traefik labels, the `edge` and `telemetry` networks with an alias equal to the id, and no host ports. Copy `heimdall-api.proxy.yml`.
   - A repository without a Compose file also needs `hermes-ui.yml` (the service) and `hermes-ui.ports.yml` (development). Copy the heimdall-ui ones.
3. **Env files**: the templates go in `env/<environment>/hermes-*.env.example`. The filled-in files go in `/etc/yggdrasil/<environment>/` on each host.
4. **In the application's repository**: a `Jenkinsfile` (`@Library('yggdrasil') _` then `yggdrasilPipeline(stack: 'hermes-api')`), the `branch-policy.yml` workflow, and a `CONTRIBUTING.md` (copy them from any existing repository). Install the GitHub App on the repository.
5. **Apply**:
   - `python github/rulesets.py hermes-api hermes-ui` on your machine.
   - `platform.sh up` on each host: the status API picks up the catalog, Prometheus the targets, and Jenkins, on restart, the new jobs.

Deployment labels (`yggdrasil.version`, `.commit`, `.deployed_at`) are added by `deploy.sh` to every container of every stack, so a new application reports its version in the console without any configuration.

## The console

`https://yggdrasil.<domain>` in a browser, or the Android app (the APK is a build artifact of this repository's CI).

- Every **system** is a card with the worst status of its applications: `up`, `degraded`, `down`, `not deployed` or `unknown`. Problems sort first, and "problems only" hides the rest.
- **Expanding** a system lists its applications. For each one you see:
  - its status and kind
  - version, commit and when it was deployed
  - the latest health probe and its latency
  - container state, health and restarts
  - links to the application and its repository

  Tapping an application opens its full details.
- **Environments**: the web console starts on the environment that serves it. Add others (name, URL, token) in Settings; the Android app starts there. Tokens are stored in the platform's secure storage. For one console to show another environment, list the console's origin in that environment's `YGGDRASIL_STATUS_CORS_ORIGINS`.
- **Refresh**: data comes from that environment's status API (`GET /api/status`, bearer `YGGDRASIL_STATUS_TOKEN`). The console refreshes it every 30 seconds while it is visible. The API itself refreshes every 15 seconds: it probes each application's health endpoint and reads container state through a read-only Docker socket proxy.

The contract between the two is [`docs/status-api.md`](docs/status-api.md).

## Layout

| Path | What |
|---|---|
| `catalog.yaml` | Systems and their applications: the list everything else reads |
| `status/` | The status API (.NET 10): probes, container state, `/api/status`, Prometheus service discovery |
| `console/` | The console (Flutter): web and Android |
| `docs/status-api.md` | The status API contract |
| `platform/compose.yml` | One host's platform: Traefik, the status API, the console, the Docker socket proxy, Prometheus, Loki, Alloy, Grafana, plus the Jenkins controller and agent under the `jenkins` and `agent` profiles |
| `platform/jenkins/controller/casc.yaml` | Jenkins, configured entirely as code: users, agents, GitHub App credential, shared library, one multibranch job per catalog application |
| `jenkins/library/vars/yggdrasilPipeline.groovy` | The pipeline every application's `Jenkinsfile` calls |
| `stacks/` | Per-application Compose files and overlays: `*.proxy.yml` (Traefik, homologation and production), `*.ports.yml` (host ports, development) |
| `scripts/deploy.sh` | Build, label, deploy, health-wait, roll back one stack. Jenkins runs this; so can you |
| `scripts/github.sh` | Wait for checks, set status, merge, release, delete branch |
| `scripts/platform.sh` | Bring a host's platform up or down |
| `github/rulesets.py` | The GitHub rulesets and settings of every catalog repository, as code |
| `env/` | Templates for every env file. The filled-in copies live on each host in `/etc/yggdrasil`, never in git |

## The release flow

```
feature/x ─┐                              ┌─ deploy to homologation (Jenkins, on push)
fix/y ─────┴─► develop ──► release/1.4.0 ─┤
                                          └─ PR → main ─ all GitHub checks green
                                                          ─ deploy to production (Jenkins)
                                                          ─ status deploy/production = success
                                                          ─ merge (merge commit), tag + release v1.4.0
                                                          ─ delete release/1.4.0
```

Enforced on GitHub by `github/rulesets.py` and each repository's **Branch Policy** workflow:

- `develop` and `main` accept changes only through pull requests: no direct pushes, force pushes or deletion.
- Into `develop`: only `feature/*` or `fix/*` branches that were cut from `develop`.
- Into `main`: only `release/x.y.z` branches that are snapshots of `develop`, for a version not yet tagged, with every check green **and** `deploy/production` set by Jenkins. So a release cannot reach `main` before it is live.
- `v*` tags cannot be moved or deleted.
- Repository admins (the owner) bypass all of it, for emergencies only.

Each repository's `CONTRIBUTING.md` has the day-to-day version.

If the production deploy fails, `deploy.sh` rolls back to the image that was running and Jenkins sets `deploy/production` to failure. The pull request stays open and can't be merged. Fix it on `develop` and cut a new release. **Rollback does not undo database migrations**: a migration must stay compatible with the previous release for one release.

## Setting up

### 1. The GitHub App (once)

Jenkins talks to GitHub as a GitHub App, not as you. It therefore does **not** inherit your bypass: its merges go through the same rules.

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**, named e.g. `yggdrasil-jenkins`.
   - Homepage URL: `https://jenkins.<domain>`
   - Webhook URL: `https://jenkins.<domain>/github-webhook/`, active
   - Repository permissions: **Contents** read and write (merge, tags, delete branches), **Pull requests** read and write, **Commit statuses** read and write, **Checks** read-only, **Metadata** read-only, **Administration** none
   - Subscribe to events: **Push**, **Pull request**, **Repository**
   - Where can it be installed: only this account
2. Generate a private key, then convert it to the PKCS#8 format Jenkins requires:
   ```bash
   openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -in yggdrasil-jenkins.*.private-key.pem -out /etc/yggdrasil/github-app.pem
   ```
3. Install the app on every catalog repository (today `heimdall-api`, `heimdall-ui`, `fortuna-api`, `fortuna-ui`) and on `yggdrasil`.

### 2. Cloudflare

- A zone for `<domain>`, and an API token with **Zone → DNS → Edit** on it.
- Records: `*.<domain>` → VPS public IP. `*.hml.<domain>` → the Windows machine's LAN IP, **DNS only** (grey cloud).
  Some home routers drop DNS answers that point at private addresses ("DNS rebinding protection"). If `*.hml` doesn't resolve at home, allow the domain in the router or add a hosts-file entry.

### 3. Production: the VPS

```bash
# Docker Engine + Compose plugin: https://docs.docker.com/engine/install/ubuntu/
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
sudo git clone https://github.com/artur-rios/yggdrasil.git /opt/yggdrasil
sudo install -d -m 700 -o "$USER" /etc/yggdrasil /etc/yggdrasil/production

cp /opt/yggdrasil/env/platform.env.example /etc/yggdrasil/platform.env
# fill it in, with COMPOSE_PROFILES=jenkins for now (the agent secret does not exist yet) and
# YGGDRASIL_STATUS_TOKEN=$(openssl rand -hex 32)
/opt/yggdrasil/scripts/platform.sh up
```

Open `https://jenkins.<domain>` and sign in as `JENKINS_ADMIN_ID`. Under **Manage Jenkins → Nodes → production**, copy the agent secret into `JENKINS_AGENT_SECRET`, set `COMPOSE_PROFILES=jenkins,agent` and `DOCKER_GID=$(getent group docker | cut -d: -f3)`, and run `platform.sh up` again. Keep the `homologation` node's secret for step 4.

Application env files: see [Application env files](#application-env-files). The cloned application repositories on the VPS are no longer needed: Jenkins checks out the exact commit it deploys.

Docker publishes ports around `ufw`. Only Traefik publishes any (80 and 443). Keep it that way: nothing else in these stacks has a `ports:` entry in homologation or production.

### 4. Homologation: WSL Ubuntu on the Windows machine

Homologation must run on its **own** Docker Engine, not on Docker Desktop's:

1. Docker Desktop → Settings → Resources → WSL integration: **turn it off for this distro**. Otherwise `docker` inside Ubuntu is Docker Desktop, the development engine, and both environments would fight over the same containers and ports.
2. Install Docker Engine inside Ubuntu (same guide as the VPS) and enable systemd in `/etc/wsl.conf` (`[boot] systemd=true`).
3. Networking: set `networkingMode=mirrored` in `%UserProfile%\.wslconfig` so Traefik's 80/443 in WSL are the Windows machine's 80/443, and the Windows PostgreSQL is reachable from WSL. Then allow inbound 80/443 for WSL in the Hyper-V firewall so other LAN devices (a phone testing the app) can reach it. **Docker Desktop must not publish 80 or 443 at the same time.**
4. PostgreSQL: either keep using the Windows instance (with mirrored networking, `DB_HOST=host.docker.internal` resolves to the host; allow the WSL address in `pg_hba.conf`), or, closer to production, install PostgreSQL inside WSL.
5. The same `platform.sh up` as the VPS, with `ENVIRONMENT=homologation`, `DOMAIN=hml.<domain>`, `COMPOSE_PROFILES=agent`, `JENKINS_AGENT_NAME=homologation`, its secret, and `JENKINS_AGENT_URL` **unset** (it connects to `JENKINS_URL` over the internet by WebSocket; no inbound port at home).

WSL stops when idle. Keep the distro running (e.g. a scheduled task running `wsl -d Ubuntu --exec sleep infinity` at log-on) or homologation deploys will queue until it's back.

### 5. Development: Docker Desktop

There's no platform stack in development. Each application runs with host ports on `127.0.0.1` against the PostgreSQL installed on Windows, through the same `deploy.sh`, from Git Bash:

```bash
export YGG_SECRETS_DIR=D:/Repositories/yggdrasil/env      # filled-in *.env there are gitignored
cp ../heimdall-api/docker/local.env.example env/development/heimdall-api.env   # and fill in
scripts/deploy.sh development heimdall-api ../heimdall-api dev
scripts/deploy.sh development heimdall-ui  ../heimdall-ui  dev
```

To debug from the IDE, keep running the APIs with `dotnet run` and the UIs with `flutter run -d chrome`, as before.

### Application env files

On each host, `/etc/yggdrasil/<environment>/<stack>.env`, where `<stack>` is a catalog application id (`heimdall-api`, `fortuna-ui`, ...). For the APIs, start from the repository's own `docker/production.env.example`, which documents every variable, then apply `env/<environment>/<stack>.env.example` from here. For the UIs, the template here is the whole file.

Each web UI calls its API **same-origin**: the UI is built with `https://<ui-host>` as its API base URL, and Traefik sends `https://<ui-host>/api/…` to the API unchanged (every API route already starts with `/api/`; see `stacks/*-api.proxy.yml`), so no CORS is involved. fortuna-api has no CORS support at all. The APIs also keep their own host name (`<app>-api.<domain>`) for the mobile and desktop clients.

## Observability

- **Grafana**: `https://grafana.<domain>`, with Prometheus and Loki provisioned. Good starting dashboards to import: *ASP.NET Core* (19924), *Traefik* (17346), *Jenkins* (9964).
- **Metrics**: each API serves `/metrics` (OpenTelemetry, Prometheus format) on port **9464**, which is not published and not routed. Prometheus takes its targets from the status API's HTTP service discovery, which lists every catalog entry with `metrics`, labelled `system`, `app` and `kind`. It reaches them over the `telemetry` network.
- **Status**: the console, for "is it up, and which version?" at a glance. Grafana is for "why?".
- **Logs**: Alloy ships the stdout of every container to Loki, with labels `environment`, `stack`, `service`, `container` and `level`. The APIs already log JSON (Serilog), so in Grafana, `{stack="heimdall-api"} | json | Level="Error"`. Their rolling files in the `logs` volumes stay the durable copy.

## Day to day

| Task | How |
|---|---|
| Release | `git switch develop && git pull && git switch -c release/1.4.0 && git push -u origin release/1.4.0`, then open the PR into `main` |
| Deploy by hand | `scripts/deploy.sh <env> <stack> <checkout> <tag>` on the host |
| Update the platform | `git -C /opt/yggdrasil pull && /opt/yggdrasil/scripts/platform.sh up` |
| Change Jenkins | Edit `casc.yaml`, pull on the VPS, `platform.sh up` (the controller reloads it on start) |
| Change GitHub policies | Edit `github/rulesets.py`, run `python github/rulesets.py` (`--dry-run` first) |
| Add a system or application | [Adding an application](#adding-an-application) |
| See what runs where | The console, or `curl -H "Authorization: Bearer $TOKEN" https://yggdrasil.<domain>/api/status` |
