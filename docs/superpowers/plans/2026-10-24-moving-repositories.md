# Moving Each Repository Implementation Plan (plan 5 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare every repository's move onto Forgejo and Jenkins:
- what yggdrasil itself still lacks for sites and multi-job repositories;
- the catalog entries;
- in each repository's local clone, a ready-to-push branch with its `yggdrasil.yml`, `ci/` scripts, site Dockerfile, Renovate config, trimmed workflows and new links.

**Architecture:** Two halves.
- **yggdrasil (Tasks 1–7).** Seven pieces:
  - an inventory tool that reads every clone's workflows and writes the final per-repository table;
  - the one pipeline change multi-job repositories need;
  - the status API's full addresses;
  - templates per kind;
  - a link rewriter;
  - the docs index;
  - the catalog with its stack files.
- **The repositories (Tasks 8–14).** For each one, a branch `feature/move-to-forgejo` in its clone under `D:\Repositories`, made from the templates.
  - Each branch is committed, verified locally (checks run in their images, site images build and answer their health check, `yggdrasil.yml` validates against the catalog), and **never pushed**.
  - The owner pushes each one to Forgejo after the cutover (runbook, Task 14) and merges it with the override, as rollout step 6 says.

**Tech Stack:**
- Python 3.10+ (stdlib + PyYAML), Bash and Docker;
- Hugo, Next.js, Vite and .NET 10 inside each repository's images;
- the status API is C# (.NET), `status/`;
- Renovate config (JSON5), unittest.

**Spec:** `docs/superpowers/specs/2026-10-22-self-hosted-git-and-ci-design.md`. Sections: "Every repository", "Sites", "Libraries", "Catalog", "Renovate", "Owner-tagged releases", "Rollout" steps 6–8.

**Not in this plan:** the machines and the window (plan 4, done). Pushing, merging, and switching GitHub Pages and Vercel off are the owner's, after the cutover, by Task 14's checklist.

## Decisions this plan makes (read first)

1. **One `yggdrasil.yml` per repository, read by every job of that repository.**
   - A repository can back two catalog entries. Examples:
     - a library (`repositories` entry: checks and NuGet) and its docs site (an application with `repository: <the library>`);
     - heimdall-api and its docs site.
   - So a job runs only **its catalog entry's `checks`**, plus the checks its `deploy.inputs` name when it deploys. Today it runs every check in the file.
   - Checks the file defines for another entry are left out of that job: no status, no run.
   - This is Task 2. It changes plan 3's planner, so one file serves both jobs without running everything twice.
2. **Branches are prepared, not pushed.** Every repository task works in `D:\Repositories\<repo>` on a new branch `feature/move-to-forgejo`, cut from `develop` (or from the default branch where there is no `develop`).
   - A clone with uncommitted changes, or one missing locally, is reported and skipped. Its row in the checklist says so.
   - Missing clones are cloned from GitHub first, read-only. These are github-readme-stats, gui-challenges, hugo-theme-nuno and landing-page.
3. **What leaves `.github/workflows`:**
   - every job running on `ubuntu-*`;
   - the Pages and Vercel workflows;
   - `branch-policy.yml`;
   - `dependabot.yml`.

   What stays:
   - jobs on `windows-*` and `macos-*`, which keep their names (they become `actionsChecks`) and get the new triggers (`push` to `develop`, `main` and `release/*`; `pull_request`; `workflow_dispatch` with `tag` for release builds);
   - workflows that only publish to GitHub (none, after Task 3 moves the rest).
4. **Sites are served by one pinned static server image** for built sites, with a `/healthz` answer and no shell: `static-web-server`, pinned by version and digest.
   - Server-rendered Next.js uses `node` (pinned) with `next start`, and github-readme-stats uses `node` too.
   - Hugo and Vite build in a pinned builder stage.
5. **Docs root:** `docs.artur-rios.tech/` redirects to `/dotnet-libraries/`. That index gains a section listing the other docs sites. The redirect is a Traefik router in dotnet-libraries' stack file, with no new application.
6. **Renovate:** one shared preset in yggdrasil, `renovate/default.json`, which each repository extends with `local>artur-rios/yggdrasil//renovate/default`.
   - It covers what each `dependabot.yml` covers: nuget, npm, github-actions, docker.
   - It adds a regex manager for `image:` lines in `yggdrasil.yml`.
   - Image updates in `yggdrasil.yml` wait for the owner, since the file is protected on `develop`.

## Global Constraints

- **Repositories:** 60 on GitHub (`artur-rios`).
- **Kinds,** as in the spec's "Every repository" table:
  - deployed applications: heimdall-api, heimdall-ui, fortuna-api, fortuna-ui, chimera;
  - sites;
  - 12 libraries: dotnet-configuration, -data, -extensions, -jwt, -logging, -mediator, -messaging, -output, -test-util, -util, -validation, -webapi-util;
  - CI and releases, not deployed: alexandria-api, alexandria-ui, cerberus-api, cerberus-ui, maestro, orpheus, personal-lab, ai-coding-skills, coding-challenge-library, dotnet-libraries, reveal.js;
  - code only;
  - archived: dotnet-common, eocp-ecommerce-system-api, -email-templates, -ui;
  - forks.
  - A repository can be of more than one kind.
- **Sites and their addresses:**
  - tech-craftsmen-company-page: `techcraftsmen.net` and `www.techcraftsmen.net`, release flow homologation and production;
  - personal-page: `www.artur-rios.tech`, with `artur-rios.tech` redirecting to it, release flow development, homologation and production;
  - personal-blog: `blog.artur-rios.tech`, production on `main`;
  - personal-lab: `lab.artur-rios.tech`, production on `main`, plus release tags;
  - under-construction-template: `placeholder.artur-rios.tech`, production on `main`;
  - github-readme-stats: `readme-stats.techcraftsmen.net`, production on `main`;
  - docs sites (heimdall-docs, fortuna-docs, alexandria-docs, orpheus docs, heimdall-api docs, dotnet-libraries and the 12 library docs): `docs.artur-rios.tech/<repository>`, production on pushes to `main`.
- **Links:**
  - references to `github.com/artur-rios/<repo>` stay (they still resolve, through the mirror);
  - `artur-rios.github.io/<repo>` and `*.vercel.app` change to the new addresses, including `PackageProjectUrl` and READMEs;
  - no redirects.
- **Libraries:** a bare version tag `1.2.3` on `main`, pushed by the owner, publishes to nuget.org from the release agent. GitHub Packages is dropped. Versioning and tag conventions are kept.
- **Vercel settings go:** `VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV` become build arguments passed by yggdrasil's deploy, and `vercel.json` is deleted.
- **github-readme-stats** reads GitHub with a read-only token, stored as an application secret.
- **Credentials:**
  - check credentials go in the catalog's `credentials`, e.g. heimdall-api's `reportgenerator-license`;
  - release-build credentials go in `releaseCredentials` (maestro's signing key, orpheus' keystore);
  - `nuget-api-key` is used only on the release agent's push step.
- **Branch rules:** repositories with a `branch-policy` workflow today get the release model; the others get `basic`.
- **Tests:**
  - in WSL;
  - repository checks run in their declared images;
  - site images built and probed;
  - shellcheck stays silent;
  - test names follow `test_given_…_when_…_then_…`;
  - commits are lowercase Conventional, ≤50 characters, both in yggdrasil and in each repository.

## Review Focus

1. **A job running another entry's checks, or missing its own.** A library's docs job must run only its deploy input (coverage), never the library's whole suite. Each job's statuses must still satisfy that entry's required checks (Task 2 tests).
2. **A dropped Linux job.** Every `ubuntu-*` job in today's workflows must map to a check in `yggdrasil.yml` or be written down as dropped, with the reason. Task 1's inventory lists them, and each repository task's verification compares against it (Tasks 1, 8–13).
3. **A site that builds but serves the wrong base path,** e.g. Hugo's `baseURL` still pointing at `artur-rios.github.io`, or a docs site at `/` instead of `/<repo>/`. Each site task fetches a deep link and an asset from the built image under the real path prefix (Tasks 3, 12–13).
4. **A link left pointing at Pages or Vercel.** `relink.py --check` must report zero in each prepared branch, NuGet metadata included (Tasks 5, 8–13).
5. **A secret a workflow used today that isn't in the catalog.** The inventory lists every `secrets.*` per repository. Each one is mapped to a catalog credential or written down as no longer needed (Tasks 1, 7).

---

### Task 1: `scripts/repo_inventory.py` and the migration checklist

**Files:** Create `scripts/repo_inventory.py`, `scripts/test_repo_inventory.py`, `docs/migration-checklist.md`.

**Interfaces:**
- `python3 scripts/repo_inventory.py <repos-dir> [--github-owner artur-rios]`.
  - It reads every git clone under `<repos-dir>` and, with a token, the GitHub repository list (forks, archived, Pages, missing clones), through `migrate.github_repositories`.
  - It prints a Markdown table, one row per repository: kind(s) from the spec's table; each workflow job as `<workflow>/<job> on <runs-on>`, marked `→ check <name>` / `stays (Actions)` / `goes (Pages|Vercel|branch-policy|dependabot)`; the `secrets.*` it uses; Pages or Vercel (a `vercel.json`, a Pages deploy step); `dependabot.yml` ecosystems; branch model (`release` with a branch-policy workflow, else `basic`); local clone state (clean, dirty, missing).
- `classify_job(runs_on: str, workflow: str, steps: list) -> str` is the rule of decision 3, as a pure function.
- `docs/migration-checklist.md` is the generated table, plus a "Prepared branch" and a "Moved" column. Tasks 8–13 fill the first, and the owner fills the second after the cutover.

- [ ] **Step 1: Failing tests** (fixture repositories under a temp directory):

```python
def test_given_an_ubuntu_job_when_classified_then_it_becomes_a_check(self):
def test_given_a_windows_job_when_classified_then_it_stays_on_actions(self):
def test_given_a_pages_deploy_workflow_when_classified_then_it_goes(self):
def test_given_a_workflow_using_secrets_when_inventoried_then_each_secret_is_listed(self):
def test_given_a_dirty_clone_when_inventoried_then_its_state_is_dirty(self):
def test_given_a_repository_missing_locally_when_inventoried_then_it_is_marked_missing(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5:** Run it against `D:\Repositories` (the GitHub part only if a token is at hand). Write `docs/migration-checklist.md`, and check the kinds against the spec's table (a repository the tool and the spec disagree on gets a note).
- [ ] **Step 6: Commit** — `feat: inventory every repository's workflows`, then `docs: add the migration checklist`.

---

### Task 2: A job runs its own entry's checks

**Files:** Modify `scripts/pipeline_config.py`, `scripts/test_pipeline_config.py`, `jenkins/library/vars/yggdrasilPipeline.groovy` (only if the planner's caller filters there), `scripts/test_pipeline_library.py`, `scripts/test_jenkins_live.py`, `docs/catalog.md`.

**Interfaces:**
- `plan_checks(...)` plans `entry["checks"]` ∪ (deploying ? `deploy.inputs` : ∅).
  - Every name must be a check in the config; a missing one is an error, as today.
  - Checks the config defines but this job doesn't need are not in the plan: no status, no run.
- `validate_against` stays as it is: every catalog check must exist in the config. The new rule: a config check that no catalog entry of this repository names is a **warning** line in `describe`'s output (`"warnings": [...]`), not an error.
- When `describe` and `plan` know the entries that share a repository, they can say which entry's checks were left out. Use `catalog.applications` and `catalog.repositories` with their `repository` field.

- [ ] **Step 1: Failing tests:**

```python
def test_given_two_entries_on_one_repository_when_planned_then_each_runs_only_its_own_checks(self):
def test_given_a_deploying_docs_job_when_planned_then_only_its_deploy_input_runs(self):
def test_given_a_config_check_no_entry_names_when_described_then_it_is_a_warning(self):
```
  Live (`test_jenkins_live.py`): a repository with a `repositories` entry (checks `unit`, `coverage`) and a site application (`checks: []`, `deploy.inputs: [coverage]`) on the same repository. A push to `main` runs `unit` and `coverage` in the library job, and only `coverage` in the docs job, which deploys with the archive.
- [ ] **Step 2–4:** FAIL, implement, PASS (unit, static and live).
- [ ] **Step 5: Commit** — `feat: run only a job's own checks`.

---

### Task 3: Site templates, the Renovate preset

**Files:**
- Create:
  - `templates/site-static/` (Dockerfile, `yggdrasil.yml`, `README.md`);
  - `templates/site-hugo/`, `templates/site-vite/`, `templates/site-next-static/`, `templates/site-next-server/`, `templates/site-node-api/`, each with the same files;
  - `templates/library/` (`yggdrasil.yml`, `ci/test.sh`, `ci/coverage.sh`, `ci/pack.sh`, docs-site Dockerfile);
  - `templates/stacks/site.proxy.yml`, `templates/stacks/site-path.proxy.yml`, `templates/stacks/site-apex-redirect.proxy.yml`;
  - `renovate/default.json`;
  - `scripts/test_templates.py`.
- Modify `templates/application/` (point at the new templates) and `docs/catalog.md`.

**Interfaces:**
- Each Dockerfile takes build arguments `GIT_COMMIT` and `YGG_ENVIRONMENT`, which replace `VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV`, and `PUBLIC_PATH` for docs sites.
  - It serves on port 8080, answers `/healthz` with 200, and runs as a non-root user.
  - The static images use the static-web-server image, pinned by digest.
  - Hugo uses `--baseURL https://<host><path>/` from the arguments.
- `deploy.sh` passes the build arguments. Check how stacks pass args today. If deploy.sh doesn't pass them yet, add the smallest change: export `GIT_COMMIT`, already known to the deploy, and `YGG_ENVIRONMENT`, and let Compose `build.args` read them. Test it in `test_deploy.py`.
- **Stack templates:**
  - `site.proxy.yml`: router `Host(${YGG_PUBLIC_HOST})`.
  - `site-path.proxy.yml`: `Host(...) && PathPrefix(${YGG_PUBLIC_PATH})`, without stripping the prefix, since the site is built for it.
  - `site-apex-redirect.proxy.yml`: an extra router on the bare domain with a `redirectRegex` to `www.`.
- **The library template's `yggdrasil.yml`:**
  - checks `test` (format, build, test categories, tests) and `coverage` (archive `coverage-report/**`, credential `REPORTGENERATOR_LICENSE` from `reportgenerator-license`; without the credential, on `renovate/*`, it falls back to the free report and says so);
  - `publish.nuget` (pack with `ci/pack.sh`, packages `artifacts/*.nupkg`);
  - `deploy.inputs: [coverage]` for the docs application.
- **`renovate/default.json`** (decision 6): `extends: ["config:recommended"]`, `baseBranches: ["develop"]`, the four ecosystems, and a `customManagers` regex for `image: <name>:<tag>@sha256:<digest>` in `yggdrasil.yml`.

- [ ] **Step 1: Failing tests.** Static tests: each template's `yggdrasil.yml` validates with `pipeline_config.load_config`, the stack templates render under `docker compose config` with sample env, and the Renovate preset is valid JSON with the regex matching sample lines. Live tests, gated by `YGG_TEMPLATE_TESTS=1`: each site template builds a sample site and answers `/healthz` and a deep link under its path.

```python
def test_given_each_site_template_when_built_then_it_answers_healthz_and_a_deep_link_under_its_path(self):
def test_given_the_library_template_when_loaded_then_it_validates(self):
def test_given_the_renovate_preset_when_its_image_regex_runs_then_a_pinned_image_line_matches(self):
def test_given_a_deploy_when_built_then_git_commit_and_environment_are_build_args(self):   # test_deploy.py
```
- [ ] **Step 2–4:** FAIL, implement, PASS (static and gated live).
- [ ] **Step 5: Commit** — `feat: add site and library templates` (and `feat: pass the commit and environment to builds`).

---

### Task 4: The status API shows each application's real address

**Files:** Modify `status/src/**` (the URL builder), `status/tests/**`, `docs/status-api.md`, `console/` only if it builds URLs itself.

**Interfaces:**
- **The `url` field:** `https://<host><hostSuffix>.<domain><path>`.
  - `domain` is the application's `domain`, else the host's `YGGDRASIL_DOMAIN`.
  - With `apex` and no host, the address is `https://<domain><path>`.
  - Platform components keep `https://<host>.<YGGDRASIL_DOMAIN>`.
- Use the same rules as `catalog.py public-host` / `public-path` (plan 3), and say so in a comment. The two must stay in step; a fixture-based test that compares them is welcome.
- The contract stays version 2. Document the field's new formation in `docs/status-api.md`.

- [ ] **Step 1: Failing tests** (the status API's own test project, run with `dotnet test` in a .NET 10 SDK container):

```
Given_AnApplicationWithADomain_When_Reported_Then_ItsUrlIsUnderThatDomain
Given_ADocsSiteWithAPath_When_Reported_Then_ItsUrlEndsWithThePath
Given_AnApexSite_When_Reported_Then_ItsUrlIsTheBareDomain
```
  Use the project's own naming convention if it differs: read `status/tests`.
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: report each application's real address`.

---

### Task 5: `scripts/relink.py`

**Files:** Create `scripts/relink.py`, `scripts/test_relink.py`.

**Interfaces:**
- `python3 scripts/relink.py <repo-dir> [--check]`.
- It builds the mapping from the catalog: each site's old address (`artur-rios.github.io/<repo>[/...]`, `<project>.vercel.app`, from a small fixed table in the script) maps to `catalog.py public-host` + `public-path`.
- It rewrites text files tracked by git, skipping binaries, lockfiles and `CHANGELOG.md` history lines, and prints each change as `file:line old → new`.
- `--check` changes nothing and exits 1 when anything would change.
- `PackageProjectUrl` in `*.csproj` and `Directory.Build.props` is covered.
- `github.com/artur-rios/<repo>` is never touched.

- [ ] **Step 1: Failing tests:**

```python
def test_given_a_pages_link_when_relinked_then_it_points_at_the_docs_host_and_path(self):
def test_given_a_vercel_link_when_relinked_then_it_points_at_the_sites_new_host(self):
def test_given_a_github_repository_link_when_relinked_then_it_is_left(self):
def test_given_a_package_project_url_when_relinked_then_it_is_rewritten(self):
def test_given_check_mode_with_a_stale_link_when_run_then_it_exits_1_and_changes_nothing(self):
```
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** — `feat: rewrite links to the new addresses`.

---

### Task 6: The catalog and the sites' stack files

**Files:** Modify `catalog.yaml` and `scripts/test_catalog.py` (a test that the real catalog validates and lists every repository of the checklist with its kind). Create `stacks/<site>.yml` and `stacks/<site>.proxy.yml` for every site, from Task 3's templates.

**Interfaces:**
- **Top-level blocks:** `forge` (type forgejo, url `https://git.techcraftsmen.net`, developers `[claude]`) and `mirror` (`github: artur-rios`, `releases: true`). These are the values the runbook sets at 5.10. With this task they are in the file, so the runbook step becomes "check they're there".
- **`applications`:** the five deployed applications as today, with:
  - `credentials` (heimdall-api: `reportgenerator-license`);
  - `actionsChecks` from Task 1's inventory (the Windows/macOS job names);
  - `domain: techcraftsmen.net` where the spec says;
  - every site, with `domain`, `host`/`apex`/`path`, `environments` overrides (production only, `trigger: branch`, `branches: main`, unless the spec's table says release flow), `checks` (empty for docs sites) and `repository` where the application id differs from the repository (`<lib>-docs` → `<lib>`).
- **`repositories`:**
  - the 12 libraries: `release: owner-tag`, `tags: "[0-9]*"`, `checks: [test, coverage]`, `credentials: [reportgenerator-license]`;
  - the CI repositories, with their checks, `actionsChecks`, `release` and `releaseCredentials` (maestro, orpheus);
  - yggdrasil itself;
  - `rules: release` only where a branch-policy workflow exists today.
- **`python3 scripts/catalog.py validate`** passes. `catalog.py repository-names` lists exactly the repositories that get Jenkins jobs, as in the checklist.

- [ ] **Step 1: Failing test:** `test_given_the_real_catalog_when_validated_then_every_checklist_repository_with_ci_has_an_entry`.
- [ ] **Step 2–4:** Write the entries and stacks; `catalog.py validate`, `docker compose config` for each stack with sample env; PASS.
- [ ] **Step 5: Commit** — `feat: catalog every repository and site`.

---

### Task 7: The docs index and the credentials list

**Files:**
- Modify `stacks/dotnet-libraries-docs.proxy.yml` (Task 6) to add the root redirect of decision 5.
- Modify `docs/hosts.md`'s credentials table: every secret Task 1 found, mapped to a catalog credential id, or "dropped: <why>". Examples of dropped secrets: a `GITHUB_TOKEN` used for Pages, and `VERCEL_TOKEN`.
- Modify `docs/migration-checklist.md`'s secrets column.

**Interfaces:** `docs.artur-rios.tech/` answers 301 to `/dotnet-libraries/`, tested by rendering the router rule with `docker compose config`.

- [ ] **Steps:** write the redirect; test the rendered labels; complete the credentials mapping; commit `feat: redirect the docs root to the index` and `docs: map every workflow secret`.

---

### Tasks 8–13: The repositories

Each repository task follows the same steps, which are spelled out once here.

**Per repository:**
1. **Branch.** In `D:\Repositories\<repo>`, check that the clone is clean, `git fetch`, and cut `feature/move-to-forgejo` from `origin/develop` (else `origin/<default>`).
2. **Port the jobs.** For each job Task 1 marked `→ check <name>`, port its steps into `ci/<name>.sh`, keeping the same commands and tool versions. List it in `yggdrasil.yml` `checks.<name>` with the image the job's setup steps imply: `mcr.microsoft.com/dotnet/sdk:10.0` for `actions/setup-dotnet 10.0.x`, `node:<version>` for `setup-node`, and so on, each pinned by digest. Add `docker: true` where the tests use Testcontainers, `paths` where the workflow had `paths:` filters, and `junit` and `archive` where it uploaded results. Linux jobs that only built for GitHub Pages or Vercel go away, replaced by the site's Dockerfile.
3. **Release files and publishing.** `release.build`, `release.windows` and `release.validate` come from the repository's `release*.yml`: Linux steps into `ci/release-*.sh`, Windows and macOS jobs kept on GitHub as the `windows` workflow, signing secrets as `releaseCredentials`. For libraries, `publish.nuget` comes from `publish-package.yml`.
4. **Sites.** Add the site's Dockerfile from the matching Task 3 template. Drop the Vercel settings: delete `vercel.json`, and turn `VERCEL_*` into the build arguments.
5. **Workflows.** Trim `.github/workflows` per decision 3; the jobs that stay get the new triggers. Delete `.github/dependabot.yml`. Add `renovate.json` (`{"extends": ["local>artur-rios/yggdrasil//renovate/default"]}`, plus repository-specific rules carried over from `dependabot.yml`: ignores, groups, schedules).
6. **Links and contributing.** Run `python3 /mnt/d/Repositories/yggdrasil/scripts/relink.py .`, then `--check` must exit 0. README and CONTRIBUTING say the repository lives at `git.techcraftsmen.net/artur-rios/<repo>` and that GitHub is a mirror.
7. **Verify.**
   - `python3 …/pipeline_config.py describe <job-id> .` with the catalog from Task 6 shows no errors.
   - Each check runs in its image from the repository root (`docker run --rm -v "$PWD:$PWD" -w "$PWD" <image> sh -c "<run>"`) and passes. A check that needs a credential runs without it and says so in the report.
   - A site image builds and answers `/healthz` and a deep link under its path.
   - Compare against Task 1's row: every `→ check` job is a check.
8. **Commit and record.** Commit in the repository (`chore: move ci and releases to jenkins` and the like). Don't push. Fill the checklist's "Prepared branch" column with the branch and its head commit.

### Task 8: yggdrasil itself and the deployed applications
heimdall-api (with its docs application `heimdall-api-docs`), heimdall-ui, fortuna-api, fortuna-ui, chimera (release files: Linux on the release agent, Windows and macOS through `release.windows`), and yggdrasil itself (its `ci.yml` Linux jobs become checks; `release.yml`'s Windows job stays).

### Task 9: The 12 libraries and dotnet-libraries
Batch these from `templates/library`. Use one small script, written for this task in the scratchpad and not committed, to apply the template, then review each repository's differences by hand: solution path, test categories and coverage filters differ. Each library also gets its docs application (`<lib>-docs`, `deploy.inputs: [coverage]`). dotnet-libraries is the index site: its `verify-versions.yml` becomes a check, and its index gains the section from decision 5.

### Task 10: CI repositories without releases
alexandria-api, cerberus-api, cerberus-ui, ai-coding-skills, coding-challenge-library and reveal.js (a fork: keep `upstream` intact and change only CI files).

### Task 11: CI repositories with release files
alexandria-ui, maestro (its update-manifest signing key moves to `releaseCredentials` and `release.build`), orpheus (its Android keystore, its `release.windows`, and its docs application) and personal-lab (owner-tag releases and its site).

### Task 12: The Vercel sites
tech-craftsmen-company-page (static Next.js export, release flow), personal-page (server-rendered Next.js, release flow, apex redirect, `VERCEL_*` turned into build arguments), personal-blog (Hugo), under-construction-template (Vite) and github-readme-stats (Node, cloned first; its GitHub token as an application secret in the catalog's env).

### Task 13: The remaining docs sites and the profile
heimdall-docs, fortuna-docs and alexandria-docs (Pages, served at their path), and the `artur-rios` profile repository (links, the readme-stats address).

---

### Task 14: The moving guide and the final check

**Files:** Create `docs/moving-repositories.md`. Modify `docs/migration-checklist.md`, `docs/cutover.md` (link it from rollout step 6), `README.md` and `CHANGELOG.md`.

**Interfaces:** For each prepared branch, after the cutover, the guide gives the owner these steps:
1. Push it to Forgejo.
2. Open the pull request.
3. Check Jenkins' results. The first pull request can't satisfy both the old rules and the new ones, so the owner merges it with the override once Jenkins' checks are green.
4. Make the repository's first release, publish or site deploy. That is its end-to-end test.
5. For a site, switch the old host off once the new address serves it:
   - Pages: Settings → Pages → None;
   - Vercel: delete the project.
   - Then delete the DNS CNAMEs that pointed at Vercel, and drop `--skip-caa artur-rios.tech` once both `www.` and `blog.` have moved (plan 4's ruling).
6. Mark the checklist's "Moved" column.

The order follows the spec: deployed applications, then yggdrasil, then libraries, CI repositories and sites.

- [ ] **Step 1: Write it.** Check every command against the code: `git push forgejo`, the URLs, and `catalog.py` commands.
- [ ] **Step 2:** Re-run `repo_inventory.py` and make sure every repository with CI has a prepared branch, or a written reason why not.
- [ ] **Step 3:** Run the full unit suite, the gated template and live suites, and shellcheck.
- [ ] **Step 4: Commit** — `docs: add the guide to moving each repository`.
