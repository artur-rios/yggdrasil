#!/usr/bin/env bash
# Enforces the branching model on a pull request. Jenkins runs it on every pull request into develop or
# main; the check it reports is what the branch rulesets require, so a pull request that breaks the model
# cannot be merged without the repository owner's bypass.
#
#     scripts/branch-policy.sh <head-ref> <base-ref> <head-sha>
#
#   into develop  only from feature/<name>, fix/<name> (or renovate/...), and the branch must have been cut
#                 from develop -- it may not carry any commit that is on main but not on develop, which is
#                 exactly what a branch cut from main would carry.
#   into main     only from release/<major>.<minor>.<patch>, and the release branch must be a snapshot of
#                 develop: every commit on it is already on develop. A release carries no changes of its
#                 own; a fix goes to develop through a fix/ branch and a new release is cut. The version
#                 must not be released already: a v<version> tag (an application's release) or a bare
#                 <version> one (an owner-tagged library's, tags "[0-9]*").
#   any other base  nothing to check.
#
# Exit 0 and "'<head>' -> '<base>' follows the branching model." when the pull request follows the model,
# exit 1 with the reason on stderr when it does not, exit 2 on bad usage.
#
# It runs inside a checkout where the pipeline has fetched origin/develop, origin/main and the tags; it
# fetches nothing itself. Neither GitHub nor Forgejo can restrict which branch a pull request comes from,
# which is why this is a script and not a branch protection rule.
set -euo pipefail

fail() { echo "$*" >&2; exit 1; }

[[ $# -eq 3 ]] || { echo "usage: branch-policy.sh <head-ref> <base-ref> <head-sha>" >&2; exit 2; }
head_ref=$1
base_ref=$2
head_sha=$3

case "$base_ref" in
  develop)
    [[ "$head_ref" =~ ^(feature|fix)/[a-z0-9][a-z0-9._-]*$ || "$head_ref" =~ ^renovate/ ]] \
      || fail "Pull requests into develop must come from feature/<name> or fix/<name> (lowercase, digits, . _ -), not '$head_ref'."

    # Commits on main that develop does not have are the release merge commits. A branch that contains
    # one of them was cut from main, not from develop.
    # Collected first, not read from a process substitution, so a checkout without origin/main or
    # origin/develop fails the check instead of finding no commits and passing it.
    main_only=$(git rev-list origin/main ^origin/develop) \
      || fail "Cannot compare '$head_ref' with main and develop: fetch origin/main and origin/develop first."
    while read -r commit; do
      [[ -n "$commit" ]] || continue
      if git merge-base --is-ancestor "$commit" "$head_sha"; then
        fail "'$head_ref' was branched from main. Recreate it from develop."
      fi
    done <<<"$main_only"
    ;;
  main)
    [[ "$head_ref" =~ ^release/[0-9]+\.[0-9]+\.[0-9]+$ ]] \
      || fail "Pull requests into main must come from release/<major>.<minor>.<patch>, not '$head_ref'."

    git merge-base --is-ancestor "$head_sha" origin/develop \
      || fail "'$head_ref' has commits that are not on develop. A release is a snapshot of develop: land the change on develop through a feature/ or fix/ branch and cut the release again."

    version=${head_ref#release/}
    # The pipeline fetched the tags, so a released version's tag is a local ref.
    for tag in "v$version" "$version"; do
      if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
        fail "Version $version was already released (tag $tag exists). Bump the version."
      fi
    done
    ;;
  *)
    echo "Pull requests into '$base_ref' are not covered by the branching model: nothing to check."
    exit 0
    ;;
esac

echo "'$head_ref' -> '$base_ref' follows the branching model."
