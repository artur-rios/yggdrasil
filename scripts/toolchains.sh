# shellcheck shell=bash
# The ci agent's toolchain cache: pinned SDKs fetched once per machine instead of once per check. Sourced by
# scripts/run-check.sh (--toolchains), never run. The Flutter SDK here; the package caches are
# scripts/package_cache.py's, mounted the same way.
#
# Where. $YGG_TOOLCHAIN_CACHE of the ci agent (agent.env; agent.sh makes it, under YGG_AGENT_WORKDIR so the
# agent sees it at its host path, as it does the workspaces). The pipeline library passes it to run-check.sh for
# the checks and the branch policy only: a release step never gets it, and the release agent has none.
#
#   flutter/<version>-<sha256>/        an unpacked SDK; never written again once it is there
#   flutter/<version>-<sha256>.used    touched by every check that uses it (what toolchain_gc goes by)
#   flutter/.lock                      held while an SDK is fetched, so concurrent checks fetch it once
#   flutter/.incoming.*                a fetch in progress (or one a killed agent left: removed after a day)
#   runs/<id>/                         one check's overlays (flutter/, packages/, ndk/, each with upper/ and
#                                      work/), and its apt.conf; removed when it ends, or a day later
#   packages/<repository>/             that repository's package cache: scripts/package_cache.py
#
# The pin. What a repository's own ci/flutter-env.sh fetches when FLUTTER_SDK_DIR has no SDK: its
# FLUTTER_VERSION and FLUTTER_SHA256 lines, in ci/flutter-env.sh or ci/pins.sh. Read as data (the whole line
# must match), never sourced: the checkout is the branch's, whoever pushed it.
#
# Why a branch cannot poison it. An SDK is fetched by this script, in the agent, never by repository code, and
# only from Flutter's own release archive (FLUTTER_ARCHIVE_URL, the version filled in), and it is kept only when
# the archive's SHA-256 is the pinned one; its directory is named by both. So a branch that pins something else
# gets a directory of its own, holding a release Google published, and the directory another repository's pin
# names is never touched. A check never sees the directory itself: its SDK is an overlay of it (below), so
# what a check writes into the SDK lands in its own upper directory and is thrown away. The one way past that is
# a `docker: true` check, which holds the ci engine and can mount anything the ci user owns; that is the accepted
# risk docs/setup.md names ("Checks run branch code, side by side"), and it is why a release, which must not
# depend on what a branch's check did, never uses this cache.
#
# Why an overlay and not a read-only mount. Flutter writes into its own SDK on every run (bin/cache's lock file
# and stamps, the engine artifacts a build needs), so it cannot run from a read-only one. The check gets a Docker
# volume of type overlay (the local driver, which a rootless engine on Linux 5.11 or later can mount: prove it
# with scripts/prove-machine.sh dev, row "toolchain overlay"): the cached SDK is its lower directory, read only,
# and runs/<id>/flutter holds its upper and work directories.

# Flutter's release archive for Linux, stable channel: what every repository's ci/flutter-env.sh fetches too.
FLUTTER_ARCHIVE_URL=${YGG_FLUTTER_ARCHIVE_URL:-https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_%s-stable.tar.xz}
# Where the SDK is in a check's container; run-check.sh sets FLUTTER_SDK_DIR to it.
# shellcheck disable=SC2034 # used by run-check.sh, which sources this file
FLUTTER_IN_CONTAINER=/opt/yggdrasil/flutter
# How long a check waits for another one's fetch of the same cache (seconds) before it fetches nothing and
# runs without the cache; a fetch and unpack takes a few minutes.
TOOLCHAIN_LOCK_WAIT=${YGG_TOOLCHAIN_LOCK_WAIT:-1200}
# An SDK no check has used for this many days is removed (after a fetch, which is when the disk fills).
TOOLCHAIN_KEEP_DAYS=30

toolchain_note() { echo "run-check.sh: $*" >&2; }

# toolchain_cache_ok <dir>: the cache directory can be named in an overlay's options and a --mount field: an
# absolute path of letters, digits and . _ - / only (no comma, colon, quote or space), and a directory.
toolchain_cache_ok() {
  [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ && "$1" != *..* && -d "$1" ]]
}

# flutter_pin <checkout>: prints "<version> <sha256>", what the checkout's ci/flutter-env.sh or ci/pins.sh pins,
# and returns 1 when it pins nothing this script can use: no such line, a value that is not a plain x.y.z version or
# 64 lowercase hex digits, or two different values (the files would disagree about what they fetch).
flutter_pin() {
  local checkout=$1 file line versions="" sums="" value
  for file in "$checkout/ci/flutter-env.sh" "$checkout/ci/pins.sh"; do
    [[ -f "$file" && ! -L "$file" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      line=${line%$'\r'}
      if [[ "$line" =~ ^FLUTTER_VERSION=([\"\']?)([0-9]+\.[0-9]+\.[0-9]+)([\"\']?)[[:space:]]*$ \
            && "${BASH_REMATCH[1]}" == "${BASH_REMATCH[3]}" ]]; then
        versions+="${BASH_REMATCH[2]}"$'\n'
      elif [[ "$line" =~ ^FLUTTER_SHA256=([\"\']?)([0-9a-f]{64})([\"\']?)[[:space:]]*$ \
              && "${BASH_REMATCH[1]}" == "${BASH_REMATCH[3]}" ]]; then
        sums+="${BASH_REMATCH[2]}"$'\n'
      elif [[ "$line" =~ ^FLUTTER_(VERSION|SHA256)= ]]; then
        # A pin this script can't read (a variable, a command): better no cache than a wrong one.
        return 1
      fi
    done <"$file"
  done
  versions=$(sort -u <<<"$versions" | sed '/^$/d')
  sums=$(sort -u <<<"$sums" | sed '/^$/d')
  [[ -n "$versions" && "$versions" != *$'\n'* && -n "$sums" && "$sums" != *$'\n'* ]] || return 1
  value="$versions $sums"
  printf '%s\n' "$value"
}

# toolchain_gc <dir>: removes, under the lock, what nothing uses any more: fetches a killed agent left half done
# (older than a day) and SDKs no check has used for TOOLCHAIN_KEEP_DAYS. Run before a fetch, which is when the
# disk fills; the run directories are swept by flutter_overlay.
toolchain_gc() {
  local cache=$1 sdk stamp
  find "$cache/flutter" -mindepth 1 -maxdepth 1 -name '.incoming.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
  for stamp in "$cache"/flutter/*.used; do
    [[ -f "$stamp" ]] || continue
    if [[ -n "$(find "$stamp" -mtime +"$TOOLCHAIN_KEEP_DAYS" 2>/dev/null)" ]]; then
      sdk=${stamp%.used}
      toolchain_note "removing Flutter ${sdk##*/} from the toolchain cache: no check used it for $TOOLCHAIN_KEEP_DAYS days"
      rm -rf "$sdk" && rm -f "$stamp"
    fi
  done
}

# flutter_fetch <dir> <version> <sha256> <target>: downloads Flutter's archive of that version, checks its SHA-256
# against the pin, unpacks it, and renames the SDK to <target> (in the same directory, so the rename is atomic: a
# check sees the whole SDK or none). Called with the lock held. Returns 1, leaving nothing behind, on any failure.
flutter_fetch() {
  local cache=$1 version=$2 sum=$3 target=$4 incoming url actual started
  started=$SECONDS
  incoming=$(mktemp -d "$cache/flutter/.incoming.XXXXXX") || return 1
  # shellcheck disable=SC2059 # the URL is the format: its %s is the version
  printf -v url "$FLUTTER_ARCHIVE_URL" "$version"
  toolchain_note "fetching Flutter $version into the toolchain cache"
  if ! curl --fail --silent --show-error --location --retry 3 -o "$incoming/flutter.tar.xz" "$url"; then
    toolchain_note "could not download $url"
    rm -rf "$incoming"
    return 1
  fi
  actual=$(sha256sum "$incoming/flutter.tar.xz" | cut -d' ' -f1)
  if [[ "$actual" != "$sum" ]]; then
    toolchain_note "Flutter $version's archive has SHA-256 $actual, not the pinned $sum: not cached"
    rm -rf "$incoming"
    return 1
  fi
  mkdir "$incoming/sdk"
  # --no-same-owner: the files are the ci user's (root in its engine), which is also who git in a check is, so
  # git does not call the SDK's own checkout "dubious".
  if ! tar -xJf "$incoming/flutter.tar.xz" --no-same-owner -C "$incoming/sdk" || [[ ! -x "$incoming/sdk/flutter/bin/flutter" ]]; then
    toolchain_note "could not unpack Flutter $version's archive"
    rm -rf "$incoming"
    return 1
  fi
  mv "$incoming/sdk/flutter" "$target"
  rm -rf "$incoming"
  toolchain_note "Flutter $version is in the toolchain cache ($((SECONDS - started)) s)"
}

# flutter_sdk <dir> <version> <sha256>: prints the cached SDK's directory, fetching it first when it isn't there,
# and returns 1 when it can't (the check then runs as it would without the cache: it fetches its own).
flutter_sdk() {
  local cache=$1 version=$2 sum=$3 sdk lock_fd status=0
  sdk="$cache/flutter/$version-$sum"
  mkdir -p "$cache/flutter" "$cache/runs" || return 1
  if [[ ! -x "$sdk/bin/flutter" ]]; then
    exec {lock_fd}>"$cache/flutter/.lock" || return 1
    if ! flock -w "$TOOLCHAIN_LOCK_WAIT" "$lock_fd"; then
      toolchain_note "another check has been fetching into the toolchain cache for ${TOOLCHAIN_LOCK_WAIT} s: running without it"
      exec {lock_fd}>&-
      return 1
    fi
    # Another check may have fetched it while this one waited.
    if [[ ! -x "$sdk/bin/flutter" ]]; then
      toolchain_gc "$cache"
      flutter_fetch "$cache" "$version" "$sum" "$sdk" || status=1
    fi
    exec {lock_fd}>&-
    ((status == 0)) || return 1
  fi
  touch "$sdk.used" 2>/dev/null || true
  printf '%s\n' "$sdk"
}

# toolchain_remove_run <run directory>: removes it. The overlay leaves its work directory's own `work` with mode 000,
# which only a process with CAP_DAC_OVERRIDE (the agent's root) could descend as it is: its owner, the ci user, can
# always give itself the permissions back first.
toolchain_remove_run() {
  chmod -R u+rwx "$1" 2>/dev/null || true
  rm -rf "$1"
}

# toolchain_run_dir <dir>: makes the directory of one check's overlays (runs/<id>, removed when the check ends) and
# prints it, after removing what checks whose agent was killed with them left a day ago.
toolchain_run_dir() {
  local cache=$1 run
  mkdir -p "$cache/runs" || return 1
  while IFS= read -r -d '' run; do
    toolchain_remove_run "$run"
  done < <(find "$cache/runs" -mindepth 1 -maxdepth 1 -mmin +1440 -print0 2>/dev/null)
  mktemp -d "$cache/runs/XXXXXXXX"
}

# overlay_mount <destination in the container> <lower directory> <directory in the run>: makes <directory>/upper and
# <directory>/work and prints the --mount field of an overlay of <lower> at <destination>. Quoted for docker's CSV
# parser: the overlay's options are themselves comma-separated. Every path is of the plain characters
# toolchain_cache_ok allows, so none can end an option early.
overlay_mount() {
  local destination=$1 lower=$2 dir=$3
  mkdir -p "$dir/upper" "$dir/work" || return 1
  printf '%s\n' "type=volume,dst=$destination,volume-driver=local,volume-opt=type=overlay,volume-opt=device=overlay,\"volume-opt=o=lowerdir=$lower,upperdir=$dir/upper,workdir=$dir/work,userxattr\""
}

# ---- Package caches: scripts/package_cache.py has the whole story --------------------------------------------

# Where a check sees its repository's package cache, and where each tool is told to keep what it downloads there.
PACKAGES_IN_CONTAINER=/opt/yggdrasil/cache
APT_CONFIG_IN_CONTAINER=/opt/yggdrasil/apt.conf
# shellcheck disable=SC2034 # used by run-check.sh, which sources this file
PACKAGE_ENVIRONMENT=(
  "PUB_CACHE=$PACKAGES_IN_CONTAINER/pub"
  "GRADLE_USER_HOME=$PACKAGES_IN_CONTAINER/gradle"
  "NUGET_PACKAGES=$PACKAGES_IN_CONTAINER/nuget"
  "npm_config_cache=$PACKAGES_IN_CONTAINER/npm"
  "APT_CONFIG=$APT_CONFIG_IN_CONTAINER"
)

# package_scope_ok <scope>: a repository's name as the catalog allows it (catalog.py's REPOSITORY), which becomes a
# directory of the cache.
package_scope_ok() {
  [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$ ]]
}

# package_apt_config <file>: writes the apt configuration a check gets as APT_CONFIG: apt-get keeps the packages it
# downloads in the cache (apt checks each one against the signed index before it uses it). The images' own
# docker-clean removes /var/cache/apt/archives/*.deb, not this directory.
package_apt_config() {
  printf 'Dir::Cache::Archives "%s/apt/archives/";\n' "$PACKAGES_IN_CONTAINER" >"$1"
}

# image_android_home <image>: the image's ANDROID_HOME (or ANDROID_SDK_ROOT), where Gradle installs the NDK a build
# asks for; nothing for an image without one, or with one that can't be a mount's destination. Pulls the image when
# the engine has not got it yet, as `docker run --pull missing` would.
image_android_home() {
  local image=$1 environment value
  environment=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" 2>/dev/null) \
    || { docker pull --quiet "$image" >/dev/null 2>&1 \
           && environment=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" 2>/dev/null); } \
    || return 0
  value=$(sed -n 's/^ANDROID_HOME=//p' <<<"$environment" | head -n 1)
  [[ -n "$value" ]] || value=$(sed -n 's/^ANDROID_SDK_ROOT=//p' <<<"$environment" | head -n 1)
  value=${value%/}
  if [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ && "$value" != *..* ]]; then
    printf '%s\n' "$value"
  fi
}
