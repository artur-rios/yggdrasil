"""Tests for scripts/prune_release_branches.py.   python3 -m unittest discover -s scripts"""

import io
import os
import unittest
import urllib.parse
from unittest import mock

import prune_release_branches as prune
import rules

OWNER = "artur-rios"
ENV = {"FORGE_TOKEN": "fg-tok-123"}

CATALOG = {
    "owner": OWNER,
    "forge": {"type": "forgejo", "url": "https://git.example.com", "jenkinsUser": "jenkins-bot"},
    "environments": [{"id": "production", "name": "Production", "trigger": "release"}],
    "systems": [
        {"id": "shop", "name": "Shop", "applications": [
            {"id": "shop-api", "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz",
             "checks": ["test"]},
        ]},
    ],
    "repositories": [
        {"id": "toolkit", "checks": ["build"], "release": "owner-tag", "tags": "[0-9]*"},
        {"id": "scratch", "rules": "basic"},
    ],
}

CATALOG_GITHUB = {key: value for key, value in CATALOG.items() if key != "forge"}

# What rules.py keeps on Forgejo for release/*, written out by hand: the restored rule must equal it.
RELEASE_RULE = {"rule_name": "release/*", "enable_push": True, "enable_push_whitelist": True,
                "push_whitelist_usernames": [OWNER], "enable_merge_whitelist": False, "apply_to_admins": False}


class Repo:
    """One repository on the fake server. `contains` lists (outer, inner) commits: inner is in outer."""

    def __init__(self, branches, tags=(), open_pulls=(), contains=(), rule=True):
        self.branches = dict(branches)
        self.tags = list(tags)
        self.open_pulls = list(open_pulls)
        self.contains = set(contains)
        self.rules = {"release/*": dict(RELEASE_RULE)} if rule else {}


class FakeForgejo:
    """Answers like Forgejo: paged lists, and a refusal to delete a branch a protection covers."""

    PAGE = 50

    def __init__(self, repos, fail=None):
        self.repos = repos
        self.fail = fail or (lambda method, path: None)
        self.calls = []

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def _page(self, items, query):
        limit = min(int(query.get("limit", [self.PAGE])[0]), self.PAGE)
        page = int(query.get("page", ["1"])[0])
        return items[(page - 1) * limit:page * limit]

    def request(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        refusal = self.fail(method, path)
        if refusal is not None:
            raise rules.ApiError(method, path, refusal, "refused")
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query)
        _, _, owner, name, section, *rest = parsed.path.split("/")
        assert owner == OWNER
        repo = self.repos[name]
        target = urllib.parse.unquote("/".join(rest))
        if section == "branches" and method == "GET":
            return self._page([{"name": b, "commit": {"id": sha}, "protected": False}
                               for b, sha in repo.branches.items()], query)
        if section == "branches" and method == "DELETE":
            if target.startswith("release/") and "release/*" in repo.rules:
                raise rules.ApiError(method, path, 403, "branch protected")
            del repo.branches[target]
            return None
        if section == "tags":
            return self._page([{"name": t, "commit": {"sha": sha}} for t, sha in repo.tags], query)
        if section == "pulls":
            assert query["state"] == ["open"]
            return self._page([{"head": {"ref": head}, "base": {"ref": base}} for head, base in repo.open_pulls], query)
        if section == "compare":
            outer, inner = target.split("...")
            return {"total_commits": 0 if outer == inner or (outer, inner) in repo.contains else 3, "commits": []}
        if section == "branch_protections" and method == "GET":
            return list(repo.rules.values())
        if section == "branch_protections" and method == "DELETE":
            del repo.rules[target]
            return None
        if section == "branch_protections" and method == "POST":
            repo.rules[body["rule_name"]] = dict(body)
            return dict(body)
        raise AssertionError(f"unexpected request: {method} {path}")


def released(**overrides):
    """A repository with release/1.0.0 in main and in the tag v1.0.0, and another branch beside it."""
    values = {"branches": {"develop": "dev", "main": "main-tip", "release/1.0.0": "rel"},
              "tags": [("v1.0.0", "tag-sha")],
              "contains": [("main-tip", "rel"), ("tag-sha", "rel")]}
    values.update(overrides)
    return Repo(**values)


def run(args, fake, catalog=CATALOG, env=None):
    out, err = io.StringIO(), io.StringIO()
    env = ENV if env is None else env
    with mock.patch.object(rules.catalog_module, "load", return_value=catalog), \
            mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(rules, "Api", lambda *a, **k: fake), \
            mock.patch.object(prune.time, "sleep"), \
            mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        try:
            prune.main(args)
            exit_ = None
        except SystemExit as raised:
            exit_ = raised
    return out.getvalue(), exit_


class DeletionTests(unittest.TestCase):
    def test_given_a_released_branch_when_run_then_it_is_deleted(self):
        repo = released()
        out, exit_ = run(["shop-api"], FakeForgejo({"shop-api": repo}))
        self.assertIsNone(exit_, out)
        self.assertEqual(set(repo.branches), {"develop", "main"})

    def test_given_a_released_branch_when_run_then_the_protection_is_back_as_the_model_has_it(self):
        repo = released()
        fake = FakeForgejo({"shop-api": repo})
        run(["shop-api"], fake)
        self.assertEqual(repo.rules, {"release/*": RELEASE_RULE})
        self.assertEqual([body for method, path, body in fake.writes() if method == "POST"], [RELEASE_RULE])

    def test_given_a_branch_only_a_later_tag_contains_when_run_then_it_is_deleted(self):
        # A date-named release has no tag of its own name: the later version tags hold its commits.
        repo = released(branches={"main": "main-tip", "release/2026.10.29": "rel"},
                        tags=[("v9.0.0", "later"), ("v1.0.0", "older")],
                        contains=[("main-tip", "rel"), ("later", "rel")])
        run(["shop-api"], FakeForgejo({"shop-api": repo}))
        self.assertEqual(set(repo.branches), {"main"})

    def test_given_an_owner_tagged_repository_when_run_then_its_own_tag_pattern_counts(self):
        repo = released(tags=[("1.0.0", "tag-sha")])
        run(["toolkit"], FakeForgejo({"toolkit": repo}))
        self.assertNotIn("release/1.0.0", repo.branches)

    def test_given_other_branches_in_main_when_run_then_only_release_branches_go(self):
        repo = released(branches={"main": "main-tip", "feature/x": "rel", "release/1.0.0": "rel"})
        run(["shop-api"], FakeForgejo({"shop-api": repo}))
        self.assertEqual(set(repo.branches), {"main", "feature/x"})

    def test_given_more_than_a_page_of_branches_and_tags_when_run_then_all_are_seen(self):
        names = [f"release/1.{n}.0" for n in range(60)]
        repo = Repo(branches={"main": "main-tip", **{n: n for n in names}},
                    tags=[(f"v0.{n}.0", f"old{n}") for n in range(60)] + [("v9.0.0", "last")],
                    contains=[("main-tip", n) for n in names] + [("last", n) for n in names])
        run(["shop-api"], FakeForgejo({"shop-api": repo}))
        self.assertEqual(set(repo.branches), {"main"})

    def test_given_no_protection_when_run_then_the_branch_goes_and_none_is_created(self):
        repo = released(rule=False)
        run(["shop-api"], FakeForgejo({"shop-api": repo}))
        self.assertEqual((set(repo.branches), repo.rules), ({"develop", "main"}, {}))


class KeptTests(unittest.TestCase):
    def assert_kept(self, repo, name="shop-api", reason=None):
        fake = FakeForgejo({name: repo})
        out, exit_ = run([name], fake)
        self.assertIsNone(exit_, out)
        self.assertIn("release/1.0.0", repo.branches)
        self.assertEqual(fake.writes(), [], "the protection is not touched when nothing is deleted")
        if reason:
            self.assertIn(reason, out)

    def test_given_a_branch_not_in_main_when_run_then_it_is_kept(self):
        self.assert_kept(released(contains=[("tag-sha", "rel")]), reason="main")

    def test_given_a_branch_no_tag_contains_when_run_then_it_is_kept(self):
        self.assert_kept(released(contains=[("main-tip", "rel")]), reason="tag")

    def test_given_only_a_tag_outside_the_version_pattern_when_run_then_it_is_kept(self):
        self.assert_kept(released(tags=[("nightly", "tag-sha")]), reason="tag")

    def test_given_an_open_pull_request_from_it_when_run_then_it_is_kept(self):
        self.assert_kept(released(open_pulls=[("release/1.0.0", "main")]), reason="pull request")

    def test_given_an_open_pull_request_into_it_when_run_then_it_is_kept(self):
        self.assert_kept(released(open_pulls=[("feature/hotfix", "release/1.0.0")]), reason="pull request")

    def test_given_a_repository_without_main_when_run_then_it_is_kept(self):
        self.assert_kept(released(branches={"master": "main-tip", "release/1.0.0": "rel"}), reason="main")


class DryRunTests(unittest.TestCase):
    def test_given_a_dry_run_when_run_then_nothing_is_written_and_the_branch_is_named(self):
        repo = released()
        fake = FakeForgejo({"shop-api": repo})
        out, exit_ = run(["--dry-run", "shop-api"], fake)
        self.assertIsNone(exit_, out)
        self.assertEqual(fake.writes(), [])
        self.assertIn("release/1.0.0", out)


class FailureTests(unittest.TestCase):
    @staticmethod
    def refuse(method_wanted, fragment, status=500, times=None):
        left = [times]

        def fail(method, path):
            if method != method_wanted or fragment not in path:
                return None
            if left[0] is None:
                return status
            if left[0] > 0:
                left[0] -= 1
                return status
            return None
        return fail

    def two_branches(self):
        return released(branches={"main": "main-tip", "release/1.0.0": "a", "release/2.0.0": "b"},
                        contains=[("main-tip", "a"), ("main-tip", "b"), ("tag-sha", "a"), ("tag-sha", "b")])

    def test_given_a_branch_the_server_will_not_delete_when_run_then_the_rest_go_and_the_run_fails_naming_it(self):
        repo = self.two_branches()
        out, exit_ = run(["shop-api"], FakeForgejo({"shop-api": repo}, self.refuse("DELETE", "branches/release%2F1.0.0")))
        self.assertEqual(set(repo.branches), {"main", "release/1.0.0"})
        self.assertIsNotNone(exit_)
        self.assertIn("release/1.0.0", str(exit_))

    def test_given_a_branch_the_server_will_not_delete_when_run_then_the_protection_still_comes_back(self):
        repo = self.two_branches()
        run(["shop-api"], FakeForgejo({"shop-api": repo}, self.refuse("DELETE", "branches/release%2F1.0.0")))
        self.assertNotIn("release/2.0.0", repo.branches, "the protection was lifted")
        self.assertEqual(repo.rules, {"release/*": RELEASE_RULE})

    def test_given_a_brief_failure_restoring_when_run_then_it_is_retried(self):
        repo = released()
        fake = FakeForgejo({"shop-api": repo}, self.refuse("POST", "branch_protections", times=2))
        out, exit_ = run(["shop-api"], fake)
        self.assertIsNone(exit_, out)
        self.assertEqual(repo.rules, {"release/*": RELEASE_RULE})
        self.assertEqual(len([call for call in fake.writes() if call[0] == "POST"]), 3)

    def test_given_the_protection_cannot_be_restored_when_run_then_the_run_stops_and_says_how_to_fix_it(self):
        first, second = released(), released()
        fake = FakeForgejo({"shop-api": first, "toolkit": second}, self.refuse("POST", "/shop-api/branch_protections"))
        second.tags = [("1.0.0", "tag-sha")]
        out, exit_ = run([], fake)
        self.assertIn("rules.py shop-api", str(exit_))
        self.assertIn("release/1.0.0", second.branches, "no further repository is opened up")
        self.assertIn("release/*", second.rules)

    def test_given_the_protection_cannot_be_lifted_when_run_then_nothing_is_deleted(self):
        repo = released()
        out, exit_ = run(["shop-api"], FakeForgejo({"shop-api": repo}, self.refuse("DELETE", "branch_protections")))
        self.assertIsNotNone(exit_)
        self.assertIn("release/1.0.0", repo.branches)


class CommandTests(unittest.TestCase):
    def test_given_no_repository_when_run_then_every_release_model_repository_is_covered(self):
        fake = FakeForgejo({"shop-api": released(), "toolkit": released(tags=[("1.0.0", "tag-sha")])})
        out, exit_ = run([], fake)
        self.assertIsNone(exit_, out)
        self.assertEqual({name: "release/1.0.0" in repo.branches for name, repo in fake.repos.items()},
                         {"shop-api": False, "toolkit": False})

    def test_given_no_token_when_run_then_it_is_refused_before_any_request(self):
        fake = FakeForgejo({})
        out, exit_ = run([], fake, env={})
        self.assertIn("FORGE_TOKEN", str(exit_))
        self.assertEqual(fake.calls, [])

    def test_given_a_github_catalog_when_run_then_it_is_refused_before_any_request(self):
        fake = FakeForgejo({})
        out, exit_ = run([], fake, catalog=CATALOG_GITHUB)
        self.assertIn("Forgejo", str(exit_))
        self.assertEqual(fake.calls, [])

    def test_given_a_basic_repository_when_named_then_it_is_refused_before_any_request(self):
        fake = FakeForgejo({})
        out, exit_ = run(["scratch"], fake)
        self.assertIn("scratch", str(exit_))
        self.assertEqual(fake.calls, [])

    def test_given_an_unknown_option_when_run_then_it_is_refused_before_any_request(self):
        fake = FakeForgejo({})
        out, exit_ = run(["--force"], fake)
        self.assertIn("--force", str(exit_))
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
