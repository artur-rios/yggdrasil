# shellcheck shell=bash
# Sourced by ci/status.sh and ci/platform.sh (not run on its own): puts the command-line tools the GitHub runner
# had, and the check images lack, on the PATH. Each `ensure_*` uses the tool already there (a machine of your own)
# and otherwise fetches a pinned build, checks it against its pinned checksum and unpacks it under a directory of
# its own ($YGG_CI_TOOLS, default a new directory under $TMPDIR), so a check that runs as a user who cannot install
# packages still gets it. Builds are for x86_64 only: the agents are, and so is the Flutter SDK the console needs.
#
# Renovate follows each version (the comment above it), not the checksums: when it moves a version, copy the new
# file's sha256 into this file by hand (the release page lists it; download.docker.com publishes none for the
# static Docker client, so that one is taken from the download, over HTTPS).

# renovate: datasource=docker depName=docker versioning=docker
DOCKER_VERSION=29.9.2
DOCKER_SHA256=9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb
# renovate: datasource=github-releases depName=docker/buildx
BUILDX_VERSION=v0.38.1
BUILDX_SHA256=e25d5cd675092bb5d1d03632fdc46cdf3fede7b514dc5f023a105e00d3ce5ad4
# renovate: datasource=github-releases depName=docker/compose
COMPOSE_VERSION=v5.6.1
COMPOSE_SHA256=47737f4236e6d3ccea7291995203f8f018058783b4c3a9469119811a0b5db8c0
# The GitHub runner's (Ubuntu 24.04) jq.
# renovate: datasource=github-releases depName=jqlang/jq versioning=regex:^jq-(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$
JQ_VERSION=jq-1.7.1
JQ_SHA256=5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5
# The ShellCheck CONTRIBUTING.md asks for locally: the same binary.
# renovate: datasource=github-releases depName=koalaman/shellcheck
SHELLCHECK_VERSION=v0.11.0
SHELLCHECK_SHA256=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198

# Where the fetched tools go: bin/ for executables, and the Docker client's CLI plugins (buildx, compose) in
# docker-plugins/, which DOCKER_CONFIG's cli-plugins directory points at.
_ci_tools_dir() {
  if [ -z "${YGG_CI_TOOLS:-}" ]; then
    YGG_CI_TOOLS=$(mktemp -d)
    export YGG_CI_TOOLS
  fi
  mkdir -p "$YGG_CI_TOOLS/bin"
  case ":$PATH:" in
    *":$YGG_CI_TOOLS/bin:"*) ;;
    *) export PATH="$YGG_CI_TOOLS/bin:$PATH" ;;
  esac
}

# _ci_fetch <url> <sha256> <file>: downloads the url to the file and checks it, or fails naming it.
_ci_fetch() {
  curl --fail --silent --show-error --location --retry 3 -o "$3" "$1"
  if ! echo "$2  $3" | sha256sum --check --strict --status; then
    echo "ci/tools.sh: $1 does not match its pinned checksum" >&2
    rm -f "$3"
    return 1
  fi
}

_ci_x86_64() {
  [ "$(uname -m)" = x86_64 ] || { echo "ci/tools.sh: no pinned $1 for $(uname -m)" >&2; return 1; }
}

ensure_docker() {
  command -v docker >/dev/null 2>&1 && return 0
  _ci_x86_64 "Docker client" && _ci_tools_dir
  echo "ci/tools.sh: no docker on the PATH; fetching the Docker $DOCKER_VERSION client"
  _ci_fetch "https://download.docker.com/linux/static/stable/x86_64/docker-$DOCKER_VERSION.tgz" "$DOCKER_SHA256" \
    "$YGG_CI_TOOLS/docker.tgz"
  # The client only: the engine is the one whose socket the check was given (docker: true).
  tar -xzf "$YGG_CI_TOOLS/docker.tgz" -C "$YGG_CI_TOOLS/bin" --strip-components=1 docker/docker
  rm -f "$YGG_CI_TOOLS/docker.tgz"
}

# _ci_plugin <name> <url> <sha256>: a Docker CLI plugin, when `docker <name> version` does not answer.
_ci_plugin() {
  docker "$1" version >/dev/null 2>&1 && return 0
  _ci_x86_64 "docker $1" && _ci_tools_dir
  echo "ci/tools.sh: no docker $1; fetching it"
  # A DOCKER_CONFIG of our own, so the plugin is found without writing to the user's ~/.docker.
  export DOCKER_CONFIG="$YGG_CI_TOOLS/docker-config"
  mkdir -p "$DOCKER_CONFIG/cli-plugins"
  _ci_fetch "$2" "$3" "$DOCKER_CONFIG/cli-plugins/docker-$1"
  chmod +x "$DOCKER_CONFIG/cli-plugins/docker-$1"
}

ensure_buildx() {
  ensure_docker
  _ci_plugin buildx "https://github.com/docker/buildx/releases/download/$BUILDX_VERSION/buildx-$BUILDX_VERSION.linux-amd64" \
    "$BUILDX_SHA256"
}

ensure_compose() {
  ensure_docker
  _ci_plugin compose "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/docker-compose-linux-x86_64" \
    "$COMPOSE_SHA256"
}

ensure_jq() {
  command -v jq >/dev/null 2>&1 && return 0
  _ci_x86_64 jq && _ci_tools_dir
  echo "ci/tools.sh: no jq on the PATH; fetching $JQ_VERSION"
  _ci_fetch "https://github.com/jqlang/jq/releases/download/$JQ_VERSION/jq-linux-amd64" "$JQ_SHA256" "$YGG_CI_TOOLS/bin/jq"
  chmod +x "$YGG_CI_TOOLS/bin/jq"
}

ensure_shellcheck() {
  command -v shellcheck >/dev/null 2>&1 && return 0
  _ci_x86_64 ShellCheck && _ci_tools_dir
  echo "ci/tools.sh: no shellcheck on the PATH; fetching ShellCheck $SHELLCHECK_VERSION"
  _ci_fetch "https://github.com/koalaman/shellcheck/releases/download/$SHELLCHECK_VERSION/shellcheck-$SHELLCHECK_VERSION.linux.x86_64.tar.xz" \
    "$SHELLCHECK_SHA256" "$YGG_CI_TOOLS/shellcheck.tar.xz"
  tar -xJf "$YGG_CI_TOOLS/shellcheck.tar.xz" -C "$YGG_CI_TOOLS/bin" --strip-components=1 \
    "shellcheck-$SHELLCHECK_VERSION/shellcheck"
  rm -f "$YGG_CI_TOOLS/shellcheck.tar.xz"
}
