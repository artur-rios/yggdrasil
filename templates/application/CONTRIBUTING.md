# Contributing

This repository is deployed by [yggdrasil](https://github.com/<owner>/yggdrasil). It is the
application `<application-id>` in that installation's `catalog.yaml`, which decides its environments,
its Jenkins deploy job, its GitHub rulesets and required checks, and its place in the console.

## Branching model

```
feature/<name> ─┐
fix/<name> ─────┴─▶ develop ──▶ release/x.y.z ──▶ main  (tag vx.y.z)
```

| Branch | Cut from | Merges into | How |
|---|---|---|---|
| `feature/<name>`, `fix/<name>` | `develop` | `develop` | Pull request, squash or merge. The branch is deleted on merge. |
| `release/x.y.z` | `develop` | `main` | Pull request. **Never merged by hand**: Jenkins merges it once deployed. |
| `develop`, `main` | — | — | Protected: no direct pushes, no force pushes, no deletion. |

Names are lowercase: letters, digits, `.`, `_` and `-`. A `release/` branch is a snapshot of
`develop` and carries no commits of its own: a fix for a release lands on `develop` through a
`fix/` branch and a new release branch is cut.

The **Branch Policy** workflow checks all of this on every pull request and is a required check
on `develop` and `main`.

## Releasing

1. `git switch develop && git pull && git switch -c release/1.4.0 && git push -u origin release/1.4.0`.
   Jenkins deploys it to every environment whose `branches` match `release/*`.
2. Open a pull request `release/1.4.0 → main`.
3. When every GitHub check passes, Jenkins deploys that commit to each release environment in
   turn, waiting for approval where the catalog asks for it, and sets `deploy/<environment>` after
   each. Then it merges the pull request, creates the tag and GitHub release `v1.4.0`, and deletes
   the release branch.
4. If a deploy fails, it rolls back to the previous image, `deploy/<environment>` is set to failure,
   and the pull request stays open. Fix on `develop` and cut a new release.

Follow a release in the yggdrasil console: each environment's card for this system shows the
version, commit, deploy time and health of this application.

Repository administrators can bypass these rules. That is for emergencies, not routine work.
