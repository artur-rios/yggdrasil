# Host Foundation Implementation Plan (plan 1 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a yggdrasil host run with nothing as root (rootless Docker per service user), in two roles
(dev machine, apps machine), with the platform able to serve several domains behind Cloudflare, Forgejo
as a platform service, build agents for CI and releases, backups, and the operator docs for all of it.

**Architecture:** `ygg install --role dev|apps` (as the admin, with sudo) configures the system and
creates the service users, then gives each its own rootless Docker engine; `ygg check --role …` reports
every item. `scripts/platform.sh` (run as `ygg`) refuses a rootful engine, mounts the user's own socket,
and renders Traefik's static configuration (domains, Cloudflare trust) from the platform's settings.
`scripts/agent.sh` (run as `ci` or `release`) brings up one Jenkins build agent in that user's engine.
One deliberate difference from the spec's wording ("new profiles `ci` and `release`"): the build agents get
a Compose file of their own (`platform/build-agent.yml`) instead of profiles in `platform/compose.yml`,
because every service of `compose.yml` without a profile (Traefik, monitoring) would otherwise start in the
`ci` and `release` engines too.

**Tech Stack:** Bash (shellcheck-clean), Python 3.10+ (standard library, PyYAML, cryptography: no new
packages), Docker Engine rootless + Compose v2.24+, Traefik v3.6, Forgejo (rootless image), systemd,
ufw, unittest.

**Spec:** `docs/superpowers/specs/2026-10-22-self-hosted-git-and-ci-design.md` — sections "The two
machines", "Users and privileges", "Forgejo › Installation", "yggdrasil platform changes", "Documentation",
"Implementation plans" (item 1).

**Not in this plan** (later plans): the git-server interface and catalog fields (plan 2); Jenkins-side
agent creation, executor counts, the Gitea plugin, Renovate's runner (plan 3; this plan only creates the
`renovate` user); Forgejo accounts, import, mirrors, rules (plans 2 and 4); building the real machines
(plan 4).

## Global Constraints

- Ubuntu 24.04 LTS is the target for `--role dev` and `--role apps`; `install` refuses other systems for
  those roles (as it does today for any role).
- `--role workstation` keeps today's `check`/`install` behavior exactly (rootful Docker, docker group): it
  is for laptops and WSL running `ports` environments.
- Users per role — apps: `ygg`. dev: `ygg`, `ci`, `release`, `renovate`, `dev`. The admin is the user
  running `install` (never created by it). SSH logins: the admin, `ygg`, and on dev `dev`. `ci`, `release`
  and `renovate` get `/usr/sbin/nologin`.
- Secrets directories: `ygg` → `/etc/yggdrasil`; any other service user `<u>` → `/etc/yggdrasil-<u>`.
  Each `750`, owned `<u>:<u>`; files in them `600`.
- `/opt/yggdrasil` (the checkout) is owned by `ygg`, mode `755`.
- Rootless engine per service user: socket `/run/user/<uid>/docker.sock`; port driver and network
  `slirp4netns` (keeps client source addresses); linger on.
- `ci`'s slice: `CPUQuota=600%`, `MemoryMax=20G`.
- `net.ipv4.ip_unprivileged_port_start=80`.
- Inbound ports — dev: 22, 80, 443, 2222. apps: 22; 80 and 443 only from Cloudflare's ranges
  (`https://www.cloudflare.com/ips-v4`, `https://www.cloudflare.com/ips-v6`).
- Forgejo: rootless image, pinned by version and digest like the other platform images; git over SSH on
  host port 2222; `DISABLE_REGISTRATION=true`; `REQUIRE_SIGNIN_VIEW=true`; SQLite; served at
  `git.${DOMAIN}`.
- Every file the installer writes outside a home directory is written under `$YGG_SYSROOT` (default
  empty), so tests can point it at a temporary directory.
- Tests: `python3 -m unittest discover -s scripts`, names `test_given_<condition>_when_<action>_then_<outcome>`,
  fake commands on `PATH` (the pattern of `scripts/test_ygg.py`), POSIX-only tests skipped elsewhere.
- Commit subjects: lowercase Conventional Commits, ≤50 characters, body wrapped at 72.

## Review Focus

1. **SSH lockout**: hardening SSH when the admin has no `~/.ssh/authorized_keys` must refuse, not cut off
   the only way in (Task 6 test).
2. **Running `install` twice**: users, config lines, ufw rules and profile lines must not duplicate, and a
   changed Cloudflare list must replace the old rules, not add to them (Tasks 6, 7 tests).
3. **Cloudflare's list can't be fetched**: the firewall step must leave the existing rules as they are and
   say so; never open 80/443 to everyone, never drop them (Task 7 test).
4. **An existing single-domain host upgrading**: a `platform.env` with `ENVIRONMENTS` but without the new
   `apps` profile, and without `EXTRA_DOMAINS`/`CLOUDFLARE_PROXIED`, must get one clear instruction, not a
   half-started platform (Task 2 test; Task 1 covers the missing new variables).
5. **`platform.sh` under `sudo` or as a user without an engine** (no `XDG_RUNTIME_DIR`, no socket): a clear
   message naming `ygg install`, never a fallback to `/var/run/docker.sock` (Task 2 test).

---

### Task 1: Traefik's static configuration, rendered for several domains and Cloudflare

**Files:**
- Create: `scripts/traefik_config.py`
- Create: `scripts/test_traefik_config.py`
- Modify: `platform/compose.yml` (service `traefik`: `command`, `volumes`, `healthcheck`)
- Modify: `scripts/platform.sh` (render the file on every command; keep it in `last-good`)
- Modify: `scripts/test_platform.py`
- Modify: `env/platform.env.example` (`EXTRA_DOMAINS`, `CLOUDFLARE_PROXIED`)

**Interfaces:**
- Produces: `render(settings: Mapping[str, str], cloudflare_ips: list[str] | None) -> dict` (the Traefik
  static configuration as a dict) and the CLI `python3 scripts/traefik_config.py [--cloudflare-ips FILE]`,
  which reads the settings from its environment and prints YAML; exit 2 with a message on a settings error.
- Produces: `platform.sh` exports `YGG_TRAEFIK_CONFIG_FILE` (absolute path of the rendered file) to Compose.
- Produces: the file `<secrets>/cloudflare-ips` (one CIDR per line) as the input when
  `CLOUDFLARE_PROXIED=true`; written by Task 7.
- Settings read: `DOMAIN` (required), `EXTRA_DOMAINS` (comma-separated, optional), `ACME_EMAIL`,
  `ACME_DNS_PROVIDER` (required), `ACME_CA_SERVER` (default `https://acme-v02.api.letsencrypt.org/directory`),
  `ACME_DNS_RESOLVERS` (default `1.1.1.1:53,8.8.8.8:53`), `CLOUDFLARE_PROXIED` (`true`/`false`, default
  `false`).

The output carries everything today's `command:` flags set (log, access log, ping on `metrics`, Docker
provider through `tcp://traefik-docker-proxy:2375` with `exposedByDefault: false` and `network: edge`, file
provider on `/etc/traefik/dynamic` watched, entry points `web` (redirect to `websecure`), `websecure`
(`certResolver: letsencrypt`), `metrics` on `:8082`, the ACME resolver with `storage: /letsencrypt/acme.json`,
Prometheus metrics with router labels, the dashboard), plus one `tls.domains` entry per domain
(`main: <d>`, `sans: ["*.<d>"]`), and, when proxied, `forwardedHeaders.trustedIPs` on `web` and `websecure`.

- [ ] **Step 1: Write the failing tests** in `scripts/test_traefik_config.py`:

```python
def test_given_one_domain_when_rendered_then_one_wildcard_certificate_domain(self):
    config = render(SETTINGS, None)
    self.assertEqual(config["entryPoints"]["websecure"]["http"]["tls"]["domains"],
                     [{"main": "techcraftsmen.net", "sans": ["*.techcraftsmen.net"]}])

def test_given_extra_domains_when_rendered_then_each_domain_gets_its_own_entry_in_order(self):
    config = render({**SETTINGS, "EXTRA_DOMAINS": "artur-rios.tech, example.org"}, None)
    self.assertEqual([d["main"] for d in config["entryPoints"]["websecure"]["http"]["tls"]["domains"]],
                     ["techcraftsmen.net", "artur-rios.tech", "example.org"])

def test_given_cloudflare_proxied_when_rendered_then_both_entry_points_trust_only_its_ranges(self):
    config = render({**SETTINGS, "CLOUDFLARE_PROXIED": "true"}, ["173.245.48.0/20", "2400:cb00::/32"])
    for name in ("web", "websecure"):
        self.assertEqual(config["entryPoints"][name]["forwardedHeaders"]["trustedIPs"],
                         ["173.245.48.0/20", "2400:cb00::/32"])

def test_given_not_proxied_when_rendered_then_no_forwarded_headers_are_trusted(self):
    self.assertNotIn("forwardedHeaders", render(SETTINGS, None)["entryPoints"]["websecure"])

def test_given_cloudflare_proxied_without_its_ranges_when_rendered_then_it_is_refused(self):
    with self.assertRaisesRegex(SettingsError, "cloudflare-ips.*ygg install"):
        render({**SETTINGS, "CLOUDFLARE_PROXIED": "true"}, None)

def test_given_a_domain_that_is_not_a_hostname_when_rendered_then_it_is_refused(self):
    with self.assertRaisesRegex(SettingsError, "EXTRA_DOMAINS"):
        render({**SETTINGS, "EXTRA_DOMAINS": "*.bad"}, None)

def test_given_the_defaults_when_rendered_then_acme_matches_todays_flags(self):
    acme = render(SETTINGS, None)["certificatesResolvers"]["letsencrypt"]["acme"]
    self.assertEqual(acme["caServer"], "https://acme-v02.api.letsencrypt.org/directory")
    self.assertEqual(acme["dnsChallenge"], {"provider": "cloudflare", "resolvers": ["1.1.1.1:53", "8.8.8.8:53"]})
```

with `SETTINGS = {"DOMAIN": "techcraftsmen.net", "ACME_EMAIL": "ops@techcraftsmen.net", "ACME_DNS_PROVIDER": "cloudflare"}`.
In `scripts/test_platform.py`:

```python
def test_given_an_env_file_when_up_then_compose_gets_a_rendered_traefik_config(self):
    # YGG_TRAEFIK_CONFIG_FILE is exported, exists while compose runs, and names both domains
def test_given_last_good_when_up_from_a_store_then_the_traefik_config_is_kept_too(self):
    # <secrets>/last-good/traefik.yml exists after a successful up from the store
```

(The fake `docker` writes `$YGG_TRAEFIK_CONFIG_FILE`'s content to the log so the test can read it.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_traefik_config scripts.test_platform -v`
Expected: FAIL (`ModuleNotFoundError: traefik_config`; the platform tests find no config in the log).

- [ ] **Step 3: Implement `render` and the CLI in `scripts/traefik_config.py`**; `SettingsError(ValueError)`
  for every refusal; a domain must match the catalog's host-label rules joined by dots
  (`^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$`). Print with `yaml.safe_dump(sort_keys=False)`.

- [ ] **Step 4: Wire it in `platform/compose.yml` and `scripts/platform.sh`**

`traefik`: `command: [--configFile=/etc/traefik/traefik.yml]`; add the volume
`${YGG_TRAEFIK_CONFIG_FILE:?run through scripts/platform.sh}:/etc/traefik/traefik.yml:ro`; healthcheck
`["CMD", "traefik", "healthcheck"]`. Move the comments that explained each flag next to the matching
code in `traefik_config.py`. In `platform.sh`: always create the private run directory (today only the
store path does: factor that block into `run_dir()`), render `traefik.yml` into it after the env file is
known (sourcing the env file in a subshell and exporting the settings, as `check_profile_variables` reads
them), pass `--cloudflare-ips "$secrets/cloudflare-ips"` when that file exists, and add `traefik.yml` to
the `last-good` copy. `--last-good` reads `last-good/traefik.yml`.

- [ ] **Step 5: Document the two variables in `env/platform.env.example`** next to `DOMAIN`:
  `EXTRA_DOMAINS` (more domains this host serves, each with its own wildcard certificate; e.g.
  `artur-rios.tech`) and `CLOUDFLARE_PROXIED` (`true` when the domains' records are proxied by Cloudflare:
  Traefik then trusts forwarded client addresses from Cloudflare's ranges only).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.test_traefik_config scripts.test_platform -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/traefik_config.py scripts/test_traefik_config.py scripts/platform.sh scripts/test_platform.py platform/compose.yml env/platform.env.example
git commit -m "feat: render traefik's config for several domains"
```

---

### Task 2: Rootless platform: the user's own socket, role profiles

**Files:**
- Modify: `platform/compose.yml` (socket-proxy anchor, `agent`, `status`/`docker-proxy`/`console`
  profiles, `prometheus` config file, header comments)
- Create: `platform/prometheus/prometheus-static.yml`
- Modify: `scripts/platform.sh`
- Modify: `scripts/test_platform.py`, `scripts/test_socket_proxies.py`
- Modify: `scripts/deploy.sh` (the env-file error message only), `env/platform.env.example`

**Interfaces:**
- Consumes: Task 1's render step in `platform.sh`.
- Produces: `platform.sh` exports `DOCKER_SOCKET` (path of the running user's rootless socket) and
  `PROMETHEUS_CONFIG_FILE` (`prometheus.yml` with profile `apps`, else `prometheus-static.yml`).
- Produces: profile names `apps` (status, docker-proxy, console), `forge` (Task 3), `jenkins`, `agent`.

Changes in `compose.yml`: the socket-proxy anchor drops `group_add` and gets `user: "0:0"` and the volume
`${DOCKER_SOCKET:?run through scripts/platform.sh}:/var/run/docker.sock:ro`; `agent` the same (`user: "0:0"`,
no `group_add`, socket read-write); `status`, `docker-proxy` and `console` get `profiles: [apps]`;
`prometheus` mounts `./prometheus/${PROMETHEUS_CONFIG_FILE:-prometheus.yml}`. Rewrite the comments that
explain the docker group: under rootless Docker, container root is the service user on the host, which is
why the proxies and the agent run as it. `DOCKER_GID` disappears from compose, `platform.sh` and the env
template.

`prometheus-static.yml`: the platform components of the dev machine as static targets, copied from the
`yggdrasil` system's entries in `catalog.yaml` that have `metrics` (excluding `status` and `console`), with
the same `system`/`app`/`kind` labels the status API's discovery gives them.

`platform.sh` gains, run before any Compose command:
- `docker_socket()`: the path in `DOCKER_HOST` when it is `unix://…`; else `/run/user/$(id -u)/docker.sock`
  when that socket exists; else die
  `"no rootless Docker engine for $(id -un): run 'ygg install' (docs/hosts.md)"`.
- `require_rootless()`: `docker info --format '{{json .SecurityOptions}}'` must contain `name=rootless`;
  else die `"the Docker engine at $DOCKER_SOCKET runs as root; the platform runs on rootless Docker only (docs/hosts.md)"`.
- `check_environments` only when the profiles include `apps`; and when `ENVIRONMENTS` is set but `apps` is
  not among the profiles, die `"ENVIRONMENTS is set but COMPOSE_PROFILES lacks apps: add apps to COMPOSE_PROFILES (the status API and console moved to that profile in this release)"`.
- The locks directory: `install -d -m 700 "$secrets/locks"`; the `chgrp docker` block goes.

- [ ] **Step 1: Write the failing tests**

`scripts/test_platform.py` (the fake `docker` answers `info` with `$FAKE_DOCKER_INFO`, default
`["name=seccomp","name=rootless"]`, and logs `DOCKER_SOCKET` with each call):

```python
def test_given_docker_host_when_up_then_compose_mounts_that_socket(self):
    # DOCKER_HOST=unix:///run/user/1001/docker.sock -> logged DOCKER_SOCKET=/run/user/1001/docker.sock
def test_given_a_rootful_engine_when_up_then_it_is_refused(self):
    # FAKE_DOCKER_INFO='["name=seccomp"]' -> returncode != 0, "runs as root", no "compose" call logged
def test_given_no_rootless_socket_when_up_then_it_names_ygg_install_and_never_falls_back(self):
    # no DOCKER_HOST, XDG_RUNTIME_DIR unset -> "run 'ygg install'", "/var/run/docker.sock" not in the log
def test_given_environments_without_the_apps_profile_when_up_then_it_says_to_add_apps(self):
def test_given_the_dev_profiles_without_environments_when_up_then_it_starts(self):
    # COMPOSE_PROFILES=forge,jenkins, no ENVIRONMENTS -> returncode 0, PROMETHEUS_CONFIG_FILE=prometheus-static.yml
```

Update the existing tests' env files to `COMPOSE_PROFILES=apps` and drop `DOCKER_GID`.
`scripts/test_socket_proxies.py`, static part:

```python
def test_given_each_socket_proxy_when_read_then_it_runs_as_container_root_without_group_add(self):
def test_given_each_socket_proxy_when_read_then_it_mounts_the_docker_socket_variable_read_only(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_platform scripts.test_socket_proxies -v`
Expected: the five new platform tests and two proxy tests FAIL.

- [ ] **Step 3: Implement the `compose.yml`, `prometheus-static.yml` and `platform.sh` changes above**;
  in `deploy.sh`, the unreadable-env-file message says "under Jenkins it must be readable by the user
  running the agent's engine".

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS, no shellcheck findings.

- [ ] **Step 5: Commit**

```bash
git add platform/compose.yml platform/prometheus/prometheus-static.yml scripts/platform.sh scripts/deploy.sh scripts/test_platform.py scripts/test_socket_proxies.py env/platform.env.example
git commit -m "feat: run the platform on rootless docker"
```

---

### Task 3: Forgejo as a platform service

**Files:**
- Modify: `platform/compose.yml` (service `forgejo`, volumes `forgejo-data`, `forgejo-config`)
- Modify: `env/platform.env.example` (a `---- Forgejo (profile forge)` section)
- Modify: `scripts/platform.sh` (profile variables)
- Create: `scripts/test_forgejo_service.py`
- Modify: `scripts/test_platform.py`

**Interfaces:**
- Consumes: profile mechanism and `check_profile_variables` from Task 2.
- Produces: service `forgejo` (profile `forge`) reachable as `forgejo:3000` on `edge`, routed at
  `git.${DOMAIN}`, SSH published on `${FORGEJO_SSH_PORT:-2222}`.

Service settings (environment `FORGEJO__<section>__<KEY>`): `server.ROOT_URL=https://git.${DOMAIN}/`,
`server.DOMAIN=git.${DOMAIN}`, `server.SSH_DOMAIN=git.${DOMAIN}`, `server.START_SSH_SERVER=true`,
`server.SSH_PORT=${FORGEJO_SSH_PORT:-2222}`, `server.SSH_LISTEN_PORT=2222`, `database.DB_TYPE=sqlite3`,
`service.DISABLE_REGISTRATION=true`, `service.REQUIRE_SIGNIN_VIEW=true`,
`security.INSTALL_LOCK=true`, `log.MODE=console`. Labels: router `forgejo`, rule ``Host(`git.${DOMAIN}`)``,
entrypoint `websecure`, middleware `secure-headers@file`, service port `3000`. `restart: unless-stopped`,
`logging: *logging`, `read_only` is not set (Forgejo writes its config).

- [ ] **Step 1: Write the failing tests** in `scripts/test_forgejo_service.py` (static, reading
  `compose.yml` with PyYAML, as `test_socket_proxies.py` does):

```python
def test_given_the_forgejo_service_when_read_then_it_is_only_in_the_forge_profile(self):
def test_given_the_forgejo_service_when_read_then_registration_is_closed_and_sign_in_is_required(self):
    # FORGEJO__service__DISABLE_REGISTRATION == "true" and FORGEJO__service__REQUIRE_SIGNIN_VIEW == "true"
def test_given_the_forgejo_service_when_read_then_ssh_is_published_on_2222_by_default(self):
def test_given_the_forgejo_service_when_read_then_its_image_is_the_rootless_variant_pinned_by_digest(self):
    # image matches r"forgejo/forgejo:\d+\.\d+\.\d+-rootless@sha256:[0-9a-f]{64}$"
def test_given_the_forgejo_service_when_read_then_it_never_mounts_the_docker_socket(self):
```

and in `scripts/test_platform.py`:
`test_given_the_forge_profile_when_up_then_no_jenkins_or_agent_variable_is_required`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_forgejo_service scripts.test_platform -v`
Expected: FAIL (no `forgejo` service).

- [ ] **Step 3: Add the service, the volumes and the env template section** (`FORGEJO_SSH_PORT`,
  documented as the host port for git over SSH, opened by the firewall on the dev machine). Pick the
  current stable Forgejo release; pin `image:` by version and digest.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.test_forgejo_service scripts.test_platform -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add platform/compose.yml env/platform.env.example scripts/platform.sh scripts/test_forgejo_service.py scripts/test_platform.py
git commit -m "feat: add forgejo as a platform service"
```

---

### Task 4: Build agents for the `ci` and `release` users

**Files:**
- Create: `platform/jenkins/build-agent/Dockerfile`
- Create: `platform/build-agent.yml`
- Create: `scripts/agent.sh`
- Create: `env/agent.env.example`
- Create: `scripts/test_agent.py`

**Interfaces:**
- Consumes: `docker_socket` and `require_rootless` logic — move both from `platform.sh` into
  `scripts/docker-engine.sh` (sourced by `platform.sh` and `agent.sh`; `# shellcheck source=docker-engine.sh`).
- Produces: `scripts/agent.sh up | down | ps | logs`, run as the agent's own user, reading
  `$YGG_SECRETS_DIR/agent.env` (default secrets dir `/etc/yggdrasil-$(id -un)`); Compose project name
  `yggdrasil-agent-<JENKINS_AGENT_NAME>`.
- Produces: `agent.env` variables `JENKINS_URL`, `JENKINS_AGENT_NAME`, `JENKINS_AGENT_SECRET`,
  `YGG_AGENT_WORKDIR` (absolute; default `$HOME/agent`), all required.

`build-agent.yml`: one service `agent`, `image: yggdrasil/build-agent:local`, `build: ./jenkins/build-agent`,
`restart: unless-stopped`, `init: true`, `user: "0:0"`, environment `JENKINS_URL`, `JENKINS_WEB_SOCKET=true`,
`JENKINS_AGENT_NAME`, `JENKINS_SECRET`, `JENKINS_AGENT_WORKDIR=${YGG_AGENT_WORKDIR}`, volumes
`${DOCKER_SOCKET}:/var/run/docker.sock` and `${YGG_AGENT_WORKDIR}:${YGG_AGENT_WORKDIR}` (the same path on
both sides, so a check's `docker run -v $WORKSPACE:…` means the same directory to the engine). Logging as the
platform's. The image: `jenkins/inbound-agent:latest-jdk21` plus git, curl, jq, python3, python3-yaml,
`docker-ce-cli` and `docker-buildx-plugin` from Docker's apt repository (as the deploy agent's Dockerfile
does), and nothing else.

- [ ] **Step 1: Write the failing tests** in `scripts/test_agent.py` (fake `docker` as in Task 2):

```python
def test_given_a_complete_agent_env_when_up_then_compose_runs_the_build_agent_project(self):
    # "compose" call with "-p yggdrasil-agent-ci" and "-f .../platform/build-agent.yml" and "up"
def test_given_a_missing_secret_when_up_then_it_is_refused_naming_the_variable(self):
def test_given_a_relative_workdir_when_up_then_it_is_refused(self):
def test_given_a_rootful_engine_when_up_then_it_is_refused(self):
def test_given_the_build_agent_file_when_read_then_the_workdir_is_mounted_at_the_same_path(self):
def test_given_the_build_agent_file_when_read_then_no_service_publishes_a_port(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_agent -v`
Expected: FAIL (no `agent.sh`).

- [ ] **Step 3: Implement `docker-engine.sh` (moved functions), `agent.sh`, the Compose file, the image and
  the env template**; `platform.sh` sources `docker-engine.sh` and Task 2's tests keep passing.

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add platform/jenkins/build-agent platform/build-agent.yml scripts/agent.sh scripts/docker-engine.sh scripts/platform.sh scripts/test_agent.py env/agent.env.example
git commit -m "feat: add build agents for ci and release users"
```

---

### Task 5: Machine roles: packages, rootful Docker off, system settings

**Files:**
- Create: `scripts/machine.sh` (functions only; sourced by `scripts/host.sh`)
- Modify: `scripts/host.sh` (source it; `check`/`install` take a role)
- Create: `scripts/test_machine.py` (fake `sudo`, `apt-get`, `dpkg-query`, `systemctl`, `sysctl`,
  `apparmor_parser`, `id`, `getent` on `PATH`; `YGG_SYSROOT` and `YGG_ASSUME_YES=1`)

**Interfaces:**
- Produces (in `machine.sh`): `machine_role [<role>]` → prints `dev|apps|workstation` (the argument, else
  the first word of `$YGG_SYSROOT/etc/yggdrasil-role`, else `workstation`); `role_users <role>` → the
  role's service users in order; `secrets_dir_of <user>`; `check_machine <role>` (prints report rows and
  fills the array `machine_todo` with step names); `install_machine <role>` (runs each step whose name is
  in `machine_todo`, asking with `confirm` first).
- Produces (in `host.sh`): `confirm` returns yes without asking when `YGG_ASSUME_YES=1`; `check [role]`
  and `install [role]` operations (`workstation` → today's `check_host`/`install_host`, unchanged).
- Steps this task adds: `packages`, `rootful-docker-off`, `system-settings`, `role-file`.
  - `packages`: `uidmap dbus-user-session slirp4netns docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin ufw rsync unattended-upgrades`
    plus today's tool packages, Docker's from its apt repository (reuse `install_docker`'s repository setup).
  - `rootful-docker-off`: `systemctl disable --now docker.service docker.socket containerd.service`, and
    every member removed from the `docker` group (`gpasswd -d`).
  - `system-settings`: writes these files, then `sysctl --system`, `systemctl restart apparmor.service`,
    `systemctl daemon-reload`:

`$YGG_SYSROOT/etc/sysctl.d/90-yggdrasil.conf`:
```
net.ipv4.ip_unprivileged_port_start=80
```
`$YGG_SYSROOT/etc/apparmor.d/usr.bin.rootlesskit`:
```
abi <abi/4.0>,
include <tunables/global>

"/usr/bin/rootlesskit" flags=(unconfined) {
  userns,

  include if exists <local/usr.bin.rootlesskit>
}
```
`$YGG_SYSROOT/etc/systemd/system/user@.service.d/delegate.conf`:
```
[Service]
Delegate=cpu cpuset io memory pids
```
  - `role-file`: `$YGG_SYSROOT/etc/yggdrasil-role` containing the role, mode 644.

Report rows (section "System"): `role`, `packages`, `rootful docker` (ok when `systemctl is-active` says
inactive for all three units and `/var/run/docker.sock` doesn't exist), `docker group` (ok when it has no
members), `unprivileged ports`, `apparmor rootlesskit`, `cgroup delegation` (each ok when its file has the
exact content above).

- [ ] **Step 1: Write the failing tests** in `scripts/test_machine.py`:

```python
def test_given_no_role_file_when_role_is_read_then_it_is_workstation(self):
def test_given_role_dev_when_installed_then_the_role_file_says_dev(self):
def test_given_a_running_rootful_engine_when_checked_then_it_is_listed_to_turn_off(self):
def test_given_role_apps_when_installed_then_the_three_docker_units_are_disabled_and_stopped(self):
    # fake systemctl log contains "disable --now docker.service docker.socket containerd.service"
def test_given_role_apps_when_installed_then_unprivileged_ports_start_at_80(self):
def test_given_role_apps_when_installed_then_rootlesskit_may_use_user_namespaces(self):
def test_given_role_apps_when_installed_twice_then_each_system_file_is_written_once_with_the_same_content(self):
def test_given_members_in_the_docker_group_when_installed_then_each_is_removed(self):
def test_given_role_workstation_when_checked_then_the_report_is_todays(self):
    # same rows as before this change: "docker group", "secrets directory", no "rootful docker" row
def test_given_debian_when_installing_role_dev_then_it_is_refused(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_machine -v`
Expected: FAIL (no `machine.sh`).

- [ ] **Step 3: Implement the four steps, their report rows, `machine_role`, `role_users`,
  `secrets_dir_of`, the `YGG_ASSUME_YES` branch in `confirm`, and the role argument of `check`/`install`
  in `host.sh`.** Writing a system file: `write_system_file <path> <mode> <content>` (via `as_root tee`,
  skipping the write when the content is already identical).

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/machine.sh scripts/host.sh scripts/test_machine.py
git commit -m "feat: add dev and apps machine roles"
```

---

### Task 6: Service users, SSH hardening, checkout and secrets ownership

**Files:**
- Modify: `scripts/machine.sh`
- Modify: `scripts/test_machine.py` (fakes for `useradd`, `usermod`, `chown`, `chmod`, `loginctl`,
  `sshd`; a fake `$YGG_SYSROOT/etc/passwd` the fake `id`/`getent` read)

**Interfaces:**
- Consumes: `role_users`, `secrets_dir_of`, `write_system_file` (Task 5).
- Produces steps: `users`, `ci-limits` (dev only), `ssh`, `checkout`, `secrets`, `unattended-upgrades`.
  - `users`: for each role user missing, `useradd --create-home --user-group --shell <shell> <u>` (shell
    `/bin/bash` for SSH users, `/usr/sbin/nologin` otherwise), then `chmod 750` its home. Never adds anyone
    to `sudo` or `docker`.
  - `ci-limits`: `$YGG_SYSROOT/etc/systemd/system/user-<uid of ci>.slice.d/limits.conf` with
    `[Slice]`, `CPUQuota=600%`, `MemoryMax=20G`; `systemctl daemon-reload`.
  - `ssh`: refuses (die, nothing written) unless `~<admin>/.ssh/authorized_keys` exists and is non-empty;
    asks whether to copy the admin's `authorized_keys` to each other SSH user that has none; writes
    `$YGG_SYSROOT/etc/ssh/sshd_config.d/90-yggdrasil.conf` with `PermitRootLogin no`,
    `PasswordAuthentication no`, `KbdInteractiveAuthentication no`,
    `AllowUsers <admin> <ssh users of the role>`; runs `sshd -t` and, only if it passes,
    `systemctl reload ssh`.
  - `checkout`: `chown -R ygg:ygg` and `chmod 755` on the checkout running the script (`$root`).
  - `secrets`: for each role user, `install -d -m 750 -o <u> -g <u> <secrets_dir_of u>`; copies
    `env/platform.env.example` → `platform.env` and `env/acme.env.example` → `acme.env` into `ygg`'s, and
    `env/agent.env.example` → `agent.env` into `ci`'s and `release`'s, each `600` and owned by that user,
    never overwriting.
  - `unattended-upgrades`: `$YGG_SYSROOT/etc/apt/apt.conf.d/20auto-upgrades` with
    `APT::Periodic::Update-Package-Lists "1";` and `APT::Periodic::Unattended-Upgrade "1";`.
- Report rows (section "Users"): one per role user (exists, shell, home mode, `no sudo` — ok when
  `sudo -l -U <u>` lists nothing and `id -nG <u>` has neither `sudo` nor `docker`), `ssh` (ok when the
  drop-in matches), `checkout owner`, `secrets` (one per directory: owner and mode).

- [ ] **Step 1: Write the failing tests**:

```python
def test_given_role_dev_when_installed_then_the_five_users_exist_without_sudo(self):
def test_given_role_apps_when_installed_then_only_ygg_is_created(self):
def test_given_role_dev_when_installed_then_ci_release_and_renovate_cannot_log_in(self):
    # their shell in the fake passwd is /usr/sbin/nologin
def test_given_role_dev_when_installed_then_ci_is_capped_at_six_cpus_and_20g(self):
def test_given_an_admin_without_authorized_keys_when_ssh_is_hardened_then_it_is_refused_and_nothing_written(self):
def test_given_an_invalid_sshd_config_when_hardened_then_ssh_is_not_reloaded(self):
    # fake sshd -t exits 1 -> no "reload ssh" in the systemctl log
def test_given_role_dev_when_hardened_then_only_the_admin_ygg_and_dev_may_log_in(self):
def test_given_existing_users_when_installed_twice_then_useradd_is_not_called_again(self):
def test_given_a_secrets_file_already_there_when_installed_then_it_is_kept(self):
def test_given_role_dev_when_checked_then_a_service_user_in_the_sudo_group_is_reported(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_machine -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the six steps and their report rows in `machine.sh`.**

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/machine.sh scripts/test_machine.py
git commit -m "feat: create the service users of each role"
```

---

### Task 7: Firewall per role, with Cloudflare's ranges on the apps machine

**Files:**
- Modify: `scripts/machine.sh`
- Modify: `scripts/test_machine.py` (fake `ufw` keeping its rules in a file; fake `curl` answering from
  `$FAKE_CURL_DIR/<url basename>` or failing when the file is missing)

**Interfaces:**
- Consumes: `secrets_dir_of ygg` (Task 5), `write_system_file`.
- Produces step `firewall` and `cloudflare_ranges` → prints the current ranges (IPv4 list, then IPv6), or
  fails (non-zero) when either download fails or returns a line that isn't a CIDR.
- Produces the file `<secrets of ygg>/cloudflare-ips` (apps; mode 644, owner `ygg`), Task 1's input.
- Rules — dev: `ufw allow OpenSSH`, `ufw allow 80,443/tcp`, `ufw allow 2222/tcp`. apps: `ufw allow OpenSSH`,
  then for each Cloudflare range `ufw allow proto tcp from <cidr> to any port 80,443 comment yggdrasil-cloudflare`.
  On a re-run with a different list, the rules whose comment is `yggdrasil-cloudflare` and whose range is no
  longer listed are deleted. SSH is always allowed before `ufw --force enable`.
- When `cloudflare_ranges` fails, the apps firewall step changes nothing and warns
  `"could not fetch Cloudflare's ranges: the firewall and cloudflare-ips are unchanged"`.
- Report rows (section "Firewall"): `ufw` (enabled), `ports` (the role's rules present; on apps, no rule
  allowing 80 or 443 from anywhere), `cloudflare ranges` (apps: ok when the rules and `cloudflare-ips`
  equal the current download; warn with the difference otherwise; warn "could not check" when offline).

- [ ] **Step 1: Write the failing tests**:

```python
def test_given_role_dev_when_the_firewall_is_set_then_ssh_web_and_2222_are_open(self):
def test_given_role_apps_when_the_firewall_is_set_then_web_is_open_only_to_cloudflare(self):
def test_given_role_apps_when_the_firewall_is_set_then_cloudflare_ips_is_written_for_traefik(self):
def test_given_a_changed_cloudflare_list_when_set_again_then_old_ranges_are_removed(self):
def test_given_cloudflare_unreachable_when_the_firewall_is_set_then_nothing_changes(self):
def test_given_a_range_that_is_not_a_cidr_when_fetched_then_nothing_changes(self):
def test_given_drift_between_rules_and_cloudflare_when_checked_then_it_is_reported(self):
def test_given_an_open_80_rule_on_apps_when_checked_then_it_is_reported(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_machine -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement `cloudflare_ranges`, the `firewall` step and the rows.** Today's
  `setup_firewall` stays for `workstation`.

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/machine.sh scripts/test_machine.py
git commit -m "feat: open the apps machine to cloudflare only"
```

---

### Task 8: One rootless engine per service user

**Files:**
- Modify: `scripts/machine.sh`
- Modify: `scripts/test_machine.py` (fake `dockerd-rootless-setuptool.sh`, `loginctl`, and `sudo -u`
  handling: the fake `sudo` logs the target user and the environment it passes)

**Interfaces:**
- Consumes: `role_users` (Task 5), users from Task 6.
- Produces `run_as_user <user> <command…>`: `as_root -u <user> env XDG_RUNTIME_DIR=/run/user/<uid>
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus <command…>`, run from `/` (so a `nologin` user
  can still run it).
- Produces step `engines`: for each role user, `loginctl enable-linger <u>`; writes
  `~<u>/.config/systemd/user/docker.service.d/override.conf` (owned by `<u>`):

```
[Service]
Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns"
Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns"
```

  then `run_as_user <u> dockerd-rootless-setuptool.sh install`,
  `run_as_user <u> systemctl --user daemon-reload`, `run_as_user <u> systemctl --user enable --now docker`,
  and appends `export DOCKER_HOST=unix:///run/user/<uid>/docker.sock` to `~<u>/.profile` once (between
  `# >>> yggdrasil >>>` and `# <<< yggdrasil <<<` markers, replaced on re-runs).
- Report rows (section "Rootless Docker"), per role user: `engine` (ok when
  `run_as_user <u> docker info --format '{{json .SecurityOptions}}'` contains `name=rootless`),
  `linger`, `client addresses` (ok when the override has the two lines above), and on dev the isolation
  rows: `ci → ygg engine` and `release → ygg engine` (ok when `run_as_user <u> test -r /run/user/<ygg uid>/docker.sock`
  fails), `ci → ygg secrets` (ok when `run_as_user ci test -r /etc/yggdrasil/platform.env` fails).

- [ ] **Step 1: Write the failing tests**:

```python
def test_given_role_dev_when_engines_are_installed_then_each_of_the_five_users_gets_one(self):
def test_given_a_nologin_user_when_its_engine_is_installed_then_the_commands_run_as_that_user(self):
def test_given_an_engine_when_installed_then_it_keeps_client_addresses(self):
def test_given_an_engine_installed_twice_then_the_profile_has_one_docker_host_line(self):
def test_given_ci_able_to_read_the_ygg_socket_when_checked_then_isolation_is_reported_broken(self):
def test_given_a_rootful_engine_answering_for_a_user_when_checked_then_its_engine_row_is_not_ok(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_machine -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement `run_as_user`, the `engines` step and the rows.**

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/machine.sh scripts/test_machine.py
git commit -m "feat: give each service user a rootless engine"
```

---

### Task 9: Backups between the two machines

**Files:**
- Create: `scripts/backup.sh`
- Create: `env/backup.conf.example`
- Create: `platform/systemd/yggdrasil-backup.service`, `platform/systemd/yggdrasil-backup.timer`
- Modify: `scripts/machine.sh` (step `backup-timer` for `ygg`)
- Create: `scripts/test_backup.py`

**Interfaces:**
- Consumes: `run_as_user` (Task 8).
- Produces `scripts/backup.sh run | list`, run as `ygg`, reading `$YGG_SECRETS_DIR/backup.conf`:
  - `BACKUP_TARGET=<user>@<host>:<path>` (required for `run`), `BACKUP_KEY` (default
    `~/.ssh/yggdrasil-backup`), `BACKUP_KEEP` (days kept locally, default `7`), then one item per line:
    `forgejo` (`docker exec` into the `yggdrasil` project's `forgejo` container: `forgejo dump` to a file
    copied out), `volume <name>` (a throwaway `busybox` container with the volume mounted read-only, `tar`
    of it), `postgres <container> <user>` (`docker exec <container> pg_dumpall -U <user>`).
  - Writes into `~/backups/<YYYY-MM-DD>/` (one file per item, `.zip`/`.tar.gz`/`.sql.gz`), deletes local
    days beyond `BACKUP_KEEP`, then `rsync -a --delete -e "ssh -i <key>" ~/backups/ <target>`. One failing
    item doesn't stop the others; the exit status is non-zero if any failed, naming them.
- Produces the units (user units of `ygg`): the service runs `/opt/yggdrasil/scripts/backup.sh run`; the
  timer `OnCalendar=*-*-* 03:30:00`, `Persistent=true`, `RandomizedDelaySec=15m`. Step `backup-timer`
  copies both into `~ygg/.config/systemd/user/` and enables the timer with `run_as_user ygg`.
- The receiving side is set up by hand (Task 10 documents it): an `authorized_keys` line restricted with
  `restrict,command="rrsync ~/backups/from-<machine>"`.

- [ ] **Step 1: Write the failing tests** in `scripts/test_backup.py` (fake `docker`, `rsync`, `ssh`):

```python
def test_given_the_three_kinds_when_run_then_one_file_each_in_todays_directory(self):
def test_given_a_failing_item_when_run_then_the_others_still_run_and_the_exit_names_it(self):
def test_given_no_target_when_run_then_it_is_refused_before_dumping(self):
def test_given_more_days_than_kept_when_run_then_the_oldest_local_days_are_removed(self):
def test_given_a_run_when_done_then_rsync_sends_with_the_backup_key(self):
def test_given_an_unknown_item_kind_when_run_then_it_is_refused_naming_the_line(self):
```

and in `scripts/test_machine.py`:
`test_given_role_dev_when_installed_then_ygg_has_the_backup_timer_enabled`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_backup scripts.test_machine -v`
Expected: FAIL.

- [ ] **Step 3: Implement `backup.sh`, the template, the units and the `backup-timer` step.**

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backup.sh env/backup.conf.example platform/systemd scripts/machine.sh scripts/test_backup.py scripts/test_machine.py
git commit -m "feat: back up each machine to the other"
```

---

### Task 10: The `ygg` command line

**Files:**
- Modify: `scripts/yggcli/commands.py`, `scripts/yggcli/tree.py` (and `complete.py` only if pick lists need
  it)
- Modify: `scripts/host.sh` (operations `agent`, `backup`)
- Modify: `scripts/test_ygg_cli.py`, `scripts/test_ygg_tree.py`

**Interfaces:**
- Consumes: `host.sh check|install [role]` (Task 5), `agent.sh` (Task 4), `backup.sh` (Task 9),
  `run_as_user` (Task 8).
- Produces the commands:
  - `ygg check [--role dev|apps|workstation]` and `ygg install [--role dev|apps|workstation]`; without
    `--role`, the role file's role (`machine_role`).
  - `ygg agent {up,down,ps,logs} --user {ci,release}`: run by the admin; `run_as_user <user>
    /opt/yggdrasil/scripts/agent.sh <command>`.
  - `ygg backup {run,list}`: run as `ygg`; `scripts/backup.sh <command>`.
- `--role` and `--user` are argparse `choices`, so the menu and completion get their pick lists from the
  existing tree (the parity test stays green).

- [ ] **Step 1: Write the failing tests**:

```python
def test_given_install_with_a_role_when_parsed_then_host_sh_gets_install_and_the_role(self):
def test_given_check_without_a_role_when_parsed_then_host_sh_gets_check_alone(self):
def test_given_an_unknown_role_when_parsed_then_it_is_refused(self):
def test_given_agent_up_for_ci_when_parsed_then_host_sh_gets_agent_up_ci(self):
def test_given_backup_run_when_parsed_then_host_sh_gets_backup_run(self):
```

plus the tree's parity and completion tests extended with the new commands (they fail until each new
argument has its pick list).

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest scripts.test_ygg_cli scripts.test_ygg_tree -v`
Expected: FAIL.

- [ ] **Step 3: Implement the commands and the two `host.sh` operations.**

- [ ] **Step 4: Run the suite and shellcheck**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/yggcli scripts/host.sh scripts/test_ygg_cli.py scripts/test_ygg_tree.py
git commit -m "feat: add roles, agents and backups to ygg"
```

---

### Task 11: Documentation and CI

**Files:**
- Create: `docs/hosts.md`
- Modify: `docs/setup.md` (sections 0, 1, 6, 8, 9, 10, Troubleshooting), `docs/dns.md`, `docs/cli.md`,
  `README.md` (the "Inside a host" diagram and the concepts table's **Host** row), `CHANGELOG.md`
  (`## [Unreleased]`)
- Modify: `.github/workflows/ci.yml` (job `platform`, step "Validate the Compose files")

**Interfaces:**
- Consumes: every name above (roles, users, files, variables, commands).

`docs/hosts.md`, in this order:
1. **Two machines** — the diagram and the "two machines" table from the spec, with what runs where, the
   Compose profiles each runs (`dev`: `forge,jenkins` under `ygg`, agents under `ci`/`release`; `apps`:
   `apps,agent` under `ygg`), names, ports.
2. **Domains and Cloudflare** — two zones, which records are proxied and why (git over SSH, push size), SSL
   mode Full (strict), the origin lock, client addresses (`CLOUDFLARE_PROXIED`, `cloudflare-ips`), caching
   (a rule so APIs and `/api/` are never cached).
3. **Users** — the spec's tables for both machines, what each user may do and why, how the owner logs in
   for daily work (`ssh ygg@…`) and for system work (the admin).
4. **System settings** — each file `ygg install` writes, its exact content and why.
5. **Rootless Docker** — one engine per user, sockets, `DOCKER_HOST`, `slirp4netns` and client addresses,
   container root = the service user, why the firewall now applies to published ports.
6. **Credentials** — the spec's credentials table, marking which ones later plans create.
7. **Backups and restore** — `backup.conf`, the timer, the receiving `authorized_keys` line, restoring
   Forgejo from a dump and a volume from its tar, step by step.
8. **Adding a machine** — another apps host: role `apps`, its own `ygg` user, its agent.
9. **Upgrading a host from rootful Docker** — stop the platform and applications, back up volumes, run
   `ygg install --role apps`, restore volumes into `ygg`'s engine, add `apps` to `COMPOSE_PROFILES`, start.

`docs/setup.md`: section 0 plans two machine roles (and keeps one-host setups: a single machine with role
`apps` running the `jenkins` profile too is still supported — say so); section 1 unchanged; section 6
covers two domains on Cloudflare; section 8 becomes "Prepare each machine: `ygg install --role …`", each
step it runs explained as today's section 8 explains its commands, with the by-hand equivalent;
sections 9 and 10 become "the dev machine" and "the apps machine" (the GitHub App and rules steps stay as
they are for now: plans 2 and 4 rewrite them); Troubleshooting adds "permission denied on
/run/user/<uid>/docker.sock", "Traefik sees Cloudflare's addresses", "engine not running after reboot
(linger)". `docs/dns.md`: Cloudflare proxied, two zones, the token's scope (DNS edit on both zones).
`docs/cli.md`: `check`/`install --role`, `agent`, `backup`. CHANGELOG: everything an operator notices,
including the breaking changes (rootless only for the platform, the `apps` profile, `DOCKER_GID` gone,
`EXTRA_DOMAINS`/`CLOUDFLARE_PROXIED`, Forgejo, build agents).

CI: the env file it writes drops `DOCKER_GID`, adds `COMPOSE_PROFILES=apps`; the step exports
`DOCKER_SOCKET=/run/user/1000/docker.sock` and renders a Traefik config with
`python3 scripts/traefik_config.py > "$secrets/traefik.yml"` and `YGG_TRAEFIK_CONFIG_FILE`; it validates
`platform/compose.yml` once with `COMPOSE_PROFILES=apps,agent` and once with `forge,jenkins`, and
`platform/build-agent.yml` with a dummy `agent.env`.

- [ ] **Step 1: Write `docs/hosts.md` and the other documentation changes.**

- [ ] **Step 2: Check every command and path the docs name exists** — each `ygg …` command against
  `python3 scripts/ygg.py help`, each file path against the tree.

Run: `python3 scripts/ygg.py help check && python3 scripts/ygg.py help install && python3 scripts/ygg.py help agent && python3 scripts/ygg.py help backup`
Expected: each prints its help, with `--role` / `--user` and their choices.

- [ ] **Step 3: Update `ci.yml` and run the Compose validation locally** (with Docker available):

Run: the "Validate the Compose files" step's script, copied into a shell with the repository as working directory.
Expected: `docker compose … config --quiet` exits 0 for both profile sets and for `build-agent.yml`.

- [ ] **Step 4: Run the whole suite one last time**

Run: `python3 -m unittest discover -s scripts -v && shellcheck scripts/*.sh scripts/ygg`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs README.md CHANGELOG.md .github/workflows/ci.yml
git commit -m "docs: document the two machines and their users"
```
