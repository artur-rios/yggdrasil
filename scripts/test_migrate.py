"""Tests for scripts/migrate.py (the move from GitHub to Forgejo).   python3 -m unittest discover -s scripts"""

import io
import json
import os
import unittest
from unittest import mock

import migrate

OWNER = "artur-rios"
MIRROR = "artur-rios-mirror"

CATALOG = {"owner": OWNER, "environments": [{"id": "dev", "name": "Dev", "trigger": "manual"}],
           "systems": [{"id": "shop", "name": "Shop", "applications": [
               {"id": "shop-api", "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz"}]}],
           "repositories": [{"id": "toolkit"}]}
CATALOG_MIRROR = {**CATALOG, "forge": {"type": "forgejo", "url": "https://git.example.com"},
                  "mirror": {"github": MIRROR}}


def repository(name, **fields):
    """A repository as GitHub's list answers it (more fields than migrate keeps, as the real one does)."""
    return {"id": 1, "name": name, "full_name": f"x/{name}", "private": False, "fork": False, "archived": False,
            "default_branch": "develop", "has_wiki": False, "size": 120, "open_issues_count": 0, **fields}


class FakeGitHub:
    """Stands in for rules.Api: keeps an owner's repositories (and their open pull requests) and answers
    like GitHub does, in pages of at most `page_size` however many are asked for, so a caller that trusts
    its own per_page loses the rest."""

    def __init__(self, repos, pulls=None, kind="User", login=OWNER, page_size=30, pulls_fail=(), empty=(),
                 commits_status=None, rulesets=None, settings=None, fail=None):
        self.repos, self.pulls, self.pulls_fail = repos, pulls or {}, set(pulls_fail)
        self.empty, self.commits_status = set(empty), commits_status or {}
        self.kind, self.login, self.page_size = kind, login, page_size
        # What prepare-github works on, per repository: `rulesets` (name -> [rulesets, as {id, name}]),
        # `settings` (name -> fields that override the defaults of an untouched copy), `fail` (name ->
        # {(method, path suffix): status} to answer with an error).
        self.rulesets, self.settings, self.fail = rulesets or {}, settings or {}, fail or {}
        self.state = {}
        self.calls = []

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def _repo_state(self, name):
        if name not in self.state:
            self.state[name] = {"has_issues": True, "description": "old description", "fixes": True,
                                "rulesets": list(self.rulesets.get(name, [])), **self.settings.get(name, {})}
        return self.state[name]

    def _prepare_request(self, method, path, segments, body):
        """The routes prepare-github uses: the repository itself, its rulesets and its automated security
        fixes. NotImplemented for any other."""
        name, rest = segments[3], segments[4:]
        if rest and rest[0] not in ("rulesets", "automated-security-fixes"):
            return NotImplemented
        for (failing_method, suffix), status in self.fail.get(name, {}).items():
            if method == failing_method and path.split("?")[0].endswith(suffix):
                raise migrate.rules.ApiError(method, path, status, "refused")
        state = self._repo_state(name)
        if rest == []:
            if method == "PATCH":
                state.update(body)
            return {"name": name, "has_issues": state["has_issues"], "description": state["description"]}
        if rest == ["rulesets"] and method == "GET":
            return list(state["rulesets"])
        if len(rest) == 2 and rest[0] == "rulesets" and method == "DELETE":
            state["rulesets"] = [r for r in state["rulesets"] if str(r["id"]) != rest[1]]
            return None
        if rest == ["automated-security-fixes"]:
            if method == "GET":
                return {"enabled": state["fixes"], "paused": False}
            if method == "DELETE":
                state["fixes"] = False
                return None
        raise AssertionError(f"unexpected request: {method} {path}")

    def _page(self, items, query):
        page = int(query.get("page", "1"))
        size = min(int(query.get("per_page", "30")), self.page_size)
        return items[(page - 1) * size:page * size]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        route, _, raw = path.partition("?")
        query = dict(part.split("=") for part in raw.split("&") if part)
        segments = route.split("/")  # ["", "repos", owner, repo, "pulls"]
        if segments[:2] == ["", "repos"] and len(segments) >= 4:
            answer = self._prepare_request(method, path, segments, body)
            if answer is not NotImplemented:
                return answer
        assert method == "GET", f"only prepare-github writes: {method} {path}"
        if route.startswith("/users/"):
            return {"login": segments[2], "type": self.kind}
        if route == "/user":
            return {"login": self.login}
        if route in ("/user/repos", f"/orgs/{OWNER}/repos"):
            return self._page(self.repos, query)
        if segments[:2] == ["", "repos"] and segments[4] == "commits":
            # GitHub answers 409 "Git Repository is empty." for a repository without a commit.
            if segments[3] in self.commits_status:
                raise migrate.rules.ApiError(method, path, self.commits_status[segments[3]], "Server Error")
            if segments[3] in self.empty:
                raise migrate.rules.ApiError(method, path, 409, "Git Repository is empty.")
            return [{"sha": "abc123"}]
        if segments[:2] == ["", "repos"] and segments[4] == "pulls":
            assert query.get("state") == "open", path
            if segments[3] in self.pulls_fail:
                raise migrate.rules.ApiError(method, path, 502, "Bad Gateway")
            return self._page([{"number": n} for n in range(self.pulls.get(segments[3], 0))], query)
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeForgejo:
    """Stands in for rules.Api on Forgejo: keeps the repositories it has (`existing`: name -> the JSON
    `GET /repos/<owner>/<name>` answers), takes migrations, and answers the way Forgejo does. `lookup` maps
    a name to a status its GET answers instead (403, 500 ...); `migrate_status` maps a name to the status its
    migration answers (500, 0 for no answer at all). Every request is kept in `calls`, with its timeout."""

    def __init__(self, existing=None, lookup=None, migrate_status=None, migrate_message=None, files=None,
                 contents_status=None):
        self.existing, self.lookup, self.migrate_status = existing or {}, lookup or {}, migrate_status or {}
        self.migrate_message = migrate_message
        # The files prepare-github looks for: (repository, path) -> the file's sha. `contents_status` maps a
        # repository to a status its contents requests answer instead (500 ...).
        self.files, self.contents_status = dict(files or {}), contents_status or {}
        self.calls, self.timeouts = [], []

    def _contents(self, method, path, body):
        """`GET|DELETE /repos/<owner>/<name>/contents/<file>`: Forgejo answers 404 for a file that is not
        there and, for a delete, wants the file's current sha."""
        route, _, query = path.partition("?")
        name, _, filepath = route[len(f"/repos/{OWNER}/"):].partition("/contents/")
        if name in self.contents_status:
            raise migrate.rules.ApiError(method, path, self.contents_status[name], "contents failed")
        sha = self.files.get((name, filepath))
        if sha is None:
            raise migrate.rules.ApiError(method, path, 404, "The target couldn't be found.")
        if method == "GET":
            return {"type": "file", "path": filepath, "sha": sha, "ref": query}
        if body.get("sha") != sha:
            raise migrate.rules.ApiError(method, path, 422, "sha does not match")
        del self.files[(name, filepath)]
        return {"commit": {"message": body["message"]}}

    def request(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        self.timeouts.append(timeout)
        if "/contents/" in path:
            return self._contents(method, path, body)
        if method == "GET" and path.startswith(f"/repos/{OWNER}/"):
            name = path.rsplit("/", 1)[1]
            if name in self.lookup:
                raise migrate.rules.ApiError(method, path, self.lookup[name], "lookup failed")
            if name in self.existing:
                return self.existing[name]
            raise migrate.rules.ApiError(method, path, 404, "The target couldn't be found.")
        if (method, path) == ("POST", "/repos/migrate"):
            status = self.migrate_status.get(body["repo_name"])
            if status is not None:
                raise migrate.rules.ApiError(method, path, status, self.migrate_message or (
                    "migration failed" if status else "timed out"))
            self.existing[body["repo_name"]] = forgejo_repository(body["repo_name"])
            return self.existing[body["repo_name"]]
        raise AssertionError(f"unexpected request: {method} {path}")

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def migrations(self):
        return [call[2] for call in self.calls if call[:2] == ("POST", "/repos/migrate")]


def forgejo_repository(name, **fields):
    """A repository as Forgejo answers `GET /repos/<owner>/<name>` (more fields than migrate reads)."""
    return {"id": 7, "name": name, "full_name": f"{OWNER}/{name}", "empty": False, "mirror": False,
            "default_branch": "develop", **fields}


def sixty(**by_name):
    """Sixty repositories repo-00 .. repo-59 (`by_name` overrides fields of some)."""
    return [repository(f"repo-{i:02d}", **by_name.get(f"repo-{i:02d}", {})) for i in range(60)]


class GitHubRepositoriesTests(unittest.TestCase):
    def test_given_sixty_repositories_on_two_pages_when_inventoried_then_every_one_is_listed(self):
        gh = FakeGitHub(sixty())
        listed = migrate.github_repositories(gh, OWNER)
        self.assertEqual([r["name"] for r in listed], [f"repo-{i:02d}" for i in range(60)])
        # Two pages, then the empty one that says there are no more.
        self.assertEqual(len([c for c in gh.calls if c[1].startswith("/user/repos")]), 3)

    def test_given_a_repository_when_listed_then_it_keeps_only_what_the_move_needs(self):
        gh = FakeGitHub([repository("shop-api", private=True, fork=True, archived=True, has_wiki=True,
                                    default_branch="main")], pulls={"shop-api": 3})
        self.assertEqual(migrate.github_repositories(gh, OWNER),
                         [{"name": "shop-api", "private": True, "fork": True, "archived": True,
                           "default_branch": "main", "has_wiki": True, "size": 120}])
        # The pull requests are counted apart, one repository at a time (open_pull_requests), so a command
        # can print a repository's line as soon as it has it and one that fails does not stop the others.
        self.assertFalse([c for c in gh.calls if "/pulls" in c[1]])
        self.assertEqual(migrate.open_pull_requests(gh, OWNER, "shop-api"), 3)

    def test_given_more_open_pull_requests_than_a_page_when_counted_then_all_are_counted(self):
        gh = FakeGitHub([repository("busy")], pulls={"busy": 250}, page_size=100)
        self.assertEqual(migrate.open_pull_requests(gh, OWNER, "busy"), 250)

    def test_given_an_organisation_when_listed_then_all_its_repositories_are_asked_for(self):
        gh = FakeGitHub([repository("a")], kind="Organization", login="somebody-else")
        self.assertEqual([r["name"] for r in migrate.github_repositories(gh, OWNER)], ["a"])
        self.assertIn(("GET", f"/orgs/{OWNER}/repos?type=all&per_page=100&page=1", None), gh.calls)

    def test_given_a_token_of_another_user_when_listed_then_it_is_refused_not_listed_without_the_private_ones(self):
        gh = FakeGitHub([repository("a")], login="somebody-else")
        with self.assertRaises(migrate.MigrateError) as raised:
            migrate.github_repositories(gh, OWNER)
        self.assertIn("somebody-else", str(raised.exception))
        self.assertFalse([c for c in gh.calls if c[1].startswith("/user/repos")])

    def test_given_only_names_when_listed_then_just_those_are_returned(self):
        gh = FakeGitHub(sixty(), pulls={"repo-03": 1})
        listed = migrate.github_repositories(gh, OWNER, only=["repo-03"])
        self.assertEqual([r["name"] for r in listed], ["repo-03"])
        self.assertEqual(migrate.open_pull_requests(gh, OWNER, "repo-03"), 1)
        self.assertEqual(len([c for c in gh.calls if "/pulls" in c[1]]), 2)  # its page and the empty one


class Run:
    """migrate.main() with the catalog, the environment and both APIs replaced (the GitHub one by `gh`, the
    Forgejo one by `forgejo`), and what it printed."""

    def __init__(self, args, gh=None, catalog=CATALOG, env=None, token="gh-tok-456", forgejo=None, out=None):
        self.gh = gh or FakeGitHub(sixty())
        self.forgejo = forgejo or FakeForgejo()
        self.made = []

        def make_api(base, token_, scheme, headers=None):
            self.made.append((base, token_, scheme))
            return self.forgejo if scheme == "token" else self.gh

        out, err = out or io.StringIO(), io.StringIO()
        with mock.patch.object(migrate.catalog_module, "load", return_value=catalog), \
                mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(migrate.rules, "Api", make_api), \
                mock.patch.object(migrate.rules, "find_github_token", return_value=token), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            self.code = migrate.main(args)
        self.out, self.err = out.getvalue(), err.getvalue()
        self.lines = self.out.splitlines()


class InventoryTests(unittest.TestCase):
    ARGS = ["--github-owner", OWNER, "inventory"]

    def test_given_sixty_repositories_when_inventoried_then_one_line_each_and_totals_and_exit_0(self):
        run = Run(self.ARGS, FakeGitHub(sixty(**{"repo-01": {"private": True}, "repo-02": {"fork": True}})))
        rows = [line.split("\t") for line in run.lines if "\t" in line]
        self.assertEqual(len(rows), 60)
        self.assertEqual(rows[0], ["repo-00", "public", "no", "0", "no"])
        self.assertEqual(rows[1][1], "private")
        self.assertEqual(rows[2][2], "yes")
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("60 repositories", run.lines[-1])
        self.assertIn("1 private", run.lines[-1])
        self.assertIn("1 fork,", run.lines[-1])
        self.assertIn("0 open pull requests", run.lines[-1])

    def test_given_open_pull_requests_when_inventoried_then_they_are_named_and_the_run_exits_1(self):
        run = Run(self.ARGS, FakeGitHub(sixty(), pulls={"repo-07": 2, "repo-40": 1}))
        self.assertEqual(run.code, 1)
        failures = [line for line in run.err.splitlines() if "open pull request" in line]
        self.assertEqual(len(failures), 2, run.err)
        self.assertIn("repo-07: 2 open pull requests", failures[0])
        self.assertIn("repo-40: 1 open pull request", failures[1])
        self.assertIn("finish or close them first", run.err)
        # The count is in the table too, and the run carried on to the end of the list.
        self.assertIn("repo-07\tpublic\tno\t2\tno", run.lines)
        self.assertIn("repo-59\tpublic\tno\t0\tno", run.lines)
        self.assertIn("3 open pull requests", run.lines[-1])

    def test_given_a_repository_in_the_catalog_when_inventoried_then_it_is_marked(self):
        repos = [repository("shop-api"), repository("toolkit"), repository("old-experiment", fork=True)]
        run = Run(self.ARGS, FakeGitHub(repos))
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("shop-api\tpublic\tno\t0\tyes", run.lines)  # an application's repository
        self.assertIn("toolkit\tpublic\tno\t0\tyes", run.lines)  # a `repositories` entry
        self.assertIn("old-experiment\tpublic\tyes\t0\tno", run.lines)  # a fork stays in the move, outside the catalog
        self.assertIn("2 in the catalog", run.lines[-1])

    def test_given_a_catalog_repository_missing_from_github_when_inventoried_then_it_is_noted(self):
        run = Run(self.ARGS, FakeGitHub([repository("toolkit")]))
        self.assertEqual(run.code, 0, run.err)
        self.assertTrue(any("shop-api" in line and "not on GitHub" in line for line in run.lines), run.out)

    def test_given_only_when_inventoried_then_just_those_are_listed(self):
        run = Run(self.ARGS + ["--only", "repo-05", "repo-06"])
        self.assertEqual([line.split("\t")[0] for line in run.lines if "\t" in line], ["repo-05", "repo-06"])
        self.assertEqual(run.code, 0, run.err)

    def test_given_an_unknown_name_in_only_when_run_then_it_exits_2_before_listing_anything(self):
        run = Run(self.ARGS + ["--only", "no-such-repo"])
        self.assertEqual(run.code, 2)
        self.assertIn("no-such-repo", run.err)
        self.assertEqual(run.out, "")

    def test_given_one_repository_whose_pull_requests_cannot_be_counted_when_inventoried_then_the_rest_are_listed(self):
        # One failing request must not hide the other lines: the line is printed (count unknown), the
        # failure names the repository, and the run goes on to the end.
        run = Run(self.ARGS, FakeGitHub(sixty(), pulls={"repo-40": 1}, pulls_fail={"repo-07"}))
        self.assertEqual(run.code, 1)
        self.assertEqual(len([line for line in run.lines if "\t" in line]), 60)
        self.assertIn("repo-07\tpublic\tno\t?\tno", run.lines)
        self.assertIn("repo-40\tpublic\tno\t1\tno", run.lines)
        failures = [line for line in run.err.splitlines() if line.startswith("  repo-")]
        self.assertEqual(len(failures), 2, run.err)
        self.assertIn("repo-07: could not count the open pull requests", failures[0])
        self.assertIn("502 Bad Gateway", failures[0])
        self.assertIn("repo-40: 1 open pull request", failures[1])
        self.assertIn("1 repository not counted", run.lines[-1])

    def test_given_repositories_being_counted_when_inventoried_then_each_line_is_out_before_the_next_count(self):
        # Lines are printed as they happen, not after the last repository was counted.
        out, seen = io.StringIO(), []
        counted = migrate.open_pull_requests

        def counting(api, owner, name):
            seen.append((name, len(out.getvalue().splitlines())))
            return counted(api, owner, name)

        with mock.patch.object(migrate, "open_pull_requests", counting):
            run = Run(self.ARGS, FakeGitHub(sixty()), out=out)
        self.assertEqual(run.code, 0, run.err)
        # The k-th repository is counted when the k lines of the ones before it are already out.
        self.assertEqual(seen[:3], [("repo-00", 0), ("repo-01", 1), ("repo-02", 2)])

    def test_given_dry_run_when_inventoried_then_it_is_the_same_read_only_run(self):
        run = Run(["--dry-run"] + self.ARGS)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(len([line for line in run.lines if "\t" in line]), 60)


FORGEJO_CATALOG = {**CATALOG, "forge": {"type": "forgejo", "url": "https://git.example.com"}}
ENV = {"FORGE_TOKEN": "fg-tok-123"}


class ImportTests(unittest.TestCase):
    ARGS = ["--github-owner", OWNER, "import"]

    def run_import(self, repos, args=None, env=None, forgejo=None, pulls=None, pulls_fail=(), **github):
        gh = FakeGitHub(repos, pulls=pulls, pulls_fail=pulls_fail, **github)
        return Run(args or self.ARGS, gh, catalog=FORGEJO_CATALOG, env=ENV if env is None else env, forgejo=forgejo)

    def test_given_a_new_repository_when_imported_then_the_migration_asks_for_issues_pulls_and_releases(self):
        run = self.run_import([repository("toolkit", private=True, has_wiki=True)])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.forgejo.migrations(), [{
            "service": "github", "clone_addr": f"https://github.com/{OWNER}/toolkit.git", "auth_token": "gh-tok-456",
            "repo_owner": OWNER, "repo_name": "toolkit", "private": True, "mirror": False, "issues": True,
            "labels": True, "milestones": True, "releases": True, "pull_requests": True, "wiki": True, "lfs": False}])
        self.assertIn("toolkit: imported", run.lines)
        # It looked first (a 404 says: not there yet), then migrated, and wrote nothing else.
        self.assertEqual([c[:2] for c in run.forgejo.calls],
                         [("GET", f"/repos/{OWNER}/toolkit"), ("POST", "/repos/migrate")])

    def test_given_a_public_repository_without_a_wiki_when_imported_then_it_stays_public_and_asks_for_no_wiki(self):
        run = self.run_import([repository("toolkit")])
        migration = run.forgejo.migrations()[0]
        self.assertEqual((migration["private"], migration["wiki"]), (False, False))

    def test_given_a_repository_already_on_forgejo_when_imported_then_it_is_left_as_it_is(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")})
        run = self.run_import([repository("toolkit"), repository("shop-api")], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("toolkit: already imported", run.lines)
        self.assertIn("shop-api: imported", run.lines)
        self.assertEqual([m["repo_name"] for m in forgejo.migrations()], ["shop-api"])
        self.assertEqual(len(forgejo.writes()), 1)  # the one migration: nothing touched toolkit

    def test_given_one_import_answering_500_when_imported_then_the_rest_go_on_and_the_run_exits_1(self):
        forgejo = FakeForgejo(migrate_status={"b": 500})
        run = self.run_import([repository("a"), repository("b"), repository("c")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertEqual([m["repo_name"] for m in forgejo.migrations()], ["a", "b", "c"])
        self.assertIn("a: imported", run.lines)
        self.assertIn("c: imported", run.lines)
        # The failure is printed where it happened and listed again at the end, with the repository's name.
        self.assertTrue(any(line.startswith("b: import failed") and "500" in line for line in run.lines), run.out)
        self.assertEqual([line for line in run.err.splitlines() if line.startswith("  ")],
                         ["  b: import failed: POST /repos/migrate: 500 migration failed"])

    def test_given_an_import_that_gets_no_answer_when_imported_then_it_is_a_failure_of_that_repository(self):
        forgejo = FakeForgejo(migrate_status={"a": 0})
        run = self.run_import([repository("a"), repository("b")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("a: import failed: POST /repos/migrate: 0 timed out", run.err)
        self.assertIn("b: imported", run.lines)

    def test_given_a_lookup_answering_neither_200_nor_404_when_imported_then_that_repository_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(lookup={"a": 403, "b": 500})
        run = self.run_import([repository("a"), repository("b"), repository("c")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertEqual([m["repo_name"] for m in forgejo.migrations()], ["c"])  # unsure -> not migrated
        failures = [line for line in run.err.splitlines() if line.startswith("  ")]
        self.assertEqual(len(failures), 2, run.err)
        self.assertIn("a: could not look for it on Forgejo", failures[0])
        self.assertIn("403", failures[0])
        self.assertIn("b: could not look for it on Forgejo", failures[1])

    def test_given_a_repository_that_exists_but_is_empty_when_imported_then_it_is_reported_unfinished_and_not_touched(self):
        forgejo = FakeForgejo(existing={"half": forgejo_repository("half", empty=True)})
        run = self.run_import([repository("half"), repository("other")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("  half: exists but looks unfinished (empty): delete it on Forgejo and run import again",
                      run.err.splitlines())
        self.assertIn("other: imported", run.lines)
        # Never deleted, migrated over or patched: the only write is the other repository's migration.
        self.assertEqual([c[:2] for c in forgejo.writes()], [("POST", "/repos/migrate")])
        self.assertEqual([m["repo_name"] for m in forgejo.migrations()], ["other"])

    def test_given_an_empty_repository_on_github_too_when_imported_then_it_is_already_imported_not_unfinished(self):
        # A repository without a commit imports as an empty one, and deleting and importing it again gives the
        # same: "unfinished" for it would never end. GitHub is asked (409 = empty) before believing it.
        forgejo = FakeForgejo(existing={"blank": forgejo_repository("blank", empty=True)})
        run = self.run_import([repository("blank", size=0)], forgejo=forgejo, empty={"blank"})
        self.assertEqual((run.code, run.err), (0, ""), run.out)
        self.assertEqual(run.lines, ["blank: already imported (empty on GitHub too)"])
        self.assertIn(("GET", f"/repos/{OWNER}/blank/commits?per_page=1", None), run.gh.calls)
        self.assertEqual(forgejo.writes(), [])

    def test_given_an_empty_copy_of_a_repository_that_shows_size_0_but_has_commits_when_imported_then_it_is_unfinished(self):
        # `size` is in KB and lags a fresh push: 0 alone must not hide a half-done copy.
        forgejo = FakeForgejo(existing={"fresh": forgejo_repository("fresh", empty=True)})
        run = self.run_import([repository("fresh", size=0)], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("  fresh: exists but looks unfinished (empty): delete it on Forgejo and run import again",
                      run.err.splitlines())
        self.assertEqual(forgejo.writes(), [])

    def test_given_an_empty_copy_of_a_repository_with_size_when_imported_then_github_is_not_asked(self):
        forgejo = FakeForgejo(existing={"half": forgejo_repository("half", empty=True)})
        run = self.run_import([repository("half", size=300)], forgejo=forgejo, empty={"half"})
        self.assertEqual(run.code, 1)
        self.assertFalse([c for c in run.gh.calls if "/commits" in c[1]])

    def test_given_github_failing_to_say_whether_it_is_empty_when_imported_then_that_repository_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={"blank": forgejo_repository("blank", empty=True)})
        run = self.run_import([repository("blank", size=0), repository("next")], forgejo=forgejo,
                              commits_status={"blank": 500})
        self.assertEqual(run.code, 1)
        self.assertIn("  blank: could not tell whether it is empty on GitHub too", run.err)
        self.assertIn("next: imported", run.lines)

    def test_given_an_error_that_echoes_the_github_token_when_imported_then_it_is_hidden_in_the_failure(self):
        forgejo = FakeForgejo(migrate_status={"a": 500}, migrate_message="clone failed: auth gh-tok-456 rejected")
        run = self.run_import([repository("a")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertNotIn("gh-tok-456", run.out + run.err)
        self.assertIn("a: import failed: POST /repos/migrate: 500 clone failed: auth *** rejected", run.err)

    def test_given_a_repository_that_exists_as_a_mirror_when_imported_then_it_is_reported_unfinished(self):
        forgejo = FakeForgejo(existing={"half": forgejo_repository("half", mirror=True)})
        run = self.run_import([repository("half")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("half: exists but looks unfinished (mirror): delete it on Forgejo and run import again", run.err)
        self.assertEqual(forgejo.writes(), [])

    def test_given_a_copy_that_is_both_empty_and_a_mirror_when_imported_then_it_is_reported_as_a_mirror(self):
        # Both flags can be on at once: a pull mirror whose first sync has not run. The mirror is the one that
        # matters: it would keep pulling from GitHub, so it is not a finished import even when GitHub's
        # repository is empty too (which alone would be "already imported"). So the mirror is named, and
        # GitHub is not even asked whether the repository is empty.
        forgejo = FakeForgejo(existing={"both": forgejo_repository("both", empty=True, mirror=True)})
        run = self.run_import([repository("both", size=0)], forgejo=forgejo, empty={"both"})
        self.assertEqual(run.code, 1)
        self.assertIn("  both: exists but looks unfinished (mirror): delete it on Forgejo and run import again",
                      run.err.splitlines())
        self.assertFalse([c for c in run.gh.calls if "/commits" in c[1]])
        self.assertEqual(forgejo.writes(), [])

    def test_given_open_pull_requests_when_imported_then_it_refuses_unless_allowed(self):
        repos = [repository("a"), repository("b"), repository("c")]
        run = self.run_import(repos, pulls={"b": 2})
        self.assertEqual(run.code, 1)
        self.assertEqual(run.forgejo.calls, [])  # not even a lookup: it refused before touching Forgejo
        self.assertIn("b: 2 open pull requests", run.err)
        self.assertIn("--allow-open-pulls", run.err)
        self.assertNotIn("imported", run.out)

        allowed = self.run_import(repos, self.ARGS + ["--allow-open-pulls"], pulls={"b": 2})
        self.assertEqual(allowed.code, 0, allowed.err)
        self.assertEqual([m["repo_name"] for m in allowed.forgejo.migrations()], ["a", "b", "c"])
        self.assertEqual([c for c in allowed.gh.calls if "/pulls" in c[1]], [])  # it did not even count them

    def test_given_the_allow_option_on_another_command_when_run_then_it_is_unknown_there(self):
        run = self.run_import([repository("a")], ["--github-owner", OWNER, "inventory", "--allow-open-pulls"])
        self.assertEqual(run.code, 2)

    def test_given_a_repository_whose_pull_requests_cannot_be_counted_when_imported_then_it_is_not_imported_and_the_rest_are(self):
        run = self.run_import([repository("a"), repository("b"), repository("c")], pulls_fail={"b"})
        self.assertEqual(run.code, 1)
        self.assertEqual([m["repo_name"] for m in run.forgejo.migrations()], ["a", "c"])
        self.assertIn("  b: could not count the open pull requests", run.err)

    def test_given_a_dry_run_when_imported_then_nothing_is_sent_and_the_payload_hides_the_token(self):
        forgejo = FakeForgejo(existing={"there": forgejo_repository("there")})
        run = self.run_import([repository("a", private=True), repository("there")], ["--dry-run"] + self.ARGS,
                              forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(forgejo.writes(), [])  # it still looks (GET), as every command does
        self.assertEqual({c[0] for c in forgejo.calls}, {"GET"})
        self.assertNotIn("gh-tok-456", run.out + run.err)
        self.assertNotIn("fg-tok-123", run.out + run.err)
        would = [line for line in run.lines if line.startswith("a: would import")]
        self.assertEqual(len(would), 1, run.out)
        payload = json.loads(would[0].split(" ", 3)[3])
        self.assertEqual(payload["auth_token"], "***")
        self.assertEqual((payload["repo_name"], payload["private"], payload["pull_requests"]), ("a", True, True))
        self.assertIn("there: already imported", run.lines)

    def test_given_the_tokens_when_imported_then_only_the_request_body_carries_the_github_one(self):
        run = self.run_import([repository("a")])
        self.assertNotIn("gh-tok-456", run.out + run.err)
        self.assertNotIn("fg-tok-123", run.out + run.err)
        # The Forgejo client got its own token (the Authorization header, rules.Api's business) ...
        self.assertIn(("https://git.example.com/api/v1", "fg-tok-123", "token"), run.made)
        # ... and the GitHub token is in no path.
        self.assertFalse([c for c in run.forgejo.calls if "gh-tok-456" in c[1]])

    def test_given_no_timeout_setting_when_imported_then_a_migration_may_take_30_minutes(self):
        run = self.run_import([repository("a")])
        self.assertEqual(run.forgejo.timeouts, [None, 1800])  # the lookup keeps the client's own timeout

    def test_given_migrate_timeout_when_imported_then_it_sets_the_wait_for_a_migration(self):
        run = self.run_import([repository("a")], env={**ENV, "MIGRATE_TIMEOUT": "90"})
        self.assertEqual(run.forgejo.timeouts, [None, 90])

    def test_given_a_migrate_timeout_that_is_not_a_positive_number_when_imported_then_it_exits_2(self):
        for value in ("soon", "0", "-5"):
            with self.subTest(value=value):
                run = self.run_import([repository("a")], env={**ENV, "MIGRATE_TIMEOUT": value})
                self.assertEqual(run.code, 2)
                self.assertIn("MIGRATE_TIMEOUT", run.err)
                self.assertEqual(run.forgejo.calls, [])

    def test_given_no_forgejo_token_when_imported_then_it_exits_2_before_anything_is_printed(self):
        run = self.run_import([repository("a")], env={})
        self.assertEqual(run.code, 2)
        self.assertIn("FORGE_TOKEN", run.err)
        self.assertEqual(run.out, "")

    def test_given_only_when_imported_then_just_those_are_imported(self):
        run = self.run_import([repository("a"), repository("b"), repository("c")], self.ARGS + ["--only", "b"],
                              pulls={"a": 3})
        self.assertEqual(run.code, 0, run.err)  # a's pull requests are not b's business
        self.assertEqual([m["repo_name"] for m in run.forgejo.migrations()], ["b"])

    def test_given_a_run_again_after_a_failure_when_imported_then_it_picks_up_where_it_stopped(self):
        forgejo = FakeForgejo(migrate_status={"b": 500})
        repos = [repository("a"), repository("b")]
        first = self.run_import(repos, forgejo=forgejo)
        self.assertEqual(first.code, 1)
        forgejo.migrate_status.clear()
        second = self.run_import(repos, forgejo=forgejo)
        self.assertEqual(second.code, 0, second.err)
        self.assertIn("a: already imported", second.lines)
        self.assertIn("b: imported", second.lines)


DEPENDABOT = ".github/dependabot.yml"
DEPENDABOT_MESSAGE = "chore: stop dependabot; renovate replaces it"


def description(name, base="https://git.example.com"):
    # The em dash (U+2014) is part of the text.
    return f"Mirror of {base}/{OWNER}/{name} — contribute there"


class PrepareGitHubTests(unittest.TestCase):
    ARGS = ["--github-owner", OWNER, "prepare-github"]

    def run_prepare(self, repos, args=None, forgejo=None, env=None, catalog=FORGEJO_CATALOG, gh=None, **github):
        """Every repository is on Forgejo unless `forgejo` says otherwise."""
        forgejo = forgejo or FakeForgejo(existing={r["name"]: forgejo_repository(r["name"]) for r in repos})
        gh = gh or FakeGitHub(repos, **github)
        return Run(args or self.ARGS, gh, catalog=catalog, env=ENV if env is None else env, forgejo=forgejo)

    def test_given_a_dependabot_file_when_prepared_then_it_is_deleted_on_forgejos_default_branch(self):
        # Forgejo's own default branch counts (the copy is what the file is deleted on), not GitHub's.
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit", default_branch="main")},
                              files={("toolkit", DEPENDABOT): "sha-1"})
        run = self.run_prepare([repository("toolkit")], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(forgejo.writes(), [
            ("DELETE", f"/repos/{OWNER}/toolkit/contents/{DEPENDABOT}",
             {"sha": "sha-1", "branch": "main", "message": DEPENDABOT_MESSAGE})])
        # The sha came from reading the file on that branch.
        self.assertIn(("GET", f"/repos/{OWNER}/toolkit/contents/{DEPENDABOT}?ref=main", None), forgejo.calls)
        self.assertIn("toolkit: deleted .github/dependabot.yml on main", run.lines)
        self.assertEqual(forgejo.files, {})

    def test_given_the_yaml_spelling_when_prepared_then_that_file_is_deleted_too(self):
        # Dependabot reads both names. A repository with both loses both, in one commit each.
        forgejo = FakeForgejo(existing={"a": forgejo_repository("a"), "b": forgejo_repository("b")},
                              files={("a", ".github/dependabot.yaml"): "sha-a",
                                     ("b", DEPENDABOT): "sha-b1", ("b", ".github/dependabot.yaml"): "sha-b2"})
        run = self.run_prepare([repository("a"), repository("b")], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual([c[1] for c in forgejo.writes()], [
            f"/repos/{OWNER}/a/contents/.github/dependabot.yaml",
            f"/repos/{OWNER}/b/contents/{DEPENDABOT}",
            f"/repos/{OWNER}/b/contents/.github/dependabot.yaml"])
        self.assertEqual(forgejo.files, {})

    def test_given_no_dependabot_file_when_prepared_then_nothing_is_deleted(self):
        run = self.run_prepare([repository("toolkit")])
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("toolkit: no dependabot file", run.lines)
        self.assertEqual(run.forgejo.writes(), [])

    def test_given_an_empty_copy_when_prepared_then_forgejo_is_not_asked_for_files_and_github_is_still_prepared(self):
        forgejo = FakeForgejo(existing={"blank": forgejo_repository("blank", empty=True, default_branch="")})
        run = self.run_prepare([repository("blank", size=0)], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("blank: no dependabot file (empty repository)", run.lines)
        self.assertFalse([c for c in forgejo.calls if "/contents/" in c[1]])
        self.assertFalse(run.gh.state["blank"]["has_issues"])

    def test_given_old_rulesets_when_prepared_then_all_but_mirror_tags_are_deleted(self):
        rulesets = [{"id": 11, "name": "default"}, {"id": 12, "name": "mirror tags"}, {"id": 13, "name": "release"}]
        run = self.run_prepare([repository("toolkit")], rulesets={"toolkit": rulesets})
        self.assertEqual(run.code, 0, run.err)
        deletes = [c[1] for c in run.gh.calls if c[0] == "DELETE" and "/rulesets/" in c[1]]
        self.assertEqual(deletes, [f"/repos/{OWNER}/toolkit/rulesets/11", f"/repos/{OWNER}/toolkit/rulesets/13"])
        self.assertEqual(run.gh.state["toolkit"]["rulesets"], [{"id": 12, "name": "mirror tags"}])
        self.assertIn(("GET", f"/repos/{OWNER}/toolkit/rulesets?per_page=100", None), run.gh.calls)
        self.assertIn("toolkit: deleted ruleset 'default'", run.lines)
        self.assertIn("toolkit: kept ruleset 'mirror tags'", run.lines)

    def test_given_a_repository_when_prepared_then_issues_are_off_and_the_description_points_at_forgejo(self):
        run = self.run_prepare([repository("toolkit")])
        self.assertEqual(run.code, 0, run.err)
        patches = [c for c in run.gh.calls if c[0] == "PATCH"]
        self.assertEqual(patches, [("PATCH", f"/repos/{OWNER}/toolkit",
                                    {"has_issues": False, "description": description("toolkit")})])
        self.assertEqual(description("toolkit"),
                         "Mirror of https://git.example.com/artur-rios/toolkit — contribute there")
        # Automated security fixes were on: one DELETE switches them off.
        self.assertEqual([c for c in run.gh.calls if c[0] == "DELETE"],
                         [("DELETE", f"/repos/{OWNER}/toolkit/automated-security-fixes", None)])
        self.assertFalse(run.gh.state["toolkit"]["fixes"])

    def test_given_another_forge_url_when_prepared_then_the_description_uses_it_not_a_fixed_host(self):
        catalog = {**CATALOG, "forge": {"type": "forgejo", "url": "https://forge.other.org/"}}
        run = self.run_prepare([repository("toolkit")], catalog=catalog)
        self.assertEqual(run.gh.state["toolkit"]["description"], description("toolkit", "https://forge.other.org"))

    def test_given_prepared_twice_when_run_then_the_second_run_changes_nothing(self):
        repos = [repository("a"), repository("b")]
        forgejo = FakeForgejo(existing={r["name"]: forgejo_repository(r["name"]) for r in repos},
                              files={("a", DEPENDABOT): "sha-a"})
        gh = FakeGitHub(repos, rulesets={"b": [{"id": 1, "name": "old"}, {"id": 2, "name": "mirror tags"}]})
        first = self.run_prepare(repos, forgejo=forgejo, gh=gh)
        self.assertEqual(first.code, 0, first.err)
        writes = len(forgejo.writes()) + len(gh.writes())
        self.assertGreater(writes, 0)
        second = self.run_prepare(repos, forgejo=forgejo, gh=gh)
        self.assertEqual(second.code, 0, second.err)
        self.assertEqual(len(forgejo.writes()) + len(gh.writes()), writes)  # not one more write
        self.assertIn("a: no dependabot file", second.lines)
        self.assertIn("a: already prepared on GitHub", second.lines)

    def test_given_security_fixes_already_off_or_unreadable_when_prepared_then_no_delete_is_sent(self):
        run = self.run_prepare([repository("off"), repository("gone")],
                               settings={"off": {"fixes": False}},
                               fail={"gone": {("GET", "/automated-security-fixes"): 404}})
        self.assertEqual(run.code, 0, run.err)
        self.assertFalse([c for c in run.gh.calls if c[0] == "DELETE"])

    def test_given_security_fixes_that_answer_404_to_the_delete_when_prepared_then_it_is_fine(self):
        run = self.run_prepare([repository("a")], fail={"a": {("DELETE", "/automated-security-fixes"): 404}})
        self.assertEqual(run.code, 0, run.err)

    def test_given_a_repository_forgejo_does_not_have_when_prepared_then_it_fails_untouched_and_the_rest_go_on(self):
        repos = [repository("a"), repository("missing"), repository("c")]
        forgejo = FakeForgejo(existing={"a": forgejo_repository("a"), "c": forgejo_repository("c")})
        run = self.run_prepare(repos, forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("  missing: not on Forgejo: run import first", run.err.splitlines())
        self.assertFalse([c for c in run.gh.calls if "/missing" in c[1]])
        self.assertFalse(run.gh.state["a"]["has_issues"])
        self.assertFalse(run.gh.state["c"]["has_issues"])

    def test_given_a_forgejo_lookup_that_fails_when_prepared_then_that_repository_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={"b": forgejo_repository("b")}, lookup={"a": 500})
        run = self.run_prepare([repository("a"), repository("b")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("a: could not look for it on Forgejo", run.err)
        self.assertFalse(run.gh.state["b"]["has_issues"])

    def test_given_forgejo_refusing_the_file_when_prepared_then_that_repository_fails_before_github_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={"a": forgejo_repository("a"), "b": forgejo_repository("b")},
                              contents_status={"a": 500})
        run = self.run_prepare([repository("a"), repository("b")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertEqual([line for line in run.err.splitlines() if line.startswith("  a:")],
                         [f"  a: could not prepare it: GET /repos/{OWNER}/a/contents/{DEPENDABOT}?ref=develop: "
                          "500 contents failed"])
        # Not half prepared: a's GitHub side waits for the re-run.
        self.assertFalse([c for c in run.gh.calls if c[1].startswith(f"/repos/{OWNER}/a") and c[0] != "GET"])
        self.assertFalse(run.gh.state["b"]["has_issues"])

    def test_given_a_github_request_that_fails_when_prepared_then_that_repository_fails_and_the_rest_go_on(self):
        run = self.run_prepare([repository("a"), repository("b"), repository("c")],
                               fail={"b": {("GET", "/rulesets"): 403}})
        self.assertEqual(run.code, 1)
        self.assertEqual([line for line in run.err.splitlines() if line.startswith("  b:")],
                         [f"  b: could not prepare it: GET /repos/{OWNER}/b/rulesets?per_page=100: 403 refused"])
        self.assertFalse(run.gh.state["a"]["has_issues"])
        self.assertFalse(run.gh.state["c"]["has_issues"])

    def test_given_an_archived_github_repository_when_prepared_then_github_is_left_alone_but_dependabot_is_still_deleted(self):
        # Archived copies are read-only on GitHub: no ruleset, settings or fixes request at all (not even a
        # read), and it is a note, not a failure. Forgejo's side is still done: the file goes there.
        forgejo = FakeForgejo(existing={"old": forgejo_repository("old")}, files={("old", DEPENDABOT): "sha-o"})
        run = self.run_prepare([repository("old", archived=True)], forgejo=forgejo)
        self.assertEqual((run.code, run.err), (0, ""), run.out)
        self.assertIn("old: archived on GitHub: left as it is", run.lines)
        self.assertIn("old: deleted .github/dependabot.yml on develop", run.lines)
        self.assertEqual(forgejo.files, {})
        self.assertFalse([c for c in run.gh.calls if c[1].startswith(f"/repos/{OWNER}/old")])

    def test_given_an_archived_repository_and_a_dry_run_when_prepared_then_it_is_the_same_note_and_no_write(self):
        forgejo = FakeForgejo(existing={"old": forgejo_repository("old")}, files={("old", DEPENDABOT): "sha-o"})
        run = self.run_prepare([repository("old", archived=True)], ["--dry-run"] + self.ARGS, forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("old: archived on GitHub: left as it is", run.lines)
        self.assertIn("old: would delete .github/dependabot.yml on develop", run.lines)
        self.assertEqual(forgejo.writes(), [])

    def test_given_an_archived_repository_not_on_forgejo_when_prepared_then_it_is_still_a_failure(self):
        run = self.run_prepare([repository("old", archived=True)], forgejo=FakeForgejo())
        self.assertEqual(run.code, 1)
        self.assertIn("old: not on Forgejo: run import first", run.err)

    def test_given_a_private_repository_whose_ruleset_list_answers_403_when_prepared_then_it_is_a_note_and_the_rest_goes_on(self):
        # Rulesets on a private repository need a paid plan: nothing there to delete.
        run = self.run_prepare([repository("secret", private=True), repository("next")],
                               fail={"secret": {("GET", "/rulesets"): 403}})
        self.assertEqual((run.code, run.err), (0, ""), run.out)
        self.assertIn("secret: rulesets not available here: nothing to delete", run.lines)
        self.assertFalse(run.gh.state["secret"]["has_issues"])  # issues, description and fixes were still done
        self.assertEqual(run.gh.state["secret"]["description"], description("secret"))
        self.assertFalse(run.gh.state["secret"]["fixes"])
        self.assertFalse(run.gh.state["next"]["has_issues"])

    def test_given_a_private_repository_whose_ruleset_list_answers_another_error_when_prepared_then_it_fails(self):
        run = self.run_prepare([repository("secret", private=True)], fail={"secret": {("GET", "/rulesets"): 500}})
        self.assertEqual(run.code, 1)
        self.assertIn("secret: could not prepare it", run.err)

    def test_given_an_error_that_echoes_the_github_token_when_prepared_then_it_is_hidden(self):
        class Echoing(FakeGitHub):
            def request(self, method, path, body=None):
                if method == "PATCH":
                    raise migrate.rules.ApiError(method, path, 422, "bad credentials gh-tok-456")
                return super().request(method, path, body)

        run = self.run_prepare([repository("a")], gh=Echoing([repository("a")]))
        self.assertEqual(run.code, 1)
        self.assertNotIn("gh-tok-456", run.out + run.err)
        self.assertIn("422 bad credentials ***", run.err)

    def test_given_a_dry_run_when_prepared_then_nothing_is_written_and_the_plan_is_printed(self):
        forgejo = FakeForgejo(existing={"a": forgejo_repository("a")}, files={("a", DEPENDABOT): "sha-a"})
        gh = FakeGitHub([repository("a")], rulesets={"a": [{"id": 1, "name": "old"}, {"id": 2, "name": "mirror tags"}]})
        run = self.run_prepare([repository("a")], ["--dry-run"] + self.ARGS, forgejo=forgejo, gh=gh)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual((forgejo.writes(), gh.writes()), ([], []))
        self.assertIn("a: would delete .github/dependabot.yml on develop", run.lines)
        self.assertIn("a: would delete ruleset 'old'", run.lines)
        self.assertIn("a: would set issues off and the description", run.lines)
        self.assertIn("a: would switch off automated security fixes", run.lines)
        self.assertEqual(forgejo.files, {("a", DEPENDABOT): "sha-a"})

    def test_given_a_dry_run_and_a_repository_not_on_forgejo_when_prepared_then_it_is_still_a_failure(self):
        run = self.run_prepare([repository("missing")], ["--dry-run"] + self.ARGS, forgejo=FakeForgejo())
        self.assertEqual(run.code, 1)
        self.assertIn("missing: not on Forgejo: run import first", run.err)

    def test_given_only_when_prepared_then_just_those_are_prepared(self):
        run = self.run_prepare([repository("a"), repository("b"), repository("c")], self.ARGS + ["--only", "b"])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(set(run.gh.state), {"b"})
        self.assertFalse([c for c in run.forgejo.calls if f"/{OWNER}/a" in c[1] or f"/{OWNER}/c" in c[1]])

    def test_given_no_forgejo_token_when_prepared_then_it_exits_2_before_anything_is_printed(self):
        run = self.run_prepare([repository("a")], env={})
        self.assertEqual(run.code, 2)
        self.assertIn("FORGE_TOKEN", run.err)
        self.assertEqual(run.out, "")

    def test_given_the_tokens_when_prepared_then_they_are_in_no_line_and_no_path(self):
        run = self.run_prepare([repository("a")])
        self.assertNotIn("gh-tok-456", run.out + run.err)
        self.assertNotIn("fg-tok-123", run.out + run.err)
        self.assertFalse([c for c in run.gh.calls + run.forgejo.calls if "tok-" in c[1]])


class ReportTests(unittest.TestCase):
    def context(self, token):
        ctx = migrate.Context(catalog=CATALOG, dry_run=False, only=[], github_owner=OWNER)
        ctx.github_token = token  # a cached_property: assigning it stands in for the lookup
        return ctx

    def test_given_a_token_in_a_failure_when_reported_then_it_is_hidden_in_the_text_that_is_printed_and_returned(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            shown = migrate._report(self.context("gh-tok-456"), "a: failed: echoed gh-tok-456")
        self.assertEqual(shown, "a: failed: echoed ***")
        self.assertEqual(out.getvalue(), "a: failed: echoed ***\n")

    def test_given_an_empty_or_missing_token_when_reported_then_the_text_is_left_as_it_is(self):
        # str.replace("", "***") would put *** between every character; None would raise.
        for token in ("", None):
            with self.subTest(token=token), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(migrate._report(self.context(token), "a: failed: no way"), "a: failed: no way")


class FrameTests(unittest.TestCase):
    def test_given_no_github_token_when_run_then_it_exits_2_with_how_to_give_one(self):
        run = Run(["--github-owner", OWNER, "inventory"], token="")
        self.assertEqual(run.code, 2)
        self.assertIn("GH_TOKEN", run.err)
        self.assertIn("gh auth login", run.err)
        self.assertEqual((run.made, run.out), ([], ""))

    def test_given_the_github_token_when_run_then_it_goes_only_to_github_in_the_bearer_scheme(self):
        run = Run(["--github-owner", OWNER, "inventory"])
        self.assertEqual(run.made, [("https://api.github.com", "gh-tok-456", "Bearer")])
        self.assertNotIn("gh-tok-456", run.out + run.err)

    def test_given_a_mirror_in_the_catalog_when_run_then_its_github_owner_is_the_owner_inventoried(self):
        run = Run(["inventory"], FakeGitHub(sixty(), login=MIRROR), catalog=CATALOG_MIRROR)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn(("GET", f"/users/{MIRROR}", None), run.gh.calls)
        pulls = [path for _, path, _ in run.gh.calls if "/pulls" in path]
        self.assertEqual(len(pulls), 60)
        self.assertTrue(all(path.startswith(f"/repos/{MIRROR}/") for path in pulls), pulls)

    def test_given_no_mirror_and_no_owner_option_when_run_then_it_exits_2_saying_what_to_give(self):
        run = Run(["inventory"])
        self.assertEqual(run.code, 2)
        self.assertIn("--github-owner", run.err)
        self.assertEqual(run.gh.calls, [])

    def test_given_an_owner_option_that_differs_from_the_mirror_when_run_then_it_exits_2(self):
        run = Run(["--github-owner", "somebody", "inventory"], catalog=CATALOG_MIRROR)
        self.assertEqual(run.code, 2)
        self.assertIn(MIRROR, run.err)

    def test_given_no_command_or_an_unknown_one_when_run_then_it_exits_2(self):
        for args in ([], ["frobnicate"]):
            with self.subTest(args=args), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(migrate.main(args), 2)

    def test_given_a_request_that_fails_when_run_then_the_error_is_the_failure_and_it_exits_1(self):
        class Refusing:
            def request(self, method, path, body=None):
                raise migrate.rules.ApiError(method, path, 403, "API rate limit exceeded")

        run = Run(["--github-owner", OWNER, "inventory"], Refusing())
        self.assertEqual(run.code, 1)
        self.assertIn("API rate limit exceeded", run.err)

    def test_given_a_command_with_failures_when_run_then_each_is_on_its_own_line_and_the_exit_is_1(self):
        def cmd_two_failures(ctx, args):
            print("working")
            return ["first went wrong", "second went wrong"]

        with mock.patch.dict(migrate.COMMANDS, {"two": cmd_two_failures}):
            run = Run(["--github-owner", OWNER, "two"])
        self.assertEqual(run.code, 1)
        self.assertEqual(run.out, "working\n")
        self.assertEqual(run.err.splitlines()[-2:], ["  first went wrong", "  second went wrong"])


class ForgejoConnectionTests(unittest.TestCase):
    """The Forgejo client the later commands share: made when first used, never by inventory."""

    def context(self, catalog=CATALOG_MIRROR):
        return migrate.Context(catalog=catalog, dry_run=False, only=[], github_owner=MIRROR)

    def test_given_a_forgejo_catalog_and_a_token_when_asked_then_the_api_uses_the_token_scheme(self):
        made = []
        with mock.patch.dict(os.environ, {"FORGE_TOKEN": "fg-tok-123"}, clear=True), \
                mock.patch.object(migrate.rules, "Api", lambda *a, **k: made.append(a) or "api"):
            ctx = self.context()
            self.assertEqual(ctx.forgejo, "api")
        self.assertEqual(made, [("https://git.example.com/api/v1", "fg-tok-123", "token")])
        self.assertEqual(ctx.forgejo_owner, OWNER)

    def test_given_no_forge_token_when_asked_then_it_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(migrate.UsageError) as raised:
            self.context().forgejo
        self.assertIn("FORGE_TOKEN", str(raised.exception))

    def test_given_a_catalog_that_is_not_on_forgejo_when_asked_then_it_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"FORGE_TOKEN": "fg-tok-123"}, clear=True), \
                self.assertRaises(migrate.UsageError) as raised:
            self.context(CATALOG).forgejo
        self.assertIn("forge", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
