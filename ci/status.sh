#!/usr/bin/env bash
# The status API (ci.yml's job "Status API"): its tests, then its container image, built and not kept.
# Run from anywhere; it works from the repository's root. `docker: true` in yggdrasil.yml: the image is built by
# the engine whose socket the check is given, with the Docker client and buildx from ci/tools.sh when the .NET
# SDK image has none.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck source=ci/tools.sh
source ci/tools.sh

dotnet test status --configuration Release

ensure_buildx
# A tag of its own (the check's process id), removed afterwards: on a shared engine, two checks at once must
# not take each other's image, and nothing is left behind.
tag="yggdrasil-status:ci-$$"
trap 'docker image rm --force "$tag" >/dev/null 2>&1 || true' EXIT
docker build --tag "$tag" status
