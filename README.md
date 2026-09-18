# yggdrasil

The infrastructure that runs **heimdall** and **fortuna**, both the APIs and the web UIs, across three
environments: the reverse proxy, TLS, observability, the Jenkins deployment pipeline and the GitHub
policies that pipeline depends on.

| | Development | Homologation | Production |
|---|---|---|---|
| Where | Windows, Docker Desktop | Same machine, Docker Engine inside WSL Ubuntu | Ubuntu VPS |
| Reached at | `127.0.0.1:<port>` | `https://*.hml.<domain>` | `https://*.<domain>` |
| Traefik + Let's Encrypt | no | yes (DNS-01, wildcard) | yes (DNS-01, wildcard) |
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
      └── grafana.<domain> ── Prometheus (scrapes :9464/metrics) + Loki ◄── Alloy (container logs)
```

## Layout

| Path | What |
|---|---|
| `platform/compose.yml` | One host's platform: Traefik, Prometheus, Loki, Alloy, Grafana, plus the Jenkins controller and agent under the `jenkins` and `agent` profiles |
| `platform/jenkins/controller/casc.yaml` | Jenkins, configured entirely as code: users, agents, GitHub App credential, shared library, one multibranch job per repository |
| `jenkins/library/vars/yggdrasilPipeline.groovy` | The pipeline every application's `Jenkinsfile` calls |
| `stacks/` | Per-application Compose files and overlays: `*.proxy.yml` (Traefik, homologation and production), `*.ports.yml` (host ports, development) |
| `scripts/deploy.sh` | Build, deploy, health-wait, roll back one stack. Jenkins runs this; so can you |
| `scripts/github.sh` | Wait for checks, set status, merge, release, delete branch |
| `scripts/platform.sh` | Bring a host's platform up or down |
| `github/rulesets.py` | The GitHub rulesets and settings of the four repositories, as code |
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
3. Install the app on `heimdall-api`, `heimdall-ui`, `fortuna-api`, `fortuna-ui` and `yggdrasil`.

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
# fill it in, with COMPOSE_PROFILES=jenkins for now (the agent secret does not exist yet)
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

On each host, `/etc/yggdrasil/<environment>/<stack>.env`, where `<stack>` is `heimdall-api`, `heimdall-ui`, `fortuna-api` or `fortuna-ui`. For the APIs, start from the repository's own `docker/production.env.example`, which documents every variable, then apply `env/<environment>/<stack>.env.example` from here. For the UIs, the template here is the whole file.

Each web UI calls its API **same-origin**: the UI is built with `https://<ui-host>` as its API base URL, and Traefik sends `https://<ui-host>/api/…` to the API unchanged (every API route already starts with `/api/`; see `stacks/*-api.proxy.yml`), so no CORS is involved. fortuna-api has no CORS support at all. The APIs also keep their own host name (`<app>-api.<domain>`) for the mobile and desktop clients.

## Observability

- **Grafana**: `https://grafana.<domain>`, with Prometheus and Loki provisioned. Good starting dashboards to import: *ASP.NET Core* (19924), *Traefik* (17346), *Jenkins* (9964).
- **Metrics**: each API serves `/metrics` (OpenTelemetry, Prometheus format) on port **9464**, which is not published and not routed. Prometheus reaches it over the `telemetry` network. Traefik and Jenkins are scraped too (`platform/prometheus/prometheus.yml`).
- **Logs**: Alloy ships the stdout of every container to Loki, with labels `environment`, `stack`, `service`, `container` and `level`. The APIs already log JSON (Serilog), so in Grafana, `{stack="heimdall-api"} | json | Level="Error"`. Their rolling files in the `logs` volumes stay the durable copy.

## Day to day

| Task | How |
|---|---|
| Release | `git switch develop && git pull && git switch -c release/1.4.0 && git push -u origin release/1.4.0`, then open the PR into `main` |
| Deploy by hand | `scripts/deploy.sh <env> <stack> <checkout> <tag>` on the host |
| Update the platform | `git -C /opt/yggdrasil pull && /opt/yggdrasil/scripts/platform.sh up` |
| Change Jenkins | Edit `casc.yaml`, pull on the VPS, `platform.sh up` (the controller reloads it on start) |
| Change GitHub policies | Edit `github/rulesets.py`, run `python github/rulesets.py` (`--dry-run` first) |
