#!/usr/bin/env bash
# Runs one check of a repository's pipeline in a container, on a Jenkins build agent. The pipeline library
# calls it for each planned check, for the release build and for packing NuGet packages.
#
#     scripts/run-check.sh <name> <image> <workspace> [--docker] [--env-file <file>]
#                          [--toolchains <cache> [--checkout <dir>]] -- <command...>
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
#   --toolchains  the ci agent's toolchain cache ($YGG_TOOLCHAIN_CACHE; scripts/toolchains.sh has the whole story):
#               when the checkout pins a Flutter SDK (FLUTTER_VERSION and FLUTTER_SHA256 in ci/flutter-env.sh or
#               ci/pins.sh), it is fetched into the cache once, and the check gets it as a copy-on-write overlay at
#               /opt/yggdrasil/flutter with FLUTTER_SDK_DIR naming it, which ci/flutter-env.sh uses instead of
#               fetching its own. A checkout without a pin, or a cache that can't supply it, runs as without
#               the option. The pipeline library passes it for checks and the branch policy only, never for a
#               release step.
#   --checkout  where in the workspace the repository's root is, for reading its pin (default: the workspace;
#               the branch policy mounts the whole workspace and runs in its app/).
#   command     what the container runs; everything after `--`.
#
# The container runs as root (0:0), which under rootless Docker is the service user on the host. That is
# what lets a check write into the workspace the agent (also root) checked out, and open the socket.
#
# With --docker the Testcontainers settings are:
#   TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE  the mounted socket's path inside the container
#   TESTCONTAINERS_HOST_OVERRIDE           where a started container's published ports are reached, not
#                                          localhost: the one in the agent's environment, which
#                                          platform/build-agent.yml gives it from scripts/agent.sh (agent.env's
#                                          TESTCONTAINERS_HOST_OVERRIDE, else the host's own primary address).
#                                          Not the container's default gateway: the rootless engines use the
#                                          slirp4netns port driver, which opens a published port on the host
#                                          only, so the gateway refuses it, while the host's address is reached
#                                          through slirp4netns. When the agent's is empty or unset (an agent
#                                          started before it had one), the default gateway, as a fallback.
#   TESTCONTAINERS_RYUK_DISABLED           Ryuk would need the socket mounted by its host path as well, and
#                                          the --rm and the engine's own cleanup are enough for a CI run.
#
#     scripts/run-check.sh --print-host-override
#
#   prints the TESTCONTAINERS_HOST_OVERRIDE this script would set (see above) and exits 0; it needs no other
#   argument and works under a plain sh, so a container without bash can run it (scripts/prove-machine.sh does).
#
# Exit: the command's exit code (docker run passes it through; 125-127 are docker's own failures), 2 on bad
# usage. Needs YGG_DOCKER_SOCKET with --docker. With an overlay, the script waits for docker instead of becoming
# it, removes the check's run directory, and exits with docker's code; TERM, INT and HUP are passed on to docker,
# which passes them to the container.

# ---- The host override: plain sh ------------------------------------------------------------------------
# Everything from here to the `set -euo pipefail` below is POSIX sh, no bash: `--print-host-override` is
# also run INSIDE a container by scripts/prove-machine.sh (`sh run-check.sh --print-host-override` in a
# busybox image, which has no bash), to find the address a published port is reached at the way a
# --docker check is told it. One definition, so the proof and the checks cannot drift apart.

# The container's default gateway: `ip` when the image has it, else the kernel's route table, whose default
# route has destination 00000000 and the gateway as little-endian hex (0141A8C0 is 192.168.65.1).
default_gateway() {
  gateway=""
  if command -v ip >/dev/null 2>&1; then
    gateway=$(ip route 2>/dev/null | awk '$1 == "default" { for (i = 2; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }') || true
  fi
  if [ -z "$gateway" ] && [ -r "${YGG_ROUTE_TABLE:-/proc/net/route}" ]; then
    while read -r _ destination hex _; do
      [ "$destination" = "00000000" ] || continue
      # Eight hex digits, or the line is not a gateway.
      case $hex in
        [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;;
        *) continue ;;
      esac
      gateway=$(printf '%s' "$hex" | sed 's/\(..\)\(..\)\(..\)\(..\)/\4 \3 \2 \1/' \
        | { read -r a b c d; printf '%d.%d.%d.%d' "0x$a" "0x$b" "0x$c" "0x$d"; })
      break
    done <"${YGG_ROUTE_TABLE:-/proc/net/route}"
  fi
  printf '%s' "$gateway"
}

# --print-host-override: prints what this script would set as TESTCONTAINERS_HOST_OVERRIDE (a non-empty
# override already in the environment wins, else the default gateway; nothing when there is neither) and exits 0.
if [ "${1:-}" = "--print-host-override" ]; then
  printf '%s\n' "${TESTCONTAINERS_HOST_OVERRIDE:-$(default_gateway)}"
  exit 0
fi

set -euo pipefail

usage() {
  echo "usage: run-check.sh <name> <image> <workspace> [--docker] [--env-file <file>] [--toolchains <cache> [--checkout <dir>]] -- <command...>" >&2
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
toolchains=""
checkout=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker) with_docker=true; shift ;;
    --env-file)
      [[ $# -ge 2 && "$2" != "--" ]] || die_usage "--env-file needs a file"
      env_file=$2; shift 2 ;;
    --toolchains)
      [[ $# -ge 2 && "$2" != "--" ]] || die_usage "--toolchains needs the cache directory"
      toolchains=$2; shift 2 ;;
    --checkout)
      [[ $# -ge 2 && "$2" != "--" ]] || die_usage "--checkout needs a directory"
      checkout=$2; shift 2 ;;
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
[[ -z "$checkout" || -n "$toolchains" ]] || die_usage "--checkout is only for --toolchains"
checkout=${checkout:-$workspace}
# The pin is read from the checkout, which must be the workspace or under it: the one the container sees.
[[ "$checkout" == "$workspace" || ( "$checkout" == "$workspace"/* && "$checkout" != *..* ) ]] \
  || die_usage "the checkout '$checkout' is not in the workspace '$workspace'"

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
    echo "run-check.sh: no TESTCONTAINERS_HOST_OVERRIDE from the agent and cannot find the default gateway; it is not set for '$name'" >&2
  fi
fi

run_dir=""
if [[ -n "$toolchains" ]]; then
  # shellcheck source=toolchains.sh
  source "$(dirname "${BASH_SOURCE[0]}")/toolchains.sh"
  if ! toolchain_cache_ok "$toolchains"; then
    # A misconfigured cache must not stop the checks: they fetch their own, as before it existed.
    echo "run-check.sh: the toolchain cache '$toolchains' is not an absolute directory of plain characters: running $name without it" >&2
  elif pin=$(flutter_pin "$checkout"); then
    read -r flutter_version flutter_sum <<<"$pin"
    if sdk=$(flutter_sdk "$toolchains" "$flutter_version" "$flutter_sum") \
        && overlay=$(flutter_overlay "$toolchains" "$sdk"); then
      run_dir=$(sed -n 2p <<<"$overlay")
      run+=(--mount "$(sed -n 1p <<<"$overlay")" -e "FLUTTER_SDK_DIR=$FLUTTER_IN_CONTAINER")
      echo "run-check.sh: Flutter $flutter_version from the toolchain cache, at $FLUTTER_IN_CONTAINER (copy-on-write)" >&2
    else
      echo "run-check.sh: Flutter $flutter_version is not in the toolchain cache: $name fetches its own" >&2
    fi
  fi
fi

echo "run-check.sh: $name in $image" >&2
if [[ -z "$run_dir" ]]; then
  # exec: the container's exit code is this script's.
  exec "${run[@]}" "$image" "$@"
fi

# With an overlay, its run directory is removed afterwards, so docker runs as a child: the signals Jenkins sends to
# stop a build go on to it (and from it to the container), and its exit code is this script's.
"${run[@]}" "$image" "$@" &
child=$!
trap 'kill -TERM "$child" 2>/dev/null || true' TERM INT HUP
while :; do
  code=0
  wait "$child" || code=$?
  # A trapped signal ends `wait` early (128 + the signal) while docker still runs: wait again.
  kill -0 "$child" 2>/dev/null || break
done
toolchain_remove_run "$run_dir" || echo "run-check.sh: could not remove $run_dir (removed after a day)" >&2
exit "$code"
