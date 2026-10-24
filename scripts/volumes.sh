#!/usr/bin/env bash
# Carries Docker volumes, databases included, from the old VPS to the apps machine, once, at the cutover.
#
#     volumes.sh export <dir> [<volume>...]
#     volumes.sh import <dir>
#
# export writes <dir>/<volume>.tar for each volume and, last, <dir>/manifest.tsv. By default it exports
# every volume that has a com.docker.compose.project label; volumes named on the command line are
# exported instead. It refuses, before writing anything, a volume that does not exist, one without the
# Compose labels (the manifest has to recreate them) and one that a running container mounts: stop the
# stack first (a database copied while it runs is not a backup). <dir> must not hold a manifest.tsv yet.
# manifest.tsv has one line per volume, five columns separated by tabs, no header:
#
#     volume  project  compose_volume  bytes  sha256
#
# (the volume's name; its com.docker.compose.project and com.docker.compose.volume labels; the size and
# the SHA-256 of its tar file). import checks the size and the sha256 of every tar file, then that every
# volume it would fill is either missing or empty and carries the manifest's labels, and only then creates
# the volumes (with those labels) and untars into them. Any problem means nothing is imported. If an
# untar fails, the volumes this run created are removed again, so a rerun starts clean.
#
# Where it runs:
#   - On the old VPS (rootful Docker, the older yggdrasil), run by the admin with sudo, after copying this
#     file and docker-engine.sh next to it, and nothing else from the repository. This is the ONLY place
#     the script may run as root, and only with DOCKER_HOST unset and a rootful engine. Needs docker and
#     sha256sum. The files it writes are private (umask 077): root's, when run with sudo.
#   - On the apps machine, as ygg, on the rootless engine (use_rootless_engine, like the other scripts).
# Root against a rootless engine, and a user against a rootful one, are refused.
set -euo pipefail
# The tar files hold databases: the directory and files are the invoking user's alone.
umask 077

die() { echo "volumes: $*" >&2; exit 1; }

# docker_socket, require_rootless, use_rootless_engine and TAR_IMAGE, from the file next to this one:
# not from the repository, which the old VPS does not have.
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=docker-engine.sh
source "$here/docker-engine.sh"

usage="usage: volumes.sh export <dir> [<volume>...] | import <dir>"
command=${1:-}
case "$command" in
  export) (($# >= 2)) || die "$usage" ;;
  import) (($# == 2)) || die "$usage" ;;
  *) die "$usage" ;;
esac
dir=$2
shift 2

# The Compose labels this script carries, on volumes and in the manifest.
PROJECT_LABEL=com.docker.compose.project
VOLUME_LABEL=com.docker.compose.volume

# A name that ends up in a file name, a manifest and a container command: no slash, no leading dash. The
# shape Docker gives volume names, and Compose's project and volume names fit it.
name_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }

# sha256_of <file>: the checksum, nothing else.
sha256_of() {
  local sum
  sum=$(sha256sum -- "$1") || return 1
  echo "${sum%% *}"
}

# ---- The engine ---------------------------------------------------------------------------------

# Root is for the old VPS and nowhere else: its rootful engine, found as docker finds it by default, which
# is what DOCKER_HOST being unset means. Everyone else is the user of a rootless engine, as in the other
# scripts of the repository.
select_engine() {
  command -v docker >/dev/null || die "docker is not installed"
  command -v sha256sum >/dev/null || die "sha256sum is not installed"
  if [[ "$(id -u)" == 0 ]]; then
    [[ -z "${DOCKER_HOST:-}" ]] \
      || die "as root this runs on the local rootful Docker engine only, with DOCKER_HOST unset (it is $DOCKER_HOST); anywhere else, run it as the user of a rootless engine (ygg)"
    local security
    security=$(docker info --format '{{json .SecurityOptions}}') \
      || die "cannot reach the Docker engine (is it running?)"
    [[ "$security" != *name=rootless* ]] \
      || die "root against a rootless Docker engine: run this as the user that owns the engine (ygg), not as root"
  else
    use_rootless_engine
  fi
}

# ---- export -------------------------------------------------------------------------------------

# The tar being written, so that an interrupted run does not leave it behind.
partial=""
cleanup_partial() { [[ -z "$partial" ]] || rm -f -- "$partial"; }

# volume_labels <volume>: "<project>|<compose volume>", either empty when the label is not there.
volume_labels() {
  docker volume inspect \
    --format "{{index .Labels \"$PROJECT_LABEL\"}}|{{index .Labels \"$VOLUME_LABEL\"}}" "$1"
}

do_export() {
  local -a names=() missing=() unusable=() busy=() rows=()
  local -A project=() compose_volume=() seen=()
  local name listing labels in_use

  # Which volumes: the ones named, or all that Compose made.
  if (($#)); then
    for name in "$@"; do
      if [[ -z "${seen[$name]:-}" ]]; then
        seen[$name]=1
        names+=("$name")
      fi
    done
  else
    listing=$(docker volume ls --filter "label=$PROJECT_LABEL" --format '{{.Name}}') \
      || die "cannot list the Docker volumes"
    [[ -n "$listing" ]] || die "no volume has a $PROJECT_LABEL label: nothing to export"
    mapfile -t names <<<"$listing"
  fi

  # Everything that can be wrong is looked for before the first byte is written, and told at once.
  listing=$(docker volume ls --format '{{.Name}}') || die "cannot list the Docker volumes"
  for name in "${names[@]}"; do
    if ! grep -qxF -- "$name" <<<"$listing"; then
      missing+=("$name")
    fi
  done
  ((${#missing[@]} == 0)) || die "no such volume: ${missing[*]}"

  for name in "${names[@]}"; do
    labels=$(volume_labels "$name") || die "cannot inspect the volume $name"
    project[$name]=${labels%%|*}
    compose_volume[$name]=${labels#*|}
    # A manifest line needs both labels, and they have to be names that can be put back as labels.
    if ! name_ok "${project[$name]}" || ! name_ok "${compose_volume[$name]}"; then
      unusable+=("$name")
    fi
  done
  ((${#unusable[@]} == 0)) \
    || die "not carried, without a usable $PROJECT_LABEL and $VOLUME_LABEL label: ${unusable[*]}"

  # A running container may be writing: a copy of a database taken under it is worthless.
  for name in "${names[@]}"; do
    in_use=$(docker ps --filter "volume=$name" --quiet) || die "cannot list the running containers"
    [[ -z "$in_use" ]] || busy+=("$name")
  done
  ((${#busy[@]} == 0)) || die "used by a running container, stop it first: ${busy[*]}"

  [[ ! -e "$dir/manifest.tsv" ]] \
    || die "$dir/manifest.tsv exists: an export is there already; use an empty directory"

  mkdir -p "$dir"
  trap cleanup_partial EXIT
  local tar_file sum size
  for name in "${names[@]}"; do
    echo "volumes: exporting $name"
    tar_file="$dir/$name.tar"
    # Written to <name>.tar.partial and renamed when whole, so a failing run never leaves a file that
    # looks like an export. The volume is mounted read-only: the data is only read. Numeric owners: the
    # ids are what the tar records, whatever names this image's /etc/passwd happens to have.
    partial="$tar_file.partial"
    docker run --rm --volume "$name:/volume:ro" "$TAR_IMAGE" \
      tar --numeric-owner -C /volume -cf - . >"$partial" \
      || die "tar of $name failed"
    mv -- "$partial" "$tar_file"
    partial=""
    sum=$(sha256_of "$tar_file") || die "cannot checksum $tar_file"
    size=$(stat -c %s -- "$tar_file")
    rows+=("$name"$'\t'"${project[$name]}"$'\t'"${compose_volume[$name]}"$'\t'"$size"$'\t'"$sum")
  done

  # The manifest is the last thing written, itself through a partial: a directory with a manifest.tsv is
  # a whole export, and one without is not.
  partial="$dir/manifest.tsv.partial"
  printf '%s\n' "${rows[@]}" >"$partial"
  mv -- "$partial" "$dir/manifest.tsv"
  partial=""
  echo "volumes: exported ${#names[@]} volume(s) to $dir"
}

# ---- import -------------------------------------------------------------------------------------

# The manifest, in parallel arrays by line.
m_volume=() m_project=() m_compose=() m_bytes=() m_sum=()

read_manifest() {
  local manifest="$dir/manifest.tsv" line number=0 tab=$'\t' name_re='[A-Za-z0-9][A-Za-z0-9_.-]*'
  local -A seen=()
  local re="^($name_re)$tab($name_re)$tab($name_re)$tab([0-9]+)$tab([0-9a-f]{64})\$"
  [[ -f "$manifest" ]] || die "no $manifest: export to this directory first"
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    [[ -n "$line" ]] || continue
    [[ "$line" =~ $re ]] \
      || die "$manifest line $number is not 'volume<TAB>project<TAB>compose volume<TAB>bytes<TAB>sha256': $line"
    [[ -z "${seen[${BASH_REMATCH[1]}]:-}" ]] || die "$manifest line $number: ${BASH_REMATCH[1]} is there twice"
    seen[${BASH_REMATCH[1]}]=1
    m_volume+=("${BASH_REMATCH[1]}")
    m_project+=("${BASH_REMATCH[2]}")
    m_compose+=("${BASH_REMATCH[3]}")
    m_bytes+=("${BASH_REMATCH[4]}")
    m_sum+=("${BASH_REMATCH[5]}")
  done <"$manifest"
  ((${#m_volume[@]})) || die "$manifest has no volumes"
}

# The volumes this run created, removed again if the import fails.
created=()
rollback() {
  local name
  for name in ${created[@]+"${created[@]}"}; do
    echo "volumes: removing $name, created by this run" >&2
    docker volume rm "$name" >/dev/null || echo "volumes: could not remove $name" >&2
  done
  created=()
}

# check_tars: every tar file is there, and as big and as intact as the export said. All of them are
# looked at, and all the problems told, before anything is created.
check_tars() {
  local -a problems=()
  local i file sum
  for i in "${!m_volume[@]}"; do
    file="$dir/${m_volume[i]}.tar"
    if [[ ! -f "$file" ]]; then
      problems+=("${m_volume[i]}: $file is missing")
    elif [[ "$(stat -c %s -- "$file")" != "${m_bytes[i]}" ]]; then
      problems+=("${m_volume[i]}: $file is not ${m_bytes[i]} bytes")
    else
      sum=$(sha256_of "$file") || die "cannot checksum $file"
      [[ "$sum" == "${m_sum[i]}" ]] || problems+=("${m_volume[i]}: the sha256 of $file is not the manifest's")
    fi
  done
  if ((${#problems[@]})); then
    printf 'volumes: %s\n' "${problems[@]}" >&2
    die "nothing imported: ${#problems[@]} tar file(s) are not what was exported"
  fi
}

# check_volumes: a volume that exists is only reused if it is empty and has the manifest's labels.
check_volumes() {
  local -a problems=()
  local i name labels listing entries
  listing=$(docker volume ls --format '{{.Name}}') || die "cannot list the Docker volumes"
  for i in "${!m_volume[@]}"; do
    name=${m_volume[i]}
    grep -qxF -- "$name" <<<"$listing" || continue
    labels=$(volume_labels "$name") || die "cannot inspect the volume $name"
    if [[ "$labels" != "${m_project[i]}|${m_compose[i]}" ]]; then
      problems+=("$name exists with other labels (its $PROJECT_LABEL|$VOLUME_LABEL: '$labels'; the manifest's: '${m_project[i]}|${m_compose[i]}')")
      continue
    fi
    entries=$(docker run --rm --volume "$name:/volume:ro" "$TAR_IMAGE" ls -A /volume) \
      || die "cannot look into the volume $name"
    [[ -z "$entries" ]] || problems+=("$name exists and is not empty")
  done
  if ((${#problems[@]})); then
    printf 'volumes: %s\n' "${problems[@]}" >&2
    die "nothing imported: ${#problems[@]} volume(s) cannot be filled"
  fi
}

# create_and_fill: one volume after the other. It returns non-zero on the first failure and is called as
# the condition of an `if` (which turns -e off), so every step says `|| return 1`.
create_and_fill() {
  local i name listing
  listing=$(docker volume ls --format '{{.Name}}') || return 1
  for i in "${!m_volume[@]}"; do
    name=${m_volume[i]}
    if grep -qxF -- "$name" <<<"$listing"; then
      echo "volumes: $name exists, empty, with the same labels: filling it"
    else
      echo "volumes: creating $name"
      docker volume create --label "$PROJECT_LABEL=${m_project[i]}" --label "$VOLUME_LABEL=${m_compose[i]}" \
        "$name" >/dev/null || return 1
      created+=("$name")
    fi
    # The tar is unpacked INSIDE a container of the engine this runs on, which is what makes ownership
    # come out right between a rootful and a rootless engine. On the old VPS the postgres files are
    # owned by uid 999 of the host. On the apps machine the rootless engine runs in a user namespace:
    # its container root is ygg, and its container uid 999 is some other, subordinate, uid on the host
    # (from /etc/subuid). Unpacked by a tar in a container, as container root, uid 999 in the tar becomes
    # 999 *in that namespace*, which is the uid the postgres container sees and expects. Unpacked by a
    # host tar (as ygg, or as root) the files would be ygg's, or a host uid the engine does not map to
    # 999, and postgres would refuse to start. -p keeps the modes; --numeric-owner takes the ids from the
    # tar, not the names, which this image's /etc/passwd does not have.
    docker run --rm -i --volume "$name:/volume" "$TAR_IMAGE" \
      tar --numeric-owner -C /volume -xpf - <"$dir/$name.tar" || return 1
  done
}

interrupted() {
  echo "volumes: interrupted" >&2
  rollback
  exit 130
}

do_import() {
  select_engine
  check_tars
  check_volumes
  trap interrupted INT TERM
  if ! create_and_fill; then
    echo "volumes: the import failed" >&2
    rollback
    exit 1
  fi
  trap - INT TERM
  echo "volumes: imported ${#m_volume[@]} volume(s) from $dir"
}

# ---- main ---------------------------------------------------------------------------------------

case "$command" in
  export)
    for name in "$@"; do
      name_ok "$name" || die "'$name' is not a volume name"
    done
    select_engine
    do_export "$@"
    ;;
  import)
    read_manifest
    do_import
    ;;
esac
