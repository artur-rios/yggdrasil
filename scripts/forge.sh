#!/usr/bin/env bash
# The git server's side of a release, the same small commands for every supported server: the pipeline
# and the other scripts call these and never learn whether the repositories live on Forgejo or GitHub.
# Needs curl, jq and (only when the environment below does not say) python3 with PyYAML.
#
#     scripts/forge.sh set-status     <repo> <sha> <state> <context> <description> [target-url]
#     scripts/forge.sh get-statuses   <repo> <sha>                  -> "<context>\t<state>" per line
#     scripts/forge.sh merge-pr       <repo> <number> <sha> <title> -> the merge commit
#     scripts/forge.sh create-tag     <repo> <tag> <sha>
#     scripts/forge.sh create-release <repo> <tag> <sha> <title> [--draft] [--notes-file <file>]
#                                                                   -> "<id>\t<html_url>"
#     scripts/forge.sh upload-release-file <repo> <release-id> <file> -> the file's download URL
#     scripts/forge.sh publish-release <repo> <release-id>          -> the release's html_url
#     scripts/forge.sh delete-branch  <repo> <branch>
#
# state is pending, success, failure or error. get-statuses prints the latest state of each context
# (the server's combined status). merge-pr makes a merge commit and fails if the pull request's head is
# no longer <sha>. delete-branch succeeds, with a note on stderr, when the branch is already gone.
#
# Exit 0 is success; 1 an error, explained on stderr as "forge: <what> failed: <the server's message>";
# 3 (delete-branch only) that the server keeps the branch, which is not an error to stop a release for.
#
# <repo> is the repository name; the owner comes from the environment, else from the catalog:
#   FORGE_TYPE         forgejo or github                  (else catalog.py forge type)
#   FORGE_API_URL      the REST API's base URL            (else, for GitHub, GITHUB_API_URL, else catalog.py forge api)
#   FORGE_OWNER        the owner of the repositories      (else GITHUB_OWNER, else catalog.py owner)
#   FORGE_TOKEN        the access token, required         (else, for GitHub, GH_TOKEN)
#   GITHUB_UPLOADS_URL where GitHub takes release files   (default https://uploads.github.com)
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

die() { echo "forge: $*" >&2; exit 1; }

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

  owner=${FORGE_OWNER:-${GITHUB_OWNER:-}}
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
# every user of the machine (ps), stdin is not. A body is an argument, which is fine: none holds a secret.
# --write-out puts the HTTP status on a line of its own after the body, split off again below. curl's
# own error line is kept out of the log unless nothing answered: for an HTTP error, the server's
# message says more ("curl: (22) ... error: 500" would also stand in a log where the delete of a
# branch that is already gone succeeded).
send() {
  local method=$1 url=$2 out status=0 errors
  shift 2
  local args=(--silent --show-error --fail-with-body --write-out '\n%{http_code}' --request "$method" --config -)
  case "${1:-}" in
    --json) args+=(--header "Content-Type: application/json" --data "$2") ;;
    --file) args+=(--header "Content-Type: application/octet-stream" --data-binary "@$2") ;;
    --form) args+=(--form "$2") ;; # curl makes the multipart Content-Type, boundary included
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
  rm -f "$errors"
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
  local repo=$1 sha=$2 out
  # The combined status already holds one state per context, the latest. 100 is the most a page takes.
  out=$(request "get-statuses of $sha" GET "$(repo_url "$repo")/commits/$(encode "$sha")/status?per_page=100") || return 1
  jq -r '.statuses[] | [.context, .state] | @tsv' <<<"$out"
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
  jq -er '[.id, .html_url] | @tsv' <<<"$out" || die "create-release failed: the answer has no release id and URL"
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
    jq -r '(.statuses // [])[] | [.context, .status] | @tsv' <<<"$out"
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
  jq -er '[.id, .html_url] | @tsv' <<<"$out" || die "create-release failed: the answer has no release id and URL"
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

command=${1:-}
shift || true
case "$command" in
  set-status) usage="<repo> <sha> <state> <context> <description> [target-url]" min=5 max=6 ;;
  get-statuses) usage="<repo> <sha>" min=2 max=2 ;;
  merge-pr) usage="<repo> <number> <sha> <title>" min=4 max=4 ;;
  create-tag) usage="<repo> <tag> <sha>" min=3 max=3 ;;
  create-release) usage="<repo> <tag> <sha> <title> [--draft] [--notes-file <file>]" min=4 max=7 ;;
  upload-release-file) usage="<repo> <release-id> <file>" min=3 max=3 ;;
  publish-release) usage="<repo> <release-id>" min=2 max=2 ;;
  delete-branch) usage="<repo> <branch>" min=2 max=2 ;;
  *) die "unknown command '$command'" ;;
esac
(($# >= min && $# <= max)) || die "usage: forge.sh $command $usage"

configure
function_name="${forge_type}_${command//-/_}"
declare -F "$function_name" >/dev/null || die "$forge_type: $command is not implemented yet"
"$function_name" "$@"
