# shellcheck shell=bash
# Sourced by ci/console.sh and ci/release-build.sh (not run on its own): installs into the image's Android SDK
# the parts the console's Android build needs and the image lacks, as the GitHub runner had them installed.
#
#   platforms;android-37.0   compileSdk = 37 (console/android/app/build.gradle.kts)
#   ndk;28.2.13676358        flutter.ndkVersion of Flutter 3.47.4 (ci/flutter-env.sh)
#
# Left to Gradle, the build installs them itself but then fails to find the platform it has just installed
# ("Failed to find target with hash string 'android-37'"). The SDK in ghcr.io/cirruslabs/flutter is root's
# (/opt/android-sdk-linux): this runs as root, as the CI and release agents run checks. Change these with
# compileSdk and with FLUTTER_VERSION.
ANDROID_PACKAGES=("platforms;android-37.0" "ndk;28.2.13676358")

ensure_android_sdk() {
  local sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}} missing=() package
  if [ -z "$sdk" ]; then
    echo "ci/android-sdk.sh: ANDROID_HOME is not set: the image has no Android SDK" >&2
    return 1
  fi
  for package in "${ANDROID_PACKAGES[@]}"; do
    [ -d "$sdk/${package//;//}" ] || missing+=("$package")
  done
  [ "${#missing[@]}" -eq 0 ] && return 0
  if [ ! -w "$sdk" ]; then
    echo "ci/android-sdk.sh: ${missing[*]} must be installed into $sdk, which this user cannot write (run as root, as the agents do)" >&2
    return 1
  fi
  echo "ci/android-sdk.sh: installing ${missing[*]}"
  yes | sdkmanager --licenses >/dev/null || true
  sdkmanager --install "${missing[@]}" >/dev/null
}
