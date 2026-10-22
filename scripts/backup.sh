#!/usr/bin/env bash
# Backs up this machine's data into ~/backups/<YYYY-MM-DD>/ and sends ~/backups to the other machine.
#
#     scripts/backup.sh run | list
#
# Run as ygg (the nightly timer, platform/systemd/yggdrasil-backup.timer, does), on the user's own
# rootless engine. Reads <secrets>/backup.conf, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil).
# Template: env/backup.conf.example. run dumps every item of the file, drops the local days beyond
# BACKUP_KEEP and rsyncs ~/backups to BACKUP_TARGET; one item failing doesn't stop the others, but the
# exit status says so. list shows the days and files that are in ~/backups.
set -euo pipefail
# The dumps hold the databases and Forgejo's repositories: the directories and files are the user's alone.
umask 077

die() { echo "backup: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless and use_rootless_engine, shared with platform.sh.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"
secrets=${YGG_SECRETS_DIR:-/etc/yggdrasil}
conf="$secrets/backup.conf"
backups="$HOME/backups"
usage="usage: backup.sh run | list"
command=${1:-}
[[ "$command" =~ ^(run|list)$ && $# -eq 1 ]] || die "$usage"

# The forgejo container is found by the labels Compose puts on it, not by its name: the name depends on
# the Compose version. Its rootless image runs as uid 1000, with the data in /var/lib/gitea.
FORGEJO_PROJECT=yggdrasil
FORGEJO_SERVICE=forgejo
FORGEJO_CONFIG=/var/lib/gitea/custom/conf/app.ini
FORGEJO_DUMP=/tmp/forgejo-dump.zip

# ---- list ---------------------------------------------------------------------------------------

if [[ "$command" == list ]]; then
  found=""
  for day in "$backups"/*/; do
    [[ -d "$day" ]] || continue
    found=1
    basename "$day"
    for file in "$day"*; do
      [[ -f "$file" ]] || continue
      printf '  %s  %s\n' "$(basename "$file")" "$(du -h "$file" | cut -f1)"
    done
  done
  [[ -n "$found" ]] || echo "No backups yet ($backups)."
  exit 0
fi

# ---- The configuration --------------------------------------------------------------------------

[[ -f "$conf" ]] || die "missing $conf (template: env/backup.conf.example)"

BACKUP_TARGET="" BACKUP_KEY="$HOME/.ssh/yggdrasil-backup" BACKUP_KEEP=7
# The items, each "<kind> <arguments>" as written.
items=()

# A name that ends up in a file name or a container command: no slash, no leading dash.
name_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }

# The whole file is read, and refused if anything in it is wrong, before the first dump: a typo found
# at 03:30 in the sixth item would leave the first five dumped and the night's run a failure.
number=0
while IFS= read -r line || [[ -n "$line" ]]; do
  number=$((number + 1))
  line=${line%$'\r'}
  # Leading and trailing blanks go; a line that is a comment or empty is skipped.
  line=${line#"${line%%[![:space:]]*}"}
  line=${line%"${line##*[![:space:]]}"}
  [[ -n "$line" && "$line" != \#* ]] || continue
  if [[ "$line" =~ ^(BACKUP_TARGET|BACKUP_KEY|BACKUP_KEEP)=(.*)$ ]]; then
    setting=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    # Quotes around the value are allowed, as in the env files; a leading ~/ is the home.
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then value=${BASH_REMATCH[1]}; fi
    [[ "$value" != "~/"* ]] || value="$HOME/${value#\~/}"
    # An empty value keeps the default (the template ships BACKUP_TARGET= empty on purpose).
    [[ -z "$value" ]] || printf -v "$setting" '%s' "$value"
    continue
  fi
  read -r -a words <<<"$line"
  case ${words[0]} in
    forgejo) ((${#words[@]} == 1)) || die "$conf line $number: forgejo takes no arguments: $line" ;;
    volume)
      if ((${#words[@]} != 2)) || ! name_ok "${words[1]}"; then
        die "$conf line $number: expected 'volume <name>': $line"
      fi
      ;;
    postgres)
      if ((${#words[@]} != 3)) || ! name_ok "${words[1]}" || ! name_ok "${words[2]}"; then
        die "$conf line $number: expected 'postgres <container> <user>': $line"
      fi
      ;;
    *) die "$conf line $number: unknown item kind '${words[0]}' (forgejo, volume <name>, postgres <container> <user>): $line" ;;
  esac
  items+=("${words[*]}")
done <"$conf"

[[ -n "$BACKUP_TARGET" ]] || die "BACKUP_TARGET is not set in $conf (<user>@<host>:<path>)"
[[ "$BACKUP_KEEP" =~ ^[1-9][0-9]*$ ]] || die "BACKUP_KEEP in $conf must be a number of days, 1 or more, not '$BACKUP_KEEP'"
# The template ships its items commented out: a copy nobody finished would otherwise "back up" nothing
# every night and report success.
((${#items[@]})) || die "no items in $conf: uncomment or add forgejo, volume <name> or postgres <container> <user> lines (template: env/backup.conf.example)"

# rsync splits -e on spaces and honours quotes, not backslashes: a key path with a blank is single-quoted.
if [[ "$BACKUP_KEY" =~ ^[A-Za-z0-9_./+@:-]+$ ]]; then
  ssh_command="ssh -i $BACKUP_KEY -o BatchMode=yes"
elif [[ "$BACKUP_KEY" == *"'"* ]]; then
  die "BACKUP_KEY in $conf must not contain a single quote: $BACKUP_KEY"
else
  ssh_command="ssh -i '$BACKUP_KEY' -o BatchMode=yes"
fi

# ---- The items ----------------------------------------------------------------------------------

# Each dump is written to <file>.partial and renamed when it is whole, so a failing one never leaves a
# file that looks like a backup. They return non-zero instead of dying, and are called as the condition
# of an `if` (which turns -e off), so every step says `|| return 1`.

# dump_forgejo <file>: Forgejo's own dump is made inside the container, copied out and removed from it.
dump_forgejo() {
  local out=$1 container status=0
  container=$(docker ps --quiet \
    --filter "label=com.docker.compose.project=$FORGEJO_PROJECT" \
    --filter "label=com.docker.compose.service=$FORGEJO_SERVICE") || return 1
  if [[ -z "$container" || "$container" == *$'\n'* ]]; then
    echo "backup: expected one running $FORGEJO_SERVICE container of the $FORGEJO_PROJECT project, found: ${container:-none}" >&2
    return 1
  fi
  # A dump left by an earlier run would make forgejo refuse to write.
  docker exec "$container" rm -f "$FORGEJO_DUMP" || return 1
  docker exec "$container" forgejo dump --config "$FORGEJO_CONFIG" --type zip --file "$FORGEJO_DUMP" || status=1
  [[ $status -ne 0 ]] || docker cp "$container:$FORGEJO_DUMP" "$out.partial" || status=1
  docker exec "$container" rm -f "$FORGEJO_DUMP" || true
  return "$status"
}

# dump_volume <name> <file>: a throwaway busybox container with the volume mounted read-only: the data is
# only read, and whatever uses the volume doesn't have to stop.
dump_volume() {
  docker run --rm --volume "$1:/volume:ro" busybox tar -C /volume -czf - . >"$2.partial" || return 1
}

# dump_postgres <container> <user> <file>
dump_postgres() {
  docker exec "$1" pg_dumpall -U "$2" | gzip >"$3.partial" || return 1
}

# run_item <item>: dumps one item into today's directory.
run_item() {
  local -a words
  read -r -a words <<<"$1"
  local out status=0
  case ${words[0]} in
    forgejo) out="$day/forgejo.zip"; dump_forgejo "$out" || status=1 ;;
    volume) out="$day/volume-${words[1]}.tar.gz"; dump_volume "${words[1]}" "$out" || status=1 ;;
    postgres) out="$day/postgres-${words[1]}.sql.gz"; dump_postgres "${words[1]}" "${words[2]}" "$out" || status=1 ;;
  esac
  if [[ $status -eq 0 ]]; then
    mv "$out.partial" "$out" || status=1
  fi
  [[ $status -eq 0 ]] || rm -f "$out.partial"
  return "$status"
}

# ---- run ----------------------------------------------------------------------------------------

# One run at a time: two overlapping ones (a slow night and the next timer, or a run by hand) would dump
# into the same files and prune under each other. The lock is on ~/backups itself and goes away with the
# process.
mkdir -p "$backups"
exec 9<"$backups"
flock -n 9 || die "another backup is running"

use_rootless_engine

day="$backups/$(date +%F)"
mkdir -p "$day"
failed=()
for item in "${items[@]}"; do
  echo "backup: $item"
  if ! run_item "$item"; then
    echo "backup: FAILED: $item" >&2
    failed+=("$item")
  fi
done
# A day that got nothing isn't a day of backups: it must not count against the ones that did.
rmdir "$day" 2>/dev/null || true

# Only the newest BACKUP_KEEP days stay (the names sort by date); anything else in ~/backups is left.
days=()
for entry in "$backups"/*/; do
  if [[ -d "$entry" && "$(basename "$entry")" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    days+=("$(basename "$entry")")
  fi
done
if ((${#days[@]} > BACKUP_KEEP)); then
  for old in "${days[@]:0:${#days[@]}-BACKUP_KEEP}"; do
    echo "backup: removing $old"
    rm -rf -- "${backups:?}/$old"
  done
fi

# --delete: the other machine's copy has the same days as this one, not every day there ever was.
if ! rsync -a --delete -e "$ssh_command" "$backups/" "$BACKUP_TARGET"; then
  echo "backup: FAILED: sending $backups to $BACKUP_TARGET" >&2
  failed+=("rsync to $BACKUP_TARGET")
fi

if ((${#failed[@]})); then
  joined=$(printf '%s; ' "${failed[@]}")
  die "failed: ${joined%; }"
fi
echo "backup: done"
