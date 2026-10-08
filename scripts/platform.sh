#!/usr/bin/env bash
# Starts, updates or stops this host's platform stack (platform/compose.yml).
#
#     scripts/platform.sh up | down | ps | logs [service] | config
#
# One platform stack per host, whatever number of environments the host runs (ENVIRONMENTS). Reads
# <secrets>/platform.env, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil). Template:
# env/platform.env.example.
set -euo pipefail

die() { echo "platform: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}
env_file="$secrets/platform.env"
[[ -f "$env_file" ]] || die "missing $env_file (template: env/platform.env.example)"

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

case "${1:-}" in
  up)
    check_environments
    check_profile_variables
    for network in edge telemetry; do
      docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null
    done
    compose up --detach --build --remove-orphans --wait
    compose ps
    ;;
  down) compose down ;;
  ps) compose ps ;;
  logs) shift; compose logs --follow --tail 200 "$@" ;;
  config) compose config ;;
  *) die "usage: platform.sh up | down | ps | logs [service] | config" ;;
esac
