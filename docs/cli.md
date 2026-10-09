# `ygg`: the host's command line app

`ygg` runs on an Ubuntu host (a VPS, a VM, WSL or a laptop) and does what [setup.md](setup.md)
otherwise has you do by hand:

- install the tools, or check they are there
- set up a new API, web front end or worker, and deploy it
- see what runs on the host, in each of its environments
- show, change and roll back an application's variables and secrets
- switch an on-demand environment on and off
- start, stop and inspect the platform; read the catalog

It has two faces that do the same things. **Run it with arguments** and it is an ordinary command,
for scripts and for going straight somewhere: `ygg vars get heimdall-api@production
HEIMDALL_MASTER_USER_PASSWORD --reveal`. **Run it alone**, in a terminal, and it opens a full-screen
menu you drive with the arrow keys, where every command and every option is too, as a form that
shows the command line it builds.

A host runs one environment or several: `ENVIRONMENTS` in its `platform.env` lists them
(`development,homologation,production` on the VPS of the
[example](examples/docker-desktop-and-vps/README.md)). Where a command needs one environment and
the host has several, it takes it as an argument or asks.

It runs the same pieces as the guide: `scripts/host.sh` (the host operations), `scripts/deploy.sh`,
`scripts/platform.sh`, `scripts/vars.py`, `scripts/catalog.py`, the files in `stacks/` and the
variables store, or the env files, in `/etc/yggdrasil`. Whatever it does, you can check, change or
undo by hand.

## Install

From the yggdrasil checkout (`/opt/yggdrasil` on a host):

```bash
scripts/ygg.sh self-install      # links /usr/local/bin/ygg to this checkout, installs tab completion
ygg version                      # yggdrasil 0.7.0 (1a2b3c4) at /opt/yggdrasil
```

`ygg install` offers it too. `scripts/ygg.sh` keeps working everywhere `ygg` does, for scripts and
docs written before 0.7; on a host without python3 it still runs `check` and `install`.

## The menu

```bash
ygg
```

```text
 yggdrasil › Applications › heimdall-api › production           vps-1 · 0.7.0
 ─────────────────────────────────────────────────────────────────────────────
   ▏type to filter
   HEIMDALL_CORS_ALLOWED_ORIGINS   https://heimdall.example.com       heimdall-api@production
 ▸ HEIMDALL_MASTER_USER_PASSWORD   ••••••                             heimdall-api@production  secret
   POSTGRES_HOST                   postgres                           @production
   + Add a variable
 ─────────────────────────────────────────────────────────────────────────────
  ygg vars list heimdall-api@production --resolved
  Enter actions · r reveal/hide · e edit · h history · d deploy · / filter · ? help · Esc back
```

The header shows where you are and the host; the footer, the command line of what you see and the
keys of the screen.

| Key | Does |
|---|---|
| ↑ ↓, PgUp PgDn | Move |
| Enter, → | Choose, open |
| Esc, ← | Back (first clears the filter) |
| Typing | Filters the list. Where letters are shortcuts (the variables screen: `r`, `e`, `h`, `d`; and `q`, `?`), `/` starts a filter |
| Backspace | Edits the filter; in a form, with the filter empty, clears the field under the cursor |
| `?` | The help of the screen or command (its full `--help`) |
| `q` | Quits (outside a text field) |
| Ctrl-C | Stops the command that is running, not the menu. At the pause after a command it returns to the menu; in the numbered menu it goes back, as Esc does |

The top menu groups the commands by task:

| Entry | What is there |
|---|---|
| **Status** | `ygg status` |
| **Applications** | This host's applications (each opens its variables, below), `deploy`, `add`, `config` |
| **Environments** | `env status`, `env start`, `env stop` |
| **Variables and secrets** | Browse any scope (same screen as an application's), and every `vars` command |
| **Platform** | `platform up`, `down`, `ps`, `logs`, `config` |
| **Host** | `check`, `install`, `self-install` |
| **Catalog** | Every `catalog` command |
| **Help and version** | `help`, `version`, `completion` |

**Forms.** A command with arguments or options opens as a form: one field per argument, a checkbox
per flag, a choice per option with fixed values or per group of options that exclude each other,
then **▶ Run**. An argument is picked from a list: applications and environments of this host,
scopes, the keys of the scope chosen above it (secrets marked), history entries, platform services.
Typing filters the list; text that matches nothing is taken as typed where that makes sense (a new
variable, a scope, a change id) after the same check as the command line. Paths and versions are
typed, with the usual default filled in. The footer shows the command line as you fill the form, so
the menu also teaches the command. Deleting, overwriting and stopping commands ask first, with no as
the default: `vars unset`, `vars rollback`, `vars import --replace`, `env stop`, `platform down`, and
`deploy` to an environment that is not on demand.

A command runs in the normal terminal, outside the full screen, so `$EDITOR`, hidden input and long
output work. When it ends, the menu always waits for **Enter**, so you can read the output, then
reloads what it shows.

**No terminal.** In a pipe, a script, a terminal smaller than 60×12, or with `YGG_PLAIN` set to any
value, the menu is numbered lines: type a number, or a row's exact text; an empty line or `0` goes
back; `q` quits. The prompts of `add`, `config` (without a store) and `status` use the same
arrow-key picker in a terminal and plain questions otherwise. In them, Esc on a choice with a
**Back** option goes back; on any other choice it stops the operation; on a yes/no question it
answers no.

## Variables and secrets of an application

**Applications › `<application>` › `<environment>`** lists what the application gets in that
environment: each variable, its value (secrets masked) and the scope it comes from
(`heimdall-api@production`, `heimdall-api`, `@production`; a reference shows where it points).
Select a variable for its actions:

| Action | Does | Command line |
|---|---|---|
| **Show value** | Shows the value on the screen until you press a key. It is drawn, not printed, so it stays out of the terminal's scrollback. The numbered menu (no terminal, or `YGG_PLAIN`) can only print it, and the row says so | `ygg vars get <scope> KEY --reveal` |
| **Change value** | A secret is typed hidden; another value starts from the current one. Then where: this application in this environment (the default), the application in every environment, or the environment's shared scope. For a value it inherits, the first choice changes the shared value and another overrides it for this application only | `ygg vars set <scope> KEY=-` (the value on stdin) |
| **Mark as secret / not secret** | Masks the value or stops masking it | `ygg vars set <scope> KEY=- --secret` |
| **Remove** | Deletes it from the scope it comes from, after you confirm | `ygg vars unset <scope> KEY` |
| **History** | Its changes; select one to roll it back (`--force` when it changed again since) | `ygg vars history`, `ygg vars rollback <id>` |
| **Copy command** | The command lines for this variable, to paste in a script | |

On the list, `r` reveals or hides every secret (the numbered menu asks first, as it prints them),
`e` edits the scope in `$EDITOR` (`ygg vars edit`), `h` shows the history, `+ Add a variable` asks
the name, secret or not, the value and where, and `d` (also **Deploy** once something changed) redeploys the application so the
changes reach it. An application with no variables yet offers to create them from its stack files,
as `add` does. **Variables and secrets › Browse a scope** opens the same screen for any scope
(`platform`, `@production`, ...).

When the store can't be read (a lost or wrong key), the screen says why and offers
`vars check --usable`; `platform.sh up --last-good` still starts the platform
([recovery](variables.md#backup-and-recovery)). The commands, the scopes and the recipes behind the
screen are in [variables.md](variables.md#commands) and [its recipes](variables.md#recipes).

From the command line, `ygg config <application> <environment>` opens that screen directly.

### On a machine without a store

Without a variables store (a machine still on env files), **Applications** opens `config`'s own
screen on the env file, `/etc/yggdrasil/<environment>/<id>.env`, not the list above. Without
arguments `config` asks for the application, then, when it deploys to more than one of the host's
environments, for the environment. An environment that isn't on this host, or that the application
doesn't deploy to, is refused. If the file doesn't exist yet, as on a new host, it creates it from
the stack files as `add` does. Then it offers:

- **Set** or **remove** a variable. Values with spaces, `#`, `$`, `"` or `\` are written in single
  quotes, which Compose reads literally; a value with a single quote is refused (use **Edit**).
  Values of variables whose name ends in `PASSWORD`, `PASSWD`, `PASS`, `PWD`, `SECRET`, `TOKEN`,
  `KEY` or `CREDENTIAL(S)`, optionally followed by `_PREVIOUS`, or in `CONNECTIONSTRING`
  (`DB_PASSWORD`, `API_KEY`, `HEIMDALL_AUTH_TOKEN_SECRET_PREVIOUS`, `FORTUNA_DATA_CONNECTIONSTRING`;
  not `HEIMDALL_PASSWORD_RESET_URL`) are typed hidden and shown as `********`, unless you ask to see them.
- **Edit** the file in `$EDITOR` (default `nano`).
- **Apply**: redeploys with `deploy.sh`. Containers read their env file only when they are created,
  and a web front end's build arguments are compiled into its image, so the image is rebuilt and
  the containers recreated. It builds from `~/yggdrasil-apps/<id>`, or asks for a checkout. By
  default it keeps the running version's label when the checkout is at that commit. If the new
  version doesn't become healthy, `deploy.sh` rolls back as usual. In an on-demand environment
  where the application is stopped, it asks whether to leave it running afterwards.

A file keeps its owner, group and mode (`640`, group `docker`), so the Jenkins agent can still
read it. Changes you don't apply reach the application on its next deploy, from Jenkins or by hand.

## Commands

`ygg help` lists them by task; `ygg help <command>` or `ygg <command> --help` explains one.

| Command | Menu | Does |
|---|---|---|
| `ygg` | | The menu |
| `ygg status` | Status | Each environment of this host, its applications' state, health, version, commit and deploy time |
| `ygg config [<app>] [<environment>]` | Applications | An application's variables in an environment, and a redeploy |
| `ygg deploy <environment> <app> [<app-dir>] [<version>] [--leave-running]` | Applications › deploy | Builds and deploys from a checkout (default `$YGG_APPS_DIR/<app>`, version `<latest tag>-<commit>`), rolling back if unhealthy. In a terminal it asks what is missing and, for a stopped on-demand application, whether to leave it running; without a terminal it does not ask and stops it again. `--leave-running` keeps it running |
| `ygg add` | Applications › add | Sets up a new application |
| `ygg env status \| start <environment> \| stop <environment> [--force]` | Environments | This host's environments |
| `ygg vars <command>` | Variables and secrets | The variables store: `init`, `set`, `get`, `list`, `unset`, `edit`, `history`, `rollback`, `import`, `export`, `render`, `render-platform`, `backup`, `check` ([variables.md](variables.md#commands)) |
| `ygg platform up \| down \| ps \| config \| logs [<service>] [--last-good]` | Platform | This host's platform stack (`scripts/platform.sh`) |
| `ygg check` | Host › check | What is installed and what is missing |
| `ygg install` | Host › install | Installs what is missing, asking before each part |
| `ygg self-install` | Host › self-install | The `ygg` command and its tab completion |
| `ygg catalog validate \| environments \| applications \| systems \| show \| plan \| environment \| get \| owner \| repository` | Catalog | Reads `catalog.yaml` (`scripts/catalog.py`) |
| `ygg help [<command>]`, `ygg version`, `ygg completion bash` | Help and version | |

Exit status: 0 success, 1 a command failed, 2 a usage error (an unknown command, a missing argument).
`ygg env` and `ygg vars` pass their words to `host.sh` and `vars.py`, whose own exit codes apply
(for `vars`, see [variables.md](variables.md#exit-status-and-environment)).

| Environment variable | Default | What |
|---|---|---|
| `YGG_SECRETS_DIR` | `/etc/yggdrasil` | Where the variables store (`vars.db`, `vars.key`) or the env files are, as for `deploy.sh` and `platform.sh` |
| `YGG_APPS_DIR` | `~/yggdrasil-apps` | Where `add` clones the applications it deploys, and where redeploys build from |
| `YGG_ENVIRONMENT` | `ENVIRONMENTS` in `platform.env` | One environment this host runs, overriding `platform.env`. Without either (nor the `ENVIRONMENT` of a `platform.env` from before 0.5), it asks. Set it on a laptop, which has no platform |
| `YGG_PLAIN` | unset | Any value: numbered prompts instead of the full screen |
| `EDITOR` | `nano` | The editor of `vars edit` and of `config` |

## Tab completion

`ygg self-install` installs it for new shells; `source <(ygg completion bash)` turns it on in the
current one. It completes commands, options, choices, applications, environments, scopes and the
keys of the scope typed before (`ygg vars get heimdall-api@production HEI<Tab>`), from the same lists
as the menu. Keys need read access to the store's key; without it they are not offered.

## Check and install

`check` looks at:

| Group | Checks |
|---|---|
| **Tools** | Ubuntu; `git`, `curl`, `openssl`, `python3`, `htpasswd` (apache2-utils), PyYAML and `cryptography` (for the variables store); Docker Engine; Compose 2.24 or later; Buildx; whether your user can reach the Docker daemon |
| **This host** | The secrets directory and its group; `platform.env` and `acme.env`, or the variables store passing `vars check`; the catalog is valid; the host's environments (`ENVIRONMENTS`, or a warning for the `ENVIRONMENT` of a `platform.env` from before 0.5); the platform is running; ufw |

`install` works on Ubuntu only. It needs root or `sudo`, and asks before each part:

1. **Packages** from Ubuntu's repositories: `git curl openssl python3 python3-yaml python3-cryptography apache2-utils ca-certificates`.
2. **Docker** from Docker's own apt repository, as in
   [docs.docker.com](https://docs.docker.com/engine/install/ubuntu/): `docker-ce`, the Compose and
   Buildx plugins. Ubuntu's own `docker.io` and similar packages are removed first, if you agree:
   their Compose is too old for the stack overlays. Images, containers and volumes stay.
3. **Your user in the `docker` group.** Log out and back in afterwards.
4. **The secrets directory**, `/etc/yggdrasil`, group `docker`, mode `2750`, with `platform.env` and
   `acme.env` copied from `env/` ([setup.md step 8.4](setup.md#8-prepare-every-host)). Fill them
   in as in [step 9.2](setup.md#92-the-env-files-first-pass) or [step 10](setup.md#10-bring-up-the-other-environment-hosts).
5. **A firewall** (ufw) letting in only SSH, 80 and 443. It's for servers, so the default is no.

Then bring the platform up with `scripts/platform.sh up`, as in the guide.

## Set up an application

`add` asks about the application, then does what [Adding things](setup.md#an-application) lists:

| It asks | Default | Becomes |
|---|---|---|
| Id | | The catalog `id`, the image name, and with the environment the Compose project (`<id>-<environment>`) and the network alias (`<id>.<environment>`) |
| Display name | the id | `name` |
| Kind | | `kind`: `api`, `web` or `worker` |
| System | | An existing system, or a new one with its own id, name and description |
| Repository name | the id | `repository`, when it differs from the id |
| Dockerfile only, or its own `docker-compose.yml` | | Which stack files it writes (below) |
| Service to route to | `api` / `ui` | With its own Compose file: the service Traefik routes to |
| Container port | `8080` | The port in `health` and in the Traefik service |
| Health path | `/health`, `/healthz` for web | `health: http://<id>:<port><path>` |
| How to check health inside the container | | A `healthcheck:` using `wget` or `curl`, or none if the Dockerfile has a `HEALTHCHECK` |
| Public host | the id; none for a worker | `host`, and the `PUBLIC_HOST` router (`<host><hostSuffix>.<DOMAIN>` in each environment) |
| Metrics port | none | `metrics: <id>:<port>`, and the telemetry network |
| Build arguments | none | For a web front end without its own Compose file: build `args`, filled from the env file |
| GitHub checks | none | `checks` |

It then writes:

- **`catalog.yaml`**: the application, at the end of its system, or a new system placed before
  `yggdrasil`. The file's comments stay, and nothing is written if the result wouldn't be valid.
- **Stack files**, following [setup.md step 4](setup.md#4-write-the-stack-files). Existing files
  are only replaced if you agree:

  | The repository has | Files |
  |---|---|
  | Only a Dockerfile | `stacks/<id>.yml`: the service, built from the checkout, with the whole env file passed to the container (`env_file`) · `stacks/<id>.proxy.yml`: the edge network and Traefik · `stacks/<id>.ports.yml`: a port on `127.0.0.1`, `HOST_PORT` in the env file |
  | Its own `docker-compose.yml` | `stacks/<id>.proxy.yml`: `ports: !reset []`, the default network kept (for its database, say), edge, and Traefik. In `ports` environments its own Compose file is used as it is |

  The proxy overlay names the alias `<id>.${YGG_ENVIRONMENT}` and the Traefik router and service
  `<id>-${YGG_ENVIRONMENT}`, so several environments of one host don't collide; `deploy.sh` sets
  `YGG_ENVIRONMENT`.

- **The env file** for one of this host's environments (it asks which, when the host has several),
  `/etc/yggdrasil/<environment>/<id>.env`. It holds every variable the stack files read, except the
  ones `deploy.sh` sets itself, plus `PUBLIC_HOST` (`<host><hostSuffix>.<DOMAIN>`, e.g.
  `shop-dev.example.com` in an environment with `hostSuffix: -dev`) in a `proxy` environment. It
  offers to open it in your editor. For the host's other environments, run
  `ygg config <id> <environment>` afterwards.
- **A first deploy**, if you agree: it clones the repository into `~/yggdrasil-apps/<id>` (enter
  the SSH URL for a private repository) and runs `deploy.sh`. The version defaults to
  `<latest tag>-<commit>`, the way Jenkins labels releases. In a `proxy` environment the platform
  must be up first. In an `onDemand` environment where it isn't running, it asks whether to leave it
  running afterwards; otherwise `deploy.sh` stops it again once it is healthy.

Last, it lists what the catalog can't do for you: push the catalog and stack files to `main`, set up
the application's repository, install the GitHub App on it, apply the rules, and restart the status
API (and Jenkins) on every host.

The generated files are a starting point, like the examples in `stacks/`: edit them freely.

## See what runs

`status` lists, for each environment of this host, every catalog application that deploys to it:

```text
== Development (development), on demand on vps-1 ==
  APPLICATION            STATE           HEALTH     VERSION          COMMIT   DEPLOYED
  heimdall-api           stopped         -          1.5.0            9b0c4d2  2026-10-07T14:12:40Z
  heimdall-ui            stopped         -          1.5.0            e41a7f0  2026-10-07T14:15:02Z

== Homologation (homologation), on demand on vps-1 ==
  APPLICATION            STATE           HEALTH     VERSION          COMMIT   DEPLOYED
  heimdall-api           not deployed
  heimdall-ui            not deployed

== Production (production) on vps-1 ==
  APPLICATION            STATE           HEALTH     VERSION          COMMIT   DEPLOYED
  heimdall-api           running 1/1     healthy    1.4.0            3f2a9c1  2026-09-28T19:55:01Z
  heimdall-ui            running 1/1     healthy    1.4.0            7348de1  2026-09-28T23:58:36Z

  Platform: 10 containers running (scripts/platform.sh ps for details).
```

The version, commit and deploy time are the labels `deploy.sh` puts on every container, the same
ones the status API reads. A stopped application is shown in red, except in an on-demand
environment, where stopped is normal. From there you can show an application's last 100 log
lines or restart its containers (it asks for the application and environment, e.g.
`heimdall-api in production`), start or stop an environment, or list the platform's services.

## Start and stop environments

An environment with `onDemand: true` in the catalog runs only while someone uses it. `env` switches
it:

```bash
ygg env start development     # docker start on every container of each <app>-development project
ygg env stop development      # docker stop on the running ones
ygg env status
```

```text
== Environments on vps-1 ==
  ENVIRONMENT      NAME                 ON DEMAND  STATE
  development      Development          yes        running (2 of 4 applications)
  homologation     Homologation         yes        stopped
  production       Production           no         running (4 of 4 applications)
```

- `start` and `stop` act only on an environment of this host (`ENVIRONMENTS` in `platform.env`, or
  `YGG_ENVIRONMENT`); any other is refused. They start and stop the containers `deploy.sh` left,
  without building or recreating anything. A start takes as long as the health checks do: `status`
  shows when the applications are healthy.
- `stop` refuses an environment that isn't `onDemand` (production, say), because it is meant to
  stay up: `--force` stops it anyway. Without `--force` it also leaves alone an application whose
  catalog entry sets `onDemand: false` for that environment.
- Without arguments (`ygg env`, or the menu), it shows `env status` and asks what to do
  and which environment; stopping an environment that isn't on demand then asks for confirmation.
- Deploys respect the switch: Jenkins' deploys on a push to `develop` or `release/*` leave a stopped
  on-demand environment stopped (after checking the new version is healthy), while a
  **Build with Parameters → `DEPLOY_TO`** deploy leaves it running.

