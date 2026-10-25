#!/usr/bin/env bash
# The platform (ci.yml's job "Platform"): ShellCheck over the scripts, the Python unit suite (about twenty
# minutes), the Docker socket proxies and Forgejo live suites, the catalog's validation, and every Compose file
# rendered. Run from anywhere; it works from the repository's root.
#
# `docker: true` in yggdrasil.yml: the live suites start containers on the engine whose socket the check is
# given, the engine the check itself runs on. The tools the GitHub runner had and the Python image lacks
# (ShellCheck, jq, the Docker client and Compose) come from ci/tools.sh; PyYAML and cryptography go into a
# virtual environment of the check's own, as the job's `pip install` put them on the runner.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
root=$PWD

# shellcheck source=ci/tools.sh
source ci/tools.sh
ensure_shellcheck
ensure_jq
ensure_compose

work=$(mktemp -d)
# The socket proxies suite's scratch directory (below) is in the checkout; it goes whatever happens.
trap 'rm -rf "$work" "$root/.ci-tmp"' EXIT

python3 -m venv "$work/venv"
export PATH="$work/venv/bin:$PATH"

echo "== Check shell scripts"
shellcheck scripts/*.sh scripts/ygg ci/*.sh

pip install --quiet pyyaml cryptography

echo "== Test the catalog tooling"
python3 -m unittest discover -s scripts -v

echo "== Test the Docker socket proxies against Traefik and Alloy"
# The pinned proxy image with compose.yml's allowlists, in front of a fake Docker API. The suite hands each proxy
# container its fake socket by path, a path the engine must see: on the CI agent the engine is the host's, which
# sees the checkout at this same path (it is mounted at it) but not this container's /tmp. So the suite's scratch
# directory is in the checkout here.
mkdir -p "$root/.ci-tmp"
TMPDIR="$root/.ci-tmp" YGG_DOCKER_TESTS=1 python3 -m unittest scripts.test_socket_proxies -v
rm -rf "$root/.ci-tmp"

echo "== Test forge.sh and rules.py against Forgejo"
# The pinned Forgejo image, its API answers and the protections rules.py applies, with real users and
# repositories (scripts/test_forgejo_live.py). discover: the tests import flat. The suite reaches Forgejo at
# 127.0.0.1, which in a check is this container, not the engine's host: Forgejo joins this container's network
# instead of publishing a port (YGG_FORGEJO_TEST_NETWORK). The container's hostname is its id (run-check.sh
# gives it no other).
if [[ -f /.dockerenv ]]; then
  YGG_FORGEJO_TEST_NETWORK="container:$(hostname)"
  export YGG_FORGEJO_TEST_NETWORK
fi
YGG_FORGEJO_TESTS=1 python3 -m unittest discover -s scripts -p test_forgejo_live.py -v

echo "== Check the catalog"
# The same validation deploy.sh, platform.sh and rules.py run before using it.
python3 scripts/catalog.py validate

echo "== Validate the Compose files"
secrets=$(mktemp -d -p "$work")
cat > "$secrets/platform.env" <<ENV
ENVIRONMENTS=$(python3 scripts/catalog.py environments | paste -sd, -)
DOMAIN=example.com
ACME_EMAIL=ci@example.com
ACME_DNS_PROVIDER=cloudflare
TRAEFIK_DASHBOARD_USERS='admin:x'
GRAFANA_ADMIN_PASSWORD=x
YGGDRASIL_STATUS_TOKEN=0000000000000000000000000000000000000000
COMPOSE_PROFILES=apps
YGG_SECRETS_DIR=$secrets
ENV
touch "$secrets/acme.env"
# Traefik's configuration is a file compose.yml requires; platform.sh renders it from the same settings.
# shellcheck source=/dev/null
(set -a; . "$secrets/platform.env"; python3 scripts/traefik_config.py) > "$secrets/traefik.yml"
# DOCKER_SOCKET is the user's rootless socket, which platform.sh finds on a host; Compose only needs the path
# here, not an engine behind it.
export DOCKER_SOCKET=/run/user/1000/docker.sock YGG_TRAEFIK_CONFIG_FILE="$secrets/traefik.yml"
# platform.sh labels Traefik with the file's hash, so a changed file recreates it.
YGG_TRAEFIK_CONFIG_HASH=$(sha256sum "$secrets/traefik.yml" | cut -d' ' -f1)
export YGG_TRAEFIK_CONFIG_HASH
GITHUB_OWNER=$(python3 scripts/catalog.py owner)
export GITHUB_OWNER
# The platform file once for each machine's profiles: the apps machine's, with its deploy agent (the env file's
# own COMPOSE_PROFILES is just apps), and the dev machine's, which runs Forgejo and the Jenkins controller. A
# variable in the shell wins over the env file.
for profiles in apps,agent forge,jenkins; do
  COMPOSE_PROFILES=$profiles docker compose --env-file "$secrets/platform.env" -f platform/compose.yml config --quiet
  echo "platform/compose.yml ($profiles) ok"
done
# The build agent of the ci and release users, with a dummy agent.env (scripts/agent.sh checks the real one;
# Compose needs all four values).
cat > "$secrets/agent.env" <<ENV
JENKINS_URL=https://jenkins.example.com
JENKINS_AGENT_NAME=ci
JENKINS_AGENT_SECRET=x
YGG_AGENT_WORKDIR=/home/ci/agent
ENV
docker compose -p yggdrasil-agent-ci --env-file "$secrets/agent.env" -f platform/build-agent.yml config --quiet
echo "platform/build-agent.yml ok"
# Every stack that has its service here, in both modes, with a dummy value for each variable it requires.
# (Stacks based on the application's own docker-compose.yml are validated by deploy.sh on the first deploy.)
for stack in $(python3 scripts/catalog.py applications --deployable); do
  [[ -f "stacks/$stack.yml" ]] || continue
  for mode in proxy ports; do
    [[ -f "stacks/$stack.$mode.yml" ]] || continue
    paths=("stacks/$stack.yml" "stacks/$stack.$mode.yml")
    # `|| true`: a stack with no variable is fine (the job's shell had no pipefail).
    { grep -ohE '[$][{][A-Z_]+' "${paths[@]}" || true; } | cut -c3- | sort -u \
      | sed -E 's/^(.*PORT)$/\1=127.0.0.1:18080/; t; s/$/=x/' > "$secrets/$stack.env"
    APP_DIR=. docker compose --env-file "$secrets/$stack.env" -f "${paths[0]}" -f "${paths[1]}" config --quiet
    echo "stacks/$stack.$mode.yml ok"
  done
done
