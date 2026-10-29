#!/usr/bin/env bash
# Starts, updates or stops this host's platform stack (platform/compose.yml).
#
#     scripts/platform.sh up | down | ps | config [--last-good]
#     scripts/platform.sh logs [--last-good] [service]
#
# Runs on the invoking user's own rootless Docker engine and nothing else: the socket comes from
# DOCKER_HOST, else the user's runtime directory, and a rootful engine is refused. One platform
# stack per host, whatever number of environments the host runs (ENVIRONMENTS). Reads
# the settings from the variables store (rendered, with a last-good copy kept; --last-good, right
# after the command, reads that copy instead), or from <secrets>/platform.env and acme.env on a host
# without a store, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil). Template:
# env/platform.env.example.
set -euo pipefail

die() { echo "platform: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless and use_rootless_engine, shared with agent.sh.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"
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
# Never as root (`sudo platform.sh …`): the platform runs on its user's rootless engine, whose
# container root is that user, so a file root writes in the secrets directory (traefik.yml above all)
# is one Traefik cannot read at its next start. Checked before anything is read or written.
[[ "$(id -u)" != 0 ]] \
  || die "run platform.sh as the user that owns the platform (ygg: log in as ygg, or sudo -iu ygg), not as root (docs/hosts.md)"
cleanup_paths=()
trap 'rm -rf ${cleanup_paths[@]+"${cleanup_paths[@]}"}' EXIT

# A private directory for the env files this run renders from the store: /run/yggdrasil for root;
# else the user's own runtime directory (a tmpfs only they read), else a directory of theirs under
# $TMPDIR. Removed on exit: Compose reads them only while this runs. (Traefik's traefik.yml is not
# here: see render_traefik_config.) Sets render_dir.
run_dir() {
  local run_base=/run/yggdrasil
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
}

# The platform's settings: rendered from the variables store when this host has one
# (docs/variables.md), else platform.env and acme.env. --last-good starts from the copy the last
# successful `up` saved, without opening the store: for when the store or its key is unusable, with
# any command (down, ps, logs and config need the settings too, to read the project).
render_dir=""
from_store=""
if [[ -n "$last_good" ]]; then
  env_file="$secrets/last-good/platform.env"
  acme_file="$secrets/last-good/acme.env"
  traefik_file="$secrets/last-good/traefik.yml"
  [[ -f "$env_file" ]] || die "no last-good copy in $secrets/last-good (one is saved by every successful 'platform.sh up' from the store)"
  [[ -f "$traefik_file" ]] || die "the last-good copy in $secrets/last-good has no traefik.yml (saved by a 'platform.sh up' from before Traefik's config was a file); run 'platform.sh up' once from the store"
  echo "platform: starting from the last-good copy in $secrets/last-good, not the variables store" >&2
elif [[ -f "$secrets/vars.db" ]]; then
  # Only the platform's own values: an application's broken reference is its deploy's problem.
  python3 "$root/scripts/vars.py" check --platform >&2 \
    || die "the variables store failed its check; fix it (scripts/ygg.sh vars check) or run 'platform.sh $command --last-good'"
  run_dir
  from_store=1
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

# Traefik's static configuration (traefik.yml), rendered from the settings: a file because the list
# of domains it serves cannot be written as flags. Compose requires it for every command (it
# interpolates the whole file), so every command renders one -- except --last-good, which reuses the
# copy kept with the rest. Cloudflare's ranges (<secrets>/cloudflare-ips) are read only when
# CLOUDFLARE_PROXIED=true.
#
# The live one is <secrets>/traefik.yml, not the run directory: Compose bind-mounts it into Traefik,
# and the engine mounts it again at every start of the container (a crash, `docker restart`, the
# engine's own restart, every boot), long after this script and its run directory are gone. Only `up`
# replaces it, once the engine is checked (publish_traefik_config): down, ps, logs and config get a
# copy in the run directory, so a `config` after a settings change can't put a configuration in front
# of the next restart that no `up` applied. Mode 600 like the rest of the directory: Traefik runs as
# container root, which is this user. `up` renders next to the live file and renames over it, so a
# failed render or a refused engine never leaves half a file there, and the running Traefik keeps the
# one it started with (a bind mount holds on to the file it was given) until Compose recreates it.
#
# render_traefik_config <directory>: renders into a new file there and points traefik_file at it.
render_traefik_config() {
  local options=() partial
  [[ -f "$secrets/cloudflare-ips" ]] && options=(--cloudflare-ips "$secrets/cloudflare-ips")
  partial=$(mktemp "$1/.traefik.yml.XXXXXX") || die "cannot write Traefik's configuration in $1 (is it yours? run platform.sh as ygg)"
  cleanup_paths+=("$partial")
  # The settings go in the renderer's environment, from the same file Compose reads.
  # shellcheck source=/dev/null
  (set -a; . "$env_file"; python3 "$root/scripts/traefik_config.py" ${options[@]+"${options[@]}"} >"$partial") \
    || die "could not render Traefik's configuration from $env_file (see above)"
  chmod 600 "$partial"
  traefik_file=$partial
}

# The configuration Compose is given: its path, and its hash. The live path is the same at every run,
# so Compose would not see a changed configuration: the hash is a label on the Traefik service
# (compose.yml), and a new hash is what makes `up` recreate Traefik.
use_traefik_config() {
  export YGG_TRAEFIK_CONFIG_FILE="$traefik_file"
  YGG_TRAEFIK_CONFIG_HASH=$(sha256sum "$traefik_file" | cut -d' ' -f1) || die "cannot read $traefik_file"
  export YGG_TRAEFIK_CONFIG_HASH
}

# `up`, once its checks passed: the rendered file becomes the live one.
publish_traefik_config() {
  [[ -n "$last_good" ]] && return
  mv -f "$traefik_file" "$secrets/traefik.yml"
  traefik_file="$secrets/traefik.yml"
  use_traefik_config
}

if [[ -n "$last_good" ]]; then
  :
elif [[ "$command" == up ]]; then
  render_traefik_config "$secrets"
else
  [[ -n "$render_dir" ]] || run_dir
  render_traefik_config "$render_dir"
fi
use_traefik_config

compose() { docker compose --env-file "$env_file" -f "$root/platform/compose.yml" "$@"; }

# COMPOSE_PROFILES of the settings as ",a,b,", so that a profile is found as ",name,".
# shellcheck source=/dev/null
profiles=$(set -a; . "$env_file"; echo ",${COMPOSE_PROFILES:-},")

# A host without the status API (no apps profile: the dev machine) has nothing to discover targets
# from, so Prometheus reads the platform's own, written out.
if [[ "$profiles" == *,apps,* ]]; then
  PROMETHEUS_CONFIG_FILE=prometheus.yml
else
  PROMETHEUS_CONFIG_FILE=prometheus-static.yml
fi
export PROMETHEUS_CONFIG_FILE

# Jenkins needs them and the catalog has them: no second copy in platform.env to drift.
GITHUB_OWNER=$(python3 "$root/scripts/catalog.py" owner) || exit 1
YGGDRASIL_REPOSITORY=$(python3 "$root/scripts/catalog.py" repository) || exit 1
export GITHUB_OWNER YGGDRASIL_REPOSITORY

# setting <name>: a value of the settings file ("" when unset), read the way Compose reads it.
setting() {
  # shellcheck source=/dev/null
  (set -a; . "$env_file"; eval "echo \"\${$1:-}\"")
}

# The git server decides which Configuration as Code files the Jenkins controller reads (the catalog's
# forge block; none means GitHub, as before the git server was configurable): casc.yaml, the forge's own
# file, and the GitHub App's credential when it is used -- always on GitHub, and on Forgejo only with a
# mirror (the GitHub Actions checks and the release copies there).
forge_type=$(python3 "$root/scripts/catalog.py" forge type) || exit 1
FORGE_URL=$(python3 "$root/scripts/catalog.py" forge url) || exit 1
FORGE_JENKINS_USER=$(python3 "$root/scripts/catalog.py" forge jenkins-user) || exit 1
uses_github_app=""
if [[ "$forge_type" == github ]] || python3 "$root/scripts/catalog.py" mirror github >/dev/null 2>&1; then
  uses_github_app=1
fi
# On GitHub, a second, read-only App can be the ci agent's identity (as forge-read is on Forgejo): its
# credential, github-read, is configured when both of its settings are there.
# Read here, before the /dev/null export below would show through an unset key.
github_read_id=$(setting GITHUB_READ_APP_ID)
github_read_key=$(setting GITHUB_READ_APP_KEY_FILE)
uses_github_read=""
if [[ "$forge_type" == github && -n "$github_read_id" && -n "$github_read_key" ]]; then
  uses_github_read=1
fi
# The NuGet API key (the owner-tagged libraries' packages), on either git server, when its file is set.
nuget_key_file=$(setting NUGET_API_KEY_FILE)
YGG_CASC_CONFIG="/etc/jenkins/casc.yaml,/etc/jenkins/casc.$forge_type.yaml"
[[ -z "$uses_github_app" ]] || YGG_CASC_CONFIG+=",/etc/jenkins/casc.github-app.yaml"
[[ -z "$uses_github_read" ]] || YGG_CASC_CONFIG+=",/etc/jenkins/casc.github-read.yaml"
[[ -z "$nuget_key_file" ]] || YGG_CASC_CONFIG+=",/etc/jenkins/casc.nuget.yaml"
# The repositories' own credentials (reportgenerator-license, the signing keys), from JENKINS_CREDENTIALS_DIR: one
# file per credential id, checked and turned into casc.credentials.yaml by scripts/jenkins_credentials.py. Not made
# in the UI: the controller replaces its whole list of system credentials with the configured ones at every start,
# so one made there is gone after the next up that recreates the controller. Rendered for every command (Compose
# interpolates every mount); `up` keeps it as <secrets>/jenkins-credentials.yaml once its checks pass, mode 644 since
# the controller reads it as "other". Its hash, with each secret file's size and modification time (never its
# content: a hash of a short password can be guessed from), is a label on the controller (compose.yml), so a
# changed credential recreates it.
credentials_dir=$(setting JENKINS_CREDENTIALS_DIR)
credentials_rendered="" YGG_JENKINS_CREDENTIALS_FILE=/dev/null YGG_JENKINS_CREDENTIALS_HASH=""
if [[ -n "$credentials_dir" ]]; then
  [[ "$credentials_dir" == /* ]] \
    || die "JENKINS_CREDENTIALS_DIR in $env_file must be an absolute path, not '$credentials_dir'"
  if [[ "$command" == up ]]; then
    credentials_into=$secrets
  else
    [[ -n "$render_dir" ]] || run_dir
    credentials_into=$render_dir
  fi
  credentials_rendered=$(mktemp "$credentials_into/.jenkins-credentials.yaml.XXXXXX") \
    || die "cannot write the credentials' configuration in $credentials_into (is it yours? run platform.sh as ygg)"
  cleanup_paths+=("$credentials_rendered")
  python3 "$root/scripts/jenkins_credentials.py" "$credentials_dir" >"$credentials_rendered" \
    || die "JENKINS_CREDENTIALS_DIR ($credentials_dir) cannot be used, see above"
  if [[ -s "$credentials_rendered" ]]; then
    chmod 644 "$credentials_rendered"
    YGG_CASC_CONFIG+=",/etc/jenkins/casc.credentials.yaml"
    YGG_JENKINS_CREDENTIALS_HASH=$({ cat "$credentials_rendered"; stat -c '%n %s %Y' "$credentials_dir"/*; } | sha256sum | cut -d' ' -f1)
    YGG_JENKINS_CREDENTIALS_FILE=$credentials_rendered
    [[ "$command" == up ]] && YGG_JENKINS_CREDENTIALS_FILE=$secrets/jenkins-credentials.yaml
  else
    credentials_rendered=""
  fi
fi
[[ -n "$credentials_rendered" ]] || export JENKINS_CREDENTIALS_DIR=/dev/null
export YGG_JENKINS_CREDENTIALS_FILE YGG_JENKINS_CREDENTIALS_HASH
export FORGE_URL FORGE_JENKINS_USER YGG_CASC_CONFIG
# Without an App there is no key to mount: /dev/null, not a path Docker would create as a directory.
[[ -n "$uses_github_app" ]] || export GITHUB_APP_KEY_FILE=/dev/null
[[ -n "$uses_github_read" ]] || export GITHUB_READ_APP_KEY_FILE=/dev/null
[[ -n "$nuget_key_file" ]] || export NUGET_API_KEY_FILE=/dev/null

# The profile-only variables compose.yml cannot mark as required (see the note at its top). The forge
# profile has none: Forgejo needs only DOMAIN, and FORGEJO_SSH_PORT defaults to 2222. The jenkins profile
# needs the library version, the secrets of the git server it is configured for, and the work
# directories of the ci and release agents (agents.groovy makes them the nodes' remote roots).
check_profile_variables() {
  local required=()
  if [[ "$profiles" == *,jenkins,* ]]; then
    required+=(JENKINS_URL JENKINS_ADMIN_PASSWORD YGGDRASIL_LIBRARY_VERSION CI_AGENT_WORKDIR RELEASE_AGENT_WORKDIR)
    [[ -z "$uses_github_app" ]] || required+=(GITHUB_APP_ID GITHUB_APP_KEY_FILE)
    [[ "$forge_type" != forgejo ]] || required+=(FORGE_WRITE_TOKEN_FILE FORGE_READ_TOKEN_FILE FORGE_WEBHOOK_SECRET_FILE)
  fi
  [[ "$profiles" == *,agent,* ]] && required+=(JENKINS_URL JENKINS_AGENT_NAME JENKINS_AGENT_SECRET)
  local name value
  for name in "${required[@]}"; do
    # shellcheck source=/dev/null
    value=$(set -a; . "$env_file"; eval "echo \"\${$name:-}\"")
    [[ -n "$value" ]] || die "$name must be set in $env_file for profiles '${profiles//,/ }'"
  done
  if [[ "$profiles" == *,jenkins,* ]]; then
    # The same path in the agent's container and on the host (platform/build-agent.yml), so absolute.
    for name in CI_AGENT_WORKDIR RELEASE_AGENT_WORKDIR; do
      value=$(setting "$name")
      [[ "$value" == /* ]] \
        || die "$name in $env_file must be an absolute path (the YGG_AGENT_WORKDIR of that user's agent.env), not '$value'"
    done
    # Half a read-only App is a typo, not a choice: say so rather than silently using the main App.
    if [[ "$forge_type" == github && -n "$github_read_id$github_read_key" && ( -z "$github_read_id" || -z "$github_read_key" ) ]]; then
      die "set both GITHUB_READ_APP_ID and GITHUB_READ_APP_KEY_FILE in $env_file (the read-only GitHub App), or neither"
    fi
  fi
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

# The environments are the apps profile's business (the status API and the console show them): checked
# when it is on, and an ENVIRONMENTS without it is a platform.env from before the profile existed.
check_apps_profile() {
  if [[ "$profiles" == *,apps,* ]]; then
    check_environments
    return
  fi
  local environments
  # shellcheck source=/dev/null
  environments=$(set -a; . "$env_file"; echo "${ENVIRONMENTS:-${ENVIRONMENT:-}}")
  [[ -z "${environments//[ ,]/}" ]] \
    || die "ENVIRONMENTS is set but COMPOSE_PROFILES lacks apps: add apps to COMPOSE_PROFILES (the status API and console moved to that profile in this release)"
}

case "$command" in
  up)
    check_apps_profile
    check_profile_variables
    use_rootless_engine
    for network in edge telemetry; do
      docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null
    done
    # The agent's deploy locks (scripts/deploy.sh): the agent's container root is this user.
    install -d -m 700 "$secrets/locks"
    publish_traefik_config
    if [[ -n "$credentials_rendered" ]]; then
      mv -f "$credentials_rendered" "$secrets/jenkins-credentials.yaml"
    else
      rm -f "$secrets/jenkins-credentials.yaml"
    fi
    compose up --detach --build --remove-orphans --wait
    compose ps
    if [[ -n "$from_store" ]]; then
      install -d -m 700 "$secrets/last-good"
      install -m 600 "$env_file" "$acme_file" "$traefik_file" "$secrets/last-good/"
    fi
    ;;
  down) use_rootless_engine; compose down ;;
  ps) use_rootless_engine; compose ps ;;
  logs) use_rootless_engine; compose logs --follow --tail 200 "$@" ;;
  config) use_rootless_engine; compose config ;;
esac
