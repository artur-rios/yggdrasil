"""Tests for scripts/migrate.py (the move from GitHub to Forgejo).   python3 -m unittest discover -s scripts"""

import io
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
            "default_branch": "develop", "has_wiki": False, "open_issues_count": 0, **fields}


class FakeGitHub:
    """Stands in for rules.Api: keeps an owner's repositories (and their open pull requests) and answers
    like GitHub does, in pages of at most `page_size` however many are asked for, so a caller that trusts
    its own per_page loses the rest."""

    def __init__(self, repos, pulls=None, kind="User", login=OWNER, page_size=30):
        self.repos, self.pulls = repos, pulls or {}
        self.kind, self.login, self.page_size = kind, login, page_size
        self.calls = []

    def _page(self, items, query):
        page = int(query.get("page", "1"))
        size = min(int(query.get("per_page", "30")), self.page_size)
        return items[(page - 1) * size:page * size]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        assert method == "GET", f"inventory only reads: {method} {path}"
        route, _, raw = path.partition("?")
        query = dict(part.split("=") for part in raw.split("&") if part)
        segments = route.split("/")  # ["", "repos", owner, repo, "pulls"]
        if route.startswith("/users/"):
            return {"login": segments[2], "type": self.kind}
        if route == "/user":
            return {"login": self.login}
        if route in ("/user/repos", f"/orgs/{OWNER}/repos"):
            return self._page(self.repos, query)
        if segments[:2] == ["", "repos"] and segments[4] == "pulls":
            assert query.get("state") == "open", path
            return self._page([{"number": n} for n in range(self.pulls.get(segments[3], 0))], query)
        raise AssertionError(f"unexpected request: {method} {path}")


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
                           "default_branch": "main", "has_wiki": True, "open_pulls": 3}])

    def test_given_more_open_pull_requests_than_a_page_when_counted_then_all_are_counted(self):
        gh = FakeGitHub([repository("busy")], pulls={"busy": 250}, page_size=100)
        self.assertEqual(migrate.github_repositories(gh, OWNER)[0]["open_pulls"], 250)

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

    def test_given_only_names_when_listed_then_pull_requests_are_counted_for_those_alone(self):
        gh = FakeGitHub(sixty(), pulls={"repo-03": 1})
        listed = migrate.github_repositories(gh, OWNER, only=["repo-03"])
        self.assertEqual([r["name"] for r in listed], ["repo-03"])
        self.assertEqual(len([c for c in gh.calls if "/pulls" in c[1]]), 2)  # its page and the empty one


class Run:
    """migrate.main() with the catalog, the environment and the GitHub API replaced, and what it printed."""

    def __init__(self, args, gh=None, catalog=CATALOG, env=None, token="gh-tok-456"):
        self.gh = gh or FakeGitHub(sixty())
        self.made = []

        def make_api(base, token_, scheme, headers=None):
            self.made.append((base, token_, scheme))
            return self.gh

        out, err = io.StringIO(), io.StringIO()
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

    def test_given_dry_run_when_inventoried_then_it_is_the_same_read_only_run(self):
        run = Run(["--dry-run"] + self.ARGS)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(len([line for line in run.lines if "\t" in line]), 60)


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
