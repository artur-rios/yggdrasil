#!/usr/bin/env bash
# Packs the library's NuGet packages into artifacts/ (`publish.nuget` in yggdrasil.yml: run in the pinned .NET SDK
# image on the release agent, from the checkout of the version tag, with no credential at all; the packages
# `artifacts/*.nupkg` are pushed afterwards by scripts/nuget-push.sh). It ports the packing steps of the
# library's publish-package.yml. Pushing is not done here: the NuGet key never reaches repository code.
#
# Replace <Name> in SOLUTION below (src/ArturRios.Util.sln for dotnet-util).
#
# RELEASE_VERSION is the tag without a leading v, set by the pipeline for a release. Every package must carry
# that version, as the old workflow's check that the tag matched the project's <Version> did: a tag 2.1.0 on a
# project that says 2.0.9 packs a 2.0.9 package, and nothing is published. A library whose packages have
# versions of their own (a multi-package solution) must change that check.
set -euo pipefail

SOLUTION='src/ArturRios.<Name>.sln'

case $SOLUTION in
  *'<'*) echo "ci/pack.sh: set SOLUTION at the top of the script to the library's solution (it still has the placeholder $SOLUTION)" >&2; exit 2 ;;
esac

# The repository's root, wherever this is run from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DOTNET_NOLOGO=1 DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1

# Only what this run packs is in artifacts/ (the test check's results are kept elsewhere under it).
rm -f artifacts/*.nupkg artifacts/*.snupkg
dotnet restore "$SOLUTION"
dotnet pack "$SOLUTION" --configuration Release --no-restore --output artifacts

shopt -s nullglob
packages=(artifacts/*.nupkg)
if [ "${#packages[@]}" -eq 0 ]; then
  echo "ci/pack.sh: nothing was packed: no project of $SOLUTION is packable (IsPackable, PackageId)" >&2
  exit 1
fi

if [ -n "${RELEASE_VERSION:-}" ]; then
  for package in "${packages[@]}"; do
    case $package in
      *".$RELEASE_VERSION.nupkg") ;;
      *) echo "ci/pack.sh: the tag is version $RELEASE_VERSION but $package was packed: the project's <Version> does not match the tag" >&2; exit 1 ;;
    esac
  done
fi
