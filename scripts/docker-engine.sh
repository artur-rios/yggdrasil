# shellcheck shell=bash
# The Docker engine a yggdrasil script talks to: the invoking user's own rootless one. Sourced (not run)
# by scripts/platform.sh, scripts/agent.sh, scripts/backup.sh, scripts/renovate.sh and scripts/volumes.sh,
# which define die() -- it prefixes the message with the script's name.

# What tars a volume (backup.sh) and untars one (volumes.sh): pinned by version and digest, like the
# platform's images, so a night's backup or the day of the cutover never pulls whatever "latest" is then.
# One definition for both, so they cannot drift apart. It is used by the scripts that source this file.
# shellcheck disable=SC2034
TAR_IMAGE=busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e

# The path of the invoking user's own Docker socket: DOCKER_HOST when it is unix://..., else the
# socket in the user's runtime directory (XDG_RUNTIME_DIR, /run/user/<uid> by default). Never
# /var/run/docker.sock: that is the root engine, which the platform does not run on. Prints the path;
# dies, naming the fix, when there is no such socket.
docker_socket() {
  if [[ "${DOCKER_HOST:-}" == unix://* ]]; then
    echo "${DOCKER_HOST#unix://}"
    return
  fi
  local socket
  socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
  [[ -S "$socket" ]] || die "no rootless Docker engine for $(id -un): run 'ygg install' (docs/hosts.md)"
  echo "$socket"
}

# Dies unless the engine the docker CLI talks to (DOCKER_SOCKET) is a rootless one. Under rootless
# Docker, root in a container is the service user on the host, which is what lets the socket proxies
# and the agents run as root in their containers; on a rootful engine the same would be host root.
require_rootless() {
  local security
  security=$(docker info --format '{{json .SecurityOptions}}') \
    || die "cannot reach the Docker engine at $DOCKER_SOCKET (is it running? systemctl --user status docker)"
  [[ "$security" == *name=rootless* ]] \
    || die "the Docker engine at $DOCKER_SOCKET runs as root; the platform runs on rootless Docker only (docs/hosts.md)"
}

# Points the docker CLI and Compose (the socket the Compose files mount into their containers) at the
# user's own engine, once it has been checked. Before any docker command.
use_rootless_engine() {
  DOCKER_SOCKET=$(docker_socket) || exit 1
  # DOCKER_HOST too, so the CLI cannot reach for another engine than the one that was checked.
  export DOCKER_SOCKET DOCKER_HOST="unix://$DOCKER_SOCKET"
  require_rootless
}
