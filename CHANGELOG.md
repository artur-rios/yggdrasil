# Changelog

All notable changes to yggdrasil are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A proprietary `LICENSE` (all rights reserved), and a Legal section in the README.

### Changed

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
- The console names an environment it adopts from a response, and shows it in the overview, by the response's
  `environmentName` (its display name) rather than its id.
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
  [docs/examples/docker-desktop-wsl-vps](docs/examples/docker-desktop-wsl-vps/README.md) worked example.

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

[Unreleased]: https://github.com/artur-rios/yggdrasil/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/artur-rios/yggdrasil/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/artur-rios/yggdrasil/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/artur-rios/yggdrasil/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/artur-rios/yggdrasil/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/artur-rios/yggdrasil/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/artur-rios/yggdrasil/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/artur-rios/yggdrasil/releases/tag/v0.1.0
