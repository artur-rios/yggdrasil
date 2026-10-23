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

# request <what> <method> <url> [--json <body> | --file <path>]
#
# Prints the answer. On an error answer, --fail-with-body puts the server's explanation on stdout,
# where a caller's jq would swallow it ("merge-pr" would print null): it goes to stderr, in the
# error line "forge: <what> failed: <message>", into the build log, instead.
#
# curl reads its headers, the token among them, from a config on stdin: arguments are readable by
# every user of the machine (ps), stdin is not. A body is an argument, which is fine: none holds a secret.
request() {
  local what=$1 method=$2 url=$3 out message
  shift 3
  local args=(--silent --show-error --fail-with-body --request "$method" --config -)
  case "${1:-}" in
    --json) args+=(--header "Content-Type: application/json" --data "$2") ;;
    --file) args+=(--header "Content-Type: application/octet-stream" --data-binary "@$2") ;;
  esac
  if ! out=$(curl "${args[@]}" "$url" <<<"$(headers)"); then
    message=$(jq -r '.message // empty' <<<"$out" 2>/dev/null) || message=""
    [[ -n "$message" ]] || message=$out
    echo "forge: $what failed${message:+: $message}" >&2
    return 1
  fi
  [[ -z "$out" ]] || printf '%s\n' "$out"
}

# The curl config with the request's headers.
headers() {
  case "$forge_type" in
    github)
      printf 'header = "Authorization: Bearer %s"\n' "$token"
      printf 'header = "Accept: application/vnd.github+json"\n'
      printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
      ;;
  esac
}

# ---- GitHub ----

github_set_status() {
  local repo=$1 sha=$2 state=$3 context=$4 description=$5 target=${6:-}
  case "$state" in
    pending | success | failure | error) ;;
    *) die "set-status: unknown state '$state' (pending, success, failure or error)" ;;
  esac
  request "set-status on $sha" POST "$(repo_url "$repo")/statuses/$(encode "$sha")" --json "$(jq -n \
    --arg state "$state" --arg context "$context" --arg description "$description" --arg target "$target" \
    '{state: $state, context: $context, description: $description} + (if $target == "" then {} else {target_url: $target} end)')" >/dev/null
}

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
  local repo=$1 tag=$2 sha=$3 title=$4 out draft=false notes_file=""
  shift 4
  while (($#)); do
    case "$1" in
      --draft) draft=true ;;
      --notes-file) notes_file=${2:?--notes-file needs a file}; shift ;;
      *) die "create-release: unknown option '$1'" ;;
    esac
    shift
  done
  # Without notes of our own GitHub writes them from the merged pull requests.
  local notes=(--argjson generate true --arg notes "")
  if [[ -n "$notes_file" ]]; then
    [[ -f "$notes_file" ]] || die "create-release: no notes file $notes_file"
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
