#!/usr/bin/env bash
# Starts, updates or stops this host's platform stack (platform/compose.yml).
#
#     scripts/platform.sh up | down | ps | config [--last-good]
#     scripts/platform.sh logs [--last-good] [service]
#
# Runs on the invoking user's own rootless Docker engine and nothing else: the socket comes from
# DOCKER_HOST, else the user's runtime directory, and a rootful engine is refused. One platform
# stack per host, whatever number of environments the host runs (ENVIRONMENTS). Reads
# the settings from the variables store (rendered, with a last-good copy kept; --last-good, right
# after the command, reads that copy instead), or from <secrets>/platform.env and acme.env on a host
# without a store, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil). Template:
# env/platform.env.example.
set -euo pipefail

die() { echo "platform: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless and use_rootless_engine, shared with agent.sh.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}
usage="usage: platform.sh up | down | ps | config [--last-good], or platform.sh logs [--last-good] [service]"
command=${1:-}
(($#)) && shift
last_good=""
if [[ "${1:-}" == --last-good ]]; then
  last_good=1
  shift
fi
[[ "$command" =~ ^(up|down|ps|logs|config)$ ]] || die "$usage"
[[ "$command" == logs || $# -eq 0 ]] || die "$usage"
cleanup_paths=()
trap 'rm -rf ${cleanup_paths[@]+"${cleanup_paths[@]}"}' EXIT

# A private directory for the env files this run renders from the store: /run/yggdrasil for root;
# else the user's own runtime directory (a tmpfs only they read), else a directory of theirs under
# $TMPDIR. Removed on exit: Compose reads them only while this runs. (Traefik's traefik.yml is not
# here: see render_traefik_config.) Sets render_dir.
run_dir() {
  local run_base=/run/yggdrasil
  if ! { mkdir -p "$run_base" 2>/dev/null && [[ -w "$run_base" ]]; }; then
    if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" && -w "$XDG_RUNTIME_DIR" ]]; then
      run_base="$XDG_RUNTIME_DIR/yggdrasil"
    else
      run_base="${TMPDIR:-/tmp}/yggdrasil-$(id -u)"
    fi
  fi
  mkdir -p "$run_base" && chmod 700 "$run_base"
  render_dir=$(mktemp -d "$run_base/platform.XXXXXX")
  cleanup_paths+=("$render_dir")
}

# The platform's settings: rendered from the variables store when this host has one
# (docs/variables.md), else platform.env and acme.env. --last-good starts from the copy the last
# successful `up` saved, without opening the store: for when the store or its key is unusable, with
# any command (down, ps, logs and config need the settings too, to read the project).
render_dir=""
from_store=""
if [[ -n "$last_good" ]]; then
  env_file="$secrets/last-good/platform.env"
  acme_file="$secrets/last-good/acme.env"
  traefik_file="$secrets/last-good/traefik.yml"
  [[ -f "$env_file" ]] || die "no last-good copy in $secrets/last-good (one is saved by every successful 'platform.sh up' from the store)"
  [[ -f "$traefik_file" ]] || die "the last-good copy in $secrets/last-good has no traefik.yml (saved by a 'platform.sh up' from before Traefik's config was a file); run 'platform.sh up' once from the store"
  echo "platform: starting from the last-good copy in $secrets/last-good, not the variables store" >&2
elif [[ -f "$secrets/vars.db" ]]; then
  # Only the platform's own values: an application's broken reference is its deploy's problem.
  python3 "$root/scripts/vars.py" check --platform >&2 \
    || die "the variables store failed its check; fix it (scripts/ygg.sh vars check) or run 'platform.sh $command --last-good'"
  run_dir
  from_store=1
  env_file="$render_dir/platform.env"
  acme_file="$render_dir/acme.env"
  (umask 077 && python3 "$root/scripts/vars.py" render-platform >"$env_file" \
    && python3 "$root/scripts/vars.py" render-platform --acme >"$acme_file") \
    || die "could not render the platform's variables; 'platform.sh $command --last-good' reads the last copy that worked"
else
  env_file="$secrets/platform.env"
  acme_file="$secrets/acme.env"
  [[ -f "$env_file" ]] || die "missing $env_file (template: env/platform.env.example)"
  echo "platform: reading $env_file; move to the variables store with scripts/ygg.sh vars init && scripts/ygg.sh vars import --all" >&2
fi
export YGG_ACME_ENV_FILE="$acme_file"

# Traefik's static configuration (traefik.yml), rendered from the settings: a file because the list
# of domains it serves cannot be written as flags. Compose requires it for every command (it
# interpolates the whole file), so every command renders one -- except --last-good, which reuses the
# copy kept with the rest. Cloudflare's ranges (<secrets>/cloudflare-ips) are read only when
# CLOUDFLARE_PROXIED=true.
#
# It lives in <secrets>/traefik.yml, not in the run directory: Compose bind-mounts it into Traefik,
# and the engine mounts it again at every start of the container (a crash, `docker restart`, the
# engine's own restart, every boot), long after this script and its run directory are gone. Mode 600
# like the rest of the directory: Traefik runs as container root, which is this user. Written to a
# temporary file next to it and renamed over it, so a failed render never leaves half a file there,
# and the running Traefik keeps the one it started with (a bind mount holds on to the file it was
# given) until Compose recreates it.
render_traefik_config() {
  traefik_file="$secrets/traefik.yml"
  local options=() partial
  [[ -f "$secrets/cloudflare-ips" ]] && options=(--cloudflare-ips "$secrets/cloudflare-ips")
  partial=$(mktemp "$secrets/.traefik.yml.XXXXXX") || die "cannot write Traefik's configuration in $secrets (is it yours? run platform.sh as ygg)"
  cleanup_paths+=("$partial")
  # The settings go in the renderer's environment, from the same file Compose reads.
  # shellcheck source=/dev/null
  (set -a; . "$env_file"; python3 "$root/scripts/traefik_config.py" ${options[@]+"${options[@]}"} >"$partial") \
    || die "could not render Traefik's configuration from $env_file (see above)"
  chmod 600 "$partial"
  mv -f "$partial" "$traefik_file"
}
[[ -n "$last_good" ]] || render_traefik_config
export YGG_TRAEFIK_CONFIG_FILE="$traefik_file"
# The path is the same at every run, so Compose would not see a changed configuration: its hash is a
# label on the Traefik service (compose.yml), and a new hash is what makes `up` recreate Traefik.
YGG_TRAEFIK_CONFIG_HASH=$(sha256sum "$traefik_file" | cut -d' ' -f1) || die "cannot read $traefik_file"
export YGG_TRAEFIK_CONFIG_HASH

compose() { docker compose --env-file "$env_file" -f "$root/platform/compose.yml" "$@"; }

# COMPOSE_PROFILES of the settings as ",a,b,", so that a profile is found as ",name,".
# shellcheck source=/dev/null
profiles=$(set -a; . "$env_file"; echo ",${COMPOSE_PROFILES:-},")

# A host without the status API (no apps profile: the dev machine) has nothing to discover targets
# from, so Prometheus reads the platform's own, written out.
if [[ "$profiles" == *,apps,* ]]; then
  PROMETHEUS_CONFIG_FILE=prometheus.yml
else
  PROMETHEUS_CONFIG_FILE=prometheus-static.yml
fi
export PROMETHEUS_CONFIG_FILE

# Jenkins needs them and the catalog has them: no second copy in platform.env to drift.
GITHUB_OWNER=$(python3 "$root/scripts/catalog.py" owner) || exit 1
YGGDRASIL_REPOSITORY=$(python3 "$root/scripts/catalog.py" repository) || exit 1
export GITHUB_OWNER YGGDRASIL_REPOSITORY

# The profile-only variables compose.yml cannot mark as required (see the note at its top). The forge
# profile has none: Forgejo needs only DOMAIN, and FORGEJO_SSH_PORT defaults to 2222.
check_profile_variables() {
  local required=()
  [[ "$profiles" == *,jenkins,* ]] && required+=(JENKINS_URL JENKINS_ADMIN_PASSWORD GITHUB_APP_ID GITHUB_APP_KEY_FILE)
  [[ "$profiles" == *,agent,* ]] && required+=(JENKINS_URL JENKINS_AGENT_NAME JENKINS_AGENT_SECRET)
  local name value
  for name in "${required[@]}"; do
    # shellcheck source=/dev/null
    value=$(set -a; . "$env_file"; eval "echo \"\${$name:-}\"")
    [[ -n "$value" ]] || die "$name must be set in $env_file for profiles '${profiles//,/ }'"
  done
}

# ENVIRONMENTS names the environments this host runs, comma-separated: each must be one in
# catalog.yaml. A platform.env from before 0.5 has only ENVIRONMENT, which counts as a list of one;
# ENVIRONMENTS wins when both are set.
check_environments() {
  local environments known environment
  # shellcheck source=/dev/null
  environments=$(set -a; . "$env_file"; echo "${ENVIRONMENTS:-${ENVIRONMENT:-}}")
  [[ -n "${environments//[ ,]/}" ]] \
    || die "ENVIRONMENTS must be set in $env_file: the environments this host runs, e.g. development,homologation,production"
  known=$(python3 "$root/scripts/catalog.py" environments) || exit 1
  IFS=', ' read -r -a environments <<<"$environments"
  for environment in "${environments[@]}"; do
    [[ -n "$environment" ]] || continue
    grep -Fqx "$environment" <<<"$known" \
      || die "'$environment' (ENVIRONMENTS in $env_file) is not an environment in catalog.yaml ($(paste -sd, - <<<"$known"))"
  done
}

# The environments are the apps profile's business (the status API and the console show them): checked
# when it is on, and an ENVIRONMENTS without it is a platform.env from before the profile existed.
check_apps_profile() {
  if [[ "$profiles" == *,apps,* ]]; then
    check_environments
    return
  fi
  local environments
  # shellcheck source=/dev/null
  environments=$(set -a; . "$env_file"; echo "${ENVIRONMENTS:-${ENVIRONMENT:-}}")
  [[ -z "${environments//[ ,]/}" ]] \
    || die "ENVIRONMENTS is set but COMPOSE_PROFILES lacks apps: add apps to COMPOSE_PROFILES (the status API and console moved to that profile in this release)"
}

case "$command" in
  up)
    check_apps_profile
    check_profile_variables
    use_rootless_engine
    for network in edge telemetry; do
      docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null
    done
    # The agent's deploy locks (scripts/deploy.sh): the agent's container root is this user.
    install -d -m 700 "$secrets/locks"
    compose up --detach --build --remove-orphans --wait
    compose ps
    if [[ -n "$from_store" ]]; then
      install -d -m 700 "$secrets/last-good"
      install -m 600 "$env_file" "$acme_file" "$traefik_file" "$secrets/last-good/"
    fi
    ;;
  down) use_rootless_engine; compose down ;;
  ps) use_rootless_engine; compose ps ;;
  logs) use_rootless_engine; compose logs --follow --tail 200 "$@" ;;
  config) use_rootless_engine; compose config ;;
esac
