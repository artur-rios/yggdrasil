"""Tests for scripts/repo_inventory.py (the per-repository table of the move to Forgejo).
python3 -m unittest discover -s scripts -p test_repo_inventory.py

The fixtures are real git clones in a temp directory (built with `git init`), because the tool reads
`git status`; nothing here touches the owner's repositories or GitHub."""

import contextlib
import io
import pathlib
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

import repo_inventory as inv

# A workflow with one Linux job, one Windows job and a secret on the Linux one.
CI = """
name: ci
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: dotnet test
        env:
          LICENSE: ${{ secrets.REPORTGENERATOR_LICENSE }}
  desktop:
    runs-on: windows-latest
    steps:
      - run: dotnet build
"""

PAGES = """
name: docs
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/upload-pages-artifact@v3
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/deploy-pages@v4
"""

MATRIX_LIST = """
name: matrix
on: [push]
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - run: echo hi
"""

MATRIX_INCLUDE = """
name: matrix
on: [push]
jobs:
  test:
    strategy:
      matrix:
        include:
          - os: windows-latest
            platform: windows
          - os: ubuntu-latest
            platform: linux
    runs-on: ${{ matrix.os }}
    steps:
      - run: echo hi
"""

UNRESOLVABLE = """
name: odd
on: [push]
jobs:
  test:
    runs-on: ${{ inputs.runner }}
    steps:
      - run: echo hi
"""

CALLS = """
on: push
jobs:
  verify:
    uses: ./.github/workflows/verify.yml
  elsewhere:
    uses: someone/else/.github/workflows/x.yml@v1
"""

DEPENDABOT = """
version: 2
updates:
  - package-ecosystem: nuget
    directory: /
    schedule: {interval: weekly}
  - package-ecosystem: github-actions
    directory: /
    schedule: {interval: weekly}
"""


def git(path, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(path), *args],
                   check=True, capture_output=True)


def make_clone(root, name, workflows=None, dependabot=None, vercel=False, dirty=False, branch="main"):
    """A git clone at root/name with the given workflow files (name -> text), committed; `dirty` leaves an
    uncommitted file behind."""
    path = pathlib.Path(root) / name
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", branch)
    (path / "README.md").write_text("# " + name + "\n", encoding="utf-8")
    for filename, text in (workflows or {}).items():
        target = path / ".github" / "workflows" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
    if dependabot:
        (path / ".github").mkdir(exist_ok=True)
        (path / ".github" / "dependabot.yml").write_text(dependabot, encoding="utf-8")
    if vercel:
        (path / "vercel.json").write_text("{}\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    if dirty:
        (path / "scratch.txt").write_text("not committed\n", encoding="utf-8")
    return path


def github(name, fork=False, archived=False):
    """A repository as migrate.github_repositories answers it."""
    return {"name": name, "private": False, "fork": fork, "archived": archived, "default_branch": "main",
            "has_wiki": False, "size": 1}


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def row(self, name, rows):
        return next(r for r in rows if r.name == name)


class ClassifyJobTests(unittest.TestCase):
    def test_given_an_ubuntu_job_when_classified_then_it_becomes_a_check(self):
        self.assertEqual(inv.CHECK, inv.classify_job("ubuntu-latest", "tests.yml", [{"run": "dotnet test"}]))
        self.assertEqual(inv.CHECK, inv.classify_job("ubuntu-24.04", "tests.yml", []))

    def test_given_an_ubuntu_job_in_a_release_workflow_when_classified_then_it_is_a_release_step(self):
        # Plan 3 runs release steps on the release agent, not as checks.
        for workflow in ("release.yml", "release-artifacts.yml", "Release.yaml"):
            with self.subTest(workflow=workflow):
                self.assertEqual("release (release.build)",
                                 inv.classify_job("ubuntu-24.04", workflow, [{"run": "dotnet pack"}]))

    def test_given_an_ubuntu_job_in_a_publish_package_workflow_when_classified_then_it_is_a_publish_step(self):
        for workflow in ("publish-package.yml", "publish-package-nuget.yaml"):
            with self.subTest(workflow=workflow):
                self.assertEqual("publish (publish.nuget)",
                                 inv.classify_job("ubuntu-latest", workflow, [{"run": "dotnet nuget push"}]))

    def test_given_a_windows_job_in_a_release_workflow_when_classified_then_it_still_stays(self):
        self.assertEqual(inv.STAYS, inv.classify_job("windows-2025", "release.yml", [{"run": "pack"}]))

    def test_given_a_pages_job_in_a_release_workflow_when_classified_then_it_goes(self):
        self.assertEqual("goes (Pages)", inv.classify_job("ubuntu-latest", "release.yml",
                                                          [{"uses": "actions/deploy-pages@v4"}]))

    def test_given_a_windows_job_when_classified_then_it_stays_on_actions(self):
        self.assertEqual(inv.STAYS, inv.classify_job("windows-latest", "ci.yml", [{"run": "dotnet build"}]))
        self.assertEqual(inv.STAYS, inv.classify_job("windows-2025", "ci.yml", []))
        self.assertEqual(inv.STAYS, inv.classify_job("macos-latest", "ci.yml", []))

    def test_given_a_pages_deploy_workflow_when_classified_then_it_goes(self):
        for action in ("actions/deploy-pages@v4", "actions/upload-pages-artifact@v3",
                       "peaceiris/actions-gh-pages@v4"):
            with self.subTest(action=action):
                self.assertEqual("goes (Pages)", inv.classify_job("ubuntu-latest", "docs.yml", [{"uses": action}]))

    def test_given_a_pages_workflow_on_windows_when_classified_then_it_still_goes(self):
        # A Pages workflow leaves whole, whatever it runs on: it only exists to publish to GitHub Pages.
        self.assertEqual("goes (Pages)", inv.classify_job("windows-latest", "docs.yml",
                                                          [{"uses": "actions/deploy-pages@v4"}]))

    def test_given_a_vercel_workflow_when_classified_then_it_goes(self):
        self.assertEqual("goes (Vercel)", inv.classify_job("ubuntu-latest", "deploy.yml",
                                                           [{"uses": "amondnet/vercel-action@v25"}]))
        self.assertEqual("goes (Vercel)", inv.classify_job("ubuntu-latest", "deploy.yml",
                                                           [{"run": "npx vercel deploy --prod"}]))

    def test_given_the_branch_policy_workflow_when_classified_then_it_goes(self):
        self.assertEqual("goes (branch-policy)", inv.classify_job("ubuntu-latest", "branch-policy.yml", []))

    def test_given_a_runner_it_cannot_resolve_when_classified_then_it_is_unknown(self):
        for runs_on in ("${{ inputs.runner }}", "self-hosted", "", None):
            with self.subTest(runs_on=runs_on):
                self.assertEqual(inv.UNKNOWN, inv.classify_job(runs_on, "ci.yml", []))

    def test_given_a_list_of_runner_labels_when_classified_then_the_operating_system_decides(self):
        self.assertEqual(inv.STAYS, inv.classify_job(["self-hosted", "windows"], "ci.yml", []))
        self.assertEqual(inv.CHECK, inv.classify_job(["ubuntu-latest"], "ci.yml", []))


class InventoryTests(Fixture):
    def inventory(self, **kwargs):
        return inv.inventory(self.root, **kwargs)

    def test_given_an_ubuntu_and_a_windows_job_when_inventoried_then_each_is_listed_with_its_verdict(self):
        make_clone(self.root, "widget", {"ci.yml": CI})
        jobs = self.row("widget", self.inventory()).jobs
        self.assertEqual(["ci.yml/build on ubuntu-latest → check build",
                          "ci.yml/desktop on windows-latest stays (Actions)"], [j.text() for j in jobs])

    def test_given_release_and_publish_workflows_when_inventoried_then_their_ubuntu_jobs_are_not_checks(self):
        job = "on: push\njobs:\n  go:\n    runs-on: ubuntu-latest\n    steps: []\n"
        make_clone(self.root, "shipper", {"release.yml": job, "publish-package.yml": job, "tests.yml": job})
        self.assertEqual(["publish-package.yml/go on ubuntu-latest → publish (publish.nuget)",
                          "release.yml/go on ubuntu-latest → release (release.build)",
                          "tests.yml/go on ubuntu-latest → check go"],
                         [j.text() for j in self.row("shipper", self.inventory()).jobs])

    def test_given_a_workflow_using_secrets_when_inventoried_then_each_secret_is_listed(self):
        make_clone(self.root, "widget", {
            "ci.yml": CI,
            "publish.yml": "on: push\njobs:\n  p:\n    runs-on: ubuntu-latest\n    steps:\n"
                           "      - run: push\n        env:\n          K: ${{ secrets.NUGET_API_KEY }}\n"
                           "          T: ${{ secrets['EXTRA_TOKEN'] }}\n          G: ${{ secrets.NUGET_API_KEY }}\n"})
        self.assertEqual(["EXTRA_TOKEN", "NUGET_API_KEY", "REPORTGENERATOR_LICENSE"],
                         self.row("widget", self.inventory()).secrets)

    def test_given_a_secret_only_named_in_a_comment_when_inventoried_then_it_is_not_listed(self):
        make_clone(self.root, "widget", {"ci.yml": "# uses secrets.OLD_KEY once\n" + CI})
        self.assertNotIn("OLD_KEY", self.row("widget", self.inventory()).secrets)

    def test_given_a_dirty_clone_when_inventoried_then_its_state_is_dirty(self):
        make_clone(self.root, "messy", dirty=True)
        make_clone(self.root, "tidy")
        rows = self.inventory()
        self.assertEqual("dirty", self.row("messy", rows).state)
        self.assertEqual("clean", self.row("tidy", rows).state)

    def test_given_the_file_being_written_inside_a_clone_when_inventoried_then_it_does_not_make_it_dirty(self):
        clone = make_clone(self.root, "self")
        output = clone / "docs" / "checklist.md"
        output.parent.mkdir()
        output.write_text("generated", encoding="utf-8")
        self.assertEqual("dirty", self.row("self", self.inventory()).state)
        self.assertEqual("clean", self.row("self", self.inventory(ignore=output)).state)

    def test_given_a_repository_missing_locally_when_inventoried_then_it_is_marked_missing(self):
        make_clone(self.root, "here")
        rows = self.inventory(github=[github("here"), github("gone")])
        self.assertEqual("missing", self.row("gone", rows).state)
        self.assertEqual("clean", self.row("here", rows).state)

    def test_given_no_github_listing_when_inventoried_then_missing_clones_are_not_guessed(self):
        # Which repositories exist beyond the clones is GitHub's to say: without its list, only clones are rows.
        make_clone(self.root, "here")
        (self.root / "databases").mkdir()
        self.assertEqual(["here"], [r.name for r in self.inventory()])

    def test_given_a_repository_the_spec_names_that_github_lacks_when_inventoried_then_a_note_says_so(self):
        make_clone(self.root, "here")
        row = self.row("github-readme-stats", self.inventory(github=[github("here")]))
        self.assertEqual("missing", row.state)
        self.assertTrue(any("not on GitHub" in n for n in row.notes))

    def test_given_a_clone_when_inventoried_then_the_current_branch_is_recorded(self):
        make_clone(self.root, "here", branch="develop")
        self.assertEqual("develop", self.row("here", self.inventory()).branch)

    def test_given_matrix_os_lists_when_inventoried_then_one_job_per_operating_system(self):
        make_clone(self.root, "listy", {"m.yml": MATRIX_LIST})
        make_clone(self.root, "included", {"m.yml": MATRIX_INCLUDE})
        self.assertEqual(["m.yml/test on ubuntu-latest → check test",
                          "m.yml/test on windows-latest stays (Actions)"],
                         [j.text() for j in self.row("listy", self.inventory()).jobs])
        self.assertEqual(["m.yml/test on windows-latest stays (Actions)",
                          "m.yml/test on ubuntu-latest → check test"],
                         [j.text() for j in self.row("included", self.inventory()).jobs])

    def test_given_an_expression_it_cannot_resolve_when_inventoried_then_the_job_is_unknown(self):
        make_clone(self.root, "odd", {"odd.yml": UNRESOLVABLE})
        job = self.row("odd", self.inventory()).jobs[0]
        self.assertEqual("odd.yml/test on ${{ inputs.runner }} unknown — check by hand", job.text())

    def test_given_a_call_to_a_local_reusable_workflow_when_inventoried_then_it_points_to_that_workflow(self):
        make_clone(self.root, "caller", {
            "ci.yml": CALLS,
            "verify.yml": CI})
        texts = [j.text() for j in self.row("caller", self.inventory()).jobs]
        self.assertIn("ci.yml/verify calls verify.yml (judged there)", texts)
        self.assertIn("ci.yml/elsewhere on reusable workflow someone/else/.github/workflows/x.yml@v1 "
                      "unknown — check by hand", texts)

    def test_given_a_workflow_that_is_not_yaml_when_inventoried_then_it_is_reported_for_a_hand_check(self):
        make_clone(self.root, "broken", {"bad.yml": "jobs: [unclosed\n"})
        row = self.row("broken", self.inventory())
        self.assertEqual(["bad.yml: unreadable — check by hand"], [j.text() for j in row.jobs])

    def test_given_a_pages_workflow_when_inventoried_then_the_repository_has_pages_and_its_jobs_go(self):
        make_clone(self.root, "docs-site", {"docs.yml": PAGES})
        row = self.row("docs-site", self.inventory())
        self.assertTrue(row.pages)
        self.assertFalse(row.vercel)
        self.assertEqual(["docs.yml/build on ubuntu-latest goes (Pages)",
                          "docs.yml/deploy on ubuntu-latest goes (Pages)"], [j.text() for j in row.jobs])

    def test_given_a_vercel_json_when_inventoried_then_the_repository_has_vercel(self):
        make_clone(self.root, "blog", vercel=True)
        row = self.row("blog", self.inventory())
        self.assertTrue(row.vercel)
        self.assertFalse(row.pages)

    def test_given_a_dependabot_file_when_inventoried_then_its_ecosystems_are_listed(self):
        make_clone(self.root, "dep", dependabot=DEPENDABOT)
        self.assertEqual(["nuget", "github-actions"], self.row("dep", self.inventory()).dependabot)

    def test_given_a_branch_policy_workflow_when_inventoried_then_the_branch_model_is_release(self):
        make_clone(self.root, "policed", {"branch-policy.yml": "on: pull_request\njobs:\n  p:\n"
                                                              "    runs-on: ubuntu-latest\n    steps: []\n"})
        make_clone(self.root, "plain", {"ci.yml": CI})
        rows = self.inventory()
        self.assertEqual("release", self.row("policed", rows).branch_model)
        self.assertEqual("basic", self.row("plain", rows).branch_model)
        self.assertEqual(["branch-policy.yml/p on ubuntu-latest goes (branch-policy)"],
                         [j.text() for j in self.row("policed", rows).jobs])


class KindTests(Fixture):
    def test_given_the_specs_table_when_a_repository_is_listed_then_its_kinds_come_from_the_table(self):
        for name in ("heimdall-api", "dotnet-util", "maestro", "dotnet-common", "file-tree-generator",
                     "some-experiment"):
            make_clone(self.root, name)
        rows = inv.inventory(self.root)
        self.assertEqual(["application"], self.row("heimdall-api", rows).kinds[:1])
        self.assertEqual(["library", "site"], self.row("dotnet-util", rows).kinds)
        self.assertEqual(["ci"], self.row("maestro", rows).kinds)
        self.assertEqual(["archived"], self.row("dotnet-common", rows).kinds)
        self.assertEqual(["fork"], self.row("file-tree-generator", rows).kinds)
        self.assertEqual(["code only"], self.row("some-experiment", rows).kinds)

    def test_given_a_repository_of_several_kinds_when_listed_then_all_its_kinds_are_shown(self):
        make_clone(self.root, "orpheus")
        make_clone(self.root, "reveal.js")
        rows = inv.inventory(self.root)
        self.assertEqual(["ci", "site"], self.row("orpheus", rows).kinds)
        self.assertEqual(["ci", "fork"], self.row("reveal.js", rows).kinds)

    def test_given_yggdrasil_when_listed_then_it_is_ci_as_the_catalog_section_makes_it_a_repositories_entry(self):
        make_clone(self.root, "yggdrasil", {"ci.yml": CI})
        row = inv.inventory(self.root)[0]
        self.assertEqual(["ci"], row.kinds)
        self.assertEqual([], row.notes)

    def test_given_a_vercel_site_of_the_spec_when_listed_then_the_kind_says_site_vercel_even_with_no_vercel_json(self):
        make_clone(self.root, "personal-page")
        make_clone(self.root, "heimdall-docs", {"docs.yml": PAGES})
        rows = inv.inventory(self.root)
        table = inv.render_table(rows)
        self.assertIn("| personal-page | site (Vercel) |", table)
        self.assertIn("| heimdall-docs | site |", table)
        # The note stays, as information: Vercel's git integration needs no vercel.json.
        note = " ".join(self.row("personal-page", rows).notes)
        self.assertIn("no vercel.json", note)
        self.assertIn("git integration", note)

    def test_given_the_spec_and_the_observation_agree_when_inventoried_then_there_is_no_note(self):
        make_clone(self.root, "heimdall-docs", {"docs.yml": PAGES})
        self.assertEqual([], inv.inventory(self.root)[0].notes)

    def test_given_a_code_only_repository_with_workflows_when_inventoried_then_a_note_says_they_disagree(self):
        make_clone(self.root, "some-experiment", {"ci.yml": CI})
        note = " ".join(inv.inventory(self.root)[0].notes)
        self.assertIn("code only", note)
        self.assertIn("1 workflow found", note)

    def test_given_pages_in_a_repository_the_spec_does_not_call_a_site_then_a_note_says_so(self):
        make_clone(self.root, "some-experiment", {"docs.yml": PAGES})
        self.assertIn("Pages", " ".join(inv.inventory(self.root)[0].notes))

    def test_given_a_site_with_no_pages_or_vercel_when_inventoried_then_a_note_says_so(self):
        make_clone(self.root, "personal-blog")
        note = " ".join(inv.inventory(self.root)[0].notes)
        self.assertIn("site", note)
        self.assertIn("vercel.json", note)

    def test_given_a_library_with_no_workflows_when_inventoried_then_a_note_says_so(self):
        make_clone(self.root, "dotnet-util")
        self.assertTrue(any("no workflows" in n for n in inv.inventory(self.root)[0].notes))

    def test_given_github_says_fork_or_archived_when_it_differs_from_the_spec_then_a_note_says_so(self):
        make_clone(self.root, "dotnet-common")
        make_clone(self.root, "file-tree-generator")
        make_clone(self.root, "tidy")
        rows = inv.inventory(self.root, github=[github("dotnet-common", archived=False),
                                                github("file-tree-generator", fork=True),
                                                github("tidy", fork=True, archived=True)])
        self.assertTrue(any("archived" in n for n in self.row("dotnet-common", rows).notes))
        self.assertEqual([], self.row("file-tree-generator", rows).notes)
        tidy = " ".join(self.row("tidy", rows).notes)
        self.assertIn("fork", tidy)
        self.assertIn("archived", tidy)

    def test_given_a_clone_github_does_not_list_when_inventoried_then_a_note_says_it_is_not_on_github(self):
        make_clone(self.root, "stray")
        rows = inv.inventory(self.root, github=[github("other")])
        self.assertTrue(any("not on GitHub" in n for n in self.row("stray", rows).notes))

    def test_given_no_github_listing_when_inventoried_then_github_facts_are_unknown(self):
        make_clone(self.root, "tidy")
        row = inv.inventory(self.root)[0]
        self.assertEqual("unknown", row.github)
        self.assertEqual([], row.notes)

    def test_given_a_github_listing_when_inventoried_then_forks_and_archived_are_shown(self):
        make_clone(self.root, "a")
        make_clone(self.root, "b")
        make_clone(self.root, "c")
        rows = inv.inventory(self.root, github=[github("a"), github("b", fork=True), github("c", archived=True)])
        self.assertEqual(["plain", "fork", "archived"], [self.row(n, rows).github for n in "abc"])

    def test_given_the_spec_table_when_checked_then_every_name_is_in_the_table_once_per_kind(self):
        for kind, names in inv.KINDS.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(names), len(set(names)))
        self.assertEqual(12, len([n for n in inv.KINDS["library"]]))
        self.assertEqual(5, len(inv.KINDS["application"]))
        self.assertEqual(12, len(inv.KINDS["ci"]))
        self.assertEqual(4, len(inv.KINDS["archived"]))
        self.assertEqual(9, len(inv.KINDS["fork"]))
        # 19 Pages sites and 5 Vercel projects.
        self.assertEqual(24, len(inv.KINDS["site"]))


class RenderTests(Fixture):
    def test_given_rows_when_rendered_then_each_repository_is_one_line_with_the_two_empty_columns(self):
        make_clone(self.root, "widget", {"ci.yml": CI}, dependabot=DEPENDABOT)
        make_clone(self.root, "blog", vercel=True)
        table = inv.render_table(inv.inventory(self.root))
        lines = table.splitlines()
        self.assertEqual(2 + 2, len(lines))
        self.assertTrue(lines[0].startswith("| Repository |"))
        self.assertTrue(lines[0].endswith("| Prepared branch | Moved |"))
        widget = next(line for line in lines if line.startswith("| widget "))
        self.assertIn("ci.yml/build on ubuntu-latest → check build", widget)
        self.assertIn("`REPORTGENERATOR_LICENSE`", widget)
        self.assertIn("nuget, github-actions", widget)
        self.assertTrue(widget.endswith("|  |  |"))

    def test_given_a_document_when_written_then_it_says_to_run_the_tool_with_windows_python_and_git(self):
        text = inv.document([])
        self.assertIn("Windows Python and git", text)
        self.assertIn("CRLF", text)

    def test_given_a_pipe_in_a_cell_when_rendered_then_it_is_escaped(self):
        row = inv.Row(name="x", kinds=["code only"], notes=["a | b"])
        self.assertIn("a \\| b", inv.render_table([row]))

    def test_given_a_filled_checklist_when_written_again_then_the_two_filled_columns_are_kept(self):
        make_clone(self.root, "widget", {"ci.yml": CI})
        make_clone(self.root, "other")
        out = self.root / "out" / "checklist.md"
        out.parent.mkdir()
        rows = inv.inventory(self.root)
        out.write_text(inv.document(rows), encoding="utf-8")
        text = out.read_text(encoding="utf-8")
        filled = text.replace("| widget |", "| widget |", 1)
        lines = filled.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("| widget "))
        lines[index] = lines[index][:-len("|  |  |")] + "| feature/move-to-forgejo | 2026-11-01 |"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        carried = inv.carried_columns(out)
        self.assertEqual(("feature/move-to-forgejo", "2026-11-01"), carried["widget"])
        rendered = inv.document(rows, carried)
        widget = next(line for line in rendered.splitlines() if line.startswith("| widget "))
        self.assertTrue(widget.endswith("| feature/move-to-forgejo | 2026-11-01 |"))

    def test_given_no_checklist_yet_when_columns_are_carried_then_there_are_none(self):
        self.assertEqual({}, inv.carried_columns(self.root / "nothing.md"))


class MainTests(Fixture):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = inv.main([str(a) for a in argv])
        return status, out.getvalue(), err.getvalue()

    def test_given_no_token_when_run_then_it_works_from_the_clones_alone_and_says_so(self):
        make_clone(self.root, "tidy")
        with mock.patch.object(inv.rules, "find_github_token", return_value=""):
            status, out, err = self.run_main(self.root)
        self.assertEqual(0, status)
        self.assertIn("| tidy ", out)
        self.assertIn("no GitHub token", err)

    def test_given_a_token_when_run_then_the_listing_comes_from_migrate_and_nothing_is_written(self):
        make_clone(self.root, "tidy")
        with mock.patch.object(inv.rules, "find_github_token", return_value="tok"), \
                mock.patch.object(inv.migrate, "github_repositories",
                                  return_value=[github("tidy"), github("gone")]) as listing:
            status, out, _ = self.run_main(self.root, "--github-owner", "someone")
        self.assertEqual(0, status)
        self.assertEqual("someone", listing.call_args.args[1])
        self.assertIn("| gone ", out)
        self.assertIn("missing", out)

    def test_given_github_fails_when_run_then_it_goes_on_with_the_clones_and_reports_the_failure(self):
        make_clone(self.root, "tidy")
        with mock.patch.object(inv.rules, "find_github_token", return_value="tok"), \
                mock.patch.object(inv.migrate, "github_repositories",
                                  side_effect=inv.rules.ApiError("GET", "/user", 401, "Bad credentials")):
            status, out, err = self.run_main(self.root)
        self.assertEqual(0, status)
        self.assertIn("| tidy ", out)
        self.assertIn("Bad credentials", err)
        self.assertNotIn("tok", err.replace("token", ""))

    def test_given_an_output_file_when_run_then_the_document_is_written_there(self):
        make_clone(self.root, "tidy")
        target = self.root / "migration-checklist.md"
        with mock.patch.object(inv.rules, "find_github_token", return_value=""):
            status, _, _ = self.run_main(self.root, "--output", target)
        self.assertEqual(0, status)
        text = target.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Migration checklist"))
        self.assertIn("| tidy ", text)

    def test_given_a_missing_directory_when_run_then_it_exits_2(self):
        status, _, err = self.run_main(self.root / "nowhere")
        self.assertEqual(2, status)
        self.assertIn("not a directory", err)


if __name__ == "__main__":
    unittest.main()
