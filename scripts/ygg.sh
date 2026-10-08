#!/usr/bin/env bash
# yggdrasil's host helper: one menu for setting up an Ubuntu host and the applications it runs.
#
#     scripts/ygg.sh                    the menu
#     scripts/ygg.sh check              which tools and host pieces are there, which are missing
#     scripts/ygg.sh install            installs what is missing (Ubuntu; asks before each part)
#     scripts/ygg.sh add                sets up a new API, web front end or worker: catalog entry,
#                                       stack files, env file, checkout, first deploy
#     scripts/ygg.sh status             what runs on this host, per environment and application
#     scripts/ygg.sh config [<app>] [<environment>]
#                                       shows and changes an application's env file in one of the
#                                       host's environments, and redeploys
#     scripts/ygg.sh env status         the host's environments: on demand or not, running or stopped
#     scripts/ygg.sh env start <environment>
#     scripts/ygg.sh env stop <environment> [--force]
#                                       starts or stops every application of an environment of this
#                                       host; stop refuses an environment that is not onDemand
#                                       (catalog.yaml) unless --force
#     scripts/ygg.sh vars <command>     the variables store (scripts/vars.py): list, set, edit,
#                                       history, rollback, import, check, backup
#
# It drives the same pieces docs/setup.md does by hand -- scripts/catalog.py, scripts/deploy.sh,
# scripts/platform.sh, the stacks/ files and the env files under $YGG_SECRETS_DIR (default
# /etc/yggdrasil) -- so anything it does can also be done, or undone, by hand. Checkouts of the
# applications it deploys go under $YGG_APPS_DIR (default ~/yggdrasil-apps). The host's environments
# are ENVIRONMENTS in platform.env (ENVIRONMENT in one from before 0.5); $YGG_ENVIRONMENT names the
# one environment of a machine without platform.env (a laptop), and overrides platform.env.
# Reference: docs/cli.md.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}
apps_dir=${YGG_APPS_DIR:-$HOME/yggdrasil-apps}
me=$(id -un)
host_env=${YGG_ENVIRONMENT:-}

ID_PATTERN='^[a-z0-9]+(-[a-z0-9]+)*$'
# The Compose version that understands !reset, which the proxy overlays use (docs/setup.md step 0).
COMPOSE_MINIMUM=2.24.0
# Distribution packages that clash with Docker's own (docs.docker.com/engine/install/ubuntu).
CONFLICTING=(docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc)
# Variables the stacks read that deploy.sh sets itself: never asked for in an env file.
DEPLOY_VARIABLES='^(APP_DIR|APP_ENV_FILE|IMAGE_TAG|API_IMAGE_TAG|YGG_ENVIRONMENT)$'

if [[ -t 1 ]]; then
  bold=$'\e[1m' dim=$'\e[2m' red=$'\e[31m' green=$'\e[32m' yellow=$'\e[33m' reset=$'\e[0m'
else
  bold="" dim="" red="" green="" yellow="" reset=""
fi

say() { printf '%s\n' "$*"; }
title() { printf '\n%s== %s ==%s\n' "$bold" "$*" "$reset"; }
note() { printf '%s%s%s\n' "$dim" "$*" "$reset"; }
warn() { printf '%s%s%s\n' "$yellow" "$*" "$reset" >&2; }
die() { printf '%sygg: %s%s\n' "$red" "$*" "$reset" >&2; exit 1; }

# ---- Prompts ------------------------------------------------------------------------------------

# The prompts set the caller's variable by name, so their own locals start with an underscore: a
# local with the caller's variable's name would hide it.

# ask <variable> <question> [default]
ask() {
  local _answer
  read -r -p "$2${3:+ [$3]}: " _answer || exit 1
  printf -v "$1" '%s' "${_answer:-${3:-}}"
}

# ask_match <variable> <question> <regex> <what it must be> [default]: until the answer matches.
# An empty answer is accepted when the regex accepts it.
ask_match() {
  local _value
  while true; do
    ask _value "$2" "${5:-}"
    if [[ "$_value" =~ $3 ]]; then
      printf -v "$1" '%s' "$_value"
      return
    fi
    warn "It must be $4."
  done
}

# confirm <question> [y]: true on yes. With y, an empty answer is yes.
confirm() {
  local _answer
  if [[ "${2:-}" == y ]]; then
    read -r -p "$1 [Y/n]: " _answer || exit 1
    [[ -z "$_answer" || "$_answer" == [yY]* ]]
  else
    read -r -p "$1 [y/N]: " _answer || exit 1
    [[ "$_answer" == [yY]* ]]
  fi
}

# choose <variable> <question> <option>...: the chosen option's text.
choose() {
  local _variable=$1 _question=$2 _answer _i
  shift 2
  (($#)) || die "nothing to choose from: $_question"
  say "$_question"
  for ((_i = 1; _i <= $#; _i++)); do printf '  %d) %s\n' "$_i" "${!_i}"; done
  while true; do
    read -r -p "> " _answer || exit 1
    if [[ "$_answer" =~ ^[0-9]+$ ]] && ((_answer >= 1 && _answer <= $#)); then
      printf -v "$_variable" '%s' "${!_answer}"
      return
    fi
    warn "Pick a number from 1 to $#."
  done
}

pause() {
  if [[ -t 0 ]]; then read -r -p "${dim}Enter to continue${reset} " _ || true; fi
}

# ---- Host facts ---------------------------------------------------------------------------------

as_root() { if ((EUID == 0)); then "$@"; else sudo "$@"; fi; }
# apt-get without its own questions (tzdata's, say): they would read the menu's answers.
apt_get() { as_root env DEBIAN_FRONTEND=noninteractive apt-get -q "$@" </dev/null; }
has() { command -v "$1" >/dev/null 2>&1; }
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }
has_yaml() { has python3 && python3 -c 'import yaml' 2>/dev/null; }
docker_ok() { has docker && docker info >/dev/null 2>&1; }
catalog() { python3 "$root/scripts/catalog.py" "$@"; }

# version_at_least <version> <minimum>
version_at_least() { [[ "$(printf '%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }

# shellcheck source=/dev/null
os_field() { (. /etc/os-release 2>/dev/null && eval "echo \"\${$1:-}\""); }
is_ubuntu() { [[ "$(os_field ID)" == ubuntu || " $(os_field ID_LIKE) " == *" ubuntu "* ]]; }

# env_value <file> <name>: the value of NAME= in an env file, without surrounding quotes.
env_value() {
  local value
  value=$(sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n1)
  value=${value%\"} value=${value#\"} value=${value%\'} value=${value#\'}
  printf '%s' "$value"
}

# The variables store (docs/variables.md): when this machine has one, every platform and application
# value comes from it instead of platform.env and <environment>/<app>.env.
has_store() { [[ -f "$secrets/vars.db" ]]; }
vars_py() { python3 "$root/scripts/vars.py" "$@"; }

# need_store: stops when the store can't be used (lost or wrong key, unreadable or damaged file),
# once per process, so that "unset" is never confused with "can't read". Only that it is usable: a
# broken reference stops the deploy that needs it (vars check names them all).
store_checked=""
need_store() {
  has_store || return 0
  [[ -z "$store_checked" ]] || return 0
  local output
  output=$(vars_py check --usable 2>&1) \
    || die "the variables store can't be used: $output (scripts/ygg.sh vars check; platform.sh up --last-good starts the platform without it)"
  store_checked=1
}

# platform_value <NAME>: a platform setting, from the store or platform.env; empty when unset.
platform_value() {
  if has_store; then
    need_store
    vars_py get platform "$1" --reveal 2>/dev/null || true
  else
    env_value "$secrets/platform.env" "$1"
  fi
}

# Where the platform settings live on this machine, for messages.
platform_source() { if has_store; then echo "the variables store"; else echo "$secrets/platform.env"; fi; }

# Whether an application has variables of its own in an environment on this machine.
app_has_variables() {
  if has_store; then
    need_store
    [[ -n "$(vars_py list "$1@$2" --resolved --keys)" ]]
  else
    [[ -f "$secrets/$2/$1.env" ]]
  fi
}

# app_field <app> <field>: one field of the application's catalog entry, empty when unset.
app_field() {
  catalog show "$1" | python3 -c 'import json, sys; v = json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else v)' "$2"
}

need_catalog() {
  has_yaml || die "python3 and PyYAML are needed first: run '$0 install'"
  catalog validate >/dev/null || exit 1
}

need_docker() {
  has docker || die "Docker is not installed: run '$0 install'"
  docker info >/dev/null 2>&1 \
    || die "can't reach Docker as $me: is the daemon running, and are you in the docker group (log out and in after '$0 install')?"
}

# The environments this host runs, in catalog order, in host_envs: $YGG_ENVIRONMENT (one), else
# ENVIRONMENTS in platform.env, else ENVIRONMENT there (a platform.env from before 0.5), else asked
# once. A `ports` machine (a developer laptop) has no platform, so it has no platform.env to say.
host_envs=()
host_environments() {
  ((${#host_envs[@]})) && return 0
  local configured source known listed environment
  [[ -n "$host_env" ]] || need_store
  if [[ -n "$host_env" ]]; then
    configured=$host_env source="YGG_ENVIRONMENT"
  else
    source="ENVIRONMENTS in $(platform_source)"
    configured=$(platform_value ENVIRONMENTS)
    [[ -n "$configured" ]] || configured=$(platform_value ENVIRONMENT)
  fi
  mapfile -t known < <(catalog environments)
  if [[ -z "${configured//[ ,]/}" ]]; then
    choose configured "Which environment does this host run? ($(platform_source) doesn't say; YGG_ENVIRONMENT=<id> skips this question)" "${known[@]}"
    source="your answer"
  fi
  IFS=', ' read -r -a listed <<<"$configured"
  for environment in "${listed[@]}"; do
    [[ -z "$environment" ]] || printf '%s\n' "${known[@]}" | grep -Fqx "$environment" \
      || die "'$environment' ($source) is not an environment in catalog.yaml"
  done
  for environment in "${known[@]}"; do
    printf '%s\n' "${listed[@]}" | grep -Fqx "$environment" && host_envs+=("$environment")
  done
  return 0
}

on_this_host() { printf '%s\n' "${host_envs[@]}" | grep -Fqx "$1"; }

# pick_environment <variable> <question> [<given>] [<app>]: one of the host's environments (those
# the application deploys to, given one): the given one, checked; the only one; or asked.
pick_environment() {
  local _variable=$1 _question=$2 _given=${3:-} _app=${4:-} _environment _candidates=()
  host_environments
  for _environment in "${host_envs[@]}"; do
    if [[ -z "$_app" ]] || catalog environments "$_app" | grep -Fqx "$_environment"; then
      _candidates+=("$_environment")
    fi
  done
  if [[ -n "$_given" ]]; then
    on_this_host "$_given" || die "'$_given' is not an environment of this host ($(IFS=,; echo "${host_envs[*]}"))"
    printf '%s\n' "${_candidates[@]}" | grep -Fqx "$_given" || die "'$_app' does not deploy to $_given (catalog.yaml)"
    printf -v "$_variable" '%s' "$_given"
  elif ((${#_candidates[@]} == 1)); then
    printf -v "$_variable" '%s' "${_candidates[0]}"
  elif ((${#_candidates[@]} == 0)); then
    die "${_app:-nothing} deploys to none of this host's environments ($(IFS=,; echo "${host_envs[*]}"))"
  else
    choose "$_variable" "$_question" "${_candidates[@]}"
  fi
}

# The applications of the catalog that deploy to an environment.
applications_in() {
  local app
  for app in $(catalog applications --deployable); do
    catalog environments "$app" | grep -Fqx "$1" && echo "$app"
  done
  return 0
}

# ---- Check --------------------------------------------------------------------------------------

missing_packages=() need_docker_install="" need_group="" need_secrets=""

row() {
  local mark
  case $1 in
    ok) mark="${green}ok${reset}" ;;
    missing) mark="${red}--${reset}" ;;
    *) mark="${yellow}!!${reset}" ;;
  esac
  printf '  %s  %-20s %s\n' "$mark" "$2" "$3"
}

# ufw's rules need root: empty when sudo would ask for a password.
ufw_status() { if ((EUID == 0)); then ufw status; else sudo -n ufw status 2>/dev/null; fi; }

# check_host [quiet]: fills in what install has to do; prints a report unless quiet.
check_host() {
  missing_packages=() need_docker_install="" need_group="" need_secrets=""
  if [[ -n "${1:-}" ]]; then
    check_report >/dev/null
  else
    check_report
  fi
}

check_report() {
  title "Tools"
  if is_ubuntu; then
    row ok "Ubuntu" "$(os_field PRETTY_NAME)"
  else
    row warn "Ubuntu" "$(os_field PRETTY_NAME || echo unknown): install only knows Ubuntu, check works anywhere"
  fi

  local command package version
  for package in git curl openssl python3 apache2-utils; do
    command=$package
    [[ $package == apache2-utils ]] && command=htpasswd
    if has "$command"; then
      version=$("$command" --version 2>/dev/null | head -n1 || true)
      row ok "$command" "${version:-installed}"
    else
      row missing "$command" "package $package"
      missing_packages+=("$package")
    fi
  done
  installed ca-certificates || missing_packages+=(ca-certificates)
  if has_yaml; then
    row ok "PyYAML" "$(python3 -c 'import yaml; print(yaml.__version__)')"
  else
    row missing "PyYAML" "package python3-yaml"
    missing_packages+=(python3-yaml)
  fi

  if has docker; then
    row ok "docker" "$(docker --version)"
    local conflicting=() name
    for name in "${CONFLICTING[@]}"; do installed "$name" && conflicting+=("$name"); done
    if ((${#conflicting[@]})); then
      row warn "docker packages" "the distribution's (${conflicting[*]}): install replaces them with Docker's"
      need_docker_install=1
    fi
    if docker compose version >/dev/null 2>&1; then
      version=$(docker compose version --short 2>/dev/null)
      if version_at_least "${version#v}" "$COMPOSE_MINIMUM"; then
        row ok "docker compose" "$version"
      else
        row missing "docker compose" "$version: $COMPOSE_MINIMUM or later is needed"
        need_docker_install=1
      fi
    else
      row missing "docker compose" "the Compose plugin is missing"
      need_docker_install=1
    fi
    if docker buildx version >/dev/null 2>&1; then
      row ok "docker buildx" "$(docker buildx version | cut -d' ' -f2)"
    else
      row missing "docker buildx" "the Buildx plugin is missing"
      need_docker_install=1
    fi
    if docker info >/dev/null 2>&1; then
      row ok "docker daemon" "reachable as $me"
    elif ((EUID != 0)) && ! id -nG "$me" | tr ' ' '\n' | grep -qx docker; then
      row missing "docker group" "$me is not in the docker group"
      need_group=1
    elif ((EUID != 0)) && ! id -nG | tr ' ' '\n' | grep -qx docker; then
      row warn "docker group" "$me was added to it: log out and back in (or run newgrp docker)"
    else
      row missing "docker daemon" "not running: sudo systemctl start docker"
    fi
  else
    row missing "docker" "Docker Engine, Compose and Buildx, from Docker's apt repository"
    need_docker_install=1
    ((EUID == 0)) || need_group=1
  fi

  title "This host"
  if [[ -d "$secrets" ]]; then
    local group
    group=$(stat -c %G "$secrets" 2>/dev/null || echo "?")
    if [[ "$group" == docker ]]; then
      row ok "secrets directory" "$secrets"
    else
      row warn "secrets directory" "$secrets belongs to group $group: the Jenkins agent reads it as docker"
    fi
    local file
    if has_store; then
      if vars_py check >/dev/null 2>&1; then
        row ok "variables store" "$secrets/vars.db"
      else
        row warn "variables store" "fails its check: $0 vars check"
      fi
    else
      for file in platform.env acme.env; do
        if [[ -f "$secrets/$file" ]]; then
          row ok "$file" "$secrets/$file"
        else
          row missing "$file" "not needed on a ports-only machine; install copies the template"
          need_secrets=1
        fi
      done
    fi
  else
    row missing "secrets directory" "$secrets"
    need_secrets=1
  fi

  if has_yaml; then
    if version=$(catalog validate 2>&1); then
      row ok "catalog" "$version"
    else
      row missing "catalog" "invalid: python3 scripts/catalog.py validate"
    fi
    local environments
    environments=$(platform_value ENVIRONMENTS)
    if [[ -n "$host_env" ]]; then
      row ok "environments" "$host_env (YGG_ENVIRONMENT)"
    elif [[ -n "$environments" ]]; then
      row ok "environments" "$environments ($(has_store && echo "variables store" || echo platform.env))"
    elif environments=$(platform_value ENVIRONMENT) && [[ -n "$environments" ]]; then
      row warn "environments" "$environments ($(platform_source)'s ENVIRONMENT, from before 0.5: rename it ENVIRONMENTS)"
    else
      row warn "environments" "$(platform_source) sets no ENVIRONMENTS: the menu asks for it"
    fi
  fi

  if docker_ok; then
    local running
    running=$(docker ps --filter label=com.docker.compose.project=yggdrasil --format '{{.Names}}' | wc -l)
    if docker network inspect edge >/dev/null 2>&1 && ((running > 0)); then
      row ok "platform" "$running containers running"
    else
      row warn "platform" "not running: scripts/platform.sh up (a ports-only machine doesn't need it)"
    fi
  fi

  if has ufw; then
    if version=$(ufw_status | head -n1) && [[ -n "$version" ]]; then
      row ok "firewall" "ufw: ${version#Status: }"
    else
      row ok "firewall" "ufw installed (sudo ufw status to see its rules)"
    fi
  fi
}

# ---- Install ------------------------------------------------------------------------------------

install_packages() {
  say "Installing ${missing_packages[*]} from Ubuntu's repositories"
  apt_get update
  apt_get install -y "${missing_packages[@]}"
}

# Docker's own packages, as docs.docker.com/engine/install/ubuntu does it: the distribution's
# docker.io ships a Compose too old for the overlays' !reset.
install_docker() {
  local conflicting=() name codename
  for name in "${CONFLICTING[@]}"; do installed "$name" && conflicting+=("$name"); done
  if ((${#conflicting[@]})); then
    warn "These packages clash with Docker's and are removed first: ${conflicting[*]}"
    note "Images, containers and volumes in /var/lib/docker stay."
    confirm "Remove them?" || return 0
    apt_get remove -y "${conflicting[@]}"
  fi
  codename=$(os_field UBUNTU_CODENAME)
  [[ -n "$codename" ]] || codename=$(os_field VERSION_CODENAME)
  apt_get update
  apt_get install -y ca-certificates curl
  as_root install -m 0755 -d /etc/apt/keyrings
  as_root curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  as_root chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    | as_root tee /etc/apt/sources.list.d/docker.list >/dev/null
  apt_get update
  apt_get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  # Without systemd (some WSL distributions) the service is started by hand.
  as_root systemctl enable --now docker 2>/dev/null || as_root service docker start 2>/dev/null || true
}

add_to_docker_group() {
  as_root usermod -aG docker "$me"
  warn "$me is now in the docker group: log out and back in (or run 'newgrp docker') before using Docker."
}

# The secrets directory and the platform templates, as in docs/setup.md step 8.4.
create_secrets() {
  getent group docker >/dev/null || die "the docker group doesn't exist yet: install Docker first"
  as_root install -d -m 2750 -o "$me" -g docker "$secrets"
  local file
  for file in platform.env acme.env; do
    if [[ ! -f "$secrets/$file" ]]; then
      as_root install -m 640 -o "$me" -g docker "$root/env/$file.example" "$secrets/$file"
      say "Created $secrets/$file from env/$file.example: fill it in (docs/setup.md step 9.2 or 10)."
    fi
  done
}

# Only SSH, HTTP and HTTPS in, as in docs/setup.md step 8.3. SSH is allowed before the firewall is
# on, so it doesn't cut the session running this.
setup_firewall() {
  has ufw || apt_get install -y ufw
  as_root ufw allow OpenSSH
  as_root ufw allow 80,443/tcp
  as_root ufw --force enable
  as_root ufw status
}

install_host() {
  is_ubuntu || die "install knows Ubuntu only; on $(os_field PRETTY_NAME) install what check lists by hand"
  ((EUID == 0)) || has sudo || die "installing needs root: run it as root or install sudo"
  check_host quiet

  local did=""
  title "Install"
  if ((${#missing_packages[@]})); then
    say "Packages: ${missing_packages[*]}"
    confirm "Install them?" y && install_packages && did=1
  fi
  if [[ -n "$need_docker_install" ]]; then
    say "Docker Engine, Compose and Buildx, from Docker's apt repository (download.docker.com)."
    confirm "Install them?" y && install_docker && did=1
  fi
  check_host quiet
  if [[ -n "$need_group" ]] && getent group docker >/dev/null; then
    say "Your user, $me, isn't in the docker group, so every docker command would need sudo."
    confirm "Add $me to it?" y && add_to_docker_group && did=1
  fi
  if [[ -n "$need_secrets" ]]; then
    say "The secrets directory $secrets, and platform.env and acme.env from their templates."
    confirm "Create them?" y && create_secrets && did=1
  fi
  say "A firewall that lets in only SSH, HTTP and HTTPS. For a server; skip it on a laptop or in WSL."
  confirm "Set up ufw?" && setup_firewall && did=1

  [[ -n "$did" ]] || say "Nothing installed."
  check_host
}

# ---- Stack and env files ------------------------------------------------------------------------

# write_file <path> <content>, asking before replacing a file.
write_file() {
  if [[ -e "$1" ]] && ! confirm "${1#"$root"/} exists. Replace it?"; then
    say "Kept ${1#"$root"/}"
    return
  fi
  printf '%s\n' "$2" >"$1"
  say "Wrote ${1#"$root"/}"
}

# The Compose service, when the repository has only a Dockerfile.
# stack_service <id> <port> <health path> <healthcheck: wget|curl|""> <build args...>
stack_service() {
  local id=$1 port=$2 path=$3 probe=$4 arg
  shift 4
  cat <<EOF
# $id: written by scripts/ygg.sh. The repository has only a Dockerfile, so this is its Compose
# service; stacks/$id.proxy.yml adds Traefik and stacks/$id.ports.yml a host port. Edit freely.
#
# Every variable of the env file reaches the container (env_file). Build arguments are compiled
# into the image: public, and different per environment -- never a secret.

services:
  app:
    build:
      context: \${APP_DIR:?APP_DIR is set by scripts/deploy.sh}
EOF
  if (($#)); then
    say "      args:"
    for arg in "$@"; do say "        $arg: \${$arg:-}"; done
  fi
  cat <<EOF
    image: $id:\${IMAGE_TAG:-latest}
    restart: unless-stopped
    env_file:
      # Set by deploy.sh, which checks the file exists; not required, so that CI's
      # docker compose config runs without one.
      - path: \${APP_ENV_FILE:?APP_ENV_FILE is set by scripts/deploy.sh}
        required: false
EOF
  case $probe in
    wget) printf '    healthcheck:\n      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:%s%s"]\n' "$port" "$path" ;;
    curl) printf '    healthcheck:\n      test: ["CMD", "curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:%s%s"]\n' "$port" "$path" ;;
  esac
  [[ -n "$probe" ]] && printf '      interval: 15s\n      timeout: 5s\n      retries: 5\n      start_period: 30s\n'
  cat <<EOF
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "5"
EOF
}

# The proxy overlay. stack_proxy <id> <service> <port> <host> <metrics: yes|""> <own compose: yes|"">
stack_proxy() {
  local id=$1 service=$2 port=$3 host=$4 metrics=$5 own=$6
  say "# $id in proxy environments: written by scripts/ygg.sh. No host port: Traefik is the only way in."
  say "# Several environments can share a host, so names carry YGG_ENVIRONMENT (set by scripts/deploy.sh)."
  say "# On the edge network, the alias $id.<environment> is how the status API reaches its health check."
  [[ -n "$metrics" ]] && say "# On the telemetry network, Prometheus scrapes its metrics at the same alias."
  [[ -n "$host" ]] && say "# Routed at PUBLIC_HOST from the env file: the catalog's host, $host, with the environment's"
  [[ -n "$host" ]] && say "# hostSuffix, under the DOMAIN. Labels are a list: Compose substitutes variables in values only."
  printf '\nservices:\n  %s:\n' "$service"
  [[ -n "$own" ]] && say "    ports: !reset []"
  say "    networks:"
  # The repository's own Compose file may run other services (a database) on the default network.
  [[ -n "$own" ]] && say "      default: {}"
  local alias="\"$id.\${YGG_ENVIRONMENT:?YGG_ENVIRONMENT is set by scripts/deploy.sh}\""
  printf '      edge:\n        aliases: [%s]\n' "$alias"
  [[ -n "$metrics" ]] && printf '      telemetry:\n        aliases: [%s]\n' "$alias"
  if [[ -n "$own" ]]; then
    printf '    logging:\n      driver: json-file\n      options:\n        max-size: 10m\n        max-file: "5"\n'
  fi
  if [[ -n "$host" ]]; then
    cat <<EOF
    labels:
      - traefik.enable=true
      - traefik.docker.network=edge
      - traefik.http.routers.$id-\${YGG_ENVIRONMENT}.rule=Host(\`\${PUBLIC_HOST:?set PUBLIC_HOST in the env file}\`)
      - traefik.http.routers.$id-\${YGG_ENVIRONMENT}.entrypoints=websecure
      - traefik.http.routers.$id-\${YGG_ENVIRONMENT}.middlewares=secure-headers@file
      - traefik.http.services.$id-\${YGG_ENVIRONMENT}.loadbalancer.server.port=$port
EOF
  fi
  printf '\nnetworks:\n  edge:\n    external: true\n'
  [[ -n "$metrics" ]] && printf '  telemetry:\n    external: true\n'
  return 0
}

# The ports overlay: published on the loopback interface only. stack_ports <id> <port> <host port>
stack_ports() {
  cat <<EOF
# $1 in ports environments (a developer machine): written by scripts/ygg.sh. Published on the
# loopback interface only, no Traefik. HOST_PORT in the env file changes the address.

services:
  app:
    ports:
      - "\${HOST_PORT:-$3}:$2"
EOF
}

# The stack files deploy.sh would combine for this application in this environment.
stack_files() {
  local id=$1 environment=$2 mode
  mode=$(catalog get "$id" "$environment" mode)
  local file
  for file in "$root/stacks/$id.yml" "$root/stacks/$id.$mode.yml" "$root/stacks/$id.$environment.yml"; do
    [[ -f "$file" ]] && echo "$file"
  done
  return 0
}

# create_env_file <app> <environment>: the application's env file on this host, or its scope in the
# variables store, with the variables its stack files read. The ones deploy.sh sets itself are left
# out. The text is built in a private temporary file, removed whatever happens. Callers test its
# status (errexit is off there), so every failure returns 1 itself.
create_env_file() {
  local id=$1 environment=$2 dir="$secrets/$2" file="$secrets/$2/$1.env"
  local mode host suffix domain="" name default files resolved="" text
  need_store
  if has_store; then resolved=$'\n'$(vars_py list "$id@$environment" --resolved --keys)$'\n'; fi
  if [[ ! -w "$secrets" ]]; then
    warn "Can't write to $secrets: create it with '$0 install' (or set YGG_SECRETS_DIR), then run '$0 config $id'."
    return 1
  fi
  mode=$(catalog get "$id" "$environment" mode)
  suffix=$(catalog get "$id" "$environment" hostSuffix)
  host=$(app_field "$id" host)
  mapfile -t files < <(stack_files "$id" "$environment")
  if [[ "$mode" == proxy && -n "$host" ]]; then
    domain=$(platform_value DOMAIN)
    [[ -n "$domain" && "$domain" != example.com ]] || ask domain "This host's DOMAIN ($(platform_source) doesn't say)" "example.com"
  fi
  has_store || [[ -d "$dir" ]] || install -d -m 2750 "$dir" || return 1
  text=$(mktemp) || return 1
  {
    say "# $id in $environment. Created by scripts/ygg.sh; change it with: scripts/ygg.sh config $id $environment"
    say "# It fills in the \${VAR}s of the Compose files, and reaches the container where they hand it"
    say "# over (env_file, or environment: entries). Never commit it."
    if [[ -n "$domain" && "$resolved" != *$'\n'PUBLIC_HOST$'\n'* ]]; then
      say ""
      say "# The host name Traefik routes to it: the catalog's host, with $environment's hostSuffix, under DOMAIN."
      say "PUBLIC_HOST=$host$suffix.$domain"
    fi
    if ((${#files[@]})); then
      while read -r name; do
        [[ "$name" =~ $DEPLOY_VARIABLES || "$name" == PUBLIC_HOST || "$resolved" == *$'\n'"$name"$'\n'* ]] && continue
        default=$(grep -ohE "[$][{]${name}:-[^}]*" "${files[@]}" | head -n1 | sed "s/^[$][{]${name}:-//")
        say ""
        say "# Read by $(grep -lE "[$][{]${name}[:}]" "${files[@]}" | sed "s|^$root/||" | paste -sd, -)"
        say "$name=$default"
      done < <(grep -ohE '[$][{][A-Za-z_][A-Za-z0-9_]*' "${files[@]}" | cut -c3- | sort -u)
    fi
  } >"$text"
  if has_store; then
    if ! vars_py import "$id@$environment" "$text"; then
      rm -f "$text"
      warn "Nothing was stored for $id in $environment."
      return 1
    fi
    say "Stored $id's variables in $environment (scripts/ygg.sh vars list $id@$environment)"
  else
    if ! install -m 640 "$text" "$file"; then
      rm -f "$text"
      return 1
    fi
    say "Wrote $file"
  fi
  rm -f "$text"
}

# ---- Deploy -------------------------------------------------------------------------------------

# What a checkout would be labelled by default: <latest tag>-<commit>, as Jenkins labels releases.
checkout_version() {
  local commit tag
  commit=$(git -C "$1" rev-parse HEAD | cut -c1-7)
  tag=$(git -C "$1" describe --tags --abbrev=0 2>/dev/null || echo dev)
  echo "${tag#v}-$commit"
}

# The version deployed now in an environment (running or stopped), without the image tag's
# <environment>- prefix; empty when nothing is deployed. running_version <app> <environment>
running_version() {
  local image
  # The first line in bash, not `| head -n1`, which could SIGPIPE docker under pipefail.
  image=$(docker ps --all --filter "label=com.docker.compose.project=$1-$2" --format '{{.Image}}')
  image=${image%%$'\n'*}
  [[ "$image" == *:* ]] && image=${image##*:} && echo "${image#"$2"-}"
  return 0
}

# Whether any container of the application runs in the environment. is_running <app> <environment>
is_running() { [[ -n "$(docker ps --filter "label=com.docker.compose.project=$1-$2" --format '{{.ID}}')" ]]; }

# deploy <app> <environment>: deploy.sh from the application's checkout.
deploy() {
  local id=$1 environment=$2 dir="$apps_dir/$1" version default running commit start=""
  need_docker
  if [[ ! -d "$dir/.git" ]]; then
    ask_match dir "Checkout of $id's repository to build from (a directory)" '.' "a path" "$dir"
    dir=${dir/#\~/$HOME}
    [[ -d "$dir" ]] || die "no such directory: $dir"
  fi
  default=$(checkout_version "$dir")
  running=$(running_version "$id" "$environment")
  if [[ -n "$running" ]]; then
    commit=$(git -C "$dir" rev-parse HEAD | cut -c1-7)
    if [[ "$running" == *"-$commit" || "$running" == "$commit" ]]; then
      default=$running
    else
      note "Running: $running. The checkout at $dir is at $commit: pull or check out what you mean to run."
    fi
  fi
  ask_match version "Version to label the image with" '^[A-Za-z0-9_.-]+$' "letters, digits, dots, dashes, underscores" "$default"
  # deploy.sh stops an on-demand environment's application again when it was not running before;
  # deploying by hand is usually using it, so ask.
  if [[ "$(catalog get "$id" "$environment" onDemand)" == true ]] && ! is_running "$id" "$environment"; then
    confirm "$environment is on demand and $id isn't running there: leave it running after the deploy?" && start=1
  fi
  if DEPLOY_START=$start "$root/scripts/deploy.sh" "$environment" "$id" "$dir" "$version"; then
    say "${green}$id $version runs in $environment.${reset}"
  else
    warn "The deploy failed: its output above says why (a missing env file variable, a health check that never passes...)."
    return 1
  fi
}

# clone <app> <repository>: a checkout under $YGG_APPS_DIR, to build from.
clone() {
  local id=$1 url dir="$apps_dir/$1"
  if [[ -d "$dir/.git" ]]; then
    say "Using the checkout at $dir"
    return
  fi
  has git || die "git is not installed: run '$0 install'"
  ask url "Repository URL (use the SSH URL, git@github.com:..., for a private repository)" "https://github.com/$(catalog owner)/$2.git"
  mkdir -p "$apps_dir"
  git clone "$url" "$dir"
}

# ---- Set up an application ----------------------------------------------------------------------

add_application() {
  need_catalog
  local id name kind system system_name="" system_description="" repository own service port path
  local probe="" host="" metrics="" arguments="" checks="" systems pick build_args=() mode environment
  title "Set up a new application"
  say "An API, a web front end or a worker, from its own repository. This adds it to catalog.yaml,"
  say "writes its stack files, creates its env file on this host and can deploy it here."
  say ""

  while true; do
    ask_match id "Application id (lowercase, digits, dashes; the image, and the Compose project and network alias with the environment)" "$ID_PATTERN" "lowercase letters, digits and dashes"
    catalog show "$id" >/dev/null 2>&1 || break
    warn "'$id' is already an application in catalog.yaml."
  done
  ask name "Display name" "$id"
  choose kind "What is it?" api web worker

  mapfile -t systems < <(catalog systems | cut -f1 | grep -vx yggdrasil || true)
  choose pick "Which system is it part of? (a system groups the applications of one product)" "${systems[@]}" "a new system"
  if [[ "$pick" == "a new system" ]]; then
    while true; do
      ask_match system "New system id" "$ID_PATTERN" "lowercase letters, digits and dashes" "${id%-*}"
      catalog systems | cut -f1 | grep -Fqx "$system" || break
      warn "'$system' already exists: pick it from the list instead."
    done
    ask system_name "System display name" "$system"
    ask system_description "What the system is, in a few words (optional)"
  else
    system=$pick
  fi

  ask_match repository "Repository name under $(catalog owner)" '^[A-Za-z0-9_.-]+$' "a repository name" "$id"

  choose pick "The repository has..." \
    "only a Dockerfile: yggdrasil writes its Compose service" \
    "its own docker-compose.yml: yggdrasil adds overlays to it"
  own=""
  [[ "$pick" == its* ]] && own=yes
  if [[ -n "$own" ]]; then
    ask_match service "The service of its docker-compose.yml to route to" '^[A-Za-z0-9_.-]+$' "a service name" "$([[ $kind == web ]] && echo ui || echo api)"
  else
    service=app
  fi
  ask_match port "Port it listens on inside the container" '^[0-9]{1,5}$' "a port number" 8080
  ask_match path "Health endpoint path (a 2xx answer means healthy)" '^/' "a path starting with /" "$([[ $kind == web ]] && echo /healthz || echo /health)"
  if [[ -z "$own" ]]; then
    choose pick "How can Docker check its health from inside the container? (deploy.sh rolls back a release that never gets healthy)" \
      "wget (Alpine and BusyBox-based images, nginx:alpine)" \
      "curl" \
      "it can't, or the Dockerfile has a HEALTHCHECK already"
    case $pick in wget*) probe=wget ;; curl) probe=curl ;; esac
  fi
  if [[ "$kind" != worker ]]; then
    ask_match host "Public host name, one label under each environment's DOMAIN (empty: not public)" "^([a-z0-9]+(-[a-z0-9]+)*)?$" "one lowercase label, or empty" "$id"
  fi
  ask_match metrics "Port serving Prometheus metrics, never published (empty: none)" '^([0-9]{1,5})?$' "a port number, or empty"
  if [[ -z "$own" && "$kind" == web ]]; then
    ask_match arguments "Build arguments compiled into the bundle, comma-separated, e.g. API_BASE_URL (empty: none)" \
      '^([A-Za-z_][A-Za-z0-9_]*( *, *[A-Za-z_][A-Za-z0-9_]*)*)?$' "variable names separated by commas"
    IFS=', ' read -r -a build_args <<<"$arguments"
  fi
  ask checks "GitHub check names required on its pull requests, comma-separated (empty: none yet)"

  title "Summary"
  say "  $id ($kind) in system $system, repository $(catalog owner)/$repository"
  say "  health     http://$id:$port$path"
  [[ -n "$host" ]] && say "  host       $host<hostSuffix>.<DOMAIN> (each environment's hostSuffix)"
  [[ -n "$metrics" ]] && say "  metrics    $id:$metrics"
  say "  stacks     $([[ -z "$own" ]] && echo "stacks/$id.yml, stacks/$id.ports.yml, ")stacks/$id.proxy.yml"
  confirm "Write it?" y || return 0

  YGG_ID=$id YGG_NAME=$name YGG_KIND=$kind YGG_REPOSITORY=$repository YGG_PORT=$port YGG_PATH=$path \
    YGG_HOST=$host YGG_METRICS=$metrics YGG_CHECKS=$checks YGG_SYSTEM=$system \
    YGG_SYSTEM_NAME=$system_name YGG_SYSTEM_DESCRIPTION=$system_description \
    python3 -c '
import json, os
e = os.environ
app = {"id": e["YGG_ID"], "name": e["YGG_NAME"], "kind": e["YGG_KIND"]}
if e["YGG_REPOSITORY"] != e["YGG_ID"]:
    app["repository"] = e["YGG_REPOSITORY"]
app["health"] = "http://" + e["YGG_ID"] + ":" + e["YGG_PORT"] + e["YGG_PATH"]
if e["YGG_METRICS"]:
    app["metrics"] = e["YGG_ID"] + ":" + e["YGG_METRICS"]
if e["YGG_HOST"]:
    app["host"] = e["YGG_HOST"]
checks = [c.strip() for c in e["YGG_CHECKS"].split(",") if c.strip()]
if checks:
    app["checks"] = checks
system = {"id": e["YGG_SYSTEM"], "name": e["YGG_SYSTEM_NAME"], "description": e["YGG_SYSTEM_DESCRIPTION"]}
print(json.dumps({"system": system, "application": app}))
' | catalog add-application

  if [[ -z "$own" ]]; then
    write_file "$root/stacks/$id.yml" "$(stack_service "$id" "$port" "$path" "$probe" "${build_args[@]}")"
  fi
  write_file "$root/stacks/$id.proxy.yml" "$(stack_proxy "$id" "$service" "$port" "$host" "$metrics" "$own")"
  if [[ -z "$own" ]]; then
    write_file "$root/stacks/$id.ports.yml" \
      "$(stack_ports "$id" "$port" "127.0.0.1:$((8090 + $(catalog applications --deployable | wc -l)))")"
  else
    note "In ports environments the repository's own docker-compose.yml publishes its ports as it is."
  fi

  title "This host"
  pick_environment environment "Which of this host's environments do you set it up in now? (the others: scripts/ygg.sh config $id <environment>)" "" "$id"
  mode=$(catalog get "$id" "$environment" mode)
  say "Setting it up in $environment ($mode)."
  if app_has_variables "$id" "$environment"; then
    if has_store; then say "Its variables are in the store: scripts/ygg.sh vars list $id@$environment"; else say "Its env file exists: $secrets/$environment/$id.env"; fi
  elif create_env_file "$id" "$environment"; then
    if confirm "Edit it now?" y; then
      if has_store; then vars_py edit "$id@$environment"; else "${EDITOR:-nano}" "$secrets/$environment/$id.env"; fi
    fi
  fi

  if [[ "$mode" == proxy ]] && ! docker network inspect edge >/dev/null 2>&1; then
    note "The platform isn't up on this host (no edge network): deploy after scripts/platform.sh up."
  elif app_has_variables "$id" "$environment" && docker_ok && confirm "Clone the repository and deploy $id to $environment now?"; then
    if clone "$id" "$repository"; then deploy "$id" "$environment" || true; fi
  fi

  title "Next"
  say "1. Publish it: Jenkins and every host read the main branch of this repository."
  say "     git add catalog.yaml stacks/$id.* && git commit -m \"feat: add $id\"   (then merge it into main)"
  say "2. In the $repository repository: the templates/application/ files and CI (docs/setup.md step 5)."
  say "3. Install the GitHub App on $repository (step 7.5), then: python3 github/rulesets.py $repository"
  say "4. On every host: cd /opt/yggdrasil && git pull && docker restart yggdrasil-status-1"
  say "   (and yggdrasil-jenkins-1 on the controller host), so they see the new catalog."
  say "5. In every other environment it deploys to, on this host or another: scripts/ygg.sh config $id <environment>,"
  say "   to create its env file there."
}

# ---- What runs ----------------------------------------------------------------------------------

# One application's row in one environment: its containers' state, health and the deploy labels.
# app_row <app> <environment> <on demand: true|false>. A stopped application of an on-demand
# environment is normal, so it is not shown in red.
app_row() {
  local id=$1 environment=$2 on_demand=$3 containers state health version commit deployed total running unhealthy line
  mapfile -t containers < <(docker ps -a --filter "label=com.docker.compose.project=$id-$environment" --format '{{.ID}}')
  if ((${#containers[@]} == 0)); then
    printf '  %-22s %s\n' "$id" "${dim}not deployed${reset}"
    return
  fi
  total=${#containers[@]} running=0 unhealthy=""
  health="-"
  while IFS='|' read -r state line; do
    [[ "$state" == running ]] && running=$((running + 1))
    case $line in
      unhealthy) unhealthy=1 health=unhealthy ;;
      starting) [[ -z "$unhealthy" ]] && health=starting ;;
      healthy) [[ "$health" == - ]] && health=healthy ;;
    esac
  done < <(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${containers[@]}")
  IFS='|' read -r version commit deployed < <(docker inspect --format \
    '{{index .Config.Labels "yggdrasil.version"}}|{{index .Config.Labels "yggdrasil.commit"}}|{{index .Config.Labels "yggdrasil.deployed_at"}}' \
    "${containers[0]}" | sed 's/<no value>//g')
  if ((running == total)) && [[ "$health" != unhealthy ]]; then
    printf -v state '%s%-15s%s' "$green" "running $running/$total" "$reset"
  elif ((running > 0)); then
    printf -v state '%s%-15s%s' "$yellow" "running $running/$total" "$reset"
  elif [[ "$on_demand" == true ]]; then
    printf -v state '%s%-15s%s' "$dim" "stopped" "$reset"
  else
    printf -v state '%s%-15s%s' "$red" "stopped" "$reset"
  fi
  printf '  %-22s %s %-10s %-16s %-8s %s\n' "$id" "$state" "$health" "${version:--}" "${commit:--}" "${deployed:--}"
}

# environment_title <environment>: its name, id, and whether it runs on demand.
environment_title() {
  local name on_demand
  name=$(catalog environment "$1" name)
  on_demand=$(catalog environment "$1" onDemand)
  title "$name ($1)$([[ "$on_demand" == true ]] && echo ", on demand") on $(hostname)"
}

show_status() {
  need_catalog
  need_docker
  host_environments
  local environment apps pick id containers choices=()
  while true; do
    choices=()
    for environment in "${host_envs[@]}"; do
      environment_title "$environment"
      mapfile -t apps < <(applications_in "$environment")
      printf '  %s%-22s %-15s %-10s %-16s %-8s %s%s\n' "$bold" APPLICATION STATE HEALTH VERSION COMMIT DEPLOYED "$reset"
      for id in "${apps[@]}"; do
        app_row "$id" "$environment" "$(catalog get "$id" "$environment" onDemand)"
        choices+=("$id in $environment")
      done
      ((${#apps[@]})) || say "  (no application of the catalog deploys to $environment)"
    done
    local platform
    platform=$(docker ps --filter label=com.docker.compose.project=yggdrasil --format '{{.Names}}' | wc -l)
    say ""
    note "  Platform: $platform containers running (scripts/platform.sh ps for details)."
    say ""
    choose pick "Then:" "Refresh" "Show an application's logs" "Restart an application" \
      "Start or stop an environment" "Platform services" "Back"
    case $pick in
      Refresh) ;;
      Show*|Restart*)
        ((${#choices[@]})) || continue
        choose id "Which application?" "${choices[@]}"
        environment=${id##* in } id=${id%% in *}
        mapfile -t containers < <(docker ps -a --filter "label=com.docker.compose.project=$id-$environment" --format '{{.Names}}')
        if ((${#containers[@]} == 0)); then
          warn "$id is not deployed in $environment here."
        elif [[ "$pick" == Show* ]]; then
          for id in "${containers[@]}"; do title "$id"; docker logs --tail 100 "$id" 2>&1 || true; done
          pause
        else
          docker restart "${containers[@]}"
        fi
        ;;
      Start*) run environment_command; pause ;;
      Platform*) "$root/scripts/platform.sh" ps || true; pause ;;
      Back) return ;;
    esac
  done
}

# ---- Environments -------------------------------------------------------------------------------

# The containers of an application in an environment: all of them, or only the running ones.
# project_containers <app> <environment> [running]
project_containers() {
  local filter=(--filter "label=com.docker.compose.project=$1-$2")
  if [[ -n "${3:-}" ]]; then
    docker ps "${filter[@]}" --format '{{.ID}}'
  else
    docker ps -a "${filter[@]}" --format '{{.ID}}'
  fi
}

# Every application of the environment, as deploy.sh left it: started with docker start, no build,
# no new container.
start_environment() {
  local environment=$1 id containers count=0
  title "Starting $environment"
  for id in $(applications_in "$environment"); do
    mapfile -t containers < <(project_containers "$id" "$environment")
    if ((${#containers[@]} == 0)); then
      note "  $id: not deployed in $environment"
      continue
    fi
    docker start "${containers[@]}" >/dev/null
    say "  $id: started"
    count=$((count + 1))
  done
  ((count)) || { warn "Nothing of $environment is deployed on this host."; return 0; }
  note "Their health checks take a moment: scripts/ygg.sh status (or env status) shows when they are up."
}

# stop_environment <environment> <force: yes|"">: the applications that are onDemand there (every
# one with force).
stop_environment() {
  local environment=$1 force=$2 id containers count=0
  if [[ "$(catalog environment "$environment" onDemand)" != true && -z "$force" ]]; then
    die "$environment is not on demand (onDemand in catalog.yaml): it is meant to stay up. Add --force to stop it anyway."
  fi
  title "Stopping $environment"
  for id in $(applications_in "$environment"); do
    if [[ -z "$force" && "$(catalog get "$id" "$environment" onDemand)" != true ]]; then
      note "  $id: not on demand in $environment (its override in catalog.yaml): left as it is"
      continue
    fi
    mapfile -t containers < <(project_containers "$id" "$environment" running)
    ((${#containers[@]})) || continue
    docker stop "${containers[@]}" >/dev/null
    say "  $id: stopped"
    count=$((count + 1))
  done
  ((count)) || say "Nothing of $environment was running."
  return 0
}

# One line per environment of the host: on demand or not, and how many of its applications run.
environments_status() {
  local environment id total running deployed state on_demand
  title "Environments on $(hostname)"
  printf '  %s%-16s %-20s %-10s %s%s\n' "$bold" ENVIRONMENT NAME "ON DEMAND" STATE "$reset"
  for environment in "${host_envs[@]}"; do
    total=0 running=0 deployed=0
    for id in $(applications_in "$environment"); do
      total=$((total + 1))
      [[ -n "$(project_containers "$id" "$environment")" ]] && deployed=$((deployed + 1))
      [[ -n "$(project_containers "$id" "$environment" running)" ]] && running=$((running + 1))
    done
    on_demand=$(catalog environment "$environment" onDemand)
    if ((deployed == 0)); then
      state="${dim}not deployed${reset}"
    elif ((running == 0)); then
      state="$([[ "$on_demand" == true ]] && echo "$dim" || echo "$red")stopped${reset}"
    elif ((running == deployed)); then
      state="${green}running${reset} ($running of $total applications)"
    else
      state="${yellow}partly running${reset} ($running of $deployed deployed)"
    fi
    printf '  %-16s %-20s %-10s %s\n' "$environment" "$(catalog environment "$environment" name)" \
      "$([[ "$on_demand" == true ]] && echo yes || echo no)" "$state"
  done
}

# env [start|stop|status] [<environment>] [--force]: without an action, asks.
environment_command() {
  need_catalog
  need_docker
  host_environments
  local action="" environment="" force="" argument
  for argument in "$@"; do
    case $argument in
      --force) force=yes ;;
      start | stop | status) [[ -z "$action" ]] && action=$argument || environment=$argument ;;
      *) [[ -z "$environment" ]] || die "usage: scripts/ygg.sh env start <environment> | stop <environment> [--force] | status"
         environment=$argument ;;
    esac
  done
  if [[ -z "$action" ]]; then
    environments_status
    say ""
    choose action "Then:" start stop back
    [[ "$action" == back ]] && return 0
  fi
  case $action in
    status) environments_status ;;
    start)
      pick_environment environment "Start which environment?" "$environment"
      start_environment "$environment"
      ;;
    stop)
      pick_environment environment "Stop which environment?" "$environment"
      if [[ -z "$force" && "$(catalog environment "$environment" onDemand)" != true && -t 0 && $# -eq 0 ]]; then
        warn "$environment is not on demand: it is meant to stay up."
        confirm "Stop every application of $environment anyway?" && force=yes || return 0
      fi
      stop_environment "$environment" "$force"
      ;;
  esac
}

# ---- Configuration ------------------------------------------------------------------------------

# Secrets are named by their last word: DB_PASSWORD, JENKINS_AGENT_SECRET, CF_DNS_API_TOKEN, API_KEY.
# Only the last one, so HEIMDALL_PASSWORD_RESET_URL, a URL, is not one -- but the retired key kept
# during a rotation (HEIMDALL_AUTH_TOKEN_SECRET_PREVIOUS) is, and so is a connection string, which
# carries the database password (FORTUNA_DATA_CONNECTIONSTRING).
SECRET_NAME='(^|_)(PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN|KEY|CREDENTIALS?)(_PREVIOUS)?$|(^|_)CONNECTION_?STRING$'

# show_env <file> <reveal: yes|"">: its variables, secrets masked unless revealed.
show_env() {
  local line name
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# || -z "${line// }" || "$line" != *=* ]] && continue
    name=${line%%=*}
    if [[ -z "$2" && "${name^^}" =~ $SECRET_NAME && -n "${line#*=}" ]]; then
      printf '  %s=%s\n' "$name" "********"
    else
      printf '  %s\n' "$line"
    fi
  done <"$1"
}

# set_env <file> <name> <value>: replaces NAME='s line, or appends one. Written in place, so the
# file keeps its owner, group and mode.
set_env() {
  local temporary
  temporary=$(mktemp)
  NAME=$2 VALUE=$3 awk '
    BEGIN { name = ENVIRON["NAME"]; value = ENVIRON["VALUE"] }
    index($0, name "=") == 1 { if (!done) print name "=" value; done = 1; next }
    { print }
    END { if (!done) print name "=" value }
  ' "$1" >"$temporary"
  cat "$temporary" >"$1"
  rm -f "$temporary"
}

unset_env() {
  local temporary
  temporary=$(mktemp)
  NAME=$2 awk 'index($0, ENVIRON["NAME"] "=") != 1' "$1" >"$temporary"
  cat "$temporary" >"$1"
  rm -f "$temporary"
}

# A value as Compose reads it back unchanged: single quotes when it has spaces, #, $ or quotes.
quote_value() {
  if [[ "$1" =~ [[:space:]\#\$\"\\\`] ]]; then
    printf "'%s'" "$1"
  else
    printf '%s' "$1"
  fi
}

configure_app() {
  need_catalog
  host_environments
  local id=${1:-} environment=${2:-} apps=() file pick name value reveal="" changed="" candidate
  if [[ -z "$id" ]]; then
    # Every application of the host's environments, each once.
    for candidate in "${host_envs[@]}"; do
      mapfile -t -O "${#apps[@]}" apps < <(applications_in "$candidate")
    done
    mapfile -t apps < <(printf '%s\n' "${apps[@]}" | awk 'NF && !seen[$0]++')
    ((${#apps[@]})) || die "no application of the catalog deploys to this host's environments ($(IFS=,; echo "${host_envs[*]}"))"
    choose id "Which application?" "${apps[@]}"
  fi
  catalog show "$id" >/dev/null || exit 1
  pick_environment environment "$id in which environment?" "$environment" "$id"
  file="$secrets/$environment/$id.env"
  need_store
  if ! app_has_variables "$id" "$environment"; then
    if has_store; then
      say "$id has no variables in $environment in the variables store yet."
    else
      say "$id has no env file in $environment on this host yet ($file)."
    fi
    confirm "Create it from its stack files?" y || return 0
    create_env_file "$id" "$environment" || return 1
  fi
  has_store || [[ -r "$file" && -w "$file" ]] || die "$file is not readable and writable by $me"

  while true; do
    if has_store; then
      title "$id in $environment (variables store)"
      vars_py list "$id@$environment" --resolved ${reveal:+--reveal}
      say ""
      choose pick "Then:" \
        "Set a variable" "Remove a variable" "Edit in ${EDITOR:-nano}" \
        "$([[ -n "$reveal" ]] && echo "Hide secret values" || echo "Show secret values")" \
        "History" "Apply: redeploy $id${changed:+ (changed)}" "Back"
      case $pick in
        Set*)
          ask_match name "Variable name" '^[A-Za-z_][A-Za-z0-9_]*$' "letters, digits and underscores"
          if [[ "${name^^}" =~ $SECRET_NAME ]]; then
            vars_py set "$id@$environment" "$name=-"
          else
            ask value "Value" "$(vars_py get "$id@$environment" "$name" 2>/dev/null || true)"
            vars_py set "$id@$environment" "$name=$value"
          fi
          changed=1
          ;;
        Remove*)
          local names
          mapfile -t names < <(vars_py list "$id@$environment" --keys)
          ((${#names[@]})) || continue
          choose name "Which variable?" "${names[@]}"
          vars_py unset "$id@$environment" "$name"
          changed=1
          ;;
        Edit*) vars_py edit "$id@$environment" ${reveal:+--reveal}; changed=1 ;;
        Show*) reveal=yes ;;
        Hide*) reveal="" ;;
        History) vars_py history "$id@$environment" --limit 20; pause ;;
        Apply*)
          if deploy "$id" "$environment"; then changed=""; fi
          pause
          ;;
        Back)
          [[ -n "$changed" ]] && warn "Changes reach $id on its next deploy (Apply, Jenkins, or scripts/deploy.sh)."
          return 0
          ;;
      esac
      continue
    fi
    title "$id in $environment: $file"
    show_env "$file" "$reveal"
    say ""
    choose pick "Then:" \
      "Set a variable" "Remove a variable" "Edit the file in ${EDITOR:-nano}" \
      "$([[ -n "$reveal" ]] && echo "Hide secret values" || echo "Show secret values")" \
      "Apply: redeploy $id${changed:+ (changed)}" "Back"
    case $pick in
      Set*)
        ask_match name "Variable name" '^[A-Za-z_][A-Za-z0-9_]*$' "letters, digits and underscores"
        if [[ "${name^^}" =~ $SECRET_NAME ]]; then
          read -r -s -p "Value (hidden): " value || exit 1
          say ""
        else
          ask value "Value" "$(env_value "$file" "$name")"
        fi
        if [[ "$value" == *"'"* ]]; then
          warn "Values with a single quote can't be written safely here: use 'Edit the file'."
          continue
        fi
        set_env "$file" "$name" "$(quote_value "$value")"
        changed=1
        ;;
      Remove*)
        local names
        mapfile -t names < <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$file" | tr -d '=')
        ((${#names[@]})) || continue
        choose name "Which variable?" "${names[@]}"
        unset_env "$file" "$name"
        changed=1
        ;;
      Edit*) "${EDITOR:-nano}" "$file"; changed=1 ;;
      Show*) reveal=yes ;;
      Hide*) reveal="" ;;
      Apply*)
        note "Containers read their env file when they are created: deploy.sh recreates them (and rebuilds the image, which a web front end's build arguments need)."
        if deploy "$id" "$environment"; then changed=""; fi
        pause
        ;;
      Back)
        [[ -n "$changed" ]] && warn "Changes reach $id on its next deploy (Apply, Jenkins, or scripts/deploy.sh)."
        return 0
        ;;
    esac
  done
}

# ---- Menu ---------------------------------------------------------------------------------------

variables_menu() {
  if ! has_store; then
    say "This machine keeps its variables in env files under $secrets."
    if confirm "Create the variables store and import them now?" n; then
      vars_py init
      vars_py import --all
    fi
    return 0
  fi
  local pick scope
  choose pick "Variables and secrets:" "List a scope" "Set a variable" "Edit a scope in ${EDITOR:-nano}" "History" "Roll a change back" "Check the store" "Back up the store" "Back"
  case $pick in
    List*) ask scope "Scope (platform, @<environment>, <application>, <application>@<environment>)" "platform"; vars_py list "$scope" ;;
    Set*) ask scope "Scope" "platform"; ask_match pick "KEY=value (KEY=- to type a hidden value)" '^[A-Za-z_][A-Za-z0-9_]*=' "KEY=value"; vars_py set "$scope" "$pick" ;;
    Edit*) ask scope "Scope" "platform"; vars_py edit "$scope" ;;
    History) vars_py history --limit 30 ;;
    Roll*) ask_match pick "Change id (from History)" '^[0-9]+$' "a number"; vars_py rollback "$pick" ;;
    Check*) vars_py check ;;
    Back\ up*) ask scope "Into which directory?" "/root/yggdrasil-backups"; vars_py backup "$scope" ;;
    Back) return 0 ;;
  esac
  pause
}

# Runs one action so that a failure (die, or a failing command under set -e) ends the action, not
# the menu.
run() {
  local status
  set +e
  (
    set -e
    "$@"
  )
  status=$?
  set -e
  ((status == 0)) || warn "(stopped)"
  return 0
}

menu() {
  local pick
  while true; do
    title "yggdrasil on $(hostname)"
    note "Repository $root, secrets $secrets"
    choose pick "What do you want to do?" \
      "Check the tools and this host" \
      "Install what is missing" \
      "Set up a new application (API, web front end or worker)" \
      "See what runs on this host" \
      "Change an application's configuration" \
      "Start or stop an environment (on demand)" \
      "Variables and secrets" \
      "Quit"
    case $pick in
      Check*) run check_host; pause ;;
      Install*) run install_host; pause ;;
      Set*) run add_application; pause ;;
      See*) run show_status ;;
      Change*) run configure_app ;;
      Start*) run environment_command; pause ;;
      Variables*) run variables_menu ;;
      Quit) return ;;
    esac
  done
}

case ${1:-} in
  "") menu ;;
  check) check_host ;;
  install) install_host ;;
  add) add_application ;;
  status) show_status ;;
  config) configure_app "${2:-}" "${3:-}" ;;
  env) shift; environment_command "$@" ;;
  vars) shift; vars_py "$@" ;;
  -h | --help | help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1': scripts/ygg.sh [check | install | add | status | config [<app>] [<environment>] | env status | env start <environment> | env stop <environment> [--force] | vars <vars.py command>]" ;;
esac
