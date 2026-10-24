# Machines and Cutover Implementation Plan (plan 4 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the owner the tools and the runbook to build both machines, move the data and all repositories from
GitHub to Forgejo in one maintenance window, mirror them back to GitHub, and prove each step worked.

**Architecture:** Plans 1–3 built the machines' setup (`ygg install` / `ygg check`, `docs/setup.md` steps 8–14),
the rules and the pipeline. This plan adds what only the move needs: `scripts/migrate.py`, one idempotent tool
with `--dry-run` for the repository move (inventory, import, GitHub copies, push mirrors, collaborators, a
verify gate); `scripts/cloudflare_dns.py` for the records; `scripts/volumes.sh` to carry Docker volumes from the
old rootful VPS to the new rootless one; `scripts/prove-machine.sh` for the spec's rootless proofs; the items
plan 3 handed over (Forgejo's webhook host list, CI engine prunes, Renovate's GitHub token); a live restore test;
and `docs/cutover.md`, the runbook for rollout steps 2–5 and 8, written against these tools.

**Tech Stack:** Python 3.10+ (stdlib + PyYAML; `rules.Api` for HTTP), Bash, Docker (rootful on the old VPS,
rootless on the new machines), systemd user timers, Forgejo 16.0.6 API, GitHub REST API, Cloudflare API v4,
unittest.

**Spec:** `docs/superpowers/specs/2026-10-22-self-hosted-git-and-ci-design.md` — "The two machines", "Users and
privileges", "Forgejo" (Installation, Accounts and repositories, The GitHub mirror), "The CI agent" (prune),
"Rollout" steps 2–5 and 8, "Testing" (rootless setup, the mirror, backups, both git servers, Cloudflare).

**Not in this plan:** each repository's `yggdrasil.yml`, `ci/` scripts, Renovate config, trimmed workflows,
catalog entries and link updates (rollout steps 6–7, plan 5); turning GitHub Pages and Vercel off (plan 5, as
each site moves).

## Decisions this plan makes (read first)

1. **One migration tool, many idempotent subcommands**, each safe to re-run during the window and with
   `--dry-run`: `inventory`, `import`, `prepare-github`, `mirrors`, `collaborators`, `verify`. Re-running after a
   failure picks up where it stopped; `verify` is the gate before pushes go to Forgejo.
2. **Dependabot is stopped from Forgejo's side**: during `prepare-github`, before the mirrors, `.github/dependabot.yml`
   is deleted on each repository's default branch through Forgejo's contents API (one commit, author the owner,
   "chore: stop dependabot; renovate replaces it"), and GitHub's automated security fixes are switched off. The
   first mirror push carries the deletion to GitHub. This happens before `rules.py` protects the branches.
3. **The volumes move as tar files of stopped volumes**, with a manifest (name, Compose labels, size, sha256),
   because the old VPS runs rootful Docker and the old yggdrasil: `scripts/volumes.sh` needs only `docker` and a
   pinned image, works with either engine, and recreates each volume with its Compose labels.
4. **Collaborators are applied by `migrate.py collaborators`**, not by hand: on every catalog repository
   (`catalog.py repository-names`), `jenkins` write, `jenkins-ci` read, `renovate` write, `claude` write. Forgejo
   drops whitelisted users without write access, so this runs before `rules.py`.
5. **Cloudflare settings that the DNS-only token can't change** (SSL mode Full (strict), the cache rule) stay
   manual steps in the runbook, with how to check each.

## Global Constraints

- Owner on both sides: `artur-rios`; repositories keep their names; 60 repositories on GitHub today.
- Forgejo at `git.techcraftsmen.net`, Jenkins at `jenkins.techcraftsmen.net` (dev machine, DNS only, not proxied);
  the apps machine: wildcard + bare domain of `techcraftsmen.net` and `artur-rios.tech`, proxied.
- Import with Forgejo's GitHub migration: issues, labels, milestones, releases, pull requests (and wiki when the
  GitHub repository has one); visibility as on GitHub. Open GitHub pull requests are finished or closed first.
- Push mirror per repository: every branch and tag, `sync_on_commit: true`, interval `8h0m0s`; token: GitHub
  fine-grained, Contents and Workflows write on the mirrored repositories.
- GitHub copies: rulesets deleted except `mirror tags` (plan 3, made by `rules.py`); issues disabled; description
  `Mirror of https://git.techcraftsmen.net/artur-rios/<repo> — contribute there`; Dependabot off.
- No secret in argv or a URL: tokens through env vars or curl `--config -` / `rules.Api` headers.
- Webhooks reach Jenkins over the dev machine's internal Docker network (spec), so Forgejo must allow that host.
- CI engine "a scheduled prune keeps them bounded (builder's call)": `ci` and `release` users, weekly.
- Nothing runs as root: every service script refuses root, as plan 1's do.
- Tests: WSL `python3 -m unittest discover -s scripts -p <file>`; live suites gated (`YGG_FORGEJO_TESTS=1`);
  shellcheck silent; names `test_given_…_when_…_then_…`; commits lowercase Conventional, ≤50 chars.

## Review Focus

1. **A re-run during the window** (a repository already imported, a mirror already there, a ruleset already
   deleted, a dependabot file already gone) is a no-op line, never a duplicate or an error (Tasks 2–5 tests).
2. **A partial import**: Forgejo answers 5xx or times out on one repository of 60 — the run reports it, carries
   on, exits 1, and `verify` names it (Tasks 2, 5 tests).
3. **A GitHub copy diverging from Forgejo** (a commit pushed to GitHub during the window, a mirror that failed):
   `verify` compares `develop`, `main` and every tag sha and fails naming the ref (Task 5 test).
4. **A volume in use or a torn copy**: `volumes.sh export` refuses a volume mounted by a running container, and
   `import` refuses a tar whose sha256 doesn't match the manifest or a volume that already holds data (Task 7 tests).
5. **DNS mistakes**: `cloudflare_dns.py` never deletes a record it didn't make, never proxies a dev-machine name,
   and a dry run shows exactly the changes (Task 6 tests).

---

### Task 1: `scripts/migrate.py`: the frame and `inventory`

**Files:** Create `scripts/migrate.py`, `scripts/test_migrate.py`.

**Interfaces:**
- Consumes: `rules.Api(base, token, scheme, headers=None)`, `rules.ApiError`, `rules.find_github_token()` (plan 3),
  `catalog.load()`, `catalog.forge()`, `catalog.mirror()`, `catalog.repository_names()`.
- Produces:
  - CLI `python3 scripts/migrate.py [--dry-run] <command> [--only <repo>…]`; env `FORGE_TOKEN` (the owner's, scope
    `write:repository`), GitHub token as rules.py finds it. Exit 0 ok, 1 something failed
    (each failure on its own line; the run carries on), 2 usage.
  - `github_repositories(gh: Api, owner: str) -> list[dict]` — every repository of the owner (paged, forks
    included): `{name, private, fork, archived, default_branch, has_wiki, open_pulls}`.
  - `inventory`: one line per repository `<name>\t<public|private>\t<fork?>\t<open pull requests>\t<in catalog?>`,
    then totals; exit 1 when any repository has open pull requests ("finish or close them first").

- [ ] **Step 1: Failing tests** (fake `Api` like test_rules.py):

```python
def test_given_sixty_repositories_on_two_pages_when_inventoried_then_every_one_is_listed(self):
def test_given_open_pull_requests_when_inventoried_then_they_are_named_and_the_run_exits_1(self):
def test_given_a_repository_in_the_catalog_when_inventoried_then_it_is_marked(self):
def test_given_no_github_token_when_run_then_it_exits_2_with_how_to_give_one(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: inventory the github repositories`.

---

### Task 2: `migrate.py import`

**Files:** Modify `scripts/migrate.py`, `scripts/test_migrate.py`, `scripts/test_forgejo_live.py`.

**Interfaces:**
- `import`: for each GitHub repository not yet on Forgejo, `POST /api/v1/repos/migrate` with `service: "github"`,
  `clone_addr: https://github.com/<owner>/<name>.git`, `auth_token` (the GitHub token), `repo_owner: <owner>`,
  `repo_name`, `private` as on GitHub, `mirror: false`, `issues`, `labels`, `milestones`, `releases`,
  `pull_requests: true`, `wiki: has_wiki`, `lfs: false`. A repository already on Forgejo: "already imported",
  untouched. One repository failing (5xx, timeout — the HTTP timeout is 30 min per repository, overridable by
  `MIGRATE_TIMEOUT`) is reported and the run continues; exit 1 at the end. Refuses to start while `inventory`
  would report open pull requests, unless `--allow-open-pulls`.
- The token goes in the JSON body (Forgejo's API needs it there), never in argv or a log line.

- [ ] **Step 1: Failing tests:**

```python
def test_given_a_new_repository_when_imported_then_the_migration_asks_for_issues_pulls_and_releases(self):
def test_given_a_repository_already_on_forgejo_when_imported_then_it_is_left_as_it_is(self):
def test_given_one_import_answering_500_when_imported_then_the_rest_go_on_and_the_run_exits_1(self):
def test_given_open_pull_requests_when_imported_then_it_refuses_unless_allowed(self):
def test_given_a_dry_run_when_imported_then_nothing_is_sent_and_the_payload_hides_the_token(self):
```
  Live (`test_forgejo_live.py`): a migration (`service: "git"`) from a repository on the suite's own Forgejo, by
  its HTTP URL, proves the request shape Forgejo accepts and the "already imported" path (GitHub itself is not
  reachable from the suite; the test container sets `FORGEJO__migrations__ALLOW_LOCALNETWORKS=true`, the
  production one doesn't).
- [ ] **Step 2–4:** FAIL, implement, PASS (unit + live).
- [ ] **Step 5: Commit** — `feat: import the repositories into forgejo`.

---

### Task 3: `migrate.py prepare-github`

**Files:** Modify `scripts/migrate.py`, `scripts/test_migrate.py`.

**Interfaces:**
- For each repository (all imported ones, `--only` to limit):
  1. Forgejo side: when the default branch has `.github/dependabot.yml`, delete it through
     `DELETE /api/v1/repos/<owner>/<repo>/contents/.github/dependabot.yml` (`sha` of the file, `branch`: default,
     `message: "chore: stop dependabot; renovate replaces it"`). Already gone: "no dependabot file".
  2. GitHub side: delete every ruleset except `mirror tags`; `PATCH /repos/<owner>/<repo>` with
     `has_issues: false` and the description of the Global Constraints; `DELETE …/automated-security-fixes`
     (204 or 404 = fine).
- Order of the window (runbook): `import` → `prepare-github` → `mirrors` → `collaborators` → `rules.py` →
  `verify`.

- [ ] **Step 1: Failing tests:**

```python
def test_given_a_dependabot_file_when_prepared_then_it_is_deleted_on_forgejos_default_branch(self):
def test_given_no_dependabot_file_when_prepared_then_nothing_is_deleted(self):
def test_given_old_rulesets_when_prepared_then_all_but_mirror_tags_are_deleted(self):
def test_given_a_repository_when_prepared_then_issues_are_off_and_the_description_points_at_forgejo(self):
def test_given_prepared_twice_when_run_then_the_second_run_changes_nothing(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: prepare the github copies`.

---

### Task 4: `migrate.py mirrors` and `collaborators`

**Files:** Modify `scripts/migrate.py`, `scripts/test_migrate.py`, `scripts/test_forgejo_live.py`.

**Interfaces:**
- `mirrors`: per repository, unless a push mirror to `https://github.com/<mirror.github>/<repo>.git` exists,
  `POST /api/v1/repos/<owner>/<repo>/push_mirrors` with `remote_address`, `remote_username` (the GitHub user that
  owns the mirror token, env `MIRROR_GITHUB_USER`), `remote_password` (env `MIRROR_GITHUB_TOKEN`), `interval:
  "8h0m0s"`, `sync_on_commit: true`; then `POST …/push_mirrors-sync` once. A mirror to another address is
  reported, not replaced.
- `collaborators`: on every catalog repository, `PUT /api/v1/repos/<owner>/<repo>/collaborators/<user>` with
  `permission`: `jenkins` write, `jenkins-ci` read, `renovate` write, `claude` write (users from `forge.jenkinsUser`
  and fixed names; a user missing on Forgejo is reported: create it first, setup.md 9.7). Already right: unchanged.

- [ ] **Step 1: Failing tests:**

```python
def test_given_no_push_mirror_when_mirrored_then_one_syncing_on_commit_every_8h_is_made(self):
def test_given_the_mirror_already_there_when_mirrored_then_it_is_kept(self):
def test_given_a_mirror_to_another_address_when_mirrored_then_it_is_reported_not_replaced(self):
def test_given_a_catalog_repository_when_collaborators_run_then_each_bot_gets_its_permission(self):
def test_given_a_missing_bot_user_when_collaborators_run_then_it_is_named_and_the_run_exits_1(self):
```
  Live: a push mirror to a second Forgejo repository (the suite's own server stands in for GitHub) is created,
  synced, and a pushed tag appears on the target; collaborators read back with their permissions.
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: add push mirrors and collaborators`.

---

### Task 5: `migrate.py verify`: the gate

**Files:** Modify `scripts/migrate.py`, `scripts/test_migrate.py`.

**Interfaces:**
- Per repository, rows `ok`/`FAIL`: on Forgejo; default branch `develop` where GitHub's was (else as GitHub);
  a push mirror whose `last_error` is empty and `last_update` is set; GitHub's `develop`, `main` and every tag at
  the same sha as Forgejo's (refs listed with `git/matching-refs` on GitHub and `git/refs` on Forgejo); GitHub
  rulesets exactly `{mirror tags}` (release model) or `{}` (basic); issues off; for catalog repositories, the
  Jenkins webhook present (`<FORGE_JENKINS_URL>/gitea-webhook/post`) and the four collaborators. Exit 1 on any
  FAIL, with a summary count.

- [ ] **Step 1: Failing tests:**

```python
def test_given_everything_in_place_when_verified_then_every_row_is_ok(self):
def test_given_a_tag_on_another_commit_on_github_when_verified_then_it_fails_naming_the_tag(self):
def test_given_a_mirror_with_a_last_error_when_verified_then_it_fails_with_the_error(self):
def test_given_a_repository_missing_on_forgejo_when_verified_then_it_fails(self):
def test_given_a_catalog_repository_without_its_webhook_when_verified_then_it_fails(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: verify the move`.

---

### Task 6: `scripts/cloudflare_dns.py`

**Files:** Create `scripts/cloudflare_dns.py`, `scripts/test_cloudflare_dns.py`; modify `env/acme.env.example`
(comment only), `docs/hosts.md` (DNS records section points at the script).

**Interfaces:**
- `python3 scripts/cloudflare_dns.py [--dry-run] --apps <ipv4> [--apps6 <ipv6>] --dev <ipv4> [--dev6 <ipv6>]`;
  token `CF_DNS_API_TOKEN` from the env (acme.env's name). Zones `techcraftsmen.net` and `artur-rios.tech`.
- `desired_records(apps, apps6, dev, dev6) -> list[dict]`: per zone `@` and `*` A/AAAA → apps, `proxied: true`;
  on `techcraftsmen.net`, `git`, `jenkins`, `grafana-dev` A/AAAA → dev, `proxied: false`; comment
  `yggdrasil` on every record it makes.
- Apply: create or update records it owns (matched by name + type); a record of the same name and type without the
  `yggdrasil` comment is reported and left ("made by hand; change it or delete it yourself"); never deletes; lists
  other records unchanged. Exit 1 on any report.

- [ ] **Step 1: Failing tests:**

```python
def test_given_both_addresses_when_desired_then_apps_names_are_proxied_and_dev_names_are_not(self):
def test_given_a_record_made_by_hand_when_applied_then_it_is_reported_and_left(self):
def test_given_records_already_right_when_applied_then_nothing_is_sent(self):
def test_given_a_dry_run_when_applied_then_only_the_changes_are_printed(self):
def test_given_no_token_when_run_then_it_exits_2(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: apply the cloudflare dns records`.

---

### Task 7: `scripts/volumes.sh`: carry the volumes

**Files:** Create `scripts/volumes.sh`, `scripts/test_volumes.py`.

**Interfaces:**
- `scripts/volumes.sh export <dir> [<volume>…]` (default: every volume with a `com.docker.compose.project` label):
  refuses a volume used by a running container (`docker ps --filter volume=<v>`); writes `<dir>/<volume>.tar`
  via the pinned tar image (the same `TAR_IMAGE` constant as backup.sh — move it to docker-engine.sh and use it
  from both) and `<dir>/manifest.tsv`: `<volume>\t<project>\t<compose volume>\t<bytes>\t<sha256>`.
- `scripts/volumes.sh import <dir>`: for each manifest line, checks the sha256, refuses a volume that exists and
  is not empty, creates it with labels `com.docker.compose.project` and `com.docker.compose.volume` from the
  manifest, untars into it. Works on rootful (`DOCKER_HOST` unset, run with sudo by the admin on the old VPS — the
  only place this script may run as root: it says so) and rootless (`ygg`, via `use_rootless_engine`) engines.

- [ ] **Step 1: Failing tests** (fake `docker` keeping volumes in a state dir):

```python
def test_given_stopped_volumes_when_exported_then_each_has_a_tar_and_a_manifest_line(self):
def test_given_a_volume_in_use_when_exported_then_it_is_refused(self):
def test_given_a_manifest_when_imported_then_volumes_come_back_with_their_compose_labels(self):
def test_given_a_tar_with_another_checksum_when_imported_then_it_is_refused(self):
def test_given_a_volume_already_holding_data_when_imported_then_it_is_refused(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS + shellcheck; backup tests still green after moving `TAR_IMAGE`.
- [ ] **Step 5: Commit** — `feat: move docker volumes between hosts`.

---

### Task 8: Handed over from plan 3: webhook hosts, prunes, Renovate's GitHub token

**Files:** Modify `platform/compose.yml` (forgejo service), `scripts/test_forgejo_service.py`; create
`platform/systemd/yggdrasil-prune.service`, `platform/systemd/yggdrasil-prune.timer`; modify `scripts/machine.sh`
(step `prune-timer`), `scripts/test_machine.py`; modify `scripts/renovate.sh`, `env/renovate.env.example`,
`scripts/test_renovate.py`; `docs/hosts.md`, `docs/setup.md` (9.4, 9.9).

**Interfaces:**
- Forgejo: `FORGEJO__webhook__ALLOWED_HOST_LIST: "external,jenkins"` — `external` keeps public hooks working, and
  `jenkins` is the controller's Compose service name on the shared network (check `platform/compose.yml`; if the
  webhook URL docs use another internal name, use that one and say so). setup.md 9.4 sets
  `FORGE_JENKINS_URL=http://jenkins:8080`.
- Prune: dev machine, users `ci` and `release`: a weekly user timer (`OnCalendar=Sun 03:00`, `Persistent=true`,
  `RandomizedDelaySec=1h`) running `docker system prune --all --force --filter until=168h` and `docker builder
  prune --all --force --filter until=168h` on the user's own engine; installed and checked by `ygg install` /
  `ygg check` like the backup timer.
- Renovate: optional `RENOVATE_GITHUB_COM_TOKEN` (a fine-grained, read-only, public-repositories token) in
  renovate.env, passed through the run-scoped env file (Renovate reads `GITHUB_COM_TOKEN`); the check row warns
  when it's empty ("release notes and changelogs from GitHub will be rate-limited").

- [ ] **Step 1: Failing tests:**

```python
def test_given_the_forgejo_service_when_read_then_webhooks_may_reach_jenkins_internally(self):
def test_given_role_dev_when_installed_then_ci_and_release_get_a_weekly_prune_timer(self):
def test_given_role_apps_when_installed_then_no_prune_timer_is_made(self):
def test_given_a_github_com_token_when_run_then_it_reaches_renovate_only_through_the_env_file(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS + shellcheck.
- [ ] **Step 5: Commit** — `feat: webhook hosts, ci prunes, renovate token` (or three commits, one each).

---

### Task 9: `scripts/prove-machine.sh`: the spec's rootless proofs

**Files:** Create `scripts/prove-machine.sh`, `scripts/test_prove_machine.py`.

**Interfaces:**
- Run by the admin (`artur`, sudo) after `ygg install`: `scripts/prove-machine.sh <apps|dev>` prints rows like
  `ygg check` (`ok`/`FAIL`/`look`) and exits 1 on any FAIL:
  - **no root services**: no process of Docker, containerd, rootlesskit, Traefik, Forgejo, Jenkins, Java or
    `node` owned by uid 0 (`ps -eo user,comm`);
  - **(dev) a check can't reach `ygg`**: as `ci`, a container on `ci`'s engine tries to open
    `/run/user/<ygg uid>/docker.sock` and list `/home/ygg` and `/etc/yggdrasil` — all must fail;
  - **(dev) Testcontainers works rootless**: as `ci`, a container publishes a port on `ci`'s engine and a second
    container reaches it at `TESTCONTAINERS_HOST_OVERRIDE` as `run-check.sh` computes it (reuse its gateway logic:
    move it to a function in run-check.sh callable with `--print-host-override`);
  - **(apps) real client addresses**: `look` row printing the last 10 distinct client addresses in Traefik's
    access log, for the admin to compare with their own address seen through Cloudflare.
- Never prints a secret; containers named `ygg-prove-*` and removed.

- [ ] **Step 1: Failing tests** (fake `ps`, `sudo`, `docker`):

```python
def test_given_a_root_owned_dockerd_when_proved_then_no_root_services_fails(self):
def test_given_the_ygg_socket_readable_from_a_check_when_proved_then_it_fails(self):
def test_given_the_published_port_reachable_when_proved_then_testcontainers_is_ok(self):
def test_given_role_apps_when_proved_then_client_addresses_are_shown_for_a_look(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS + shellcheck.
- [ ] **Step 5: Commit** — `feat: prove a machine's rootless setup`.

---

### Task 10: Live restore of Forgejo from a dump

**Files:** Create `scripts/test_backup_live.py`; modify `docs/hosts.md` (Restoring, if the run proves a step wrong).

**Interfaces:**
- Gated by `YGG_FORGEJO_TESTS=1`. Starts Forgejo 16.0.6 (`ygg-backup-test-*`), creates a repository with a
  commit, a tag and a release, runs `scripts/backup.sh`'s Forgejo dump item against it (point the script at the
  container with the env it uses in tests), starts a fresh Forgejo container on a new volume, restores with the
  steps docs/hosts.md "Restoring" gives, and asserts the repository, the tag and the release are back.

- [ ] **Step 1: Write the test** (red until the steps work).
- [ ] **Step 2: Run** until green; fix docs/hosts.md where the run proves a restore step wrong.
- [ ] **Step 3: Commit** — `test: restore forgejo from a dump live`.

---

### Task 11: `docs/cutover.md`: the runbook

**Files:** Create `docs/cutover.md`; modify `docs/setup.md` (link it from step 0 and step 12), `README.md` (docs
list), `CHANGELOG.md`.

**Interfaces:** A numbered runbook for rollout steps 2–5 and 8, each step with: the command(s), what success
looks like, and how to back out. Every command checked against the code.
- **Before**: the scratch GitHub repository check of the GitHub paths (a repository with a `yggdrasil.yml`, a pull
  request, a check, a release, `forge.type: github`) — plan 3's gate; pin the current controller's library to the
  current release tag (CHANGELOG "Upgrading…").
- **Cloudflare** (step 2): both zones; `artur-rios.tech` name servers if needed, keeping `www.`/`blog.` at
  Vercel; the DNS token; SSL Full (strict) and the cache rule (APIs and the status API never cached) by hand, with
  how to check each (`curl -sI` headers: `cf-cache-status`).
- **Dev machine** (step 3): setup.md 8 and 9 (link), then `prove-machine.sh dev`, Forgejo accounts and bots
  (setup.md 9.7), Jenkins up with an empty catalog, Renovate's first run by hand (`scripts/renovate.sh run` as
  `renovate`, plan 3's note), the webhook host check.
- **Apps machine** (step 4): setup.md 10–11, `prove-machine.sh apps`, `ygg check`.
- **Cutover** (step 5), in order: announce; stop the old VPS's environments; `volumes.sh export` (old VPS, sudo);
  copy (`rsync` as `ygg`); `volumes.sh import` (new VPS, `ygg`); host names: the inventory command (`git grep -nE
  '<old domains>'` across the application repositories and `/etc/yggdrasil` env files) and the edits;
  `cloudflare_dns.py`; repositories: `migrate.py inventory` (no open PRs) → `import` → `prepare-github` →
  `mirrors` → `collaborators` → `rules.py` → `verify` (must be all ok) → local clones' `origin` to Forgejo (keep
  `github` as a second remote); start each environment and check it; the old VPS stays as the rollback.
- **Rollback**: before the DNS switch nothing has changed. After it: point DNS back (`cloudflare_dns.py --apps
  <old ip>`) and start the old VPS. Repositories: the GitHub copies carry every commit made on Forgejo (the
  mirrors), so the old controller can deploy from GitHub again once the push mirrors are paused and its
  rulesets are back (`rules.py` with a GitHub catalog); issues and descriptions are switched back through the
  API. Say exactly what is and isn't reversible (pull requests and issues opened on Forgejo stay there).
- **Clean up** (step 8): old VPS off after the new one ran a release; the GitHub App's permissions reduced (list);
  `scripts/test_*` Spec Testing items left to the owner listed as a checklist (mirror: a Forgejo commit and tag on
  GitHub and Actions ran; Cloudflare: origin refuses non-Cloudflare, real client IPs, APIs uncached, SSL mode).

- [ ] **Step 1: Write it**, verifying every command, flag, path and env var named against the code.
- [ ] **Step 2: Dry-run the repository part** against the live Forgejo suite's server if feasible
  (`migrate.py --dry-run verify` against a fake/scratch setup) and note what was and wasn't exercised.
- [ ] **Step 3: Run** the full unit suite, the Forgejo live suites and shellcheck.
- [ ] **Step 4: Commit** — `docs: add the cutover runbook`.
