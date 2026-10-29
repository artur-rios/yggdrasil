# Moving each repository: from its prepared branch to Forgejo and Jenkins

This is the owner's guide to the work per repository that follows the [cutover](cutover.md): rollout steps 6 and 7 of
the [design](superpowers/specs/2026-10-22-self-hosted-git-and-ci-design.md#rollout), and the per-site part of step 8.
Every repository that has CI or a site has a local branch, **`feature/move-to-forgejo`**, prepared and never pushed.
It holds the repository's `yggdrasil.yml` and `ci/` scripts, its Renovate configuration, its trimmed
`.github/workflows` (the Windows and macOS jobs, with their new triggers), its links at the new addresses, and
whatever the kind needs (a release section, a site `Dockerfile`). This page lands those branches, one repository at a
time.

It does not repeat how Jenkins, Forgejo or the pipeline work: [setup.md](setup.md) does, and [catalog.md](catalog.md)
describes the catalog and `yggdrasil.yml`. What this page adds is the order, the steps that are the same for every
branch ([section 1](#1-the-steps-for-every-branch)), the table of branches ([section 2](#2-the-branches-in-order)),
and what each repository needs from you besides ([section 3](#3-what-each-repository-needs-from-you)).

| Section | What |
|---|---|
| [0. Before the first branch](#0-before-the-first-branch) | What must be true, and what must not happen early |
| [1. The steps for every branch](#1-the-steps-for-every-branch) | Push, pull request, Jenkins' results, first release, switch the old host off, tick the checklist |
| [2. The branches, in order](#2-the-branches-in-order) | Every prepared branch with its head commit, in the order to land them |
| [3. What each repository needs from you](#3-what-each-repository-needs-from-you) | Credentials, variables, tags and things to delete, by group |
| [4. Not done here](#4-not-done-here) | What the plan left to you |

## 0. Before the first branch

**The cutover was carried out on 2026-10-26.** The old server became the
[Linux Application Server](../README.md#concepts), the [Linux DevOps Server](../README.md#concepts) was added, and all
the repositories were imported into Forgejo. [`migrate.py verify`](cutover.md#516-verify-the-gate) said `0 checks
failed`, Jenkins scanned the repositories ([5.18](cutover.md#518-the-new-controller-sees-the-repositories)), and your
clone points at Forgejo ([5.19](cutover.md#519-your-clones-point-at-forgejo)). The cutover names the remotes `origin`
(Forgejo) and `github` (the mirrored copy, for reading only), and every command below pushes to `origin`:

```bash
git -C D:/Repositories/<repo> remote -v      # origin: git.techcraftsmen.net, github: github.com
```

Never push these branches to `github`: the next mirror sync would overwrite them there, and the GitHub copy is only a
mirror now.

**The branches are where the plan left them.** Each is a local branch of the clone in `D:\Repositories\<repo>`, based
on the clone's `develop` (or `main`/`master` where it has no `develop`) as it was when the plan prepared it. Before
pushing one, `git -C D:/Repositories/<repo> rev-parse --short=7 feature/move-to-forgejo` must print the head in
[section 2](#2-the-branches-in-order). A different commit means somebody changed it since: read `git log` before you
push.

**yggdrasil has already moved, and is released.** The cutover's [5.8a](cutover.md#58a-the-release-that-carries-the-repositories-plan)
merges this plan and yggdrasil's own move branch into `develop`, releases yggdrasil, and points the controller at
that tag. Every branch below relies on the pipeline of this plan (checks scoped to a catalog entry,
`release.finalize`, prereleases, the `basic` rules), which Jenkins loads at the tag in the controller's
`YGGDRASIL_LIBRARY_VERSION`. Check that it is a tag that has them before the first pull request:

```bash
grep '^YGGDRASIL_LIBRARY_VERSION=' /etc/yggdrasil/platform.env                  # on the DevOps Server
git ls-remote --tags origin 'v*' | tail -3                                      # the tags on Forgejo's yggdrasil
git show <tag>:jenkins/library/vars/yggdrasilPipeline.groovy | grep -c finalize    # not 0 when the tag has them
```

If it is `0`, 5.8a was not done or the variable was not changed: do that first. Later, to upgrade the pipeline,
release yggdrasil and set the variable again ([README, Day to day](../README.md#day-to-day): "Upgrade the pipeline").

**What had to wait for the window.** The window is over, so none of this is pending; it is the reason the branches
were prepared and left unpushed until now.

- **Nothing of this plan reached `main` before the window.** The old controller read `main`, so the plan's branch
  (`feature/plan-5-repositories`) and yggdrasil's move branch went onto `develop` and `main` in 5.8a, once the old
  controller was stopped (5.3), and the `forge` and `mirror` blocks of `catalog.yaml` were added only at
  [2.2](cutover.md#22-the-catalog-the-devops-server-reads), [4.2](cutover.md#42-your-catalog-locally) and
  [5.9](cutover.md#59-the-catalog-goes-to-main); `python3 scripts/catalog.py forge type` printed `github` until then
  (and prints `forgejo` now).
- **Merging the plan into `develop` early would have frozen yggdrasil's releases until 5.8a.** A release branch is a
  snapshot of `develop`, so any yggdrasil release cut after that merge would have carried the plan to `main`, which the
  old controller read.
- **No branch was pushed to Forgejo before the window finished.** Until 5.16 passed, pushes still went to GitHub.

**Create the credentials and variables a branch needs before its first check or release uses them.** The Jenkins
credentials are in [hosts.md, section 6](hosts.md#6-credentials): `reportgenerator-license` (every library, cerberus-api,
fortuna-api, heimdall-api), the four `heimdall-ui-android-*`, `maestro-signing-key` and `maestro-signing-public-key`,
and the four `orpheus-android-*`. Each is a file named by its id in `JENKINS_CREDENTIALS_DIR` on the DevOps Server,
which `scripts/platform.sh up` turns into a secret text credential ([hosts.md, Repository credentials](hosts.md#repository-credentials)).
Don't create them in the Jenkins UI: the controller drops a credential made there at its next restart. A check that
names a credential Jenkins does not have cannot start (`Could not find credentials entry with ID
'reportgenerator-license'`): create `reportgenerator-license` before the first pull request of a library,
cerberus-api, fortuna-api or heimdall-api. One more comes from its own file on the DevOps Server:
the nuget.org API key the libraries' tags push with, the file `NUGET_API_KEY_FILE` names in `platform.env`, which the
controller turns into the credential `nuget-api-key` ([cutover.md, 2.3](cutover.md#23-forgejo-the-controller-and-the-agents);
[setup.md, Owner tags and NuGet](setup.md#owner-tags-and-nuget)). Without it a library's tag build packs, then fails
at the push. The secrets that go away are in the same page's
[Secrets that go away](hosts.md#secrets-that-go-away).

**The agents can reach what the checks download.** Outbound from the CI and deploy agents: `github.com`, Docker Hub,
`proxy.golang.org`, `registry.npmjs.org`, Google Fonts, `storage.googleapis.com` (Flutter), `nuget.org` and
`crates.io`. Several checks download their toolchains when they run: Flutter alone is about 700 MB to 1.5 GB per
check, and there are also CEF, the Android NDK, Temurin and CPython. The first run of each is slow; later ones reuse
what the agent kept.

**The checklist is the reference.** [migration-checklist.md](migration-checklist.md) lists every repository, with its
GitHub workflows and what each became. Two sections of it are written by hand and are authoritative:
[Check names](migration-checklist.md#check-names) and [Credentials](migration-checklist.md#credentials). The table's
*Jobs* column is generated and says `→ check` for some Linux jobs that became `release.build` (cerberus-ui's and
fortuna-ui's `linux`; maestro's and orpheus's show `→ release`): read the *Check names* section, not the verdict. The
checklist's last section, [Repositories with no prepared branch](migration-checklist.md#repositories-with-no-prepared-branch),
says why the other 24 need nothing.

## 1. The steps for every branch

Do these in order for one repository, then tick it off, then the next. A repository's group in [section 2](#2-the-branches-in-order)
says where it is in the order; [section 3](#3-what-each-repository-needs-from-you) says what it needs on the way.

### Step 1: push the branch to Forgejo

In the repository's clone, on your machine:

```bash
cd D:/Repositories/<repo>
git remote get-url origin                      # https://git.techcraftsmen.net/artur-rios/<repo>.git
git log --oneline -1 feature/move-to-forgejo   # the head of section 2
git push -u origin feature/move-to-forgejo
```

`git push` asks for your Forgejo user and a token, or uses SSH ([cutover.md, 5.19](cutover.md#519-your-clones-point-at-forgejo)).

**Success looks like:** `https://git.techcraftsmen.net/artur-rios/<repo>/src/branch/feature/move-to-forgejo` shows the
branch. Within a minute the mirror copies it, and `git ls-remote github refs/heads/feature/move-to-forgejo` prints the same
commit; where the repository has Windows or macOS jobs ([`actionsChecks`](catalog.md#git-server)), they start on GitHub
from that push.
**If it fails:** `Authentication failed`: the token or its scope (5.19). `remote origin` is `github.com`: do 5.19 first.
**Back out:** `git push origin --delete feature/move-to-forgejo`; the next mirror sync deletes it on GitHub.

### Step 2: open the pull request

On Forgejo, `https://git.techcraftsmen.net/artur-rios/<repo>/compare/<base>...feature/move-to-forgejo`, where `<base>` is
the table's *Base* column: `develop` for a repository under the release model, else its `main` or `master`. Title it
`chore: move ci and releases to jenkins`, and say in the description that it is the repository's move
(link to [migration-checklist.md](migration-checklist.md)). One pull request per repository, from the branch as it is.

### Step 3: check Jenkins' results, then merge with the override

Jenkins builds the pull request in the job named like the catalog entry (an application's id, or a `repositories` entry's id;
`python3 scripts/catalog.py repository-names` lists the repositories, `python3 scripts/catalog.py repository-entry <repo>`
shows a `repositories` entry's checks and `python3 scripts/catalog.py show <application>` an application's):
`https://jenkins.techcraftsmen.net/job/<job>/` → **Pull Requests** → `PR-<n>`. A repository can have more than one job
(a library and its `-docs` site; heimdall-api and `heimdall-api-docs`), and each reports on the same pull request.
It appears there on its own, through the webhook; if not, **Scan Repository Now**.

On the pull request, Forgejo shows a status for `branch-policy`, for each check of the job's catalog entry, and (with a
`mirror`) for each GitHub Actions job of its `actionsChecks`, copied from the run on the GitHub copy of the branch
([setup.md](setup.md#github-actions-and-the-skip)). The expected set is the repository's rows in the checklist's
[Check names](migration-checklist.md#check-names).

**Success looks like:** every check of the repository's rows is `success`, the build is green and not unstable
(an unstable build means `yggdrasil.yml` defines a check that no catalog entry of the repository runs; a check the
catalog names that the file does not define is an error, and the build fails), and no check is `skipped` that you
expected to run.

**Then merge with the override.** The first pull request of a repository cannot satisfy the old rules and the new
ones at once: under the release model `develop` requires `branch-policy` and the checks, and `yggdrasil.yml` is a
protected file there, so only the owner can merge a change to it, with a forced merge. Once Jenkins' checks are green,
use **Merge anyway** on the pull request ([setup.md](setup.md#forgejo-details-worth-knowing); through the API,
`force_merge: true`). Merge with a merge commit. Do not use it while a check you expected is red or missing.

- **A repository under the `basic` rules and with no `develop`** (the sites deployed from `main` or `master`, and
  coding-challenge-library, dotnet-libraries and reveal.js) has no required checks, so Forgejo may let you merge
  normally. Its `branch-policy` passes with `basic: nothing to check` for any pull request, so a pull request from
  `feature/move-to-forgejo` into `main` (or `master`, which the basic rules treat like `main`) is fine; read the
  other checks, and merge.
- **A merge into `main` of such a site deploys it.** See step 4.
- **A merge into `develop`** of an application deploys it to `development`, which is on demand: Jenkins starts it,
  checks it and stops it again.

**If it fails:** a failed check is the repository's: read its log in Jenkins (the check's status links to the build).
`describe` warnings and errors are in the build's first stage. A check that needs a credential you have not
created yet says which. Fix it on the branch (`git switch feature/move-to-forgejo`, commit, `git push`), and the
pull request builds again; do not merge around a real failure.
**Back out:** close the pull request.

### Step 4: the first release, publish or site deploy

Each repository's first release, publish or deploy after the move is its end-to-end test. What it is depends on the
kind:

| Kind | The first one | Success looks like |
|---|---|---|
| **Deployed application** (heimdall-api, heimdall-ui, fortuna-api, fortuna-ui, chimera) | A release, as [setup.md, 14](setup.md#14-the-first-release): `git switch develop && git pull && git switch -c release/<x.y.z> && git push -u origin release/<x.y.z>`, then the pull request `release/<x.y.z>` → `main`, approving each **Deploy** | Jenkins merges and tags `v<x.y.z>`; the release has its files; `ygg status` on the Application Server shows the new version. Set what the repository's `CONTRIBUTING.md` asks for (a version in the manifest, a changelog heading) on `develop` first: a release branch carries no commits of its own |
| **Library** (the 12 `dotnet-*`) | The release of its `CONTRIBUTING.md`, "Releasing": a `release/<version>` pull request into `main`, merged by you with a merge commit, then an **annotated** tag with the bare version, pushed the same day | The package is on nuget.org, and the docs site is rebuilt at `docs.artur-rios.tech/<repo>` by the push to `main` |
| **Owner-tag repository with release files** (yggdrasil, alexandria-ui, cerberus-ui, maestro, orpheus, personal-lab) | The same, with a `v<version>` tag | The release has its files (and, for alexandria-ui, cerberus-ui, maestro and orpheus, their Windows ones); for maestro, `release-manifest.json` and `.sig` |
| **CI repository with no release** (alexandria-api, cerberus-api, ai-coding-skills, coding-challenge-library, reveal.js) | Nothing to release: the pull request's green checks are the test | Step 3 |
| **A site deployed from `main` or `master`** (the docs sites, personal-blog, dotnet-libraries; on `master`, the forks under-construction-template and github-readme-stats) | The merge of the pull request into `main` or `master` | The site answers at its address ([address](#where-the-sites-are)) with the new content |
| **A site in the release flow** (personal-page, tech-craftsmen-company-page) | A release, as for an application, which deploys homologation, then production | Both addresses answer |

**Owner tags.** `git tag -a <version> -m <version> && git push origin <version>`, on a commit of `main`, **annotated** and
pushed the same day: Jenkins builds a tag only within a day of its creation, and dates a lightweight tag by its commit, so
an old commit's tag is silently never built ([setup.md, Owner tags and NuGet](setup.md#owner-tags-and-nuget)). If a tag
was not built, run `release-resume`.

#### Where the sites are

`python3 scripts/catalog.py public-host <application> <environment> techcraftsmen.net` prints a site's host and
`python3 scripts/catalog.py public-path <application>` its path; for example `personal-page production` prints
`www.artur-rios.tech`, and `heimdall-docs production` prints `docs.artur-rios.tech` with the path `/heimdall-docs`.

### Step 5: switch the old host off (sites)

For a repository with a site on GitHub Pages or Vercel, once the new address serves it:

- **GitHub Pages** (the 19 Pages sites: the three docs repositories, the 12 libraries, dotnet-libraries, orpheus,
  heimdall-api and personal-lab): on the GitHub copy, **Settings → Pages**, set the source to **None**.
- **Vercel** (the five projects: personal-page, personal-blog, tech-craftsmen-company-page, under-construction-template,
  github-readme-stats): in Vercel, delete the project.
- **Then the DNS.** In Cloudflare's **DNS → Records**, delete the site's records that pointed at Vercel
  (`cname.vercel-dns.com`, or an `A` to `76.76.21.21`), the ones the cutover kept ([5.8](cutover.md#58-dns-the-application-servers-names-behind-cloudflare);
  1.5's zone exports list them), in both zones:
  - `artur-rios.tech`: the `CNAME`s of `www` (personal-page) and `blog` (personal-blog), and the bare domain's record
    if it points there;
  - `techcraftsmen.net`: the company page's `www` and the bare domain's record (tech-craftsmen-company-page), and any
    other name of the zone the export shows on Vercel, with its site.

  The wildcard record then sends a subdomain to the Application Server. A bare domain has no record left: run
  `cloudflare_dns.py` (below) right after deleting it, with the `--skip-caa` flags still needed, and it makes the bare
  domain's `A`. These names stay the same, so the new site is served only once their Vercel record
  is gone, and you cannot try it first: the Application Server refuses anything that does not come through Cloudflare
  ([cutover.md, 8](cutover.md#8-what-you-check-by-hand)). For each, delete the record first, look at the site at once
  (`curl -sI https://www.artur-rios.tech/`, `curl -sI https://www.techcraftsmen.net/`), and delete the Vercel project
  only when it is right. To go back, put the record back from 1.5's zone export, DNS only.
- **Then the CAA records.** Once a zone's Vercel names have all moved (`artur-rios.tech`: `www.` and `blog.`;
  `techcraftsmen.net`: the company page and any other), drop that zone's `--skip-caa` (plan 4's ruling,
  [hosts.md, DNS records](hosts.md#dns-records)); the next run makes the zone's `CAA`, which allows Let's Encrypt only
  through DNS-01. Keep the flag of a zone that still has a name on Vercel:

  ```bash
  python3 scripts/cloudflare_dns.py --dry-run --apps <apps ip> --dev <dev ip> [--skip-caa <the zone still on Vercel>]
  python3 scripts/cloudflare_dns.py --apps <apps ip> --dev <dev ip> [--skip-caa <the zone still on Vercel>]
  ```

**Success looks like:** the site answers at its new address; the old one is gone (a Pages address answers `404`); the
dry run prints `would create CAA <zone>: ...` for the zone whose flag you dropped, and the real run creates it.
**If it fails:** the site is down after you deleted a record: put the record back from 1.5's zone export
([cutover.md](cutover.md#15-the-records)).

### Step 6: tick the checklist

In [migration-checklist.md](migration-checklist.md), fill the repository's **Moved** cell with the date (for example
`2026-11-02`). Regenerating the checklist keeps it:

```bash
python scripts/repo_inventory.py D:/Repositories --output docs/migration-checklist.md
```

Run it with Windows Python and git (WSL reads the clones' line endings as changes). It updates the facts, keeps the
*Prepared branch* and *Moved* columns and the hand-written sections, and shows the clones as they are now: a clone that
is on its move branch lists the trimmed workflows, so switch back to `develop` before running it.

## 2. The branches, in order

The order is the design's: the deployed applications, then yggdrasil, then the libraries, the other CI repositories and
the sites; within a group, the order is yours. **Base** is the pull request's target: `develop`, or the repository's
only branch. Each branch is local to the clone and has never been pushed.

| Group | Repository | Head of `feature/move-to-forgejo` | Base | Also on the way |
|---|---|---|---|---|
| 1. Deployed applications | heimdall-api | `23e1a61` | `develop` | Its docs site is a second job, `heimdall-api-docs` |
| | heimdall-ui | `c99a397` | `develop` | |
| | fortuna-api | `2b3ff9f` | `develop` | |
| | fortuna-ui | `ccbfb6f` | `develop` | |
| | chimera | `1273a10` | `develop` | |
| 2. yggdrasil | yggdrasil | `247ab03` | `develop` | In the worktree `D:\Repositories\yggdrasil-move`; the head is before the rebase of 5.8a, which moves it and merges it ([below](#yggdrasil-itself)) |
| 3. Libraries | dotnet-configuration | `93b613d` | `develop` | Each also has a docs site, `<repo>-docs` |
| | dotnet-data | `86a357f` | `develop` | Tags `<package>@<version>`; release branches named by date |
| | dotnet-extensions | `d15fd94` | `develop` | |
| | dotnet-jwt | `a80ff8a` | `develop` | |
| | dotnet-logging | `d7de9c1` | `develop` | |
| | dotnet-mediator | `5f1ca85` | `develop` | |
| | dotnet-messaging | `5112afd` | `develop` | |
| | dotnet-output | `45bb9f3` | `develop` | |
| | dotnet-test-util | `652f985` | `develop` | |
| | dotnet-util | `18d618b` | `develop` | |
| | dotnet-validation | `9e73a91` | `develop` | |
| | dotnet-webapi-util | `6ac2044` | `develop` | |
| 4. CI repositories | alexandria-api | `1b78189` | `develop` | |
| | alexandria-ui | `d8691c5` | `develop` | |
| | cerberus-api | `0a0d512` | `develop` | |
| | cerberus-ui | `93f5b36` | `develop` | |
| | maestro | `6de8636` | `develop` | |
| | orpheus | `96a3767` | `develop` | Its docs site is a second job, `orpheus-docs` |
| | personal-lab | `2dfde56` | `develop` | Its site is the second job `personal-lab-site` |
| | ai-coding-skills | `a8e14db` | `develop` | |
| | coding-challenge-library | `f0b02c7` | `main` | |
| | reveal.js | `3290a3f` | `master` | A fork |
| | dotnet-libraries | `710a709` | `main` | The index of the libraries' docs; the docs site is `dotnet-libraries-docs` |
| 5. Sites | tech-craftsmen-company-page | `b99bd84` | `develop` | Its `develop` is the cutover's ([5.9a](cutover.md#59a-the-company-pages-develop), [below](#sites)) |
| | personal-page | `d3a2488` | `develop` | |
| | personal-blog | `1092020` | `main` | |
| | under-construction-template | `c2be3e9` | `master` | A fork |
| | github-readme-stats | `6ff454d` | `master` | A fork |
| | heimdall-docs | `d832794` | `main` | |
| | fortuna-docs | `c7249a7` | `main` | |
| | alexandria-docs | `8347506` | `main` | |
| | artur-rios | `644bac5` | `main` | The GitHub profile; merge it last ([below](#sites)) |

38 branches. The other 24 repositories have none, each with its reason in the checklist. The heads are the same as the
checklist's *Prepared branch* column.

## 3. What each repository needs from you

### The deployed applications

- **heimdall-api.** Credential `reportgenerator-license`. Its Docsy submodule became the pinned `@docsy/theme` package,
  and the docs site's image is `docs.Dockerfile` (the root `Dockerfile` is the API's). The docs site
  (`docs.artur-rios.tech/heimdall-api`) deploys from `main`, so its GitHub Pages ([step 5](#step-5-switch-the-old-host-off-sites))
  goes off after the first release.
- **heimdall-ui.** The four `heimdall-ui-android-*` credentials, before the first release build (they sign the APK).
- **fortuna-api.** Credential `reportgenerator-license`. Do not bump the MSRV image pin: Renovate is told not to.
- **fortuna-ui.** The release APK has no `INTERNET` permission in its `AndroidManifest`, and is debug-signed. That was
  already so before the move; fixing it is yours, in the app.
- **chimera.** Nine of its checks are `actionsChecks` on the GitHub copy (Windows, and Safari on macOS). Its
  ReportGenerator test skips because of `--allow-roll-forward`, both before and after the move; dropping the flag so
  the test runs is a follow-up for you.

### yggdrasil itself

Its branch lives in the worktree `D:\Repositories\yggdrasil-move` (`feature/move-to-forgejo`@`247ab03`, based on
`7f29487`), cut from this plan's branch and never merged into it, so that the plan could land on `develop` without
trimming `ci.yml` and relinking to a server that was not live yet. It is **already merged by the time the
per-repository order starts**: the cutover's [5.8a](cutover.md#58a-the-release-that-carries-the-repositories-plan)
rebases it onto the plan, merges both into `develop`, releases yggdrasil (a `release/<x.y.z>` pull request into
`main`, merged by you, then an annotated `v<x.y.z>` tag) and sets the controller's `YGGDRASIL_LIBRARY_VERSION` to
the tag. So steps 1 to 3 do not apply to it, and the head in section 2 is the one before the rebase. What is left:

- **Its release on Forgejo.** The tag of 5.8a, if Jenkins finds it within a day of its creation (5.18's scan), is built
  as yggdrasil's first release there: the release files, then the release on Forgejo and its GitHub copy. If not,
  the next release is the end-to-end test ([step 4](#step-4-the-first-release-publish-or-site-deploy)): an annotated
  `v<version>` tag on `main`, after a `release/<version>` pull request merged by you.
- `Console (Windows)` is an `actionsCheck`, and its `pipeline-live` workflow is no check: it stays a GitHub Actions
  workflow on the GitHub copy, run by hand.
- Tick its row of the checklist, with the head 5.8a left.

### The libraries

- Credential `reportgenerator-license` for all twelve (their `coverage` check; their docs sites take its report).
  The release agent's `nuget-api-key` comes from the key file you put on the DevOps Server and name in `NUGET_API_KEY_FILE`
  ([section 0](#0-before-the-first-branch)): check it is there (**Manage Jenkins → Credentials** lists `nuget-api-key`)
  before the first library's tag.
- **The release.** `CONTRIBUTING.md`'s "Releasing" on each branch: the version and the changelog go on `develop` first,
  the release branch is a snapshot of `develop`, and the **annotated** tag has the bare version (`1.3.0`), pushed the same
  day. dotnet-data tags `<package>@<version>` and names its release branches by date (`release/YYYY.MM.DD`).
- **Network.** The checks pull the .NET SDK image and NuGet packages; dotnet-data's integration tests start MySQL and
  DynamoDB through the CI engine.
- GitHub Packages publishing is gone: nuget.org is the only feed.

### The CI repositories

- **alexandria-ui.** 0.x releases are now normal releases: only a SemVer pre-release suffix (`-alpha`, `-rc.1`)
  makes a prerelease, where they were all GitHub prereleases before. The `Sandboxed page engine (linux)` job stays on
  GitHub as an `actionsCheck`: Chromium's sandbox needs user namespaces that a Jenkins check container does not have.
- **alexandria-api.** `performance` stays a GitHub Actions workflow (pushes only, never gating).
- **cerberus-ui.** Released by `v*` tags like alexandria-ui; its Linux build is `release.build`, its Windows one stays on
  GitHub. Its `web-bundle` check builds the site's image, so it needs the CI engine (`docker: true`). A release fails
  when its version differs from `pubspec.yaml`'s.
- **maestro.**
  - The Windows workflow (`release-windows.yml`) reads its update URLs and its public key from **GitHub** repository
    settings, not Jenkins: the variables `MAESTRO_RELEASE_MANIFEST_URL` and `MAESTRO_RELEASE_SIGNATURE_URL`, and the secret
    `MAESTRO_RELEASE_PUBLIC_KEY_BASE64`. Set them on the GitHub copy, equal to the Linux build's defaults:
    `https://github.com/artur-rios/maestro/releases/latest/download/release-manifest.json` and `.../release-manifest.sig`.
  - The GitHub build-provenance attestations are gone: they need GitHub's own runners.
  - **Signing is mandatory.** A release stops without both Jenkins credentials, `maestro-signing-key` and
    `maestro-signing-public-key`. The signed update manifest covers the Windows files, so it is made by `release.finalize`
    after they are gathered.
- **orpheus.** The signing certificate's fingerprint is committed in `android/signing-sha256.txt` (it was a GitHub
  variable; it is public, not a secret). Without it a release fails. The four `orpheus-android-*` credentials before
  the first release build. Its docs site is `orpheus-docs`.
- **personal-lab.** The release is the owner-tag flow (it replaces the old workflow that tagged on its own). Its Nuno
  theme is fetched by commit in the `Dockerfile`; `lab.artur-rios.tech` is the site.
- **cerberus-api, ai-coding-skills, coding-challenge-library, reveal.js.** Nothing besides step 3. reveal.js is a fork:
  Renovate manages only the pinned tools, never the upstream manifests.
- **dotnet-libraries.** Its check is `verify`; the docs site it builds is the index of the 17 docs sites.

### Sites

- **tech-craftsmen-company-page** had **no `develop`** on GitHub. The cutover made it from `main`
  ([5.9a](cutover.md#59a-the-company-pages-develop)), before the import, so it is on Forgejo, the default branch and
  protected; its pull request goes into it. Its branch adds a `yggdrasil.yml` that names no check: the file turns the
  pipeline on, so the pull request gets `branch-policy`, which `develop` requires.
- **personal-page.** Its `CONTRIBUTING.md` names `www-dev.artur-rios.tech` and `www-hml.artur-rios.tech`: check that DNS
  has them (the wildcard record of the zone should).
- **personal-blog.**
  - Delete the local, untracked `.vercel/` and `.env.local` in the clone: `.env.local` holds a `VERCEL_OIDC_TOKEN`.
  - Giscus needs the GitHub repository `personal-blog-comments` with **Discussions** on.
- **github-readme-stats.** On the Application Server, as application variables of `production`
  (`ygg vars set github-readme-stats@production PAT_1=-`, or `/etc/yggdrasil/production/github-readme-stats.env`):
  - `PAT_1`, and optionally `PAT_2` to `PAT_n`: read-only GitHub tokens with no permissions;
  - `WHITELIST=artur-rios`, required: it limits the token to your cards;
  - `GIST_WHITELIST` if gists are served.

  Vercel's edge cache is gone, so only `max-age` reaches browsers and `camo`. These are application variables, not Jenkins credentials.
- **The docs sites** (heimdall-docs, fortuna-docs, alexandria-docs) have a `build` check, as their Pages workflow built on
  every pull request; the merge into `main` deploys them.
- **artur-rios** (the profile) has no CI. Its `Top Langs` card points at `readme-stats.techcraftsmen.net`, which answers
  only after github-readme-stats is deployed with its token: merge it last, and check the card on the profile.

## 4. Not done here

What the plan left for later or for you:

- **Docsy "edit this page" links** still point at the GitHub mirror. Forgejo's edit address is `/_edit/<branch>/<path>`,
  not Docsy's `/edit/`, so every Docsy site needs a change in its configuration.
- **`relink.py` can rewrite test fixtures** that deliberately use old addresses: it did in heimdall-docs and fortuna-docs,
  both fixed. The other branches were not checked for it. Before a branch's pull request, read what it changed under
  test and fixture paths, against the table's *Base* (`develop`, `main` or `master`) as the remote has it:
  `git -C D:/Repositories/<repo> diff origin/<base>...feature/move-to-forgejo -- '*test*' '*fixture*'`
  (`origin/main` for tech-craftsmen-company-page, whose `develop` exists only on the server). An address rewritten
  there that a test meant to keep old is put back on the branch.
- **A 0.x prerelease option** in the pipeline, only if you want one (alexandria-ui's 0.x releases were prereleases).
- **Rotate the GitHub CLI token now**, not after the moves: it was exposed in a subagent's transcript during Task 1 of
  this plan. On GitHub, **Settings → Applications → Authorized OAuth Apps → GitHub CLI → Revoke** (a token you gave `gh` yourself:
  delete it under **Developer settings → Personal access tokens**), then `gh auth login` again.
- **A Renovate bump of a tool pin whose checksum sits beside it** (a version and its SHA-256 together in a `ci/` script
  or a `Dockerfile`) fails its check until you update the checksum by hand, on Renovate's branch: Renovate changes the
  version only.
- **chimera's ReportGenerator test** skips because of `--allow-roll-forward`, before and after the move.
- **fortuna-ui's release APK** lacks the `INTERNET` permission, and is debug-signed.
- **The checklist's generated *Jobs* column** says `→ check` for some jobs that became `release.build` (cerberus-ui's
  and fortuna-ui's `linux`); only the *Check names* section is right, and regenerating does not change that.
