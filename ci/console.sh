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

cd console
flutter pub get
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build web --release --no-web-resources-cdn
# The job installed Temurin 17 for this step; the image's JDK builds it (the app targets Java 17). The SDK parts it
# needs are installed first, as root (ci/android-sdk.sh).
ensure_android_sdk
flutter build apk --release
