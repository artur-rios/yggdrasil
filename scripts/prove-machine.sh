#!/usr/bin/env bash
# Proves, on a real machine, what the spec says about the rootless setup. Run by the admin (a sudo-capable
# user, never root) after `ygg install`:
#
#     scripts/prove-machine.sh <apps|dev>
#
# Prints rows shaped like `ygg check`'s and exits 1 when any is FAIL (2 on bad usage). The status words here are
# ok / FAIL / look: they REPLACE ygg check's ok / -- / !! marks, not add to them. This proof is run once, by a
# person, to decide whether the setup holds, so a failed proof says FAIL in words rather than a mark to be
# interpreted:
#
#   ok     the proof held
#   FAIL   it did not, or could not be made: a proof that couldn't run proves nothing, so that is a FAIL too
#   look   a thing for the admin to compare with their own eyes (never a failure)
#
# Both roles:
#   no root services     no process of uid 0 is one of the platform's services (a root dockerd, traefik, java...).
#                        Kernel threads and sshd are root's, and fine.
# dev only:
#   ci -> ygg (host)       `ci` can't read ygg's engine socket (sudo -u ci test -r).
#   ci -> ygg (docker)  a container on ci's engine, which mounts ygg's runtime directory, home and secrets,
#                          can't open the socket or list either. A rootless engine can't mount a path its own
#                          user can't read, so `docker run` failing there is the proof, not an error.
#   testcontainers         a container on ci's engine publishes a port (random, on every interface, the way
#                          Testcontainers does) and a second container on the same engine reaches it at the
#                          TESTCONTAINERS_HOST_OVERRIDE that run-check.sh computes.
# apps only:
#   client addresses      a `look`: the last 10 distinct client addresses in Traefik's access log, to compare with
#                         the admin's own address as Cloudflare saw it (docs/hosts.md, Client addresses).
#
# The engines are the service users': this script acts on them through sudo, with machine.sh's run_as_user, as
# `ygg check` does, and never uses the admin's own (it has none). Containers are named ygg-prove-* and are
# removed on every way out. Nothing secret is read or printed: no env file, no token, and from the access log
# only the client address (never a path or a header).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

die() { echo "prove-machine: $*" >&2; exit 1; }
usage() { echo "usage: prove-machine.sh <apps|dev>" >&2; exit 2; }
has() { command -v "$1" >/dev/null 2>&1; }

# TAR_IMAGE (the pinned busybox every helper container uses), and machine.sh's run_as_user, passwd_field,
# user_exists, machine_engine_socket, secrets_dir_of and machine_list. machine.sh is functions and tables only.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"
# shellcheck source=machine.sh
source "$root/scripts/machine.sh"

[[ $# -eq 1 ]] || usage
role=$1
case $role in
  apps | dev) ;;
  *) usage ;;
esac

[[ "$(id -u)" != 0 ]] || die "run this as the admin user, not as root: it asks sudo for what needs root, and the services must not be root's"
has sudo || die "this needs sudo: it acts on the service users' engines as those users"

# ---- Rows ---------------------------------------------------------------------------------------

if [[ -t 1 ]]; then
  bold=$'\e[1m' red=$'\e[31m' green=$'\e[32m' yellow=$'\e[33m' reset=$'\e[0m'
else
  bold="" red="" green="" yellow="" reset=""
fi

failed=0

title() { printf '\n%s== %s ==%s\n' "$bold" "$*" "$reset"; }

# row <ok|FAIL|look> <name> <detail>: the same shape as host.sh's row (two spaces, the status, two spaces,
# the name padded to 20, the detail), with this script's three words, padded to the longest so the names line up.
row() {
  local mark
  case $1 in
    ok) mark="${green}ok${reset}  " ;;
    FAIL) mark="${red}FAIL${reset}" ;;
    *) mark="${yellow}look${reset}" ;;
  esac
  printf '  %s  %-20s %s\n' "$mark" "$2" "$3"
}

ok() { row ok "$1" "$2"; }
fail() {
  row FAIL "$1" "$2"
  failed=1
}

# ---- Acting as a service user -------------------------------------------------------------------

# user_docker <user> <docker arguments...>: docker against that user's own engine, as that user: machine.sh's
# run_as_user (sudo -u, from /, in the user's runtime directory), with DOCKER_HOST set to the user's socket so
# the CLI can't reach for another engine. stdin is closed so nothing waits on it.
user_docker() {
  local user=$1
  shift
  run_as_user "$user" env "DOCKER_HOST=unix://$(machine_engine_socket "$user")" docker "$@" </dev/null
}
ci_docker() { user_docker ci "$@"; }

# user_docker_input <user> <file> <docker arguments...>: user_docker with the file as the command's stdin (docker
# run -i), for handing a container a file without mounting anything from the host.
user_docker_input() {
  local user=$1 input=$2
  shift 2
  run_as_user "$user" env "DOCKER_HOST=unix://$(machine_engine_socket "$user")" docker "$@" <"$input"
}

# The containers this script starts, all on ci's engine. Removed (by name, so also a leftover of a killed run)
# before the proofs and on every way out of the script: the EXIT trap runs on a normal exit, on `die` and on the
# signals below.
PROVE_CONTAINERS=(ygg-prove-probe ygg-prove-server ygg-prove-client)
cleanup_needed=""
cleanup() {
  local name
  [[ -n "$cleanup_needed" ]] || return 0
  for name in "${PROVE_CONTAINERS[@]}"; do
    ci_docker rm -f "$name" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# ---- No root services ---------------------------------------------------------------------------

# The commands that must never run as uid 0: the engines (rootless ones run as their service user; a rootlesskit
# or dockerd of root's is a rootful engine), and what the platform runs, which is all in containers whose root is
# the service user's uid on the host (Traefik, Forgejo, Jenkins' java, node, the monitoring).
ROOT_FORBIDDEN=(dockerd containerd rootlesskit traefik forgejo gitea java node prometheus loki alloy grafana)

prove_no_root_services() {
  local name="no root services" listing pid uid comm offenders=() forbidden
  listing=$(ps -eo pid=,uid=,comm=) || {
    fail "$name" "ps failed: nothing proven"
    return 0
  }
  while read -r pid uid comm; do
    [[ "$uid" == 0 ]] || continue
    for forbidden in "${ROOT_FORBIDDEN[@]}"; do
      if [[ "$comm" == "$forbidden" ]]; then
        offenders+=("$pid $comm")
        break
      fi
    done
  done <<<"$listing"
  if ((${#offenders[@]})); then
    fail "$name" "owned by root (pid name): $(machine_list "${offenders[@]}")"
  else
    ok "$name" "none of the platform's ${#ROOT_FORBIDDEN[@]} service commands runs as root"
  fi
}

# ---- (dev) a check can't reach ygg --------------------------------------------------------------

# Whether ci's engine can run the container proofs: it answers, it is rootless, and it has the image. Said once
# (empty when it can), because a `docker run` that fails for any of these reasons would otherwise look like the
# refusal the proof is after.
ci_problem=""
ci_engine_problem() {
  local security
  if ! user_exists ci; then
    ci_problem="there is no ci user"
  elif ! security=$(ci_docker info --format '{{json .SecurityOptions}}' 2>/dev/null); then
    ci_problem="ci's engine does not answer on $(machine_engine_socket ci)"
  elif [[ "$security" != *name=rootless* ]]; then
    ci_problem="ci's engine is not rootless ($security)"
  elif ! ci_docker image inspect "$TAR_IMAGE" >/dev/null 2>&1 && ! ci_docker pull --quiet "$TAR_IMAGE" >/dev/null 2>&1; then
    ci_problem="ci's engine can't pull $TAR_IMAGE: needed for the probe containers"
  fi
}

# prove_ci_host: the sockets are the root of everything on ygg's engine, so ci (which runs other people's code)
# must not read it. The row proves its test could fail first: that commands run as ci at all, and that the
# socket exists (asked of root; a path that isn't there can't be read either).
prove_ci_host() {
  local name="ci -> ygg (host)" socket
  if ! user_exists ygg || ! user_exists ci; then
    fail "$name" "no ygg or ci user: nothing proven"
    return 0
  fi
  socket=$(machine_engine_socket ygg)
  if ! run_as_user ci true </dev/null 2>/dev/null; then
    fail "$name" "sudo does not run commands as ci: nothing proven"
  elif ! sudo test -e "$socket" 2>/dev/null; then
    fail "$name" "$socket is not there (ygg's engine is not running): nothing proven"
  elif run_as_user ci test -r "$socket" </dev/null 2>/dev/null; then
    fail "$name" "ci can read $socket: the isolation of the users is broken (look at the permissions on it and on the run directory)"
  else
    ok "$name" "ci can't read $socket"
  fi
}

# What the probe container runs (busybox sh): reports what it could reach, then that it got to the end.
# Existence is tested, not opening: open() on a socket's node fails (ENXIO) whoever asks, so it would pass for a
# socket that is wide open. A socket that can be seen at the mount is one the container's root (ci on the host)
# could traverse to; a directory it can list is one it can read.
# shellcheck disable=SC2016  # it is a script for the container's sh: nothing in it is for this shell to expand
PROBE_SCRIPT='r=""
[ -e /probe-run/docker.sock ] && r="$r run-socket"
ls /probe-home >/dev/null 2>&1 && r="$r home"
ls /probe-etc >/dev/null 2>&1 && r="$r etc"
echo "reached:$r"
echo probe-done'

prove_ci_container() {
  local name="ci -> ygg (docker)" ygg_uid ygg_home etc path missing=() out status reached
  if [[ -n "$ci_problem" ]]; then
    fail "$name" "$ci_problem: nothing proven"
    return 0
  fi
  if ! user_exists ygg; then
    fail "$name" "no ygg user: nothing proven"
    return 0
  fi
  ygg_uid=$(passwd_field ygg 3)
  ygg_home=$(passwd_field ygg 6)
  etc=$(secrets_dir_of ygg)
  # docker would make a source path that isn't there (or refuse because it can't): either way the run says
  # nothing about ygg, so the paths are asked of root first.
  for path in "/run/user/$ygg_uid" "$ygg_home" "$etc"; do
    sudo test -e "$path" 2>/dev/null || missing+=("$path")
  done
  if ((${#missing[@]})); then
    fail "$name" "not there to mount: $(machine_list "${missing[@]}"): nothing proven"
    return 0
  fi

  status=0
  out=$(ci_docker run --rm --name ygg-prove-probe --network none \
    -v "/run/user/$ygg_uid:/probe-run:ro" -v "$ygg_home:/probe-home:ro" -v "$etc:/probe-etc:ro" \
    "$TAR_IMAGE" sh -c "$PROBE_SCRIPT" 2>&1) || status=$?
  if ((status != 0)); then
    # The engine runs as ci and can't mount a path ci can't read: it refuses, and that is the isolation working.
    # Only that refusal is the proof: docker's own failures use status 125, and the message must be about a
    # mount or a permission. Anything else (a daemon error, a probe that crashed, a signal) proves nothing.
    if ((status == 125)) && grep -qiE 'permission denied|not a directory|no such file|operation not permitted' <<<"$out"; then
      ok "$name" "the engine of ci refused the mounts of ygg's directories: $(head -n1 <<<"$out" | cut -c1-110)"
    else
      fail "$name" "refused for another reason (status $status): $(head -n1 <<<"$out" | cut -c1-110)"
    fi
    return 0
  fi
  if ! grep -qx 'probe-done' <<<"$out"; then
    fail "$name" "the probe container ran and did not finish its checks: nothing proven"
    return 0
  fi
  reached=$(sed -n 's/^reached://p' <<<"$out" | head -n1)
  reached=${reached# }
  if [[ -n "${reached// /}" ]]; then
    fail "$name" "a container of ci reached ygg's: $reached (run-socket is ygg's engine socket, home its home, etc its secrets)"
  else
    ok "$name" "a container of ci can't open ygg's socket or list its home and secrets"
  fi
}

# ---- (dev) Testcontainers works rootless --------------------------------------------------------

# Where Testcontainers' own tests reach a published port: the container's default gateway, which is the host.
# The server publishes a random port on every interface (-p 0:8080); the client is a second container on the
# same engine, and finds the address by running run-check.sh --print-host-override, run by busybox's sh (that
# part of the script is plain sh for this) -- the same code a --docker check is told its override by. The
# script reaches the container on its stdin (docker run -i), not as a bind mount: ci's daemon resolves a mount's
# source itself, so a checkout under a directory ci can't traverse (the admin's home, say) would have the run
# refused for a reason that has nothing to do with Testcontainers.
# shellcheck disable=SC2016
SERVER_SCRIPT='mkdir -p /www && echo ygg-prove >/www/index.html && exec httpd -f -p 8080 -h /www'
# shellcheck disable=SC2016
CLIENT_SCRIPT='cat >/tmp/run-check.sh
h=$(sh /tmp/run-check.sh --print-host-override)
echo "override:$h"
body=""
if [ -n "$h" ]; then
  i=0
  while [ "$i" -lt 5 ]; do
    body=$(wget -qO- -T 5 "http://$h:$1/") && break
    i=$((i + 1))
    sleep 1
  done
fi
echo "body:$body"'

prove_testcontainers() {
  local name="testcontainers" ports port out status override body
  if [[ -n "$ci_problem" ]]; then
    fail "$name" "$ci_problem: nothing proven"
    return 0
  fi
  if ! ci_docker run -d --name ygg-prove-server -p 0:8080 "$TAR_IMAGE" sh -c "$SERVER_SCRIPT" >/dev/null 2>&1; then
    fail "$name" "ci's engine could not start the test server on a published port"
    return 0
  fi
  ports=$(ci_docker port ygg-prove-server 8080/tcp 2>/dev/null) || ports=""
  # "0.0.0.0:49153" and "[::]:49153": the port is after the last colon of the first line.
  port=$(head -n1 <<<"$ports")
  port=${port##*:}
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    fail "$name" "the engine did not say which host port it published"
    return 0
  fi

  status=0
  out=$(user_docker_input ci "$root/scripts/run-check.sh" run -i --rm --name ygg-prove-client \
    "$TAR_IMAGE" sh -c "$CLIENT_SCRIPT" sh "$port" 2>&1) || status=$?
  override=$(sed -n 's/^override://p' <<<"$out" | head -n1)
  body=$(sed -n 's/^body://p' <<<"$out" | head -n1)
  if ((status != 0)) && [[ -z "$override" ]]; then
    fail "$name" "the second container could not run: $(head -n1 <<<"$out" | cut -c1-110)"
  elif [[ -z "$override" ]]; then
    fail "$name" "run-check.sh --print-host-override found no default gateway in the container: Testcontainers would not know where to connect"
  elif [[ "$body" == *ygg-prove* ]]; then
    ok "$name" "a published port (random, on every interface) answered at $override:$port, the TESTCONTAINERS_HOST_OVERRIDE of run-check.sh"
  else
    fail "$name" "$override:$port did not answer from a second container: a published port isn't reachable at the override"
  fi
}

# ---- (apps) real client addresses ---------------------------------------------------------------

# Traefik logs every request as one JSON line on its stdout (traefik_config.py: accessLog, format json), so the
# engine keeps it: `docker logs`, as ygg (whose engine Traefik runs in), with the container found by the labels
# Compose puts on it. ClientHost is the client's address: the connection's own, or, where Traefik trusts the
# forwarded headers (behind Cloudflare, from its ranges only), the first address of X-Forwarded-For. Only that
# field is taken; the line also holds the request's path and more, which are not printed. Anything that isn't an
# address is dropped, so the log can't put text of its own on the admin's screen.
LOG_LINES=5000
prove_client_addresses() {
  local name="client addresses" id log addresses count
  if ! user_exists ygg; then
    fail "$name" "no ygg user: nothing to look at"
    return 0
  fi
  id=$(user_docker ygg ps -q --filter label=com.docker.compose.project=yggdrasil \
    --filter label=com.docker.compose.service=traefik 2>/dev/null | head -n1) || id=""
  if [[ -z "$id" ]]; then
    fail "$name" "Traefik's container is not running in ygg's engine: platform.sh up, then prove again"
    return 0
  fi
  log=$(user_docker ygg logs --tail "$LOG_LINES" "$id" 2>/dev/null) || log=""
  addresses=$({ grep -o '"ClientHost":"[^"]*"' <<<"$log" || true; } \
    | sed 's/^"ClientHost":"//; s/"$//' | { grep -E '^[0-9A-Fa-f:.]+$' || true; } \
    | tac | awk '!seen[$0]++ { print; if (++n == 10) exit }') || true
  if [[ -z "$addresses" ]]; then
    row look "$name" "no request in the last $LOG_LINES lines of Traefik's access log: make one from your machine, then prove again"
    return 0
  fi
  count=$(wc -l <<<"$addresses")
  row look "$name" "the $count latest distinct, newest first: $(paste -sd' ' - <<<"$addresses" | sed 's/ /, /g'). Yours, as Cloudflare saw it, must be there; a Cloudflare or an internal address in its place means the real one is lost (docs/hosts.md)"
}

# ---- Run ----------------------------------------------------------------------------------------

title "Rootless proofs ($role)"
prove_no_root_services

if [[ "$role" == dev ]]; then
  # Anything a killed run left is removed before a name is used again (there is nothing to remove from the engine
  # of a ci that isn't there, and asking for it would stop the script).
  if user_exists ci; then
    cleanup_needed=1
    cleanup
  fi
  prove_ci_host
  ci_engine_problem
  prove_ci_container
  prove_testcontainers
else
  prove_client_addresses
fi

exit "$failed"
