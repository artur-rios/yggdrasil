#!/usr/bin/env bash
# Starts, updates or stops this host's platform stack (platform/compose.yml).
#
#     scripts/platform.sh up | down | ps | logs [service] | config
#
# Reads <secrets>/platform.env, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil). Template:
# env/platform.env.example.
set -euo pipefail

die() { echo "platform: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}
env_file="$secrets/platform.env"
[[ -f "$env_file" ]] || die "missing $env_file (template: env/platform.env.example)"

compose() { docker compose --env-file "$env_file" -f "$root/platform/compose.yml" "$@"; }

# The profile-only variables compose.yml cannot mark as required (see the note at its top).
check_profile_variables() {
  local profiles
  profiles=$(set -a; . "$env_file"; echo ",${COMPOSE_PROFILES:-},")
  local required=()
  [[ "$profiles" == *,jenkins,* ]] && required+=(JENKINS_URL JENKINS_ADMIN_PASSWORD GITHUB_APP_ID GITHUB_APP_KEY_FILE)
  [[ "$profiles" == *,agent,* ]] && required+=(JENKINS_URL JENKINS_AGENT_NAME JENKINS_AGENT_SECRET DOCKER_GID)
  local name value
  for name in "${required[@]}"; do
    value=$(set -a; . "$env_file"; eval "echo \"\${$name:-}\"")
    [[ -n "$value" ]] || die "$name must be set in $env_file for profiles '${profiles//,/ }'"
  done
}

case "${1:-}" in
  up)
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
