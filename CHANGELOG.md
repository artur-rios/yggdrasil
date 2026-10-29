# Changelog

All notable changes to yggdrasil are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A site deploys without variables.** `deploy.sh` stops an application that has no variables in an environment,
  as never configured on that machine. Every static site (the docs sites, personal-page, personal-blog,
  personal-lab-site, under-construction-template, tech-craftsmen-company-page) reads none, so none of them could
  deploy: `heimdall-docs has no variables in production in the variables store`. An application whose Compose files
  read no variables of their own now deploys with none: no `env_file`, and no `${NAME}` but the ones `deploy.sh`
  sets. One that reads some (an API, heimdall-ui, github-readme-stats) is stopped as before.
- **A refused branch name is red in Jenkins.** The Branch policy stage posted `failure` to the forge but returned
  normally, so Jenkins drew it green, and the build failed with every stage green. It now marks its stage failed
  after posting the status, as a failed check does since 0.9.4. The checks still run, and the build fails once
  they have reported, as before.

## [0.9.4] - 2026-10-29

### Fixed

- **`ygg` no longer refuses a host's own environment now and then.** It checked whether a name was in a list with
  `printf '%s\n' <list> | grep -q`. Under pipefail that fails at random on a match: printf writes one line at a
  time, grep exits at the match, and printf's next line dies on a broken pipe. `ygg env`, `ygg deploy` and the
  others could then stop with "'homologation' (ENVIRONMENTS …) is not an environment in catalog.yaml", and the
  `platform` check failed three times on the CI agent while 0.9.4 was being prepared. host.sh now looks names up in bash
  (`in_list`), and a test keeps `printf | grep -q` out of the scripts.
- **A Flutter repository's check results are collected.** `flutter pub get` links each desktop plugin from the pub
  cache, outside the checkout (`<platform>/flutter/ephemeral/.plugin_symlinks`), and the pipeline refuses to collect
  anything from a checkout with a link that leads out of it: every Flutter repository's check (alexandria-ui,
  cerberus-ui, chimera, fortuna-ui, heimdall-ui, maestro, orpheus) ended unstable, its test results and archives not
  collected, and a release build's files would have been refused the same way. The pipeline's link guard now
  removes those generated directories first; a `.plugin_symlinks` that is itself a link, and every other link out,
  are refused as before.
- **The docs say what a missing check credential does.** moving-repositories.md and the library template said a
  check without `reportgenerator-license` falls back to the free report; Jenkins cannot start it at all (`Could not
  find credentials entry`), and a `renovate/*` build is refused a check with credentials.
- **A failed check is red in Jenkins.** Each check's `sh` returned its exit status, so nothing failed the check's
  parallel branch: Jenkins drew a failed check green, and only the build's end (`Failed: test`) and the forge's
  `failure` status said otherwise. After posting the status, a failed check now marks its branch failed, with the
  status's description as the reason. The build's result is untouched there: the other checks still run, and
  the build fails once all have reported, as before.
- **A repository whose name has a dot is checked.** The pipeline library refused any job name but an application's
  id (lowercase letters, digits and dashes), while `scripts/catalog.py` lets a `repositories` entry's id be its
  repository's name, dots included: every build of `reveal.js` failed in its first second with "is not a catalog
  id", before it posted any status, so its pull request never showed a check. The library now accepts a
  repository's name too, though never `.`, `..` or one starting with `-`.
- **Every GitHub Actions check the catalog names is copied.** A pull request's `actionsChecks` are reported as
  statuses, and the library refused a status name that started with anything but a letter or digit or had a comma
  in it. chimera's `.NET project evaluation (windows-latest)` stopped its build after four of its nine copied checks,
  which were left pending for good. A status name is now any printable ASCII up to 100 characters with no space at
  either end, and `scripts/catalog.py` validates `actionsChecks` with that same rule, so a name the pipeline could
  not report is a catalog error rather than a stuck pull request.

## [0.9.3] - 2026-10-27

### Fixed

- **The console's Repository links open the git server.** The status API built every application's `repository`
  as `https://github.com/<owner>/<repository>`, so on a Forgejo installation the console's Repository button
  opened the read-only GitHub copy. It now reads the catalog's `forge` block, as `scripts/catalog.py` does: Forgejo's
  `url` when there is one, `https://github.com` otherwise. The status API's contract is unchanged (still a string or
  `null`); its catalog errors no longer say GitHub where they mean the git server.
- **Ctrl-C at the menu's pause always returns to the menu.** After a command, "Enter to return to the menu"
  waited in `input()`, which a Ctrl-C ended through SIGINT; a SIGINT that came after the prompt but before
  `input()` was reading went unnoticed, and the pause waited on with Ctrl-C lost. On a busy machine that happened
  about one time in seven (and failed the TUI test in Jenkins). On a terminal the pause now reads keys with the
  terminal's signals off, switched before the prompt shows, as the menu itself does: Ctrl-C is a key, Enter and
  Ctrl-D return as before.
- **The socket proxies' live suite runs on a Jenkins agent.** Its fake Docker sockets live in the check's scratch
  directory, in the checkout, where the engine can mount them, and on an agent the checkout's path is long
  (`/home/ci/agent/workspace/<job>@<n>/app`): past the 107 bytes a Unix socket's path may have, every build's
  `platform` check stopped at `OSError: AF_UNIX path too long`. The fake binds through a short link in `/tmp`
  when the path is too long; the socket is still made where the engine mounts it. The suite's Traefik listens on
  8080: with no entry point it took :80, which a check may not bind even in its own container (the `ci` user's
  slice denies ports below 1024, docs/hosts.md), and it stopped at `bind: operation not permitted`. A client
  that does not get where the test waits for now fails with its own log and the requests its fake received, and
  Traefik waits up to 60 seconds, as Alloy does.
- **`catalog.yaml` has its `forge` and `mirror` blocks once.** Cutover step 5.9 had pasted both twice, identically,
  which YAML reads without a word (the last of two equal keys wins). A test now refuses a top-level key that is in
  the repository's catalog twice.

### Changed

- **yggdrasil is free and open source**, under the Artur Rios Attribution License (`LICENSE`): you may view, use,
  fork, modify and extend it, crediting the author. It was proprietary.
- **The docs name the two servers the Linux DevOps Server and the Linux Application Server**, defined in the
  README's Concepts, instead of "the dev machine", "the apps machine" and "the VPS": any Ubuntu 24.04 or 26.04
  LTS machine will do, rented, in a cloud, physical or at home. The roles keep their names (`dev`, `apps`), as
  does the deploy agent `vps`. The docs were reviewed for what the move to Forgejo and Jenkins made out of date;
  the README's overview diagram shows the two servers and the GitHub mirror, its Upgrading section links every
  upgrade note, and its note on the GitHub mirror is now at the end of Contributing.
- **The `platform` check runs the unit suite in parallel.** It took twenty minutes on its own, forty in Jenkins
  next to the other checks, most of it `test_machine` starting short processes one test after another.
  `scripts/run_tests.py` runs the same tests as `python3 -m unittest discover -s scripts`, with the same results,
  in chunks on up to eight workers (`-j`, or `YGG_TEST_WORKERS`); `ci/platform.sh` and CONTRIBUTING.md use it.
- **`machine check` starts far fewer processes.** A check asked `getent` for a user's uid or home over a thousand
  times, `sudo -n true` almost as often and `dpkg-query` once per package. Within one check (which adds no user,
  installs nothing and does not change sudo), each is now asked once, when it starts, and forgotten when it ends;
  `passwd_field` splits the entry itself instead of with `cut`. Outside a check, and for anything a listing does not
  hold, the system is asked as before. One check test went from 13 to 7.5 seconds; with the parallel runner the
  whole unit suite takes about two minutes instead of twenty.
- **setup.md explains the 403 at the end of every build.** The Gitea plugin posts a build status of its own with
  the read-only `forge-read` token, Forgejo refuses it, and the log ends with `Could not send notifications ...
  HTTP 403/Forbidden`. It is harmless (the pipeline posts the statuses that count with `forge-write`) and the plugin
  has no switch to stop it: [Forgejo details worth knowing](docs/setup.md#forgejo-details-worth-knowing).

## [0.9.2] - 2026-10-27

### Fixed

- **The unit suite passes on `main` again.** Cutover step 5.9 put the `forge` and `mirror` blocks in `main`'s
  `catalog.yaml` only, and twelve tests assumed this repository's catalog is a GitHub one: `test_catalog` said so,
  and `test_platform` added its scenarios' blocks on top of the real catalog. Every build of `main` failed its
  `platform` check, while every branch of `develop` passed. `develop` now has `main`'s catalog as it is, the
  platform tests start from a copy without the forge and mirror blocks, and the catalog test asserts Forgejo
  mirrored to the owner. (`main`'s catalog has each block twice, identically; that is left for a later change, so
  the next release merges cleanly.)

## [0.9.1] - 2026-10-26

### Fixed

- **Forgejo Actions is off.** Jenkins runs every check, but Forgejo's own Actions was on by default. It ran each
  repository's `.github/workflows` (Forgejo reads them when there is no `.forgejo/workflows`), and with no runner
  those jobs sat on every pull request as "Waiting for a runner with the following label: ubuntu-latest". The
  `forgejo` service of `platform/compose.yml` now sets `FORGEJO__actions__ENABLED=false`. It takes effect when the
  container is recreated (`ygg platform up`).
- **The `renovate` token's scopes.** The docs and `env/renovate.env.example` asked for `write:repository` and
  `write:issue` only, and Renovate stopped at once with `Authentication failure`: it asks Forgejo who it is when
  it starts, and Forgejo answers that only to a token with `read:user`. The token now has `read:user` and
  `read:organization` as well, as Renovate's docs for Forgejo ask, and setup.md 9.9 shows how to check a token's
  scopes without printing it. An installation with the old token makes a new one (setup.md 9.7) and puts it in
  `renovate.env`.
- **The Jenkins tokens' scopes.** setup.md 9.7 gave the `jenkins` token `write:repository` and the `jenkins-ci` token
  `read:repository` only. When Jenkins scans a repository, its Gitea plugin looks the owner up, which Forgejo answers
  only to a token with `read:user` and `read:organization`: every scan failed with `HTTP 403/Forbidden`, the jobs
  found no pull requests, and no pull request got a status from Jenkins. Both tokens now have both scopes, and 9.7
  shows how to check a token file without printing it. An installation with the old tokens makes new ones (9.7),
  writes them to `forge-write-token` and `forge-read-token`, restarts the controller and scans the jobs again.
- **The console check's APK is collected again.** `flutter pub get` links each plugin of the console's Windows build
  from the pub cache, outside the checkout (`console/windows/flutter/ephemeral/.plugin_symlinks`), and Jenkins refuses
  to collect anything from a checkout with a link that leads out of it: every build stopped at `a link in the
  checkout leads outside it (exit 3); nothing of console is collected` and was unstable, without the APK.
  `ci/console.sh` now deletes that generated folder when it ends, whether the check passed or failed;
  `scripts/test_ci_console.py` runs it with a fake Flutter to prove no such link is left.

## [0.9.0] - 2026-10-26

### Added

- **Templates for the sites and the libraries.** `templates/site-static/`, `site-hugo/`, `site-vite/`,
  `site-next-static/`, `site-next-server/` and `site-node-api/` (a Dockerfile, a `yggdrasil.yml` and a README each),
  `templates/library/` (`ci/test.sh`, `ci/coverage.sh`, `ci/pack.sh` and the docs site's Dockerfile, in place of a
  library's three workflows) and the stack files `templates/stacks/site.yml`, `site.proxy.yml`,
  `site-path.proxy.yml` and `site-apex-redirect.proxy.yml`. The images are pinned by version and digest
  (static-web-server, Node, Hugo, the .NET SDK); the sites listen on port 8080 as a non-root user and answer
  `/healthz`. `renovate/default.json` is the shared Renovate preset (nuget, npm, github-actions, docker, and the
  pinned images of `yggdrasil.yml`). `scripts/test_templates.py` tests them, and with `YGG_TEMPLATE_TESTS=1` builds
  each site around a sample and probes it. See [docs/catalog.md](docs/catalog.md#templates-for-repositories).
- **The guide to moving each repository**, [docs/moving-repositories.md](docs/moving-repositories.md): after the
  cutover, how to land the `feature/move-to-forgejo` branch prepared for each of 38 repositories (the table gives every
  head): push it to Forgejo, open the pull request, read Jenkins' results and merge with the owner's override, make
  the first release, publish or site deploy, switch GitHub Pages or Vercel off and drop `--skip-caa artur-rios.tech`,
  and tick [docs/migration-checklist.md](docs/migration-checklist.md). It lists what each repository needs from the
  owner (credentials, variables, tags, files to delete) and what the plan left undone. The checklist now records every
  prepared branch, maps github-readme-stats' workflows (its `test` job is the check `test`, the rest is upstream
  automation, dropped) and gives a reason for each of the 24 repositories without a branch.
  `scripts/repo_inventory.py` no longer lists a linked worktree (yggdrasil's own move branch) as a repository.

### Changed

- **yggdrasil lives at [git.techcraftsmen.net/artur-rios/yggdrasil](https://git.techcraftsmen.net/artur-rios/yggdrasil)**,
  with [github.com/artur-rios/yggdrasil](https://github.com/artur-rios/yggdrasil) as its read-only mirror, and its own
  CI and releases run on its pipeline: `yggdrasil.yml` declares the checks `status`, `console` and `platform`
  (`ci/*.sh`) and the release (the APK built by Jenkins, the Windows installer by `release-windows.yml` on the
  mirror). `ci.yml` keeps only `Console (Windows)`; `pipeline-live.yml` is the manual live suite; the Branch Policy
  workflow and Dependabot are gone (Renovate, `renovate.json`). A fork releases by pushing an annotated `v*` tag
  ([CONTRIBUTING.md](CONTRIBUTING.md#releasing)).

- **The cutover converts the old VPS in place.** [docs/cutover.md](docs/cutover.md) no longer rents a fresh apps
  machine: only the dev machine is new, and the old VPS becomes the apps machine during the window, as
  [docs/hosts.md](docs/hosts.md#9-upgrading-a-host-from-rootful-docker) section 9 describes. Section 3 now only reads
  the server days before (Ubuntu 24.04 or 26.04, the old VPS's being 26.04.1, which the dev machine, rented on
  26.04 too, proves before the window; `sudo` and the admin's key, the owners, the firewall, its PostgreSQL, cron
  jobs and timers, the room for the archives, the dumps and the backups until the clean-up) and keeps a record of it.
  **The databases move too**: heimdall-api's and fortuna-api's six databases leave the PostgreSQL on the host, which
  a rootless engine's containers cannot reach, for the stacks' `db` containers. Section 0 records their roles,
  owners and whole connection strings; the rehearsal archives one volume read-only from the rootful engine and
  restores one database, both on the dev machine's rootless engine; the window archives every volume and
  `/etc/yggdrasil` into `/srv/migration` with `volumes.sh`, dumps the roles and each database (`pg_dump`, a custom
  and a plain copy), checks every archive and dump against its checksum, copies them off the server, runs `ygg
  install --role apps`, hands the settings to `ygg`, changes the platform's settings in the variables store,
  restores the volumes into `ygg`'s engine, restores each database into a labelled `<application>-<environment>_db`
  volume through a temporary container with only its own roles and compares it with the plain dump, sets
  `DB_HOST=db` (and `Host=db` in a whole connection string) and a new `DB_SUPERUSER_PASSWORD`, turns Cloudflare's
  proxy on for the names that stay on the same address and sets up the backups, the six databases' dumps included.
  The rollback reverses each change of the install (rootful Docker unmasked, the `docker` group, the checkout at
  `v0.7.4`, the copy of `/etc/yggdrasil`, which brings `DB_HOST=host.docker.internal` back, the firewall rule),
  points `jenkins.` back in every rollback, carries production's writes back into the host's PostgreSQL or states
  that they are let go, and says what it cannot undo; the clean-up switches the host's PostgreSQL off, then deletes
  the archives, the rootful engine's data and the PostgreSQL packages once the apps machine has run a release. The
  window is now estimated at 4 to 8 hours. Since `main` stays `v0.8.0` until the window's 0.9.0, whose installer
  refuses 26.04, the dev machine (2.1) and the window (5.5) run `ygg install` from `<installer commit>`, the merge of
  this change on `develop`, then put the checkout back on `main`; homologation is deployed by hand at its recorded
  versions, as production is.
- **`backup.sh` backs up the variables store.** The new item `vars` takes a consistent copy of the machine's store and
  its key (`ygg vars backup`) into `vars.tar.gz` every night, and sends it to the other machine with the other
  backups: until now the store, with every application's secrets, had no copy off the machine.
  [docs/hosts.md](docs/hosts.md#7-backups-and-restore) says how to restore it, and the cutover's `backup.conf`
  (5.21) has it on both machines.
- **`backup.sh` dumps a stopped database too.** A `postgres <container> <user>` item whose container is stopped (an
  on-demand environment's database, most nights) failed, since `docker exec` cannot reach it. The container is now
  started for the dump, alone, waited for over TCP (60 seconds, `BACKUP_POSTGRES_START_WAIT`) and stopped again;
  one that never accepts connections fails the item and is stopped again too. Only an exited or created container
  is started: one that is restarting, paused or dead fails its item, since starting and stopping it would mark it
  stopped by hand and `restart: unless-stopped` would no longer bring it back. It stays up when another container
  of its Compose project runs by the end of the dump (a deploy or `ygg env start` meanwhile), and a run that is
  interrupted stops it on the way out.
- **The `dev` and `apps` roles install on Ubuntu 26.04 LTS too.** `ygg install --role dev|apps` accepted Ubuntu
  24.04 only; it now accepts 24.04 and 26.04 (`MACHINE_UBUNTU_VERSIONS` in `scripts/machine.sh`), and `ygg check`
  no longer warns about 26.04. Docker's apt repository was already picked by the release's codename.
- **heimdall-api and fortuna-api run their own PostgreSQL.** `stacks/heimdall-api.proxy.yml` and
  `stacks/fortuna-api.proxy.yml` add a `db` service (`postgres:18.6`, pinned by digest, its data in the volume
  `<application>-<environment>_db`) on the project's own network only, which the API waits for and reaches as
  `DB_HOST=db`; the image's superuser has the new variable `DB_SUPERUSER_PASSWORD`. A PostgreSQL on the host is no
  longer assumed: a rootless engine's containers cannot reach one. The env file examples say `DB_HOST=db`.
  **Upgrading:** an installation whose APIs use a PostgreSQL on the host moves each database first, into its
  `<application>-<environment>_db` volume ([docs/cutover.md](docs/cutover.md) 5.4 and 5.6a, or the new-environment
  steps of [the example's README](docs/examples/docker-desktop-and-vps/README.md#postgresql) with a restore in
  place of `new-deployment.sql`), then sets `DB_SUPERUSER_PASSWORD` (the one that volume was initialised with) and
  `DB_HOST=db`, then deploys. A deploy before the move fails on the missing `DB_SUPERUSER_PASSWORD`, and one with it
  set but no move initialises an empty database beside the old one, which the API then migrates and runs on.
- **The status API reports each application's real address.** `url` is now
  `https://<host><hostSuffix>.<domain><path>`: the application's own `domain` (else `YGGDRASIL_DOMAIN`) and `path`,
  and the bare domain for an `apex` site without a `host`, as `scripts/catalog.py public-host` and `public-path`
  form it for `deploy.sh`; the console links to it unchanged. A platform component stays
  `https://<host>.<YGGDRASIL_DOMAIN>`. The status API now refuses a catalog whose `domain`, `path` or `apex` is
  invalid, as `catalog.py validate` does. The contract stays version 2
  ([docs/status-api.md](docs/status-api.md)).
- **`scripts/deploy.sh` exports `GIT_COMMIT`**, the whole SHA of the checkout being deployed (it already exported
  `YGG_ENVIRONMENT`), so a stack passes both to a build in `build.args`, as the site templates' `site.yml` does:
  they replace Vercel's `VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV`.
- The Renovate documentation (docs/catalog.md, docs/setup.md 9.9,
  `scripts/renovate.sh`, `renovate/default.json`) says that Renovate targets `develop` and that a repository on `main`
  or `master` overrides `baseBranchPatterns` in its own `renovate.json`.
- **The branch policy passes for a repository under the `basic` rules.** A site deployed on pushes to `main`, a fork or
  a `repositories` entry with `rules: basic` has no release model, so its pull requests no longer report
  `branch-policy` failed (it demanded `release/x.y.z` into `main` of a repository that has no `develop`): the status
  says `basic: nothing to check`, and neither `scripts/branch-policy.sh` nor the file's own `branchPolicy` runs.
  `pipeline_config.py describe` adds `rules` to the entry (`catalog.py`'s new `repository_model`, which
  `scripts/rules.py` uses too).
- [docs/cutover.md](docs/cutover.md) has a new step, 5.8a, between 5.8 and 5.9: this plan and yggdrasil's own move
  branch go onto `develop`, yggdrasil is released (a `release/x.y.z` pull request into `main`, then an annotated
  `v<x.y.z>` tag) and the controller's `YGGDRASIL_LIBRARY_VERSION` is set to that tag, so that `main` has the catalog 5.9
  extends and the controller has this pipeline. That release is 0.9.0; 2.3 starts the controller on `v0.8.0` for the
  dry runs, and rollback step 6 pins the old controller's library to `v0.7.4` again.

### Fixed

- **The rootless engines start on Ubuntu 26.04.** 26.04's `apparmor` package ships its own profile for
  rootlesskit, `/etc/apparmor.d/rootlesskit`, and `ygg install` wrote a second one for the same program. AppArmor
  applied neither (`conflicting profile attachments`), and each engine's setup failed with `fork/exec
  /proc/self/exe: permission denied`. Where the system ships that profile, the install now writes none, and
  unloads and deletes the one an earlier install wrote; `ygg check` reports the system's profile.
- **A check's Testcontainers reaches the containers it starts.** A `docker: true` check was told
  `TESTCONTAINERS_HOST_OVERRIDE` = its container's default gateway (`172.17.0.1`), but the rootless engines' `slirp4netns`
  port driver opens a published port on the host only, so the gateway refused it, and `scripts/prove-machine.sh dev`
  failed its `testcontainers` proof. `scripts/agent.sh` now hands each build agent the host's own primary IPv4
  address (the source of its route to the internet, `host_published_address` in `scripts/docker-engine.sh`), or the
  `TESTCONTAINERS_HOST_OVERRIDE` of the user's `agent.env` when it sets one; `platform/build-agent.yml` passes it to the
  agent and `scripts/run-check.sh` to the check, falling back to the gateway when it is empty. The proof gives its
  client container the same value and names the address. **An agent started before this has no value: run
  `ygg agent up --user ci` and `ygg agent up --user release` again.**

## [0.8.0] - 2026-10-25

### Added

- **One interface to the git server, for Forgejo and GitHub.** `scripts/forge.sh` has the commands a release needs
  (set and read statuses, merge, tag, create, upload and publish releases, delete a branch) and does them on
  whichever server the catalog's `forge.type` names; `scripts/github.sh` now keeps only the GitHub Actions
  commands (`wait-checks` and the release-artifacts workflow) and calls it for the rest, with the same commands
  and arguments, and the same output on stdout, so the Jenkins pipeline is unchanged. What differs: its error
  lines start `forge:`, `delete-branch`'s "already deleted" note goes to stderr, and `merge-pr` and `release`
  fail when GitHub's answer has no merge commit or release address (before, they printed `null`). On Forgejo
  `merge-pr` waits while Forgejo says "try again later", and `delete-branch` leaves a protected branch alone
  (exit 3: every `release/x.y.z` is kept, the owner deletes them by hand). See
  [docs/setup.md](docs/setup.md#the-git-server-commands).
- **`scripts/rules.py`** applies the branching model to every repository of the catalog, on GitHub (rulesets) or
  Forgejo (settings, branch and tag protections, `yggdrasil.yml` and the `Jenkinsfile` as protected files on
  `develop`, so only the owner merges a change to what Jenkins runs (Renovate's image bumps there too), and Jenkins'
  webhook), with `--dry-run`. `github/rulesets.py` is now a shim that says it moved and runs it. Forgejo needs
  `FORGE_TOKEN`, and `FORGE_JENKINS_URL` with `FORGE_WEBHOOK_SECRET` for webhooks; the users it names must have
  write access to the repository, or Forgejo drops them (the script warns). On Forgejo it deletes nothing, and on
  GitHub only a ruleset named `default`, as `github/rulesets.py` did; a protection the model no longer has is
  noted. [docs/setup.md](docs/setup.md#12-apply-the-branch-rules) step 12 lists what each
  server gets. On Forgejo, the user who creates the version tags can also move and delete them: an accepted gap,
  which the `tag-check` job detects (below). With a `mirror`, every repository's GitHub copy gets the ruleset
  **`mirror tags`** (update and deletion, no bypass, administrators included; nothing on the copy's branches or
  settings), so the copies keep the tags as released and are what a moved or deleted Forgejo tag is repaired
  from; that needs a GitHub token with administration of the copies, and without one the Forgejo rules are still
  applied and the run exits 1. A copy GitHub refuses (one the push mirror has not made yet, a private copy on a
  free plan) is reported and skipped: the other repositories are still applied and the run exits 1 at the end. A
  mirror push that would move or delete a version tag is refused, and Forgejo shows the mirror sync failing:
  intended.
- **Catalog fields for the git server:** `forge` (`type` `forgejo` or `github`, `url`, `jenkinsUser`, `developers`;
  a catalog without it is GitHub), `mirror` (Forgejo to GitHub), `repositories` (libraries, CI-only repositories and
  yggdrasil itself, with `release`, `tags`, `rules`, `checks`, `actionsChecks`, `credentials`) and, on applications,
  `actionsChecks` (GitHub Actions jobs to require on `develop`), `domain`, `path` and `apex` (for the site routing
  to come). `catalog.py` reads them with `forge`, `mirror`, `repositories` and `repository-entry`.
  [docs/catalog.md](docs/catalog.md).
- `scripts/test_forgejo_live.py` runs forge.sh and rules.py against the pinned Forgejo in a container
  (`YGG_FORGEJO_TESTS=1`; CI runs it) and proves the protections hold.
- **Two machine roles.** `ygg check` and `ygg install` take `--role dev|apps|workstation`. `dev` and `apps` set up an
  Ubuntu 24.04 server: the service users (`ygg` on both; `ci`, `release`, `renovate` and `dev` on the dev machine),
  a rootless Docker engine for each, the system settings they need, SSH hardening that is verified against what
  sshd really runs (`00-yggdrasil.conf`), the firewall, unattended upgrades and the nightly backup timer. The apps
  machine's firewall opens 80 and 443 to Cloudflare's address ranges only, and `ygg check` compares it with
  Cloudflare's current list. `workstation`, the default on a machine with no role file, keeps the earlier behavior.
  The firewall fails closed (SSH in, incoming denied by default, even when Cloudflare's list can't be fetched), and
  `check` reports any rule that lets everyone in that `install` didn't make. On the dev machine `ci`, `release`,
  `renovate` and `dev` cannot bind ports below 1024, and `check` proves that no service user reaches `ygg`'s engine or
  secrets (nor `ci` the release agent's). `install` asks for the sudo password first, runs only from
  `/opt/yggdrasil`, and writes the role file before its first change. `check` shows whether backups work:
  `backup.conf`, the last run's result and the newest day's age. dns.md and hosts.md add a `CAA` record per zone
  (Let's Encrypt, DNS-01 only).
  [docs/hosts.md](docs/hosts.md) (new) explains the two machines, their users, every file the installer writes, the
  credentials and the backups; [docs/setup.md](docs/setup.md) step 8 explains each part of the install and the
  commands to do it by hand.
- **Forgejo** as a platform service, on its own profile (`forge`): `codeberg.org/forgejo/forgejo:16.0.6-rootless`,
  pinned by digest, at `git.<DOMAIN>`, git over SSH on port `FORGEJO_SSH_PORT` (2222), SQLite in the
  `yggdrasil_forgejo-data` volume, registration closed and sign-in required to view anything. The first account is
  made from the command line (setup.md 9.6).
- **Build agents** for the `ci` and `release` users: `platform/build-agent.yml` and `ygg agent up|down|ps|logs --user
  ci|release`, each on its user's own engine, configured by `/etc/yggdrasil-<user>/agent.env`
  (`env/agent.env.example`).
- **Backups**: `ygg backup run|list` and a systemd user timer (03:30) dump Forgejo, volumes and PostgreSQL databases
  into `~/backups` and send them to the other machine over SSH with a key restricted to `rrsync`. Configured in
  `backup.conf` (`env/backup.conf.example`); `ygg install` does not write it. Each item's newest good dump is kept
  even beyond `BACKUP_KEEP`, and `ygg backup list` says whether the newest day is complete. Restore steps are in
  [docs/hosts.md](docs/hosts.md#7-backups-and-restore).
- **Several domains on one machine** (`EXTRA_DOMAINS` in `platform.env`, each with its own wildcard certificate) and
  **Cloudflare in front** (`CLOUDFLARE_PROXIED`): Traefik trusts the forwarded client address from Cloudflare's ranges
  only, from `<secrets>/cloudflare-ips`, which `ygg install --role apps` writes. Traefik's static configuration is now a
  file rendered by `scripts/traefik_config.py` into `<secrets>/traefik.yml` (kept there, so Traefik starts again
  after a reboot); the settings are the same. Only `platform.sh up` replaces it (the other commands render a
  throwaway copy), and `platform.sh` now refuses to run as root: a root-owned `traefik.yml` is one the rootless
  Traefik can't read at its next start. Run it as the platform's user (`ygg`).
- **`PLATFORM_HOST_SUFFIX`** (optional, empty by default) in `platform.env`: put after `traefik` and `grafana` in
  the names of the Traefik dashboard and Grafana (and Grafana's root URL). The dev machine sets `-dev`, so it serves
  `traefik-dev.<DOMAIN>` and `grafana-dev.<DOMAIN>` instead of claiming the apps machine's `traefik.` and `grafana.`
  under the same domain; they need DNS-only records to the dev machine. Unset, nothing changes.
- A machine without the `apps` profile (the dev machine) has Prometheus read a written-out list of the platform's own
  targets (`platform/prometheus/prometheus-static.yml`).
- **The pipeline in Jenkins.** A repository describes its checks and releases in a `yggdrasil.yml` (data: images,
  commands, globs and the names of credentials; reference in [docs/catalog.md](docs/catalog.md#repository-pipeline-file)),
  and Jenkins runs them on two build agents of their own: `ci` (4 executors) for the branch policy and the checks,
  each reported as a commit status (`pending`, then `success` or `failure`) and reused for a commit only when
  `forge.jenkinsUser` already set it, and `release` (1 executor) for release files. `deploy.inputs` hands a check's
  archive to an image build. A push deploys only if its checks passed, and the commit they saw. A repository without
  the file deploys and releases as before. How it all fits, what happens on each push, pull request and tag, and the
  accepted risks: [docs/setup.md, The pipeline](docs/setup.md#the-pipeline).
- **Releases from Jenkins**: after the merge and the tag, `release.build` builds the files on the release agent
  (with only the credentials the catalog entry's new `releaseCredentials` allows: never its checks' `credentials`,
  since a check runs any branch's code),
  `release.windows` has GitHub Actions build the Windows ones, `release.validate` checks them (pass or fail; it cannot
  change them), and `scripts/publish-release.sh` uploads and checks them on a draft release on Forgejo and, with
  a `mirror`, on GitHub, then publishes Forgejo's and GitHub's last. The notes are the version's `CHANGELOG.md`
  section, else the titles of the merged pull requests (`scripts/release_notes.py`). If the GitHub Actions
  checks (`actionsChecks`: Windows, macOS) don't finish, Jenkins asks **Deploy without GitHub Actions checks**; after
  it the release goes out on Forgejo only (`GitHub copy pending`, the build unstable). The jobs **`release-resume`**
  (finishes a release from its tag: after a failure past the merge, to make the GitHub copy, for a tag never built)
  and **`actions-release`** (attaches Windows files later) are started by hand with `REPOSITORY` and `TAG`;
  `release-resume` also takes `COMMIT`, for a release merged but never tagged: it tags the merge commit (only when
  it is that release's merge on `main`'s first-parent line), since only Jenkins may create an application's `v*`
  tags.
- **The `tag-check` job**: every day (cron `H 4 * * *`) and by hand, on the `ci` agent with the read credential
  only, it checks that every published release of every catalog entry that releases still has its tag on the
  commit it was released from (`forge.sh release-tags`, new: each published release's tag and target, and
  `tag-sha`). A tag that moved or was deleted fails the build, named `MOVED` or `DELETED` at the end of the log,
  the count in the build's description; a release that names a branch, not a commit, is logged as an unknown
  target. A deleted tag's release becomes a draft (Forgejo, GitHub), which the read credential cannot see, so
  each run archives the releases it knows of (`release-tags.tsv`) and the next checks those too. Its optional
  `FORGET` parameter (`<repository> <tag>` a line, checked before anything runs) drops releases deleted on
  purpose from that record. [docs/setup.md](docs/setup.md#version-tags-tag-check-and-repairs) says how to read it and how to repair a
  tag from its GitHub copy. Its script, `jenkins/tag-check.groovy`, is mounted by `platform/compose.yml` like the
  other jobs'.
- **Owner-tagged releases and NuGet**: a `repositories` entry with `release: owner-tag` is released by the owner's
  tag on `main` (tag with `git tag -a`: Jenkins builds only tags at most a day old and dates a lightweight tag by its
  commit). `publish.nuget` packs a library's packages with no credential, then `scripts/nuget-push.sh` pushes them
  (`--skip-duplicate`) with the Jenkins credential `nuget-api-key`, bound only there (`NUGET_API_KEY_FILE`).
- **Renovate**: `scripts/renovate.sh run`, a daily systemd user timer of the `renovate` user (05:00), in its own
  rootless engine, against the catalog's repositories only, on Forgejo. Its branches are `renovate/*`; a repository with
  no Renovate configuration of its own is skipped. Settings in `/etc/yggdrasil-renovate/renovate.env`
  (`env/renovate.env.example`). Run it once by hand first ([docs/setup.md, 9.9](docs/setup.md#99-renovate)).
- **Settings for the pipeline** in `platform.env`: `YGGDRASIL_LIBRARY_VERSION` (required with the `jenkins` profile: a
  yggdrasil release tag of your fork, the version of `jenkins/library/` every pipeline loads), `CI_AGENT_WORKDIR` and
  `RELEASE_AGENT_WORKDIR` (required with it: each the `YGG_AGENT_WORKDIR` of that agent's `agent.env`),
  `FORGE_WRITE_TOKEN_FILE`, `FORGE_READ_TOKEN_FILE` and `FORGE_WEBHOOK_SECRET_FILE` (required on Forgejo),
  `GITHUB_READ_APP_ID` and `GITHUB_READ_APP_KEY_FILE` (optional, GitHub: a read-only App for the CI agent) and
  `NUGET_API_KEY_FILE` (optional). `scripts/platform.sh` chooses the Configuration as Code files the controller
  reads (`casc.yaml`, `casc.forgejo.yaml` or `casc.github.yaml`, and `casc.github-app.yaml`, `casc.github-read.yaml`,
  `casc.nuget.yaml` where they apply) from the catalog's `forge` and `mirror` and these settings.
  [docs/setup.md, 9.2 to 9.9](docs/setup.md#92-the-env-files-first-pass).
- **Sites**: `deploy.sh` exports `YGG_PUBLIC_HOST` and `YGG_PUBLIC_PATH` from the catalog's `host`, `domain`, `path`
  and `apex` (`catalog.py public-host` and `public-path`); the deploy agent gets the platform's `DOMAIN` as `YGG_DOMAIN`.
  An `apex` site without a `host` is on the bare domain in every environment, so deploy it to one environment.
- `scripts/test_jenkins_live.py` runs the pipeline against a real Jenkins controller, its agents and Forgejo
  (`YGG_JENKINS_TESTS=1`; CI job `pipeline-live`, by hand only). The GitHub paths and rootless Docker are not proven by it.
- **The cutover runbook**, [docs/cutover.md](docs/cutover.md): the move from one rootful VPS that deploys from GitHub
  to the two rootless machines, with Forgejo as the git server and GitHub as a push-mirrored copy. In order, each
  step with its commands, what success looks like and how to back out: Cloudflare (both zones, the token, SSL Full
  (strict) and the cache rule by hand, the records), the dev and apps machines, a rehearsal (one import, one mirror,
  the volumes compared, one database volume carried and started), the maintenance window (the volumes, the DNS switch, the repositories: `inventory`,
  `import`, `prepare-github`, `mirrors`, `collaborators`, `rules.py`, `verify` as the gate), what can and cannot be
  rolled back at each point, the clean-up, and the checks left to the owner. It drives the tools made for the move:
  `scripts/migrate.py` (the repositories, idempotent, with `--dry-run`; `mirrors` refuses a repository whose first
  `--mirror` push would delete or move a branch or tag only GitHub has, unless `--allow-prune`),
  `scripts/cloudflare_dns.py` (the records of both zones), `scripts/volumes.sh` (Docker volumes carried from a rootful engine to a rootless one, checked by size
  and SHA-256) and `scripts/prove-machine.sh` (the rootless setup proved on a real machine). The Forgejo token for
  the move needs `read:user` besides `write:repository`: `migrate.py collaborators` looks the bot accounts up, and
  Forgejo refuses that to a token without it. [docs/hosts.md](docs/hosts.md)'s restore and upgrade steps now run the
  pinned `busybox` image that `backup.sh` and `volumes.sh` use, not whatever `busybox` is current.

### Changed

- **The rules are applied by `python3 scripts/rules.py`** (replaces `python3 github/rulesets.py`, which still
  works and runs it). On a GitHub installation it changes what the rulesets require: on `develop` and `main`,
  `branch-policy` and the `checks` stay required but are bound to no app (any status of that name satisfies
  them; until now only GitHub Actions' could), because Jenkins is to report them (plan 3); the new
  `actionsChecks` (applications and repositories) are required, bound to GitHub Actions, on `develop` only, so a
  job moved there from `checks` is no longer required on `main`. **An existing GitHub installation should run the
  rules for a repository only once it has adopted `yggdrasil.yml`**, so that Jenkins reports those contexts: until
  then its rulesets stay as they are. GitHub cannot restrict who may merge one file, so the `Jenkinsfile`
  protection is Forgejo's only.
  [docs/setup.md](docs/setup.md#an-existing-github-installation).
- **Breaking, GitHub: nobody can move or delete a version tag any more, the owner included.** The `release tags`
  ruleset loses its administrator bypass. An owner-tagged repository's creation rule moves out of it into a new
  ruleset, `release tag creation`, which administrators bypass, so the owner still creates those tags. To fix a
  wrong tag, disable the ruleset first, then run `rules.py` again
  ([docs/setup.md](docs/setup.md#version-tags-tag-check-and-repairs)).
- **Breaking: the platform runs on rootless Docker only.** `platform.sh`, `agent.sh` and `backup.sh` use the Docker
  engine of the user who runs them (the socket from `DOCKER_HOST`, else `/run/user/<uid>/docker.sock`) and refuse a
  rootful one. Run them as `ygg`. Root inside a container is that user on the host, so the socket proxies and the
  agents run as container root. The `ufw` rules now apply to published ports, which rootful Docker bypassed.
- **Breaking: the `apps` profile.** The status API, its socket proxy and the console moved from the base platform to
  profile `apps`. A host that runs environments must have `apps` in `COMPOSE_PROFILES` (`apps,agent`, `apps,jenkins`,
  ...); `ENVIRONMENTS` without it is refused with that instruction.
- **Breaking: the secrets directories belong to the service users.** `/etc/yggdrasil` is owned by `ygg`, mode `750`,
  files `600`, with no `docker` group; other service users have `/etc/yggdrasil-<user>`. `ygg install` never touches a
  file that is already there, so a host moved from the earlier layout must hand its files to `ygg` (see Upgrading).
- The Jenkins controller's GitHub App key is mounted into a container whose user is not `ygg`: keep
  `/etc/yggdrasil/github-app.pem` at mode `644` (setup.md 9.1), not owned by uid 1000.
- Two example env files changed: `env/agent.env.example` leaves `JENKINS_URL`, `JENKINS_AGENT_NAME` and
  `YGG_AGENT_WORKDIR` empty on purpose (`https://jenkins.<DOMAIN>/`; `ci` and `/home/ci/agent`, or `release` and
  `/home/release/agent`); edit the copies with `sudoedit` as the admin, and `env/backup.conf.example` names the
  Jenkins volume `yggdrasil_jenkins`. `env/platform.env.example` gives the first-account command that finds Forgejo by
  its labels.
- [docs/setup.md](docs/setup.md) is organized around the two machines: step 0 plans the roles (one machine with role
  `apps` running the `jenkins` profile too is still supported), steps 6 and 8 to 11 and Troubleshooting follow the
  new layout, and [docs/dns.md](docs/dns.md) covers two zones, the proxy and the token's scope. [docs/cli.md](docs/cli.md)
  documents `check`/`install --role`, `agent` and `backup`.
- Chimera production releases now require an explicit Jenkins approval. After a successful deploy,
  Jenkins creates an immutable tag and private draft, dispatches Chimera's release-artifact
  workflow, and waits for its validated artifacts to publish before completing cleanup. Existing
  installations must grant the Jenkins GitHub App **Actions: Read and write** before the first such
  release. Release flows are serialized per application across multibranch jobs.
- The example's PostgreSQL steps create each heimdall-api database with heimdall-api's
  `scripts/sql/new-deployment.sql`: an owner role that migrates and a runtime login that owns nothing, with a
  `pg_hba.conf` line for each, and `HEIMDALL_MIGRATIONS_CONNECTIONSTRING` in the store. A database the API's own
  login owns is moved with heimdall-api's `separate-owner.sql` runbook.
- **Breaking: Jenkins never reads a repository's `Jenkinsfile`.** Every job runs `jenkins/Jenkinsfile.default`, kept in
  Jenkins, which calls the pinned library; a branch pushed by someone who is not the owner would otherwise run its own
  pipeline code with every credential in reach. The library is loaded at the tag `YGGDRASIL_LIBRARY_VERSION`, and
  the `scripts/` it calls are cloned at that same tag; only the owner's data, `catalog.yaml` and `stacks/`, still
  comes from `main`. A `Jenkinsfile` in a repository is ignored (and the Forgejo
  protection of it on `develop` protects nothing about the pipeline now); `templates/application/` has a
  `yggdrasil.yml` to start from in place of its `Jenkinsfile`. Every build now starts with a step on the
  `ci` agent that reads the commit, even for a repository without a `yggdrasil.yml`: an installation needs the CI
  agent, `CI_AGENT_WORKDIR`/`RELEASE_AGENT_WORKDIR` and `YGGDRASIL_LIBRARY_VERSION` before its next build.
- The branch policy accepts `renovate/...` into `develop` in place of `dependabot/...` (`scripts/branch-policy.sh`, run
  by Jenkins; this repository's own workflow still accepts `dependabot/...`).
- `scripts/github.sh` gains `actions-status`, `wait-ref`, `dispatch`, `run-status` and `download-artifacts`, the
  GitHub Actions commands the pipeline uses, and `scripts/forge.sh` gains `find-release`, `release-files` and
  `tag-sha`. `catalog.py` gains `public-host` and `public-path`.
- `platform/build-agent.yml` passes `YGG_DOCKER_SOCKET` (the host path of the user's socket) to the agent, and
  `platform/compose.yml` passes `YGG_DOMAIN` to the deploy agent: agents started by an earlier release need
  `ygg agent up --user ci` (and `release`), and `scripts/platform.sh up` on each apps machine, again.
- [docs/setup.md](docs/setup.md) adds 9.7 (the Forgejo accounts and tokens), 9.8 (the CI and release agents), 9.9
  (Renovate) and a reference section, [The pipeline](docs/setup.md#the-pipeline); the credentials table of
  [docs/hosts.md](docs/hosts.md#6-credentials) lists `forge-read`, `forge-write`, `forge-webhook`, `github-app`,
  `github-read` and `nuget-api-key` and where each lives.

### Removed

- `DOCKER_GID` and the `docker` group setup of the platform: nothing reads them. Delete `DOCKER_GID` from
  `platform.env`.
- The machine's own Docker engine on a `dev` or `apps` machine: its units are disabled and masked, and the `docker`
  group is emptied.

### Upgrading an existing GitHub installation to the pipeline

**First, before this reaches your fork's `main`:** pin the running controller's library to your current release
tag (**Manage Jenkins → System → Global Trusted Pipeline Libraries → `yggdrasil` → Default version**, e.g.
`v0.7.4`), and keep the controller host's checkout at that release until you upgrade. A controller from before
this release loads the library at `main`, so it would run the new library at once, before `scripts/platform.sh up`
(builds would wait for a `ci` agent that does not exist; the HTTP Request plugin is missing). The old library
keeps working pinned: it still clones `scripts/` from `main`, and the `github.sh` commands it calls (`wait-checks`,
`set-status`, `merge-pr`, `release`, `publish-release`, `delete-branch`) and `deploy.sh <environment> <application>
<checkout> <version>` take the same arguments as before. **Then, before relying on it on GitHub:** run a scratch
GitHub repository with `forge.type: github` through a pull request, its checks and a release, end to end; the
GitHub paths have been tested against fakes only ([docs/setup.md](docs/setup.md#an-existing-github-installation)).

Nothing in a repository has to change for it to keep deploying and releasing. On the dev (controller) host: set
`YGGDRASIL_LIBRARY_VERSION` (a release tag of your fork that has this pipeline), `CI_AGENT_WORKDIR` and
`RELEASE_AGENT_WORKDIR` in `platform.env`, fill in the `ci` and `release` agents' `agent.env` and start them
(`ygg agent up --user ci`, `--user release`), `scripts/platform.sh up`. Then adopt `yggdrasil.yml` one repository at
a time: move each Actions job's steps into `ci/` scripts, list them as `checks` in `yggdrasil.yml`, name them in the
catalog's `checks` and keep the Windows and macOS jobs in `actionsChecks`, and, now that Jenkins reports them,
run `scripts/rules.py` for that repository. Step by step: [docs/setup.md](docs/setup.md#an-existing-github-installation).

**Version tags.** The first `rules.py` run for a repository removes the administrator bypass from its `release
tags` ruleset (updated in place, by name): from then on you can no longer move or delete a version tag on GitHub
without disabling the ruleset first. For an owner-tagged repository it also creates the ruleset `release tag
creation` (only administrators create its tags), and `release tags` loses the creation rule it had.

### Upgrading: start tag-check's record

Right after `scripts/platform.sh up` has created the `tag-check` job, run it once by hand (**Build with
Parameters**, `FORGET` empty). It remembers only the releases it has seen published, and a deleted tag's release
turns into a draft it cannot see, so the sooner its first run, the fewer deletions can slip by
([docs/setup.md](docs/setup.md#version-tags-tag-check-and-repairs)).

### Upgrading a host from rootful Docker

For a VPS set up with the earlier guide, on the same server: stop the platform and applications with the old checkout,
back up the volumes, pull and run `ygg install --role apps`, hand `/etc/yggdrasil` to `ygg`, restore the volumes into
`ygg`'s engine, add `apps` to `COMPOSE_PROFILES`, start. Step by step:
[docs/hosts.md](docs/hosts.md#9-upgrading-a-host-from-rootful-docker). Plan for downtime.

A last-good copy saved by an earlier release lacks `traefik.yml` (Traefik's configuration is now a rendered file),
so `platform.sh config --last-good` and `logs --last-good` stop on it: run `platform.sh up` once from the
variables store after upgrading, which saves a complete copy.

## [0.7.5] - 2026-10-20

### Changed

- The examples and docs give heimdall-api an ES256 signing key and fortuna-api Heimdall's key set URL instead of a
  shared HS256 secret, and the rotation recipe rotates the signing key.

## [0.7.4] - 2026-10-20

### Added

- Chimera is registered as a web application across local, development,
  homologation and production, with hardened proxy and loopback-port stacks.
- Heimdall's credential routes are limited per source at the edge. A third router, `heimdall-api-<environment>-auth`,
  takes requests to `/api/auth/` (any case) on the API's host and on the web UI's same-origin `/api/`, with any
  method but `GET`, `HEAD` and `OPTIONS`, and its `heimdall-api-<environment>-auth-limit` middleware admits 10 a
  minute with a burst of 10 per source, an IPv6 caller counted by its /56. A refused request gets Traefik's own
  `429` and never reaches Heimdall. The profile and admin pages' `GET`s under `/api/auth/` stay on the other
  routers. The rule excludes the safe methods rather than naming `POST` because Traefik matches a method
  case-sensitively and ASP.NET does not: under ``Method(`POST`)``, a lowercase `post` would reach Heimdall's
  `[HttpPost]` actions unlimited. Applies to an environment once heimdall-api is redeployed there; see
  *Upgrading* below first. [docs/dns.md](docs/dns.md) (A7) notes that the limit, like Heimdall's own, counts the
  address Traefik sees: with the Cloudflare proxy on, or for IPv6 clients, many users share one bucket.

### Changed

- The heimdall-ui and fortuna-ui routers use the new `secure-headers-ui` middleware: the same headers as
  `secure-headers` without `Referrer-Policy`, so each UI's own Referrer-Policy reaches the browser, including
  heimdall-ui's `no-referrer` on the pages whose link carries a token. Pull this release onto the host before
  deploying a UI: a router that names a missing `secure-headers-ui@file` is disabled.
- The example fortuna-api env files point `FORTUNA_HEIMDALL_BASE_URL` at heimdall-api's alias on the edge network,
  `http://heimdall-api.<environment>:8080/`, instead of its public host, so Fortuna's users never share the
  edge limit on Heimdall's credential routes. Needs fortuna-api 3.1.0 or later, which accepts a plain-HTTP URL to a
  private address; an earlier version refuses it at start-up.
- The example fortuna-api env files list `FORTUNA_HEIMDALL_JWKS_URL` (heimdall-api's alias again) and an empty
  `FORTUNA_LOCAL_TOKEN_SECRET`, which fortuna-api 3.0.0 or later requires at start-up.

### Upgrading from 0.7.3 to 0.7.4

Order matters, per environment:

1. Pull this release onto the host (`git -C /opt/yggdrasil pull --ff-only`) before deploying any web UI. The UI
   routers name `secure-headers-ui@file`, which only this release's `middlewares.yml` defines, and Traefik
   disables a router whose middleware is missing.
2. Before redeploying heimdall-api with this release's labels, move Fortuna to fortuna-api 3.1.0 or later and point
   its `FORTUNA_HEIMDALL_BASE_URL` at `http://heimdall-api.<environment>:8080/`. A fortuna-api that still calls
   Heimdall's public host goes through Traefik from one source, so every sign-in it relays would share one edge
   bucket of 10 a minute.
3. Redeploy heimdall-api, heimdall-ui and fortuna-ui so that their containers carry the new labels. Labels only
   change when a container is recreated.

## [0.7.3] - 2026-10-13

### Fixed

- The Yggdrasil card is back to one chip per environment, like the other systems, and each chip now shows that
  environment's real state: *Stopped* in an on-demand environment that is stopped, the platform's own status
  elsewhere. 0.7.1 had replaced the chips with a single *All environments* one. The status API reports the platform
  system as `stopped` in a stopped environment; expanded, each component still shows its own status.

## [0.7.2] - 2026-10-13

### Changed

- The license now grants what the setup guide asks for: you may fork or copy the repository, change it and run
  it on hosts you control to deploy your own applications. Redistributing it or offering it as a service is still
  not allowed. See [LICENSE](LICENSE).

## [0.7.1] - 2026-10-13

### Fixed

- The console showed the Yggdrasil card as `Up` in every environment, so a stopped on-demand environment looked
  running while `ygg env stop` found nothing running in it. Platform components serve the whole host: the card
  now has one *All environments* chip and, expanded, lists each component once.

## [0.7.0] - 2026-10-09

### Added

- **`ygg`, the host's command line app** ([docs/cli.md](docs/cli.md)). Every command of the host scripts
  (`host.sh`, `vars.py`, `platform.sh`, `deploy.sh`, `catalog.py`'s read commands) is a `ygg` command and is in
  an arrow-key menu: forms built from the commands' own options, pick lists of applications, environments,
  scopes and keys (or type a value), the command line shown as you fill a form, confirmation before deleting
  or stopping, help on `?`. Without a terminal, or in one too small or that this machine has no terminfo for,
  the menu is numbered lines.
- **An application's variables and secrets in the menu**: Applications › `<app>` › `<environment>` lists them
  with the scope each comes from; select one to show its value (drawn, never printed), change it (hidden for a
  secret, in the scope you choose), mark it secret, remove it, or roll back one of its changes.
- `ygg self-install` (also offered by `ygg install`) links `/usr/local/bin/ygg` and installs bash tab
  completion (`ygg completion bash`) of commands, options, applications, environments, scopes and keys.
- `ygg deploy <environment> <app> [<app-dir>] [<version>] [--leave-running]`, `ygg platform ...`,
  `ygg catalog ...`, `ygg version`, `ygg help [<command>]`. Without a terminal, `ygg deploy` does not ask and
  does not leave a stopped on-demand application running unless `--leave-running` is given.
- The questions of the host operations (`add`, `install`, `deploy`, `status`, `env`, `config` without a store) are
  arrow-key prompts in a terminal, drawn below what the operation printed, which stays in view; a long question is
  wrapped, and each leaves one line with its answer. Esc goes back where there is a **Back** option, stops the
  operation on any other choice or text question, and answers no to a yes/no question; Ctrl-C stops the operation
  (exit 130). `YGG_PLAIN` (any value), `TERM=dumb` or a prompt that can't run fall back to plain numbered prompts.

### Changed

- `vars.py --help` (and `scripts/ygg.sh vars` with no command) explains every command, the scopes, examples, exit
  codes and environment variables; `vars <command> --help` explains each command and option.
- [docs/variables.md](docs/variables.md): exit status and environment variables, a recipes section (see what an
  application gets, add a hidden secret, rotate a signing secret, share a value, references, undo a change), the
  `render` commands, and the exact messages for a missing, damaged or foreign key. [docs/cli.md](docs/cli.md) gains
  a command summary of `ygg.sh vars`.
- `scripts/ygg.sh` now runs `ygg` (`scripts/ygg.py`); its operations moved to `scripts/host.sh`. Every existing
  `scripts/ygg.sh` command line works as before, except that an unknown command is now a usage error (exit 2,
  was 1) and `scripts/ygg.sh help` prints `ygg help`.
- `scripts/ygg.sh config <app> <environment>` (and `ygg config`), in a terminal on a host with a variables store,
  opens the menu's variables screen of that application instead of the numbered `config` screen. An application or
  environment that isn't on this host is refused with the same message as before (exit 1); in a pipe, or with
  `YGG_PLAIN`, and on a host without a store, `config` is the numbered screen as before.
- [docs/cli.md](docs/cli.md) is rewritten around `ygg`, its menu, the variables screens and tab completion; the
  other docs use `ygg` in their commands.

### Upgrading from 0.6 to 0.7

Nothing breaks and nothing is required, but each host that runs the helper gains from one step:

1. Update the checkout (`/opt/yggdrasil`) and run `scripts/ygg.sh self-install` once. It links
   `/usr/local/bin/ygg` to the checkout and installs bash tab completion for new shells. `ygg version` shows
   what is installed; `ygg check` warns when `ygg` is missing or points at another checkout.
2. Scripts that call `scripts/ygg.sh` keep working. If one tests for exit status 1 on an unknown command, it
   gets 2 now, and if one reads `scripts/ygg.sh help`, it gets `ygg help`'s text.

No container is touched and no variable or env file changes.

## [0.6.1] - 2026-10-08

### Fixed

- `vars import` names the line and the key of every value it refuses (a single quote, surrounding whitespace, a bad
  name, a reference outside an application scope), not only of a `$` it would change.
- `vars edit` keeps the text of a refused edit in a uniquely named file: two refused edits of one scope in the same
  second no longer lose the second one's text.
- `deploy.sh` says `(previous: <image>, running)` again, not `running` followed by the container ids.

## [0.6.0] - 2026-10-08

### Added

- **The variables store** ([docs/variables.md](docs/variables.md)): an encrypted SQLite database per machine
  (`vars.db` and `vars.key` in the secrets directory) for the platform's settings and every application's variables, managed by
  `scripts/vars.py` and `scripts/ygg.sh vars`. Values are defined in layers (`@<environment>`, `<application>`,
  `<application>@<environment>`), one application can take another's value with `${ref:<application>:<KEY>}`, every value is
  encrypted, secrets are masked, and every change is recorded in a history that `vars rollback` can undo. Also
  `vars import` (including `--all`, with move-up offers for shared values; values are stored as Compose read them, `$$` as
  `$`, and a value Compose would have interpolated is refused), `vars export`, `vars edit`,
  `vars backup` and `vars check` (strict for what the machine runs; `--platform` and `--usable` for the scripts
  that need less). It is opt-in.
- `scripts/platform.sh up --last-good`: starts the platform from the copy of its settings saved by the last successful
  `up` from the store, without opening the store (for a lost or wrong key). `down`, `ps`, `logs` and `config` take
  `--last-good` too.
- `ygg.sh` menu entry **Variables and secrets**.

### Changed

- `scripts/deploy.sh`, `scripts/platform.sh` and `scripts/ygg.sh` read the variables store when `vars.db` exists in the
  secrets directory, and the env files as before when it doesn't (with a notice). The store is checked before every deploy
  (that application's values) and platform start (the platform's values); `ygg.sh` checks only that it is usable. As a missing env file
  did, an application with no variables in the store for that environment stops its deploy.
- The deploy lock moved from the application's env file to `<secrets>/locks/<application>-<environment>.lock`. `platform.sh up`
  creates the directory (group `docker`, mode `2770`).
- `scripts/ygg.sh check` and `install` list and install `python3-cryptography` with the other packages.
- The Jenkins agent image installs `python3-cryptography`, and `platform/compose.yml` mounts `<secrets>/locks` read-write
  next to the read-only secrets directory.

### Upgrading from 0.5 to 0.6

Optional: nothing changes until you run `scripts/ygg.sh vars init`; env files keep working.

1. On every host, `git pull`, then `scripts/platform.sh up` once. The Jenkins agent gets its new package and the `locks`
   mount, after which deploys work with either source, files or the store. The scripts need `python3-cryptography` on the host
   only to use the store (`apt install python3-cryptography`).
2. To move a host to the store, as in [docs/variables.md](docs/variables.md#moving-to-the-store): copy `/etc/yggdrasil`,
   `scripts/ygg.sh vars init` (store the key it prints), `scripts/ygg.sh vars import --all` (answer the move-up offers),
   `scripts/ygg.sh vars check`, `scripts/platform.sh up`, redeploy one application, and delete the `*.env.imported` files
   once everything runs.
3. Schedule `scripts/ygg.sh vars backup <dir>`
   ([backup and recovery](docs/variables.md#backup-and-recovery)).

## [0.5.1] - 2026-10-08

### Fixed

- `scripts/deploy.sh`: a deploy that fails in an `onDemand` environment that wasn't running is stopped again, also
  when there was nothing to roll back to. A first deploy that never became healthy was left restarting.

## [0.5.0] - 2026-10-08

### Added

- **Several environments on one host.** One Docker engine can run any number of environments: the default catalog
  runs development, homologation and production on one VPS, plus `local` on a developer's Docker Desktop ([docs/examples/docker-desktop-and-vps](docs/examples/docker-desktop-and-vps/README.md)). Every name an
  environment's containers share is qualified with the environment (see Changed), and one platform stack, one Jenkins
  agent, one `DOMAIN` and one `*.DOMAIN` certificate and DNS record serve every environment of a host.
- Two environment options in `catalog.yaml`, overridable per application like the others:
  - `hostSuffix` (default empty), appended to every application's `host` in the environment, so environments sharing
    a host and its domain get their own host names (`heimdall-dev.<domain>`, `heimdall-hml.<domain>`). It must keep
    `<host><hostSuffix>` one DNS label of at most 63 characters.
  - `onDemand` (default `false`): the environment runs only while it is used.
- `scripts/ygg.sh env start <environment>`, `env stop <environment> [--force]` and `env status`, also in the menu
  ("Start or stop an environment (on demand)"): start or stop every application of one of the host's environments.
  `stop` refuses an environment that isn't `onDemand` unless `--force`.
- `scripts/deploy.sh` in an `onDemand` environment: an application that wasn't running before the deploy (switched off,
  or never deployed) is still started and waited for, so a broken build is caught and rolled back, then stopped again.
  `DEPLOY_START=1` leaves it running; Jenkins sets it for **Build with Parameters → `DEPLOY_TO`** builds, and `DEPLOY_TO` now
  offers `branch` environments as well as `manual` ones.
- A `stopped` application status in the status API and the console: an application stopped normally in an on-demand
  environment. Neutral, not a problem; it isn't probed.
- `scripts/catalog.py environment <environment> [<option>]`, an environment's own options with the defaults applied.
- `ENVIRONMENTS` in `platform.env`: the environments a host runs, comma-separated (`development,homologation,production`).
  The status container gets it as `YGGDRASIL_ENVIRONMENTS`.
- Prometheus targets and Loki log lines of applications carry an `environment` label.
- [docs/setup.md](docs/setup.md#on-demand-environments) has an *On-demand environments* section, a *Planning the
  hosts* table, and how to add an environment to a host that already runs others.
- A proprietary `LICENSE` (all rights reserved), and a Legal section in the README.

### Changed

- **The status API contract is version 2** ([docs/status-api.md](docs/status-api.md)): one response per host, with
  `host`, an `environments` list (`id`, `name`, `onDemand`, `status`) and, per system, `environments[]` each with its
  `status` and `applications`. Application objects are unchanged. The host, every environment and every system have a
  status from one roll-up rule in which `not_deployed` and `stopped` members never make anything worse. Consoles before
  0.5 can't read it; the 0.5 console reads v1 and v2.
- The status API serves every environment of its host (`YGGDRASIL_ENVIRONMENTS`; the legacy `YGGDRASIL_ENVIRONMENT`
  counts as a list of one). It finds an application in an environment as the Compose project
  `<container.project or id>-<environment>`, probes the catalog's `health` with the host qualified
  (`http://heimdall-api.production:8080/healthcheck`), and builds `url` with the environment's `hostSuffix`. Platform
  components are probed once per refresh and appear in every environment. `/internal/prometheus/targets` has one group
  per environment and application, `<metrics host>.<environment>:<port>`, labelled `environment`. Restarts no longer
  count as "restarted recently" in an on-demand environment, where `scripts/ygg.sh env start` restarts containers.
- The console shows a **host** with all its environments: one status chip per environment in the header and on each
  system card, and, expanded, a section per environment with its applications. The saved connections are called
  hosts in every screen and dialog (**Hosts**, **Add host**); a host adopts its name from the response's `host`. The
  offline demo shows the default setup on `example.com`.
- `scripts/deploy.sh` names everything per environment: the Compose project `<application>-<environment>`
  (`heimdall-api-production`), `YGG_ENVIRONMENT` exported for the overlays, image tags `<environment>-<version>`
  (`production-1.4.0-3f2a9c1`; rollback and pruning only consider the environment's own tags), and a
  `yggdrasil.environment` label on every container. The rollback target is the newest container of the project,
  running or not.
- The stack overlays give each application the network alias `<application>.<environment>` on `edge` and `telemetry`,
  and name its Traefik routers and services `<application>-<environment>`; their labels are a list, so the names can
  use `${YGG_ENVIRONMENT}`. `scripts/ygg.sh add` writes new stack files the same way.
- The catalog's `health` and `metrics` stay written with the plain id; for every application that isn't
  `kind: platform`, the status API and Prometheus qualify the host with the environment.
- `scripts/catalog.py validate` checks `hostSuffix` and `onDemand`, that `<host><hostSuffix>` stays one DNS label, and
  rejects two (application, environment) pairs whose Compose projects `<application>-<environment>` would be the same.
- `platform.env` takes `ENVIRONMENTS` instead of `ENVIRONMENT`. `scripts/platform.sh up` checks each of them against
  the catalog. A `platform.env` with only `ENVIRONMENT` still works, as a list of one; `ENVIRONMENTS` wins when both are
  set.
- Jenkins creates one agent per distinct `agent` of the catalog's environments: one per host. The default catalog's
  development, homologation and production share the agent `vps`.
- `scripts/ygg.sh` works with every environment of the host: `status` lists each of them, `config [<app>]
  [<environment>]` and `add` take or ask for the environment, and `YGG_ENVIRONMENT` still forces one.
- Alloy labels each log line with its container's `yggdrasil.environment` label instead of the host's `ENVIRONMENT`;
  platform containers have none. The `stack` label is the Compose project, so `heimdall-api-production`.
- The default `catalog.yaml` has four environments: `local` (Docker Desktop, `ports`, `manual`), and on one VPS with the
  agent `vps`, `development` (from `develop`, `hostSuffix: -dev`, on demand), `homologation` (from `release/*`,
  `hostSuffix: -hml`, on demand) and `production` (`release`).
- [docs/setup.md](docs/setup.md) follows that layout by default, a host per environment staying an option;
  [docs/dns.md](docs/dns.md) uses one `*.<DOMAIN>` record per host, environments told apart by `hostSuffix`, instead of
  a sub-domain per environment. `templates/application/` describes the default environments and on-demand
  environments.
- `templates/application/CONTRIBUTING.md` also sets out the commit message, changelog and Semantic Versioning
  conventions of an application repository, has each release's changelog (and version file, if any) finalized on
  `develop` before the release branch is cut, and covers releasing an application that has no release environment.
- [docs/setup.md](docs/setup.md) clones the fork's `main` branch, since `develop` is this repository's default branch.
- `release.yml` builds and attaches the console's installable apps only when the release's tag is on `main`.
- `scripts/catalog.py validate` also enforces the rules the status API applies when it loads the catalog (application
  and system names, at least one system and one application per system, unique system ids, `metrics` as host:port,
  `metricsPath`, `host`, `repository` and `checks` formats), so CI rejects a catalog that would stop the status API on
  every host. It also rejects `true`/`false` as a number, and an application that switches an environment to
  `trigger: branch` without any `branches`.
- `scripts/deploy.sh` runs one deploy of a stack to an environment at a time, locking the env file (with `flock`, when
  available): a second deploy waits for the first.
- `scripts/ygg.sh config` also masks `*_PREVIOUS` keys kept during a rotation and connection strings.
- `DOCKER_GID` is now required in every host's `platform.env`, not only with the `agent` profile: the Docker socket
  proxies run in the host's `docker` group.
- The status API reads only Docker's container list. `container.startedAt` is the container's creation time when it
  has not restarted, and as precise as Docker's "Up 5 minutes" when it has; "restarted in the last 10 minutes" now
  counts any start more than 5 minutes after the container's creation, so a reboot of the host or of Docker counts too.
- The console uses `flutter_riverpod` 3 (from 2.6.1). Its automatic retry of failing providers is turned off, so a
  failure to load the saved environments still shows at once and the status polling keeps its own schedule.

### Deprecated

- `container.restartCount` in the status API's responses is always `null`: Docker only gives the count with the
  container inspect, which the status API may no longer read. The console already shows nothing for `null`.

### Removed

- The rule "one environment per Docker engine".
- The plain `<application>` network alias: an application is `<application>.<environment>`. Calls between applications
  go to the one of their own environment.
- Version 1 of the status API's `/api/status` and `/api/systems/{id}` (`environment`, `environmentName`,
  `systems[].applications`).
- The `ENVIRONMENT` variable of the Alloy container.
- `docs/examples/docker-desktop-wsl-vps`, with its homologation in WSL on the workstation: replaced by
  [docs/examples/docker-desktop-and-vps](docs/examples/docker-desktop-and-vps/README.md), with env file templates for
  four applications in four environments.

### Fixed

- `scripts/deploy.sh` rolled a failed release of a stack that also runs a database (or any other service with an image
  of its own) back to that service's tag (`postgres:16` gave `16`), which failed and left the application down. It now
  rolls back to the previous image of the stack's own build, and prunes old images by the same names.
- `scripts/deploy.sh` reports an env file it cannot read up front, and labels deployments with the 7-character commit
  Jenkins puts in the tag even when `git` would abbreviate it longer.
- `scripts/github.sh wait-checks` no longer fails a release on a single unanswered request to GitHub while it waits for
  the checks, and every failed GitHub call (a refused merge, say) now prints GitHub's reason in the build log.
- `scripts/platform.sh up` and `scripts/ygg.sh` matched `ENVIRONMENT` and application ids as regular expressions, so
  `prod.*` passed for `production`.
- The console could show, after an environment's URL was edited, the late answer of the old URL, and keep it as that
  environment's last known status.

### Security

- The status API, which answers the internet behind its token, could read every container's environment (every
  application's database passwords, signing keys and tokens), logs and files: its Docker socket proxy
  (`tecnativa/docker-socket-proxy` with `CONTAINERS=1`) also allows the container inspect, `/logs` and `/archive`. It
  now reads Docker through a `wollomatic/socket-proxy` (pinned by digest) that allows only `GET /containers/json`, and
  only from the status API's container.
- Traefik and Alloy no longer mount the Docker socket, whose `:ro` doesn't restrict the API: either could create or
  exec into containers, i.e. take over the host. Each reads Docker through its own socket proxy on a private network,
  which allows only the read-only requests it makes (Traefik: version, container list, inspect and events; Alloy:
  container and network lists, inspect and logs). Only the Jenkins agent, which deploys, still holds the socket.
  `scripts/test_socket_proxies.py` checks the allowlists, and CI runs Traefik and Alloy through them.

### Upgrading from 0.4 to 0.5

For a host that runs one environment, as every 0.4 host does. Read it through first: steps 4 to 6 happen between the
pull and the first deploy of each application.

1. **Update the consoles first.** Install the Windows and Android apps of the 0.5 release: they read the status API of
   0.4 and 0.5 hosts, while a console before 0.5 can't read a 0.5 host. The web console updates with its host.
2. **Pull**, on every host: `cd /opt/yggdrasil && git pull`. If your fork carries its own catalog, merge the release
   into it first; to move several environments onto one host, give them the same `agent`, a `hostSuffix` each and,
   for those used only now and then, `onDemand: true` ([docs/catalog.md](docs/catalog.md#several-environments-on-one-host)).
3. **`platform.env`**: replace `ENVIRONMENT=<id>` with `ENVIRONMENTS=` listing every environment the host runs, e.g.
   `ENVIRONMENTS=development,homologation,production` (an `ENVIRONMENT` alone still works, as one environment). Then
   `scripts/platform.sh up` and `docker restart yggdrasil-status-1`.
4. **Your own stack files**, if you wrote any: give the proxy overlay the alias `<id>.${YGG_ENVIRONMENT}` instead of
   `<id>`, name its routers and services `<id>-${YGG_ENVIRONMENT}`, and write its labels as a list, as
   [stacks/heimdall-api.proxy.yml](stacks/heimdall-api.proxy.yml) does.
5. **Remove the old projects before the first deploy** of each application. The project was `<application>`; it is now
   `<application>-<environment>`, and the old containers keep their routers and aliases, which collide with the new
   ones. On each host, for each application:
   ```bash
   docker compose -p heimdall-api down
   ```
   The application is down from then until its first 0.5 deploy: do it right before deploying
   (`scripts/ygg.sh config <application> <environment>` → Apply, or a Jenkins build).
6. **Volumes** are named after the project, so they change name too: `heimdall-api_logs` becomes
   `heimdall-api-production_logs`. If the data matters, copy it after step 5 and before the first deploy:
   ```bash
   docker volume create heimdall-api-production_logs
   docker run --rm -v heimdall-api_logs:/from -v heimdall-api-production_logs:/to alpine cp -a /from/. /to/
   ```
   Then remove the old volume when you no longer need it.
7. **Images**: the old `<application>:<version>` images are neither rollback targets nor pruned, since only
   `<application>:<environment>-*` tags are. The first deploy after the upgrade has nothing to roll back to. Remove the
   old images by hand once it is healthy (`docker image ls heimdall-api`, then `docker image rm`).
8. **Application env files** keep their path, `<secrets>/<environment>/<application>.env`. Replace every URL that uses
   the old plain alias (`http://<application>:<port>`): with `http://<application>.<environment>:<port>`, or with the
   public HTTPS host where the application requires HTTPS (fortuna-api's `FORTUNA_HEIMDALL_BASE_URL`). An environment
   new on the host needs its own env files, with the suffixed host names in `PUBLIC_HOST` and `UI_HOST`
   (`heimdall-api-dev.example.com`).
9. **The Jenkins agent**, if the host's environments now share an `agent` (`vps` in the default catalog): restart the
   controller (`docker restart yggdrasil-jenkins-1`), which creates the new agent; set `JENKINS_AGENT_NAME=vps` and
   its secret (**Manage Jenkins → Nodes → vps**) in `platform.env` and run `scripts/platform.sh up`; then delete the old
   per-environment nodes in **Manage Jenkins → Nodes**, which Jenkins never deletes by itself.
10. **Grafana**: Loki and Prometheus queries that relied on the `environment` label of platform containers find none
    now, and Loki's `stack` label is the Compose project (`heimdall-api-production`, no longer `heimdall-api`). Update
    saved queries and dashboards.

## [0.4.0] - 2026-09-29

### Added

- `scripts/ygg.sh`, a host helper menu for Ubuntu hosts whose entries are also commands: check and install the tools
  (Docker from Docker's apt repository, the docker group, the secrets directory, ufw), set up an application (catalog
  entry, stack files, env file, first deploy), see what runs on the host, and change an application's env file and
  redeploy it ([docs/cli.md](docs/cli.md)).
- `scripts/catalog.py add-application`, which inserts into `catalog.yaml` keeping its comments, plus the `systems` and
  `show` commands.
- `scripts/deploy.sh` exports `APP_ENV_FILE`, so a stack can hand the env file to the container.

## [0.3.3] - 2026-09-28

### Fixed

- A release build no longer ends in failure after merging and tagging when GitHub has already deleted the release
  branch (`delete_branch_on_merge`, which `github/rulesets.py` turns on).

## [0.3.2] - 2026-09-25

### Fixed

- Traefik builds HTTPS routers again: the `websecure` entrypoint no longer refers to the unknown TLS options
  `default@file`, which made Traefik close every TLS handshake.

## [0.3.1] - 2026-09-25

### Added

- [docs/dns.md](docs/dns.md), a step-by-step DNS and certificates guide for Cloudflare with your own domain or a free
  DuckDNS subdomain, including a first run against the Let's Encrypt staging CA and troubleshooting.

### Changed

- [docs/setup.md](docs/setup.md) is rewritten as ordered steps from an empty GitHub account to a first release, with
  the GitHub App setup and host preparation detailed command by command.
- `env/platform.env.example` marks every required variable and ships `COMPOSE_PROFILES` empty, since an agent needs a
  secret that only exists once the controller has created it. Env files are expected to be mode 640 in
  `/etc/yggdrasil` (group docker, mode 2750) so the Jenkins agent can read them.
- `scripts/deploy.sh`'s missing env file error points at the real cause, including the agent's read access.

### Fixed

- Traefik is updated from v3.5 to v3.6, which negotiates the API version with Docker Engine 29; v3.5 saw no
  containers there, so it had no routes and no certificate.
- The Cloudflare API token documented in `env/acme.env.example` also needs Zone > Zone > Read.
- `scripts/__pycache__` is no longer tracked, so running the catalog tooling no longer leaves modified files.

## [0.3.0] - 2026-09-18

### Added

- Any number of environments in `catalog.yaml`, in promotion order, each with its options: `mode` (`proxy` or
  `ports`), `trigger` (`manual`, `branch` or `release`), `branches`, `agent`, `approval`, `waitTimeout`, `keepImages`
  and `checksTimeout`. An application deploys to every environment or to its own list, and can override any option per
  environment.
- `scripts/catalog.py` validates the catalog and resolves each application's environment options.
- The GitHub owner and repository come from the catalog, and the ACME DNS provider is configurable.
- [docs/catalog.md](docs/catalog.md) (catalog reference), [docs/setup.md](docs/setup.md) (setup guide),
  `templates/application/` (what each application repository needs) and the
  `docs/examples/docker-desktop-wsl-vps` worked example.

### Changed

- `deploy.sh`, the Jenkins pipeline (one stage per environment, approval, `deploy/<environment>` statuses), the
  Jenkins agents and job branches, `rulesets.py` and the status API follow the catalog's environments instead of a
  fixed development, homologation and production.

### Fixed

- `scripts/catalog.py` prints plain LF on Windows, so Git Bash callers get no trailing carriage return.

## [0.2.0] - 2026-09-18

### Added

- The console as an installable Windows app: an Inno Setup installer, per-user by default, with a Start menu entry, an
  optional desktop shortcut and in-place upgrades, and the Visual C++ runtime bundled.
- The Windows installer and the Android APK are attached to every `v*` GitHub release.

## [0.1.0] - 2026-09-18

### Added

- The platform each environment host runs: Traefik with Let's Encrypt DNS-01 (Cloudflare) wildcard certificates,
  Prometheus, Loki, Alloy and Grafana, and a Jenkins controller and WebSocket agents configured as code.
- `catalog.yaml`, listing every system and the applications it is made of. Jenkins jobs, GitHub rulesets, Prometheus
  targets and `deploy.sh` read it.
- `scripts/deploy.sh`, which builds, waits for health and rolls back an application, labels every container with the
  version, commit and time of its deployment and restores the previous labels on rollback.
- The shared Jenkins pipeline, which deploys release branches and green release pull requests into `main`, then
  merges, tags and deletes the branch through `scripts/github.sh`.
- `github/rulesets.py`, the repositories' branch and tag rulesets as code.
- The status API (.NET 10), one per environment host: it probes every application's health endpoint, reads container
  state and deployment labels through a read-only Docker socket proxy, and serves `/api/status` behind a bearer token
  and Prometheus HTTP service discovery ([docs/status-api.md](docs/status-api.md)).
- The console (Flutter) for the web and Android, showing every system's status and, expanded, each of its
  applications.

[Unreleased]: https://github.com/artur-rios/yggdrasil/compare/v0.9.4...HEAD
[0.9.4]: https://github.com/artur-rios/yggdrasil/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/artur-rios/yggdrasil/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/artur-rios/yggdrasil/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/artur-rios/yggdrasil/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/artur-rios/yggdrasil/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/artur-rios/yggdrasil/compare/v0.7.4...v0.8.0
[0.7.5]: https://github.com/artur-rios/yggdrasil/compare/v0.7.4...78ee46c
[0.7.4]: https://github.com/artur-rios/yggdrasil/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/artur-rios/yggdrasil/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/artur-rios/yggdrasil/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/artur-rios/yggdrasil/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/artur-rios/yggdrasil/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/artur-rios/yggdrasil/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/artur-rios/yggdrasil/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/artur-rios/yggdrasil/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/artur-rios/yggdrasil/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/artur-rios/yggdrasil/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/artur-rios/yggdrasil/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/artur-rios/yggdrasil/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/artur-rios/yggdrasil/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/artur-rios/yggdrasil/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/artur-rios/yggdrasil/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/artur-rios/yggdrasil/releases/tag/v0.1.0
