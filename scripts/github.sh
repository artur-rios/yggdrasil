#!/usr/bin/env bash
# The GitHub Actions side of a release, as small commands the pipeline calls one at a time.
# Needs GH_TOKEN (Jenkins passes the GitHub App's installation token), curl and jq.
#
#     scripts/github.sh wait-checks   <repo> <sha> [timeout-seconds]
#     scripts/github.sh set-status    <repo> <sha> <state> <context> <description> [target-url]
#     scripts/github.sh merge-pr      <repo> <number> <sha> <title>      -> prints the merge commit
#     scripts/github.sh release       <repo> <version> <sha>             -> direct GitHub release
#     scripts/github.sh publish-release <repo> <version> <sha> [timeout] -> verified public release
#     scripts/github.sh delete-branch <repo> <branch>
#
# Only wait-checks and the release-artifacts workflow flow of publish-release are GitHub Actions; the
# rest (statuses, merge, tags, releases, branches) is the git server's interface, which scripts/forge.sh
# implements for GitHub and Forgejo alike, and this script hands those steps to it with the type set to
# github. The commands, their arguments and their output are what the Jenkins pipeline has always called.
#
# <repo> is the repository name; the owner is $GITHUB_OWNER (the catalog's owner; the pipeline sets it).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
owner=${GITHUB_OWNER:?set GITHUB_OWNER to the GitHub owner of the repositories}
api_url=${GITHUB_API_URL:-https://api.github.com}
# Seconds between two looks at the checks (wait-checks).
poll_seconds=${GITHUB_POLL_SECONDS:-20}

die() { echo "github: $*" >&2; exit 1; }
[[ -n "${GH_TOKEN:-}" ]] || die "GH_TOKEN is not set"

# forge.sh learns everything from here, not from the catalog: this runs on agents that only know
# the environment the pipeline gives them.
export FORGE_TYPE=github FORGE_API_URL=$api_url FORGE_OWNER=$owner FORGE_TOKEN=$GH_TOKEN
forge() { bash "$here/forge.sh" "$@"; }

# api <method> <path> [json-body]
api() {
  local method=$1 path=$2 body=${3:-}
  # The token reaches curl on stdin, in a config, never as an argument that ps would show.
  local config
  config=$(printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github+json"\nheader = "X-GitHub-Api-Version: 2022-11-28"' "$GH_TOKEN")
  local args=(--silent --show-error --fail-with-body --request "$method" --config -)
  [[ -n "$body" ]] && args+=(--header "Content-Type: application/json" --data "$body")
  local out
  # On an error answer, --fail-with-body puts GitHub's explanation on stdout, where a caller's jq
  # would swallow it: it goes to stderr, into the build log, instead.
  if ! out=$(curl "${args[@]}" "$api_url/$path" <<<"$config"); then
    echo "github: $method $path failed${out:+: $out}" >&2
    return 1
  fi
  [[ -z "$out" ]] || printf '%s\n' "$out"
}

# Waits until every GitHub Actions check on <sha> has completed, then succeeds only if none failed.
#
# Only GitHub Actions is considered: Jenkins' own check or status on the same commit is still in
# progress while this runs, and waiting for it would wait forever. Check suites are read as well as
# check runs because a workflow that has been triggered but not yet scheduled has a queued suite and
# no runs -- looking at runs alone could declare victory before a whole workflow started.
wait_checks() {
  local repo=$1 sha=$2 timeout=${3:-3600}
  local deadline=$((SECONDS + timeout)) unanswered=0

  while :; do
    local suites runs
    if ! suites=$(api GET "repos/$owner/$repo/commits/$sha/check-suites?per_page=100") \
      || ! runs=$(api GET "repos/$owner/$repo/commits/$sha/check-runs?per_page=100&filter=latest"); then
      # One unanswered look (a GitHub 5xx, a network blip) must not fail a release that may have
      # been waiting for most of an hour; several in a row are a real problem.
      unanswered=$((unanswered + 1))
      ((unanswered < 5)) || die "GitHub did not answer $unanswered times in a row about the checks on $sha"
      ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for checks on $sha"
      sleep "$poll_seconds"
      continue
    fi
    unanswered=0

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
    sleep "$poll_seconds"
  done
}

release() {
  local repo=$1 version=$2 sha=$3
  # Applications without a release-artifact workflow retain the original GitHub release path.
  forge create-release "$repo" "v$version" "$sha" "v$version" | cut -f2
}

publish_release() {
  local repo=$1 version=$2 sha=$3 timeout=${4:-7200}
  local tag="v$version" deadline=$((SECONDS + timeout)) release_json release_id release_url created

  # The tag is made explicit before packaging, so the application workflow checks out exactly the
  # commit Jenkins deployed and merged. Its release-tags ruleset makes that ref immutable.
  forge create-tag "$repo" "$tag" "$sha"

  # Artifacts are assembled behind a draft. The application workflow alone publishes it, after it
  # has compared every uploaded byte and digest with the locally validated bundle.
  created=$(forge create-release "$repo" "$tag" "$sha" "$tag" --draft) \
    || die "GitHub did not create an unpublished draft release for $tag"
  IFS=$'\t' read -r release_id release_url <<<"$created"

  api POST "repos/$owner/$repo/actions/workflows/release-artifacts.yml/dispatches" "$(jq -n \
    --arg ref "$tag" --arg tag "$tag" --arg release_id "$release_id" \
    '{ref: $ref, inputs: {tag: $tag, publish: "true", release_id: $release_id}}')" >/dev/null

  while :; do
    if release_json=$(api GET "repos/$owner/$repo/releases/$release_id"); then
      [[ "$(jq -r '.tag_name' <<<"$release_json")" == "$tag" ]] \
        || die "draft release $release_id changed from tag $tag"
      if [[ "$(jq -r '.draft' <<<"$release_json")" == "false" ]]; then
        jq -er '.html_url' <<<"$release_json"
        return 0
      fi
    fi
    ((SECONDS < deadline)) \
      || die "timed out after ${timeout}s waiting for the packaging workflow to publish $tag; draft: $release_url"
    echo "github: waiting for the packaging workflow to publish $tag"
    sleep "$poll_seconds"
  done
}

command=${1:-}
shift || true
case "$command" in
  wait-checks) wait_checks "$@" ;;
  set-status) forge set-status "$@" ;;
  merge-pr) forge merge-pr "$@" ;;
  release) release "$@" ;;
  publish-release) publish_release "$@" ;;
  delete-branch) forge delete-branch "$@" ;;
  *) die "unknown command '$command'" ;;
esac
