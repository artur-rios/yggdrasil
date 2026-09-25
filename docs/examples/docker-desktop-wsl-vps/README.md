# Example: Docker Desktop, WSL and a Linux VPS

A complete installation on two machines: a Windows workstation and a Linux VPS. It is the setup
this repository's `catalog.yaml` describes: two systems, each an ASP.NET Core API with a Flutter
web UI, and three environments.

| Environment | Where | `mode` | `trigger` | Reached at |
|---|---|---|---|---|
| `development` | Windows workstation, **Docker Desktop** | `ports` | `manual` | `http://127.0.0.1:<port>` |
| `homologation` | Same workstation, **Docker Engine inside WSL Ubuntu** | `proxy` | `branch` (`release/*`) | `https://*.hml.<domain>` |
| `production` | **Linux VPS** (Ubuntu), also runs the Jenkins controller | `proxy` | `release` | `https://*.<domain>` |

```mermaid
flowchart TB
    subgraph ws [Windows workstation]
        subgraph dd [Docker Desktop · development]
            devapps[applications on 127.0.0.1 ports]
        end
        subgraph wsl [WSL Ubuntu · Docker Engine · homologation]
            hmltraefik[Traefik :443<br/>*.hml.domain] --> hmlapps[applications]
            hmlagent[Jenkins agent] --> hmlapps
            hmlstatus[status API · monitoring]
        end
        pg[(PostgreSQL<br/>on Windows)]
        devapps --> pg
        hmlapps --> pg
    end
    subgraph vps [Linux VPS · production]
        traefik[Traefik :443<br/>*.domain] --> apps[applications]
        jenkins[Jenkins controller] --- agent[Jenkins agent] --> apps
        status[status API · monitoring]
        vpspg[(PostgreSQL)]
        apps --> vpspg
    end
    gh([GitHub]) -- webhooks --> jenkins
    hmlagent -. WebSocket, outbound .-> jenkins
```

The environment section of the catalog:

```yaml
environments:
  - id: development
    name: Development
    mode: ports
    trigger: manual
  - id: homologation
    name: Homologation
    mode: proxy
    trigger: branch
    branches: release/*
  - id: production
    name: Production
    mode: proxy
    trigger: release
```

Env file templates for every application in each environment are in [`env/`](env). They use
`example.com` as the domain: replace every `example.com` with yours (`hml.example.com` →
`hml.<your domain>`), and fill in the secrets.

In this page, `<domain>` stands for your domain, e.g. `example.dev`. Type the real value in the files:
`platform.sh` reads them with bash, which fails on a literal `<domain>`.

## DNS

Cloudflare hosts the domain (`ACME_DNS_PROVIDER=cloudflare`, `CF_DNS_API_TOKEN` in `acme.env`: an API token with **Zone → Zone → Read** and **Zone → DNS → Edit** on the zone; step by step in [dns.md](../../dns.md)).

| Record | Points at | Proxy |
|---|---|---|
| `*.<domain>` | The VPS's public IP | Either |
| `*.hml.<domain>` | The workstation's **LAN** IP | **DNS only** (grey cloud) |

Homologation is only reachable on the LAN. The DNS-01 challenge still issues it a real certificate, because Let's Encrypt never has to connect to it. Some home routers drop DNS answers that point at private addresses ("DNS rebinding protection"). If `*.hml.<domain>` doesn't resolve at home, allow the domain in the router, or add hosts-file entries.

## Production: the VPS

Production comes first: it runs the Jenkins controller that homologation's agent connects to. Follow
[setup.md](../../setup.md) steps 6 to 9, then 11. The lines of `platform.env` this example fixes, on
top of every variable of [setup.md 9.2](../../setup.md#92-the-env-files-first-pass):

```
ENVIRONMENT=production
DOMAIN=example.dev
JENKINS_URL=https://jenkins.example.dev/
# jenkins alone at step 9.2, jenkins,agent from step 9.5
COMPOSE_PROFILES=jenkins,agent
JENKINS_AGENT_NAME=production
JENKINS_AGENT_URL=http://jenkins:8080/
```

Keep comments on their own lines, as here: `platform.sh` and Compose read the file differently, and
a comment after a value isn't safe in both.

PostgreSQL runs on the VPS itself. The API stacks reach it at `DB_HOST=host.docker.internal`, which
their Compose files map to the host's Docker bridge (`extra_hosts: host-gateway`). By default
PostgreSQL only listens on `localhost`, and `ufw` drops the containers' connections, so:

1. `postgresql.conf`: `listen_addresses = 'localhost,172.17.0.1'` (the Docker bridge's address:
   `ip -4 addr show docker0`).
2. `pg_hba.conf`: allow the Compose networks, e.g.
   `host all all 172.16.0.0/12 scram-sha-256`.
3. `sudo ufw allow from 172.16.0.0/12 to any port 5432 proto tcp`, then
   `sudo systemctl restart postgresql`.
4. Check from a container:
   `docker run --rm --add-host h:host-gateway postgres:17 pg_isready -h h` prints
   `h:5432 - accepting connections`.

## Homologation: WSL Ubuntu on the workstation

Homologation needs its **own** Docker Engine, separate from Docker Desktop's: one environment per engine.

1. **Separate the engines.** Docker Desktop → Settings → Resources → WSL integration: **turn it off for this distribution**. Otherwise `docker` inside Ubuntu *is* Docker Desktop, the development engine, and the two environments would replace each other's containers.
2. **Docker Engine inside Ubuntu** (same install as the VPS), with systemd: `/etc/wsl.conf` → `[boot]` `systemd=true`, then `wsl --shutdown`.
3. **Mirrored networking.** In `%UserProfile%\.wslconfig`, `[wsl2]` `networkingMode=mirrored`. This makes WSL's ports 80/443 the workstation's own, and lets WSL reach services on Windows. Then allow inbound 80/443 for WSL in the Hyper-V firewall, so other LAN devices (a phone testing the app) can reach it. In an administrator PowerShell:
   ```powershell
   New-NetFirewallHyperVRule -Name WSL-Web -DisplayName "WSL 80/443" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 80,443 -Action Allow
   ```
   `-VMCreatorId` is WSL's fixed id. **Docker Desktop must not publish 80 or 443 at the same time.**
4. **PostgreSQL.** Inside WSL, `host.docker.internal` is the WSL machine itself, not Windows. Either:
   - install PostgreSQL inside WSL, closer to production, and set it up as on the VPS above; or
   - keep the one installed on Windows: set `DB_HOST` to the workstation's LAN IP, allow port 5432
     in Windows Firewall, and allow the WSL and Docker addresses (`172.16.0.0/12`) in its
     `pg_hba.conf`.
5. **The platform**, as in [setup.md](../../setup.md#10-bring-up-the-other-environment-hosts) step 10, once production is up: the agent's secret comes from **Manage Jenkins → Nodes → homologation** on the controller. The lines this example fixes:
   ```
   ENVIRONMENT=homologation
   DOMAIN=hml.example.dev
   COMPOSE_PROFILES=agent
   JENKINS_URL=https://jenkins.example.dev/
   JENKINS_AGENT_NAME=homologation
   JENKINS_AGENT_SECRET=<the secret from the controller>
   # empty: the agent dials the VPS out over a WebSocket, so no inbound port
   JENKINS_AGENT_URL=
   ```
   Plus `DOCKER_GID` and the ACME, Traefik, Grafana and status token variables of step 10.
6. **Keep WSL running.** WSL stops when idle, and homologation deploys queue until it's back. A scheduled task at log-on running `wsl -d Ubuntu --exec sleep infinity` keeps it up.

## Development: Docker Desktop

No platform stack and no Jenkins: `deploy.sh` publishes each application on the host and points it at the PostgreSQL on Windows. From Git Bash, in your checkout of the fork, with the application repositories cloned next to it (`../heimdall-api`, `../heimdall-ui`, ...):

```bash
python3 --version && pip install pyyaml              # once, for scripts/catalog.py
export YGG_SECRETS_DIR="$PWD/env"                    # filled-in env/**/*.env files are gitignored
mkdir -p env/development
cp ../heimdall-api/docker/local.env.example env/development/heimdall-api.env   # and fill in
cp docs/examples/docker-desktop-wsl-vps/env/development/heimdall-ui.env.example env/development/heimdall-ui.env
scripts/deploy.sh development heimdall-api ../heimdall-api dev
scripts/deploy.sh development heimdall-ui  ../heimdall-ui  dev
```

- If `python3 --version` opens the Microsoft Store, install Python from python.org and turn off the
  `python3` *App execution alias* in Windows settings: `deploy.sh` calls `python3`.
- Give each API its own `API_PORT` in its env file (e.g. heimdall-api `8080`, fortuna-api `8083`),
  and point each UI's `*_API_BASE_URL` at it. The APIs' local templates publish on every interface,
  for testing from a phone; write `API_PORT=127.0.0.1:8080` to keep an API on this machine only.

To debug from the IDE, keep running the APIs with `dotnet run` and the UIs with `flutter run -d chrome`.

## The applications

Each system is an API plus a Flutter web UI:

| Application | Public host | Health | Metrics |
|---|---|---|---|
| `heimdall-api` | `heimdall-api.<DOMAIN>` | `/healthcheck` | `:9464` |
| `heimdall-ui` | `heimdall.<DOMAIN>` | `/healthz` | — |
| `fortuna-api` | `fortuna-api.<DOMAIN>` | `/healthcheck` | `:9464` |
| `fortuna-ui` | `fortuna.<DOMAIN>` | `/healthz` | — |

- **Same-origin APIs.** Each web UI calls its API on its own origin. It's built with `https://<ui-host>` as its API base URL, and the API's proxy overlay also routes `https://<ui-host>/api/…` to the API (every API route already starts with `/api/`). So the browser needs no CORS, which matters because fortuna-api has no CORS support at all. The APIs keep their own host names for the mobile and desktop clients.
- **Service to service.** fortuna-api validates tokens against heimdall-api over the `edge` network, at `http://heimdall-api:8080/`, not through the internet.
- **Homologation environment names.** heimdall-api runs as `Staging`, so e-mails are logged instead of sent and no Mailgun account is needed. fortuna-api runs as `Production`, because it treats every other ASP.NET environment as a debugging one.
- **API env files.** Start each from the application repository's own `docker/production.env.example`, then apply the template here: `PUBLIC_HOST`, `UI_HOST`, `ASPNETCORE_ENVIRONMENT`, `DB_HOST` and the other overrides.

## The console

- Web: `https://yggdrasil.<domain>` and `https://yggdrasil.hml.<domain>`.
- Windows or Android: add both environments on the **Environments** screen, with each host's `YGGDRASIL_STATUS_TOKEN`.
- For the production web console to also show homologation, set `YGGDRASIL_STATUS_CORS_ORIGINS=https://yggdrasil.<domain>` in homologation's `platform.env`. Browsers may still block, or ask permission for, a public page calling a LAN address; if it fails, use the Windows or Android console for homologation.
