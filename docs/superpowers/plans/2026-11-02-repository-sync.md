# Repository sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A repository's `yggdrasil.yml` can declare a `sync` section. On a push to `main` whose checks pass,
Jenkins copies the listed files (from the checkout, or from the archive of a listed check) into another catalog
repository and pushes one commit directly to that repository's `main`.

**Architecture:** `scripts/pipeline_config.py` validates the section and says which job runs it. A new
`scripts/sync.py` does the work in two halves:
- `gather`, on the ci agent: copies each `from` into a staging directory and refuses links.
- `push`, on the publish agent, inside `onPublishAgent(ctx, true, false)`: clones the target, replaces each `to`
  path wholesale, commits only if something changed, and pushes, retrying on a moved `main`.

The Jenkins library calls both halves from `branchBuild` on `main`, after the checks pass. Files cross between the
agents as a stash. No repository code runs while the write token is bound.

**Tech Stack:** Python 3 (stdlib and PyYAML, as `pipeline_config.py`), git, Groovy (the Jenkins shared library,
static-tested by `scripts/test_pipeline_library.py`).

**Spec:** `D:\Repositories\heimdall-docs\docs\superpowers\specs\2026-11-02-centralized-documentation-design.md`
("The copy"). This plan is that spec's step 1. The heimdall repositories adopt the feature in later steps.

## Global Constraints

- The sync runs only on a push to `main`, never on a pull request or any other branch, and only after every check
  of that build passed.
- Only the repository's **deciding entry** (the first of `catalog.repository_entries(catalog)[repository]`) runs
  its sync. Other jobs of the same repository never do.
- The write token is bound only by `onPublishAgent(ctx, true, ...)`. No repository code, and no command a
  repository's file names, runs inside that closure: only `scripts/sync.py push` and git.
- The target branch is always `main`. Nothing outside the listed `to` paths is ever changed in the target.
- Each `to` path is replaced wholesale by its `from` (a file deleted at the source disappears from the target).
- No change, no commit: an unchanged sync pushes nothing and succeeds.
- Commit subject: `docs: sync <source-repository> <version>`. `<version>` is `v<x.y.z>` when the source `HEAD`
  subject matches `^release: v(\d+\.\d+\.\d+)\b` (the subject `mergeAndTag` gives a release merge). Otherwise it is
  the first 7 characters of the source commit.
- Commit author and committer: `$FORGE_USER <$FORGE_USER@noreply.<host of FORGE_URL>>`.
- A rejected push is retried on a fresh clone. At most 3 attempts, then the build fails.
- Every `sh` step is a single-quoted Groovy literal, with values passed through `withEnv` (the library's existing
  rule, checked by `test_pipeline_library.py`).
- Branch `feature/repository-sync` from `develop`. Commits use lowercase Conventional Commits. The `CHANGELOG.md`
  `## [Unreleased]` entry goes in this pull request.

## Review Focus

1. A `from` that does not exist in the checkout or the archive: the gather fails and names it, and nothing is
   pushed. (Task 2: `test_given_a_missing_from_when_gathered_then_it_fails_naming_it`.)
2. A symbolic link anywhere under a `from`, or a `from` that is a link: the gather refuses it, because a link could
   copy agent files into the target. (Task 2: `test_given_a_link_under_a_from_when_gathered_then_it_is_refused`.)
3. A file deleted at the source: it is deleted in the target, while files beside the `to` paths stay untouched.
   (Task 2: `test_given_a_file_removed_at_the_source_when_pushed_then_it_is_removed_and_nothing_else_changes`.)
4. The target's `main` moving between clone and push: retried on a fresh clone, and the other writer's commit
   survives. (Task 2: `test_given_main_moved_after_the_clone_when_pushed_then_it_retries_and_keeps_both`.)
5. A `to` that escapes the target or overlaps another (`..`, absolute, `.git`, `a` and `a/b`): a config error before
   anything runs. (Task 1: `test_given_unsafe_or_overlapping_paths_when_loaded_then_each_is_refused`.)

---

### Task 1: The `sync` section in `pipeline_config.py`

**Files:**
- Modify: `scripts/pipeline_config.py` (module docstring field list; `SECTIONS` or a dedicated validator;
  `validate_against`; `job_checks`; `needed_checks`; `plan_checks`; `unclaimed_checks`; `describe`; `PLAN_FLAGS`;
  `plan`; `_command`)
- Test: `scripts/test_pipeline_config.py`

**Interfaces:**
- Produces:
  - Schema:
    `sync: {repository: str (required), inputs: [check names] (optional), paths: [{from: str, to: str}] (required, ≥1)}`.
  - `syncs(catalog, entry) -> bool`: true when `entry` is its repository's deciding entry.
  - `describe(...)` gains the key `"syncs": bool`, which is true only when the file has a `sync` section and this
    job is the deciding entry.
  - `sync_inputs(config) -> list[str]`: the `sync.inputs` that are checks with an `archive`.
  - `needed_checks(config, entry, deploying=False, release_checks=None, syncing=False)`: with `syncing`, it appends
    the `sync.inputs` that are not already listed.
  - `plan_checks(..., syncing=False)`.
  - `plan` CLI flag `--syncing` (added to `PLAN_FLAGS`), passed through `plan(..., syncing=False)`.

- [ ] **Step 0: Branch.** `git switch develop && git pull && git switch -c feature/repository-sync`.

- [ ] **Step 1: Write the failing tests** in `test_pipeline_config.py`. Add a second application with
  `repository: shop-frontend` to the test `CATALOG`, so one repository has two entries, and add a `SYNC` YAML
  fragment:
  `sync: {repository: shop-specs, inputs: [unit], paths: [{from: out/report, to: static/report}, {from: docs/req, to: content/req}]}`.
  - `test_given_a_sync_section_when_loaded_then_it_is_returned`: `FULL` plus `SYNC` loads, and
    `config["sync"]["paths"][0] == {"from": "out/report", "to": "static/report"}`.
  - `test_given_a_sync_without_repository_or_paths_when_loaded_then_each_is_named`: errors contain
    `sync.repository: required` and `sync.paths: required`. `paths: []` gives an error starting
    `sync.paths: must list at least one`.
  - `test_given_unsafe_or_overlapping_paths_when_loaded_then_each_is_refused`: each of these `from` or `to` values
    gives one error naming `sync.paths[<i>].<field>`: `/abs`, `a/../b`, `..`, `.`, `a//b`, `a\\b`, `.git/x`,
    `x/.git`, `""`, `"a\n"`. Two `to` values `site` and `site/api` give an error containing `overlap`.
  - `test_given_a_sync_to_an_unknown_or_its_own_repository_when_validated_then_it_is_an_error`:
    `repository: nope` gives `sync.repository: 'nope' is not a repository in catalog.yaml`. The source repository
    itself gives an error containing `its own repository`.
  - `test_given_sync_inputs_that_are_not_archived_checks_when_validated_then_it_is_an_error`: an unknown name gives
    `sync.inputs: 'x' is not a check in yggdrasil.yml`. A check without `archive` gives an error containing
    `has no archive`.
  - `test_given_the_deciding_entry_when_described_then_it_syncs_and_another_entry_does_not`: for the repository
    `shop-frontend`, `describe` returns `syncs: True` for the
    deciding entry and `False` for the other. A file without `sync` returns `syncs: False` everywhere.
  - `test_given_syncing_when_planned_then_the_sync_inputs_are_needed_after_the_checks`:
    `needed_checks(config, entry, syncing=True)` returns `entry["checks"]` followed by the missing `sync.inputs`,
    each once.
  - `test_given_sync_input_credentials_when_validated_then_the_syncing_entry_must_allow_them`: a sync input
    binding a credential outside the deciding entry's `credentials` is an error. The same credential on an entry
    that does not sync is not judged.
  - `test_given_a_sync_input_no_entry_checks_when_described_then_it_is_not_unclaimed`: `unclaimed_checks` claims
    `sync.inputs` (the deciding entry runs them on `main`).
  - `test_given_the_syncing_flag_when_the_cli_plans_then_the_inputs_are_in_the_plan`: `_command([... "--syncing"])`
    plans the sync inputs too, and the flag can be combined with `--deploying` in any order.

- [ ] **Step 2: Run them to see them fail**
  Run: `cd scripts && python3 -m unittest test_pipeline_config -v`
  Expected: the new tests fail on `sync: unknown field` or `AttributeError`. The existing tests pass.

- [ ] **Step 3: Implement.** Add `"sync"` to the top-level keys. `paths` needs a dedicated validator, because it
  is a list of mappings, which `_section` cannot express. A path is valid when:
  - it is a non-empty string matching `[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*`;
  - no part is `.`, `..` or `.git`.

  Overlap means one `to` equals another or is a prefix of it at a `/` boundary.
  - In `validate_against`, judge `sync` only when `syncs(...)` (pass a `syncing: bool` argument from `describe`).
    The repository must be in `cat.repository_entries(catalog)` and must not be `entry["repository"]`. Each input
    must be a check of the file with an `archive`.
  - `job_checks` includes `sync.inputs` for the syncing entry.
  - Update the module docstring's field list and `plan` usage line.

- [ ] **Step 4: Run the whole module**
  Run: `cd scripts && python3 -m unittest test_pipeline_config -v`
  Expected: all pass.

- [ ] **Step 5: Commit**
  `git add scripts/pipeline_config.py scripts/test_pipeline_config.py docs/superpowers/plans/2026-11-02-repository-sync.md`, then
  `git commit -m "feat: validate a sync section in yggdrasil.yml"`

### Task 2: `scripts/sync.py`, gather and push

**Files:**
- Create: `scripts/sync.py`
- Test: `scripts/test_sync.py` (real git repositories in a temporary directory, with a bare repository as the
  remote. No network and no forge.)

**Interfaces:**
- Consumes: the validated `sync` section (Task 1), handed over as a JSON file:
  `{"repository": str, "paths": [{"from": str, "to": str}]}`.
- Produces two CLI commands, each exiting 0 on success and 1 with `sync: <message>` on stderr:
  - `python3 scripts/sync.py gather <spec.json> <app-dir> <out-dir>`
    - For each path `i`, it copies `<app-dir>/<from>` (a file or a directory) to `<out-dir>/<i>`.
    - It writes `<out-dir>/manifest.json`, which holds the spec's paths in order.
    - It fails if a `from` is missing, or is or contains a symbolic link. It checks everything before copying
      anything.
  - `python3 scripts/sync.py push <out-dir> <target-url> <message-file>`
    - It clones `<target-url>` `main` with depth 1.
    - For each manifest entry, it removes `<clone>/<to>` and copies `<out-dir>/<i>` there.
    - It runs `git add -A -- <to>...`, commits with the message from `<message-file>` only if something is staged,
      and pushes `HEAD:main`.
    - When the push is rejected, it starts again from a fresh clone, 3 attempts in total.
    - Its stdout says `sync: pushed <sha>` or `sync: nothing to change`.
    - Credentials come from the environment only: when `FORGE_USER` and `FORGE_TOKEN` are set, git gets the same
      inline `credential.helper` that `checkoutTools()` uses. They never appear in an argument or in the output.
  - `python3 scripts/sync.py message <source-repo> <app-dir>`: prints the commit message (the Global Constraints
    subject, a blank line, then one `- <from> -> <to>` line per path and `Source: <source-repo>@<full sha>`).

- [ ] **Step 1: Write the failing tests** in `test_sync.py`. A fixture builds a source checkout (`git init`, files,
  commit), a bare target with `main` holding `README.md`, `static/old.txt` and `content/req/gone.md`, and a spec
  with paths `out/report -> static/report` and `docs/req -> content/req`.
  - `test_given_files_when_gathered_then_each_from_is_staged_by_index_with_a_manifest`.
  - `test_given_a_missing_from_when_gathered_then_it_fails_naming_it`: the exit code is 1, stderr contains
    `out/report`, and `<out-dir>` has no `manifest.json`.
  - `test_given_a_link_under_a_from_when_gathered_then_it_is_refused`: covers a link inside `docs/req`, and
    separately `out/report` being a link. Exit 1, stderr contains `link`, nothing staged.
  - `test_given_new_content_when_pushed_then_main_has_one_commit_with_the_message`: the target `main` advanced by
    exactly one commit. Its subject equals the message's first line, `static/report/...` and `content/req/...`
    match the source, `README.md` and `static/old.txt` are unchanged, and `content/req/gone.md` is gone.
  - `test_given_a_file_removed_at_the_source_when_pushed_then_it_is_removed_and_nothing_else_changes`: a second
    push after deleting a source file. That file is gone from the target, and the `git diff --stat` of the commit
    touches only `to` paths.
  - `test_given_no_change_when_pushed_again_then_nothing_is_committed`: the target `main` sha is unchanged, and
    stdout contains `nothing to change`.
  - `test_given_main_moved_after_the_clone_when_pushed_then_it_retries_and_keeps_both`: patch the push step so the
    first attempt commits elsewhere in the target before pushing (for example, an injected hook through
    `SYNC_TEST_BEFORE_PUSH`, or `unittest.mock` of the push function). The result has both commits on `main`.
  - `test_given_pushes_always_rejected_when_pushed_then_it_fails_after_three_attempts`: exit 1, and stderr says
    `3 attempts`.
  - `test_given_a_release_merge_when_the_message_is_made_then_it_names_the_version`: a `HEAD` subject of
    `release: v4.3.0 (#12)` gives `docs: sync heimdall-api v4.3.0`. Any other subject gives the 7-character sha.
  - `test_given_a_token_when_pushing_then_it_never_reaches_arguments_or_output`: run with
    `FORGE_TOKEN=secret-xyz` against the bare repository. `secret-xyz` is not in stdout or stderr, and is not in
    any recorded argv (wrap `subprocess.run` with a mock that records calls).

- [ ] **Step 2: Run them to see them fail**
  Run: `cd scripts && python3 -m unittest test_sync -v`
  Expected: an import error or file-not-found for `sync.py`.

- [ ] **Step 3: Implement `scripts/sync.py`.**
  - Use the stdlib only, and run git through `subprocess.run([...], check=...)` with
    `GIT_TERMINAL_PROMPT=0`.
  - Copy with `shutil.copytree(..., symlinks=True)` after the link check, so a link that appears in a race is copied
    as a link, never followed.
  - Set the author email host from `urllib.parse.urlparse(os.environ["FORGE_URL"]).hostname`. Without
    `FORGE_URL`, fall back to `localhost`.
  - Write the module docstring in the style of `pipeline_config.py`: usage, what each command does, and why the two
    halves run on different agents.

- [ ] **Step 4: Run the tests**
  Run: `cd scripts && python3 -m unittest test_sync -v`
  Expected: all pass. Then `python3 scripts/run_tests.py`; expected: the whole suite passes.

- [ ] **Step 5: Commit**
  `git add scripts/sync.py scripts/test_sync.py`, then
  `git commit -m "feat: copy files into another repository with sync.py"`

### Task 3: The pipeline runs the sync on `main`, documented

**Files:**
- Modify: `jenkins/library/vars/yggdrasilPipeline.groovy`:
  - `prepare` and `planChecks`: a `syncing` argument, `--syncing`, and forcing `run` for the sync inputs as
    `deploying` does;
  - `neededChecks`;
  - `branchBuild`;
  - the new functions `syncInputs(Map config)` (`@NonCPS`, like `deployInputs`) and `syncRepository(Map ctx,
    Map state)`;
  - the credential-rules header comment.
- Test: `scripts/test_pipeline_library.py`
- Modify: `docs/catalog.md` (a new "Copying files into another repository (`sync`)" subsection under
  "Repository pipeline file", and `sync` in the example block and the "Which checks run" list),
  `CHANGELOG.md` (`## [Unreleased]` → `### Added`)

**Interfaces:**
- Consumes: `describe`'s `syncs` and `config.sync`, `plan --syncing` (Task 1), and `sync.py gather|message|push`
  (Task 2).
- Produces: `void syncRepository(Map ctx, Map state)`.
  - Stage `Sync to <repository>`. In `node('ci')`, it runs `checkoutCommit(ctx, state.sha)`, unstashes
    `check-<name>` for each sync input into `app/`, then runs `sync.py gather` and `sync.py message` into a
    scratch directory. It stashes that directory as `sync-files`.
  - Then, in `onPublishAgent(ctx, true, false)`, it unstashes `sync-files` and runs `sync.py push` with
    `YGG_SYNC_URL=${env.FORGE_URL}/${env.GITHUB_OWNER}/<repository>.git`.
  - The target repository name is checked against `REPOSITORY_ID` before use.

- [ ] **Step 1: Write the failing static tests** in `test_pipeline_library.py`, using its existing helpers
  (`node_blocks`, `call_blocks`, `functions`, `calls`):
  - `test_given_the_sync_when_read_then_its_push_runs_only_on_the_publish_agent_with_the_write_token`: the text
    `sync.py push` appears only inside a closure given to `onPublishAgent`, and that call is
    `onPublishAgent(ctx, true, false)`.
  - `test_given_the_sync_when_read_then_gathering_runs_on_the_ci_agent_without_the_write_token`: `sync.py gather`
    appears only in a `node('ci')` block of `syncRepository`, and that function never calls `withForgeToken('write')`.
  - `test_given_a_branch_build_when_read_then_it_syncs_only_on_main_after_the_checks`: in `branchBuild`, the
    `syncRepository(` call sits after the `runChecks(` failure `error`, behind a condition that tests
    `branch == 'main'` and `state.syncs`.
  - `test_given_syncing_when_planned_then_sync_inputs_are_forced_to_run`: `planChecks` passes `--syncing` when
    `YGG_SYNCING` is set, and has a loop that sets `check.action = 'run'` for `syncInputs(state.config)`.
  - The existing rules (no interpolated `sh`, the write credential only in `reportStatus`/`onPublishAgent`, nothing
    waiting on an agent) keep passing unchanged.

- [ ] **Step 2: Run them to see them fail**
  Run: `cd scripts && python3 -m unittest test_pipeline_library -v`
  Expected: the four new tests fail, and the rest pass.

- [ ] **Step 3: Implement.**
  - `describeCheckout` keeps `document.syncs`, and `prepare` stores it as `state.syncs`.
  - `branchBuild` computes `boolean syncing = branch == 'main' && !deployToParameter` and passes it to `prepare`.
    When `state.syncs` is true, it calls `syncRepository(ctx, state)` after the checks pass and before the
    `!targets` early return.
  - Comment each new function in the style of its neighbours, and add the sync to the credential rules in the
    header comment.

- [ ] **Step 4: Run the library tests and the whole suite**
  Run: `cd scripts && python3 -m unittest test_pipeline_library -v`, then `python3 scripts/run_tests.py`
  Expected: all pass.

- [ ] **Step 5: Document.**
  - In `docs/catalog.md`, cover:
    - what `sync` does and when it runs (pushes to `main` only, after the checks, deciding entry only);
    - the fields, with the heimdall-api example from the spec;
    - the validation rules;
    - that it pushes directly to the target's `main` (so the target must accept the publish account's direct
      pushes: a `rules: basic` repository does);
    - that a sync failure fails the build of `main` and does not undo a release;
    - the commit message format.
  - In `CHANGELOG.md`, add an operator-facing entry under `## [Unreleased]`.

- [ ] **Step 6: Commit**
  `git add jenkins/library/vars/yggdrasilPipeline.groovy scripts/test_pipeline_library.py docs/catalog.md CHANGELOG.md`, then
  `git commit -m "feat: sync files to another repository on pushes to main"`

### Task 4: Pull request

- [ ] **Step 1:** Run `python3 scripts/run_tests.py` once more. Expected: the whole suite passes.
- [ ] **Step 2:** `git push -u origin feature/repository-sync`.
- [ ] **Step 3:** Open the pull request into `develop` on Forgejo with `tea pulls create --repo artur-rios/yggdrasil --base develop --head feature/repository-sync`.
  - Title: `feat: sync files to another repository on pushes to main`.
  - The body says what the feature is for (centralizing the Heimdall docs) and lists the operator steps after
    release: release yggdrasil, set `YGGDRASIL_LIBRARY_VERSION`, and run `platform.sh up`. It ends with the
    Claude Code attribution line.
  - The live Jenkins suite was not run, so the body says so.
