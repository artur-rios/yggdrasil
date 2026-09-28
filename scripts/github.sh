#!/usr/bin/env bash
# The GitHub side of a release, as small commands the pipeline calls one at a time.
# Needs GH_TOKEN (Jenkins passes the GitHub App's installation token), curl and jq.
#
#     scripts/github.sh wait-checks   <repo> <sha> [timeout-seconds]
#     scripts/github.sh set-status    <repo> <sha> <state> <context> <description> [target-url]
#     scripts/github.sh merge-pr      <repo> <number> <sha> <title>      -> prints the merge commit
#     scripts/github.sh release       <repo> <version> <sha>             -> tag + GitHub release v<version>
#     scripts/github.sh delete-branch <repo> <branch>
#
# <repo> is the repository name; the owner is $GITHUB_OWNER (the catalog's owner; the pipeline sets it).
set -euo pipefail

owner=${GITHUB_OWNER:?set GITHUB_OWNER to the GitHub owner of the repositories}
api_url=${GITHUB_API_URL:-https://api.github.com}

die() { echo "github: $*" >&2; exit 1; }
[[ -n "${GH_TOKEN:-}" ]] || die "GH_TOKEN is not set"

# api <method> <path> [json-body]
api() {
  local method=$1 path=$2 body=${3:-}
  local args=(--silent --show-error --fail-with-body --request "$method"
    --header "Authorization: Bearer $GH_TOKEN"
    --header "Accept: application/vnd.github+json"
    --header "X-GitHub-Api-Version: 2022-11-28")
  [[ -n "$body" ]] && args+=(--header "Content-Type: application/json" --data "$body")
  curl "${args[@]}" "$api_url/$path"
}

# Waits until every GitHub Actions check on <sha> has completed, then succeeds only if none failed.
#
# Only GitHub Actions is considered: Jenkins' own check or status on the same commit is still in
# progress while this runs, and waiting for it would wait forever. Check suites are read as well as
# check runs because a workflow that has been triggered but not yet scheduled has a queued suite and
# no runs -- looking at runs alone could declare victory before a whole workflow started.
wait_checks() {
  local repo=$1 sha=$2 timeout=${3:-3600}
  local deadline=$((SECONDS + timeout))

  while :; do
    local suites runs
    suites=$(api GET "repos/$owner/$repo/commits/$sha/check-suites?per_page=100")
    runs=$(api GET "repos/$owner/$repo/commits/$sha/check-runs?per_page=100&filter=latest")

    local pending_suites pending_runs failed policy
    pending_suites=$(jq '[.check_suites[] | select(.app.slug == "github-actions" and .status != "completed")] | length' <<<"$suites")
    pending_runs=$(jq '[.check_runs[] | select(.app.slug == "github-actions" and .status != "completed")] | length' <<<"$runs")
    failed=$(jq -r '[.check_runs[] | select(.app.slug == "github-actions" and .status == "completed"
                      and (.conclusion | IN("success", "skipped", "neutral") | not))
                    | "\(.name): \(.conclusion)"] | join("\n")' <<<"$runs")
    # Present on every pull request into main, so it doubles as proof the workflows have started.
    policy=$(jq '[.check_runs[] | select(.app.slug == "github-actions" and .name == "branch-policy")] | length' <<<"$runs")

    if [[ -n "$failed" ]]; then
      echo "github: checks failed on $sha:" >&2
      echo "$failed" >&2
      return 1
    fi

    if [[ "$policy" -gt 0 && "$pending_suites" -eq 0 && "$pending_runs" -eq 0 ]]; then
      echo "github: every GitHub Actions check on $sha passed:"
      jq -r '.check_runs[] | select(.app.slug == "github-actions") | "  \(.name): \(.conclusion)"' <<<"$runs"
      return 0
    fi

    ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for checks on $sha"
    echo "github: waiting for checks on $sha ($pending_suites suite(s), $pending_runs run(s) pending)"
    sleep 20
  done
}

set_status() {
  local repo=$1 sha=$2 state=$3 context=$4 description=$5 target=${6:-}
  api POST "repos/$owner/$repo/statuses/$sha" "$(jq -n \
    --arg state "$state" --arg context "$context" --arg description "$description" --arg target "$target" \
    '{state: $state, context: $context, description: $description} + (if $target == "" then {} else {target_url: $target} end)')" >/dev/null
}

merge_pr() {
  local repo=$1 number=$2 sha=$3 title=$4
  # A merge commit, never squash or rebase: develop and main must keep sharing history, or the next
  # release branch cut from develop would conflict with main. sha makes the merge fail if the pull
  # request moved on since it was deployed.
  api PUT "repos/$owner/$repo/pulls/$number/merge" "$(jq -n --arg sha "$sha" --arg title "$title" \
    '{merge_method: "merge", sha: $sha, commit_title: $title}')" | jq -r '.sha'
}

release() {
  local repo=$1 version=$2 sha=$3
  # Creating the release creates the lightweight tag v<version> on <sha> with it, and the release
  # notes are generated from the pull requests merged since the previous tag.
  api POST "repos/$owner/$repo/releases" "$(jq -n --arg tag "v$version" --arg sha "$sha" \
    '{tag_name: $tag, target_commitish: $sha, name: $tag, generate_release_notes: true}')" | jq -r '.html_url'
}

delete_branch() {
  local repo=$1 branch=$2 out
  # github/rulesets.py turns on "delete head branches on merge", so GitHub has usually deleted the
  # branch already by the time this runs, and answers 422 "Reference does not exist": that is done.
  if ! out=$(api DELETE "repos/$owner/$repo/git/refs/heads/$branch" 2>&1); then
    if grep -q 'Reference does not exist' <<<"$out"; then
      echo "github: $branch was already deleted"
      return 0
    fi
    echo "$out" >&2
    return 1
  fi
}

command=${1:-}
shift || true
case "$command" in
  wait-checks) wait_checks "$@" ;;
  set-status) set_status "$@" ;;
  merge-pr) merge_pr "$@" ;;
  release) release "$@" ;;
  delete-branch) delete_branch "$@" ;;
  *) die "unknown command '$command'" ;;
esac
