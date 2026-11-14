#!/usr/bin/env bash
# Starts, updates or stops this host's platform stack (platform/compose.yml).
#
#     scripts/platform.sh up | down | ps | config [--last-good]
#     scripts/platform.sh logs [--last-good] [service]
#     scripts/platform.sh update-plugins [--last-good] [--now]
#
# update-plugins (the Linux DevOps Server: the jenkins profile) rebuilds the Jenkins controller's image with
# the latest compatible plugins, prints which change, then puts Jenkins in quiet-down and waits until it can restart
# safely, as Jenkins' own safe restart does (pipelines pause and carry on after the restart; other builds finish
# first), and brings the platform up on the new image, as `up` does. --now skips the wait. With nothing to change it
# leaves Jenkins running as it is.
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
usage="usage: platform.sh up | down | ps | config [--last-good], or platform.sh logs [--last-good] [service], or platform.sh update-plugins [--last-good] [--now]"
command=${1:-}
(($#)) && shift
last_good=""
if [[ "${1:-}" == --last-good ]]; then
  last_good=1
  shift
fi
[[ "$command" =~ ^(up|down|ps|logs|config|update-plugins)$ ]] || die "$usage"
now=""
if [[ "$command" == update-plugins && "${1:-}" == --now ]]; then
  now=1
  shift
fi
[[ "$command" == logs || $# -eq 0 ]] || die "$usage"
# up and update-plugins bring the platform up: they publish what the other commands only render.
brings_up=""
if [[ "$command" == up || "$command" == update-plugins ]]; then
  brings_up=1
fi
# Never as root (`sudo platform.sh …`): the platform runs on its user's rootless engine, whose
# container root is that user, so a file root writes in the secrets directory (traefik.yml above all)
# is one Traefik cannot read at its next start. Checked before anything is read or written.
[[ "$(id -u)" != 0 ]] \
  || die "run platform.sh as the user that owns the platform (ygg: log in as ygg, or sudo -iu ygg), not as root (docs/hosts.md)"
cleanup_paths=()
# update-plugins: whether it tagged the image the controller ran as previous_image (the build moves the image's
# own tag, and Docker's containerd image store deletes an image the moment it has no tag left), whether that image
# is to be tagged back if this stops on the way (so the next `up` does not restart Jenkins on plugins nobody waited
# for), and whether Jenkins is in a quiet-down this run started.
kept_previous=""
restore_previous=""
quieted=""
on_exit() {
  if [[ -n "$quieted" ]]; then
    echo "platform: cancelling the quiet-down: Jenkins starts builds again" >&2
    jenkins_cli cancel-quiet-down >&2 \
      || echo "platform: could not cancel the quiet-down; cancel it at the top of Manage Jenkins" >&2
  fi
  if [[ -n "$restore_previous" ]]; then
    docker tag "$previous_image" "$jenkins_image" \
      || echo "platform: could not tag $previous_image back as $jenkins_image" >&2
  fi
  if [[ -n "$kept_previous" ]]; then
    docker image rm "$previous_image" >/dev/null 2>&1 || true
  fi
  rm -rf ${cleanup_paths[@]+"${cleanup_paths[@]}"}
}
trap on_exit EXIT

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
elif [[ -n "$brings_up" ]]; then
  render_traefik_config "$secrets"
else
  [[ -n "$render_dir" ]] || run_dir
  render_traefik_config "$render_dir"
fi
use_traefik_config

compose() { docker compose --env-file "$env_file" -f "$root/platform/compose.yml" "$@"; }

# When update-plugins last fetched the Jenkins plugins, passed to every build of the controller's image (the
# Dockerfile's PLUGINS_REFRESHED says why); none before the first.
plugins_stamp_file=$secrets/jenkins-plugins-refreshed
YGG_JENKINS_PLUGINS_REFRESHED=""
if [[ -f "$plugins_stamp_file" ]]; then
  YGG_JENKINS_PLUGINS_REFRESHED=$(<"$plugins_stamp_file")
fi
export YGG_JENKINS_PLUGINS_REFRESHED
# The controller's image, as compose.yml names it, and the tag update-plugins keeps the one it ran under meanwhile.
jenkins_image=yggdrasil/jenkins:local
previous_image=yggdrasil/jenkins:previous

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
  if [[ -n "$brings_up" ]]; then
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
    [[ -n "$brings_up" ]] && YGG_JENKINS_CREDENTIALS_FILE=$secrets/jenkins-credentials.yaml
  else
    credentials_rendered=""
  fi
fi
[[ -n "$credentials_rendered" ]] || export JENKINS_CREDENTIALS_DIR=/dev/null
export YGG_JENKINS_CREDENTIALS_FILE YGG_JENKINS_CREDENTIALS_HASH

# The files the controller mounts from this checkout (compose.yml), which it reads only when it starts: its
# Configuration as Code, the Jenkinsfile and job scripts kept in Jenkins, the catalog and the agents script. A
# `git pull` changes them under the same paths, so Compose would leave the controller running on the old ones
# (and a single-file bind mount keeps showing the old file until the container starts again): 0.13.0's CSP
# setting reached no controller that way. Their hash is a label on the controller (compose.yml), so a changed
# file recreates it. scripts/test_jenkins_config.py keeps this list equal to the mounts.
jenkins_config_files=(
  platform/jenkins/controller/casc.yaml
  platform/jenkins/controller/casc.forgejo.yaml
  platform/jenkins/controller/casc.github.yaml
  platform/jenkins/controller/casc.github-app.yaml
  platform/jenkins/controller/casc.github-read.yaml
  platform/jenkins/controller/casc.nuget.yaml
  platform/jenkins/controller/init.groovy.d/agents.groovy
  jenkins/Jenkinsfile.default
  jenkins/actions-release.groovy
  jenkins/release-resume.groovy
  jenkins/tag-check.groovy
  catalog.yaml
)
YGG_JENKINS_CONFIG_HASH=$(cd "$root" && sha256sum -- "${jenkins_config_files[@]}" | sha256sum | cut -d' ' -f1) \
  || die "cannot read the controller's configuration in $root"
export YGG_JENKINS_CONFIG_HASH
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

# up, once its checks passed: publishes what it rendered and brings the stack up.
bring_up() {
  local network
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
  # The controller's container before and after: a new one may have missed the jobs it created (load_new_jobs).
  local controller=""
  if [[ "$profiles" == *,jenkins,* ]]; then
    controller=$(compose ps --quiet jenkins 2>/dev/null) || controller=""
  fi
  compose up --detach --build --remove-orphans --wait
  compose ps
  if [[ "$profiles" == *,jenkins,* && "$(compose ps --quiet jenkins 2>/dev/null || true)" != "$controller" ]]; then
    load_new_jobs
  fi
  if [[ -n "$from_store" ]]; then
    install -d -m 700 "$secrets/last-good"
    install -m 600 "$env_file" "$acme_file" "$traefik_file" "$secrets/last-good/"
  fi
}

# image_id <image>: the image's id, "" when there is none.
image_id() { docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

# plugin_versions <image>: "<plugin> <version>" per line, sorted, of the plugins the image installs (each one's
# manifest, read the way the image's own jenkins-support reads it).
plugin_versions() {
  # shellcheck disable=SC2016 # expanded by the container's shell
  docker run --rm --entrypoint sh "$1" -c 'cd /usr/share/jenkins/ref/plugins && for p in *.jpi; do
      printf "%s %s\n" "${p%.jpi}" "$(unzip -p "$p" META-INF/MANIFEST.MF | tr -d "\r" | sed -n "s/^Plugin-Version: //p")"
    done' | sort
}

# plugin_changes <before> <after>: one line per plugin whose version differs between two plugin_versions lists.
plugin_changes() {
  awk 'NF == 0 { next }
       FILENAME == ARGV[1] { before[$1] = $2; next }
       !($1 in before) { print "  " $1 ": new, " $2; next }
       before[$1] != $2 { print "  " $1 ": " before[$1] " -> " $2 }
       { delete before[$1] }
       END { for (p in before) print "  " p ": removed (was " before[p] ")" }' \
    <(printf '%s\n' "$1") <(printf '%s\n' "$2") | sort
}

# jenkins_cli <command> [<argument>...]: the Jenkins CLI, in the running controller, as its admin; its standard
# input goes to the command. The password comes from the container's own environment: never on this host's command
# line. -http, because the CLI's default WebSocket refuses a connection whose origin (localhost) is not the
# configured JENKINS_URL.
jenkins_cli() {
  # shellcheck disable=SC2016 # expanded by the container's shell
  compose exec -T jenkins sh -c 'cd /tmp \
      && { [ -s jenkins-cli.jar ] || curl -fsSo jenkins-cli.jar http://localhost:8080/jnlpJars/jenkins-cli.jar; } \
      && JENKINS_USER_ID=$JENKINS_ADMIN_ID JENKINS_API_TOKEN=$JENKINS_ADMIN_PASSWORD \
         exec java -jar jenkins-cli.jar -s http://localhost:8080/ -http "$@"' jenkins-cli "$@"
}

# blocking_builds: how many busy executors keep Jenkins from restarting safely, by Jenkins' own rule (core's
# RestartListener.Default, what a safe restart waits for). A pipeline does not: in quiet-down it pauses at its next
# step and carries on after the restart, a running sh step going on on its agent meanwhile. A freestyle build does,
# until it ends.
blocking_builds() {
  jenkins_cli groovy = <<'GROOVY'
def executors = jenkins.model.Jenkins.get().computers.findAll { it.online }.collectMany { it.allExecutors }
println executors.count { it.busy && (it.asynchronousExecution == null || it.asynchronousExecution.blocksRestart()) }
GROOVY
}

# How long update-plugins waits for Jenkins to be ready to restart, and how often it looks (seconds; the tests
# shorten both).
quiet_timeout=${YGG_JENKINS_QUIET_TIMEOUT:-1800}
quiet_poll=${YGG_JENKINS_QUIET_POLL:-15}

# wait_for_builds: puts the running controller in quiet-down (queued and new builds wait, pipelines pause at their
# next step) and returns once nothing blocks a restart. Gives up after quiet_timeout, and the quiet-down is
# cancelled on the way out.
wait_for_builds() {
  if [[ -z "$(compose ps --quiet jenkins)" ]]; then
    return 0
  fi
  jenkins_cli quiet-down >&2 \
    || die "could not put Jenkins in quiet-down to wait for its builds; 'update-plugins --now' restarts it without waiting"
  quieted=1
  local waited=0 running
  while :; do
    running=$(blocking_builds) \
      || die "could not ask Jenkins which builds it runs; 'update-plugins --now' restarts it without waiting"
    [[ "$running" =~ ^[0-9]+$ ]] \
      || die "Jenkins answered '$running' for its running builds; 'update-plugins --now' restarts it without waiting"
    if ((running == 0)); then
      return 0
    fi
    if ((waited >= quiet_timeout)); then
      die "$running build(s) still running after $((quiet_timeout / 60)) minutes; nothing was changed. Run update-plugins again later, or with --now to restart Jenkins under them"
    fi
    echo "platform: waiting for $running build(s) that cannot survive a restart (pipelines pause and carry on after it); Jenkins starts no new ones meanwhile" >&2
    sleep "$quiet_poll"
    waited=$((waited + quiet_poll))
  done
}

# How long `up` waits for a new controller to finish starting before it looks for the jobs it missed, and how often
# it asks (seconds; the tests shorten both).
start_timeout=${YGG_JENKINS_START_TIMEOUT:-600}
start_poll=${YGG_JENKINS_START_POLL:-5}

# load_new_jobs: once a new controller has started, has it load the jobs it created but dropped, by reloading its
# Configuration as Code. The job script of casc.yaml runs early in Jenkins' startup, before the job directories are
# loaded but after Jenkins listed the ones it will load, and core then drops every item it did not load from disk
# ("Cleaning up obsolete items deleted from the disk"): a new catalog entry's job was written and dropped, and came
# back only at the next start. A reload runs the same script once Jenkins is up, which registers it; for the jobs
# Jenkins has, it changes nothing. It runs only when a job directory has no job, and it never stops the platform:
# a failure is a warning, with what to do by hand.
load_new_jobs() {
  local waited=0 answer
  while :; do
    # The answer's first line is `starting` until Jenkins reaches COMPLETED, then `ready`, followed by one
    # `registered <job>` per job the reload brought in and one `unregistered <job>` per job directory still without
    # a job. A refused CLI (Jenkins not answering yet) is an empty answer.
    answer=$(jenkins_cli groovy = 2>/dev/null <<'GROOVY'
def j = jenkins.model.Jenkins.get()
if (j.initLevel != hudson.init.InitMilestone.COMPLETED) {
  println 'starting'
  return
}
def unloaded = {
  (new File(j.rootDir, 'jobs').listFiles() ?: []).findAll { new File(it, 'config.xml').isFile() }*.name
    .findAll { j.getItem(it) == null }.sort()
}
def missing = unloaded()
if (missing) {
  io.jenkins.plugins.casc.ConfigurationAsCode.get().configure()
}
def left = unloaded()
println 'ready'
(missing - left).each { println 'registered ' + it }
left.each { println 'unregistered ' + it }
GROOVY
    ) || answer=""
    [[ "$answer" != ready* ]] || break
    if ((waited >= start_timeout)); then
      echo "platform: could not check that Jenkins loaded every job: it did not answer as started within $((start_timeout / 60)) minutes. If a new catalog entry has no job, restart the controller once it is idle: docker restart yggdrasil-jenkins-1" >&2
      return 0
    fi
    sleep "$start_poll"
    waited=$((waited + start_poll))
  done
  local line unregistered=()
  while IFS= read -r line; do
    case "$line" in
      "registered "*) echo "platform: Jenkins loaded the new job ${line#registered }" >&2 ;;
      "unregistered "*) unregistered+=("${line#unregistered }") ;;
    esac
  done <<<"$answer"
  if ((${#unregistered[@]})); then
    echo "platform: Jenkins has no job for these job directories even after reloading its configuration: ${unregistered[*]}. Look for the reason in its log (scripts/platform.sh logs jenkins); restarting the controller once it is idle loads what it can: docker restart yggdrasil-jenkins-1" >&2
  fi
  return 0
}

case "$command" in
  up)
    check_apps_profile
    check_profile_variables
    use_rootless_engine
    bring_up
    ;;
  update-plugins)
    [[ "$profiles" == *,jenkins,* ]] \
      || die "this host does not run Jenkins (COMPOSE_PROFILES in $env_file has no jenkins): update-plugins is for the Linux DevOps Server"
    check_apps_profile
    check_profile_variables
    use_rootless_engine
    old_image=$(image_id "$jenkins_image")
    old_plugins=""
    if [[ -n "$old_image" ]]; then
      old_plugins=$(plugin_versions "$jenkins_image") || die "could not read the plugins of $jenkins_image"
      docker tag "$jenkins_image" "$previous_image"
      kept_previous=1
      restore_previous=1
    fi
    echo "platform: building $jenkins_image with the latest compatible plugins" >&2
    YGG_JENKINS_PLUGINS_REFRESHED=$(date -u +%Y%m%dT%H%M%SZ)
    compose build jenkins
    new_plugins=$(plugin_versions "$jenkins_image") || die "could not read the plugins of the new $jenkins_image"
    if [[ -n "$old_image" && "$new_plugins" == "$old_plugins" ]]; then
      # on_exit tags the image the controller runs back, so the next `up` does not recreate it for nothing (and
      # the build's own image goes, untagged).
      echo "platform: the Jenkins plugins are already up to date; Jenkins was left running" >&2
      exit 0
    fi
    if [[ -n "$old_image" ]]; then
      echo "Jenkins plugins that change:"
      plugin_changes "$old_plugins" "$new_plugins"
    fi
    [[ -n "$now" ]] || wait_for_builds
    # From here on the new image is the one: every `up` builds it from the same layer.
    printf '%s\n' "$YGG_JENKINS_PLUGINS_REFRESHED" >"$plugins_stamp_file"
    restore_previous=""
    bring_up
    quieted=""
    ;;
  down) use_rootless_engine; compose down ;;
  ps) use_rootless_engine; compose ps ;;
  logs) use_rootless_engine; compose logs --follow --tail 200 "$@" ;;
  config) use_rootless_engine; compose config ;;
esac
