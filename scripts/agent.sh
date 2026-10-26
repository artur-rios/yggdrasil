#!/usr/bin/env bash
# Starts, updates or stops this user's Jenkins build agent (platform/build-agent.yml): the agent of
# the `ci` or `release` service user, each on its own rootless Docker engine.
#
#     scripts/agent.sh up | down | ps | logs
#
# Run as the agent's own user. Reads <secrets>/agent.env, secrets being $YGG_SECRETS_DIR (default
# /etc/yggdrasil-<user>). Template: env/agent.env.example. Hands the agent TESTCONTAINERS_HOST_OVERRIDE:
# agent.env's, else the host's own address (below). The Compose project is
# yggdrasil-agent-<JENKINS_AGENT_NAME>, so the agents of several users never share one. The deploy
# agent is not this: it is the `agent` profile of the platform stack (scripts/platform.sh).
set -euo pipefail

die() { echo "agent: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless and use_rootless_engine, shared with platform.sh; host_published_address,
# shared with prove-machine.sh.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil-$(id -un)}
usage="usage: agent.sh up | down | ps | logs"
command=${1:-}
[[ "$command" =~ ^(up|down|ps|logs)$ && $# -eq 1 ]] || die "$usage"

env_file="$secrets/agent.env"
[[ -f "$env_file" ]] || die "missing $env_file (template: env/agent.env.example)"

# Compose interpolates these into the file, and an empty one would be a silent misconfiguration (an
# agent that never connects, a workdir mounted from nowhere), so each is checked here.
for name in JENKINS_URL JENKINS_AGENT_NAME JENKINS_AGENT_SECRET YGG_AGENT_WORKDIR; do
  # shellcheck source=/dev/null
  value=$(set -a; . "$env_file"; eval "echo \"\${$name:-}\"")     || die "cannot read $env_file: it must be shell-style NAME=value lines (template: env/agent.env.example)"
  [[ -n "$value" ]] || die "$name must be set in $env_file (template: env/agent.env.example)"
  printf -v "$name" '%s' "$value"
done
[[ "$YGG_AGENT_WORKDIR" == /* ]] \
  || die "YGG_AGENT_WORKDIR in $env_file must be an absolute path, not '$YGG_AGENT_WORKDIR': it is mounted at the same path in the agent and on the host"
# A Compose project name is lowercase letters, digits, - and _.
[[ "$JENKINS_AGENT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] \
  || die "JENKINS_AGENT_NAME in $env_file must be lowercase letters, digits, - and _, not '$JENKINS_AGENT_NAME'"

# Where a check's Testcontainers reaches the containers it starts: build-agent.yml hands this to the agent, and
# run-check.sh to a --docker check. agent.env's when it sets one (optional), else the host's own address
# (host_published_address in docker-engine.sh says why not the gateway). One already in this script's own
# environment is ignored (unset before agent.env is read). Empty when neither is found, and run-check.sh then
# falls back to the containers' gateway, as it did before.
# shellcheck source=/dev/null
TESTCONTAINERS_HOST_OVERRIDE=$(unset TESTCONTAINERS_HOST_OVERRIDE; set -a; . "$env_file"; echo "${TESTCONTAINERS_HOST_OVERRIDE:-}") \
  || die "cannot read $env_file: it must be shell-style NAME=value lines (template: env/agent.env.example)"
[[ -n "$TESTCONTAINERS_HOST_OVERRIDE" ]] || TESTCONTAINERS_HOST_OVERRIDE=$(host_published_address)
export TESTCONTAINERS_HOST_OVERRIDE

compose() {
  docker compose -p "yggdrasil-agent-$JENKINS_AGENT_NAME" --env-file "$env_file" \
    -f "$root/platform/build-agent.yml" "$@"
}

use_rootless_engine
case "$command" in
  up)
    # Created by this user, not by Docker as root-in-the-container for a mount that would then fail
    # to be written by the build user.
    install -d -m 700 "$YGG_AGENT_WORKDIR"
    reached_at=${TESTCONTAINERS_HOST_OVERRIDE:-"the gateway of its container (no host address found)"}
    echo "agent: a check's Testcontainers reaches published ports at $reached_at" >&2
    compose up --detach --build --remove-orphans --wait
    compose ps
    ;;
  down) compose down ;;
  ps) compose ps ;;
  logs) compose logs --follow --tail 200 ;;
esac
