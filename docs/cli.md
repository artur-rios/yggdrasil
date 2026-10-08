# The host helper: `scripts/ygg.sh`

An interactive menu, on an Ubuntu host (a VPS, a VM, WSL or a laptop), for what
[setup.md](setup.md) otherwise has you do by hand:

- install the tools, or check they are there
- set up a new API, web front end or worker
- see what runs on the host
- change an application's configuration and redeploy it

It runs the same pieces as the guide: `scripts/catalog.py`, `scripts/deploy.sh`,
`scripts/platform.sh`, the files in `stacks/` and the env files in `/etc/yggdrasil`. So whatever it
does, you can check, change or undo by hand.

```bash
cd /opt/yggdrasil
scripts/ygg.sh
```

```text
== yggdrasil on vps-1 ==
Repository /opt/yggdrasil, secrets /etc/yggdrasil
What do you want to do?
  1) Check the tools and this host
  2) Install what is missing
  3) Set up a new application (API, web front end or worker)
  4) See what runs on this host
  5) Change an application's configuration
  6) Quit
>
```

Each menu entry is also a command, for scripts and for going straight to it:

| Command | Does |
|---|---|
| `scripts/ygg.sh check` | Lists what is installed and what is missing, without changing anything |
| `scripts/ygg.sh install` | Installs what `check` found missing, asking before each part |
| `scripts/ygg.sh add` | Sets up a new application |
| `scripts/ygg.sh status` | Shows the applications of this host's environment, their state and version |
| `scripts/ygg.sh config [<app>]` | Shows and changes an application's env file, and redeploys it |

| Environment variable | Default | What |
|---|---|---|
| `YGG_SECRETS_DIR` | `/etc/yggdrasil` | Where the env files are, as for `deploy.sh` and `platform.sh` |
| `YGG_APPS_DIR` | `~/yggdrasil-apps` | Where `add` clones the applications it deploys, and where redeploys build from |
| `YGG_ENVIRONMENT` | `ENVIRONMENT` in `platform.env` | This host's environment. Without either, it asks. Set it on a laptop, which has no platform |

## Check and install

`check` looks at:

| Group | Checks |
|---|---|
| **Tools** | Ubuntu; `git`, `curl`, `openssl`, `python3`, `htpasswd` (apache2-utils) and PyYAML; Docker Engine; Compose 2.24 or later; Buildx; whether your user can reach the Docker daemon |
| **This host** | The secrets directory and its group; `platform.env` and `acme.env`; the catalog is valid; the host's environment; the platform is running; ufw |

`install` works on Ubuntu only. It needs root or `sudo`, and asks before each part:

1. **Packages** from Ubuntu's repositories: `git curl openssl python3 python3-yaml apache2-utils ca-certificates`.
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
| Id | | The catalog `id`, the Compose project, the network alias, the image name |
| Display name | the id | `name` |
| Kind | | `kind`: `api`, `web` or `worker` |
| System | | An existing system, or a new one with its own id, name and description |
| Repository name | the id | `repository`, when it differs from the id |
| Dockerfile only, or its own `docker-compose.yml` | | Which stack files it writes (below) |
| Service to route to | `api` / `ui` | With its own Compose file: the service Traefik routes to |
| Container port | `8080` | The port in `health` and in the Traefik service |
| Health path | `/health`, `/healthz` for web | `health: http://<id>:<port><path>` |
| How to check health inside the container | | A `healthcheck:` using `wget` or `curl`, or none if the Dockerfile has a `HEALTHCHECK` |
| Public host | the id; none for a worker | `host`, and the `PUBLIC_HOST` router |
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

- **The env file** for this host's environment, `/etc/yggdrasil/<environment>/<id>.env`. It holds
  every variable the stack files read, except the ones `deploy.sh` sets itself, plus `PUBLIC_HOST`
  (`<host>.<DOMAIN>`) in a `proxy` environment. It offers to open it in your editor.
- **A first deploy**, if you agree: it clones the repository into `~/yggdrasil-apps/<id>` (enter
  the SSH URL for a private repository) and runs `deploy.sh`. The version defaults to
  `<latest tag>-<commit>`, the way Jenkins labels releases. In a `proxy` environment the platform
  must be up first.

Last, it lists what the catalog can't do for you: push the catalog and stack files to `main`, set up
the application's repository, install the GitHub App on it, apply the rules, and restart the status
API (and Jenkins) on every host.

The generated files are a starting point, like the examples in `stacks/`: edit them freely.

## See what runs

`status` lists every catalog application that deploys to this host's environment:

```text
== Applications in production on vps-1 ==
  APPLICATION            STATE           HEALTH     VERSION          COMMIT   DEPLOYED
  heimdall-api           running 1/1     healthy    1.4.0            3f2a9c1  2026-09-28T19:55:01Z
  heimdall-ui            running 1/1     healthy    1.4.0            7348de1  2026-09-28T23:58:36Z
  shop-web               not deployed
  Platform: 10 containers running (scripts/platform.sh ps for details).
```

The version, commit and deploy time are the labels `deploy.sh` puts on every container, the same
ones the status API reads. From there you can show an application's last 100 log lines, restart
its containers, or list the platform's services.

## Change the configuration

`config` opens an application's env file on this host, `/etc/yggdrasil/<environment>/<id>.env`. If
the file doesn't exist yet, as on a new host, it creates it from the stack files as `add` does. Then:

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
  version doesn't become healthy, `deploy.sh` rolls back as usual.

The file keeps its owner, group and mode (`640`, group `docker`), so the Jenkins agent can still
read it. Changes you don't apply reach the application on its next deploy, from Jenkins or by hand.
