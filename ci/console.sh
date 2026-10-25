#!/usr/bin/env bash
# The console (ci.yml's job "Console"): formatting, analysis and tests, then the web build and the Android
# release APK (kept with the build: `archive` in yggdrasil.yml, as the job uploaded it). The Windows build stays
# on GitHub Actions ("Console (Windows)").
# Run from anywhere; it works from the repository's root.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck source=ci/flutter-env.sh
source ci/flutter-env.sh
# shellcheck source=ci/android-sdk.sh
source ci/android-sdk.sh
# The SDK parts the APK build needs, installed as root (ci/android-sdk.sh). First, so that a run that cannot
# install them (not root) fails now, not after the tests and the web build.
ensure_android_sdk

cd console
flutter pub get
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build web --release --no-web-resources-cdn
# The job installed Temurin 17 for this step; the image's JDK builds it (the app targets Java 17).
flutter build apk --release
