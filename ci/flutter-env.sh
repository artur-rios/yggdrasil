# shellcheck shell=bash
# Puts the pinned Flutter SDK on PATH. Sourced by ci/console.sh and ci/release-build.sh, not run.
#
# The workflows pinned Flutter 3.47.4 (subosito/flutter-action). The newest ghcr.io/cirruslabs/flutter image
# is 3.44.0, whose Dart (3.12) is older than console/pubspec.yaml's `sdk: ^3.13.3`, so the image only supplies
# the Android SDK and the JDK, and the SDK itself is fetched here from Flutter's release archive and checked
# against its checksum (console/Dockerfile does the same). FLUTTER_SHA256 comes from
# https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json; change it together
# with FLUTTER_VERSION (Renovate bumps the version, not the checksum), with the same two lines in
# console/Dockerfile, and with flutter-version in the GitHub workflows (ci.yml, release-windows.yml).
#
# FLUTTER_SDK_DIR reuses an unpacked SDK between runs (default: a directory under $TMPDIR).

# renovate: datasource=github-tags depName=flutter/flutter versioning=regex:^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$
FLUTTER_VERSION=3.47.4
FLUTTER_SHA256=5b45f0ceda99b9bebdc873e7e69f6450aeb4c30f454b505e2e62fc9255a907d3

FLUTTER_SDK_DIR=${FLUTTER_SDK_DIR:-${TMPDIR:-/tmp}/flutter-$FLUTTER_VERSION}

if [ ! -x "$FLUTTER_SDK_DIR/bin/flutter" ]; then
  _flutter_work=$(mktemp -d)
  echo "ci/flutter-env.sh: fetching Flutter $FLUTTER_VERSION"
  curl --fail --silent --show-error --location --retry 3 -o "$_flutter_work/flutter.tar.xz" \
    "https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz"
  echo "$FLUTTER_SHA256  $_flutter_work/flutter.tar.xz" | sha256sum -c -
  # --no-same-owner: unpacked as root, the archive's owner would stay, and git would refuse the SDK's
  # checkout as "dubious ownership".
  tar -xJf "$_flutter_work/flutter.tar.xz" --no-same-owner -C "$_flutter_work"
  mkdir -p "$(dirname "$FLUTTER_SDK_DIR")"
  rm -rf "$FLUTTER_SDK_DIR"
  mv "$_flutter_work/flutter" "$FLUTTER_SDK_DIR"
  rm -rf "$_flutter_work"
  unset _flutter_work
fi

export FLUTTER_ROOT=$FLUTTER_SDK_DIR
export FLUTTER_HOME=$FLUTTER_SDK_DIR
export PATH="$FLUTTER_SDK_DIR/bin:$FLUTTER_SDK_DIR/bin/cache/dart-sdk/bin:$PATH"
export CI=true

flutter config --no-analytics --no-cli-animations >/dev/null
dart --disable-analytics >/dev/null
flutter --version
