#!/usr/bin/env bash
# Starts, updates or stops this host's platform stack (platform/compose.yml).
#
#     scripts/platform.sh up | down | ps | config [--last-good]
#     scripts/platform.sh logs [--last-good] [service]
#
# One platform stack per host, whatever number of environments the host runs (ENVIRONMENTS). Reads
# the settings from the variables store (rendered, with a last-good copy kept; --last-good, right
# after the command, reads that copy instead), or from <secrets>/platform.env and acme.env on a host
# without a store, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil). Template:
# env/platform.env.example.
set -euo pipefail

die() { echo "platform: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
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

# The platform's settings: rendered from the variables store when this host has one
# (docs/variables.md), else platform.env and acme.env. --last-good starts from the copy the last
# successful `up` saved, without opening the store: for when the store or its key is unusable, with
# any command (down, ps, logs and config need the settings too, to read the project).
render_dir=""
if [[ -n "$last_good" ]]; then
  env_file="$secrets/last-good/platform.env"
  acme_file="$secrets/last-good/acme.env"
  [[ -f "$env_file" ]] || die "no last-good copy in $secrets/last-good (one is saved by every successful 'platform.sh up' from the store)"
  echo "platform: starting from the last-good copy in $secrets/last-good, not the variables store" >&2
elif [[ -f "$secrets/vars.db" ]]; then
  # Only the platform's own values: an application's broken reference is its deploy's problem.
  python3 "$root/scripts/vars.py" check --platform >&2 \
    || die "the variables store failed its check; fix it (scripts/ygg.sh vars check) or run 'platform.sh $command --last-good'"
  # /run/yggdrasil for root; else the user's own runtime directory (a tmpfs only they read), else
  # a directory of theirs under $TMPDIR.
  run_base=/run/yggdrasil
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

compose() { docker compose --env-file "$env_file" -f "$root/platform/compose.yml" "$@"; }

# Jenkins needs them and the catalog has them: no second copy in platform.env to drift.
GITHUB_OWNER=$(python3 "$root/scripts/catalog.py" owner) || exit 1
YGGDRASIL_REPOSITORY=$(python3 "$root/scripts/catalog.py" repository) || exit 1
export GITHUB_OWNER YGGDRASIL_REPOSITORY

# The profile-only variables compose.yml cannot mark as required (see the note at its top).
check_profile_variables() {
  local profiles
  # shellcheck source=/dev/null
  profiles=$(set -a; . "$env_file"; echo ",${COMPOSE_PROFILES:-},")
  local required=()
  [[ "$profiles" == *,jenkins,* ]] && required+=(JENKINS_URL JENKINS_ADMIN_PASSWORD GITHUB_APP_ID GITHUB_APP_KEY_FILE)
  # DOCKER_GID too, but on every host: compose.yml requires it for the socket proxies.
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

case "$command" in
  up)
    check_environments
    check_profile_variables
    for network in edge telemetry; do
      docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null
    done
    # The agent's deploy locks (scripts/deploy.sh): group docker may create and hold them.
    install -d -m 2770 "$secrets/locks"
    if getent group docker >/dev/null && ! chgrp docker "$secrets/locks" 2>/dev/null; then
      echo "platform: could not give $secrets/locks to group docker; the Jenkins agent may deploy unlocked" >&2
    fi
    compose up --detach --build --remove-orphans --wait
    compose ps
    if [[ -n "$render_dir" ]]; then
      install -d -m 700 "$secrets/last-good"
      install -m 600 "$env_file" "$acme_file" "$secrets/last-good/"
    fi
    ;;
  down) compose down ;;
  ps) compose ps ;;
  logs) compose logs --follow --tail 200 "$@" ;;
  config) compose config ;;
esac
