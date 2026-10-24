#!/usr/bin/env bash
# The git server's side of a release, the same small commands for every supported server: the pipeline
# and the other scripts call these and never learn whether the repositories live on Forgejo or GitHub.
# Needs curl, jq and (only when the environment below does not say) python3 with PyYAML.
#
#     scripts/forge.sh set-status     <repo> <sha> <state> <context> <description> [target-url]
#     scripts/forge.sh get-statuses   <repo> <sha>                  -> "<context>\t<state>\t<creator>" per line
#     scripts/forge.sh merge-pr       <repo> <number> <sha> <title> -> the merge commit
#     scripts/forge.sh create-tag     <repo> <tag> <sha>
#     scripts/forge.sh create-release <repo> <tag> <sha> <title> [--draft] [--notes-file <file>]
#                                                                   -> "<id>\t<html_url>"
#     scripts/forge.sh upload-release-file <repo> <release-id> <file> -> the file's download URL
#     scripts/forge.sh publish-release <repo> <release-id>          -> the release's html_url
#     scripts/forge.sh find-release   <repo> <tag>                  -> "<id>\t<html_url>\t<draft: true|false>"
#     scripts/forge.sh release-tags   <repo>                        -> "<tag>\t<target_commitish>" per published release
#     scripts/forge.sh release-files  <repo> <release-id>           -> "<name>\t<size>" per line
#     scripts/forge.sh tag-sha        <repo> <tag>                  -> the commit the tag points to
#     scripts/forge.sh delete-branch  <repo> <branch>
#
# state is pending, success, failure or error. get-statuses prints the latest state of each context
# (GitHub: the newest status in the commit's list of statuses; Forgejo: its combined status) and the
# login that created it (empty when the server has none: a deleted user), which is how the pipeline
# tells its own results from anyone else's. merge-pr makes a merge commit and fails if the pull
# request's head is no longer <sha>. delete-branch succeeds, with a note on stderr, when the branch
# is already gone. find-release finds a release, a draft too, by its tag (GitHub's own lookup by tag
# does not return drafts, so the list of releases is searched); tag-sha peels an annotated tag to the
# commit it points to. release-files lists the files attached to a release, which is how an upload is
# checked. release-tags lists the published releases (drafts left out), each with the target it was
# released from: create-release sends the release's commit as target_commitish, so for a release the
# pipeline made it is the commit the tag must still point to (what the tag-check job compares with
# tag-sha); a release made by hand may name a branch there instead.
#
# Exit 0 is success; 1 an error, explained on stderr as "forge: <what> failed: <the server's message>";
# 3 (delete-branch only) that the server keeps the branch, which is not an error to stop a release for;
# 4 (find-release and tag-sha only) that there is no such release or tag, which is an answer, not an
# error: the caller creates it, or waits for it;
# 2 that there was no command at all (it prints the list of commands).
# On Forgejo merge-pr waits (every 3 s, up to 20 times) while the server answers "try again later": it
# checks a pull request again in the background after a new commit status. The token reaches curl on
# stdin, never in an argument or a message. An unknown command is refused; a known command with the
# wrong arguments prints its usage. Reference: docs/setup.md, "The git server commands".
#
# <repo> is the repository name; the owner comes from the environment, else from the catalog:
#   FORGE_TYPE         forgejo or github                  (else catalog.py forge type)
#   FORGE_API_URL      the REST API's base URL            (else, for GitHub, GITHUB_API_URL, else catalog.py forge api)
#   FORGE_OWNER        the owner of the repositories      (else, for GitHub, GITHUB_OWNER, else catalog.py owner)
#   FORGE_TOKEN        the access token, required         (else, for GitHub, GH_TOKEN)
#   GITHUB_UPLOADS_URL where GitHub takes release files   (default https://uploads.github.com)
# and curl's limits, in seconds: YGG_CURL_CONNECT_TIMEOUT (10), YGG_CURL_MAX_TIME (60, an API call),
# YGG_CURL_TRANSFER_MAX_TIME (3600, an upload, which also stops when stalled under 1 KiB/s for 60 s).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

die() { echo "forge: $*" >&2; exit 1; }

# How long curl waits, so a server that hangs cannot hold an agent (and the release lock) forever.
# An API call: 10 s to connect, 60 s in all. An upload, which may be large: given up on only when
# it stalls under 1 KiB/s for a minute, or after an hour. YGG_CURL_CONNECT_TIMEOUT, YGG_CURL_MAX_TIME
# and YGG_CURL_TRANSFER_MAX_TIME (seconds) change them.
seconds() {
  local name=$1 value
  value=${!name-$2}
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be a whole number of seconds, not '$value'"
  printf '%s' "$value"
}
connect_timeout=$(seconds YGG_CURL_CONNECT_TIMEOUT 10) || exit 1
max_time=$(seconds YGG_CURL_MAX_TIME 60) || exit 1
transfer_max_time=$(seconds YGG_CURL_TRANSFER_MAX_TIME 3600) || exit 1
api_timeouts=(--connect-timeout "$connect_timeout" --max-time "$max_time")
transfer_timeouts=(--connect-timeout "$connect_timeout" --speed-limit 1024 --speed-time 60 --max-time "$transfer_max_time")

catalog() { python3 "$root/scripts/catalog.py" "$@"; }

# Reads the environment, asking the catalog only for what it leaves out: a Jenkins agent that sets
# everything needs neither a catalog.yaml nor PyYAML.
configure() {
  forge_type=${FORGE_TYPE:-}
  [[ -n "$forge_type" ]] || forge_type=$(catalog forge type) || die "cannot read the git server type from the catalog"
  case "$forge_type" in
    forgejo | github) ;;
    *) die "unknown git server type '$forge_type' (forgejo or github)" ;;
  esac

  token=${FORGE_TOKEN:-}
  [[ -n "$token" || "$forge_type" != github ]] || token=${GH_TOKEN:-}
  [[ -n "$token" ]] || die "FORGE_TOKEN is not set"
  # The token is written into a curl config below; anything that is not a token's alphabet could
  # end the quoted value and smuggle in another option.
  [[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "FORGE_TOKEN has characters a token does not have"

  api_url=${FORGE_API_URL:-}
  [[ -n "$api_url" || "$forge_type" != github ]] || api_url=${GITHUB_API_URL:-}
  [[ -n "$api_url" ]] || api_url=$(catalog forge api) || die "cannot read the git server's API from the catalog"
  api_url=${api_url%/}

  # GITHUB_OWNER is the pipeline's name for the GitHub owner: it says nothing about a Forgejo one.
  owner=${FORGE_OWNER:-}
  [[ -n "$owner" || "$forge_type" != github ]] || owner=${GITHUB_OWNER:-}
  [[ -n "$owner" ]] || owner=$(catalog owner) || die "cannot read the owner from the catalog"

  uploads_url=${GITHUB_UPLOADS_URL:-https://uploads.github.com}
  uploads_url=${uploads_url%/}
}

# encode <text>: a URL path segment.
encode() { jq -rn --arg text "$1" '$text | @uri'; }

# encode_path <text>: a path made of segments, such as a branch name: every segment encoded, the
# slashes between them kept, for the endpoints that take the rest of the URL.
encode_path() { jq -rn --arg text "$1" '$text | split("/") | map(@uri) | join("/")'; }

# The repository's URL under the API: <api>/repos/<owner>/<repo>
repo_url() { printf '%s/repos/%s/%s' "$api_url" "$(encode "$owner")" "$(encode "$1")"; }

# send <method> <url> [--json <body> | --file <path> | --form <field=@path>]
#
# Does the call and leaves what came back in three variables, for the callers that must tell failures
# apart (delete-branch, merge-pr): answer (the body), http_status (000 when there was no answer at all)
# and, when curl failed, failure: the server's message (the JSON .message, else the raw body, else just
# the status: Forgejo shows the message of a 500 to administrators only), or curl's own error when
# nothing answered. Returns curl's status.
#
# curl reads its headers, the token among them, from a config on stdin: arguments are readable by
# every user of the machine (ps), stdin is not. A JSON body (none holds a secret) goes through a
# temporary file, removed afterwards, not an argument: release notes can be longer than the most one
# argument may hold (128 KiB on Linux), and stdin is the config's.
# --write-out puts the HTTP status on a line of its own after the body, split off again below. curl's
# own error line is kept out of the log unless nothing answered: for an HTTP error, the server's
# message says more ("curl: (22) ... error: 500" would also stand in a log where the delete of a
# branch that is already gone succeeded).
send() {
  local method=$1 url=$2 out status=0 errors body=""
  shift 2
  local args=(--silent --show-error --fail-with-body --write-out '\n%{http_code}' --request "$method" --config -)
  case "${1:-}" in
    --json)
      body=$(mktemp)
      printf '%s' "$2" >"$body"
      args+=(--header "Content-Type: application/json" --data-binary "@$body")
      ;;
    --file) args+=(--header "Content-Type: application/octet-stream" --data-binary "@$2") ;;
    --form) args+=(--form "$2") ;; # curl makes the multipart Content-Type, boundary included
  esac
  # A file going up (--file, --form) is a transfer; everything else an API call.
  case "${1:-}" in
    --file | --form) args+=("${transfer_timeouts[@]}") ;;
    *) args+=("${api_timeouts[@]}") ;;
  esac
  errors=$(mktemp)
  out=$(curl "${args[@]}" "$url" <<<"$(headers)" 2>"$errors") || status=$?
  if [[ "$out" == *$'\n'* ]]; then
    answer=${out%$'\n'*}
    http_status=${out##*$'\n'}
  else
    answer=$out
    http_status=000
  fi
  failure=""
  if ((status)); then
    if [[ -n "$answer" ]]; then
      failure=$(jq -er '.message' <<<"$answer" 2>/dev/null) || failure=$answer
    fi
    if [[ -z "$failure" && "$http_status" != 000 ]]; then
      failure="HTTP $http_status, without a message"
    fi
    if [[ -z "$failure" ]]; then
      failure=$(<"$errors")
      [[ -n "$failure" ]] || failure="no answer (curl exit $status)"
    fi
  fi
  rm -f "$errors" ${body:+"$body"}
  return "$status"
}

# request <what> <method> <url> [--json <body> | --file <path> | --form <field=@path>]
#
# Prints the answer. On an error answer, --fail-with-body puts the server's explanation on stdout,
# where a caller's jq would swallow it ("merge-pr" would print null): it goes to stderr, in the
# error line "forge: <what> failed: <message>", into the build log, instead.
request() {
  local what=$1
  shift
  if ! send "$@"; then
    echo "forge: $what failed${failure:+: $failure}" >&2
    return 1
  fi
  [[ -z "$answer" ]] || printf '%s\n' "$answer"
}

# missing_or_failed <what>: after a send that failed, for the commands where "not found" is an answer:
# prints the exit status to return, 4 for a 404, else 1 (explained on stderr like request does).
missing_or_failed() {
  if [[ "$http_status" == 404 ]]; then
    echo "forge: $1: not found" >&2
    echo 4
  else
    echo "forge: $1 failed${failure:+: $failure}" >&2
    echo 1
  fi
}

# The curl config with the request's headers.
headers() {
  case "$forge_type" in
    github)
      printf 'header = "Authorization: Bearer %s"\n' "$token"
      printf 'header = "Accept: application/vnd.github+json"\n'
      printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
      ;;
    forgejo)
      # Forgejo's own scheme; "Bearer" is for OAuth2 tokens.
      printf 'header = "Authorization: token %s"\n' "$token"
      printf 'header = "Accept: application/json"\n'
      ;;
  esac
}

# set_status <repo> <sha> <state> <context> <description> [target-url]: GitHub and Forgejo take the
# same body on the same path.
set_status() {
  local repo=$1 sha=$2 state=$3 context=$4 description=$5 target=${6:-}
  case "$state" in
    pending | success | failure | error) ;;
    *) die "set-status: unknown state '$state' (pending, success, failure or error)" ;;
  esac
  request "set-status on $sha" POST "$(repo_url "$repo")/statuses/$(encode "$sha")" --json "$(jq -n \
    --arg state "$state" --arg context "$context" --arg description "$description" --arg target "$target" \
    '{state: $state, context: $context, description: $description} + (if $target == "" then {} else {target_url: $target} end)')" >/dev/null
}

# release_options <create-release's options>: sets draft (true or false) and notes_file ("" for none).
release_options() {
  draft=false notes_file=""
  while (($#)); do
    case "$1" in
      --draft) draft=true ;;
      --notes-file) notes_file=${2:?--notes-file needs a file}; shift ;;
      *) die "create-release: unknown option '$1'" ;;
    esac
    shift
  done
  [[ -z "$notes_file" || -f "$notes_file" ]] || die "create-release: no notes file $notes_file"
}

# ---- GitHub ----

github_set_status() { set_status "$@"; }

github_get_statuses() {
  local repo=$1 sha=$2 out page=1 count pages=()
  # The list of statuses, not the combined status: only the list's items carry the creator (the
  # combined status' items are "simple" statuses without one). The list is the commit's whole history,
  # the newest first, 100 to a page at most, so the first one seen of a context is its latest. 100
  # pages is far more than the 1000 statuses GitHub keeps per commit and context.
  while ((page <= 100)); do
    out=$(request "get-statuses of $sha" GET "$(repo_url "$repo")/commits/$(encode "$sha")/statuses?per_page=100&page=$page") || return 1
    pages+=("$out")
    count=$(jq 'length' <<<"$out") || return 1
    ((count == 100)) || break
    page=$((page + 1))
  done
  printf '%s\n' "${pages[@]}" | jq -rs 'add // [] | reduce .[] as $s ({seen: {}, lines: []};
      if .seen[$s.context] then . else .seen[$s.context] = true | .lines += [$s] end)
    | .lines[] | [.context, .state, (.creator.login // "")] | @tsv'
}

github_merge_pr() {
  local repo=$1 number=$2 sha=$3 title=$4 out
  # A merge commit, never squash or rebase: develop and main must keep sharing history, or the next
  # release branch cut from develop would conflict with main. sha makes the merge fail if the pull
  # request moved on since it was deployed.
  out=$(request "merge-pr #$number" PUT "$(repo_url "$repo")/pulls/$(encode "$number")/merge" --json "$(jq -n \
    --arg sha "$sha" --arg title "$title" '{merge_method: "merge", sha: $sha, commit_title: $title}')") || return 1
  jq -er '.sha' <<<"$out" || die "merge-pr failed: the answer has no merge commit"
}

github_create_tag() {
  local repo=$1 tag=$2 sha=$3
  request "create-tag $tag" POST "$(repo_url "$repo")/git/refs" --json "$(jq -n \
    --arg ref "refs/tags/$tag" --arg sha "$sha" '{ref: $ref, sha: $sha}')" >/dev/null
}

github_create_release() {
  local repo=$1 tag=$2 sha=$3 title=$4 out draft notes_file
  shift 4
  release_options "$@"
  # Without notes of our own GitHub writes them from the merged pull requests.
  local notes=(--argjson generate true --arg notes "")
  if [[ -n "$notes_file" ]]; then
    notes=(--argjson generate false --rawfile notes "$notes_file")
  fi
  out=$(request "create-release $tag" POST "$(repo_url "$repo")/releases" --json "$(jq -n \
    --arg tag "$tag" --arg sha "$sha" --arg title "$title" --argjson draft "$draft" "${notes[@]}" \
    '{tag_name: $tag, target_commitish: $sha, name: $title, draft: $draft, prerelease: false}
     + (if $generate then {generate_release_notes: true} else {body: $notes} end)')") || return 1
  if [[ "$draft" == true ]]; then
    jq -e '.draft == true' <<<"$out" >/dev/null || die "create-release failed: the server did not create an unpublished draft release for $tag"
  fi
  # Both, or the pipeline would go on without the release's page (a draft's above all: its address
  # is how the owner finds a release whose packaging failed).
  jq -er 'select(.id != null and (.html_url // "") != "") | [.id, .html_url] | @tsv' <<<"$out" \
    || die "create-release failed: the answer has no release id and URL"
}

github_upload_release_file() {
  local repo=$1 id=$2 file=$3 out
  [[ -f "$file" ]] || die "upload-release-file: no file $file"
  out=$(request "upload-release-file $(basename "$file")" POST \
    "$uploads_url/repos/$(encode "$owner")/$(encode "$repo")/releases/$(encode "$id")/assets?name=$(encode "$(basename "$file")")" \
    --file "$file") || return 1
  jq -er '.browser_download_url' <<<"$out" || die "upload-release-file failed: the answer has no download URL"
}

github_publish_release() {
  local repo=$1 id=$2 out
  out=$(request "publish-release $id" PATCH "$(repo_url "$repo")/releases/$(encode "$id")" --json '{"draft": false}') || return 1
  jq -er 'select(.draft == false) | .html_url' <<<"$out" || die "publish-release failed: the release is still a draft"
}

github_find_release() {
  local repo=$1 tag=$2 out page=1 count found
  # GET /releases/tags/<tag> leaves drafts out, and a draft is what an interrupted release leaves: the
  # list (the newest first, 100 to a page at most, drafts included for a token that can push) is searched.
  # 100 pages is far more releases than any repository has.
  while ((page <= 100)); do
    out=$(request "find-release $tag" GET "$(repo_url "$repo")/releases?per_page=100&page=$page") || return 1
    found=$(jq -r --arg tag "$tag" '[.[] | select(.tag_name == $tag)][0] // empty
      | [.id, .html_url, (.draft // false)] | @tsv' <<<"$out") || return 1
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
    count=$(jq 'length' <<<"$out") || return 1
    ((count == 100)) || break
    page=$((page + 1))
  done
  echo "forge: no release for $tag" >&2
  return 4
}

github_release_tags() {
  local repo=$1 out page=1 count
  # The newest first, 100 to a page at most; a token that can push sees the drafts too, left out here.
  # 100 pages is far more releases than any repository has.
  while ((page <= 100)); do
    out=$(request "release-tags of $repo" GET "$(repo_url "$repo")/releases?per_page=100&page=$page") || return 1
    jq -r '.[] | select((.draft // false) == false) | [.tag_name, (.target_commitish // "")] | @tsv' <<<"$out" || return 1
    count=$(jq 'length' <<<"$out") || return 1
    ((count == 100)) || return 0
    page=$((page + 1))
  done
}

github_release_files() {
  local repo=$1 id=$2 out page=1 count
  while ((page <= 100)); do
    out=$(request "release-files of $id" GET "$(repo_url "$repo")/releases/$(encode "$id")/assets?per_page=100&page=$page") || return 1
    jq -r '.[] | [.name, .size] | @tsv' <<<"$out" || return 1
    count=$(jq 'length' <<<"$out") || return 1
    ((count == 100)) || return 0
    page=$((page + 1))
  done
}

github_tag_sha() {
  local repo=$1 tag=$2 object type sha
  # git/ref (singular) is the exact tag, not every ref that starts with its name. A lightweight tag
  # points at the commit; an annotated one at a tag object, which points at the commit (or at another
  # tag object): followed until a commit.
  send GET "$(repo_url "$repo")/git/ref/tags/$(encode_path "$tag")" || return "$(missing_or_failed "tag-sha $tag")"
  object=$(jq -er '.object | select(.type != null and .sha != null) | .type + " " + .sha' <<<"$answer") || die "tag-sha failed: the answer has no object"
  read -r type sha <<<"$object"
  while [[ "$type" == tag ]]; do
    send GET "$(repo_url "$repo")/git/tags/$(encode "$sha")" || return "$(missing_or_failed "tag-sha $tag")"
    object=$(jq -er '.object | select(.type != null and .sha != null) | .type + " " + .sha' <<<"$answer") || die "tag-sha failed: the answer has no object"
    read -r type sha <<<"$object"
  done
  printf '%s\n' "$sha"
}

github_delete_branch() {
  local repo=$1 branch=$2 out
  # "delete head branches on merge" (github/rulesets.py turns it on) means GitHub has usually deleted
  # the branch already by the time this runs, and answers 422 "Reference does not exist": that is done.
  if ! out=$(request "delete-branch $branch" DELETE "$(repo_url "$repo")/git/refs/heads/$(encode_path "$branch")" 2>&1); then
    if grep -q 'Reference does not exist' <<<"$out"; then
      echo "forge: $branch was already deleted" >&2
      return 0
    fi
    echo "$out" >&2
    return 1
  fi
}

# ---- Forgejo ----

forgejo_set_status() { set_status "$@"; }

forgejo_get_statuses() {
  local repo=$1 sha=$2 out page=1 count
  # The combined status holds one state per context, the latest, in pages of at most 50 (Forgejo's
  # largest; its default is 30); a commit without any status has "statuses": null, i.e. no line.
  # The state is in the field "status" (GitHub's "state"). 100 pages is far more than any commit has.
  while ((page <= 100)); do
    out=$(request "get-statuses of $sha" GET "$(repo_url "$repo")/commits/$(encode "$sha")/status?limit=50&page=$page") || return 1
    jq -r '(.statuses // [])[] | [.context, .status, (.creator.login // "")] | @tsv' <<<"$out"
    count=$(jq '.statuses // [] | length' <<<"$out")
    ((count == 50)) || return 0
    page=$((page + 1))
  done
}

forgejo_merge_pr() {
  local repo=$1 number=$2 sha=$3 title=$4 out body attempt=1
  # A merge commit, never squash or rebase (see github_merge_pr). head_commit_id makes the merge fail
  # if the pull request moved on since it was deployed. Unlike GitHub's, the answer is empty: the merge
  # commit is read from the pull request afterwards (Forgejo fills it in before it answers).
  body=$(jq -n --arg sha "$sha" --arg title "$title" '{Do: "merge", MergeTitleField: $title, head_commit_id: $sha}')
  # Forgejo works out again in the background whether a pull request can be merged, after a new commit
  # status among other things, and meanwhile refuses with 405 "Please try again later". The pipeline
  # merges right after posting the last deploy status, so that refusal is tried again for a minute.
  until send POST "$(repo_url "$repo")/pulls/$(encode "$number")/merge" --json "$body"; do
    if [[ "$http_status" == 405 && "$failure" == *"try again later"* ]] && ((attempt < 20)); then
      attempt=$((attempt + 1))
      sleep 3
      continue
    fi
    echo "forge: merge-pr #$number failed${failure:+: $failure}" >&2
    return 1
  done
  out=$(request "merge-pr #$number" GET "$(repo_url "$repo")/pulls/$(encode "$number")") || return 1
  jq -er '.merge_commit_sha | select(. != null and . != "")' <<<"$out" || die "merge-pr failed: the pull request has no merge commit"
}

forgejo_create_tag() {
  local repo=$1 tag=$2 sha=$3
  request "create-tag $tag" POST "$(repo_url "$repo")/tags" --json "$(jq -n \
    --arg tag "$tag" --arg sha "$sha" '{tag_name: $tag, target: $sha}')" >/dev/null
}

forgejo_create_release() {
  local repo=$1 tag=$2 sha=$3 title=$4 out draft notes_file
  shift 4
  release_options "$@"
  # Without notes of our own the body stays empty: Forgejo does not write release notes.
  local notes=(--arg notes "")
  [[ -z "$notes_file" ]] || notes=(--rawfile notes "$notes_file")
  out=$(request "create-release $tag" POST "$(repo_url "$repo")/releases" --json "$(jq -n \
    --arg tag "$tag" --arg sha "$sha" --arg title "$title" --argjson draft "$draft" "${notes[@]}" \
    '{tag_name: $tag, target_commitish: $sha, name: $title, body: $notes, draft: $draft, prerelease: false}')") || return 1
  if [[ "$draft" == true ]]; then
    jq -e '.draft == true' <<<"$out" >/dev/null || die "create-release failed: the server did not create an unpublished draft release for $tag"
  fi
  # Both, or the pipeline would go on without the release's page (a draft's above all: its address
  # is how the owner finds a release whose packaging failed).
  jq -er 'select(.id != null and (.html_url // "") != "") | [.id, .html_url] | @tsv' <<<"$out" \
    || die "create-release failed: the answer has no release id and URL"
}

forgejo_upload_release_file() {
  local repo=$1 id=$2 file=$3 out
  [[ -f "$file" ]] || die "upload-release-file: no file $file"
  # A multipart form with the file in the field "attachment"; the file's own name is the asset's.
  out=$(request "upload-release-file $(basename "$file")" POST \
    "$(repo_url "$repo")/releases/$(encode "$id")/assets?name=$(encode "$(basename "$file")")" \
    --form "attachment=@$file") || return 1
  jq -er '.browser_download_url' <<<"$out" || die "upload-release-file failed: the answer has no download URL"
}

forgejo_publish_release() {
  local repo=$1 id=$2 out
  out=$(request "publish-release $id" PATCH "$(repo_url "$repo")/releases/$(encode "$id")" --json '{"draft": false}') || return 1
  jq -er 'select(.draft == false) | .html_url' <<<"$out" || die "publish-release failed: the release is still a draft"
}

forgejo_find_release() {
  local repo=$1 tag=$2 out page=1 count found
  # The list, not /releases/tags/<tag>: it answers 404 for a draft. Pages of at most 50 (Forgejo's
  # largest), drafts included for a token that can write.
  while ((page <= 100)); do
    out=$(request "find-release $tag" GET "$(repo_url "$repo")/releases?limit=50&page=$page") || return 1
    found=$(jq -r --arg tag "$tag" '[.[] | select(.tag_name == $tag)][0] // empty
      | [.id, .html_url, (.draft // false)] | @tsv' <<<"$out") || return 1
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
    count=$(jq 'length' <<<"$out") || return 1
    ((count == 50)) || break
    page=$((page + 1))
  done
  echo "forge: no release for $tag" >&2
  return 4
}

forgejo_release_tags() {
  local repo=$1 out page=1 count
  # Pages of at most 50 (Forgejo's largest); a token that can write sees the drafts too, left out here.
  while ((page <= 100)); do
    out=$(request "release-tags of $repo" GET "$(repo_url "$repo")/releases?limit=50&page=$page") || return 1
    jq -r '.[] | select((.draft // false) == false) | [.tag_name, (.target_commitish // "")] | @tsv' <<<"$out" || return 1
    count=$(jq 'length' <<<"$out") || return 1
    ((count == 50)) || return 0
    page=$((page + 1))
  done
}

forgejo_release_files() {
  local repo=$1 id=$2 out
  # Forgejo lists a release's files in one answer.
  out=$(request "release-files of $id" GET "$(repo_url "$repo")/releases/$(encode "$id")/assets") || return 1
  jq -r '.[] | [.name, .size] | @tsv' <<<"$out"
}

forgejo_tag_sha() {
  local repo=$1 tag=$2
  # .commit is the commit the tag points to, also for an annotated tag (.id is the tag object).
  send GET "$(repo_url "$repo")/tags/$(encode_path "$tag")" || return "$(missing_or_failed "tag-sha $tag")"
  jq -er '.commit.sha | select(. != null and . != "")' <<<"$answer" || die "tag-sha failed: the answer has no commit"
}

forgejo_delete_branch() {
  local repo=$1 branch=$2 url refused
  url="$(repo_url "$repo")/branches/$(encode_path "$branch")"
  send DELETE "$url" && return 0
  # A protected branch is a decision of the owner's, not a failure of the release: exit 3. Forgejo
  # answers 403 "branch protected".
  if [[ "$http_status" == 403 ]] && grep -qi 'protected' <<<"$failure"; then
    echo "forge: kept: $branch is protected" >&2
    return 3
  fi
  refused=$failure
  # Delete head branch after merge (rules.py turns it on) means Forgejo has usually deleted the branch
  # already by the time this runs: that is done. Forgejo 16 answers the delete of a missing branch with
  # a 500 whose message ("object does not exist") only an administrator sees, so the answer cannot
  # tell: the branch is looked up instead. Gone (404) from a repository that is there (a repository
  # that is not, or that the token cannot see, is a 404 too) means done.
  if ! send GET "$url" && [[ "$http_status" == 404 ]] && send GET "$(repo_url "$repo")"; then
    echo "forge: $branch was already deleted" >&2
    return 0
  fi
  echo "forge: delete-branch $branch failed${refused:+: $refused}" >&2
  return 1
}

# ---- Dispatch ----

# The commands and their arguments: what a call without any prints.
commands() {
  cat >&2 <<'EOF'
usage: forge.sh <command> <arguments>
  set-status          <repo> <sha> <state> <context> <description> [target-url]
  get-statuses        <repo> <sha>
  merge-pr            <repo> <number> <sha> <title>
  create-tag          <repo> <tag> <sha>
  create-release      <repo> <tag> <sha> <title> [--draft] [--notes-file <file>]
  upload-release-file <repo> <release-id> <file>
  publish-release     <repo> <release-id>
  find-release        <repo> <tag>
  release-tags        <repo>
  release-files       <repo> <release-id>
  tag-sha             <repo> <tag>
  delete-branch       <repo> <branch>
Reference: docs/setup.md, "The git server commands".
EOF
}

(($#)) || { commands; exit 2; }
command=$1
shift
case "$command" in
  set-status) usage="<repo> <sha> <state> <context> <description> [target-url]" min=5 max=6 ;;
  get-statuses) usage="<repo> <sha>" min=2 max=2 ;;
  merge-pr) usage="<repo> <number> <sha> <title>" min=4 max=4 ;;
  create-tag) usage="<repo> <tag> <sha>" min=3 max=3 ;;
  create-release) usage="<repo> <tag> <sha> <title> [--draft] [--notes-file <file>]" min=4 max=7 ;;
  upload-release-file) usage="<repo> <release-id> <file>" min=3 max=3 ;;
  publish-release) usage="<repo> <release-id>" min=2 max=2 ;;
  find-release) usage="<repo> <tag>" min=2 max=2 ;;
  release-tags) usage="<repo>" min=1 max=1 ;;
  release-files) usage="<repo> <release-id>" min=2 max=2 ;;
  tag-sha) usage="<repo> <tag>" min=2 max=2 ;;
  delete-branch) usage="<repo> <branch>" min=2 max=2 ;;
  *) die "unknown command '$command'" ;;
esac
(($# >= min && $# <= max)) || die "usage: forge.sh $command $usage"

configure
function_name="${forge_type}_${command//-/_}"
declare -F "$function_name" >/dev/null || die "$forge_type: $command is not implemented yet"
"$function_name" "$@"
