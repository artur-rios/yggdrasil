# `ygg`: the host's command line app

`ygg` runs on an Ubuntu host (a VPS, a VM, WSL or a laptop) and does what [setup.md](setup.md)
otherwise has you do by hand:

- install the tools, or check they are there, and set up a machine as a dev or apps machine
  ([hosts.md](hosts.md))
- start the Jenkins build agents of the `ci` and `release` users, and back up the machine
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
`scripts/platform.sh`, `scripts/agent.sh`, `scripts/backup.sh`, `scripts/vars.py`, `scripts/catalog.py`,
the files in `stacks/` and the variables store, or the env files, in `/etc/yggdrasil`. Whatever it does, you can check, change or
undo by hand.

## Install

From the yggdrasil checkout (`/opt/yggdrasil` on a host):

```bash
scripts/ygg.sh self-install      # links /usr/local/bin/ygg to this checkout, installs tab completion
ygg version                      # yggdrasil 0.7.0 (1a2b3c4) at /opt/yggdrasil
```

`ygg install` offers it too (for the `workstation` role; on a dev or apps machine, run `self-install`
yourself after the install, [setup.md 8.4](setup.md#84-the-ygg-command)). `scripts/ygg.sh` keeps working
everywhere `ygg` does, for scripts and docs written before 0.7; on a host without python3 it still runs
`check` and `install`, with their `--role`.

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
| **Host** | `check`, `install`, `agent`, `backup`, `self-install` |
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

**No terminal.** In a pipe, a script, a terminal smaller than 60×12 or one this machine has no
terminfo entry for (a new terminal's own `TERM` over SSH, say), or with `YGG_PLAIN` set to any
value, the menu is numbered lines: type a number, or a row's exact text; an empty line or `0` goes
back; `q` quits.

**The questions of the host operations.** `add`, `install`, `deploy`, `status`, `env` and `config`
(without a store) ask in the terminal, below what they printed, which stays in view: a list you
move through with the arrow keys (typing filters it), a text field, or yes/no. A long question is
wrapped, not cut. Once answered, the question leaves one line with its answer (`Then: start`). Esc
on a choice with a **Back** option goes back; on any other choice or a text question it stops the
operation; on a yes/no question it answers no. Ctrl-C stops the operation (exit 130). Without a
terminal, with `TERM=dumb` or `YGG_PLAIN`, or when the arrow-key prompt can't run, they are plain
numbered questions.

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

From the command line, `ygg config <application> <environment>` opens that screen directly, in a
terminal (one of them left out is asked). An application or environment that isn't on this host is
refused, as without a store (exit 1).

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
  (`DB_PASSWORD`, `API_KEY`, `HEIMDALL_AUTH_SIGNING_KEY_PREVIOUS`, `FORTUNA_DATA_CONNECTIONSTRING`;
  not `HEIMDALL_PASSWORD_RESET_URL`) are typed hidden and shown as `********`, unless you ask to see them.
- **Edit** the file in `$EDITOR` (default `nano`).
- **Apply**: redeploys with `deploy.sh`. Containers read their env file only when they are created,
  and a web front end's build arguments are compiled into its image, so the image is rebuilt and
  the containers recreated. It builds from `~/yggdrasil-apps/<id>`, or asks for a checkout. By
  default it keeps the running version's label when the checkout is at that commit. If the new
  version doesn't become healthy, `deploy.sh` rolls back as usual. In an on-demand environment
  where the application is stopped, it asks whether to leave it running afterwards.

A file keeps its owner, group and mode (owned by `ygg`, who runs `ygg config`), so the deploy agent, which
reads it as `ygg` inside its container, can still read it. Changes you don't apply reach the application on its next deploy, from Jenkins or by hand.

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
| `ygg check [--role dev\|apps\|workstation]` | Host › check | What is installed and what is missing, for the machine's role |
| `ygg install [--role dev\|apps\|workstation]` | Host › install | Installs what is missing, asking before each part. A dev or apps machine: as the admin |
| `ygg agent up\|down\|ps\|logs --user ci\|release` | Host › agent | The Jenkins build agent of a service user (`scripts/agent.sh`), as that user. As the admin |
| `ygg backup run\|list` | Host › backup | Back up this machine, or list the backups (`scripts/backup.sh`). As `ygg` |
| `ygg self-install` | Host › self-install | The `ygg` command and its tab completion |
| `ygg catalog validate \| environments \| applications \| systems \| show \| plan \| environment \| get \| owner \| repository` | Catalog | Reads `catalog.yaml` (`scripts/catalog.py`) |
| `ygg help [<command>]`, `ygg version`, `ygg completion bash` | Help and version | |

Exit status: 0 success, 1 a command failed, 2 a usage error (an unknown command, a missing argument),
130 stopped with Ctrl-C.
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

A machine has a **role**: `dev`, `apps` or `workstation`. `--role` picks it; without it the command uses
the role `install` wrote to `/etc/yggdrasil-role`, and `workstation` when there is none. `dev` and `apps`
are the Ubuntu 24.04 servers of [hosts.md](hosts.md), where every Docker engine is rootless;
`workstation` is the check and install that came before the roles, for a laptop or a host set up by hand.

### `--role dev` and `--role apps`

`check` prints one row per piece (`ok`, `warn` or `missing`) and changes nothing:

| Group | Checks |
|---|---|
| **System** | The packages; the machine's own Docker is switched off, and disabled after a reboot, and nobody is in the `docker` group; the three system files (unprivileged ports, AppArmor, cgroup delegation); unattended upgrades |
| **Users** | The role's service users: their shell, a home of mode `750`, no `sudo` or privileged group; the `ci` limits (dev); SSH hardening, **as sshd really runs it** (`sshd -T`), not only the file; the checkout is `ygg`'s; each secrets directory is its user's, mode `750`; the role file |
| **Firewall** | `ufw` is on and denies incoming by default, with the role's ports, and no other rule lets everyone in (a port, a range or a profile such as `Nginx Full`). On an apps machine, 80 and 443 are open to Cloudflare only, and the rules and `cloudflare-ips` match Cloudflare's current list |
| **Rootless Docker** | Per user: its engine answers on `/run/user/<uid>/docker.sock` and is rootless; linger is on; the `slirp4netns` settings are there. On a dev machine: `ci` and `release` cannot read `ygg`'s socket or secrets |
| **Backups** | `ygg`'s backup units are the checkout's copies and the timer is enabled |

Rows that ask another user's engine or read a closed directory need `sudo`. `check` runs `sudo -v` first, which asks
for your password once; if that fails it says so and goes on, and those rows say "not checked".

`install` works on Ubuntu 24.04 only, as the **admin user** with `sudo` (it refuses `root`), and asks before each
part: packages, turning the machine's Docker off, system settings, unattended upgrades, service users, the `ci`
limits, SSH, the checkout, secrets directories, the firewall, each user's engine, the backup timer and the role
file. It refuses up front, changing nothing: when it doesn't run from `/opt/yggdrasil` (clone yggdrasil there), when
`sudo -v` fails (a check that can't run as root would find nothing to do), and when the admin has no
`~/.ssh/authorized_keys`. It writes the role file before the first part it does, so an install that stops halfway
is still a dev or apps machine to the next `ygg install`. It puts its SSH file back if sshd would not run with it. Each part, and what you would type by hand, is in
[setup.md step 8](setup.md#83-install); the files and why, in [hosts.md](hosts.md#4-system-settings).

`install` does not write `backup.conf`: copy `env/backup.conf.example` yourself
([hosts.md](hosts.md#backupconf)).

### `--role workstation`

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
   `acme.env` copied from `env/`.
5. **A firewall** (ufw) letting in only SSH, 80 and 443. It's for servers, so the default is no.

The platform itself (`platform.sh`) now runs only on a **rootless** engine of the user running it, so a
workstation-role host needs one set up by hand ([setup.md 8.6](setup.md#86-not-on-ubuntu-2404-or-not-behind-cloudflare)).

## Build agents

```bash
ygg agent up --user ci          # start or update the CI agent
ygg agent logs --user release   # follow the release agent's log
ygg agent ps --user ci
ygg agent down --user ci
```

On the dev machine, the `ci` and `release` users each run a Jenkins build agent on their own rootless engine
([hosts.md](hosts.md#3-users)). `ygg agent` is run by the **admin**: it uses `sudo` to run
`scripts/agent.sh` as the service user, which has no login. `--user` is required and is `ci` or `release`.

`agent.sh` reads `/etc/yggdrasil-<user>/agent.env`, which `ygg install` copied from
[env/agent.env.example](../env/agent.env.example). Fill it in as that file says:

| Variable | Value |
|---|---|
| `JENKINS_URL` | The controller's address; the agent dials out to it over WebSocket |
| `JENKINS_AGENT_NAME` | `ci` for the `ci` user, `release` for `release`. Left empty in the template on purpose, so an unedited copy fails instead of starting the wrong node. Lowercase letters, digits, `-` and `_` |
| `JENKINS_AGENT_SECRET` | The node's secret, from its page in Jenkins once the node exists |
| `YGG_AGENT_WORKDIR` | Absolute, owned by the user: `/home/ci/agent` for `ci`, `/home/release/agent` for `release`. Mounted at the same path in the agent and on the host, so a check's `docker run -v $WORKSPACE:…` finds the same directory in the engine. Also empty in the template |

An empty or missing variable stops `agent.sh` before anything starts, and says which. The Compose project is
`yggdrasil-agent-<name>` (so the two agents never share one), from
[platform/build-agent.yml](../platform/build-agent.yml). `up` builds the agent's image and starts it, waiting
until it runs. This agent is not the deploy agent: that one is the `agent` profile of the platform stack and runs on the
apps machine.

## Backups

```bash
ygg backup run      # dump now, prune old days, send them to the other machine
ygg backup list     # the days and files in ~/backups
```

Run as `ygg`, whose engine holds the data; the nightly timer does it at 03:30. What is backed up and where it goes
is `backup.conf`; how to set it up and how to restore is in [hosts.md](hosts.md#7-backups-and-restore).

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

