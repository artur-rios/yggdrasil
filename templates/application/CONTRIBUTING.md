# Contributing

This repository is deployed by [yggdrasil](https://github.com/<owner>/<repository>) (the `owner` and
`repository` of that installation's `catalog.yaml`). It is the application `<application-id>` in
that catalog, which decides its environments, its Jenkins deploy job, its GitHub rulesets and
required checks, and its place in the console.

## Environments

The catalog decides where each branch goes. With yggdrasil's default catalog:

| Environment | Deployed by | Runs |
|---|---|---|
| `local` | You, on your machine (`scripts/deploy.sh local <application-id> ...` in yggdrasil, or this repository's own instructions) | While you run it |
| `development` | Jenkins, on every push to `develop` | On demand |
| `homologation` | Jenkins, on every push of a `release/x.y.z` branch | On demand |
| `production` | Jenkins, on a green `release/x.y.z → main` pull request, which it then merges and tags | Always |

An on-demand environment runs only while someone uses it. On its host,
`scripts/ygg.sh env start <environment>` (in the yggdrasil checkout) turns it on and
`scripts/ygg.sh env stop <environment>` off again. While it is off, Jenkins still deploys to it:
it builds the new version, checks it becomes healthy (rolling back otherwise), and switches it off
again, so the next `env start` runs the new version.

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

## Commits and changelog

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) with a
lowercase subject, e.g. `feat: add password reset` or `fix: return 404 for an unknown order`.
With squash merges, the pull request title becomes the commit on `develop`: give it the same form.

Record every change a user or operator of this application would notice under `## [Unreleased]`
in `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format), in the same
pull request that makes it.

## Versioning

This application follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). For an
application, the parts of a version mean:

- **Major**: something its users, its clients or its operator must adapt to. A feature or
  behaviour users rely on removed or changed; an HTTP endpoint, field or status code removed or
  changed so that existing clients break; a new required setting or env variable, or one renamed;
  a database migration the previous release can't run against (yggdrasil's rollback does not undo
  migrations).
- **Minor**: something new that is compatible with all of the above: a feature, an endpoint or an
  optional field, an optional setting, a migration the previous release still works with.
- **Patch**: a fix that changes nothing users, clients or the operator must adapt to.

Before 1.0.0, SemVer allows anything to change; until then a change that would need a major
version increments the minor version, and its changelog entry says what to adapt.

The version is the release branch's name, `release/<major>.<minor>.<patch>`: Jenkins deploys the
branch as that version, and the release is tagged `v<version>`. If the application also records its version in a
file (`package.json`, a `.csproj`, `pubspec.yaml`...), bump it on `develop` together with the
changelog (step 1 below), never on the release branch.

## Releasing

1. A release branch carries no commits of its own, so prepare the release on `develop` first, in a
   `feature/` or `fix/` branch merged like any other: in `CHANGELOG.md`, rename `## [Unreleased]`
   to `## [1.4.0] - <yyyy-mm-dd>` above a fresh, empty `## [Unreleased]` and update the compare
   links at the bottom; bump the version file, if there is one.
2. Cut the release branch from an up-to-date `develop` and push it:
   ```bash
   git switch develop && git pull && git switch -c release/1.4.0 && git push -u origin release/1.4.0
   ```
   Jenkins deploys it to every environment with `trigger: branch` whose `branches` match
   `release/1.4.0` (homologation, with the default catalog), as version `1.4.0` (image tag
   `<environment>-1.4.0-<7-character commit>`). To try it there, turn the environment on (see
   [Environments](#environments)).
3. Open a pull request `release/1.4.0 → main`.
4. When every **GitHub Actions** check on its head commit has passed (`branch-policy` included;
   Jenkins waits up to the catalog's `checksTimeout`, 1 hour by default), Jenkins marks every
   `deploy/<environment>` status pending, then deploys that commit to each release environment in
   catalog order, setting `deploy/<environment>` after each.
   - Where the catalog sets `approval: true`, open the build in Jenkins and click **Deploy**. A build
     still waiting after 4 hours is aborted.
   - Don't push to the release branch meanwhile: the build fails if its head moves.
5. After the last environment, Jenkins merges the pull request (`release: v1.4.0 (#<n>)`), creates
   the tag and GitHub release `v1.4.0`, and deletes the release branch.

Steps 4 and 5 are Jenkins' only when the application has a release environment (`trigger: release`)
in the catalog. Otherwise, once the checks pass, merge the pull request yourself with a merge commit
and create the release, which creates the tag:

```bash
gh release create v1.4.0 --target main --generate-notes
```

If a deploy fails:
- that environment is rolled back to the image that was running (if there was one), its
  `deploy/<environment>` is set to failure, and the environments after it aren't deployed;
- the pull request stays open, blocked by the ruleset.

Then either re-run the build in Jenkins (**Build with Parameters** on the `PR-<n>` job, `DEPLOY_TO` left empty) for a transient failure,
or fix it on `develop` through a `fix/` branch and cut a new release (the failed version was never
tagged, so you may reuse it: close the pull request, delete the branch, and cut it again).

Follow a release in the yggdrasil console: this system's card has a status per environment
(`Stopped` for an on-demand environment that is off: not a problem); expand it to see this
application's version, commit, deploy time and health in each environment.

Repository administrators can bypass these rules. That is for emergencies, not routine work.
