# Changelog

All notable changes to yggdrasil are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The variables store** ([docs/variables.md](docs/variables.md)): an encrypted SQLite database per machine
  (`vars.db` and `vars.key` in the secrets directory) for the platform's settings and every application's variables, managed by
  `scripts/vars.py` and `scripts/ygg.sh vars`. Values are defined in layers (`@<environment>`, `<application>`,
  `<application>@<environment>`), one application can take another's value with `${ref:<application>:<KEY>}`, every value is
  encrypted, secrets are masked, and every change is recorded in a history that `vars rollback` can undo. Also
  `vars import` (including `--all`, with move-up offers for shared values), `vars export`, `vars edit`,
  `vars backup` and `vars check` (strict for what the machine runs; `--platform` and `--usable` for the scripts
  that need less). It is opt-in.
- `scripts/platform.sh up --last-good`: starts the platform from the copy of its settings saved by the last successful
  `up` from the store, without opening the store (for a lost or wrong key).
- `ygg.sh` menu entry **Variables and secrets**.

### Changed

- `scripts/deploy.sh`, `scripts/platform.sh` and `scripts/ygg.sh` read the variables store when `vars.db` exists in the
  secrets directory, and the env files as before when it doesn't (with a notice). The store is checked before every deploy
  (that application's values) and platform start (the platform's values); `ygg.sh` checks only that it is usable.
- The deploy lock moved from the application's env file to `<secrets>/locks/<application>-<environment>.lock`. `platform.sh up`
  creates the directory (group `docker`, mode `2770`).
- The Jenkins agent image installs `python3-cryptography`, and `platform/compose.yml` mounts `<secrets>/locks` read-write
  next to the read-only secrets directory.

### Upgrading from 0.5 to 0.6

Optional: nothing changes until you run `scripts/ygg.sh vars init`; env files keep working.

1. On every host, `git pull`, then `scripts/platform.sh up` once. The Jenkins agent gets its new package and the `locks`
   mount, after which deploys work with either source, files or the store. The scripts need `python3-cryptography` on the host
   only to use the store (`apt install python3-cryptography`).
2. To move a host to the store, as in [docs/variables.md](docs/variables.md#moving-to-the-store): copy `/etc/yggdrasil`,
   `scripts/ygg.sh vars init` (store the key it prints), `scripts/ygg.sh vars import --all` (answer the move-up offers),
   `scripts/ygg.sh vars check`, `scripts/platform.sh up`, redeploy one application, and delete the `*.env.imported` files
   once everything runs.
3. Schedule `scripts/ygg.sh vars backup <dir>`
   ([backup and recovery](docs/variables.md#backup-and-recovery)).

## [0.5.1] - 2026-10-08

### Fixed

- `scripts/deploy.sh`: a deploy that fails in an `onDemand` environment that wasn't running is stopped again, also
  when there was nothing to roll back to. A first deploy that never became healthy was left restarting.

## [0.5.0] - 2026-10-08

### Added

- **Several environments on one host.** One Docker engine can run any number of environments: the default catalog
  runs development, homologation and production on one VPS, plus `local` on a developer's Docker Desktop ([docs/examples/docker-desktop-and-vps](docs/examples/docker-desktop-and-vps/README.md)). Every name an
  environment's containers share is qualified with the environment (see Changed), and one platform stack, one Jenkins
  agent, one `DOMAIN` and one `*.DOMAIN` certificate and DNS record serve every environment of a host.
- Two environment options in `catalog.yaml`, overridable per application like the others:
  - `hostSuffix` (default empty), appended to every application's `host` in the environment, so environments sharing
    a host and its domain get their own host names (`heimdall-dev.<domain>`, `heimdall-hml.<domain>`). It must keep
    `<host><hostSuffix>` one DNS label of at most 63 characters.
  - `onDemand` (default `false`): the environment runs only while it is used.
- `scripts/ygg.sh env start <environment>`, `env stop <environment> [--force]` and `env status`, also in the menu
  ("Start or stop an environment (on demand)"): start or stop every application of one of the host's environments.
  `stop` refuses an environment that isn't `onDemand` unless `--force`.
- `scripts/deploy.sh` in an `onDemand` environment: an application that wasn't running before the deploy (switched off,
  or never deployed) is still started and waited for, so a broken build is caught and rolled back, then stopped again.
  `DEPLOY_START=1` leaves it running; Jenkins sets it for **Build with Parameters → `DEPLOY_TO`** builds, and `DEPLOY_TO` now
  offers `branch` environments as well as `manual` ones.
- A `stopped` application status in the status API and the console: an application stopped normally in an on-demand
  environment. Neutral, not a problem; it isn't probed.
- `scripts/catalog.py environment <environment> [<option>]`, an environment's own options with the defaults applied.
- `ENVIRONMENTS` in `platform.env`: the environments a host runs, comma-separated (`development,homologation,production`).
  The status container gets it as `YGGDRASIL_ENVIRONMENTS`.
- Prometheus targets and Loki log lines of applications carry an `environment` label.
- [docs/setup.md](docs/setup.md#on-demand-environments) has an *On-demand environments* section, a *Planning the
  hosts* table, and how to add an environment to a host that already runs others.
- A proprietary `LICENSE` (all rights reserved), and a Legal section in the README.

### Changed

- **The status API contract is version 2** ([docs/status-api.md](docs/status-api.md)): one response per host, with
  `host`, an `environments` list (`id`, `name`, `onDemand`, `status`) and, per system, `environments[]` each with its
  `status` and `applications`. Application objects are unchanged. The host, every environment and every system have a
  status from one roll-up rule in which `not_deployed` and `stopped` members never make anything worse. Consoles before
  0.5 can't read it; the 0.5 console reads v1 and v2.
- The status API serves every environment of its host (`YGGDRASIL_ENVIRONMENTS`; the legacy `YGGDRASIL_ENVIRONMENT`
  counts as a list of one). It finds an application in an environment as the Compose project
  `<container.project or id>-<environment>`, probes the catalog's `health` with the host qualified
  (`http://heimdall-api.production:8080/healthcheck`), and builds `url` with the environment's `hostSuffix`. Platform
  components are probed once per refresh and appear in every environment. `/internal/prometheus/targets` has one group
  per environment and application, `<metrics host>.<environment>:<port>`, labelled `environment`. Restarts no longer
  count as "restarted recently" in an on-demand environment, where `scripts/ygg.sh env start` restarts containers.
- The console shows a **host** with all its environments: one status chip per environment in the header and on each
  system card, and, expanded, a section per environment with its applications. The saved connections are called
  hosts in every screen and dialog (**Hosts**, **Add host**); a host adopts its name from the response's `host`. The
  offline demo shows the default setup on `example.com`.
- `scripts/deploy.sh` names everything per environment: the Compose project `<application>-<environment>`
  (`heimdall-api-production`), `YGG_ENVIRONMENT` exported for the overlays, image tags `<environment>-<version>`
  (`production-1.4.0-3f2a9c1`; rollback and pruning only consider the environment's own tags), and a
  `yggdrasil.environment` label on every container. The rollback target is the newest container of the project,
  running or not.
- The stack overlays give each application the network alias `<application>.<environment>` on `edge` and `telemetry`,
  and name its Traefik routers and services `<application>-<environment>`; their labels are a list, so the names can
  use `${YGG_ENVIRONMENT}`. `scripts/ygg.sh add` writes new stack files the same way.
- The catalog's `health` and `metrics` stay written with the plain id; for every application that isn't
  `kind: platform`, the status API and Prometheus qualify the host with the environment.
- `scripts/catalog.py validate` checks `hostSuffix` and `onDemand`, that `<host><hostSuffix>` stays one DNS label, and
  rejects two (application, environment) pairs whose Compose projects `<application>-<environment>` would be the same.
- `platform.env` takes `ENVIRONMENTS` instead of `ENVIRONMENT`. `scripts/platform.sh up` checks each of them against
  the catalog. A `platform.env` with only `ENVIRONMENT` still works, as a list of one; `ENVIRONMENTS` wins when both are
  set.
- Jenkins creates one agent per distinct `agent` of the catalog's environments: one per host. The default catalog's
  development, homologation and production share the agent `vps`.
- `scripts/ygg.sh` works with every environment of the host: `status` lists each of them, `config [<app>]
  [<environment>]` and `add` take or ask for the environment, and `YGG_ENVIRONMENT` still forces one.
- Alloy labels each log line with its container's `yggdrasil.environment` label instead of the host's `ENVIRONMENT`;
  platform containers have none. The `stack` label is the Compose project, so `heimdall-api-production`.
- The default `catalog.yaml` has four environments: `local` (Docker Desktop, `ports`, `manual`), and on one VPS with the
  agent `vps`, `development` (from `develop`, `hostSuffix: -dev`, on demand), `homologation` (from `release/*`,
  `hostSuffix: -hml`, on demand) and `production` (`release`).
- [docs/setup.md](docs/setup.md) follows that layout by default, a host per environment staying an option;
  [docs/dns.md](docs/dns.md) uses one `*.<DOMAIN>` record per host, environments told apart by `hostSuffix`, instead of
  a sub-domain per environment. `templates/application/` describes the default environments and on-demand
  environments.
- `templates/application/CONTRIBUTING.md` also sets out the commit message, changelog and Semantic Versioning
  conventions of an application repository, has each release's changelog (and version file, if any) finalized on
  `develop` before the release branch is cut, and covers releasing an application that has no release environment.
- [docs/setup.md](docs/setup.md) clones the fork's `main` branch, since `develop` is this repository's default branch.
- `release.yml` builds and attaches the console's installable apps only when the release's tag is on `main`.
- `scripts/catalog.py validate` also enforces the rules the status API applies when it loads the catalog (application
  and system names, at least one system and one application per system, unique system ids, `metrics` as host:port,
  `metricsPath`, `host`, `repository` and `checks` formats), so CI rejects a catalog that would stop the status API on
  every host. It also rejects `true`/`false` as a number, and an application that switches an environment to
  `trigger: branch` without any `branches`.
- `scripts/deploy.sh` runs one deploy of a stack to an environment at a time, locking the env file (with `flock`, when
  available): a second deploy waits for the first.
- `scripts/ygg.sh config` also masks `*_PREVIOUS` keys kept during a rotation and connection strings.
- `DOCKER_GID` is now required in every host's `platform.env`, not only with the `agent` profile: the Docker socket
  proxies run in the host's `docker` group.
- The status API reads only Docker's container list. `container.startedAt` is the container's creation time when it
  has not restarted, and as precise as Docker's "Up 5 minutes" when it has; "restarted in the last 10 minutes" now
  counts any start more than 5 minutes after the container's creation, so a reboot of the host or of Docker counts too.
- The console uses `flutter_riverpod` 3 (from 2.6.1). Its automatic retry of failing providers is turned off, so a
  failure to load the saved environments still shows at once and the status polling keeps its own schedule.

### Deprecated

- `container.restartCount` in the status API's responses is always `null`: Docker only gives the count with the
  container inspect, which the status API may no longer read. The console already shows nothing for `null`.

### Removed

- The rule "one environment per Docker engine".
- The plain `<application>` network alias: an application is `<application>.<environment>`. Calls between applications
  go to the one of their own environment.
- Version 1 of the status API's `/api/status` and `/api/systems/{id}` (`environment`, `environmentName`,
  `systems[].applications`).
- The `ENVIRONMENT` variable of the Alloy container.
- `docs/examples/docker-desktop-wsl-vps`, with its homologation in WSL on the workstation: replaced by
  [docs/examples/docker-desktop-and-vps](docs/examples/docker-desktop-and-vps/README.md), with env file templates for
  four applications in four environments.

### Fixed

- `scripts/deploy.sh` rolled a failed release of a stack that also runs a database (or any other service with an image
  of its own) back to that service's tag (`postgres:16` gave `16`), which failed and left the application down. It now
  rolls back to the previous image of the stack's own build, and prunes old images by the same names.
- `scripts/deploy.sh` reports an env file it cannot read up front, and labels deployments with the 7-character commit
  Jenkins puts in the tag even when `git` would abbreviate it longer.
- `scripts/github.sh wait-checks` no longer fails a release on a single unanswered request to GitHub while it waits for
  the checks, and every failed GitHub call (a refused merge, say) now prints GitHub's reason in the build log.
- `scripts/platform.sh up` and `scripts/ygg.sh` matched `ENVIRONMENT` and application ids as regular expressions, so
  `prod.*` passed for `production`.
- The console could show, after an environment's URL was edited, the late answer of the old URL, and keep it as that
  environment's last known status.

### Security

- The status API, which answers the internet behind its token, could read every container's environment (every
  application's database passwords, signing keys and tokens), logs and files: its Docker socket proxy
  (`tecnativa/docker-socket-proxy` with `CONTAINERS=1`) also allows the container inspect, `/logs` and `/archive`. It
  now reads Docker through a `wollomatic/socket-proxy` (pinned by digest) that allows only `GET /containers/json`, and
  only from the status API's container.
- Traefik and Alloy no longer mount the Docker socket, whose `:ro` doesn't restrict the API: either could create or
  exec into containers, i.e. take over the host. Each reads Docker through its own socket proxy on a private network,
  which allows only the read-only requests it makes (Traefik: version, container list, inspect and events; Alloy:
  container and network lists, inspect and logs). Only the Jenkins agent, which deploys, still holds the socket.
  `scripts/test_socket_proxies.py` checks the allowlists, and CI runs Traefik and Alloy through them.

### Upgrading from 0.4 to 0.5

For a host that runs one environment, as every 0.4 host does. Read it through first: steps 4 to 6 happen between the
pull and the first deploy of each application.

1. **Update the consoles first.** Install the Windows and Android apps of the 0.5 release: they read the status API of
   0.4 and 0.5 hosts, while a console before 0.5 can't read a 0.5 host. The web console updates with its host.
2. **Pull**, on every host: `cd /opt/yggdrasil && git pull`. If your fork carries its own catalog, merge the release
   into it first; to move several environments onto one host, give them the same `agent`, a `hostSuffix` each and,
   for those used only now and then, `onDemand: true` ([docs/catalog.md](docs/catalog.md#several-environments-on-one-host)).
3. **`platform.env`**: replace `ENVIRONMENT=<id>` with `ENVIRONMENTS=` listing every environment the host runs, e.g.
   `ENVIRONMENTS=development,homologation,production` (an `ENVIRONMENT` alone still works, as one environment). Then
   `scripts/platform.sh up` and `docker restart yggdrasil-status-1`.
4. **Your own stack files**, if you wrote any: give the proxy overlay the alias `<id>.${YGG_ENVIRONMENT}` instead of
   `<id>`, name its routers and services `<id>-${YGG_ENVIRONMENT}`, and write its labels as a list, as
   [stacks/heimdall-api.proxy.yml](stacks/heimdall-api.proxy.yml) does.
5. **Remove the old projects before the first deploy** of each application. The project was `<application>`; it is now
   `<application>-<environment>`, and the old containers keep their routers and aliases, which collide with the new
   ones. On each host, for each application:
   ```bash
   docker compose -p heimdall-api down
   ```
   The application is down from then until its first 0.5 deploy: do it right before deploying
   (`scripts/ygg.sh config <application> <environment>` → Apply, or a Jenkins build).
6. **Volumes** are named after the project, so they change name too: `heimdall-api_logs` becomes
   `heimdall-api-production_logs`. If the data matters, copy it after step 5 and before the first deploy:
   ```bash
   docker volume create heimdall-api-production_logs
   docker run --rm -v heimdall-api_logs:/from -v heimdall-api-production_logs:/to alpine cp -a /from/. /to/
   ```
   Then remove the old volume when you no longer need it.
7. **Images**: the old `<application>:<version>` images are neither rollback targets nor pruned, since only
   `<application>:<environment>-*` tags are. The first deploy after the upgrade has nothing to roll back to. Remove the
   old images by hand once it is healthy (`docker image ls heimdall-api`, then `docker image rm`).
8. **Application env files** keep their path, `<secrets>/<environment>/<application>.env`. Replace every URL that uses
   the old plain alias (`http://<application>:<port>`): with `http://<application>.<environment>:<port>`, or with the
   public HTTPS host where the application requires HTTPS (fortuna-api's `FORTUNA_HEIMDALL_BASE_URL`). An environment
   new on the host needs its own env files, with the suffixed host names in `PUBLIC_HOST` and `UI_HOST`
   (`heimdall-api-dev.example.com`).
9. **The Jenkins agent**, if the host's environments now share an `agent` (`vps` in the default catalog): restart the
   controller (`docker restart yggdrasil-jenkins-1`), which creates the new agent; set `JENKINS_AGENT_NAME=vps` and
   its secret (**Manage Jenkins → Nodes → vps**) in `platform.env` and run `scripts/platform.sh up`; then delete the old
   per-environment nodes in **Manage Jenkins → Nodes**, which Jenkins never deletes by itself.
10. **Grafana**: Loki and Prometheus queries that relied on the `environment` label of platform containers find none
    now, and Loki's `stack` label is the Compose project (`heimdall-api-production`, no longer `heimdall-api`). Update
    saved queries and dashboards.

## [0.4.0] - 2026-09-29

### Added

- `scripts/ygg.sh`, a host helper menu for Ubuntu hosts whose entries are also commands: check and install the tools
  (Docker from Docker's apt repository, the docker group, the secrets directory, ufw), set up an application (catalog
  entry, stack files, env file, first deploy), see what runs on the host, and change an application's env file and
  redeploy it ([docs/cli.md](docs/cli.md)).
- `scripts/catalog.py add-application`, which inserts into `catalog.yaml` keeping its comments, plus the `systems` and
  `show` commands.
- `scripts/deploy.sh` exports `APP_ENV_FILE`, so a stack can hand the env file to the container.

## [0.3.3] - 2026-09-28

### Fixed

- A release build no longer ends in failure after merging and tagging when GitHub has already deleted the release
  branch (`delete_branch_on_merge`, which `github/rulesets.py` turns on).

## [0.3.2] - 2026-09-25

### Fixed

- Traefik builds HTTPS routers again: the `websecure` entrypoint no longer refers to the unknown TLS options
  `default@file`, which made Traefik close every TLS handshake.

## [0.3.1] - 2026-09-25

### Added

- [docs/dns.md](docs/dns.md), a step-by-step DNS and certificates guide for Cloudflare with your own domain or a free
  DuckDNS subdomain, including a first run against the Let's Encrypt staging CA and troubleshooting.

### Changed

- [docs/setup.md](docs/setup.md) is rewritten as ordered steps from an empty GitHub account to a first release, with
  the GitHub App setup and host preparation detailed command by command.
- `env/platform.env.example` marks every required variable and ships `COMPOSE_PROFILES` empty, since an agent needs a
  secret that only exists once the controller has created it. Env files are expected to be mode 640 in
  `/etc/yggdrasil` (group docker, mode 2750) so the Jenkins agent can read them.
- `scripts/deploy.sh`'s missing env file error points at the real cause, including the agent's read access.

### Fixed

- Traefik is updated from v3.5 to v3.6, which negotiates the API version with Docker Engine 29; v3.5 saw no
  containers there, so it had no routes and no certificate.
- The Cloudflare API token documented in `env/acme.env.example` also needs Zone > Zone > Read.
- `scripts/__pycache__` is no longer tracked, so running the catalog tooling no longer leaves modified files.

## [0.3.0] - 2026-09-18

### Added

- Any number of environments in `catalog.yaml`, in promotion order, each with its options: `mode` (`proxy` or
  `ports`), `trigger` (`manual`, `branch` or `release`), `branches`, `agent`, `approval`, `waitTimeout`, `keepImages`
  and `checksTimeout`. An application deploys to every environment or to its own list, and can override any option per
  environment.
- `scripts/catalog.py` validates the catalog and resolves each application's environment options.
- The GitHub owner and repository come from the catalog, and the ACME DNS provider is configurable.
- [docs/catalog.md](docs/catalog.md) (catalog reference), [docs/setup.md](docs/setup.md) (setup guide),
  `templates/application/` (what each application repository needs) and the
  `docs/examples/docker-desktop-wsl-vps` worked example.

### Changed

- `deploy.sh`, the Jenkins pipeline (one stage per environment, approval, `deploy/<environment>` statuses), the
  Jenkins agents and job branches, `rulesets.py` and the status API follow the catalog's environments instead of a
  fixed development, homologation and production.

### Fixed

- `scripts/catalog.py` prints plain LF on Windows, so Git Bash callers get no trailing carriage return.

## [0.2.0] - 2026-09-18

### Added

- The console as an installable Windows app: an Inno Setup installer, per-user by default, with a Start menu entry, an
  optional desktop shortcut and in-place upgrades, and the Visual C++ runtime bundled.
- The Windows installer and the Android APK are attached to every `v*` GitHub release.

## [0.1.0] - 2026-09-18

### Added

- The platform each environment host runs: Traefik with Let's Encrypt DNS-01 (Cloudflare) wildcard certificates,
  Prometheus, Loki, Alloy and Grafana, and a Jenkins controller and WebSocket agents configured as code.
- `catalog.yaml`, listing every system and the applications it is made of. Jenkins jobs, GitHub rulesets, Prometheus
  targets and `deploy.sh` read it.
- `scripts/deploy.sh`, which builds, waits for health and rolls back an application, labels every container with the
  version, commit and time of its deployment and restores the previous labels on rollback.
- The shared Jenkins pipeline, which deploys release branches and green release pull requests into `main`, then
  merges, tags and deletes the branch through `scripts/github.sh`.
- `github/rulesets.py`, the repositories' branch and tag rulesets as code.
- The status API (.NET 10), one per environment host: it probes every application's health endpoint, reads container
  state and deployment labels through a read-only Docker socket proxy, and serves `/api/status` behind a bearer token
  and Prometheus HTTP service discovery ([docs/status-api.md](docs/status-api.md)).
- The console (Flutter) for the web and Android, showing every system's status and, expanded, each of its
  applications.

[Unreleased]: https://github.com/artur-rios/yggdrasil/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/artur-rios/yggdrasil/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/artur-rios/yggdrasil/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/artur-rios/yggdrasil/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/artur-rios/yggdrasil/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/artur-rios/yggdrasil/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/artur-rios/yggdrasil/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/artur-rios/yggdrasil/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/artur-rios/yggdrasil/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/artur-rios/yggdrasil/releases/tag/v0.1.0
