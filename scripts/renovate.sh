#!/usr/bin/env bash
# Runs Renovate once against the repositories of the catalog, on Forgejo: the `renovate` service user of the
# dev machine, in its own rootless Docker engine, from the systemd user timer yggdrasil-renovate.timer
# (platform/systemd, installed by `ygg install`, step renovate-timer). Renovate opens pull requests from
# renovate/* branches into develop; branch-policy accepts them there.
#
#     scripts/renovate.sh run
#
# Run as the user `renovate`. It runs package-manager code (restores, lockfile updates) while holding a token
# that can push, so it never uses the CI engine or Jenkins: only the engine of the user running this.
# Reads <secrets>/renovate.env, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil-<user>). Template:
# env/renovate.env.example (RENOVATE_TOKEN; RENOVATE_ENDPOINT, else the catalog's Forgejo API;
# RENOVATE_PLATFORM=gitea and RENOVATE_AUTODISCOVER=false, which nothing else is accepted for).
#
# What it adds to that file, on every run:
#   RENOVATE_REPOSITORIES   <owner>/<repo> for every repository of the catalog (`catalog.py repository-names`:
#                           the applications' and the `repositories` entries), the owner being FORGE_OWNER,
#                           else the catalog's, as forge.sh reads it. Renovate never autodiscovers: it visits
#                           these and nothing else the token can see.
#   RENOVATE_CONFIG         the global options: branchPrefix renovate/, baseBranches develop, and no
#                           onboarding: a repository without a Renovate config of its own is skipped
#                           (requireConfig: required) instead of getting an onboarding pull request.
# The container gets all of it through an --env-file, a copy of renovate.env plus those two lines, made
# for this run in the user's runtime directory (mode 600) and removed when the run ends: the token is never an
# argument of any command (arguments are readable by every user of the machine) or an -e value.
#
# Exit 0 when the container ended well, its status when it did not, 1 for a setting that is wrong (explained
# on stderr, the token never in it), 2 for a command that is not `run`.
set -euo pipefail

die() { echo "renovate: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless and use_rootless_engine, shared with platform.sh and agent.sh.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"

# The image, an exact version and the digest of its manifest list: what runs against every repository with a
# push token changes with a commit here (reviewed), never because a tag moved. The default (slim) image, not
# -full: its tools are installed on demand by containerbase, and there is no Docker socket for the other
# binarySource anyway. Update both parts together: the digest of renovate/renovate:<version> on Docker Hub
# (`docker buildx imagetools inspect renovate/renovate:<version>`), after reading the release notes.
RENOVATE_IMAGE=renovate/renovate:44.181.0@sha256:1a06e15f2d8bc08342828ec43c0a90de0ac9192900f513af90e77765e0991b6d

# The global options (see the header): the branches Renovate pushes and opens pull requests from, where they
# go, and what it does with a repository that has no config of its own.
RENOVATE_GLOBAL_CONFIG='{"branchPrefix":"renovate/","baseBranches":["develop"],"onboarding":false,"requireConfig":"required"}'

usage="usage: renovate.sh run"
[[ "${1:-}" == run && $# -eq 1 ]] || { echo "renovate: $usage" >&2; exit 2; }

catalog() { python3 "$root/scripts/catalog.py" "$@"; }

secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil-$(id -un)}
env_file="$secrets/renovate.env"
[[ -f "$env_file" ]] || die "missing $env_file (template: env/renovate.env.example)"

# Docker reads an --env-file line by line, NAME=value, literally (no quotes, no shell). So does this: a line
# that is not a comment, a blank or NAME=value is refused (by its number: it may be the token), and the
# value is never printed.
names=()
declare -A values=()
line_number=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line_number=$((line_number + 1))
  [[ -n "$line" && "$line" != "#"* ]] || continue
  [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] \
    || die "$env_file, line $line_number: not NAME=value (no quotes, no export, no comment after a value; template: env/renovate.env.example)"
  name=${BASH_REMATCH[1]}
  [[ -z "${values[$name]+set}" ]] || die "$env_file: $name is set twice"
  names+=("$name")
  values[$name]=${BASH_REMATCH[2]}
done <"$env_file"

# What this script computes, or fixes: a copy in the file would be stale, or a way to visit more than the
# catalog lists.
for name in RENOVATE_REPOSITORIES RENOVATE_CONFIG; do
  [[ -z "${values[$name]+set}" ]] || die "$name in $env_file: renovate.sh computes it on every run; remove the line"
done
[[ "${values[RENOVATE_PLATFORM]:-gitea}" == gitea ]] \
  || die "RENOVATE_PLATFORM in $env_file must be gitea (Forgejo's platform in Renovate)"
[[ "${values[RENOVATE_AUTODISCOVER]:-false}" == false ]] \
  || die "RENOVATE_AUTODISCOVER in $env_file must be false: Renovate visits the catalog's repositories only"

token=${values[RENOVATE_TOKEN]:-}
[[ -n "$token" ]] || die "RENOVATE_TOKEN must be set in $env_file (template: env/renovate.env.example)"
# Docker takes the value literally, so a token with a quote or a space in it would be a different token.
[[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "RENOVATE_TOKEN in $env_file has characters a token does not have"

# Renovate here is Forgejo's: with a catalog without a forge block (GitHub) there is nothing to run against.
forge_type=$(catalog forge type) || die "cannot read the git server from the catalog"
[[ "$forge_type" == forgejo ]] || die "the catalog's git server is $forge_type: Renovate runs against Forgejo (forge.type in catalog.yaml)"
endpoint=${values[RENOVATE_ENDPOINT]:-}
[[ -n "$endpoint" ]] || endpoint=$(catalog forge api) || die "cannot read Forgejo's API from the catalog"

owner=${FORGE_OWNER:-}
[[ -n "$owner" ]] || owner=$(catalog owner) || die "cannot read the owner from the catalog"
repositories=""
while IFS= read -r repository; do
  [[ -z "$repository" ]] || repositories+="${repositories:+,}$owner/$repository"
done < <(catalog repository-names || die "cannot read the repositories from the catalog")
[[ -n "$repositories" ]] || die "the catalog lists no repository: nothing to run Renovate on"

use_rootless_engine

# The run's own env file, in the runtime directory of the user (tmpfs, theirs alone) when there is one, mode
# 600 from the first byte (umask), removed however the run ends.
run_env=$(umask 077 && mktemp "${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/yggdrasil-renovate.XXXXXX") \
  || die "cannot make a file in ${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
trap 'rm -f "$run_env"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
{
  for name in "${names[@]}"; do
    # Written once, below, whichever way each was found (the file's own value is the same, or not there).
    case "$name" in
      RENOVATE_ENDPOINT | RENOVATE_PLATFORM | RENOVATE_AUTODISCOVER) ;;
      *) printf '%s=%s
' "$name" "${values[$name]}" ;;
    esac
  done
  printf '%s=%s
' RENOVATE_ENDPOINT "$endpoint" RENOVATE_PLATFORM gitea RENOVATE_AUTODISCOVER false     RENOVATE_REPOSITORIES "$repositories" RENOVATE_CONFIG "$RENOVATE_GLOBAL_CONFIG"
} >"$run_env"

status=0
docker run --rm --name yggdrasil-renovate --env-file "$run_env" "$RENOVATE_IMAGE" || status=$?
exit "$status"
