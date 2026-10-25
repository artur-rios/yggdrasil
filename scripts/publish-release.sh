#!/usr/bin/env bash
# Publishes a release on the installation's git server (Forgejo by default) and, when asked, a copy on
# GitHub: the tag, a draft release with the notes and every file of a directory, checked, then published.
# Runs on the publish agent, the one that holds the write token and the GitHub App's token, and does every
# call through scripts/forge.sh: it never learns how either server's API looks.
#
#     scripts/publish-release.sh <repo> <tag> <sha> <title> <notes-file> <files-dir> [--mirror-releases] [--missing <list>] [--replace-files]
#     scripts/publish-release.sh attach <repo> <tag> <files-dir> [--mirror-releases]
#
# The first form, per server:
#   1. the tag: created on <sha> if the server has none; a tag on another commit is refused. On GitHub the
#      tag is pushed by the mirror (Forgejo's push mirror), so the script does not make it there: it waits
#      for it (scripts/github.sh wait-ref) and checks it is on <sha>;
#   2. a draft release for the tag: the one a failed earlier run left, else a new one with the notes;
#   3. every file under <files-dir> (any depth, by its file name) the release does not have yet with the
#      same size is uploaded; a file that is there under the same name with another size stops the run
#      (the owner deletes it: there is no command to);
#   4. the release's files are read again and must hold every file of <files-dir> with its size.
# Only when both servers' drafts are complete is Forgejo's published, then GitHub's, last: a release
# that is public on GitHub has been published everywhere else. A run that stopped half way is started
# again with the same arguments: the tag, the drafts and the files that are there are reused, and a
# release that is already published is left as it is.
#
# --mirror-releases (the installation is Forgejo and the catalog's mirror.releases is on) does the same
# steps on GitHub, as the GitHub App. --missing <list> adds a "Missing files" section to the notes: the
# files that could not be built (a skipped GitHub Actions run), separated by commas or spaces.
# --replace-files (a release with release.finalize, whose manifest and signature describe this run's files):
# on a draft, a file already there under the same name is deleted and uploaded again whatever its size, so
# the draft holds exactly this run's bytes, never an older file of the same name and size. A published
# release is never changed: its files are checked by name and size as without the option.
#
# `attach` puts the files of <files-dir> on the releases that exist already, published or not, the same
# way (steps 3 and 4), for the Windows files that arrive after a release was published. Both releases are
# looked up first; with --mirror-releases and no release of the tag on GitHub (its copy pending: the
# release was published after a skip, on the git server only), the files go to the git server's release
# only and the exit is 5.
#
# Prints the release's address on the git server, then (with --mirror-releases) GitHub's, one per line.
# Exit 0 is success; 1 a step failed, explained on stderr as "publish-release: <what> failed: <why>" (the
# drafts stay); 2 a wrong command line, or --mirror-releases without GH_TOKEN and GITHUB_OWNER; 5 (attach
# only) attached on the git server, GitHub has no release of the tag yet (the release-resume job makes it).
#
# Environment: the installation's git server as forge.sh reads it (FORGE_TYPE, FORGE_API_URL,
# FORGE_OWNER, FORGE_TOKEN; on a GitHub installation GITHUB_OWNER, GH_TOKEN...), and for --mirror-releases
# GH_TOKEN, GITHUB_OWNER, GITHUB_API_URL (default https://api.github.com) and GITHUB_UPLOADS_URL.
#   GITHUB_TAG_WAIT   seconds to wait for the mirror to push the tag to GitHub  (default 600)
#   YGG_FORGE_SH      replaces scripts/forge.sh, and YGG_GITHUB_SH scripts/github.sh (for the tests)
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
forge_sh=${YGG_FORGE_SH:-$here/forge.sh}
github_sh=${YGG_GITHUB_SH:-$here/github.sh}
tag_wait=${GITHUB_TAG_WAIT:-600}

usage() {
  cat >&2 <<'EOF'
usage: publish-release.sh <repo> <tag> <sha> <title> <notes-file> <files-dir> [--mirror-releases] [--missing <list>] [--replace-files]
       publish-release.sh attach <repo> <tag> <files-dir> [--mirror-releases]
EOF
  exit 2
}

# fail <what> [<why>]: a step failed; the drafts stay.
fail() {
  echo "publish-release: $1 failed${2:+: $2}" >&2
  exit 1
}

# ---- Arguments ----

mode=publish
[[ "${1:-}" != attach ]] || { mode=attach; shift; }
mirror=false
missing=""
replace=false
positional=()
while (($#)); do
  case "$1" in
    --mirror-releases) mirror=true ;;
    --replace-files)
      [[ "$mode" == publish ]] || usage
      replace=true
      ;;
    --missing)
      [[ "$mode" == publish && $# -ge 2 ]] || usage
      missing=$2
      shift
      ;;
    -*) usage ;;
    *) positional+=("$1") ;;
  esac
  shift
done
if [[ "$mode" == attach ]]; then
  ((${#positional[@]} == 3)) || usage
  repo=${positional[0]} tag=${positional[1]} files_dir=${positional[2]}
else
  ((${#positional[@]} == 6)) || usage
  repo=${positional[0]} tag=${positional[1]} sha=${positional[2]} title=${positional[3]}
  notes_file=${positional[4]} files_dir=${positional[5]}
  [[ -f "$notes_file" ]] || fail "reading the notes" "no file $notes_file"
fi
[[ -d "$files_dir" ]] || fail "reading the files" "no directory $files_dir"

# GitHub is not the installation's own server: it is reached as the GitHub App, whose token the caller gives.
github_api_url=${GITHUB_API_URL:-https://api.github.com}
if [[ "$mirror" == true && "${FORGE_TYPE:-}" == github ]]; then
  # Already GitHub: a copy of the release on GitHub would be the release itself.
  echo "publish-release: --mirror-releases is for a Forgejo installation, ignored" >&2
  mirror=false
fi
if [[ "$mirror" == true ]]; then
  if [[ -z "${GH_TOKEN:-}" || -z "${GITHUB_OWNER:-}" ]]; then
    echo "publish-release: --mirror-releases needs GH_TOKEN and GITHUB_OWNER" >&2
    exit 2
  fi
fi

case "${FORGE_TYPE:-}" in
  github) label_primary=GitHub ;;
  forgejo) label_primary=Forgejo ;;
  *) label_primary="the git server" ;;
esac

# ---- Calls ----

# call <what> <command...>: runs it and leaves its stdout in out, its stderr in err and its status in rc,
# never stopping the script: the callers tell the statuses apart (forge.sh exits 4 for "not there").
call() {
  local errors
  errors=$(mktemp)
  out=$("${@}" 2>"$errors") && rc=0 || rc=$?
  err=$(<"$errors")
  rm -f "$errors"
  return 0
}

# forge <side> <forge.sh command...>: side is primary (the installation's server, as the environment says)
# or mirror (GitHub, as the GitHub App).
forge() {
  local side=$1
  shift
  if [[ "$side" == mirror ]]; then
    call env FORGE_TYPE=github FORGE_API_URL="$github_api_url" FORGE_OWNER="$GITHUB_OWNER" FORGE_TOKEN="$GH_TOKEN" \
      bash "$forge_sh" "$@"
  else
    call bash "$forge_sh" "$@"
  fi
}

# must_forge <what> <side> <forge.sh command...>: the same, and a failure ends the run.
must_forge() {
  local what=$1
  shift
  forge "$@"
  ((rc == 0)) || fail "$what" "${err:-forge.sh exited $rc}"
}

label() { [[ "$1" == mirror ]] && echo GitHub || echo "$label_primary"; }

# ---- Files ----

# The files of the directory, by name: names[] and paths[] (a file's name is what the release shows).
names=() paths=()
declare -A local_sizes=()
while IFS= read -r -d '' path; do
  name=${path##*/}
  [[ -z "${local_sizes[$name]:-}" ]] || fail "reading the files" "two files are named $name"
  names+=("$name")
  paths+=("$path")
  local_sizes[$name]=$(wc -c <"$path" | tr -d '[:space:]')
done < <(find "$files_dir" -type f -print0 | sort -z)

# remote_files <side> <release-id>: fills remote_sizes (name -> size) from the server.
declare -A remote_sizes=()
remote_files() {
  local side=$1 id=$2 name size
  must_forge "reading the files of release $id on $(label "$side")" "$side" release-files "$repo" "$id"
  remote_sizes=()
  while IFS=$'\t' read -r name size; do
    [[ -z "$name" ]] || remote_sizes[$name]=$size
  done <<<"$out"
}

# upload_files <side> <release-id>: what the release does not have yet, then a check of all of it. With
# --replace-files, on a draft, what it has under the same name is replaced.
upload_files() {
  local side=$1 id=$2 index name
  remote_files "$side" "$id"
  for index in "${!names[@]}"; do
    name=${names[$index]}
    if [[ -n "${remote_sizes[$name]+set}" && "$replace" == true && "${release_draft[$side]:-}" == true ]]; then
      must_forge "replacing $name on $(label "$side")" "$side" delete-release-file "$repo" "$id" "$name"
      unset "remote_sizes[$name]"
    fi
    if [[ -n "${remote_sizes[$name]+set}" ]]; then
      [[ "${remote_sizes[$name]}" == "${local_sizes[$name]}" ]] \
        || fail "uploading $name to $(label "$side")" "the release has a file of that name with ${remote_sizes[$name]} bytes, not ${local_sizes[$name]}; delete it and run again"
      continue
    fi
    must_forge "uploading $name to $(label "$side")" "$side" upload-release-file "$repo" "$id" "${paths[$index]}"
  done
  remote_files "$side" "$id"
  for name in "${names[@]}"; do
    [[ "${remote_sizes[$name]:-}" == "${local_sizes[$name]}" ]] \
      || fail "checking the files of $(label "$side")" "$name has ${remote_sizes[$name]:-no file} there, ${local_sizes[$name]} bytes here"
  done
}

# ---- The release, per side ----

declare -A release_id=() release_url=() release_draft=()

# read_release <side>: from find-release's "<id>\t<url>\t<draft>" in out.
read_release() {
  local side=$1
  IFS=$'\t' read -r "release_id[$side]" "release_url[$side]" "release_draft[$side]" <<<"$out"
  [[ -n "${release_id[$side]}" && -n "${release_url[$side]}" ]] || fail "reading the release on $(label "$side")" "the answer is '$out'"
}

# the_tag <side>
the_tag() {
  local side=$1
  if [[ "$side" == mirror ]]; then
    # The push mirror makes the tag on GitHub; this waits for it. wait-ref exits 2 when the time is up.
    call bash "$github_sh" wait-ref "$repo" "refs/tags/$tag" "$tag_wait"
    ((rc == 0)) || fail "waiting for tag $tag on GitHub" "${err:-github.sh exited $rc}"
    must_forge "reading tag $tag on GitHub" "$side" tag-sha "$repo" "$tag"
    [[ "$out" == "$sha" ]] || fail "checking tag $tag on GitHub" "it points to $out, not $sha"
    return 0
  fi
  forge "$side" tag-sha "$repo" "$tag"
  case "$rc" in
    0) [[ "$out" == "$sha" ]] || fail "checking tag $tag on $(label "$side")" "it points to $out, not $sha" ;;
    4) must_forge "creating tag $tag on $(label "$side")" "$side" create-tag "$repo" "$tag" "$sha" ;;
    *) fail "reading tag $tag on $(label "$side")" "${err:-forge.sh exited $rc}" ;;
  esac
}

# the_draft <side>: the release of the tag, a draft made now when there is none.
the_draft() {
  local side=$1
  forge "$side" find-release "$repo" "$tag"
  case "$rc" in
    0) read_release "$side" ;;
    4)
      must_forge "creating the draft release on $(label "$side")" "$side" create-release "$repo" "$tag" "$sha" "$title" \
        --draft --notes-file "$release_notes"
      IFS=$'\t' read -r "release_id[$side]" "release_url[$side]" <<<"$out"
      release_draft[$side]=true
      ;;
    *) fail "looking for the release of $tag on $(label "$side")" "${err:-forge.sh exited $rc}" ;;
  esac
}

# the_existing_release <side>: attach puts files on a release that is there.
the_existing_release() {
  local side=$1
  forge "$side" find-release "$repo" "$tag"
  case "$rc" in
    0) read_release "$side" ;;
    4) fail "looking for the release of $tag on $(label "$side")" "there is none" ;;
    *) fail "looking for the release of $tag on $(label "$side")" "${err:-forge.sh exited $rc}" ;;
  esac
}

sides=(primary)
[[ "$mirror" != true ]] || sides+=(mirror)

pending=false
if [[ "$mode" == attach ]]; then
  # Both releases looked up before anything is uploaded. GitHub without one is its copy still to make
  # (a release published after a skip): the files go to the git server's release only, and exit 5 says so.
  for side in "${sides[@]}"; do
    if [[ "$side" == mirror ]]; then
      forge mirror find-release "$repo" "$tag"
      case "$rc" in
        0) read_release mirror ;;
        4) pending=true ;;
        *) fail "looking for the release of $tag on GitHub" "${err:-forge.sh exited $rc}" ;;
      esac
    else
      the_existing_release "$side"
    fi
  done
  [[ "$pending" != true ]] || sides=(primary)
  for side in "${sides[@]}"; do
    upload_files "$side" "${release_id[$side]}"
  done
else
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  release_notes=$tmp/notes.md
  cp "$notes_file" "$release_notes"
  if [[ -n "$missing" ]]; then
    {
      printf '\n\n## Missing files\n\nThese files could not be built and are not attached to this release:\n\n'
      tr -s ',[:space:]' '\n' <<<"$missing" | while IFS= read -r item; do
        [[ -z "$item" ]] || printf -- '- %s\n' "$item"
      done
    } >>"$release_notes"
  fi

  # Everything that can fail is done on both servers before anything is published.
  for side in "${sides[@]}"; do
    the_tag "$side"
    the_draft "$side"
    upload_files "$side" "${release_id[$side]}"
  done
  # Forgejo first, GitHub last. A release that is not a draft any more (an earlier run got this far) stays.
  for side in "${sides[@]}"; do
    if [[ "${release_draft[$side]}" == true ]]; then
      must_forge "publishing the release on $(label "$side")" "$side" publish-release "$repo" "${release_id[$side]}"
      release_url[$side]=$out
    fi
  done
fi

for side in "${sides[@]}"; do
  printf '%s\n' "${release_url[$side]}"
done
if [[ "$pending" == true ]]; then
  echo "publish-release: GitHub has no release of $tag yet (its copy is pending): the files are on $label_primary's release only; the release-resume job makes the GitHub copy, with them" >&2
  exit 5
fi
