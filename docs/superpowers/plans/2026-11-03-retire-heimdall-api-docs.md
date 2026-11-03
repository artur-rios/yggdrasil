# Retire heimdall-api-docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** yggdrasil stops deploying heimdall-api's own docs site. Its address,
`https://docs.artur-rios.tech/heimdall-api/...`, then redirects permanently to the matching page of heimdall-docs.

**Architecture:**
- The `heimdall-api-docs` catalog entry and its two stack files are removed.
- The redirect is a Traefik file router in `platform/traefik/dynamic/`, the same mechanism as `artur-rios-com.yml`.
  It is a chain of `redirectRegex` middlewares that map old paths to new ones, with a catch-all last.
- The hosts get the file with a `git pull` of yggdrasil's `main`, and Traefik watches the folder. So the redirect goes
  live when the operator pulls the release, after they have stopped the old container.

**Tech Stack:** YAML (catalog and Traefik dynamic configuration), Python `unittest` (`scripts/test_catalog.py`,
`scripts/test_edge.py`, `scripts/test_jenkins_config.py`).

**Spec:** `D:\Repositories\heimdall-docs\docs\superpowers\specs\2026-11-02-centralized-documentation-design.md`. This
plan covers the spec's step 3 ("Order of work") and its "Old addresses" section. The new pages exist on
heimdall-docs' branch `feature/centralized-docs`, in artur-rios/heimdall-docs#7.

## Global Constraints

- Every redirect is permanent (301), and Host is `docs.artur-rios.tech`. The router matches
  `Path(`/heimdall-api`) || PathPrefix(`/heimdall-api/`)`, the rule the old stack used.
- It runs on entry point `websecure` with the `secure-headers` middleware first, as `artur-rios-com.yml` does.
- The query string and the fragment survive where the target is a page of the new site.
- The new base is `https://docs.artur-rios.tech/heimdall-docs/`.
- The path map is below. The first rule that matches wins, so the order in the chain is the order of the table:

  | Old path (after `/heimdall-api`) | New URL |
  |---|---|
  | `/coverage-report/<rest>` | `/heimdall-docs/api/coverage-report/<rest>` |
  | `/openapi/heimdall.json` | `/heimdall-docs/api/openapi/heimdall.json` |
  | `/docs/overview/` | `/heimdall-docs/docs/overview/` |
  | `/docs/architecture/` | `/heimdall-docs/docs/architecture/` |
  | `/docs/domain-model/` | `/heimdall-docs/docs/architecture/domain-model/` |
  | `/docs/getting-started/` | `/heimdall-docs/docs/installation/` |
  | `/docs/environments-and-deployment/` | `/heimdall-docs/docs/installation/environments-and-deployment/` |
  | `/docs/api-reference/` | `/heimdall-docs/docs/api/reference/` |
  | `/docs/api-explorer/` | `/heimdall-docs/docs/api/explorer/` |
  | `/docs/operations/` | `/heimdall-docs/docs/api/operations/` |
  | `/docs/flows/<rest>` | `/heimdall-docs/docs/api/flows/<rest>` |
  | `/docs/requirements/` | `/heimdall-docs/docs/api/requirements/` |
  | `/docs/requirements/<slug>/` | `/heimdall-docs/docs/api/requirements/documents/<slug>/` |
  | `/docs/changelog/` | `https://github.com/artur-rios/heimdall-api/blob/main/CHANGELOG.md` |
  | `/docs/contributing/` | `https://github.com/artur-rios/heimdall-api/blob/main/CONTRIBUTING.md` |
  | anything else, `/heimdall-api` itself included | `/heimdall-docs/docs/api/` |

  The `/docs/` prefixed pages match with or without their trailing slash. A page's own subpaths (`index.xml`,
  `_print/`) fall to the catch-all.
- Do not change `heimdall-api`'s own catalog entry. Its `deploy.inputs: [test]` stays in heimdall-api's
  `yggdrasil.yml` until step 4. With one entry left, the input is harmless.
- History stays as written: `CHANGELOG.md` entries already released, `docs/superpowers/` plans and specs,
  `docs/migration-checklist.md`, and the record table in `docs/moving-repositories.md`.
- Branch `feature/retire-heimdall-api-docs` from `origin/develop`, with a pull request into `develop`. Commits use
  lowercase Conventional Commits. The `CHANGELOG.md` entry goes under `## [Unreleased]`.

## Review Focus

1. **The `/heimdall-api` root and paths without a trailing slash** (`/heimdall-api/docs/overview`) redirect to the
   right page, not the catch-all. Tested in Task 2's table.
2. **Rule order:** `/docs/requirements/` (the index) and `/docs/requirements/<slug>/` (a document) must not shadow
   each other, and `/docs/flows/` must keep its sub-path. Tested in Task 2's table.
3. **Exact matching:** a URL under `/heimdall-apiX/` (another site whose name starts the same) is not matched by the
   router. Tested in Task 2.
4. **Catalog checks after the removal:** the remaining checks must still pass. These are the sites count, the
   shared-repository example in the tests, and any check that every stack file has a catalog entry and the reverse.
   Covered by Task 1's full suite run.
5. **The operator steps:** they are in the CHANGELOG entry in an order that never leaves both routers answering on
   `/heimdall-api`, and never leaves a gap before heimdall-docs#7 is live. Task 1's CHANGELOG step, checked at
   review.

---

### Task 1: Remove the heimdall-api-docs application

**Files:**
- Modify: `catalog.yaml`. Delete the `heimdall-api-docs` entry and its comment block (lines around 341–355).
  Re-word any comment on `heimdall-api` or `heimdall-docs` that mentions it.
- Delete: `stacks/heimdall-api-docs.yml` and `stacks/heimdall-api-docs.proxy.yml`.
- Modify: `scripts/test_catalog.py`.
  - The sites count goes from 25 to 24.
  - The `build.dockerfile` clause that names `heimdall-api-docs` becomes an assertion that no site now overrides its
    Dockerfile.
  - Check the file for any other mention.
- Modify: `scripts/test_jenkins_config.py` (line around 96) and `platform/jenkins/controller/casc.yaml` (line around
  165). Their comments use heimdall-api-docs as the example of a shared repository; use one that still exists, such
  as a library and its `-docs` site.
- Modify: `docs/hosts.md`, the `reportgenerator-license` row, if it names heimdall-api-docs as a user of the
  credential. Read the row and the catalog `credentials` lists, and state who uses it now.
- Modify: `CHANGELOG.md`, with a `### Removed` entry under `## [Unreleased]`. Besides the change, it carries the
  operator steps, in this order:
  1. Merge heimdall-docs#7, and check that its pages are live.
  2. Release yggdrasil.
  3. On the Application Server, as `ygg`, run `docker compose -p heimdall-api-docs-production down`. Confirm the
     project name with `docker compose ls` first.
  4. Run `cd /opt/yggdrasil && git pull`, which brings the redirect file (Task 2).
  5. Delete the `heimdall-api-docs` job in Jenkins. The Job DSL leaves removed jobs in place, because
     `removedJobAction` is not set. Check `platform/jenkins/controller/casc.yaml` and say what it actually does.
  6. Check with `curl -sI https://docs.artur-rios.tech/heimdall-api/docs/overview/`, which should give a 301 to
     `/heimdall-docs/docs/overview/`.

- [ ] **Step 0: Branch** with `git switch -c feature/retire-heimdall-api-docs origin/develop`.
- [ ] **Step 1: Make the test edits first** (the count and the dockerfile clause). Run
  `python -m unittest test_catalog -v` from `scripts/`. Expected: the count test fails with `25 != 24`.
- [ ] **Step 2: Remove the entry and the stacks, and fix the comments.**
- [ ] **Step 3: Run the tests.** Run `python -m unittest test_catalog test_jenkins_config -v`, then
  `python scripts/run_tests.py`. Expected: they pass, apart from the pre-existing Windows-only failures, compared
  with a run on `origin/develop`. Also run `grep -rn "heimdall-api-docs" --exclude-dir=.git .`. Expected: only
  history files (see Global Constraints) and the new CHANGELOG entry.
- [ ] **Step 4: Commit** with `git commit -m "feat: retire the heimdall-api docs site"`, including this plan.

### Task 2: Redirect the old address

**Files:**
- Create: `platform/traefik/dynamic/heimdall-api-docs.yml`. It holds one router, `heimdall-api-docs`, and one
  middleware per table row, named `heimdall-api-docs-<n>`, in table order. The header comment follows
  `artur-rios-com.yml`: what it does, why it is a file router, and that it is watched.
- Modify: `scripts/test_edge.py`, with a new test next to the artur-rios-com one.
- Modify: `docs/hosts.md`, with one paragraph next to "A domain that only redirects" naming the file and what it
  maps. Update `CHANGELOG.md` (Task 1's entry) to mention the redirect.

**Interfaces:**
- Consumes: the path map in Global Constraints.
- Produces: the router `heimdall-api-docs` on `websecure`, with middlewares
  `[secure-headers, heimdall-api-docs-1, …, heimdall-api-docs-16]` and `service: noop@internal`.

- [ ] **Step 1: Write the failing test**
  `test_given_the_retired_heimdall_api_site_when_requested_then_each_page_redirects_to_heimdall_docs`.
  - It loads the file and checks the router's rule, entry points and middleware list.
  - Then it walks the chain the way Traefik does. For each URL, the first middleware whose `regex` matches gives
    `re.sub(regex, replacement with ${n} → \n, url)`. It checks `permanent: true` on each.
  - The URL cases include one per table row; the `/heimdall-api` root, with and without its slash; `/docs/overview`
    with no slash; `/docs/requirements/` against `/docs/requirements/vision-document/`; `/docs/flows/two-factor/`
    with a query string `?x=1`; and `/coverage-report/index.html`.
  - A separate assertion checks the router rule does not match `/heimdall-apix/`, by evaluating the
    `Path`/`PathPrefix` semantics by hand.
- [ ] **Step 2: Run it.** Run `python -m unittest test_edge -v` from `scripts/`. Expected: it fails because the
  file does not exist.
- [ ] **Step 3: Write the file.** Anchor each regex as `^https?://[^/]+/heimdall-api<path>(<rest>)$`, so the catch-all
  only matches what earlier rules did not.
- [ ] **Step 4: Run the tests.** Run `python -m unittest test_edge -v`, then `python scripts/run_tests.py`.
  Expected: they pass, apart from the pre-existing Windows-only failures.
- [ ] **Step 5: Commit** with `git commit -m "feat: redirect the retired heimdall-api docs to heimdall-docs"`.

### Task 3: Pull request

- [ ] Run `git push -u origin feature/retire-heimdall-api-docs`, then
  `tea pulls create --repo artur-rios/yggdrasil --base develop --head feature/retire-heimdall-api-docs`.
  - Title: `feat: retire the heimdall-api docs site`.
  - The body lists the path map, the operator steps in order, and that it depends on heimdall-docs#7 being merged
    first. It ends with the Claude Code attribution line.
