#!/usr/bin/env bash
# The release files built on the release agent (`release.build` in yggdrasil.yml), from the checkout of the
# release's tag: release.yml's "Android APK" job. The Windows installer is release-windows.yml's
# (`release.windows`); the web console is not a release file (each host builds it, scripts/platform.sh).
#
#   dist/yggdrasil-console-<version>.apk
#
# RELEASE_VERSION is the tag without its v (set by the pipeline; "0.0.0" when this is run by hand). As before,
# the APK is signed with the debug key (console/android/app/build.gradle.kts): there is no release key.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
root=$PWD

version=${RELEASE_VERSION:-0.0.0}

# shellcheck source=ci/flutter-env.sh
source ci/flutter-env.sh

rm -rf dist
mkdir dist

cd console
flutter pub get
flutter build apk --release "--build-name=$version"
cp build/app/outputs/flutter-apk/app-release.apk "$root/dist/yggdrasil-console-$version.apk"
