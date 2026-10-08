#!/usr/bin/env bash
# Builds and (re)deploys one application stack on this host's Docker engine, and rolls back to the
# image that was running if the new one does not become healthy.
#
#     scripts/deploy.sh <environment> <stack> <app-dir> <version>
#
#   environment  an environment id from catalog.yaml that the application deploys to
#   stack        an application id from catalog.yaml
#   app-dir      a checkout of the stack's repository at the commit to deploy
#   version      the version, e.g. 1.4.0-3f2a9c1 (Jenkins passes <release>-<short sha>)
#
# Several environments can run on one Docker engine, so everything is named per environment:
#
#   Compose project   <stack>-<environment>, e.g. heimdall-api-production
#   image tag         <environment>-<version>, e.g. production-1.4.0-3f2a9c1, exported as IMAGE_TAG
#                     and API_IMAGE_TAG. The same commit is built once per environment (a web front
#                     end compiles environment values into its image), and rollback and pruning only
#                     ever look at this environment's tags.
#   network alias     <stack>.<environment> on the edge and telemetry networks: the proxy overlays
#                     set it from YGG_ENVIRONMENT, which this script exports
#
# Every container of the stack is labelled yggdrasil.environment, yggdrasil.version (the version
# without the environment, and without the commit when it ends with it), yggdrasil.commit and
# yggdrasil.deployed_at, which is where the status API, the console and Loki read "what is running"
# from.
#
# Jenkins runs exactly this; running it by hand does the same thing.
#
# The environment's options come from catalog.yaml, resolved for this application (defaults, then
# the environment, then the application's override) by scripts/catalog.py: its mode, how long to
# wait for health (waitTimeout), how many images to keep (keepImages) and whether it runs on demand
# (onDemand). DEPLOY_WAIT_TIMEOUT and DEPLOY_KEEP_IMAGES in the environment override the two numbers
# for one run.
#
# On demand: when the environment is onDemand and nothing of the stack was running before the deploy
# (the environment is switched off, or this is the first deploy), the new version is still started
# and waited for -- a broken build is caught and rolled back as anywhere else -- and then stopped
# again, so a push to develop does not switch the development environment on. DEPLOY_START=1 leaves
# it running instead: Jenkins sets it for a deploy asked for by hand (DEPLOY_TO). scripts/ygg.sh env
# start <environment> starts it later.
#
# Compose files, in order:
#   stacks/<stack>.yml                  the service definition when the repository has no Compose
#                                       file of its own; otherwise <app-dir>/docker-compose.yml
#   stacks/<stack>.<mode>.yml           proxy: Traefik labels, the edge and telemetry networks, no
#                                       host ports; ports: host ports on 127.0.0.1, no Traefik
#   stacks/<stack>.<environment>.yml    optional: anything only this environment needs (replicas,
#                                       resource limits, an extra volume...)
#
# The variables come from the variables store when <secrets>/vars.db exists (rendered into a private
# temporary file), else from <secrets>/<environment>/<stack>.env, secrets being $YGG_SECRETS_DIR
# (default /etc/yggdrasil). Start that file from the application repository's own env template;
# examples for the sample applications are in docs/examples/docker-desktop-and-vps/env/. Its path is
# exported as APP_ENV_FILE, for stacks that hand the whole file to the container (env_file:, as the
# stacks scripts/ygg.sh generates do).
set -euo pipefail

die() { echo "deploy: $*" >&2; exit 1; }

[[ $# -eq 4 ]] || die "usage: deploy.sh <environment> <stack> <app-dir> <version>"

environment=$1
stack=$2
app_dir=$(cd "$3" && pwd) || die "no such directory: $3"
version=$4

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}

[[ "$stack" =~ ^[a-z0-9-]+$ ]] || die "invalid stack name '$stack'"
[[ "$environment" =~ ^[a-z0-9-]+$ ]] || die "invalid environment name '$environment'"

# Fails, naming the problem, when the stack is not in the catalog or does not deploy here.
option() { python3 "$root/scripts/catalog.py" get "$stack" "$environment" "$1"; }
mode=$(option mode) || exit 1
wait_timeout=${DEPLOY_WAIT_TIMEOUT:-$(option waitTimeout)}
keep=${DEPLOY_KEEP_IMAGES:-$(option keepImages)}
on_demand=$(option onDemand)
[[ "$version" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid version '$version'"
project="$stack-$environment"
tag="$environment-$version"
# The application's variables: rendered from the variables store when this machine has one
# (docs/variables.md), else today's env file. The rendered file lives in a private directory and is
# removed when this script exits.
cleanup_paths=()
trap 'rm -rf ${cleanup_paths[@]+"${cleanup_paths[@]}"}' EXIT
if [[ -f "$secrets/vars.db" ]]; then
  python3 "$root/scripts/vars.py" check "$stack" "$environment" >&2 \
    || die "the variables store failed its check (scripts/ygg.sh vars check)"
  run_base=/run/yggdrasil
  mkdir -p "$run_base" 2>/dev/null && [[ -w "$run_base" ]] || run_base="${TMPDIR:-/tmp}/yggdrasil-$(id -u)"
  mkdir -p "$run_base" && chmod 700 "$run_base"
  render_dir=$(mktemp -d "$run_base/deploy.XXXXXX")
  cleanup_paths+=("$render_dir")
  env_file="$render_dir/$stack-$environment.env"
  (umask 077 && python3 "$root/scripts/vars.py" render "$stack" "$environment" >"$env_file") \
    || die "could not render the variables of $stack in $environment"
else
  env_file="$secrets/$environment/$stack.env"
  [[ -f "$env_file" && -r "$env_file" ]] || die "missing or unreadable env file $env_file: create it (docs/setup.md step 11); under Jenkins it must be readable by the agent (uid 1000, or the docker group)"
  echo "deploy: reading $env_file; move to the variables store with scripts/ygg.sh vars init && scripts/ygg.sh vars import --all" >&2
fi

# One deploy of a stack to an environment at a time: two at once (two release branches pushed
# together, or a push next to a manual deploy) would each take the other's half-started containers
# for the version to roll back to. The lock is a file in <secrets>/locks, which the Jenkins agent
# mounts read-write, so the agent and a deploy by hand on the host exclude each other. Released when
# this script exits.
lock_file="$secrets/locks/$stack-$environment.lock"
# An existing lock file is only opened for reading, so it may belong to the other user.
if command -v flock >/dev/null 2>&1 \
  && { mkdir -p "$secrets/locks" 2>/dev/null; [[ -e "$lock_file" ]] || (umask 002 && : >>"$lock_file") 2>/dev/null; } \
  && [[ -r "$lock_file" ]]; then
  exec {lock}<"$lock_file"
  locked=0
  flock --nonblock --conflict-exit-code 75 "$lock" || locked=$?
  if ((locked == 75)); then
    echo "deploy: another deploy of $stack to $environment is running; waiting for it to finish" >&2
    flock "$lock"
  elif ((locked != 0)); then
    echo "deploy: cannot lock $lock_file; going on unlocked" >&2
  fi
elif command -v flock >/dev/null 2>&1; then
  echo "deploy: cannot create $lock_file; going on unlocked" >&2
fi

files=()
if [[ -f "$root/stacks/$stack.yml" ]]; then
  files+=(-f "$root/stacks/$stack.yml")
elif [[ -f "$app_dir/docker-compose.yml" ]]; then
  files+=(-f "$app_dir/docker-compose.yml")
else
  die "neither stacks/$stack.yml nor $app_dir/docker-compose.yml exists"
fi
[[ -f "$root/stacks/$stack.$mode.yml" ]] && files+=(-f "$root/stacks/$stack.$mode.yml")
[[ -f "$root/stacks/$stack.$environment.yml" ]] && files+=(-f "$root/stacks/$stack.$environment.yml")

# Read by the Compose files. Shell variables win over the env file, so the tag cannot be overridden
# by a stale value left in it. API_IMAGE_TAG is the name the API repositories' own Compose files use.
# YGG_ENVIRONMENT names the network aliases and the Traefik routers of the proxy overlays.
export APP_DIR="$app_dir"
export APP_ENV_FILE="$env_file"
export YGG_ENVIRONMENT="$environment"
export IMAGE_TAG="$tag"
export API_IMAGE_TAG="$tag"

compose() { docker compose --project-name "$project" --env-file "$env_file" "${files[@]}" "$@"; }

# What the status API reports as the deployment. The version is <release>-<commit> when Jenkins
# deploys; a hand deploy may use any version, and then all of it is the release.
# Exactly 7 characters, as Jenkins writes it (--short can return more to stay unambiguous).
commit=$(git -C "$app_dir" rev-parse HEAD 2>/dev/null | cut -c1-7 || true)
release=$version
[[ -n "$commit" && "$version" == *"-$commit" ]] && release=${version%-"$commit"}
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Labels for every service of the stack, whatever the stack is: generated rather than written into
# each stacks/*.yml, so a new application gets them without doing anything.
# write_labels <version> <commit> <deployed-at>
labels_file=$(mktemp)
cleanup_paths+=("$labels_file")
services=$(compose config --services)
write_labels() {
  {
    echo "services:"
    for service in $services; do
      echo "  $service:"
      echo "    labels:"
      echo "      yggdrasil.environment: \"$environment\""
      echo "      yggdrasil.version: \"$1\""
      echo "      yggdrasil.commit: \"$2\""
      echo "      yggdrasil.deployed_at: \"$3\""
    done
  } >"$labels_file"
}
write_labels "$release" "$commit" "$deployed_at"
files+=(-f "$labels_file")

# The images this deploy builds and tags (<stack>:<environment>-<version>), as opposed to the ones
# a stack also runs as they are (a database's postgres:16): only those are rolled back and pruned.
repositories=()
while read -r image; do
  if [[ "$image" == *":$tag" ]]; then repositories+=("${image%":$tag"}"); fi
done < <(compose config --images | sort -u)

# Whether anything of the stack runs now: an on-demand environment that is switched off stays off.
# The whole list, not `| head -n1`: head closing the pipe early would kill docker with SIGPIPE, and
# pipefail would then stop this script.
was_running=$(docker ps --filter "label=com.docker.compose.project=$project" --format '{{.ID}}')

# The deployment to come back to: the newest container of the project, running or not (a stopped
# on-demand environment rolls back too), whose image is one of those repositories with this
# environment's tag. Not just any container of the project, whose tag ("16") would mean nothing.
# Empty on a first deploy.
previous="" previous_tag="" previous_container=""
while read -r id image; do
  for repository in "${repositories[@]}"; do
    if [[ "$image" == "$repository:$environment-"* ]]; then
      previous=$image previous_tag=${image#"$repository":} previous_container=$id
      break 2
    fi
  done
done < <(docker ps --all --filter "label=com.docker.compose.project=$project" --format '{{.ID}} {{.Image}}')
previous_labels=()
if [[ -n "$previous" ]]; then
  for label in version commit deployed_at; do
    previous_labels+=("$(docker inspect --format "{{index .Config.Labels \"yggdrasil.$label\"}}" "$previous_container" | sed "s/<no value>//")")
  done
fi

echo "deploy: $stack $version to $environment as project $project (previous: ${previous:-none}${previous:+, ${was_running:+running}${was_running:-stopped}})"

# After a deploy or a rollback: an on-demand environment that was off is switched off again.
stop_if_on_demand() {
  [[ "$on_demand" == true && -z "$was_running" && "${DEPLOY_START:-}" != 1 ]] || return 0
  compose stop || echo "deploy: could not stop $project again" >&2
  echo "deploy: $environment is on demand and $stack was not running: stopped it again (scripts/ygg.sh env start $environment to use it)"
}

if [[ "$mode" == proxy ]]; then
  for network in edge telemetry; do
    docker network inspect "$network" >/dev/null 2>&1 \
      || die "network '$network' is missing: run scripts/platform.sh up on this host first"
  done
fi

compose config --quiet
compose build --pull

if compose up --detach --remove-orphans --wait --wait-timeout "$wait_timeout"; then
  echo "deploy: $stack $version is healthy"
  stop_if_on_demand
else
  echo "deploy: $stack $version did not become healthy" >&2
  compose ps >&2 || true
  compose logs --tail 100 >&2 || true

  if [[ -n "$previous_tag" && "$previous_tag" != "$tag" ]]; then
    # The image is still local (see the pruning below), so no build: this is the exact image that
    # was running before. Database migrations the failed version applied are NOT undone -- the
    # applications' migrations must stay backward compatible for one release for this to be safe.
    echo "deploy: rolling back to $previous_tag" >&2
    # Back to what the previous deployment said about itself, not the failed one's labels.
    write_labels "${previous_labels[@]}"
    IMAGE_TAG="$previous_tag" API_IMAGE_TAG="$previous_tag" \
      compose up --detach --no-build --wait --wait-timeout "${DEPLOY_ROLLBACK_WAIT_TIMEOUT:-300}" \
      || echo "deploy: ROLLBACK FAILED -- $stack is down in $environment" >&2
  fi
  # Rolled back, failed to, or had nothing to roll back to: an on-demand environment that was off is
  # switched off again either way, rather than left crash-looping where nobody looks.
  stop_if_on_demand
  exit 1
fi

# Keep the last few images of this stack in this environment for rollbacks; drop older ones. Only
# this environment's tags (<environment>-*) count, so a busy development never prunes production's
# rollback images -- nor those of an environment whose id starts with this one's and a dash
# (pre-prod-* is not pre's).
longer=()
while read -r other; do
  if [[ "$other" == "$environment-"* ]]; then longer+=("$other-"); fi
done < <(python3 "$root/scripts/catalog.py" environments)
for repository in "${repositories[@]}"; do
  docker image ls "$repository" --format '{{.CreatedAt}}\t{{.Tag}}' | sort -r \
    | while IFS=$'\t' read -r _ image_tag; do
      [[ "$image_tag" == "$environment-"* ]] || continue
      for prefix in ${longer[@]+"${longer[@]}"}; do [[ "$image_tag" == "$prefix"* ]] && continue 2; done
      echo "$repository:$image_tag"
    done \
    | tail -n +"$((keep + 1))" | xargs -r docker image rm >/dev/null 2>&1 || true
done
