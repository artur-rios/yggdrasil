#!/usr/bin/env bash
# Backs up this machine's data into ~/backups/<YYYY-MM-DD>/ and sends ~/backups to the other machine.
#
#     scripts/backup.sh run | list
#
# Run as ygg (the nightly timer, platform/systemd/yggdrasil-backup.timer, does), on the user's own
# rootless engine. Reads <secrets>/backup.conf, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil).
# Template: env/backup.conf.example. run dumps every item of the file, drops the local days beyond
# BACKUP_KEEP (except, for each item, its newest good file, wherever it is) and rsyncs ~/backups to
# BACKUP_TARGET; one item failing doesn't stop the others, but the exit status says so. list shows the
# days and files that are in ~/backups, and whether the newest day has every item of backup.conf.
set -euo pipefail
# The dumps hold the databases and Forgejo's repositories: the directories and files are the user's alone.
umask 077

die() { echo "backup: $*" >&2; exit 1; }

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless, use_rootless_engine and TAR_IMAGE (what tars a volume), shared with
# platform.sh and volumes.sh.
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
# How many seconds a stopped database container gets to accept connections once started for its dump.
POSTGRES_START_WAIT=${BACKUP_POSTGRES_START_WAIT:-60}
# A day of ~/backups is a directory named by its date.
DAY_PATTERN='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'

# ---- The configuration --------------------------------------------------------------------------

BACKUP_TARGET="" BACKUP_KEY="$HOME/.ssh/yggdrasil-backup" BACKUP_KEEP=7
# The items, each "<kind> <arguments>" as written.
items=()

# A name that ends up in a file name or a container command: no slash, no leading dash.
name_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }

# read_conf: reads backup.conf into the settings and items. The whole file is read, and refused if
# anything in it is wrong, before the first dump: a typo found at 03:30 in the sixth item would leave
# the first five dumped and the night's run a failure.
read_conf() {
  [[ -f "$conf" ]] || die "missing $conf (template: env/backup.conf.example)"
  local number=0 line setting value words
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
      [[ "$value" != \~/* ]] || value="$HOME/${value#\~/}"
      # An empty value keeps the default (the template ships BACKUP_TARGET= empty on purpose).
      [[ -z "$value" ]] || printf -v "$setting" '%s' "$value"
      continue
    fi
    read -r -a words <<<"$line"
    case ${words[0]} in
      forgejo | vars) ((${#words[@]} == 1)) || die "$conf line $number: ${words[0]} takes no arguments: $line" ;;
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
      *) die "$conf line $number: unknown item kind '${words[0]}' (forgejo, vars, volume <name>, postgres <container> <user>): $line" ;;
    esac
    items+=("${words[*]}")
  done <"$conf"
}

# item_file <item>: the name of the file the item's dump is in, inside a day.
item_file() {
  local -a words
  read -r -a words <<<"$1"
  case ${words[0]} in
    forgejo) echo forgejo.zip ;;
    vars) echo vars.tar.gz ;;
    volume) echo "volume-${words[1]}.tar.gz" ;;
    postgres) echo "postgres-${words[1]}.sql.gz" ;;
  esac
}

# backup_days: the days in ~/backups, oldest first, one per line (the names sort by date).
backup_days() {
  local entry
  for entry in "$backups"/*/; do
    if [[ -d "$entry" && "$(basename "$entry")" =~ $DAY_PATTERN ]]; then
      basename "$entry"
    fi
  done
}

# ---- list ---------------------------------------------------------------------------------------

# The days and their files, then the newest day and whether it has a file for every item of
# backup.conf: a night whose run failed halfway leaves a day that looks like the others.
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
  if [[ -z "$found" ]]; then
    echo "No backups yet ($backups)."
    exit 0
  fi
  newest=$(backup_days | tail -n1)
  [[ -n "$newest" ]] || exit 0
  # In a subshell: a backup.conf that is missing or wrong says so on its own, and list goes on.
  if expected=$(read_conf 2>/dev/null && for item in ${items[@]+"${items[@]}"}; do item_file "$item"; done) \
    && [[ -n "$expected" ]]; then
    missing=()
    while IFS= read -r file; do
      [[ -f "$backups/$newest/$file" ]] || missing+=("$file")
    done <<<"$expected"
    if ((${#missing[@]})); then
      joined=$(printf '%s, ' "${missing[@]}")
      echo "Newest: $newest, INCOMPLETE: no ${joined%, }"
    else
      echo "Newest: $newest, complete (every item of backup.conf)"
    fi
  else
    echo "Newest: $newest (whether it is complete needs a backup.conf with items: $conf)"
  fi
  exit 0
fi

# ---- The settings -------------------------------------------------------------------------------

read_conf
[[ -n "$BACKUP_TARGET" ]] || die "BACKUP_TARGET is not set in $conf (<user>@<host>:<path>)"
[[ "$BACKUP_KEEP" =~ ^[1-9][0-9]*$ ]] || die "BACKUP_KEEP in $conf must be a number of days, 1 or more, not '$BACKUP_KEEP'"
# The template ships its items commented out: a copy nobody finished would otherwise "back up" nothing
# every night and report success.
((${#items[@]})) || die "no items in $conf: uncomment or add forgejo, vars, volume <name> or postgres <container> <user> lines (template: env/backup.conf.example)"

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

# dump_vars <file>: the variables store and its key ($secrets/vars.db and vars.key), as vars.db and
# vars.key in one archive. vars.py's backup takes a consistent copy while the store is in use; the key
# goes with it, since a store without its key cannot be read, and a machine that is lost takes its
# key along. Restored by unpacking both into the secrets directory (docs/hosts.md, Restore).
dump_vars() {
  local out=$1 tmp status=0
  tmp=$(mktemp -d) || return 1
  if YGG_SECRETS_DIR=$secrets python3 "$root/scripts/vars.py" backup "$tmp" >/dev/null; then
    mv "$tmp"/vars-*.db "$tmp/vars.db" && mv "$tmp"/vars-*.key "$tmp/vars.key" \
      && tar -C "$tmp" -czf "$out.partial" vars.db vars.key || status=1
  else
    status=1
  fi
  rm -rf -- "$tmp"
  return "$status"
}

# dump_volume <name> <file>: a throwaway container of the pinned TAR_IMAGE with the volume mounted
# read-only: the data is only read, and whatever uses the volume doesn't have to stop.
dump_volume() {
  docker run --rm --volume "$1:/volume:ro" "$TAR_IMAGE" tar -C /volume -czf - . >"$2.partial" || return 1
}

# dump_postgres <container> <user> <file>. The database of an on-demand environment is stopped most
# nights, and `docker exec` cannot reach a stopped container: it is started for the dump, alone (the
# API that uses it stays stopped), and stopped again, so every night's file is that night's data. Only
# a stopped one (exited, or created and never run): a restarting, paused or dead one fails the item,
# since starting and then stopping it would mark it stopped by hand, and `restart: unless-stopped`
# would no longer bring it back. It is not stopped again when another container of its Compose project
# runs by then (a deploy or `ygg env start` meanwhile), and a run that is killed stops it on the way out.
dump_postgres() {
  local container=$1 user=$2 out=$3 state status=0 i
  state=$(docker inspect --format '{{.State.Status}}' "$container") || return 1
  case $state in
    running) ;;
    exited | created)
      echo "backup: $container is stopped: starting it for the dump"
      docker start "$container" >/dev/null || return 1
      started_container=$container
      # Over TCP, as the stacks' health check: while a new volume is initialised the socket answers too.
      status=1
      for ((i = 0; i < POSTGRES_START_WAIT; i++)); do
        if docker exec "$container" pg_isready --quiet --username "$user" --host 127.0.0.1; then
          status=0
          break
        fi
        sleep 1
      done
      [[ $status -eq 0 ]] || echo "backup: $container did not accept connections within ${POSTGRES_START_WAIT}s" >&2
      ;;
    *)
      echo "backup: $container is $state, neither running nor stopped: not dumped" >&2
      return 1
      ;;
  esac
  if [[ $status -eq 0 ]]; then
    docker exec "$container" pg_dumpall -U "$user" | gzip >"$out.partial" || status=1
  fi
  if [[ -n "$started_container" ]]; then
    stop_started || status=1
  fi
  return "$status"
}

# stop_started: stops the database container dump_postgres started, unless another container of its
# Compose project runs now: then its environment was started meanwhile, and the database is in use.
started_container=""
stop_started() {
  local container=$started_container project own others id
  started_container=""
  [[ -n "$container" ]] || return 0
  project=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container") || project=""
  if [[ -n "$project" && "$project" != "<no value>" ]]; then
    own=$(docker inspect --format '{{.Id}}' "$container") || own=""
    others=$(docker ps --quiet --filter "label=com.docker.compose.project=$project") || others=""
    while read -r id; do
      # docker ps prints the short id, the start of the whole one; the database itself runs too.
      if [[ -n "$id" && "$own" != "$id"* ]]; then
        echo "backup: $project is running now: $container stays up"
        return 0
      fi
    done <<<"$others"
  fi
  docker stop "$container" >/dev/null || { echo "backup: could not stop $container again" >&2; return 1; }
}

# run_item <item>: dumps one item into today's directory.
run_item() {
  local out
  out="$day/$(item_file "$1")"
  local -a words
  read -r -a words <<<"$1"
  local status=0
  case ${words[0]} in
    forgejo) dump_forgejo "$out" || status=1 ;;
    vars) dump_vars "$out" || status=1 ;;
    volume) dump_volume "${words[1]}" "$out" || status=1 ;;
    postgres) dump_postgres "${words[1]}" "${words[2]}" "$out" || status=1 ;;
  esac
  if [[ $status -eq 0 ]]; then
    mv "$out.partial" "$out" || status=1
  fi
  [[ $status -eq 0 ]] || rm -f "$out.partial"
  return "$status"
}

# prune: only the newest BACKUP_KEEP days stay; anything in ~/backups that isn't a day is left. Except,
# for each item of backup.conf, its newest good file, wherever it is: an item that has failed for longer
# than BACKUP_KEEP days would otherwise lose its last good copy here, and (rsync --delete) on the other
# machine too. Such a day keeps that file alone.
prune() {
  local -a days=()
  local -A newest=()
  local item file old path name kept i
  mapfile -t days < <(backup_days)
  ((${#days[@]} > BACKUP_KEEP)) || return 0
  for item in "${items[@]}"; do
    file=$(item_file "$item")
    for ((i = ${#days[@]} - 1; i >= 0; i--)); do
      if [[ -f "$backups/${days[i]}/$file" ]]; then
        newest[$file]=${days[i]}
        break
      fi
    done
  done
  for old in "${days[@]:0:${#days[@]}-BACKUP_KEEP}"; do
    kept=""
    for path in "$backups/$old"/* "$backups/$old"/.[!.]*; do
      [[ -e "$path" || -L "$path" ]] || continue
      name=$(basename "$path")
      if [[ "${newest[$name]:-}" == "$old" ]]; then
        echo "backup: keeping $old/$name: the newest good $name there is"
        kept=1
      else
        rm -rf -- "$path"
      fi
    done
    if [[ -z "$kept" ]]; then
      echo "backup: removing $old"
      rm -rf -- "${backups:?}/$old"
    fi
  done
}

# ---- run ----------------------------------------------------------------------------------------

# One run at a time: two overlapping ones (a slow night and the next timer, or a run by hand) would dump
# into the same files and prune under each other. The lock is on ~/backups itself and goes away with the
# process. Fd 9 is closed for everything this starts (9>&- on the calls below; bash keeps its own copy,
# close-on-exec): a child that outlived the run (an ssh master kept by ControlPersist) would otherwise
# hold the lock, and every later night would find "another backup is running".
mkdir -p "$backups"
exec 9<"$backups"
flock -n 9 || die "another backup is running"

use_rootless_engine 9>&-
# A run stopped half-way (systemd, a signal) does not leave behind a database it started.
trap 'stop_started 9>&- || true' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

day="$backups/$(date +%F)"
mkdir -p "$day"
failed=()
for item in "${items[@]}"; do
  echo "backup: $item"
  if ! run_item "$item" 9>&-; then
    echo "backup: FAILED: $item" >&2
    failed+=("$item")
  fi
done
# A day that got nothing isn't a day of backups: it must not count against the ones that did.
rmdir "$day" 2>/dev/null || true

prune

# --delete: the other machine's copy has the same days as this one, not every day there ever was.
if ! rsync -a --delete -e "$ssh_command" "$backups/" "$BACKUP_TARGET" 9>&-; then
  echo "backup: FAILED: sending $backups to $BACKUP_TARGET" >&2
  failed+=("rsync to $BACKUP_TARGET")
fi

if ((${#failed[@]})); then
  joined=$(printf '%s; ' "${failed[@]}")
  die "failed: ${joined%; }"
fi
echo "backup: done"
