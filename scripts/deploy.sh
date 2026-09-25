#!/usr/bin/env bash
# Builds and (re)deploys one application stack on this host's Docker engine, and rolls back to the
# image that was running if the new one does not become healthy.
#
#     scripts/deploy.sh <environment> <stack> <app-dir> <version>
#
#   environment  an environment id from catalog.yaml that the application deploys to
#   stack        an application id from catalog.yaml
#   app-dir      a checkout of the stack's repository at the commit to deploy
#   version      the image tag, e.g. 1.4.0-3f2a9c1 (Jenkins passes <release>-<short sha>)
#
# Every container of the stack is labelled yggdrasil.version, yggdrasil.commit and
# yggdrasil.deployed_at, which is where the status API and the console read "what is running" from.
#
# Jenkins runs exactly this; running it by hand does the same thing.
#
# The environment's options come from catalog.yaml, resolved for this application (defaults, then
# the environment, then the application's override) by scripts/catalog.py: its mode, how long to
# wait for health (waitTimeout) and how many images to keep (keepImages). DEPLOY_WAIT_TIMEOUT and
# DEPLOY_KEEP_IMAGES in the environment override the last two for one run.
#
# Compose files, in order:
#   stacks/<stack>.yml                  the service definition when the repository has no Compose
#                                       file of its own; otherwise <app-dir>/docker-compose.yml
#   stacks/<stack>.<mode>.yml           proxy: Traefik labels, the edge and telemetry networks, no
#                                       host ports; ports: host ports on 127.0.0.1, no Traefik
#   stacks/<stack>.<environment>.yml    optional: anything only this environment needs (replicas,
#                                       resource limits, an extra volume...)
#
# The env file is <secrets>/<environment>/<stack>.env, secrets being $YGG_SECRETS_DIR (default
# /etc/yggdrasil). Start it from the application repository's own env template; examples for the
# sample applications are in docs/examples/docker-desktop-wsl-vps/env/.
set -euo pipefail

die() { echo "deploy: $*" >&2; exit 1; }

[[ $# -eq 4 ]] || die "usage: deploy.sh <environment> <stack> <app-dir> <version>"

environment=$1
stack=$2
app_dir=$(cd "$3" && pwd) || die "no such directory: $3"
version=$4

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}
env_file="$secrets/$environment/$stack.env"

[[ "$stack" =~ ^[a-z0-9-]+$ ]] || die "invalid stack name '$stack'"
[[ "$environment" =~ ^[a-z0-9-]+$ ]] || die "invalid environment name '$environment'"

# Fails, naming the problem, when the stack is not in the catalog or does not deploy here.
option() { python3 "$root/scripts/catalog.py" get "$stack" "$environment" "$1"; }
mode=$(option mode) || exit 1
wait_timeout=${DEPLOY_WAIT_TIMEOUT:-$(option waitTimeout)}
keep=${DEPLOY_KEEP_IMAGES:-$(option keepImages)}
[[ "$version" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid version '$version'"
[[ -f "$env_file" ]] || die "missing or unreadable env file $env_file: create it (docs/setup.md step 11); under Jenkins it must be readable by the agent (uid 1000, or the docker group)"

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
export APP_DIR="$app_dir"
export IMAGE_TAG="$version"
export API_IMAGE_TAG="$version"

compose() { docker compose --project-name "$stack" --env-file "$env_file" "${files[@]}" "$@"; }

# What the status API reports as the deployment. The tag is <version>-<commit> when Jenkins deploys;
# a hand deploy may use any tag, and then the whole tag is the version.
commit=$(git -C "$app_dir" rev-parse --short=7 HEAD 2>/dev/null || true)
release=$version
[[ -n "$commit" && "$version" == *"-$commit" ]] && release=${version%-"$commit"}
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Labels for every service of the stack, whatever the stack is: generated rather than written into
# each stacks/*.yml, so a new application gets them without doing anything.
# write_labels <version> <commit> <deployed-at>
labels_file=$(mktemp)
trap 'rm -f "$labels_file"' EXIT
services=$(compose config --services)
write_labels() {
  {
    echo "services:"
    for service in $services; do
      echo "  $service:"
      echo "    labels:"
      echo "      yggdrasil.version: \"$1\""
      echo "      yggdrasil.commit: \"$2\""
      echo "      yggdrasil.deployed_at: \"$3\""
    done
  } >"$labels_file"
}
write_labels "$release" "$commit" "$deployed_at"
files+=(-f "$labels_file")

# What is running now, to come back to. Empty on a first deploy.
previous=$(docker ps --filter "label=com.docker.compose.project=$stack" --format '{{.Image}}' | head -n1)
previous_tag=${previous##*:}
[[ "$previous" == *:* ]] || previous_tag=""
previous_labels=()
if [[ -n "$previous" ]]; then
  previous_container=$(docker ps --filter "label=com.docker.compose.project=$stack" --format '{{.ID}}' | head -n1)
  for label in version commit deployed_at; do
    previous_labels+=("$(docker inspect --format "{{index .Config.Labels \"yggdrasil.$label\"}}" "$previous_container" | sed "s/<no value>//")")
  done
fi

echo "deploy: $stack $version to $environment (running: ${previous:-nothing})"

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
else
  echo "deploy: $stack $version did not become healthy" >&2
  compose ps >&2 || true
  compose logs --tail 100 >&2 || true

  if [[ -n "$previous_tag" && "$previous_tag" != "$version" ]]; then
    # The image is still local (see the pruning below), so no build: this is the exact image that
    # was running before. Database migrations the failed version applied are NOT undone -- the
    # applications' migrations must stay backward compatible for one release for this to be safe.
    echo "deploy: rolling back to $previous_tag" >&2
    # Back to what the previous deployment said about itself, not the failed one's labels.
    write_labels "${previous_labels[@]}"
    IMAGE_TAG="$previous_tag" API_IMAGE_TAG="$previous_tag" \
      compose up --detach --no-build --wait --wait-timeout "${DEPLOY_ROLLBACK_WAIT_TIMEOUT:-300}" \
      || echo "deploy: ROLLBACK FAILED -- $stack is down" >&2
  fi
  exit 1
fi

# Keep the last few images of this stack for rollbacks; drop older ones. Images are named after the
# stack (<stack>:<version>).
docker image ls "$stack" --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' \
  | sort -r | tail -n +"$((keep + 1))" | cut -f2 \
  | xargs -r docker image rm >/dev/null 2>&1 || true
