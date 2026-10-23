# Git-Server Interface and Catalog Implementation Plan (plan 2 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let yggdrasil talk to either git server (Forgejo, the default, or GitHub) through one
interface (`scripts/forge.sh` and one rules script), and give the catalog the fields the rest of the design
needs (`forge`, `mirror`, `repositories`, `actionsChecks`, `credentials`, `domain`, `path`, `apex`).

**Architecture:** `scripts/catalog.py` validates and serves the new fields. `scripts/forge.sh` exposes the
write and read commands the pipeline needs, and dispatches to a GitHub or a Forgejo implementation by
`FORGE_TYPE`. `scripts/github.sh` keeps only what is about GitHub Actions and delegates the rest to `forge.sh`,
so today's Jenkins pipeline keeps working unchanged. `scripts/rules.py` computes the branch/tag model once
from the catalog and applies it to either server (rulesets on GitHub; branch and tag protections, repository
settings and Jenkins webhooks on Forgejo). Tests use fake `curl`/HTTP backends, plus a gated suite against a
real Forgejo container.

**Tech Stack:** Bash, curl, jq (forge.sh, as github.sh today); Python 3.10+ with the standard library and
PyYAML only (`urllib` for HTTP: no new packages); unittest; Forgejo 16.0.6 (API `/api/v1`); GitHub REST
`2022-11-28`.

**Spec:** `docs/superpowers/specs/2026-10-22-self-hosted-git-and-ci-design.md`: sections "Git server:
Forgejo or GitHub", "Forgejo › Branch and tag rules" and "Accounts and repositories", "Catalog", "Every
repository", "Sites" (the catalog fields only), "Implementation plans" item 2.

**Not in this plan:** the pipeline using any of this (Jenkins checks, statuses, the skip, releases: plan 3);
the Gitea plugin and Job DSL (plan 3); creating the Forgejo accounts and importing repositories (plan 4);
switching the owner's `catalog.yaml` to Forgejo (plan 4's cutover; this plan leaves its `forge` unset, so it
stays GitHub until then); sites' stack files and Traefik routing (plan 5).

## Global Constraints

- **Two git servers, one interface.** `forge.type` is `forgejo` or `github`. A catalog **without** a `forge`
  block is GitHub, exactly as today (existing installations keep working unchanged); the docs and the
  example catalog present `forgejo` as the default for new installations.
- Every feature is written once against `forge.sh`'s commands; `github.sh` keeps the GitHub Actions commands
  (`wait-checks`, the release-artifacts workflow flow) and nothing else of its own.
- Rules model (spec "Branch and tag rules"), for repositories that follow the release model:
  - `develop`: no direct push, no force push, no deletion; required `branch-policy` + `checks` +
    `actionsChecks`; merges by the owner and the catalog's `forge.developers`; squash or merge commit.
  - `main`: no direct push, no force push, no deletion; required `branch-policy` + `checks` +
    `deploy/<environment>` for each release environment; merges only by `forge.jenkinsUser` (applications)
    or the owner (repositories released by owner tags).
  - `release/*`: only the owner may push.
  - Protected file `Jenkinsfile` on `develop` and `main` (Forgejo): only the owner can merge a change to it.
  - Version tags: in applications only `forge.jenkinsUser` creates `v*`; in owner-tagged repositories only the
    owner creates their pattern (`v*`, or `[0-9]*` for libraries); nobody moves or deletes them.
  - Repository settings: default branch `develop`; delete head branch after merge.
  - Repositories with `rules: basic`: `develop` and `main` without force push or deletion, nothing else.
- On GitHub: Jenkins' contexts (`branch-policy`, `checks`, `deploy/…`) are **not** bound to an integration;
  `actionsChecks` stay bound to GitHub Actions (integration id 15368).
- Forgejo branch protections leave repository admins able to merge regardless (`apply_to_admins: false`): the
  owner's override for emergencies and GitHub outages.
- Forgejo webhooks to Jenkins: `<JENKINS_URL>/gitea-webhook/post`, JSON, with a secret, one per repository,
  updated in place on re-runs.
- Tokens never appear in a process's arguments (curl reads its auth header from a config on stdin; Python
  sends it in a header), never in logs or `--dry-run` output.
- Python: standard library + PyYAML. Shell: shellcheck-clean (`.shellcheckrc` exists; `shellcheck
  scripts/*.sh scripts/ygg` must stay silent).
- Tests: `python3 -m unittest discover -s scripts` in WSL Ubuntu (POSIX-only tests skip on Windows), names
  `test_given_<condition>_when_<action>_then_<outcome>`; the real-Forgejo suite runs only with
  `YGG_FORGEJO_TESTS=1` and Docker.
- Commit subjects: lowercase Conventional Commits, ≤50 chars; body wrapped at 72.

## Review Focus

1. **A token showing up where it shouldn't**: in `ps` (curl arguments), a log, an error message, or
   `--dry-run` output (Tasks 3, 6, 7 tests).
2. **Running the rules script twice**: no duplicate branch protections, tag protections or webhooks; the second
   run changes nothing it doesn't need to (Tasks 6, 7, 8 tests).
3. **A server error answer** (401, 403, 404, 409, 422): the command fails naming the call and the server's
   message; only the documented "already done" cases succeed (a branch already deleted) (Tasks 3, 4 tests).
4. **An existing GitHub installation** (no `forge` block, `checks` naming Actions jobs): `github.sh`'s commands,
   the catalog CLI and the rules script behave as today, except that Jenkins-reported contexts are no longer
   bound to GitHub Actions (Tasks 1, 3, 6 tests).
5. **Names that need care in URLs**: a repository with a dot (`reveal.js`), a branch with a slash
   (`release/1.2.3`), a tag pattern with `*` — on both servers (Tasks 3, 4, 8 tests).

---

### Task 1: Catalog: `forge` and `mirror`

**Files:**
- Modify: `scripts/catalog.py` (validation, accessors, CLI)
- Modify: `scripts/test_catalog.py`
- Modify: `docs/catalog.md` (new "Git server" section)

**Interfaces:**
- Produces in `catalog.py`:
  - `forge(catalog: dict) -> dict` with keys `type` (`"forgejo"`|`"github"`), `url` (web base, no trailing
    slash; GitHub: `https://github.com`), `api` (GitHub: `https://api.github.com`; Forgejo: `<url>/api/v1`),
    `jenkinsUser` (default `"jenkins"`), `developers` (list of user names, default `[]`).
    No `forge` block → `{"type": "github", "url": "https://github.com", "api": "https://api.github.com",
    "jenkinsUser": "jenkins", "developers": []}`.
  - `mirror(catalog: dict) -> dict | None`: `{"github": <owner>, "releases": <bool, default True>}`, or `None`.
  - CLI: `catalog.py forge [type|url|api|jenkins-user]` (no argument: JSON of `forge()`), `catalog.py mirror
    [github|releases]` (no mirror: exit 1, "catalog.yaml has no mirror").
- Validation (messages name the field):
  - `forge.type` one of the two; `forge.url` required for `forgejo`, an `https://` URL without a path beyond
    `/`, no trailing slash kept; not allowed for `github` (GitHub Enterprise is out of scope: say so).
  - `forge.jenkinsUser`: a user name (`^[A-Za-z0-9][A-Za-z0-9._-]*$`); `forge.developers`: a list of such.
  - `mirror` only with `forge.type: forgejo`; `mirror.github` required (a GitHub owner name, `REPOSITORY`
    pattern); `mirror.releases` a bool.

- [ ] **Step 1: Write the failing tests** in `scripts/test_catalog.py`:

```python
def test_given_no_forge_block_when_read_then_it_is_github_as_before(self):
    self.assertEqual(catalog.forge(base_catalog())["type"], "github")
    self.assertEqual(catalog.forge(base_catalog())["api"], "https://api.github.com")

def test_given_forgejo_when_read_then_the_api_is_under_its_url(self):
    c = base_catalog(forge={"type": "forgejo", "url": "https://git.techcraftsmen.net/"})
    self.assertEqual(catalog.forge(c)["url"], "https://git.techcraftsmen.net")
    self.assertEqual(catalog.forge(c)["api"], "https://git.techcraftsmen.net/api/v1")

def test_given_forgejo_without_a_url_when_validated_then_it_is_an_error(self):
def test_given_a_url_for_github_when_validated_then_it_is_an_error(self):
def test_given_a_plain_http_forgejo_url_when_validated_then_it_is_an_error(self):
def test_given_a_mirror_on_github_when_validated_then_it_is_an_error(self):
def test_given_a_mirror_when_read_then_releases_default_to_true(self):
def test_given_developers_when_read_then_they_are_listed(self):
    c = base_catalog(forge={"type": "forgejo", "url": "https://git.techcraftsmen.net", "developers": ["claude"]})
    self.assertEqual(catalog.forge(c)["developers"], ["claude"])
def test_given_the_cli_when_asked_for_the_forge_type_then_it_prints_it(self):
```

(`base_catalog(**top_level)` is a helper building a minimal valid catalog plus the given top-level keys; add
it if the file has none.)

- [ ] **Step 2: Run them to verify they fail**

Run: `wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/d/Repositories/yggdrasil && python3 -m unittest discover -s scripts -p test_catalog.py'`
Expected: FAIL (`AttributeError: module 'catalog' has no attribute 'forge'`).

- [ ] **Step 3: Implement the validation, `forge`, `mirror` and the two CLI commands**; update the module
  docstring's command list and its "Used by" line (`scripts/forge.sh`, `scripts/rules.py`).

- [ ] **Step 4: Document the fields in `docs/catalog.md`** ("Git server": `forge.type` and its default for an
  existing catalog, `url`, `jenkinsUser`, `developers`, `mirror`), with an example block for each type.

- [ ] **Step 5: Run the catalog tests and the full suite**

Run: `wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/d/Repositories/yggdrasil && python3 -m unittest discover -s scripts'`
Expected: PASS (the repository's own catalog, without `forge`, still validates).

- [ ] **Step 6: Commit**

```bash
git add scripts/catalog.py scripts/test_catalog.py docs/catalog.md
git commit -m "feat: add the git server to the catalog"
```

---

### Task 2: Catalog: `repositories` and the new application fields

**Files:**
- Modify: `scripts/catalog.py`, `scripts/test_catalog.py`, `docs/catalog.md`

**Interfaces:**
- Consumes: `forge()` (Task 1).
- Produces in `catalog.py`:
  - `repositories(catalog: dict) -> list[dict]`, each normalized to `{"id", "checks": [], "actionsChecks": [],
    "credentials": [], "release": "none"|"owner-tag", "tags": "v*", "rules": "release"|"basic"}`
    (defaults shown; `tags` only meaningful with `release: owner-tag`).
  - The application fields, validated: `actionsChecks` (list of non-empty strings), `credentials` (list of
    Jenkins credential ids, `^[A-Za-z0-9][A-Za-z0-9._-]*$`), `domain` (a host name, `HOST_NAME` with at least
    one dot), `path` (starts with `/`, no trailing `/`, no `..`), `apex` (bool).
  - CLI: `catalog.py repositories` (ids, one per line), `catalog.py repository-entry <id>` (JSON of the
    normalized entry; unknown id: exit 1).
- Validation:
  - `repositories` a list; each `id` matches `REPOSITORY`, unique, and not the repository of any application
    (an application's `repository`, else its `id`): "<id> is already an application's repository".
  - `release` one of `none`, `owner-tag`; `tags` a non-empty pattern of `[A-Za-z0-9.*_-]` characters; `rules`
    one of `release`, `basic`; `checks`/`actionsChecks`/`credentials` as for applications.
  - `checks` keeps its validation; its docs now say "the checks Jenkins reports".
- The status API ignores unknown keys (`CatalogLoader.cs` uses `IgnoreUnmatchedProperties`): no C# change. The
  Jenkins library's `resolve` reads environment options only, which this task doesn't change.

- [ ] **Step 1: Write the failing tests**:

```python
def test_given_a_repository_entry_when_read_then_its_defaults_are_filled(self):
    c = base_catalog(repositories=[{"id": "dotnet-util"}])
    self.assertEqual(catalog.repositories(c), [{"id": "dotnet-util", "checks": [], "actionsChecks": [],
        "credentials": [], "release": "none", "tags": "v*", "rules": "release"}])

def test_given_a_library_entry_when_read_then_its_tag_pattern_is_kept(self):
    c = base_catalog(repositories=[{"id": "dotnet-util", "release": "owner-tag", "tags": "[0-9]*"}])
    self.assertEqual(catalog.repositories(c)[0]["tags"], "[0-9]*")

def test_given_a_repository_that_is_also_an_applications_when_validated_then_it_is_an_error(self):
def test_given_two_repositories_with_the_same_id_when_validated_then_it_is_an_error(self):
def test_given_a_repository_with_a_dot_when_validated_then_it_is_valid(self):  # reveal.js
def test_given_an_unknown_release_kind_when_validated_then_it_is_an_error(self):
def test_given_an_application_path_without_a_leading_slash_when_validated_then_it_is_an_error(self):
def test_given_an_application_path_with_dot_dot_when_validated_then_it_is_an_error(self):
def test_given_an_application_domain_without_a_dot_when_validated_then_it_is_an_error(self):
def test_given_a_credential_id_with_a_space_when_validated_then_it_is_an_error(self):
def test_given_the_cli_when_asked_for_a_repository_entry_then_it_prints_it_as_json(self):
```

- [ ] **Step 2: Run them to verify they fail** (same command as Task 1). Expected: FAIL.

- [ ] **Step 3: Implement validation, `repositories`, the two CLI commands.**

- [ ] **Step 4: Document** in `docs/catalog.md`: the `repositories` list (one example each: a library with
  owner tags `[0-9]*`, a CI-only repository, yggdrasil itself), the application fields `actionsChecks`,
  `credentials` ("the only Jenkins credentials its checks may bind"), `domain`, `path`, `apex`, and the new
  meaning of `checks`. Say which later plans consume `domain`/`path`/`apex`.

- [ ] **Step 5: Run the full suite.** Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/catalog.py scripts/test_catalog.py docs/catalog.md
git commit -m "feat: add repositories and site fields to the catalog"
```

---

### Task 3: `scripts/forge.sh`, GitHub implementation, and `github.sh` delegating to it

**Files:**
- Create: `scripts/forge.sh`, `scripts/test_forge.py`
- Modify: `scripts/github.sh` (delegates `set-status`, `merge-pr`, `release`, `delete-branch` and the tag/draft
  steps of `publish-release` to `forge.sh`; keeps `wait-checks` and the workflow-dispatch flow)
- Modify: `scripts/test_github.py` only where a request's shape legitimately changed (say which in the report)

**Interfaces:**
- Consumes: `catalog.py forge type|api` and `catalog.py owner` (Task 1) when the environment doesn't say.
- Produces `scripts/forge.sh <command> …` (exit 0 ok, 1 error with `forge: <what> failed: <server message>` on
  stderr, 3 = the server keeps the branch, for `delete-branch` only):

| Command | Prints |
|---|---|
| `set-status <repo> <sha> <state> <context> <description> [target-url]` (state: `pending`, `success`, `failure`, `error`) | nothing |
| `get-statuses <repo> <sha>` | `<context>\t<state>` per line, the latest per context (the combined status) |
| `merge-pr <repo> <number> <sha> <title>` (merge commit, fails if the head moved from `<sha>`) | the merge commit's sha |
| `create-tag <repo> <tag> <sha>` | nothing |
| `create-release <repo> <tag> <sha> <title> [--draft] [--notes-file <file>]` | `<id>\t<html_url>` |
| `upload-release-file <repo> <release-id> <file>` | the file's download URL |
| `publish-release <repo> <release-id>` | the release's html_url |
| `delete-branch <repo> <branch>` | nothing; already gone → success with a note on stderr |

- Environment: `FORGE_TYPE` (else `catalog.py forge type`), `FORGE_API_URL` (else `catalog.py forge api`; for
  GitHub also `GITHUB_API_URL` as today), `FORGE_OWNER` (else `GITHUB_OWNER`, else `catalog.py owner`),
  `FORGE_TOKEN` (else, for GitHub, `GH_TOKEN`; required), `GITHUB_UPLOADS_URL` (default
  `https://uploads.github.com`).
- GitHub endpoints: as today's `github.sh` for statuses, merge (`PUT …/pulls/<n>/merge`, `merge_method:
  merge`, `sha`), tags (`POST …/git/refs`), releases (`POST …/releases`, `generate_release_notes: true` when no
  `--notes-file`), branch delete (`DELETE …/git/refs/heads/<branch>`, 422 "Reference does not exist" = already
  gone); new: `GET …/commits/<sha>/status` (combined), `POST <uploads>/repos/…/releases/<id>/assets?name=<file
  name>` (`Content-Type: application/octet-stream`, `--data-binary @file`), `PATCH …/releases/<id>`
  `{"draft": false}`.
- **The token never goes on curl's command line**: pass the headers with `--config -` on stdin (a here-string
  holding `header = "Authorization: Bearer …"`), so `ps` never shows it.
- Path segments (repository, branch) are URL-encoded except for `/` in a branch where the endpoint takes the
  rest of the path (`git/refs/heads/<branch>` on GitHub).

- [ ] **Step 1: Write the failing tests** in `scripts/test_forge.py`, with `test_github.py`'s fake `curl`
  pattern (copy it; also record the stdin config so tests can see headers):

```python
def test_given_github_when_setting_a_status_then_it_posts_to_the_commit(self):
def test_given_github_when_reading_statuses_then_the_latest_per_context_is_printed(self):
    # combined status with two contexts -> "branch-policy\tsuccess\ntest\tfailure\n"
def test_given_github_when_merging_then_it_prints_the_merge_commit(self):
def test_given_github_when_creating_a_release_without_notes_then_github_writes_them(self):
def test_given_github_when_creating_a_draft_release_then_it_prints_its_id_and_url(self):
def test_given_github_when_uploading_a_file_then_it_goes_to_the_uploads_host_with_its_name(self):
def test_given_github_when_publishing_then_the_draft_is_turned_off(self):
def test_given_a_branch_with_a_slash_when_deleting_on_github_then_the_ref_path_keeps_it(self):
def test_given_a_repository_with_a_dot_when_calling_github_then_the_path_is_right(self):
def test_given_any_command_when_run_then_the_token_is_not_in_curls_arguments(self):
def test_given_github_refuses_when_merging_then_the_reason_is_shown_and_it_fails(self):
def test_given_no_token_when_run_then_it_is_refused_before_any_request(self):
def test_given_no_forge_type_in_the_environment_when_run_then_the_catalogs_is_used(self):
```

  `scripts/test_github.py`'s existing tests must keep passing (they exercise the delegation).

- [ ] **Step 2: Run them to verify they fail**

Run: `wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/d/Repositories/yggdrasil && python3 -m unittest discover -s scripts -p "test_forge.py"'`
Expected: FAIL (no `forge.sh`).

- [ ] **Step 3: Implement `forge.sh`** with the dispatch (`github_<command>` / `forgejo_<command>` functions; the
  Forgejo ones may `die "not implemented yet"` until Task 4), and **rewrite `github.sh`'s** delegating commands as
  `exec "$root/scripts/forge.sh" <command> …` with `FORGE_TYPE=github`. Keep `github.sh`'s header documenting
  that it is the GitHub Actions side now.

- [ ] **Step 4: Run both suites and shellcheck**

Run: `wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/d/Repositories/yggdrasil && python3 -m unittest discover -s scripts -p "test_forge.py" && python3 -m unittest discover -s scripts -p "test_github.py" && ~/.local/bin/shellcheck scripts/*.sh scripts/ygg'`
Expected: PASS, no shellcheck output. (`~/.local/bin/shellcheck` is shellcheck-py; install with
`pip install --user shellcheck-py` if missing.)

- [ ] **Step 5: Commit**

```bash
git add scripts/forge.sh scripts/test_forge.py scripts/github.sh scripts/test_github.py
git commit -m "feat: add forge.sh with a github side"
```

---

### Task 4: `forge.sh`, Forgejo implementation

**Files:**
- Modify: `scripts/forge.sh`, `scripts/test_forge.py`

**Interfaces:**
- Consumes: Task 3's command table and conventions (same outputs and exit codes).
- Forgejo endpoints (API v1, auth header `Authorization: token <FORGE_TOKEN>`):

| Command | Call |
|---|---|
| `set-status` | `POST /repos/{o}/{r}/statuses/{sha}` `{state, context, description, target_url}` |
| `get-statuses` | `GET /repos/{o}/{r}/commits/{sha}/status` → `.statuses[] \| .context, .status` |
| `merge-pr` | `POST /repos/{o}/{r}/pulls/{n}/merge` `{"Do": "merge", "MergeTitleField": <title>, "head_commit_id": <sha>}` (empty answer), then `GET /repos/{o}/{r}/pulls/{n}` → `.merge_commit_sha` |
| `create-tag` | `POST /repos/{o}/{r}/tags` `{tag_name, target}` |
| `create-release` | `POST /repos/{o}/{r}/releases` `{tag_name, target_commitish, name, body, draft, prerelease: false}`; body from `--notes-file`, else empty |
| `upload-release-file` | `POST /repos/{o}/{r}/releases/{id}/assets?name=<file name>`, multipart field `attachment` → `.browser_download_url` |
| `publish-release` | `PATCH /repos/{o}/{r}/releases/{id}` `{"draft": false}` → `.html_url` |
| `delete-branch` | `DELETE /repos/{o}/{r}/branches/{branch}`; 404 = already gone (success); 403 because the branch is protected = exit 3 with "kept: <branch> is protected" |

  (Task 8 verifies every one of these against a real Forgejo 16.0.6; adjust there if one differs.)
- Branch names keep their `/` in `branches/{branch}` (Forgejo's route takes the rest of the path); repository
  and owner segments are URL-encoded.

- [ ] **Step 1: Write the failing tests** (one per command for `FORGE_TYPE=forgejo`, plus):

```python
def test_given_forgejo_when_merging_then_the_merge_commit_comes_from_the_pull_request(self):
def test_given_forgejo_when_the_merge_is_refused_then_its_message_is_shown(self):  # 405 "not mergeable"
def test_given_a_protected_branch_when_deleting_on_forgejo_then_it_exits_3_and_says_it_is_kept(self):
def test_given_a_missing_branch_when_deleting_on_forgejo_then_it_succeeds(self):
def test_given_forgejo_when_uploading_then_the_file_is_sent_as_the_attachment_field(self):
def test_given_forgejo_when_any_command_runs_then_the_token_is_not_in_curls_arguments(self):
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL ("not implemented yet").

- [ ] **Step 3: Implement the `forgejo_*` functions.**

- [ ] **Step 4: Run `test_forge.py`, `test_github.py` and shellcheck** (Task 3 Step 4 command). Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/forge.sh scripts/test_forge.py
git commit -m "feat: add the forgejo side of forge.sh"
```

---

### Task 5: `scripts/rules.py`: the model

**Files:**
- Create: `scripts/rules.py` (model only in this task), `scripts/test_rules.py`

**Interfaces:**
- Consumes: `catalog.forge`, `catalog.repositories`, `catalog.applications(catalog, deployable=True)`,
  `catalog.resolve` (release environments: `trigger == "release"`), `catalog.load` (Tasks 1–2).
- Produces:

```python
@dataclass(frozen=True)
class RepoRules:
    repo: str                    # repository name under the owner
    model: str                   # "release" | "basic"
    develop_checks: tuple        # required on develop (release model): branch-policy, checks, actionsChecks
    main_checks: tuple           # required on main: branch-policy, checks, deploy/<env>...
    actions_checks: frozenset    # the subset GitHub binds to GitHub Actions
    develop_mergers: tuple       # owner + forge.developers
    main_mergers: tuple          # (forge.jenkinsUser,) for applications, (owner,) otherwise
    release_pushers: tuple       # (owner,)
    tag_pattern: str             # "v*" or the repository's `tags`
    tag_creators: tuple          # (forge.jenkinsUser,) for applications, (owner,) for owner-tag repositories
    protected_files: tuple       # ("Jenkinsfile",)

def desired_rules(catalog: dict) -> list[RepoRules]   # applications first (catalog order), then repositories
```

  - `release: none` repositories with the release model get the same develop/main rules; their tags:
    `tag_creators = (owner,)`, `tag_pattern = "v*"`.
  - Checks keep catalog order, `branch-policy` first, no duplicates.

- [ ] **Step 1: Write the failing tests** (`scripts/test_rules.py`, with a small catalog in the test):

```python
def test_given_an_application_when_modelled_then_main_requires_its_release_deploys(self):
    r = rules.desired_rules(CATALOG)[0]
    self.assertEqual(r.main_checks, ("branch-policy", "test", "docker", "deploy/production"))
def test_given_an_application_when_modelled_then_only_jenkins_merges_main_and_tags(self):
def test_given_actions_checks_when_modelled_then_develop_requires_them_and_main_does_not(self):
def test_given_developers_when_modelled_then_they_may_merge_develop_only(self):
def test_given_a_library_when_modelled_then_the_owner_creates_its_numeric_tags(self):
    self.assertEqual((r.tag_pattern, r.tag_creators), ("[0-9]*", ("artur-rios",)))
def test_given_a_basic_repository_when_modelled_then_it_has_no_checks_or_mergers(self):
def test_given_a_check_listed_twice_when_modelled_then_it_is_required_once(self):
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL (no `rules`).

- [ ] **Step 3: Implement `RepoRules` and `desired_rules`.**

- [ ] **Step 4: Run them.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/rules.py scripts/test_rules.py
git commit -m "feat: model the branch rules from the catalog"
```

---

### Task 6: `rules.py`, GitHub side, and the `github/rulesets.py` shim

**Files:**
- Modify: `scripts/rules.py`, `scripts/test_rules.py`
- Modify: `github/rulesets.py` (becomes a shim: prints that the script moved, then runs `scripts/rules.py`
  with the same arguments)

**Interfaces:**
- Consumes: `RepoRules`, `desired_rules` (Task 5).
- Produces:
  - `class Api` (`scripts/rules.py`): `Api(base: str, token: str, scheme: str)` with
    `request(method: str, path: str, body: dict | None = None) -> dict | list | None`; raises
    `ApiError(method, path, status, message)` on a non-2xx answer; the token only in the `Authorization`
    header. GitHub token: `GH_TOKEN`, else the output of `gh auth token`.
  - `github_rulesets(r: RepoRules) -> list[dict]`: the ruleset payloads (names `develop`, `main`,
    `release tags`, as today, so existing rulesets are updated in place), with:
    - `required_status_checks`: Jenkins contexts without `integration_id`; `actions_checks` with
      `integration_id: 15368`;
    - basic model: only `deletion` and `non_fast_forward` on develop and main, no tags ruleset;
    - owner-tag repositories: the tag ruleset also has `creation`, for `refs/tags/<tag_pattern>`;
    - admin bypass as today. GitHub has no per-file merge restriction: `protected_files` is not applied there
      (the Jenkins trust rule in plan 3 covers it) — say so in a comment and the docs.
  - `apply_github(api: Api, owner: str, r: RepoRules, dry_run: bool) -> list[str]` (lines of what it did or
    would do): repository settings (`default_branch: develop`, `delete_branch_on_merge: true`), rulesets
    created or updated by name, the old `default` ruleset removed, as today.
  - CLI: `python3 scripts/rules.py [--dry-run] [<repo>…]`; with `forge.type: github` it runs the GitHub side;
    `--dry-run` prints payloads without sending any write (GETs still run).

- [ ] **Step 1: Write the failing tests** (a fake `Api` recording calls):

```python
def test_given_jenkins_checks_when_github_rulesets_are_built_then_they_are_not_bound_to_an_app(self):
def test_given_actions_checks_when_github_rulesets_are_built_then_they_are_bound_to_github_actions(self):
    # {"context": "Safari", "integration_id": 15368} on develop only
def test_given_an_existing_ruleset_when_applied_then_it_is_updated_in_place(self):  # PUT, not POST
def test_given_rules_applied_twice_when_nothing_changed_then_no_ruleset_is_created_again(self):
def test_given_an_owner_tag_repository_when_built_then_only_admins_create_its_tags(self):
def test_given_a_basic_repository_when_built_then_only_deletion_and_force_push_are_blocked(self):
def test_given_dry_run_when_applied_then_no_write_is_sent_and_no_token_is_printed(self):
def test_given_the_old_rulesets_py_when_run_then_it_runs_rules_py(self):
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement the GitHub side, the CLI and the shim.**

- [ ] **Step 4: Compare with today's output**: for the repository's own catalog, `--dry-run` payloads equal
  today's `github/rulesets.py --dry-run` except the removed `integration_id` on Jenkins contexts. Write that
  comparison as a test (`test_given_todays_catalog_when_built_then_only_the_binding_changed`) by building
  today's payloads from the old module's functions before deleting them (or a fixture copied from its output).

- [ ] **Step 5: Run the full suite.** Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/rules.py scripts/test_rules.py github/rulesets.py
git commit -m "feat: apply the branch rules on github"
```

---

### Task 7: `rules.py`, Forgejo side: settings, protections, tags, webhooks

**Files:**
- Modify: `scripts/rules.py`, `scripts/test_rules.py`

**Interfaces:**
- Consumes: `Api`, `RepoRules`, the CLI from Task 6.
- Produces `apply_forgejo(api: Api, owner: str, r: RepoRules, webhook: dict | None, dry_run: bool) -> list[str]`:
  - Repository settings: `PATCH /repos/{o}/{r}` `{default_branch: "develop", default_delete_branch_after_merge:
    true, allow_merge_commits: true, allow_squash_merge: true, allow_rebase: false, allow_rebase_explicit:
    false, default_merge_style: "merge"}`.
  - Branch protections (`GET /repos/{o}/{r}/branch_protections`, then `POST` or `PATCH …/{rule_name}`),
    `rule_name` = the branch pattern:
    - `develop`: `enable_push: false`, `enable_merge_whitelist: true`, `merge_whitelist_usernames`:
      `develop_mergers`, `enable_status_check: true`, `status_check_contexts`: `develop_checks`,
      `required_approvals: 0`, `protected_file_patterns: "Jenkinsfile"`, `apply_to_admins: false`.
    - `main`: the same with `main_mergers` and `main_checks`.
    - `release/*`: `enable_push: true`, `enable_push_whitelist: true`, `push_whitelist_usernames`:
      `release_pushers`, `enable_merge_whitelist: false`, `apply_to_admins: false`.
    - basic model: `develop` and `main` with `enable_push: true` (force push and deletion are blocked by any
      protection), no checks, no whitelist.
  - Tag protection (`GET/POST/PATCH /repos/{o}/{r}/tag_protections`, matched by `name_pattern`):
    `{name_pattern: tag_pattern, whitelist_usernames: tag_creators}` (release model only).
  - Webhook (when `webhook` is given: `{"url": "<JENKINS_URL>/gitea-webhook/post", "secret": …}`):
    `GET /repos/{o}/{r}/hooks`, matched by `config.url`; `POST` or `PATCH` `{type: "gitea", active: true,
    events: ["create", "delete", "push", "pull_request"], branch_filter: "*", config: {url, content_type:
    "json", secret}}`. The secret never appears in returned lines or `--dry-run` output (shown as `***`).
  - CLI with `forge.type: forgejo`: token `FORGE_TOKEN` (required), webhook from `JENKINS_URL` and
    `FORGE_WEBHOOK_SECRET` (both or neither: with neither, webhooks are skipped and the output says so).
  - A PATCH is sent only when the current object differs from the desired one in a field this script owns.

- [ ] **Step 1: Write the failing tests** (fake `Api` holding state like a server):

```python
def test_given_an_application_when_applied_on_forgejo_then_main_is_merged_only_by_jenkins(self):
def test_given_develop_when_applied_then_its_checks_include_the_actions_checks(self):
def test_given_any_release_model_repository_when_applied_then_jenkinsfile_is_a_protected_file(self):
def test_given_release_branches_when_applied_then_only_the_owner_may_push_them(self):
def test_given_admins_when_applied_then_the_rules_do_not_bind_them(self):  # apply_to_admins false
def test_given_a_library_when_applied_then_only_the_owner_creates_its_numeric_tags(self):
def test_given_rules_applied_twice_when_nothing_changed_then_the_second_run_sends_no_write(self):
def test_given_an_existing_webhook_for_jenkins_when_applied_then_it_is_updated_not_duplicated(self):
def test_given_a_webhook_secret_when_dry_run_then_it_is_not_printed(self):
def test_given_jenkins_url_without_a_secret_when_run_then_it_is_refused(self):
def test_given_a_basic_repository_when_applied_then_no_checks_or_whitelists_are_set(self):
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement the Forgejo side.**

- [ ] **Step 4: Run the full suite.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/rules.py scripts/test_rules.py
git commit -m "feat: apply the branch rules on forgejo"
```

---

### Task 8: Against a real Forgejo

**Files:**
- Create: `scripts/test_forgejo_live.py`
- Modify: `scripts/forge.sh`, `scripts/rules.py` (only what the live run proves wrong)
- Modify: `.github/workflows/ci.yml` (job `platform`: a step running this suite with `YGG_FORGEJO_TESTS=1`)

**Interfaces:**
- Consumes: everything above; the Forgejo image pinned in `platform/compose.yml` (16.0.6-rootless).
- The suite (skipped unless `YGG_FORGEJO_TESTS=1` and `docker` works):
  - starts one container of the pinned image on a random local port with SQLite, `INSTALL_LOCK=true`;
    creates users `artur-rios` (admin), `jenkins`, `claude` with `forgejo admin user create`, and tokens with
    `forgejo admin user generate-access-token --scopes all`;
  - creates repositories `shop-api` and `reveal.js` with `develop`, `main` and a `Jenkinsfile`; removes the
    container at the end (named `ygg-forgejo-test-*`, never touching other containers).

- [ ] **Step 1: Write the tests**:

```python
def test_given_a_commit_when_statuses_are_set_and_read_then_the_latest_per_context_comes_back(self):
def test_given_a_release_flow_when_run_then_tag_draft_upload_and_publish_work(self):
def test_given_a_pull_request_when_jenkins_merges_it_then_the_merge_commit_is_printed(self):
def test_given_a_branch_with_a_slash_when_deleted_then_it_is_gone(self):          # release/1.2.3, unprotected
def test_given_a_protected_release_branch_when_jenkins_deletes_it_then_forge_sh_exits_3(self):
def test_given_rules_applied_when_read_back_then_protections_match_and_a_rerun_changes_nothing(self):
def test_given_the_rules_when_claude_merges_a_jenkinsfile_change_then_it_is_refused(self):
def test_given_the_rules_when_the_owner_merges_with_failing_checks_then_it_is_allowed(self):  # admin override
def test_given_the_rules_when_claude_creates_a_v_tag_then_it_is_refused(self):
def test_given_the_rules_when_claude_pushes_a_release_branch_then_it_is_refused(self):
def test_given_a_repository_with_a_dot_when_the_commands_run_then_they_work(self):
```

- [ ] **Step 2: Run them** (Docker Desktop's engine from WSL works, as plan 1's tasks did):

Run: `wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/d/Repositories/yggdrasil && YGG_FORGEJO_TESTS=1 python3 -m unittest discover -s scripts -p "test_forgejo_live.py" -v'`
Expected at first: some FAIL where Task 4's or Task 7's endpoint assumptions differ from Forgejo 16.0.6.

- [ ] **Step 3: Fix `forge.sh`/`rules.py`** to match the real API (and their fake-backed tests to match the
  real answers), until the live suite passes. Record every difference found in the report.

- [ ] **Step 4: Add the CI step** (`YGG_FORGEJO_TESTS=1 python3 -m unittest scripts.test_forgejo_live -v`
  pattern as the socket-proxy step does) and run the full suite once more. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/test_forgejo_live.py scripts/forge.sh scripts/rules.py scripts/test_forge.py scripts/test_rules.py .github/workflows/ci.yml
git commit -m "test: check forge.sh and rules against forgejo"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/setup.md` (section 12, the rules: both servers; the Forgejo webhook variables), `docs/catalog.md`
  (the "Read by" table: `scripts/forge.sh`, `scripts/rules.py`), `README.md` (the scripts table and the "Change
  GitHub rules" row), `CONTRIBUTING.md:~130`, `docs/hosts.md` (credentials table: the webhook secret and where it
  lives), `CHANGELOG.md` (`## [Unreleased]`), the header comments of `scripts/github.sh`, `scripts/forge.sh`,
  `scripts/rules.py`, `jenkins/library/vars/yggdrasilPipeline.groovy` (the comment naming `github.sh`; no code
  change).

- [ ] **Step 1: Write the documentation**:
  - setup.md §12: "Apply the branch rules" — `python3 scripts/rules.py --dry-run`, then without; GitHub needs an
    authenticated `gh` (or `GH_TOKEN`) with admin rights, as today; Forgejo needs `FORGE_TOKEN` (the owner's
    token, `write:repository` scope) and, for webhooks, `JENKINS_URL` and `FORGE_WEBHOOK_SECRET` (the same secret
    configured in Jenkins in plan 3); what each server gets (tables from Tasks 6–7); the GitHub limitation on
    protected files; running it again after adding an application or repository.
  - CHANGELOG: `scripts/forge.sh`, `scripts/rules.py` (with `github/rulesets.py` now a shim), the catalog fields,
    Jenkins contexts no longer bound to GitHub Actions on GitHub.
- [ ] **Step 2: Check every command and path the docs name exists** (`python3 scripts/rules.py --help`,
  `scripts/forge.sh` with no arguments prints its usage, `python3 scripts/catalog.py forge`).
- [ ] **Step 3: Run the full suite and shellcheck.** Expected: PASS, silent shellcheck.
- [ ] **Step 4: Commit**

```bash
git add docs README.md CONTRIBUTING.md CHANGELOG.md scripts/github.sh scripts/forge.sh scripts/rules.py jenkins/library/vars/yggdrasilPipeline.groovy
git commit -m "docs: document the git server interface"
```
