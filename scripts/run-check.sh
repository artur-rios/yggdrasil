#!/usr/bin/env bash
# Runs one check of a repository's pipeline in a container, on a Jenkins build agent. The pipeline library
# calls it for each planned check, for the release build and for packing NuGet packages.
#
#     scripts/run-check.sh <name> <image> <workspace> [--docker] [--env-file <file>] -- <command...>
#
#   name        the check's name; only used to label the output.
#   image       pulled when the engine does not have it (--pull missing), never refreshed.
#   workspace   the checkout, as an absolute path that means the same directory inside the agent and on the
#               host (platform/build-agent.yml mounts it at the same path on both sides): the `docker run`
#               below goes to the host's engine, which resolves the mount's source on the host.
#   --docker    the check needs a Docker engine (Testcontainers, `docker build`): the user's rootless socket
#               is mounted into the container and Testcontainers is pointed at it. The socket is named by
#               its host path, $YGG_DOCKER_SOCKET (the agent's environment, from platform/build-agent.yml),
#               for the same reason as the workspace: the agent's own /var/run/docker.sock is not a path the
#               host's engine can mount.
#   --env-file  a file of NAME=value lines (the credentials the pipeline bound, written 600 by the library).
#               Handed to docker as a file, so no value is ever on a command line or in the process list,
#               and this script never reads or prints it.
#   command     what the container runs; everything after `--`.
#
# The container runs as root (0:0), which under rootless Docker is the service user on the host. That is
# what lets a check write into the workspace the agent (also root) checked out, and open the socket.
#
# With --docker the Testcontainers settings are:
#   TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE  the mounted socket's path inside the container
#   TESTCONTAINERS_HOST_OVERRIDE           where a started container's published ports are reached: the
#                                          container's default gateway, which is the host, not localhost.
#                                          An override already in the agent's environment wins.
#   TESTCONTAINERS_RYUK_DISABLED           Ryuk would need the socket mounted by its host path as well, and
#                                          the --rm and the engine's own cleanup are enough for a CI run.
#
# Exit: the command's exit code (docker run passes it through; 125-127 are docker's own failures), 2 on bad
# usage. Needs YGG_DOCKER_SOCKET with --docker.
set -euo pipefail

usage() {
  echo "usage: run-check.sh <name> <image> <workspace> [--docker] [--env-file <file>] -- <command...>" >&2
  exit 2
}
die_usage() { echo "run-check.sh: $*" >&2; usage; }

[[ $# -ge 3 ]] || usage
name=$1
image=$2
workspace=$3
shift 3

with_docker=false
env_file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker) with_docker=true; shift ;;
    --env-file)
      [[ $# -ge 2 && "$2" != "--" ]] || die_usage "--env-file needs a file"
      env_file=$2; shift 2 ;;
    --) shift; break ;;
    *) die_usage "unknown option '$1'" ;;
  esac
done
# `--` is what separates this script's options from the check's command; without it, or with nothing after
# it, there is nothing to run.
[[ $# -gt 0 ]] || die_usage "no command after --"

[[ "$workspace" == /* ]] || die_usage "the workspace must be an absolute path, not '$workspace'"
[[ -d "$workspace" ]] || die_usage "the workspace '$workspace' is not a directory"
[[ -z "$env_file" || -f "$env_file" ]] || die_usage "the env file '$env_file' does not exist"

# The container's default gateway: `ip` when the image has it, else the kernel's route table, whose default
# route has destination 00000000 and the gateway as little-endian hex (0141A8C0 is 192.168.65.1).
default_gateway() {
  local gateway="" destination hex
  if command -v ip >/dev/null 2>&1; then
    gateway=$(ip route 2>/dev/null | awk '$1 == "default" { for (i = 2; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }') || true
  fi
  if [[ -z "$gateway" && -r "${YGG_ROUTE_TABLE:-/proc/net/route}" ]]; then
    while read -r _ destination hex _; do
      if [[ "$destination" == "00000000" && "$hex" =~ ^[0-9A-Fa-f]{8}$ ]]; then
        gateway=$(printf '%d.%d.%d.%d' "0x${hex:6:2}" "0x${hex:4:2}" "0x${hex:2:2}" "0x${hex:0:2}")
        break
      fi
    done <"${YGG_ROUTE_TABLE:-/proc/net/route}"
  fi
  printf '%s' "$gateway"
}

run=(docker run --rm --init --pull missing --user 0:0
     -v "$workspace:$workspace" -w "$workspace")

[[ -z "$env_file" ]] || run+=(--env-file "$env_file")

if $with_docker; then
  [[ -n "${YGG_DOCKER_SOCKET:-}" ]] \
    || die_usage "--docker needs YGG_DOCKER_SOCKET, the host path of the Docker socket (set by platform/build-agent.yml)"
  host=${TESTCONTAINERS_HOST_OVERRIDE:-$(default_gateway)}
  run+=(-v "$YGG_DOCKER_SOCKET:/var/run/docker.sock"
        -e DOCKER_HOST=unix:///var/run/docker.sock
        -e TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
        -e TESTCONTAINERS_RYUK_DISABLED=true)
  if [[ -n "$host" ]]; then
    run+=(-e "TESTCONTAINERS_HOST_OVERRIDE=$host")
  else
    # Better a check that does not use Testcontainers running than none: only one that starts containers
    # and connects to them fails, and its error names the address it tried.
    echo "run-check.sh: cannot find the default gateway; TESTCONTAINERS_HOST_OVERRIDE is not set for '$name'" >&2
  fi
fi

echo "run-check.sh: $name in $image" >&2
# exec: the container's exit code is this script's.
exec "${run[@]}" "$image" "$@"
