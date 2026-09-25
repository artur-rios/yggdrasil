# Contributing

This repository is deployed by [yggdrasil](https://github.com/<owner>/<repository>) (the `owner` and
`repository` of that installation's `catalog.yaml`). It is the application `<application-id>` in
that catalog, which decides its environments, its Jenkins deploy job, its GitHub rulesets and
required checks, and its place in the console.

## Branching model

```
feature/<name> ─┐
fix/<name> ─────┴─▶ develop ──▶ release/x.y.z ──▶ main  (tag vx.y.z)
```

| Branch | Cut from | Merges into | How |
|---|---|---|---|
| `feature/<name>`, `fix/<name>` | `develop` | `develop` | Pull request, squash or merge. The branch is deleted on merge. |
| `release/x.y.z` | `develop` | `main` | Pull request, merge commit only. If the application has a release environment in the catalog, **never merge it by hand**: Jenkins merges it once deployed. Otherwise merge it yourself. |
| `develop`, `main` | — | — | Protected: no direct pushes, no force pushes, no deletion. |

A `release/` branch is a snapshot of `develop` and carries no commits of its own: a fix for a
release lands on `develop` through a `fix/` branch, and a new release branch is cut.

The **Branch Policy** workflow (check `branch-policy`) enforces this on every pull request, and is
required on `develop` and `main`. It fails, with the message shown, when:

| Rule | Message |
|---|---|
| Into `develop`, the branch must be `feature/<name>` or `fix/<name>` (`dependabot/...` is also accepted). `<name>` starts with a lowercase letter or digit, then lowercase letters, digits, `.`, `_` or `-` | `Pull requests into develop must come from feature/<name> or fix/<name> ...` |
| Into `develop`, the branch must have been cut from `develop`: it may not contain a release merge commit from `main` | `'<branch>' was branched from main. Recreate it from develop.` |
| Into `main`, the branch must be `release/<major>.<minor>.<patch>` | `Pull requests into main must come from release/<major>.<minor>.<patch> ...` |
| Into `main`, every commit of the release branch must already be on `develop` | `'<branch>' has commits that are not on develop. ...` |
| Into `main`, the version must not be released yet (no tag `v<version>`) | `Version <version> was already released (tag v<version> exists). Bump the version.` |

`v*` tags can't be moved or deleted once created.

## Releasing

1. Cut the release branch from an up-to-date `develop` and push it:
   ```bash
   git switch develop && git pull && git switch -c release/1.4.0 && git push -u origin release/1.4.0
   ```
   Jenkins deploys it to every environment with `trigger: branch` whose `branches` match
   `release/1.4.0`, as version `1.4.0` (image tag `1.4.0-<7-character commit>`).
2. Open a pull request `release/1.4.0 → main`.
3. When every **GitHub Actions** check on its head commit has passed (`branch-policy` included;
   Jenkins waits up to the catalog's `checksTimeout`, 1 hour by default), Jenkins marks every
   `deploy/<environment>` status pending, then deploys that commit to each release environment in
   catalog order, setting `deploy/<environment>` after each.
   - Where the catalog sets `approval: true`, open the build in Jenkins and click **Deploy**. A build
     still waiting after 4 hours is aborted.
   - Don't push to the release branch meanwhile: the build fails if its head moves.
4. After the last environment, Jenkins merges the pull request (`release: v1.4.0 (#<n>)`), creates
   the tag and GitHub release `v1.4.0`, and deletes the release branch.

If a deploy fails:
- that environment is rolled back to the image that was running (if there was one), its
  `deploy/<environment>` is set to failure, and the environments after it aren't deployed;
- the pull request stays open, blocked by the ruleset.

Then either re-run the build in Jenkins (**Build Now** on the `PR-<n>` job) for a transient failure,
or fix it on `develop` through a `fix/` branch and cut a new release (the failed version was never
tagged, so you may reuse it: close the pull request, delete the branch, and cut it again).

Follow a release in the yggdrasil console: select each environment in turn, and expand this
system's card to see this application's version, commit, deploy time and health.

Repository administrators can bypass these rules. That is for emergencies, not routine work.
