#!/usr/bin/env bash
# Pushes a library's NuGet packages, on the release agent, in the publish step of an owner-tagged
# library's tag build (jenkins/library/vars/yggdrasilPipeline.groovy, pushPackages). The packages were
# packed before, in another step and container (publish.nuget.run), with no key in reach; this step runs
# no repository code at all: only `dotnet nuget push`, in the pinned .NET SDK image below.
#
#     scripts/nuget-push.sh <packages-glob> [--source <url>] [--ca-file <file>]
#
#   packages-glob  <directory>/<pattern>: the directory is the part before the first component with a
#                  wildcard (* ? [), the pattern the rest (** crosses directories). An absolute path that
#                  means the same directory inside the agent and on the host, like run-check.sh's
#                  workspace: the `docker run` below goes to the host's engine, which mounts it (read-only)
#                  as /packages. Every match must be a regular file named *.nupkg inside the directory,
#                  reached through no symbolic link; a match that is not, or no match at all, refuses the
#                  whole push before anything is pushed. A .snupkg next to a .nupkg goes with it (dotnet's
#                  own rule).
#   --source       the feed's service index, https only: the key never travels in the clear
#                  (default https://api.nuget.org/v3/index.json).
#   --ca-file      a CA bundle the container checks the feed's certificate against (SSL_CERT_FILE)
#                  instead of the image's: a private feed, or the live tests' local one.
#
# The key is NUGET_API_KEY in this script's environment (the pipeline binds the Jenkins credential
# nuget-api-key for this step only). It reaches docker as `-e NUGET_API_KEY`, a name without a value, so
# docker takes it from its own environment: never on a command line here or in the process list. The
# container's shell puts it on dotnet's command line, inside the container.
#
# One `docker run` per package, sorted, with --skip-duplicate: a version the feed has already is not an
# error, so a push that stopped half way is run again as it is (the release-resume job).
#
# Exit: 0 every package pushed (or already there); 1 a push failed, named on stderr ("nuget-push: pushing
# <package> failed (exit N)"), the later ones not tried; 2 a wrong command line, or no NUGET_API_KEY;
# 3 the packages refused (none, a link, outside the directory, not a .nupkg).
set -euo pipefail

# The .NET SDK, by version and digest: what pushes a library's packages does not change under it.
IMAGE='mcr.microsoft.com/dotnet/sdk:10.0.401@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317'
NUGET_ORG='https://api.nuget.org/v3/index.json'

usage() {
  echo "usage: nuget-push.sh <packages-glob> [--source <url>] [--ca-file <file>]" >&2
  exit 2
}
die_usage() { echo "nuget-push: $*" >&2; usage; }
refuse() { echo "nuget-push: $*; nothing is pushed" >&2; exit 3; }

glob=""
source_url=$NUGET_ORG
ca_file=""
while (($#)); do
  case "$1" in
    --source)
      (($# >= 2)) || die_usage "--source needs a URL"
      source_url=$2
      shift 2
      ;;
    --ca-file)
      (($# >= 2)) || die_usage "--ca-file needs a file"
      ca_file=$2
      shift 2
      ;;
    -*) die_usage "unknown option '$1'" ;;
    *)
      [[ -z "$glob" ]] || die_usage "one packages glob only"
      glob=$1
      shift
      ;;
  esac
done
[[ -n "$glob" ]] || usage
[[ -n "${NUGET_API_KEY:-}" ]] || die_usage "NUGET_API_KEY is not set: the pipeline binds it for this step"
[[ "$source_url" =~ ^https://[^[:space:]\"\'\<\>]+$ ]] || die_usage "--source must be an https URL, not '$source_url'"
if [[ -n "$ca_file" ]]; then
  [[ -f "$ca_file" ]] || die_usage "the CA file '$ca_file' does not exist"
  ca_file=$(realpath -e -- "$ca_file")
  [[ "$ca_file" != *:* ]] || die_usage "the CA file's path has a ':', which docker -v cannot mount"
fi

# ---- The glob: a directory and a pattern under it ----

[[ "$glob" != *[[:space:]]* ]] || die_usage "the packages glob has a space: '$glob'"
IFS=/ read -r -a parts <<<"$glob"
for part in "${parts[@]}"; do
  [[ "$part" != . && "$part" != .. ]] || refuse "'$glob' could lead outside its directory (a . or .. part)"
done
directory="" pattern=""
for index in "${!parts[@]}"; do
  if [[ "${parts[$index]}" == *[\*\?\[]* ]]; then
    pattern=$(IFS=/; echo "${parts[*]:$index}")
    break
  fi
  if ((index == ${#parts[@]} - 1)); then
    pattern=${parts[$index]}  # no wildcard at all: one file
  else
    directory+="${parts[$index]}/"
  fi
done
directory=${directory%/}
if [[ -z "$directory" ]]; then
  [[ "$glob" == /* ]] && directory=/ || directory=.
fi
[[ -n "$pattern" ]] || die_usage "the packages glob '$glob' names no file"
[[ -d "$directory" ]] || die_usage "the packages' directory '$directory' does not exist"
root=$(realpath -e -- "$directory")
[[ "$root" != *:* ]] || die_usage "the packages' directory has a ':', which docker -v cannot mount"

# ---- The packages: every match checked before anything is pushed ----

cd "$root"
shopt -s globstar nullglob
# shellcheck disable=SC2206 # the pattern is expanded here, on purpose (it has no whitespace)
matches=($pattern)
shopt -u globstar nullglob
((${#matches[@]})) || refuse "no package matches '$glob'"
packages=()
for match in "${matches[@]}"; do
  [[ ! -L "$match" ]] || refuse "$match is a symbolic link"
  [[ -f "$match" ]] || refuse "$match is not a file"
  real=$(realpath -e -- "$match")
  # Through a linked directory, or out of the directory: the file is not the one the glob names.
  [[ "$real" == "$root/$match" ]] || refuse "$match leads outside $root (it is $real)"
  [[ "$match" == *.nupkg ]] || refuse "$match is not a .nupkg"
  packages+=("$match")
done
mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort)

# ---- The pushes ----

run=(docker run --rm --pull missing
     -e NUGET_API_KEY -e DOTNET_CLI_TELEMETRY_OPTOUT=1 -e DOTNET_NOLOGO=1
     -v "$root:/packages:ro")
[[ -z "$ca_file" ]] || run+=(-v "$ca_file:/ygg/ca.crt:ro" -e SSL_CERT_FILE=/ygg/ca.crt)

for package in "${packages[@]}"; do
  echo "nuget-push: pushing ${package##*/} to $source_url" >&2
  code=0
  # The key in the container's own shell: '$NUGET_API_KEY' is not expanded here.
  # shellcheck disable=SC2016
  "${run[@]}" "$IMAGE" sh -c 'exec dotnet nuget push "$1" --api-key "$NUGET_API_KEY" --source "$2" --skip-duplicate' \
    sh "/packages/$package" "$source_url" || code=$?
  ((code == 0)) || { echo "nuget-push: pushing ${package##*/} failed (exit $code)" >&2; exit 1; }
done
echo "nuget-push: ${#packages[@]} package(s) pushed (or already there) to $source_url" >&2
