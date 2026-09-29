#!/usr/bin/env bash
# yggdrasil's host helper: one menu for setting up an Ubuntu host and the applications it runs.
#
#     scripts/ygg.sh                    the menu
#     scripts/ygg.sh check              which tools and host pieces are there, which are missing
#     scripts/ygg.sh install            installs what is missing (Ubuntu; asks before each part)
#     scripts/ygg.sh add                sets up a new API, web front end or worker: catalog entry,
#                                       stack files, env file, checkout, first deploy
#     scripts/ygg.sh status             what runs on this host, per catalog application
#     scripts/ygg.sh config [<app>]     shows and changes an application's env file, and redeploys
#
# It drives the same pieces docs/setup.md does by hand -- scripts/catalog.py, scripts/deploy.sh,
# scripts/platform.sh, the stacks/ files and the env files under $YGG_SECRETS_DIR (default
# /etc/yggdrasil) -- so anything it does can also be done, or undone, by hand. Checkouts of the
# applications it deploys go under $YGG_APPS_DIR (default ~/yggdrasil-apps). $YGG_ENVIRONMENT names
# this host's environment where platform.env doesn't (a laptop). Reference: docs/cli.md.
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
DEPLOY_VARIABLES='^(APP_DIR|APP_ENV_FILE|IMAGE_TAG|API_IMAGE_TAG)$'

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

pause() { [[ -t 0 ]] && read -r -p "${dim}Enter to continue${reset} " _ || true; }

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

# The environment this host runs: $YGG_ENVIRONMENT, else ENVIRONMENT in platform.env, else asked
# once. A `ports` machine (a developer laptop) has no platform, so it has no platform.env to say.
host_environment() {
  [[ -z "$host_env" ]] && host_env=$(env_value "$secrets/platform.env" ENVIRONMENT)
  if [[ -z "$host_env" ]]; then
    local environments
    mapfile -t environments < <(catalog environments)
    choose host_env "Which environment does this host run? ($secrets/platform.env doesn't say)" "${environments[@]}"
  fi
  catalog environments | grep -qx "$host_env" \
    || die "ENVIRONMENT='$host_env' in $secrets/platform.env is not an environment in catalog.yaml"
}

# The applications of the catalog that deploy to an environment.
applications_in() {
  local app
  for app in $(catalog applications --deployable); do
    catalog environments "$app" | grep -qx "$1" && echo "$app"
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
    for file in platform.env acme.env; do
      if [[ -f "$secrets/$file" ]]; then
        row ok "$file" "$secrets/$file"
      else
        row missing "$file" "not needed on a ports-only machine; install copies the template"
        need_secrets=1
      fi
    done
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
    local environment
    environment=$(env_value "$secrets/platform.env" ENVIRONMENT)
    if [[ -n "$environment" ]]; then
      row ok "environment" "$environment (platform.env)"
    else
      row warn "environment" "platform.env sets no ENVIRONMENT: the menu asks for it"
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
  say "# On the edge network, the alias $id is how the status API reaches its health check (catalog.yaml)."
  [[ -n "$metrics" ]] && say "# On the telemetry network, Prometheus scrapes its metrics."
  [[ -n "$host" ]] && say "# Routed at PUBLIC_HOST from the env file: the catalog's host, $host, under the DOMAIN."
  printf '\nservices:\n  %s:\n' "$service"
  [[ -n "$own" ]] && say "    ports: !reset []"
  say "    networks:"
  # The repository's own Compose file may run other services (a database) on the default network.
  [[ -n "$own" ]] && say "      default: {}"
  printf '      edge:\n        aliases: [%s]\n' "$id"
  [[ -n "$metrics" ]] && printf '      telemetry:\n        aliases: [%s]\n' "$id"
  if [[ -n "$own" ]]; then
    printf '    logging:\n      driver: json-file\n      options:\n        max-size: 10m\n        max-file: "5"\n'
  fi
  if [[ -n "$host" ]]; then
    cat <<EOF
    labels:
      traefik.enable: "true"
      traefik.docker.network: edge
      traefik.http.routers.$id.rule: Host(\`\${PUBLIC_HOST:?set PUBLIC_HOST in the env file}\`)
      traefik.http.routers.$id.entrypoints: websecure
      traefik.http.routers.$id.middlewares: secure-headers@file
      traefik.http.services.$id.loadbalancer.server.port: "$port"
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

# create_env_file <app> <environment>: the application's env file on this host, with the variables
# its stack files read. The ones deploy.sh sets itself are left out.
create_env_file() {
  local id=$1 environment=$2 dir="$secrets/$2" file="$secrets/$2/$1.env"
  local mode host domain="" name default files
  if [[ ! -w "$secrets" ]]; then
    warn "Can't write to $secrets: create it with '$0 install' (or set YGG_SECRETS_DIR), then run '$0 config $id'."
    return 1
  fi
  mode=$(catalog get "$id" "$environment" mode)
  host=$(app_field "$id" host)
  mapfile -t files < <(stack_files "$id" "$environment")
  if [[ "$mode" == proxy && -n "$host" ]]; then
    domain=$(env_value "$secrets/platform.env" DOMAIN)
    [[ -n "$domain" && "$domain" != example.com ]] || ask domain "This environment's DOMAIN (platform.env doesn't say)" "example.com"
  fi
  [[ -d "$dir" ]] || install -d -m 2750 "$dir"
  {
    say "# $id in $environment. Created by scripts/ygg.sh; change it with: scripts/ygg.sh config $id"
    say "# It fills in the \${VAR}s of the Compose files, and reaches the container where they hand it"
    say "# over (env_file, or environment: entries). Never commit it."
    if [[ -n "$domain" ]]; then
      say ""
      say "# The host name Traefik routes to it: the catalog's host under this environment's DOMAIN."
      say "PUBLIC_HOST=$host.$domain"
    fi
    if ((${#files[@]})); then
      while read -r name; do
        [[ "$name" =~ $DEPLOY_VARIABLES || "$name" == PUBLIC_HOST ]] && continue
        default=$(grep -ohE "[$][{]${name}:-[^}]*" "${files[@]}" | head -n1 | sed "s/^[$][{]${name}:-//")
        say ""
        say "# Read by $(grep -lE "[$][{]${name}[:}]" "${files[@]}" | sed "s|^$root/||" | paste -sd, -)"
        say "$name=$default"
      done < <(grep -ohE '[$][{][A-Za-z_][A-Za-z0-9_]*' "${files[@]}" | cut -c3- | sort -u)
    fi
  } >"$file"
  chmod 640 "$file"
  say "Wrote $file"
}

# ---- Deploy -------------------------------------------------------------------------------------

# What a checkout would be labelled by default: <latest tag>-<commit>, as Jenkins labels releases.
checkout_version() {
  local commit tag
  commit=$(git -C "$1" rev-parse --short=7 HEAD)
  tag=$(git -C "$1" describe --tags --abbrev=0 2>/dev/null || echo dev)
  echo "${tag#v}-$commit"
}

# The image tag of what runs now, empty when nothing does.
running_tag() {
  local image
  image=$(docker ps --filter "label=com.docker.compose.project=$1" --format '{{.Image}}' | head -n1)
  [[ "$image" == *:* ]] && echo "${image##*:}"
  return 0
}

# deploy <app> <environment>: deploy.sh from the application's checkout.
deploy() {
  local id=$1 environment=$2 dir="$apps_dir/$1" version default running commit
  need_docker
  if [[ ! -d "$dir/.git" ]]; then
    ask_match dir "Checkout of $id's repository to build from (a directory)" '.' "a path" "$dir"
    dir=${dir/#\~/$HOME}
    [[ -d "$dir" ]] || die "no such directory: $dir"
  fi
  default=$(checkout_version "$dir")
  running=$(running_tag "$id")
  if [[ -n "$running" ]]; then
    commit=$(git -C "$dir" rev-parse --short=7 HEAD)
    if [[ "$running" == *"-$commit" || "$running" == "$commit" ]]; then
      default=$running
    else
      note "Running: $running. The checkout at $dir is at $commit: pull or check out what you mean to run."
    fi
  fi
  ask_match version "Version to label the image with" '^[A-Za-z0-9_.-]+$' "letters, digits, dots, dashes, underscores" "$default"
  if "$root/scripts/deploy.sh" "$environment" "$id" "$dir" "$version"; then
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
    ask_match id "Application id (lowercase, digits, dashes; the Compose project and network alias)" "$ID_PATTERN" "lowercase letters, digits and dashes"
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
      catalog systems | cut -f1 | grep -qx "$system" || break
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
  [[ -n "$host" ]] && say "  host       $host.<DOMAIN>"
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
  host_environment
  environment=$host_env
  mode=$(catalog get "$id" "$environment" mode)
  say "This host runs $environment ($mode)."
  if [[ -f "$secrets/$environment/$id.env" ]]; then
    say "Its env file exists: $secrets/$environment/$id.env"
  elif create_env_file "$id" "$environment"; then
    if confirm "Edit it now?" y; then "${EDITOR:-nano}" "$secrets/$environment/$id.env"; fi
  fi

  if [[ "$mode" == proxy ]] && ! docker network inspect edge >/dev/null 2>&1; then
    note "The platform isn't up on this host (no edge network): deploy after scripts/platform.sh up."
  elif [[ -f "$secrets/$environment/$id.env" ]] && docker_ok && confirm "Clone the repository and deploy $id to $environment now?"; then
    clone "$id" "$repository" && deploy "$id" "$environment" || true
  fi

  title "Next"
  say "1. Publish it: Jenkins and every host read the main branch of this repository."
  say "     git add catalog.yaml stacks/$id.* && git commit -m \"feat: add $id\"   (then merge it into main)"
  say "2. In the $repository repository: the templates/application/ files and CI (docs/setup.md step 5)."
  say "3. Install the GitHub App on $repository (step 7.5), then: python3 github/rulesets.py $repository"
  say "4. On every host: cd /opt/yggdrasil && git pull && docker restart yggdrasil-status-1"
  say "   (and yggdrasil-jenkins-1 on the controller host), so they see the new catalog."
  say "5. On every other host it deploys to: scripts/ygg.sh config $id, to create its env file there."
}

# ---- What runs ----------------------------------------------------------------------------------

# One application's row: its containers' state, health and the deploy labels.
app_row() {
  local id=$1 containers state health version commit deployed total running unhealthy line
  mapfile -t containers < <(docker ps -a --filter "label=com.docker.compose.project=$id" --format '{{.ID}}')
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
  else
    printf -v state '%s%-15s%s' "$red" "stopped" "$reset"
  fi
  printf '  %-22s %s %-10s %-16s %-8s %s\n' "$id" "$state" "$health" "${version:--}" "${commit:--}" "${deployed:--}"
}

show_status() {
  need_catalog
  need_docker
  host_environment
  local apps pick id containers
  mapfile -t apps < <(applications_in "$host_env")
  while true; do
    title "Applications in $host_env on $(hostname)"
    printf '  %s%-22s %-15s %-10s %-16s %-8s %s%s\n' "$bold" APPLICATION STATE HEALTH VERSION COMMIT DEPLOYED "$reset"
    for id in "${apps[@]}"; do app_row "$id"; done
    ((${#apps[@]})) || say "  (no application of the catalog deploys to $host_env)"
    local platform
    platform=$(docker ps --filter label=com.docker.compose.project=yggdrasil --format '{{.Names}}' | wc -l)
    note "  Platform: $platform containers running (scripts/platform.sh ps for details)."
    say ""
    choose pick "Then:" "Refresh" "Show an application's logs" "Restart an application" "Platform services" "Back"
    case $pick in
      Refresh) ;;
      Show*|Restart*)
        ((${#apps[@]})) || continue
        choose id "Which application?" "${apps[@]}"
        mapfile -t containers < <(docker ps -a --filter "label=com.docker.compose.project=$id" --format '{{.Names}}')
        if ((${#containers[@]} == 0)); then
          warn "$id is not deployed here."
        elif [[ "$pick" == Show* ]]; then
          for id in "${containers[@]}"; do title "$id"; docker logs --tail 100 "$id" 2>&1 || true; done
          pause
        else
          docker restart "${containers[@]}"
        fi
        ;;
      Platform*) "$root/scripts/platform.sh" ps || true; pause ;;
      Back) return ;;
    esac
  done
}

# ---- Configuration ------------------------------------------------------------------------------

# Secrets are named by their last word: DB_PASSWORD, JENKINS_AGENT_SECRET, CF_DNS_API_TOKEN, API_KEY.
# Only the last one, so HEIMDALL_PASSWORD_RESET_URL, a URL, is not one.
SECRET_NAME='(^|_)(PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN|KEY|CREDENTIALS?)$'

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
  host_environment
  local id=${1:-} apps file pick name value reveal="" changed=""
  mapfile -t apps < <(applications_in "$host_env")
  if [[ -z "$id" ]]; then
    ((${#apps[@]})) || die "no application of the catalog deploys to $host_env"
    choose id "Which application?" "${apps[@]}"
  fi
  printf '%s\n' "${apps[@]}" | grep -qx "$id" || die "'$id' does not deploy to $host_env (catalog.yaml)"
  file="$secrets/$host_env/$id.env"
  if [[ ! -f "$file" ]]; then
    say "$id has no env file on this host yet ($file)."
    confirm "Create it from its stack files?" y || return 0
    create_env_file "$id" "$host_env" || return 1
  fi
  [[ -r "$file" && -w "$file" ]] || die "$file is not readable and writable by $me"

  while true; do
    title "$id in $host_env: $file"
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
        deploy "$id" "$host_env" && changed="" || true
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
      "Quit"
    case $pick in
      Check*) run check_host; pause ;;
      Install*) run install_host; pause ;;
      Set*) run add_application; pause ;;
      See*) run show_status ;;
      Change*) run configure_app ;;
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
  config) configure_app "${2:-}" ;;
  -h | --help | help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1': scripts/ygg.sh [check | install | add | status | config [<app>]]" ;;
esac
