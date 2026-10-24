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
                 commits_status=None, rulesets=None, settings=None, fail=None, refs=None, refs_fail=()):
        self.repos, self.pulls, self.pulls_fail = repos, pulls or {}, set(pulls_fail)
        # What verify reads: `refs` (name -> {full ref name: sha}, served by git/matching-refs by prefix,
        # in pages) and `refs_fail` (names whose refs answer 502).
        self.refs, self.refs_fail = refs or {}, set(refs_fail)
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

    def _matching_refs(self, method, path, segments, query):
        """`GET /repos/<o>/<r>/git/matching-refs/<prefix>`: the refs that start with `refs/<prefix>`, in pages
        (an empty repository answers 409, as GitHub does)."""
        name = segments[3]
        if name in self.refs_fail:
            raise migrate.rules.ApiError(method, path, 502, "Bad Gateway")
        if name in self.empty:
            raise migrate.rules.ApiError(method, path, 409, "Git Repository is empty.")
        prefix = "refs/" + "/".join(segments[6:])
        found = [{"ref": ref, "object": {"sha": sha, "type": "commit"}}
                 for ref, sha in sorted(self.refs.get(name, {}).items()) if ref.startswith(prefix)]
        return self._page(found, query)

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
        if segments[:2] == ["", "repos"] and segments[4:6] == ["git", "matching-refs"]:
            return self._matching_refs(method, path, segments, query)
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
                 contents_status=None, mirrors=None, mirror_fail=None, users=None, collaborators=None,
                 collaborator_fail=None, user_fail=None, refs=None, hooks=None, verify_fail=None, ref_page=30,
                 effective_read=False, commits=None):
        self.existing, self.lookup, self.migrate_status = existing or {}, lookup or {}, migrate_status or {}
        # What verify reads. `refs`: repository -> {full ref name: sha} (every namespace, `refs/pull/*`
        # included), served by git/refs/<prefix> in pages of `ref_page` (None: all at once on every page, as
        # a server that ignores `page` does); `hooks`: repository -> its webhooks; `verify_fail`: (repository,
        # "git/refs" | "hooks" | "collaborators" | "push_mirrors") -> status to answer with an error;
        # `effective_read`: the permission endpoint answers `read` for anyone who is not a collaborator
        # (what Forgejo does on a public repository).
        self.refs, self.hooks, self.verify_fail = refs or {}, hooks or {}, verify_fail or {}
        self.ref_page, self.effective_read = ref_page, effective_read
        # What mirrors' guard asks of a GitHub sha, `GET .../git/commits/<sha>`: a repository knows the commits
        # its refs point at and those `commits` (repository -> shas) adds, older ones of its history; any
        # other is 404, as Forgejo answers a commit it does not have. verify_fail's "git/commits" fails it.
        self.commits = {name: set(shas) for name, shas in (commits or {}).items()}
        self.migrate_message = migrate_message
        # What mirrors and collaborators work on. `mirrors`: repository -> the push mirrors it has (as
        # Forgejo lists them); `mirror_fail`: (method, last path segment) -> status to answer with an error,
        # for every repository, or (name, method, segment) for one. `users`: the accounts that exist (None:
        # every one does); `user_fail`: user -> status its lookup answers instead. `collaborators`:
        # (repository, user) -> permission; `collaborator_fail`: (repository, method) -> status.
        self.mirrors = {name: list(found) for name, found in (mirrors or {}).items()}
        self.mirror_fail, self.users, self.user_fail = mirror_fail or {}, users, user_fail or {}
        self.collaborators, self.collaborator_fail = dict(collaborators or {}), collaborator_fail or {}
        self.synced = []
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
        if "/git/commits/" in path:
            return self._commit(method, path)
        for route in (self._verify_request, self._mirror_request, self._collaborator_request):
            answer = route(method, path, body)
            if answer is not NotImplemented:
                return answer
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

    def _commit(self, method, path):
        """`GET /repos/<owner>/<name>/git/commits/<sha>`: the commit when the repository has it, else 404."""
        assert method == "GET", path
        route = path.partition("?")[0]
        name, _, sha = route[len(f"/repos/{OWNER}/"):].partition("/git/commits/")
        if (name, "git/commits") in self.verify_fail:
            raise migrate.rules.ApiError(method, path, self.verify_fail[(name, "git/commits")], "refused")
        if sha in set(self.refs.get(name, {}).values()) | self.commits.get(name, set()):
            return {"sha": sha}
        raise migrate.rules.ApiError(method, path, 404, "commit does not exist")

    def _verify_request(self, method, path, body):
        """What verify reads: `GET /repos/<owner>/<name>/git/refs[/<prefix>]` (Forgejo answers 404 for a prefix
        nothing matches), `.../hooks` and the collaborators list `.../collaborators` (logins only)."""
        route, _, raw = path.partition("?")
        if not route.startswith(f"/repos/{OWNER}/") or method != "GET":
            return NotImplemented
        name, _, rest = route[len(f"/repos/{OWNER}/"):].partition("/")
        kind = next((k for k in ("git/refs", "hooks", "collaborators", "push_mirrors")
                     if rest == k or (k == "git/refs" and rest.startswith("git/refs/"))), None)
        if kind is None:
            return NotImplemented
        if (name, kind) in self.verify_fail:
            raise migrate.rules.ApiError(method, path, self.verify_fail[(name, kind)], "refused")
        query = dict(part.split("=") for part in raw.split("&") if part)
        if kind == "push_mirrors":
            return NotImplemented
        if kind == "hooks":
            return self._listed(self.hooks.get(name, []), query)
        if kind == "collaborators":
            return self._listed([{"login": user} for repo, user in sorted(self.collaborators) if repo == name], query)
        prefix = "refs/" + rest[len("git/refs/"):] if rest != "git/refs" else "refs/"
        found = [{"ref": ref, "object": {"sha": sha, "type": "commit"}}
                 for ref, sha in sorted(self.refs.get(name, {}).items()) if ref.startswith(prefix)]
        if not found and rest != "git/refs":
            raise migrate.rules.ApiError(method, path, 404, "The target couldn't be found.")
        return self._listed(found, query, self.ref_page)

    def _listed(self, items, query, size=30):
        """One page of a Forgejo list (`size` per page; None: every item on every page)."""
        if size is None:
            return list(items)
        page = int(query.get("page", "1"))
        return items[(page - 1) * size:page * size]

    def _mirror_request(self, method, path, body):
        """`GET|POST /repos/<owner>/<name>/push_mirrors` and `POST .../push_mirrors-sync`, as Forgejo answers
        them: the list leaves the credentials out of `remote_address`; the sync answers 200 and runs later."""
        name, _, last = path[len(f"/repos/{OWNER}/"):].partition("/")
        if not path.startswith(f"/repos/{OWNER}/") or last not in ("push_mirrors", "push_mirrors-sync"):
            return NotImplemented
        for key in ((method, last), (name, method, last)):
            if key in self.mirror_fail:
                raise migrate.rules.ApiError(method, path, self.mirror_fail[key], "mirror refused")
        if (method, last) == ("GET", "push_mirrors"):
            return list(self.mirrors.get(name, []))
        if (method, last) == ("POST", "push_mirrors"):
            made = {"remote_name": f"remote_mirror_{len(self.mirrors.get(name, []))}",
                    "remote_address": body["remote_address"], "interval": body["interval"],
                    "sync_on_commit": body["sync_on_commit"], "last_update": None, "last_error": ""}
            self.mirrors.setdefault(name, []).append(made)
            return made
        if (method, last) == ("POST", "push_mirrors-sync"):
            self.synced.append(name)
            return None
        raise AssertionError(f"unexpected request: {method} {path}")

    def _collaborator_request(self, method, path, body):
        """`GET /users/<name>`, and a repository's `GET .../collaborators/<user>/permission` (404 for one who
        is not a collaborator) and `PUT .../collaborators/<user>` (204)."""
        if path.startswith("/users/"):
            user = path.rsplit("/", 1)[1]
            if user in self.user_fail:
                raise migrate.rules.ApiError(method, path, self.user_fail[user], "user lookup failed")
            if self.users is not None and user not in self.users:
                raise migrate.rules.ApiError(method, path, 404, "user does not exist")
            return {"login": user}
        name, _, rest = path[len(f"/repos/{OWNER}/"):].partition("/collaborators/")
        if not path.startswith(f"/repos/{OWNER}/") or not rest:
            return NotImplemented
        user = rest.split("/")[0]
        if (name, method) in self.collaborator_fail:
            raise migrate.rules.ApiError(method, path, self.collaborator_fail[(name, method)], "refused")
        if method == "GET" and rest == f"{user}/permission":
            if (name, user) not in self.collaborators:
                if self.effective_read:
                    return {"permission": "read", "role_name": "read"}
                raise migrate.rules.ApiError(method, path, 404, "not a collaborator")
            return {"permission": self.collaborators[(name, user)], "role_name": self.collaborators[(name, user)]}
        if method == "PUT" and rest == user:
            self.collaborators[(name, user)] = body["permission"]
            return None
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
        # Both scopes are named: collaborators looks the bots up under /users, which needs read:user.
        self.assertIn("scopes write:repository and read:user", run.err)
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


MIRROR_ENV = {**ENV, "MIRROR_GITHUB_USER": "mirror-bot", "MIRROR_GITHUB_TOKEN": "mirror-tok-789"}
MIRROR_CATALOG = {**CATALOG_MIRROR, "repositories": [{"id": "toolkit"}, {"id": "other-lib"}]}


def push_mirror(name, address=None, **fields):
    """A push mirror as Forgejo lists it: the address without credentials."""
    return {"remote_name": "remote_mirror_x", "remote_address": address or f"https://github.com/{MIRROR}/{name}.git",
            "interval": "8h0m0s", "sync_on_commit": True, "last_update": "2026-10-24T10:00:00Z", "last_error": "",
            **fields}


class MirrorsTests(unittest.TestCase):
    ARGS = ["mirrors"]

    def run_mirrors(self, repos, args=None, forgejo=None, env=None, catalog=MIRROR_CATALOG, **github):
        """Every repository is on Forgejo unless `forgejo` says otherwise. The GitHub owner is the catalog's
        mirror.github (the token of that user, as `login`)."""
        forgejo = forgejo or FakeForgejo(existing={r["name"]: forgejo_repository(r["name"]) for r in repos})
        gh = FakeGitHub(repos, login=MIRROR, **github)
        return Run(args or self.ARGS, gh, catalog=catalog, env=MIRROR_ENV if env is None else env, forgejo=forgejo)

    def test_given_no_push_mirror_when_mirrored_then_one_syncing_on_commit_every_8h_is_made(self):
        run = self.run_mirrors([repository("toolkit")])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.forgejo.writes(), [
            ("POST", f"/repos/{OWNER}/toolkit/push_mirrors",
             {"remote_address": f"https://github.com/{MIRROR}/toolkit.git", "remote_username": "mirror-bot",
              "remote_password": "mirror-tok-789", "interval": "8h0m0s", "sync_on_commit": True}),
            ("POST", f"/repos/{OWNER}/toolkit/push_mirrors-sync", None)])
        # The first sync is asked for once, so the existing branches and tags go to GitHub now.
        self.assertEqual(run.forgejo.synced, ["toolkit"])
        self.assertEqual(run.lines, ["toolkit: push mirror created", "toolkit: sync requested"])

    def test_given_the_mirror_already_there_when_mirrored_then_it_is_kept(self):
        # Forgejo may list the address with or without `.git`, and never with credentials; neither matters.
        for address in (f"https://github.com/{MIRROR}/toolkit.git", f"https://github.com/{MIRROR}/toolkit",
                        f"https://github.com/{MIRROR}/toolkit.git/", f"HTTPS://GitHub.com/{MIRROR}/Toolkit.git",
                        f"https://mirror-bot:mirror-tok-789@github.com/{MIRROR}/toolkit.git"):
            with self.subTest(address=address):
                forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                                      mirrors={"toolkit": [push_mirror("toolkit", address)]})
                run = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
                self.assertEqual(run.code, 0, run.err)
                self.assertEqual(run.forgejo.writes(), [])
                self.assertEqual(run.lines, ["toolkit: push mirror already there"])

    def test_given_a_mirror_never_synced_when_mirrored_then_the_sync_is_asked_for_again(self):
        # A run that stopped between the creation and the sync leaves a mirror that never pushed; the
        # re-run finishes it (Forgejo's `last_update` is empty until the first push).
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              mirrors={"toolkit": [push_mirror("toolkit", last_update=None)]})
        run = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.forgejo.writes(), [("POST", f"/repos/{OWNER}/toolkit/push_mirrors-sync", None)])
        self.assertEqual(run.lines, ["toolkit: push mirror already there, never synced: sync requested"])

    def test_given_a_mirror_to_another_address_when_mirrored_then_it_is_reported_not_replaced(self):
        other = "https://github.com/someone-else/toolkit.git"
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("toolkit", "other-lib")},
                              mirrors={"toolkit": [push_mirror("toolkit", other)]})
        run = self.run_mirrors([repository("toolkit"), repository("other-lib")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn(f"  toolkit: has a push mirror to {other}, not to https://github.com/{MIRROR}/toolkit.git: "
                      "not replaced; remove it on Forgejo if it is wrong", run.err.splitlines())
        # Nothing was written to it (no second mirror, no delete); the next repository went on.
        self.assertEqual([c[1] for c in run.forgejo.writes()],
                         [f"/repos/{OWNER}/other-lib/push_mirrors", f"/repos/{OWNER}/other-lib/push_mirrors-sync"])
        self.assertEqual(forgejo.mirrors["toolkit"], [push_mirror("toolkit", other)])

    def test_given_a_credential_in_the_other_address_when_reported_then_it_is_not_shown(self):
        # Forgejo never lists one, but a failure line must not depend on that.
        other = "https://someone:secret-pass@github.com/someone-else/toolkit.git"
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              mirrors={"toolkit": [push_mirror("toolkit", other)]})
        run = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertNotIn("secret-pass", run.out + run.err)
        self.assertIn("https://github.com/someone-else/toolkit.git", run.err)

    def test_given_two_mirrors_one_of_them_right_when_mirrored_then_it_is_kept_and_nothing_is_written(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              mirrors={"toolkit": [push_mirror("toolkit", "https://example.org/x.git"),
                                                   push_mirror("toolkit")]})
        run = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
        self.assertEqual((run.code, run.forgejo.writes()), (0, []))

    def test_given_an_archived_github_repository_when_mirrored_then_it_gets_no_mirror_and_that_is_no_failure(self):
        run = self.run_mirrors([repository("toolkit", archived=True)])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.lines, ["toolkit: archived on GitHub: no mirror"])
        self.assertEqual(run.forgejo.writes(), [])

    def test_given_a_repository_forgejo_does_not_have_when_mirrored_then_it_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={"other-lib": forgejo_repository("other-lib")})
        run = self.run_mirrors([repository("toolkit"), repository("other-lib")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("toolkit: not on Forgejo: run import first", run.err)
        self.assertIn("other-lib: push mirror created", run.lines)
        self.assertEqual(forgejo.synced, ["other-lib"])

    def test_given_an_archived_repository_not_on_forgejo_when_mirrored_then_it_is_still_a_failure(self):
        run = self.run_mirrors([repository("toolkit", archived=True)], forgejo=FakeForgejo())
        self.assertEqual(run.code, 1)
        self.assertIn("toolkit: not on Forgejo: run import first", run.err)

    def test_given_a_creation_answering_500_when_mirrored_then_that_repository_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("toolkit", "other-lib")},
                              mirror_fail={("toolkit", "POST", "push_mirrors"): 500})
        run = self.run_mirrors([repository("toolkit"), repository("other-lib")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("toolkit: could not make the push mirror: POST", run.err)
        self.assertIn("other-lib: sync requested", run.lines)
        self.assertEqual(forgejo.synced, ["other-lib"])  # no sync for the one whose creation failed

    def test_given_a_list_that_fails_when_mirrored_then_nothing_is_written_for_that_repository(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("toolkit", "other-lib")},
                              mirror_fail={("toolkit", "GET", "push_mirrors"): 502})
        run = self.run_mirrors([repository("toolkit"), repository("other-lib")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("toolkit: could not list its push mirrors", run.err)
        self.assertEqual([c[1] for c in forgejo.writes()],
                         [f"/repos/{OWNER}/other-lib/push_mirrors", f"/repos/{OWNER}/other-lib/push_mirrors-sync"])

    def test_given_a_sync_that_fails_after_the_mirror_was_made_when_mirrored_then_the_run_again_asks_for_it(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              mirror_fail={("POST", "push_mirrors-sync"): 500})
        first = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
        self.assertEqual(first.code, 1)
        self.assertIn("toolkit: push mirror created", first.lines)
        self.assertIn("toolkit: could not sync it", first.err)
        forgejo.mirror_fail.clear()
        second = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
        self.assertEqual(second.code, 0, second.err)
        self.assertEqual(forgejo.synced, ["toolkit"])
        self.assertEqual(len(forgejo.mirrors["toolkit"]), 1)

    def test_given_the_mirrors_made_when_mirrored_again_then_the_second_run_writes_nothing(self):
        repos = [repository("toolkit")]
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")})
        self.run_mirrors(repos, forgejo=forgejo)
        forgejo.mirrors["toolkit"][0]["last_update"] = "2026-10-24T10:00:00Z"  # the sync has run
        forgejo.calls.clear()
        again = self.run_mirrors(repos, forgejo=forgejo)
        self.assertEqual((again.code, forgejo.writes()), (0, []))

    def test_given_a_dry_run_when_mirrored_then_nothing_is_written_and_the_password_is_masked(self):
        run = self.run_mirrors([repository("toolkit")], args=["--dry-run", "mirrors"])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.forgejo.writes(), [])
        planned = json.loads(run.lines[0].split("would create a push mirror ", 1)[1])
        self.assertEqual(planned, {"remote_address": f"https://github.com/{MIRROR}/toolkit.git",
                                   "remote_username": "mirror-bot", "remote_password": "***",
                                   "interval": "8h0m0s", "sync_on_commit": True})
        self.assertEqual(run.lines[1], "toolkit: would request the sync")
        self.assertNotIn("mirror-tok-789", run.out + run.err)

    def test_given_the_mirror_token_when_mirrored_then_it_goes_only_in_the_request_body(self):
        run = self.run_mirrors([repository("toolkit")])
        self.assertNotIn("mirror-tok-789", run.out + run.err)
        self.assertFalse([c for c in run.gh.calls + run.forgejo.calls if "tok-" in c[1]])
        self.assertEqual([c for c in run.forgejo.calls if "mirror-tok-789" in json.dumps(c[2])],
                         [c for c in run.forgejo.calls if c[1].endswith("/push_mirrors") and c[0] == "POST"])
        self.assertNotIn("mirror-tok-789", json.dumps(run.gh.calls))

    def test_given_an_error_that_echoes_the_mirror_token_when_mirrored_then_it_is_hidden(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")})
        original = forgejo._mirror_request

        def echoing(method, path, body):
            if (method, path.rsplit("/", 1)[1]) == ("POST", "push_mirrors"):
                raise migrate.rules.ApiError(method, path, 500, f"bad password mirror-tok-789 for {body}")
            return original(method, path, body)

        forgejo._mirror_request = echoing
        run = self.run_mirrors([repository("toolkit")], forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertNotIn("mirror-tok-789", run.out + run.err)
        self.assertIn("***", run.err)

    def test_given_no_mirror_user_or_token_when_mirrored_then_it_exits_2_naming_it_before_anything_is_printed(self):
        for missing in ("MIRROR_GITHUB_USER", "MIRROR_GITHUB_TOKEN"):
            with self.subTest(missing=missing):
                run = self.run_mirrors([repository("toolkit")],
                                       env={k: v for k, v in MIRROR_ENV.items() if k != missing})
                self.assertEqual(run.code, 2)
                self.assertIn(missing, run.err)
                self.assertEqual((run.out, run.forgejo.calls, run.gh.calls), ("", [], []))

    def test_given_an_empty_mirror_token_when_mirrored_then_it_exits_2(self):
        run = self.run_mirrors([repository("toolkit")], env={**MIRROR_ENV, "MIRROR_GITHUB_TOKEN": "  "})
        self.assertEqual(run.code, 2)
        self.assertIn("MIRROR_GITHUB_TOKEN", run.err)

    def test_given_the_mirror_variables_on_another_command_when_run_then_they_are_not_required(self):
        run = Run(["--github-owner", OWNER, "prepare-github"], FakeGitHub([repository("a")]),
                  catalog=FORGEJO_CATALOG, env=ENV, forgejo=FakeForgejo(existing={"a": forgejo_repository("a")}))
        self.assertEqual(run.code, 0, run.err)

    def test_given_no_forgejo_token_when_mirrored_then_it_exits_2_before_anything_is_printed(self):
        run = self.run_mirrors([repository("toolkit")],
                               env={k: v for k, v in MIRROR_ENV.items() if k != "FORGE_TOKEN"})
        self.assertEqual(run.code, 2)
        self.assertIn("FORGE_TOKEN", run.err)
        self.assertEqual(run.out, "")

    def test_given_only_when_mirrored_then_just_those_are_mirrored(self):
        run = self.run_mirrors([repository("toolkit"), repository("other-lib")],
                               args=["mirrors", "--only", "other-lib"])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.lines, ["other-lib: push mirror created", "other-lib: sync requested"])

    def test_given_an_unknown_name_in_only_when_mirrored_then_it_exits_2_before_writing(self):
        run = self.run_mirrors([repository("toolkit")], args=["mirrors", "--only", "nope"])
        self.assertEqual(run.code, 2)
        self.assertEqual(run.forgejo.writes(), [])

    def test_given_a_repository_outside_the_catalog_when_mirrored_then_it_is_mirrored_too(self):
        # The move covers every repository of the GitHub owner, forks included; the catalog is for the bots.
        run = self.run_mirrors([repository("old-fork", fork=True)])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.forgejo.synced, ["old-fork"])

    # --- The guard: the first --mirror push must take nothing from GitHub -----------------------------------

    def guarded(self, github_refs, forgejo_refs, args=None, commits=None, mirrors=None):
        """mirrors over toolkit (whose refs the test gives, GitHub's and Forgejo's) and other-lib (the same
        develop on both sides, a repository the guard lets through)."""
        same = {"refs/heads/develop": SHA_DEVELOP}
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("toolkit", "other-lib")},
                              refs={"toolkit": forgejo_refs, "other-lib": same}, commits=commits, mirrors=mirrors)
        return self.run_mirrors([repository("toolkit"), repository("other-lib")], args=args, forgejo=forgejo,
                                refs={"toolkit": github_refs, "other-lib": same})

    def made_for(self, run):
        """The repositories a push mirror was made for."""
        return [path.split("/")[3] for method, path, _ in run.forgejo.writes() if path.endswith("/push_mirrors")]

    def test_given_a_branch_or_tag_only_github_has_when_mirrored_then_that_repository_is_refused_naming_it(self):
        # A push to GitHub between the import and the mirrors: --mirror would delete it on the first sync,
        # and verify would then pass, both sides being the same.
        run = self.guarded({"refs/heads/develop": SHA_DEVELOP, "refs/heads/hotfix": SHA_OTHER,
                            "refs/tags/v1.1.0": SHA_OTHER},
                           {"refs/heads/develop": SHA_DEVELOP})
        self.assertEqual(run.code, 1)
        (failure,) = [line for line in run.err.splitlines() if line.startswith("  toolkit: ")]
        self.assertIn("refs/heads/hotfix only on GitHub", failure)
        self.assertIn("refs/tags/v1.1.0 only on GitHub", failure)
        self.assertIn("--allow-prune", failure)
        # Nothing was written for it; the next repository got its mirror.
        self.assertEqual(self.made_for(run), ["other-lib"])
        self.assertEqual(run.forgejo.synced, ["other-lib"])

    def test_given_a_github_branch_at_a_commit_forgejo_lacks_when_mirrored_then_it_is_refused(self):
        # A stale copy -- 4.4's rehearsal import left in place reads "already imported" -- misses the commits
        # made on GitHub since: the mirror push would rewind GitHub's develop and drop them.
        run = self.guarded({"refs/heads/develop": SHA_OTHER}, {"refs/heads/develop": SHA_DEVELOP})
        self.assertEqual(run.code, 1)
        self.assertIn(f"refs/heads/develop at {SHA_OTHER[:12]} on GitHub, a commit Forgejo does not have", run.err)
        self.assertEqual(self.made_for(run), ["other-lib"])
        # The commit was asked of the repository's own Forgejo copy.
        self.assertIn(f"/repos/{OWNER}/toolkit/git/commits/{SHA_OTHER}",
                      [path.partition("?")[0] for _, path, _ in run.forgejo.calls])

    def test_given_forgejo_ahead_of_github_when_mirrored_then_the_mirror_is_made(self):
        # The usual case: prepare-github's Dependabot commit is on Forgejo's develop, on top of GitHub's
        # develop, which Forgejo has; and a branch only Forgejo has is pushed, not lost. Nothing on GitHub goes.
        run = self.guarded({"refs/heads/develop": SHA_MAIN, "refs/tags/v1.0.0": SHA_TAG},
                           {"refs/heads/develop": SHA_DEVELOP, "refs/heads/feature/x": SHA_OTHER,
                            "refs/tags/v1.0.0": SHA_TAG},
                           commits={"toolkit": {SHA_MAIN}})
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(self.made_for(run), ["toolkit", "other-lib"])

    def test_given_a_tag_forgejo_has_at_another_commit_when_mirrored_then_it_is_refused(self):
        # A version names one commit for ever: a tag moved on one side is not the mirror's to settle, even
        # when Forgejo knows the commit GitHub's tag names.
        run = self.guarded({"refs/heads/develop": SHA_DEVELOP, "refs/tags/v1.0.0": SHA_TAG},
                           {"refs/heads/develop": SHA_DEVELOP, "refs/tags/v1.0.0": SHA_OTHER},
                           commits={"toolkit": {SHA_TAG}})
        self.assertEqual(run.code, 1)
        self.assertIn(f"refs/tags/v1.0.0 at {SHA_TAG[:12]} on GitHub, {SHA_OTHER[:12]} on Forgejo", run.err)

    def test_given_allow_prune_when_mirrored_then_what_only_github_has_is_named_and_the_mirror_is_made(self):
        run = self.guarded({"refs/heads/develop": SHA_OTHER, "refs/heads/hotfix": SHA_OTHER},
                           {"refs/heads/develop": SHA_DEVELOP}, args=["mirrors", "--allow-prune"])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(self.made_for(run), ["toolkit", "other-lib"])
        (note,) = [line for line in run.lines if line.startswith("toolkit: --allow-prune")]
        self.assertIn("refs/heads/hotfix only on GitHub", note)
        self.assertIn("refs/heads/develop at", note)

    def test_given_a_mirror_never_synced_and_what_only_github_has_when_mirrored_then_no_sync_is_asked(self):
        # A mirror made by a run that stopped before its sync has pushed nothing yet: the sync asked for would
        # be the first --mirror push, so the guard stands before it too.
        run = self.guarded({"refs/heads/develop": SHA_DEVELOP, "refs/heads/hotfix": SHA_OTHER},
                           {"refs/heads/develop": SHA_DEVELOP},
                           mirrors={"toolkit": [push_mirror("toolkit", last_update=None)]})
        self.assertEqual(run.code, 1)
        self.assertIn("refs/heads/hotfix only on GitHub", run.err)
        self.assertEqual(run.forgejo.synced, ["other-lib"])

    def test_given_the_mirror_already_synced_when_mirrored_then_the_refs_are_not_compared(self):
        # It has pushed: GitHub holds Forgejo's state already, and what differs now is verify's to say.
        run = self.guarded({"refs/heads/hotfix": SHA_OTHER}, {"refs/heads/develop": SHA_DEVELOP},
                           mirrors={"toolkit": [push_mirror("toolkit")]})
        self.assertEqual(run.code, 0, run.err)
        self.assertFalse([c for c in run.gh.calls if "/toolkit/git/" in c[1]])

    def test_given_a_dry_run_and_what_only_github_has_when_mirrored_then_it_is_the_same_refusal(self):
        run = self.guarded({"refs/heads/hotfix": SHA_OTHER, "refs/heads/develop": SHA_DEVELOP},
                           {"refs/heads/develop": SHA_DEVELOP}, args=["--dry-run", "mirrors"])
        self.assertEqual(run.code, 1)
        self.assertIn("refs/heads/hotfix only on GitHub", run.err)
        self.assertEqual(run.forgejo.writes(), [])
        self.assertIn("other-lib: would create a push mirror", run.out)

    def test_given_refs_that_cannot_be_read_when_mirrored_then_that_repository_fails_unmirrored(self):
        for side in ("github", "forgejo", "commit"):
            with self.subTest(side=side):
                forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("toolkit", "other-lib")},
                                      refs={"toolkit": {"refs/heads/develop": SHA_DEVELOP}})
                if side == "forgejo":
                    forgejo.verify_fail[("toolkit", "git/refs")] = 502
                if side == "commit":
                    forgejo.verify_fail[("toolkit", "git/commits")] = 500
                run = self.run_mirrors([repository("toolkit"), repository("other-lib")], forgejo=forgejo,
                                       refs={"toolkit": {"refs/heads/develop": SHA_OTHER}},
                                       refs_fail={"toolkit"} if side == "github" else ())
                self.assertEqual(run.code, 1)
                self.assertIn("toolkit: could not compare its branches and tags with GitHub's", run.err)
                self.assertEqual(self.made_for(run), ["other-lib"])

    def test_given_allow_prune_on_another_command_when_run_then_it_is_unknown_there(self):
        run = Run(["--github-owner", OWNER, "verify", "--allow-prune"], env=ENV)
        self.assertEqual(run.code, 2)
        self.assertIn("--allow-prune", run.err)


COLLABORATOR_USERS = ("jenkins", "jenkins-ci", "renovate", "claude")


class CollaboratorsTests(unittest.TestCase):
    ARGS = ["collaborators"]

    def run_collaborators(self, args=None, forgejo=None, env=None, catalog=CATALOG_MIRROR, gh=None):
        """The catalog's repositories are shop-api (the application) and toolkit; all are on Forgejo unless
        `forgejo` says otherwise. GitHub is never asked."""
        forgejo = forgejo or FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit")})
        return Run(args or self.ARGS, gh or FakeGitHub([]), catalog=catalog, env=ENV if env is None else env,
                   forgejo=forgejo)

    def test_given_a_catalog_repository_when_collaborators_run_then_each_bot_gets_its_permission(self):
        run = self.run_collaborators()
        self.assertEqual(run.code, 0, run.err)
        for repo in ("shop-api", "toolkit"):
            self.assertEqual({user: run.forgejo.collaborators[(repo, user)] for user in COLLABORATOR_USERS},
                             {"jenkins": "write", "jenkins-ci": "read", "renovate": "write", "claude": "write"})
        self.assertEqual([c for c in run.forgejo.writes() if c[1].startswith(f"/repos/{OWNER}/toolkit/")], [
            ("PUT", f"/repos/{OWNER}/toolkit/collaborators/jenkins", {"permission": "write"}),
            ("PUT", f"/repos/{OWNER}/toolkit/collaborators/jenkins-ci", {"permission": "read"}),
            ("PUT", f"/repos/{OWNER}/toolkit/collaborators/renovate", {"permission": "write"}),
            ("PUT", f"/repos/{OWNER}/toolkit/collaborators/claude", {"permission": "write"})])
        self.assertIn("toolkit: jenkins-ci: read granted", run.lines)
        self.assertEqual(run.gh.calls, [])

    def test_given_only_the_catalog_when_collaborators_run_then_other_repositories_are_not_touched(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit", "old-fork")})
        run = self.run_collaborators(forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertFalse([c for c in forgejo.calls if "old-fork" in c[1]])

    def test_given_another_jenkins_user_in_the_catalog_when_collaborators_run_then_that_one_gets_write(self):
        catalog = {**CATALOG_MIRROR, "forge": {**CATALOG_MIRROR["forge"], "jenkinsUser": "ci-jenkins"}}
        run = self.run_collaborators(catalog=catalog)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.forgejo.collaborators[("toolkit", "ci-jenkins")], "write")
        self.assertNotIn(("toolkit", "jenkins"), run.forgejo.collaborators)

    def test_given_a_missing_bot_user_when_collaborators_run_then_it_is_named_and_the_run_exits_1(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit")},
                              users={"jenkins", "jenkins-ci", "claude"})
        run = self.run_collaborators(forgejo=forgejo)
        self.assertEqual(run.code, 1)
        # Named once, however many repositories there are, and the others are still granted.
        self.assertEqual(run.err.count("renovate"), 1, run.err)
        self.assertIn("renovate: not on Forgejo: create it first (docs/setup.md 9.7)", run.err)
        self.assertFalse([c for c in forgejo.calls if "collaborators/renovate" in c[1]])
        self.assertEqual(forgejo.collaborators[("toolkit", "claude")], "write")
        self.assertEqual(forgejo.collaborators[("shop-api", "jenkins-ci")], "read")

    def test_given_a_user_lookup_that_fails_when_collaborators_run_then_that_user_is_skipped_and_named_once(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit")},
                              user_fail={"claude": 500})
        run = self.run_collaborators(forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertEqual(run.err.count("claude: could not look for the user"), 1, run.err)
        self.assertNotIn(("toolkit", "claude"), forgejo.collaborators)
        self.assertEqual(forgejo.collaborators[("toolkit", "renovate")], "write")

    def test_given_a_collaborator_with_the_right_permission_when_collaborators_run_then_it_is_left_unchanged(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              collaborators={("toolkit", "jenkins"): "write", ("toolkit", "jenkins-ci"): "read"})
        run = self.run_collaborators(["collaborators", "--only", "toolkit"], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual([c[1].rsplit("/", 1)[1] for c in forgejo.writes()], ["renovate", "claude"])
        self.assertIn("toolkit: jenkins: already write", run.lines)

    def test_given_a_collaborator_with_another_permission_when_collaborators_run_then_it_is_updated(self):
        # jenkins-ci is read-only on purpose: a bot that was given more is brought back down.
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              collaborators={("toolkit", "jenkins-ci"): "write", ("toolkit", "renovate"): "read"})
        run = self.run_collaborators(["collaborators", "--only", "toolkit"], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual((forgejo.collaborators[("toolkit", "jenkins-ci")],
                          forgejo.collaborators[("toolkit", "renovate")]), ("read", "write"))
        self.assertIn("toolkit: jenkins-ci: changed from write to read", run.lines)
        self.assertIn("toolkit: renovate: changed from read to write", run.lines)

    def test_given_the_collaborators_set_when_run_again_then_the_second_run_writes_nothing(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit")})
        self.run_collaborators(forgejo=forgejo)
        forgejo.calls.clear()
        again = self.run_collaborators(forgejo=forgejo)
        self.assertEqual((again.code, forgejo.writes()), (0, []))

    def test_given_a_dry_run_when_collaborators_run_then_nothing_is_written_and_the_plan_is_printed(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")},
                              collaborators={("toolkit", "renovate"): "read"})
        run = self.run_collaborators(["--dry-run", "collaborators", "--only", "toolkit"], forgejo=forgejo)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(forgejo.writes(), [])
        self.assertIn("toolkit: jenkins: would grant write", run.lines)
        self.assertIn("toolkit: renovate: would change from read to write", run.lines)

    def test_given_a_repository_forgejo_does_not_have_when_collaborators_run_then_it_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")})
        run = self.run_collaborators(forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("shop-api: not on Forgejo: run import first", run.err)
        self.assertEqual(forgejo.collaborators[("toolkit", "claude")], "write")

    def test_given_a_refused_grant_when_collaborators_run_then_that_repository_fails_and_the_rest_go_on(self):
        forgejo = FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit")},
                              collaborator_fail={("shop-api", "PUT"): 403})
        run = self.run_collaborators(forgejo=forgejo)
        self.assertEqual(run.code, 1)
        self.assertIn("shop-api: could not set the collaborators: PUT", run.err)
        self.assertEqual(forgejo.collaborators[("toolkit", "claude")], "write")

    def test_given_only_when_collaborators_run_then_just_those_are_done_and_a_name_outside_the_catalog_exits_2(self):
        run = self.run_collaborators(["collaborators", "--only", "toolkit"])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual({repo for repo, _ in run.forgejo.collaborators}, {"toolkit"})
        refused = self.run_collaborators(["collaborators", "--only", "old-fork"])
        self.assertEqual(refused.code, 2)
        self.assertIn("old-fork", refused.err)
        self.assertEqual(refused.forgejo.writes(), [])

    def test_given_no_forgejo_token_when_collaborators_run_then_it_exits_2_before_anything_is_printed(self):
        run = self.run_collaborators(env={})
        self.assertEqual(run.code, 2)
        self.assertIn("FORGE_TOKEN", run.err)
        self.assertEqual(run.out, "")

    def test_given_no_github_token_when_collaborators_run_then_it_still_works(self):
        # Collaborators are a Forgejo matter: GitHub's token is not looked for.
        run = Run(self.ARGS, FakeGitHub([]), catalog=CATALOG_MIRROR, env=ENV, token="",
                  forgejo=FakeForgejo(existing={n: forgejo_repository(n) for n in ("shop-api", "toolkit")}))
        self.assertEqual(run.code, 0, run.err)

    def test_given_a_failure_and_no_github_token_when_collaborators_run_then_the_failure_is_still_reported(self):
        run = Run(self.ARGS, FakeGitHub([]), catalog=CATALOG_MIRROR, env=ENV, token="",
                  forgejo=FakeForgejo(existing={"toolkit": forgejo_repository("toolkit")}))
        self.assertEqual(run.code, 1)
        self.assertIn("shop-api: not on Forgejo: run import first", run.err)


JENKINS = "https://jenkins.example.com"
VERIFY_ENV = {**ENV, "FORGE_JENKINS_URL": JENKINS}
WEBHOOK_URL = f"{JENKINS}/gitea-webhook/post"
SHA_DEVELOP, SHA_MAIN, SHA_TAG, SHA_OTHER = "a1" * 20, "b2" * 20, "c3" * 20, "d4" * 20
# The repositories of MIRROR_CATALOG: the application shop-api's and two `repositories` entries. Any other is not
# in the catalog.
CATALOG_NAMES = ("shop-api", "toolkit", "other-lib")


def verified_world(names=("toolkit",), catalog_names=CATALOG_NAMES, **github_fields):
    """A GitHub and a Forgejo in which every one of `names` is moved and prepared as verify wants it: the same
    develop, main and tag on both sides (Forgejo also has a pull request ref, which is left out), a healthy
    push mirror, GitHub with `mirror tags` and issues off; a catalog repository also with its Jenkins webhook
    and the four bots. Returns (gh, forgejo); a test breaks one thing and runs verify."""
    heads = {"refs/heads/develop": SHA_DEVELOP, "refs/heads/main": SHA_MAIN, "refs/tags/v1.0.0": SHA_TAG}
    in_catalog = [n for n in names if n in catalog_names]
    gh = FakeGitHub([repository(n) for n in names], login=MIRROR,
                    refs={n: dict(heads) for n in names},
                    rulesets={n: [{"id": 5, "name": "mirror tags"}] for n in in_catalog},
                    settings={n: {"has_issues": False} for n in names}, **github_fields)
    forgejo = FakeForgejo(
        existing={n: forgejo_repository(n) for n in names},
        mirrors={n: [push_mirror(n)] for n in names},
        refs={n: {**heads, "refs/pull/1/head": SHA_OTHER} for n in names},
        hooks={n: [{"id": 4, "type": "gitea", "active": True, "config": {"url": WEBHOOK_URL}}] for n in in_catalog},
        collaborators={(n, user): permission for n in in_catalog for user, permission in
                       (("jenkins", "write"), ("jenkins-ci", "read"), ("renovate", "write"), ("claude", "write"))})
    return gh, forgejo


class VerifyTests(unittest.TestCase):
    ARGS = ["verify"]

    def run_verify(self, world=None, args=None, env=None, catalog=MIRROR_CATALOG):
        gh, forgejo = world or verified_world()
        return Run(args or self.ARGS, gh, catalog=catalog, env=VERIFY_ENV if env is None else env, forgejo=forgejo)

    def rows(self, run, repo=None):
        """The rows [repository, check, ok|FAIL, detail] the run printed, for one repository or all."""
        rows = [line.split("\t") for line in run.lines if "\t" in line]
        return [row for row in rows if repo in (None, row[0])]

    def row(self, run, repo, check):
        (found,) = [row for row in self.rows(run, repo) if row[1] == check]
        return found

    def assert_only_failing(self, run, repo, *checks):
        self.assertEqual([row[1] for row in self.rows(run, repo) if row[2] == "FAIL"], list(checks), run.out)

    def test_given_everything_in_place_when_verified_then_every_row_is_ok(self):
        world = verified_world(("toolkit", "old-fork"))
        run = self.run_verify(world)
        self.assertEqual(run.code, 0, run.out + run.err)
        self.assertEqual({row[2] for row in self.rows(run)}, {"ok"})
        # A catalog repository gets all eight checks; one that is not in the catalog has no webhook, no bots.
        self.assertEqual([row[1] for row in self.rows(run, "toolkit")],
                         ["on Forgejo", "default branch", "push mirror", "refs", "rulesets", "issues", "webhook",
                          "collaborators"])
        self.assertEqual([row[1] for row in self.rows(run, "old-fork")],
                         ["on Forgejo", "default branch", "push mirror", "refs", "rulesets", "issues"])
        self.assertEqual(run.lines[-1], "2 repositories, 0 checks failed")
        self.assertEqual({len(line.split("\t")) for line in run.lines if not line.startswith("note: ")} - {1}, {4})
        # Read only, however it goes: no write to either server.
        self.assertEqual((world[0].writes(), world[1].writes()), ([], []))

    def test_given_a_tag_on_another_commit_on_github_when_verified_then_it_fails_naming_the_tag(self):
        gh, forgejo = verified_world()
        gh.refs["toolkit"]["refs/tags/v1.0.0"] = SHA_OTHER
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 1)
        self.assert_only_failing(run, "toolkit", "refs")
        detail = self.row(run, "toolkit", "refs")[3]
        self.assertIn("refs/tags/v1.0.0", detail)
        self.assertIn(SHA_OTHER[:12], detail)
        self.assertIn(SHA_TAG[:12], detail)
        self.assertEqual(run.lines[-1], "1 repositories, 1 checks failed")
        self.assertIn("  toolkit: refs: " + detail, run.err.splitlines())

    def test_given_a_mirror_with_a_last_error_when_verified_then_it_fails_with_the_error(self):
        gh, forgejo = verified_world()
        # What GitHub says to a --mirror push that carries Forgejo's own pull request refs, on two lines and
        # with the address as git echoes it, credentials and all.
        error = ("push to https://mirror-bot:mirror-tok-789@github.com/x/toolkit.git failed:\n"
                 "! [remote rejected] refs/pull/1/head -> refs/pull/1/head (deny updating a hidden ref)")
        forgejo.mirrors["toolkit"] = [push_mirror("toolkit", last_error=error)]
        run = self.run_verify((gh, forgejo), env={**VERIFY_ENV, "MIRROR_GITHUB_TOKEN": "mirror-tok-789"})
        self.assertEqual(run.code, 1)
        self.assert_only_failing(run, "toolkit", "push mirror")
        detail = self.row(run, "toolkit", "push mirror")[3]
        self.assertIn("deny updating a hidden ref", detail)
        self.assertIn("refs/pull/1/head", detail)
        # One line, and the token and the credentials are gone from the row and from the failure list.
        self.assertNotIn("mirror-tok-789", run.out + run.err)
        self.assertNotIn("mirror-bot:", run.out + run.err)

    def test_given_a_repository_missing_on_forgejo_when_verified_then_it_fails(self):
        gh, forgejo = verified_world(("toolkit", "other-lib"))
        del forgejo.existing["other-lib"]
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 1)
        self.assertEqual(self.row(run, "other-lib", "on Forgejo")[2:], ["FAIL", "not on Forgejo: run import first"])
        # Nothing else is asked of a repository that is not there; the next one is verified in full.
        self.assertEqual([row[1] for row in self.rows(run, "other-lib")], ["on Forgejo"])
        self.assertEqual({row[2] for row in self.rows(run, "toolkit")}, {"ok"})
        self.assertEqual(run.lines[-1], "2 repositories, 1 checks failed")

    def test_given_a_catalog_repository_without_its_webhook_when_verified_then_it_fails(self):
        gh, forgejo = verified_world()
        forgejo.hooks["toolkit"] = [{"id": 9, "config": {"url": "https://elsewhere.example.com/gitea-webhook/post"}}]
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 1)
        self.assert_only_failing(run, "toolkit", "webhook")
        self.assertIn(WEBHOOK_URL, self.row(run, "toolkit", "webhook")[3])

    def test_given_a_trailing_slash_in_the_jenkins_url_when_verified_then_the_webhook_is_still_found(self):
        # rules.py strips it when it makes the hook, so the hook has none.
        run = self.run_verify(env={**VERIFY_ENV, "FORGE_JENKINS_URL": JENKINS + "/"})
        self.assertEqual(run.code, 0, run.out)

    def test_given_no_jenkins_url_when_verified_then_the_webhook_of_a_catalog_repository_fails(self):
        run = self.run_verify(verified_world(("toolkit", "old-fork")), env=ENV)
        self.assertEqual(run.code, 1)
        self.assertEqual(self.row(run, "toolkit", "webhook")[2:],
                         ["FAIL", "FORGE_JENKINS_URL not set: can't check the webhook"])
        self.assert_only_failing(run, "toolkit", "webhook")
        # A repository outside the catalog has no webhook to check.
        self.assertEqual({row[2] for row in self.rows(run, "old-fork")}, {"ok"})

    def test_given_no_push_mirror_when_verified_then_it_fails(self):
        gh, forgejo = verified_world()
        forgejo.mirrors["toolkit"] = []
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "push mirror")
        self.assertIn("no push mirror", self.row(run, "toolkit", "push mirror")[3])

    def test_given_a_mirror_to_another_address_when_verified_then_it_fails_naming_it(self):
        gh, forgejo = verified_world()
        forgejo.mirrors["toolkit"] = [push_mirror("toolkit", "https://github.com/someone-else/toolkit.git")]
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "push mirror")
        self.assertIn("https://github.com/someone-else/toolkit.git", self.row(run, "toolkit", "push mirror")[3])

    def test_given_a_mirror_that_never_synced_when_verified_then_it_fails(self):
        for last_update in (None, ""):
            with self.subTest(last_update=last_update):
                gh, forgejo = verified_world()
                forgejo.mirrors["toolkit"] = [push_mirror("toolkit", last_update=last_update)]
                run = self.run_verify((gh, forgejo))
                self.assert_only_failing(run, "toolkit", "push mirror")
                self.assertIn("never synced", self.row(run, "toolkit", "push mirror")[3])

    def test_given_the_mirror_address_in_another_spelling_when_verified_then_it_is_the_same_mirror(self):
        gh, forgejo = verified_world()
        forgejo.mirrors["toolkit"] = [push_mirror("toolkit", f"https://github.com/{MIRROR}/toolkit")]
        self.assertEqual(self.run_verify((gh, forgejo)).code, 0)

    def test_given_a_ref_on_one_side_only_when_verified_then_it_fails_naming_the_ref_and_the_side(self):
        gh, forgejo = verified_world()
        del gh.refs["toolkit"]["refs/heads/main"]               # Forgejo has main, GitHub has not
        forgejo.refs["toolkit"]["refs/tags/v2.0.0"] = SHA_TAG  # a tag GitHub lacks
        gh.refs["toolkit"]["refs/tags/v0.9.0"] = SHA_TAG        # a tag Forgejo lacks
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "refs")
        detail = self.row(run, "toolkit", "refs")[3]
        self.assertIn("refs/heads/main only on Forgejo", detail)
        self.assertIn("refs/tags/v2.0.0 only on Forgejo", detail)
        self.assertIn("refs/tags/v0.9.0 only on GitHub", detail)

    def test_given_refs_outside_branches_and_tags_when_verified_then_they_are_not_compared(self):
        # refs/pull/* is Forgejo's own (and GitHub's, for its pull requests); notes are neither a branch nor
        # a tag. Neither side's are compared.
        gh, forgejo = verified_world()
        forgejo.refs["toolkit"].update({"refs/pull/2/head": SHA_OTHER, "refs/notes/commits": SHA_OTHER})
        gh.refs["toolkit"].update({"refs/pull/9/head": SHA_MAIN})
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 0, run.out)
        self.assertEqual(self.row(run, "toolkit", "refs")[3], "3 refs the same")

    def test_given_any_branch_on_one_side_or_differing_when_verified_then_every_branch_is_compared(self):
        # After a --mirror push GitHub holds exactly Forgejo's branches, so every one is compared, not only
        # develop and main: one that differs is a push the mirror has not carried (or one made on GitHub).
        gh, forgejo = verified_world()
        forgejo.refs["toolkit"]["refs/heads/feature/x"] = SHA_OTHER
        gh.refs["toolkit"]["refs/heads/develop-old"] = SHA_OTHER
        gh.refs["toolkit"]["refs/heads/release/1.0"] = SHA_MAIN
        forgejo.refs["toolkit"]["refs/heads/release/1.0"] = SHA_DEVELOP
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "refs")
        detail = self.row(run, "toolkit", "refs")[3]
        self.assertIn("refs/heads/feature/x only on Forgejo", detail)
        self.assertIn("refs/heads/develop-old only on GitHub", detail)
        self.assertIn(f"refs/heads/release/1.0 differs (GitHub {SHA_MAIN[:12]}, Forgejo {SHA_DEVELOP[:12]})", detail)
        # The same branches on both sides: they count among the refs compared.
        gh.refs["toolkit"]["refs/heads/feature/x"] = SHA_OTHER
        forgejo.refs["toolkit"]["refs/heads/develop-old"] = SHA_OTHER
        forgejo.refs["toolkit"]["refs/heads/release/1.0"] = SHA_MAIN
        run = self.run_verify((gh, forgejo))
        self.assertEqual(self.row(run, "toolkit", "refs")[2:], ["ok", "6 refs the same"])

    def test_given_more_refs_than_a_page_when_verified_then_all_are_compared_whichever_side_pages(self):
        for forgejo_page in (30, 100, None):  # None: a server that ignores `page` and answers everything each time
            with self.subTest(forgejo_page=forgejo_page):
                gh, forgejo = verified_world()
                forgejo.ref_page = forgejo_page
                for n in range(75):
                    gh.refs["toolkit"][f"refs/tags/v2.{n}.0"] = SHA_TAG
                    forgejo.refs["toolkit"][f"refs/tags/v2.{n}.0"] = SHA_TAG
                self.assertEqual(self.run_verify((gh, forgejo)).code, 0)
                # The last tag, far past the first pages, is one that counts.
                gh.refs["toolkit"]["refs/tags/v2.74.0"] = SHA_OTHER
                run = self.run_verify((gh, forgejo))
                self.assertIn("refs/tags/v2.74.0", self.row(run, "toolkit", "refs")[3])

    def test_given_no_branch_or_tag_on_either_side_of_a_copy_that_is_not_empty_when_verified_then_it_fails(self):
        # GitHub answers a prefix nothing matches with [], Forgejo with 404: neither is an error. But a copy
        # Forgejo says is not empty has a branch: when neither side lists one, nothing was compared, and an
        # `ok` would say the two agree without having looked.
        gh, forgejo = verified_world()
        gh.refs["toolkit"], forgejo.refs["toolkit"] = {}, {}
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "refs")
        self.assertIn("nothing compared", self.row(run, "toolkit", "refs")[3])

    def test_given_an_empty_repository_when_verified_then_githubs_409_is_no_refs(self):
        gh, forgejo = verified_world()
        gh.empty.add("toolkit")
        gh.refs["toolkit"], forgejo.refs["toolkit"] = {}, {}
        forgejo.existing["toolkit"] = forgejo_repository("toolkit", empty=True, default_branch="")
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 0, run.out)
        self.assertIn("empty", self.row(run, "toolkit", "refs")[3])

    def test_given_a_default_branch_that_is_not_githubs_when_verified_then_it_fails(self):
        gh, forgejo = verified_world()
        forgejo.existing["toolkit"] = forgejo_repository("toolkit", default_branch="main")
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "default branch")
        detail = self.row(run, "toolkit", "default branch")[3]
        self.assertIn("develop", detail)
        self.assertIn("main", detail)

    def test_given_githubs_default_branch_is_not_develop_when_verified_then_forgejos_must_be_the_same(self):
        gh, forgejo = verified_world()
        gh.repos[0] = repository("toolkit", default_branch="main")
        forgejo.existing["toolkit"] = forgejo_repository("toolkit", default_branch="main")
        self.assertEqual(self.run_verify((gh, forgejo)).code, 0)
        forgejo.existing["toolkit"] = forgejo_repository("toolkit", default_branch="develop")
        self.assert_only_failing(self.run_verify((gh, forgejo)), "toolkit", "default branch")

    def test_given_an_archived_repository_when_verified_then_only_forgejo_is_checked(self):
        gh, forgejo = verified_world(("toolkit", "old-fork"))
        gh.repos[1] = repository("old-fork", archived=True)
        forgejo.mirrors["old-fork"] = []                      # none expected
        gh.refs["old-fork"]["refs/tags/v1.0.0"] = SHA_OTHER   # GitHub's copy is frozen: not compared
        gh.settings["old-fork"] = {"has_issues": True}
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 0, run.out)
        self.assertEqual(self.rows(run, "old-fork"),
                         [["old-fork", "on Forgejo", "ok", "present"],
                          ["old-fork", "default branch", "ok", "develop"],
                          ["old-fork", "github", "ok", "archived on GitHub: Forgejo only"]])
        self.assertFalse([c for c in gh.calls if c[1].startswith(f"/repos/{MIRROR}/old-fork")])
        # Its presence on Forgejo still counts.
        del forgejo.existing["old-fork"]
        self.assertEqual(self.row(self.run_verify((gh, forgejo)), "old-fork", "on Forgejo")[2], "FAIL")

    def test_given_rulesets_when_verified_then_a_release_repository_has_exactly_mirror_tags(self):
        cases = {
            "missing": ([], "mirror tags"),
            "extra": ([{"id": 5, "name": "mirror tags"}, {"id": 6, "name": "develop"}], "develop"),
            "another": ([{"id": 6, "name": "release tags"}], "mirror tags"),
        }
        for label, (rulesets, named) in cases.items():
            with self.subTest(label):
                gh, forgejo = verified_world()
                gh.rulesets["toolkit"] = rulesets
                run = self.run_verify((gh, forgejo))
                self.assert_only_failing(run, "toolkit", "rulesets")
                self.assertIn(named, self.row(run, "toolkit", "rulesets")[3])

    def test_given_a_basic_or_non_catalog_repository_when_verified_then_it_must_have_no_rulesets(self):
        catalog = {**MIRROR_CATALOG, "repositories": [{"id": "toolkit", "rules": "basic"}, {"id": "other-lib"}]}
        gh, forgejo = verified_world(("toolkit", "old-fork"), catalog_names=("other-lib",))
        run = self.run_verify((gh, forgejo), catalog=catalog)
        self.assertEqual(self.row(run, "toolkit", "rulesets")[2], "ok")
        self.assertEqual(self.row(run, "old-fork", "rulesets")[2], "ok")
        for name in ("toolkit", "old-fork"):
            with self.subTest(name):
                # (A new world each time: the fake keeps a repository's state from its first request.)
                gh, forgejo = verified_world(("toolkit", "old-fork"), catalog_names=("other-lib",))
                gh.rulesets[name] = [{"id": 5, "name": "mirror tags"}]
                run = self.run_verify((gh, forgejo), catalog=catalog)
                self.assertEqual(self.row(run, name, "rulesets")[2], "FAIL")
                self.assertIn("mirror tags", self.row(run, name, "rulesets")[3])

    def test_given_a_private_repository_whose_rulesets_are_refused_when_verified_then_it_is_not_a_failure(self):
        gh, forgejo = verified_world(("toolkit", "old-fork"))
        gh.repos[0] = repository("toolkit", private=True)
        gh.fail = {"toolkit": {("GET", "/rulesets"): 403}, "old-fork": {("GET", "/rulesets"): 403}}
        run = self.run_verify((gh, forgejo))
        # Private: a plan without rulesets, there are none to check. Public: a real refusal.
        self.assertEqual(self.row(run, "toolkit", "rulesets")[2:], ["ok", "rulesets not available here"])
        self.assertEqual(self.row(run, "old-fork", "rulesets")[2], "FAIL")
        self.assertIn("403", self.row(run, "old-fork", "rulesets")[3])

    def test_given_issues_on_when_verified_then_it_fails(self):
        gh, forgejo = verified_world()
        gh.settings["toolkit"] = {"has_issues": True}
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "issues")

    def test_given_a_bot_that_is_not_a_collaborator_when_verified_then_it_fails_though_the_repository_reads_for_anyone(self):
        gh, forgejo = verified_world()
        # A public repository: the permission endpoint says `read` for jenkins-ci whether it is one or not,
        # so only the collaborators list tells.
        forgejo.effective_read = True
        del forgejo.collaborators[("toolkit", "jenkins-ci")]
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "collaborators")
        self.assertIn("jenkins-ci: not a collaborator", self.row(run, "toolkit", "collaborators")[3])

    def test_given_a_bot_with_another_permission_when_verified_then_it_fails_naming_both(self):
        gh, forgejo = verified_world()
        forgejo.collaborators[("toolkit", "claude")] = "read"
        forgejo.collaborators[("toolkit", "jenkins-ci")] = "write"
        run = self.run_verify((gh, forgejo))
        self.assert_only_failing(run, "toolkit", "collaborators")
        detail = self.row(run, "toolkit", "collaborators")[3]
        self.assertIn("claude: has read, wants write", detail)
        self.assertIn("jenkins-ci: has write, wants read", detail)

    def test_given_another_jenkins_user_in_the_catalog_when_verified_then_that_one_is_the_bot_looked_for(self):
        catalog = {**MIRROR_CATALOG, "forge": {**MIRROR_CATALOG["forge"], "jenkinsUser": "ci-jenkins"}}
        run = self.run_verify(catalog=catalog)
        self.assertIn("ci-jenkins: not a collaborator", self.row(run, "toolkit", "collaborators")[3])

    def test_given_calls_that_error_when_verified_then_only_those_checks_fail_and_the_run_goes_on(self):
        gh, forgejo = verified_world(("toolkit", "other-lib"))
        forgejo.verify_fail[("toolkit", "hooks")] = 500
        forgejo.verify_fail[("toolkit", "git/refs")] = 502
        forgejo.mirror_fail[("toolkit", "GET", "push_mirrors")] = 500
        gh.refs_fail.add("other-lib")
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 1)
        self.assert_only_failing(run, "toolkit", "push mirror", "refs", "webhook")
        self.assertIn("could not check", self.row(run, "toolkit", "webhook")[3])
        self.assertIn("500", self.row(run, "toolkit", "webhook")[3])
        self.assert_only_failing(run, "other-lib", "refs")
        self.assertEqual(self.row(run, "other-lib", "collaborators")[2], "ok")
        self.assertEqual(run.lines[-1], "2 repositories, 4 checks failed")

    def test_given_a_forgejo_lookup_that_errors_when_verified_then_the_repository_fails_and_the_run_goes_on(self):
        gh, forgejo = verified_world(("toolkit", "other-lib"))
        forgejo.lookup["toolkit"] = 500
        run = self.run_verify((gh, forgejo))
        self.assertEqual(run.code, 1)
        self.assertIn("could not look for it on Forgejo", self.row(run, "toolkit", "on Forgejo")[3])
        self.assertEqual({row[2] for row in self.rows(run, "other-lib")}, {"ok"})

    def test_given_only_when_verified_then_just_those_are_verified_and_an_unknown_name_exits_2(self):
        gh, forgejo = verified_world(("toolkit", "other-lib"))
        run = self.run_verify((gh, forgejo), ["verify", "--only", "other-lib"])
        self.assertEqual(run.code, 0, run.out)
        self.assertEqual({row[0] for row in self.rows(run)}, {"other-lib"})
        self.assertEqual(run.lines[-1], "1 repositories, 0 checks failed")
        self.assertFalse([c for c in forgejo.calls if "toolkit" in c[1]])
        refused = self.run_verify((gh, forgejo), ["verify", "--only", "nope"])
        self.assertEqual(refused.code, 2)

    def test_given_a_dry_run_when_verified_then_it_is_the_same_read_only_run(self):
        world = verified_world()
        run = self.run_verify(world, ["--dry-run", "verify"])
        self.assertEqual(run.code, 0, run.out)
        self.assertEqual((world[0].writes(), world[1].writes()), ([], []))

    def test_given_a_catalog_repository_not_on_github_when_verified_then_it_is_noted_not_verified(self):
        run = self.run_verify(verified_world(("toolkit",)))
        self.assertIn(f"note: other-lib is in the catalog but not on GitHub under {MIRROR}", run.lines)
        self.assertEqual(run.code, 0, run.out)

    def test_given_no_forgejo_token_when_verified_then_it_exits_2_before_anything_is_printed(self):
        run = self.run_verify(env={})
        self.assertEqual(run.code, 2)
        self.assertIn("FORGE_TOKEN", run.err)
        self.assertEqual(run.out, "")

    def test_given_the_forgejo_token_in_an_error_when_verified_then_it_is_hidden(self):
        gh, forgejo = verified_world()
        forgejo.mirrors["toolkit"] = [push_mirror("toolkit", last_error="denied for fg-tok-123")]
        run = self.run_verify((gh, forgejo))
        self.assertIn("denied for ***", self.row(run, "toolkit", "push mirror")[3])
        self.assertNotIn("fg-tok-123", run.out + run.err)


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

    def test_given_the_mirror_token_in_a_failure_when_reported_then_it_is_hidden_too(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"MIRROR_GITHUB_TOKEN": "mirror-tok-789"}):
            shown = migrate._report(self.context("gh-tok-456"), "a: echoed mirror-tok-789 and gh-tok-456")
        self.assertEqual(shown, "a: echoed *** and ***")

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
