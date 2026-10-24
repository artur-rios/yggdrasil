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
# GitHub Actions only runs the Windows/macOS jobs of a repository, on its push-mirror; the pipeline reads
# and starts those runs with these, one look at a time (it never holds an executor waiting):
#     scripts/github.sh actions-status <repo> <sha> [<name>...]          -> <name> TAB <state> TAB <url> per check
#     scripts/github.sh wait-ref       <repo> <ref> <timeout-seconds>    -> until the mirror has the tag or branch
#     scripts/github.sh dispatch       <repo> <workflow-file> <ref> [key=value...]   -> the new run's id
#     scripts/github.sh run-status     <repo> <run-id>                   -> pending | success | failure
#     scripts/github.sh download-artifacts <repo> <run-id> <dir>         -> <dir>/<artifact name>/...
# actions-status and run-status exit 0 when everything finished successfully (skipped and neutral count),
# 1 when something failed (failure, cancelled, timed_out, action_required, startup_failure; the check is
# named on stderr), 3 while anything is pending (including a check not started yet, and a commit the mirror
# has not pushed), 2 when GitHub did not answer. wait-ref exits 2 on timeout; dispatch 2 when no run appears.
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
# Seconds between two looks for a ref or a dispatched run (wait-ref, dispatch), and how long dispatch
# looks for its run before giving up.
fast_poll_seconds=${GITHUB_DISPATCH_POLL_SECONDS:-5}
dispatch_timeout=${GITHUB_DISPATCH_TIMEOUT:-120}

die() { echo "github: $*" >&2; exit 1; }
[[ -n "${GH_TOKEN:-}" ]] || die "GH_TOKEN is not set"

# How long curl waits, so a GitHub that hangs cannot hold an agent forever (forge.sh does the same):
# an API call 10 s to connect and 60 s in all; an artifact download, which may be large, only when it
# stalls under 1 KiB/s for a minute, or after an hour. YGG_CURL_CONNECT_TIMEOUT, YGG_CURL_MAX_TIME and
# YGG_CURL_TRANSFER_MAX_TIME (seconds) change them.
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

# forge.sh learns everything from here, not from the catalog: this runs on agents that only know
# the environment the pipeline gives them.
export FORGE_TYPE=github FORGE_API_URL=$api_url FORGE_OWNER=$owner FORGE_TOKEN=$GH_TOKEN
forge() { bash "$here/forge.sh" "$@"; }

# The token reaches curl on stdin, in a config, never as an argument that ps would show.
auth_config() {
  printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github+json"\nheader = "X-GitHub-Api-Version: 2022-11-28"' "$GH_TOKEN"
}

# api <method> <path> [json-body]
api() {
  local method=$1 path=$2 body=${3:-}
  local config
  config=$(auth_config)
  local args=(--silent --show-error --fail-with-body --request "$method" --config - "${api_timeouts[@]}")
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

# raw_api <method> <path> [json-body]: like api, but for the commands that must tell GitHub's answers apart
# (a commit the mirror has not pushed yet is not an outage). Sets http_code and http_body and returns 0
# whenever GitHub answered, with whatever status; returns 1, with http_code 000, when it did not (a network
# error, a timeout).
http_code=000 http_body=""
raw_api() {
  local method=$1 path=$2 body=${3:-}
  local args=(--silent --show-error --request "$method" --config - --write-out $'\n%{http_code}' "${api_timeouts[@]}")
  [[ -n "$body" ]] && args+=(--header "Content-Type: application/json" --data "$body")
  local config out
  config=$(auth_config)
  http_code=000 http_body=""
  out=$(curl "${args[@]}" "$api_url/$path" <<<"$config") || return 1
  # The last line is the status curl was asked to append; everything before it is the body.
  http_code=${out##*$'\n'}
  http_body=${out%$'\n'*}
  [[ "$http_code" =~ ^[0-9]{3}$ ]] || { http_code=000 http_body=""; return 1; }
}

# The jq definition that turns a check run (or a workflow run) into success, failure or pending: not completed yet is
# pending, and skipped and neutral are as good as success. Anything else -- failure, cancelled, timed_out,
# action_required, startup_failure -- is a failure.
state_def='def state: if .status != "completed" then "pending" elif (.conclusion | IN("success", "skipped", "neutral")) then "success" else "failure" end;'

# Takes one look at the GitHub Actions check runs on <sha> and says where each stands; see the header for
# the exit codes. A failure wins over a pending check: it is final, so the caller need not wait for the rest.
actions_status() {
  (($# >= 2)) || die "usage: actions-status <repo> <sha> [<name>...]"
  local repo=$1 sha=$2 page=1 runs='[]' names='[]' page_runs
  shift 2
  if (($#)); then names=$(printf '%s\n' "$@" | jq -R . | jq -sc .); fi

  while :; do
    raw_api GET "repos/$owner/$repo/commits/$sha/check-runs?per_page=100&filter=latest&page=$page" \
      || { echo "github: GitHub did not answer about the checks on $sha" >&2; return 2; }
    case "$http_code" in
      200) ;;
      # The mirror has not pushed the commit yet: GitHub says 422 "No commit found for SHA" (a 404 on
      # some paths). That is pending, not an error -- the next look will find it.
      404 | 422)
        echo "github: GitHub does not know $sha yet (HTTP $http_code)" >&2
        runs='[]'
        break
        ;;
      *)
        echo "github: asking about the checks on $sha failed with HTTP $http_code: $http_body" >&2
        return 2
        ;;
    esac
    page_runs=$(jq -c '.check_runs // []' <<<"$http_body") \
      || { echo "github: unreadable answer about the checks on $sha" >&2; return 2; }
    runs=$(jq -c --argjson more "$page_runs" '. + $more' <<<"$runs")
    [[ "$(jq 'length' <<<"$page_runs")" -ge 100 ]] || break
    page=$((page + 1))
  done

  # Only GitHub Actions counts (Jenkins' own check on the same commit never finishes while this looks). A
  # check that was run again has several runs under one name: the latest, by id, is the one that counts.
  local lines
  lines=$(jq -r --argjson names "$names" "$state_def"'
    [.[] | select(.app.slug == "github-actions")]
    | group_by(.name) | map(max_by([(.id // 0), (.started_at // "")])) as $latest
    | (if ($names | length) == 0 then $latest
       else $names | map(. as $n | ($latest | map(select(.name == $n)) | first) // {name: $n, missing: true}) end)
    | .[] | [.name, (if .missing then "pending" else state end), (.conclusion // "-"), (.html_url // "")] | @tsv' <<<"$runs")

  local name state conclusion url failed=0 pending=0 count=0
  while IFS=$'\t' read -r name state conclusion url; do
    [[ -n "$name" ]] || continue
    count=$((count + 1))
    printf '%s\t%s\t%s\n' "$name" "$state" "$url"
    case "$state" in
      failure)
        failed=1
        echo "github: check '$name' failed on $sha: $conclusion" >&2
        ;;
      pending) pending=1 ;;
    esac
  done <<<"$lines"

  ((failed == 0)) || return 1
  # Without names, nothing running yet is not "everything passed": the mirror may have just pushed.
  ((pending == 0 && count > 0)) || return 3
}

# Waits until the mirror has pushed <ref> -- refs/tags/x, refs/heads/x, or a bare name, which is a tag.
wait_ref() {
  (($# == 3)) || die "usage: wait-ref <repo> <ref> <timeout-seconds>"
  local repo=$1 ref=$2 timeout=$3 path
  [[ "$timeout" =~ ^[0-9]+$ ]] || die "the timeout must be a number of seconds, not '$timeout'"
  case "$ref" in
    refs/*) path=${ref#refs/} ;;
    *) path=tags/$ref ;;
  esac
  local deadline=$((SECONDS + timeout))
  while :; do
    # Any answer but "it is there" -- not found, an outage -- is a reason to look again, until the time is up.
    if raw_api GET "repos/$owner/$repo/git/ref/$path" && [[ "$http_code" == 200 ]]; then
      return 0
    fi
    ((SECONDS < deadline)) || { echo "github: $ref is not on GitHub after ${timeout}s (last answer: HTTP $http_code)" >&2; return 2; }
    sleep "$fast_poll_seconds"
  done
}

# Starts <workflow-file> on <ref> with the inputs given as key=value and prints the new run's id. GitHub
# answers a dispatch with 204 and no run id, so the run is found afterwards: the workflow's dispatched runs
# on that ref, created at or after the moment before the dispatch.
dispatch() {
  (($# >= 3)) || die "usage: dispatch <repo> <workflow-file> <ref> [key=value...]"
  local repo=$1 workflow=$2 ref=$3 inputs='{}' pair
  shift 3
  for pair in "$@"; do
    [[ "$pair" == *=* ]] || die "an input is key=value, not '$pair'"
    inputs=$(jq -c --arg key "${pair%%=*}" --arg value "${pair#*=}" '. + {($key): $value}' <<<"$inputs")
  done
  # A run of a tag or branch carries the short name as its head_branch.
  local branch=${ref#refs/tags/}
  branch=${branch#refs/heads/}
  # A few seconds before, for the clocks of GitHub and this host differing a little.
  local since=$(($(date -u +%s) - 5))

  raw_api POST "repos/$owner/$repo/actions/workflows/$workflow/dispatches" \
    "$(jq -nc --arg ref "$ref" --argjson inputs "$inputs" '{ref: $ref, inputs: $inputs}')" \
    || { echo "github: GitHub did not answer the dispatch of $workflow on $ref" >&2; return 2; }
  case "$http_code" in
    200 | 204) ;;
    5??) echo "github: dispatching $workflow on $ref failed with HTTP $http_code: $http_body" >&2; return 2 ;;
    *) echo "github: dispatching $workflow on $ref was refused (HTTP $http_code): $http_body" >&2; return 1 ;;
  esac

  local deadline=$((SECONDS + dispatch_timeout)) query id
  query=$(jq -rn --arg branch "$branch" '$branch | @uri')
  while :; do
    if raw_api GET "repos/$owner/$repo/actions/workflows/$workflow/runs?event=workflow_dispatch&branch=$query&per_page=20" \
      && [[ "$http_code" == 200 ]]; then
      id=$(jq -r --arg branch "$branch" --argjson since "$since" \
        '[.workflow_runs[]? | select(.event == "workflow_dispatch" and .head_branch == $branch
                                     and (.created_at | fromdateiso8601) >= $since)]
         | max_by(.id) | .id // empty' <<<"$http_body")
      if [[ -n "$id" ]]; then
        echo "$id"
        return 0
      fi
    fi
    ((SECONDS < deadline)) || { echo "github: no run of $workflow on $ref appeared within ${dispatch_timeout}s" >&2; return 2; }
    sleep "$fast_poll_seconds"
  done
}

# Prints pending, success or failure for a workflow run, with the exit codes of actions-status.
run_status() {
  (($# == 2)) || die "usage: run-status <repo> <run-id>"
  local repo=$1 run=$2 state conclusion
  raw_api GET "repos/$owner/$repo/actions/runs/$run" \
    || { echo "github: GitHub did not answer about run $run" >&2; return 2; }
  [[ "$http_code" == 200 ]] \
    || { echo "github: asking about run $run failed with HTTP $http_code: $http_body" >&2; return 2; }
  state=$(jq -r "$state_def"' state' <<<"$http_body") \
    || { echo "github: unreadable answer about run $run" >&2; return 2; }
  echo "$state"
  case "$state" in
    success) return 0 ;;
    failure)
      conclusion=$(jq -r '.conclusion' <<<"$http_body")
      echo "github: run $run concluded $conclusion" >&2
      return 1
      ;;
    *) return 3 ;;
  esac
}

# Downloads every artifact of a run, unzipped into <dir>/<artifact name>/. GitHub answers the download of
# an artifact with a redirect to signed storage: that second request is made with a curl that was never
# given the token, rather than trusting -L to take the header off on the way, so the token cannot reach
# the storage host.
work=""
download_artifacts() {
  (($# == 3)) || die "usage: download-artifacts <repo> <run-id> <dir>"
  local repo=$1 run=$2 dir=$3 page=1 count id expired url name zip_file out code redirect
  command -v python3 >/dev/null || die "python3 is needed to unzip the artifacts"
  # Every way out of here -- an error, a die -- ends the script, so the scratch directory goes with it.
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  mkdir -p "$dir"

  while :; do
    raw_api GET "repos/$owner/$repo/actions/runs/$run/artifacts?per_page=100&page=$page" \
      || { echo "github: GitHub did not answer about the artifacts of run $run" >&2; return 2; }
    [[ "$http_code" == 200 ]] \
      || { echo "github: listing the artifacts of run $run failed with HTTP $http_code: $http_body" >&2; return 2; }
    count=$(jq '.artifacts | length' <<<"$http_body")

    while IFS=$'\t' read -r id expired url name; do
      [[ -n "$id" ]] || continue
      # The name becomes a directory: nothing that could leave <dir>.
      if [[ -z "$name" || "$name" == . || "$name" == .. || "$name" == */* || "$name" == *\\* ]]; then
        die "refusing artifact $id: its name '$name' is not a plain directory name"
      fi
      if [[ "$expired" == true ]]; then
        echo "github: artifact '$name' of run $run has expired; skipped" >&2
        continue
      fi
      # The token goes only where the API URL is.
      if [[ "$url" != "$api_url/"* ]]; then
        die "refusing artifact '$name': its download URL is not on $api_url"
      fi

      zip_file=$work/$id.zip
      out=$(curl --silent --show-error --config - --output "$zip_file" "${transfer_timeouts[@]}" \
        --write-out '%{http_code} %{redirect_url}' "$url" <<<"$(auth_config)") \
        || { echo "github: GitHub did not answer the download of artifact '$name'" >&2; return 2; }
      read -r code redirect <<<"$out"
      case "$code" in
        200) ;;
        301 | 302 | 303 | 307 | 308)
          [[ -n "$redirect" ]] || { echo "github: artifact '$name' redirected nowhere" >&2; return 2; }
          curl --silent --show-error --fail --location --proto '=https' --output "$zip_file" "${transfer_timeouts[@]}" \
            "$redirect" </dev/null \
            || { echo "github: downloading artifact '$name' from its storage failed" >&2; return 2; }
          ;;
        *)
            echo "github: downloading artifact '$name' failed with HTTP $code" >&2
          return 2
          ;;
      esac

      mkdir -p "$dir/$name"
      # Python's zipfile refuses members that climb out of the directory.
      python3 -I -m zipfile -e "$zip_file" "$dir/$name" </dev/null \
        || { die "artifact '$name' is not a readable zip"; }
      rm -f "$zip_file"
    done < <(jq -r '.artifacts[] | [.id, (.expired // false), .archive_download_url, .name] | @tsv' <<<"$http_body")

    ((count >= 100)) || break
    page=$((page + 1))
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
  actions-status) actions_status "$@" ;;
  wait-ref) wait_ref "$@" ;;
  dispatch) dispatch "$@" ;;
  run-status) run_status "$@" ;;
  download-artifacts) download_artifacts "$@" ;;
  *) die "unknown command '$command'" ;;
esac
