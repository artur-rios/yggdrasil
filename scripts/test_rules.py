"""Tests for scripts/rules.py (the model).   python3 -m unittest discover -s scripts"""

import copy
import dataclasses
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import threading
import unittest
import urllib.parse
from unittest import mock

import rules

ROOT = pathlib.Path(__file__).resolve().parent.parent

CATALOG = {
    "owner": "artur-rios",
    "forge": {"type": "forgejo", "url": "https://git.example.com", "jenkinsUser": "jenkins-bot",
              "developers": ["alice", "bob"]},
    "environments": [
        {"id": "dev", "name": "Dev", "trigger": "manual"},
        {"id": "staging", "name": "Staging", "trigger": "branch", "branches": "release/*"},
        {"id": "production", "name": "Production", "trigger": "release"},
    ],
    "systems": [
        {"id": "shop", "name": "Shop", "applications": [
            {"id": "shop-api", "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz",
             "checks": ["test", "docker"], "actionsChecks": ["lint"]},
            {"id": "shop-web", "name": "Web", "kind": "web", "health": "http://shop-web:8080/healthz",
             "repository": "storefront", "environments": {"dev": {}, "staging": {}}},
        ]},
        {"id": "platform", "name": "Platform", "applications": [
            {"id": "traefik", "name": "Traefik", "kind": "platform", "health": "http://traefik:8082/ping"},
        ]},
    ],
    "repositories": [
        {"id": "toolkit", "checks": ["build"], "release": "owner-tag", "tags": "[0-9]*"},
        {"id": "scratch", "rules": "basic", "checks": ["ignored"]},
        {"id": "notes", "checks": ["lint"]},
    ],
}

CATALOG_GITHUB = copy.deepcopy(CATALOG)
del CATALOG_GITHUB["forge"]  # no forge block: GitHub, as before


def by_repo(catalog=CATALOG):
    return {r.repo: r for r in rules.desired_rules(catalog)}


class DesiredRulesTests(unittest.TestCase):
    def test_given_a_catalog_when_modelled_then_applications_come_first_then_repositories(self):
        self.assertEqual([r.repo for r in rules.desired_rules(CATALOG)],
                         ["shop-api", "storefront", "toolkit", "scratch", "notes"])

    def test_given_an_application_when_modelled_then_main_requires_its_release_deploys(self):
        r = rules.desired_rules(CATALOG)[0]
        self.assertEqual(r.main_checks, ("branch-policy", "test", "docker", "deploy/production"))

    def test_given_an_application_without_release_environments_when_modelled_then_main_has_no_deploys(self):
        self.assertEqual(by_repo()["storefront"].main_checks, ("branch-policy",))

    def test_given_an_application_when_modelled_then_only_jenkins_merges_main_and_tags(self):
        r = by_repo()["shop-api"]
        self.assertEqual((r.main_mergers, r.tag_pattern, r.tag_creators, r.release_pushers),
                         (("jenkins-bot",), "v*", ("jenkins-bot",), ("artur-rios",)))
        self.assertEqual(r.protected_files, ("Jenkinsfile", "yggdrasil.yml"))
        self.assertEqual(r.model, "release")

    def test_given_actions_checks_when_modelled_then_develop_requires_them_and_main_does_not(self):
        r = by_repo()["shop-api"]
        self.assertEqual(r.develop_checks, ("branch-policy", "test", "docker", "lint"))
        self.assertNotIn("lint", r.main_checks)
        self.assertEqual(r.actions_checks, frozenset({"lint"}))

    def test_given_developers_when_modelled_then_they_may_merge_develop_only(self):
        r = by_repo()["shop-api"]
        self.assertEqual(r.develop_mergers, ("artur-rios", "alice", "bob"))
        self.assertNotIn("alice", r.main_mergers)

    def test_given_no_developers_when_modelled_then_the_owner_alone_merges_develop(self):
        value = copy.deepcopy(CATALOG)
        del value["forge"]
        r = by_repo(value)["shop-api"]
        self.assertEqual((r.develop_mergers, r.main_mergers, r.tag_creators),
                         (("artur-rios",), ("jenkins",), ("jenkins",)))

    def test_given_a_library_when_modelled_then_the_owner_creates_its_numeric_tags(self):
        r = by_repo()["toolkit"]
        self.assertEqual((r.tag_pattern, r.tag_creators), ("[0-9]*", ("artur-rios",)))
        self.assertEqual((r.main_mergers, r.main_checks), (("artur-rios",), ("branch-policy", "build")))
        self.assertEqual(r.develop_mergers, ("artur-rios", "alice", "bob"))

    def test_given_a_repository_without_releases_when_modelled_then_the_owner_creates_v_tags(self):
        r = by_repo()["notes"]
        self.assertEqual((r.tag_pattern, r.tag_creators, r.main_mergers),
                         ("v*", ("artur-rios",), ("artur-rios",)))
        self.assertEqual(r.develop_checks, ("branch-policy", "lint"))

    def test_given_the_catalog_when_modelled_then_only_the_repositories_entries_have_owner_tags(self):
        self.assertEqual({repo: r.owner_tags for repo, r in by_repo().items()},
                         {"shop-api": False, "storefront": False, "toolkit": True, "scratch": True, "notes": True})

    def test_given_a_basic_repository_when_modelled_then_it_has_no_checks_or_mergers(self):
        r = by_repo()["scratch"]
        self.assertEqual(r.model, "basic")
        self.assertEqual((r.develop_checks, r.main_checks, r.actions_checks), ((), (), frozenset()))
        self.assertEqual((r.develop_mergers, r.main_mergers, r.protected_files), ((), (), ()))

    def test_given_a_check_listed_twice_when_modelled_then_it_is_required_once(self):
        value = copy.deepcopy(CATALOG)
        app = value["systems"][0]["applications"][0]
        app["checks"] = ["test", "branch-policy", "test"]
        app["actionsChecks"] = ["test", "lint", "lint"]
        r = by_repo(value)["shop-api"]
        self.assertEqual(r.develop_checks, ("branch-policy", "test", "lint"))
        self.assertEqual(r.main_checks, ("branch-policy", "test", "deploy/production"))

    def test_given_two_applications_in_one_repository_when_modelled_then_it_is_one_entry(self):
        value = copy.deepcopy(CATALOG)
        value["systems"][0]["applications"][1].update({"repository": "shop-api", "checks": ["e2e"]})
        modelled = [r for r in rules.desired_rules(value) if r.repo == "shop-api"]
        self.assertEqual(len(modelled), 1)
        self.assertEqual(modelled[0].main_checks, ("branch-policy", "test", "docker", "e2e", "deploy/production"))

    def test_given_a_library_and_its_docs_application_when_modelled_then_it_is_one_owner_tag_rule_set(self):
        value = copy.deepcopy(CATALOG)
        value["systems"][0]["applications"].append(
            {"id": "toolkit-docs", "name": "Docs", "kind": "web", "health": "http://toolkit-docs:8080/",
             "repository": "toolkit", "checks": ["links"], "actionsChecks": ["pages"],
             "environments": {"staging": {}}})
        modelled = [r for r in rules.desired_rules(value) if r.repo == "toolkit"]
        self.assertEqual(len(modelled), 1)
        r = modelled[0]
        # The library releases (owner-tag): the owner merges main and creates its numeric tags.
        self.assertEqual((r.model, r.owner_tags, r.tag_pattern, r.tag_creators, r.main_mergers),
                         ("release", True, "[0-9]*", ("artur-rios",), ("artur-rios",)))
        # Both jobs' statuses are required: the deciding entry's checks first, then the others'.
        self.assertEqual(r.develop_checks, ("branch-policy", "build", "links", "pages"))
        self.assertEqual(r.main_checks, ("branch-policy", "build", "links"))
        self.assertEqual(r.actions_checks, frozenset({"pages"}))
        # The repository keeps its place among the repositories entries.
        self.assertEqual([r.repo for r in rules.desired_rules(value)],
                         ["shop-api", "storefront", "toolkit", "scratch", "notes"])

    def test_given_an_application_and_its_docs_application_when_modelled_then_it_is_one_release_model_rule_set(self):
        value = copy.deepcopy(CATALOG)
        value["systems"][0]["applications"].append(
            {"id": "shop-api-docs", "name": "Docs", "kind": "web", "health": "http://shop-api-docs:8080/",
             "repository": "shop-api", "checks": ["links"], "environments": {"staging": {}}})
        modelled = [r for r in rules.desired_rules(value) if r.repo == "shop-api"]
        self.assertEqual(len(modelled), 1)
        r = modelled[0]
        self.assertEqual((r.model, r.owner_tags, r.tag_pattern, r.tag_creators, r.main_mergers),
                         ("release", False, "v*", ("jenkins-bot",), ("jenkins-bot",)))
        self.assertEqual(r.main_checks, ("branch-policy", "test", "docker", "links", "deploy/production"))
        self.assertEqual(r.develop_checks, ("branch-policy", "test", "docker", "links", "lint"))

    def test_given_a_docs_application_on_a_repository_entry_without_releases_when_modelled_then_the_entry_decides(self):
        # Nothing releases: the repositories entry supplies the model (basic here), not the application.
        value = copy.deepcopy(CATALOG)
        value["systems"][0]["applications"].append(
            {"id": "scratch-docs", "name": "Docs", "kind": "web", "health": "http://scratch-docs:8080/",
             "repository": "scratch", "checks": ["links"], "environments": {"staging": {}}})
        (r,) = [r for r in rules.desired_rules(value) if r.repo == "scratch"]
        self.assertEqual((r.model, r.owner_tags, r.main_mergers), ("basic", True, ()))
        # With the release model the entry's owner merges main, and both entries' checks are required.
        value["repositories"][1]["rules"] = "release"
        (r,) = [r for r in rules.desired_rules(value) if r.repo == "scratch"]
        self.assertEqual((r.model, r.main_mergers, r.main_checks), ("release", ("artur-rios",),
                                                                    ("branch-policy", "ignored", "links")))

    def test_given_two_applications_without_releases_when_modelled_then_the_first_one_decides(self):
        value = copy.deepcopy(CATALOG)
        value["systems"][0]["applications"].append(
            {"id": "storefront-docs", "name": "Docs", "kind": "web", "health": "http://storefront-docs:8080/",
             "repository": "storefront", "checks": ["links"], "environments": {"dev": {}}})
        (r,) = [r for r in rules.desired_rules(value) if r.repo == "storefront"]
        self.assertEqual((r.model, r.main_mergers, r.main_checks), ("release", ("jenkins-bot",),
                                                                    ("branch-policy", "links")))

    def test_given_the_repository_catalog_when_modelled_then_every_entry_is_valid(self):
        import catalog
        modelled = rules.desired_rules(catalog.load())
        self.assertTrue(all(r.model in ("release", "basic") for r in modelled))


def ruleset(payloads, name):
    return next(p for p in payloads if p["name"] == name)


def required(payload):
    """The required_status_checks entries of a ruleset payload."""
    rule = next(r for r in payload["rules"] if r["type"] == "required_status_checks")
    return rule["parameters"]["required_status_checks"]


class FakeGitHub:
    """Stands in for rules.Api: records every call and keeps the repository's rulesets, so applying
    twice shows what the second run does with what the first one left."""

    def __init__(self, existing=()):
        self.calls = []
        self.rulesets = {i + 1: {"name": name} for i, name in enumerate(existing)}
        self.next_id = len(self.rulesets) + 1

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/rulesets" in path:
            return [{"id": i, "name": r["name"]} for i, r in self.rulesets.items()]
        if method == "POST":
            self.rulesets[self.next_id] = {"name": body["name"]}
            self.next_id += 1
        if method == "DELETE":
            del self.rulesets[int(path.rsplit("/", 1)[1])]
        return None

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class GitHubRulesetsTests(unittest.TestCase):
    def test_given_jenkins_checks_when_github_rulesets_are_built_then_they_are_not_bound_to_an_app(self):
        payloads = rules.github_rulesets(by_repo()["shop-api"])
        for name in ("develop", "main"):
            for check in required(ruleset(payloads, name)):
                if check["context"] != "lint":
                    self.assertNotIn("integration_id", check, check)
        self.assertEqual([c["context"] for c in required(ruleset(payloads, "main"))],
                         ["branch-policy", "test", "docker", "deploy/production"])

    def test_given_actions_checks_when_github_rulesets_are_built_then_they_are_bound_to_github_actions(self):
        payloads = rules.github_rulesets(by_repo()["shop-api"])
        self.assertEqual(required(ruleset(payloads, "develop")),
                         [{"context": "branch-policy"}, {"context": "test"}, {"context": "docker"},
                          {"context": "lint", "integration_id": 15368}])
        self.assertNotIn("lint", [c["context"] for c in required(ruleset(payloads, "main"))])

    def test_given_an_existing_ruleset_when_applied_then_it_is_updated_in_place(self):
        api = FakeGitHub(existing=["develop", "main", "release tags"])
        rules.apply_github(api, "artur-rios", by_repo()["shop-api"], dry_run=False)
        sent = {(m, p) for m, p, _ in api.writes()}
        self.assertIn(("PUT", "/repos/artur-rios/shop-api/rulesets/1"), sent)
        self.assertIn(("PUT", "/repos/artur-rios/shop-api/rulesets/3"), sent)
        self.assertNotIn("POST", {m for m, _, _ in api.writes()})

    def test_given_rules_applied_twice_when_nothing_changed_then_no_ruleset_is_created_again(self):
        api = FakeGitHub()
        r = by_repo()["shop-api"]
        rules.apply_github(api, "artur-rios", r, dry_run=False)
        self.assertEqual([m for m, _, _ in api.writes()], ["PATCH", "POST", "POST", "POST"])
        api.calls.clear()
        rules.apply_github(api, "artur-rios", r, dry_run=False)
        self.assertEqual([m for m, _, _ in api.writes()], ["PATCH", "PUT", "PUT", "PUT"])
        self.assertEqual(len(api.rulesets), 3)

    def test_given_the_repository_settings_when_applied_then_develop_is_default_and_branches_are_deleted(self):
        api = FakeGitHub()
        rules.apply_github(api, "artur-rios", by_repo()["shop-api"], dry_run=False)
        self.assertEqual(api.writes()[0],
                         ("PATCH", "/repos/artur-rios/shop-api",
                          {"default_branch": "develop", "delete_branch_on_merge": True}))

    def test_given_the_old_default_ruleset_when_applied_then_it_is_removed(self):
        api = FakeGitHub(existing=["default", "develop"])
        lines = rules.apply_github(api, "artur-rios", by_repo()["shop-api"], dry_run=False)
        self.assertIn(("DELETE", "/repos/artur-rios/shop-api/rulesets/1", None), api.writes())
        self.assertNotIn("default", [r["name"] for r in api.rulesets.values()])
        self.assertTrue(any("removed ruleset 'default'" in line for line in lines))

    def test_given_any_release_repository_when_built_then_nobody_bypasses_the_move_and_delete_rule(self):
        # A version names one commit forever, on GitHub the record a moved Forgejo tag is repaired from: no
        # bypass at all, administrators (the owner) included. To fix a wrong tag the owner edits the ruleset.
        for repo, pattern in (("shop-api", "v*"), ("toolkit", "[0-9]*"), ("notes", "v*")):
            tags = ruleset(rules.github_rulesets(by_repo()[repo]), "release tags")
            self.assertEqual(tags["target"], "tag", repo)
            self.assertEqual(tags["conditions"]["ref_name"]["include"], [f"refs/tags/{pattern}"], repo)
            self.assertEqual([r["type"] for r in tags["rules"]], ["update", "deletion"], repo)
            self.assertEqual(tags["bypass_actors"], [], repo)

    def test_given_an_owner_tag_repository_when_built_then_only_admins_create_its_tags(self):
        # Creation is a ruleset of its own, so that its administrator bypass (the owner creates the tags)
        # is not also a bypass of the move and delete rule.
        creation = ruleset(rules.github_rulesets(by_repo()["toolkit"]), "release tag creation")
        self.assertEqual((creation["target"], creation["conditions"]["ref_name"]["include"]), ("tag", ["refs/tags/[0-9]*"]))
        self.assertEqual([r["type"] for r in creation["rules"]], ["creation"])
        self.assertEqual(creation["bypass_actors"], rules.GITHUB_ADMIN_BYPASS)

    def test_given_an_application_when_built_then_its_tags_may_still_be_created_by_jenkins(self):
        # Jenkins creates v* tags itself, so no `creation` rule: it would lock Jenkins out.
        payloads = rules.github_rulesets(by_repo()["shop-api"])
        self.assertEqual([p["name"] for p in payloads], ["develop", "main", "release tags"])
        tags = ruleset(payloads, "release tags")
        self.assertEqual(tags["conditions"]["ref_name"]["include"], ["refs/tags/v*"])
        self.assertEqual([r["type"] for r in tags["rules"]], ["update", "deletion"])

    def test_given_an_owner_tag_repository_when_applied_twice_then_its_four_rulesets_are_updated_in_place(self):
        api = FakeGitHub(existing=["develop", "main", "release tags"])
        r = by_repo()["toolkit"]
        rules.apply_github(api, "artur-rios", r, dry_run=False)
        # The existing `release tags` (which had the creation rule and the bypass) is updated in place;
        # `release tag creation` is new, and written first: a run that fails between the two never leaves
        # the creation of the tags unrestricted.
        self.assertEqual([(m, b["name"]) for m, _, b in api.writes() if m != "PATCH"],
                         [("PUT", "develop"), ("PUT", "main"), ("POST", "release tag creation"),
                          ("PUT", "release tags")])
        api.calls.clear()
        rules.apply_github(api, "artur-rios", r, dry_run=False)
        self.assertEqual([m for m, _, _ in api.writes()], ["PATCH", "PUT", "PUT", "PUT", "PUT"])
        self.assertEqual(len(api.rulesets), 4)

    def test_given_an_existing_release_tags_ruleset_when_applied_then_its_put_drops_the_admin_bypass(self):
        # The update in place is what removes the bypass from an installation's existing ruleset: PUT replaces
        # the whole ruleset, so the body must carry the empty list (an absent field would keep the old one).
        for repo in ("shop-api", "toolkit"):
            api = FakeGitHub(existing=["develop", "main", "release tags"])
            rules.apply_github(api, "artur-rios", by_repo()[repo], dry_run=False)
            (body,) = [b for m, p, b in api.writes() if m == "PUT" and p.endswith("/rulesets/3")]
            self.assertEqual(body["name"], "release tags", repo)
            self.assertIn("bypass_actors", body, repo)
            self.assertEqual(body["bypass_actors"], [], repo)
            self.assertEqual([r["type"] for r in body["rules"]], ["update", "deletion"], repo)

    def test_given_an_application_with_a_creation_ruleset_when_applied_then_it_is_noted_not_removed(self):
        # A repository that was owner-tagged before it became an application: Jenkins creates its tags now,
        # which the old creation ruleset blocks; deleting it is still the owner's call.
        api = FakeGitHub(existing=["develop", "main", "release tags", "release tag creation"])
        lines = rules.apply_github(api, "artur-rios", by_repo()["shop-api"], dry_run=False)
        self.assertNotIn("DELETE", [m for m, _, _ in api.writes()])
        notes = [line for line in lines if "note" in line]
        self.assertEqual(len(notes), 1, lines)
        self.assertIn("ruleset 'release tag creation'", notes[0])

    def test_given_jenkins_acting_as_the_owner_when_built_then_an_applications_tags_are_still_not_locked(self):
        # Whose tags they are is the model's, not a comparison of user names: with forge.jenkinsUser
        # the owner itself, an application's tags are still created by Jenkins, which a `creation`
        # rule would lock out.
        value = copy.deepcopy(CATALOG_GITHUB)
        value["forge"] = {"type": "github", "jenkinsUser": "artur-rios"}
        r = by_repo(value)["shop-api"]
        self.assertEqual(r.tag_creators, r.release_pushers)
        tags = ruleset(rules.github_rulesets(r), "release tags")
        self.assertEqual([rule["type"] for rule in tags["rules"]], ["update", "deletion"])

    def test_given_a_github_app_as_the_jenkins_user_when_built_then_the_rulesets_are_the_same(self):
        # The bot login is only a name to compare statuses with: GitHub rulesets never list who merges.
        value = copy.deepcopy(CATALOG_GITHUB)
        value["forge"] = {"type": "github", "jenkinsUser": "my-app[bot]"}
        self.assertEqual(rules.github_rulesets(by_repo(value)["shop-api"]),
                         rules.github_rulesets(by_repo()["shop-api"]))

    def test_given_a_basic_repository_when_built_then_only_deletion_and_force_push_are_blocked(self):
        payloads = rules.github_rulesets(by_repo()["scratch"])
        self.assertEqual([p["name"] for p in payloads], ["develop", "main"])
        for payload in payloads:
            self.assertEqual([r["type"] for r in payload["rules"]], ["deletion", "non_fast_forward"])

    def test_given_a_basic_repository_when_applied_then_no_tags_ruleset_is_sent(self):
        api = FakeGitHub()
        rules.apply_github(api, "artur-rios", by_repo()["scratch"], dry_run=False)
        self.assertEqual([b["name"] for m, _, b in api.writes() if m == "POST"], ["develop", "main"])

    def test_given_a_basic_repository_with_a_release_tags_ruleset_when_applied_then_it_is_noted_not_removed(self):
        # A repository that was released before it was turned `rules: basic`: its tag ruleset is no
        # longer the model's, but deleting it is the owner's call.
        api = FakeGitHub(existing=["develop", "main", "release tags"])
        lines = rules.apply_github(api, "artur-rios", by_repo()["scratch"], dry_run=False)
        self.assertNotIn("DELETE", [m for m, _, _ in api.writes()])
        self.assertIn("release tags", [r["name"] for r in api.rulesets.values()])
        notes = [line for line in lines if "note" in line]
        self.assertEqual(len(notes), 1, lines)
        self.assertIn("ruleset 'release tags'", notes[0])
        self.assertIn("left as it is", notes[0])

    def test_given_a_basic_repository_with_both_tag_rulesets_when_applied_then_both_are_noted(self):
        api = FakeGitHub(existing=["develop", "main", "release tags", "release tag creation"])
        lines = rules.apply_github(api, "artur-rios", by_repo()["scratch"], dry_run=False)
        self.assertNotIn("DELETE", [m for m, _, _ in api.writes()])
        notes = [line for line in lines if "note" in line]
        self.assertEqual(len(notes), 2, lines)
        self.assertIn("ruleset 'release tag creation'", notes[0])
        self.assertIn("ruleset 'release tags'", notes[1])

    def test_given_an_application_when_applied_then_its_release_tags_ruleset_is_not_noted(self):
        api = FakeGitHub(existing=["develop", "main", "release tags"])
        lines = rules.apply_github(api, "artur-rios", by_repo()["shop-api"], dry_run=False)
        self.assertEqual([line for line in lines if "note" in line], [])

    def test_given_protected_files_when_built_then_github_gets_no_file_rule(self):
        # GitHub cannot restrict who merges a file; Jenkins' trust rule covers the Jenkinsfile there.
        text = json.dumps(rules.github_rulesets(by_repo()["shop-api"]))
        self.assertNotIn("Jenkinsfile", text)

    def test_given_dry_run_when_applied_then_no_write_is_sent_and_no_token_is_printed(self):
        api = FakeGitHub(existing=["main", "default"])
        lines = rules.apply_github(api, "artur-rios", by_repo()["shop-api"], dry_run=True)
        self.assertEqual(api.writes(), [])
        self.assertTrue(all(m == "GET" for m, _, _ in api.calls))
        self.assertTrue(any("would" in line for line in lines))
        self.assertTrue(any('"required_status_checks"' in line for line in lines))

    def test_given_dry_run_on_the_command_line_when_run_then_the_token_is_never_printed(self):
        fake = FakeGitHub()
        made = []

        def make_api(base, token, scheme, headers=None):
            made.append((base, scheme))
            return fake

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rules.catalog_module, "load", return_value=CATALOG_GITHUB), \
                mock.patch.dict(os.environ, {"GH_TOKEN": "s3cr3t-token"}), \
                mock.patch.object(rules, "Api", make_api), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rules.main(["--dry-run", "shop-api"])
        self.assertEqual(made, [("https://api.github.com", "Bearer")])
        self.assertNotIn("s3cr3t-token", out.getvalue() + err.getvalue())
        self.assertIn("artur-rios/shop-api", out.getvalue())
        self.assertEqual(fake.writes(), [])

    def test_given_a_repository_not_in_the_catalog_when_run_then_it_is_refused_by_name(self):
        with mock.patch.object(rules.catalog_module, "load", return_value=CATALOG_GITHUB), \
                mock.patch.object(rules, "Api", side_effect=AssertionError("no API call expected")):
            with self.assertRaises(SystemExit) as raised:
                rules.main(["--dry-run", "shop-api", "ghost", "phantom"])
        self.assertIn("ghost, phantom", str(raised.exception))

    def test_given_todays_catalog_when_built_then_only_the_binding_and_the_tag_bypass_changed(self):
        # The fixture is what github/rulesets.py built before it became a shim, for a catalog of two
        # applications: shop-api (checks test and docker, a production release environment) and
        # storefront (neither). The differences: Jenkins' contexts no longer carry integration_id, and
        # nobody bypasses `release tags` any more (administrators could move and delete tags before).
        fixture = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                              "github-rulesets-before-plan-2.json").read_text(encoding="utf-8"))
        catalog = copy.deepcopy(CATALOG_GITHUB)
        del catalog["repositories"]
        catalog["systems"][0]["applications"][0]["actionsChecks"] = []
        built = {r.repo: rules.github_rulesets(r) for r in rules.desired_rules(catalog)}
        self.assertEqual(sorted(built), sorted(fixture))
        for repo, old in fixture.items():
            for payload in old:
                if payload["name"] == "release tags":
                    self.assertEqual(payload["bypass_actors"], rules.GITHUB_ADMIN_BYPASS)
                    payload["bypass_actors"] = []
                for rule in payload["rules"]:
                    if rule["type"] == "required_status_checks":
                        for check in rule["parameters"]["required_status_checks"]:
                            check.pop("integration_id", None)
            self.assertEqual(built[repo], old, repo)


OWNER = "artur-rios"
SECRET = "wh-s3cret-value"
HOOK_URL = "https://jenkins.example.com/gitea-webhook/post"


# What Forgejo 16 answers for a hook created with the event "pull_request": the whole group, spelled out
# (seen on the real server, test_forgejo_live.py).
PULL_REQUEST_EVENTS = ["pull_request", "pull_request_assign", "pull_request_label", "pull_request_milestone",
                       "pull_request_comment", "pull_request_review_approved", "pull_request_review_rejected",
                       "pull_request_review_comment", "pull_request_sync", "pull_request_review_request"]


class FakeForgejo:
    """Stands in for rules.Api: records every call and keeps one repository's settings, branch and tag
    protections and hooks like a Forgejo server would -- with the extra fields it answers, whitelists
    in another order, no hook secret, "pull_request" answered as its whole group of events, and users
    without write access (not in `writers`, when given) left off the whitelists -- so applying twice
    shows what the second run does."""

    def __init__(self, hooks=(), protections=(), tags=(), writers=None):
        self.calls = []
        self.writers = writers
        self.repo = {"id": 1, "name": "shop-api", "default_branch": "main", "private": True,
                     "default_delete_branch_after_merge": False, "allow_merge_commits": True,
                     "allow_squash_merge": True, "allow_rebase": True, "allow_rebase_explicit": True,
                     "default_merge_style": "squash"}
        self.protections = {p["rule_name"]: dict(p) for p in protections}
        self.tags = {i + 1: {"id": i + 1, **t} for i, t in enumerate(tags)}
        self.hooks = {h["id"]: dict(h) for h in hooks}
        self.secrets = {}  # hook id -> the secret last sent (never returned)
        self.next_id = 100

    @staticmethod
    def _out(value):
        """What the server answers: lists come back in another order."""
        if isinstance(value, dict):
            return {k: FakeForgejo._out(v) for k, v in value.items()}
        return list(reversed(value)) if isinstance(value, list) else value

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        segments = path.split("/")  # ["", "repos", owner, repo, ...]
        assert segments[:3] == ["", "repos", OWNER], path
        parts = [urllib.parse.unquote(p) for p in segments[4:]]
        if not parts:
            if method == "PATCH":
                self.repo.update(body)
            return self._out(self.repo)
        kind, key = parts[0], (parts[1] if len(parts) > 1 else None)
        store = {"branch_protections": self.protections, "tag_protections": self.tags, "hooks": self.hooks}[kind]
        if method == "GET":
            return self._out(list(store.values()))
        if method == "POST":
            ident = body["rule_name"] if kind == "branch_protections" else self.next_id
            self.next_id += 1
            entry = {"id": ident, **copy.deepcopy(body)}
            if kind == "hooks":
                self.secrets[ident] = entry["config"].pop("secret")
            store[ident] = self._stored(entry)
            return self._out(entry)
        ident = key if kind == "branch_protections" else int(key)
        entry = store[ident]  # a PATCH of something that is not there is a KeyError: the test fails
        for field, value in body.items():
            if kind == "hooks" and field == "config":
                value = dict(value)
                self.secrets[ident] = value.pop("secret")
                entry["config"] = {**entry["config"], **value}
            else:
                entry[field] = value
        self._stored(entry)
        return self._out(entry)

    def _stored(self, entry):
        """The entry as the server keeps it: whitelists without users lacking write access, a hook's
        "pull_request" spelled out."""
        for field in ("merge_whitelist_usernames", "push_whitelist_usernames", "whitelist_usernames"):
            if field in entry and self.writers is not None:
                entry[field] = [user for user in entry[field] if user in self.writers]
        if "pull_request" in entry.get("events", []):
            entry["events"] = [e for e in entry["events"] if e not in PULL_REQUEST_EVENTS] + PULL_REQUEST_EVENTS
        return entry

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def written(self, kind):
        """The bodies POSTed or PATCHed under one collection (branch_protections, tag_protections, hooks)."""
        return [(m, p, b) for m, p, b in self.writes() if f"/{kind}" in p]


def forgejo_apply(api, repo, webhook=None, dry_run=False):
    return rules.apply_forgejo(api, OWNER, by_repo()[repo], webhook, dry_run)


def protection(api, name):
    return api.protections[name]


class ForgejoRulesTests(unittest.TestCase):
    def test_given_the_repository_settings_when_applied_then_develop_is_default_and_merges_are_set(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        self.assertEqual(api.writes()[0], ("PATCH", f"/repos/{OWNER}/shop-api", {
            "default_branch": "develop", "default_delete_branch_after_merge": True, "allow_merge_commits": True,
            "allow_squash_merge": True, "allow_rebase": False, "allow_rebase_explicit": False,
            "default_merge_style": "merge"}))

    def test_given_an_application_when_applied_on_forgejo_then_main_is_merged_only_by_jenkins(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        main = protection(api, "main")
        self.assertEqual((main["enable_push"], main["enable_merge_whitelist"], main["merge_whitelist_usernames"]),
                         (False, True, ["jenkins-bot"]))
        self.assertEqual(main["status_check_contexts"], ["branch-policy", "test", "docker", "deploy/production"])
        self.assertEqual((main["enable_status_check"], main["required_approvals"]), (True, 0))

    def test_given_develop_when_applied_then_its_checks_include_the_actions_checks(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        develop = protection(api, "develop")
        self.assertEqual(develop["status_check_contexts"], ["branch-policy", "test", "docker", "lint"])
        self.assertEqual((develop["enable_push"], develop["merge_whitelist_usernames"]),
                         (False, ["artur-rios", "alice", "bob"]))

    def test_given_any_release_model_repository_when_applied_then_its_pipeline_files_are_protected_on_develop_only(self):
        # yggdrasil.yml says what Jenkins runs (images, commands, which credentials a check binds): only the
        # owner merges a change to it. main only receives snapshots of develop, merged by Jenkins: a
        # protected file there would stop it from merging any release that changes them.
        for r in rules.desired_rules(CATALOG):
            if r.model != "release":
                continue
            api = FakeForgejo()
            rules.apply_forgejo(api, OWNER, r, None, False)
            self.assertEqual(protection(api, "develop")["protected_file_patterns"], "Jenkinsfile;yggdrasil.yml", r.repo)
            self.assertEqual(protection(api, "main")["protected_file_patterns"], "", r.repo)

    def test_given_release_branches_when_applied_then_only_the_owner_may_push_them(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        release = protection(api, "release/*")
        self.assertEqual((release["enable_push"], release["enable_push_whitelist"], release["push_whitelist_usernames"],
                          release["enable_merge_whitelist"]), (True, True, ["artur-rios"], False))

    def test_given_admins_when_applied_then_the_rules_do_not_bind_them(self):
        for repo in ("shop-api", "toolkit", "scratch"):
            api = FakeForgejo()
            forgejo_apply(api, repo)
            self.assertTrue(api.protections)
            for name, entry in api.protections.items():
                self.assertIs(entry["apply_to_admins"], False, (repo, name))

    def test_given_a_library_when_applied_then_only_the_owner_creates_its_numeric_tags(self):
        api = FakeForgejo()
        forgejo_apply(api, "toolkit")
        self.assertEqual([b for _, _, b in api.written("tag_protections")],
                         [{"name_pattern": "[0-9]*", "whitelist_usernames": ["artur-rios"]}])

    def test_given_an_application_when_applied_then_only_jenkins_creates_its_v_tags(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        self.assertEqual([b for _, _, b in api.written("tag_protections")],
                         [{"name_pattern": "v*", "whitelist_usernames": ["jenkins-bot"]}])

    def test_given_a_basic_repository_when_applied_then_no_checks_or_whitelists_are_set(self):
        api = FakeForgejo()
        lines = forgejo_apply(api, "scratch")
        self.assertEqual(sorted(api.protections), ["develop", "main"])
        for entry in api.protections.values():
            self.assertEqual({k: v for k, v in entry.items() if k not in ("id", "rule_name")},
                             {"enable_push": True, "enable_merge_whitelist": False, "enable_status_check": False,
                              "status_check_contexts": [], "protected_file_patterns": "", "apply_to_admins": False})
        self.assertEqual(api.written("tag_protections"), [])
        self.assertFalse(any("release/*" in line or "tag" in line for line in lines), lines)

    def test_given_a_release_repository_turned_basic_when_applied_then_develop_and_main_lose_their_rules(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        api.calls.clear()
        basic = dataclasses.replace(by_repo()["scratch"], repo="shop-api")
        lines = rules.apply_forgejo(api, OWNER, basic, None, False)
        self.assertEqual(sorted((m, p) for m, p, _ in api.writes()),
                         [("PATCH", f"/repos/{OWNER}/shop-api/branch_protections/develop"),
                          ("PATCH", f"/repos/{OWNER}/shop-api/branch_protections/main")])
        for name in ("develop", "main"):
            entry = protection(api, name)
            self.assertEqual((entry["enable_push"], entry["enable_merge_whitelist"], entry["enable_status_check"],
                              entry["status_check_contexts"], entry["protected_file_patterns"]),
                             (True, False, False, [], ""), name)
        # What the basic model has no place for is left, and said so: deleting it is the owner's call.
        self.assertIn("release/*", api.protections)
        self.assertEqual(len(api.tags), 1)
        notes = [line for line in lines if "note" in line]
        self.assertEqual(len(notes), 2, lines)
        self.assertIn("branch protection 'release/*'", notes[0])
        self.assertIn("tag protection 'v*'", notes[1])
        api.calls.clear()
        self.assertEqual([line for line in rules.apply_forgejo(api, OWNER, basic, None, False) if "note" in line],
                         notes)
        self.assertEqual(api.writes(), [])

    def test_given_a_tag_pattern_that_changed_when_applied_then_the_new_one_is_protected_and_the_old_noted(self):
        api = FakeForgejo()
        forgejo_apply(api, "toolkit")
        api.calls.clear()
        renamed = dataclasses.replace(by_repo()["toolkit"], tag_pattern="v*")
        lines = rules.apply_forgejo(api, OWNER, renamed, None, False)
        self.assertEqual([b for m, _, b in api.written("tag_protections")],
                         [{"name_pattern": "v*", "whitelist_usernames": ["artur-rios"]}])
        self.assertEqual(sorted(t["name_pattern"] for t in api.tags.values()), ["[0-9]*", "v*"])
        notes = [line for line in lines if "note" in line]
        self.assertEqual(len(notes), 1, lines)
        self.assertIn("tag protection '[0-9]*'", notes[0])
        self.assertIn("left as it is", notes[0])

    def test_given_protections_the_model_never_had_when_applied_then_they_are_not_noted(self):
        # Only what the model itself could have left behind is noted; the owner's own rules are theirs.
        api = FakeForgejo(protections=[{"rule_name": "feature/*", "enable_push": True}])
        lines = forgejo_apply(api, "shop-api")
        self.assertEqual([line for line in lines if "note" in line], [])

    def test_given_rules_applied_twice_when_nothing_changed_then_the_second_run_sends_no_write(self):
        webhook = {"url": HOOK_URL, "secret": SECRET}
        for repo in ("shop-api", "toolkit", "scratch"):
            api = FakeForgejo()
            forgejo_apply(api, repo, webhook)
            self.assertGreater(len(api.writes()), 0)
            api.calls.clear()
            forgejo_apply(api, repo, webhook)
            self.assertEqual(api.writes(), [], repo)

    def test_given_a_protection_that_differs_when_applied_then_only_that_one_is_patched_by_name(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api")
        api.protections["release/*"]["push_whitelist_usernames"] = ["someone-else"]
        api.protections["develop"]["merge_whitelist_usernames"] = ["bob", "alice", "artur-rios"]  # same set
        api.calls.clear()
        forgejo_apply(api, "shop-api")
        self.assertEqual([(m, p) for m, p, _ in api.writes()],
                         [("PATCH", f"/repos/{OWNER}/shop-api/branch_protections/release%2F%2A")])
        self.assertEqual(protection(api, "release/*")["push_whitelist_usernames"], ["artur-rios"])

    def test_given_an_existing_webhook_for_jenkins_when_applied_then_it_is_updated_not_duplicated(self):
        old = {"id": 7, "type": "gitea", "active": False, "events": ["push"], "branch_filter": "main",
               "config": {"url": HOOK_URL, "content_type": "form"}}
        other = {"id": 8, "type": "gitea", "active": True, "events": ["push"], "branch_filter": "*",
                 "config": {"url": "https://elsewhere.example.com/hook", "content_type": "json"}}
        api = FakeForgejo(hooks=[old, other])
        forgejo_apply(api, "shop-api", {"url": HOOK_URL, "secret": SECRET})
        self.assertEqual([(m, p) for m, p, _ in api.written("hooks")], [("PATCH", f"/repos/{OWNER}/shop-api/hooks/7")])
        self.assertEqual(sorted(api.hooks), [7, 8])
        hook = api.hooks[7]
        self.assertEqual((hook["active"], set(hook["events"]), hook["branch_filter"], hook["config"]["content_type"]),
                         (True, {"create", "delete", "push", *PULL_REQUEST_EVENTS}, "*", "json"))
        self.assertEqual(api.secrets[7], SECRET)
        self.assertEqual(api.hooks[8]["events"], ["push"])

    def test_given_no_webhook_yet_when_applied_then_one_is_created_for_jenkins(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api", {"url": HOOK_URL, "secret": SECRET})
        (method, path, body), = api.written("hooks")
        self.assertEqual((method, path), ("POST", f"/repos/{OWNER}/shop-api/hooks"))
        self.assertEqual(body, {"type": "gitea", "active": True, "events": ["create", "delete", "push", "pull_request"],
                                "branch_filter": "*",
                                "config": {"url": HOOK_URL, "content_type": "json", "secret": SECRET}})

    def test_given_a_webhook_with_the_right_fields_when_applied_then_it_is_left_alone(self):
        # Forgejo never returns the secret, so a changed one cannot be seen: the hook has to be deleted.
        hook = {"id": 7, "type": "gitea", "active": True, "branch_filter": "*",
                "events": ["push", "create", "delete", *PULL_REQUEST_EVENTS],
                "config": {"url": HOOK_URL, "content_type": "json"}}
        api = FakeForgejo(hooks=[hook])
        lines = forgejo_apply(api, "shop-api", {"url": HOOK_URL, "secret": "a-new-secret"})
        self.assertEqual(api.written("hooks"), [])
        self.assertTrue(any("delete the hook" in line for line in lines), lines)

    def test_given_a_webhook_with_only_some_pull_request_events_when_applied_then_it_is_updated(self):
        # A sub-event alone is not the group Jenkins needs: the hook is rewritten with "pull_request".
        hook = {"id": 7, "type": "gitea", "active": True, "branch_filter": "*",
                "events": ["push", "create", "delete", "pull_request_sync"],
                "config": {"url": HOOK_URL, "content_type": "json"}}
        api = FakeForgejo(hooks=[hook])
        forgejo_apply(api, "shop-api", {"url": HOOK_URL, "secret": SECRET})
        self.assertEqual([(m, p) for m, p, _ in api.written("hooks")], [("PATCH", f"/repos/{OWNER}/shop-api/hooks/7")])
        self.assertIn("pull_request", api.hooks[7]["events"])

    def test_given_a_developer_without_write_access_when_applied_then_the_run_names_them(self):
        # Forgejo leaves such a user off the whitelist without an error (seen on the real server).
        api = FakeForgejo(writers={"artur-rios", "alice", "jenkins-bot"})
        lines = forgejo_apply(api, "shop-api")
        self.assertEqual(protection(api, "develop")["merge_whitelist_usernames"], ["artur-rios", "alice"])
        warnings = [line for line in lines if "warning" in line]
        self.assertEqual(warnings, ["  warning: Forgejo left bob off who may merge (branch 'develop'): "
                                    "give them write access to the repository and run again"])

    def test_given_jenkins_without_write_access_when_applied_then_the_tag_protection_names_it(self):
        api = FakeForgejo(writers={"artur-rios", "alice", "bob"})
        lines = forgejo_apply(api, "shop-api")
        warnings = [line for line in lines if "warning" in line]
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("jenkins-bot off who may merge (branch 'main')", warnings[0])
        self.assertIn("jenkins-bot off who may create tags (tags 'v*')", warnings[1])

    def test_given_no_webhook_when_applied_then_hooks_are_not_touched(self):
        api = FakeForgejo()
        forgejo_apply(api, "shop-api", None)
        self.assertFalse([c for c in api.calls if "/hooks" in c[1]])

    def test_given_a_webhook_secret_when_dry_run_then_it_is_not_printed(self):
        api = FakeForgejo()
        lines = forgejo_apply(api, "shop-api", {"url": HOOK_URL, "secret": SECRET}, dry_run=True)
        self.assertEqual(api.writes(), [])
        text = "\n".join(lines)
        self.assertNotIn(SECRET, text)
        self.assertIn('"secret": "***"', text)
        self.assertIn(HOOK_URL, text)
        self.assertIn("would", text)

    def test_given_a_webhook_secret_when_applied_then_no_line_shows_it(self):
        api = FakeForgejo()
        lines = forgejo_apply(api, "shop-api", {"url": HOOK_URL, "secret": SECRET})
        self.assertNotIn(SECRET, "\n".join(lines))
        self.assertEqual(list(api.secrets.values()), [SECRET])  # sent to the server, shown nowhere

    def test_given_dry_run_when_applied_then_nothing_is_written_and_what_would_be_is_listed(self):
        api = FakeForgejo()
        lines = forgejo_apply(api, "shop-api", dry_run=True)
        self.assertEqual(api.writes(), [])
        self.assertTrue(all(m == "GET" for m, _, _ in api.calls))
        text = "\n".join(lines)
        for expected in ("release/*", "develop", "main", "protect tags", "v*"):
            self.assertIn(expected, text)


class ForgejoCommandTests(unittest.TestCase):
    ENV = {"FORGE_TOKEN": "fg-tok-123"}

    def run_main(self, args, env, catalog=CATALOG, fake=None):
        """Runs the command with the environment given (and nothing of the real one), the API faked."""
        fake = fake or FakeForgejo()
        made = []

        def make_api(base, token, scheme, headers=None):
            made.append((base, token, scheme))
            return fake

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rules.catalog_module, "load", return_value=catalog), \
                mock.patch.dict(os.environ, env, clear=True), mock.patch.object(rules, "Api", make_api), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                rules.main(args)
                exit_ = None
            except SystemExit as raised:
                exit_ = raised
        return fake, made, out.getvalue(), err.getvalue(), exit_

    def test_given_a_forgejo_catalog_when_run_then_the_forgejo_api_is_used_with_the_token_scheme(self):
        fake, made, out, err, exit_ = self.run_main(["--dry-run", "shop-api"], self.ENV)
        self.assertIsNone(exit_)
        self.assertEqual(made, [("https://git.example.com/api/v1", "fg-tok-123", "token")])
        self.assertIn("artur-rios/shop-api", out)
        self.assertEqual(fake.writes(), [])
        self.assertNotIn("fg-tok-123", out + err)

    def test_given_no_forge_token_when_run_then_it_is_refused_before_any_request(self):
        fake, made, out, err, exit_ = self.run_main(["--dry-run"], {})
        self.assertIn("FORGE_TOKEN", str(exit_))
        self.assertEqual((made, fake.calls), ([], []))

    def test_given_neither_jenkins_url_nor_secret_when_run_then_webhooks_are_skipped_and_it_says_so(self):
        fake, _, out, _, exit_ = self.run_main(["shop-api"], self.ENV)
        self.assertIsNone(exit_)
        self.assertIn("webhook", out.lower())
        self.assertIn("skipped", out)
        self.assertFalse([c for c in fake.calls if "/hooks" in c[1]])

    def test_given_jenkins_url_without_a_secret_when_run_then_it_is_refused(self):
        env = {**self.ENV, "FORGE_JENKINS_URL": "https://jenkins.example.com"}
        fake, made, _, _, exit_ = self.run_main(["--dry-run"], env)
        self.assertIn("FORGE_WEBHOOK_SECRET", str(exit_))
        self.assertEqual((made, fake.calls), ([], []))

    def test_given_only_the_jenkins_url_jenkins_sets_in_every_build_when_run_then_it_is_ignored(self):
        # Jenkins sets JENKINS_URL in every build: it must neither ask for a secret nor add a webhook.
        env = {**self.ENV, "JENKINS_URL": "https://jenkins.example.com/"}
        fake, _, out, _, exit_ = self.run_main(["shop-api"], env)
        self.assertIsNone(exit_)
        self.assertIn("webhooks skipped", out)
        self.assertFalse([c for c in fake.calls if "/hooks" in c[1]])

    def test_given_a_secret_without_jenkins_url_when_run_then_it_is_refused_without_printing_it(self):
        env = {**self.ENV, "FORGE_WEBHOOK_SECRET": SECRET}
        fake, made, out, err, exit_ = self.run_main(["--dry-run"], env)
        self.assertIn("FORGE_JENKINS_URL", str(exit_))
        self.assertNotIn(SECRET, str(exit_) + out + err)
        self.assertEqual((made, fake.calls), ([], []))

    def test_given_both_when_run_then_the_hook_url_has_no_double_slash_and_the_secret_stays_hidden(self):
        env = {**self.ENV, "FORGE_JENKINS_URL": "https://jenkins.example.com/", "FORGE_WEBHOOK_SECRET": SECRET}
        fake, _, out, err, exit_ = self.run_main(["--dry-run", "shop-api"], env)
        self.assertIsNone(exit_)
        self.assertIn(HOOK_URL, out)
        self.assertNotIn(SECRET, out + err)
        env = {**env, "FORGE_JENKINS_URL": "https://jenkins.example.com/jenkins///"}
        fake, _, out, _, _ = self.run_main(["shop-api"], env)
        self.assertEqual(next(iter(fake.hooks.values()))["config"]["url"],
                         "https://jenkins.example.com/jenkins/gitea-webhook/post")

    def test_given_a_server_error_that_echoes_the_secret_when_run_then_it_is_scrubbed(self):
        class Failing(FakeForgejo):
            def request(self, method, path, body=None):
                if method == "POST" and path.endswith("/hooks"):
                    raise rules.ApiError(method, path, 422, f"bad secret {SECRET}")
                return super().request(method, path, body)

        env = {**self.ENV, "FORGE_JENKINS_URL": "https://jenkins.example.com", "FORGE_WEBHOOK_SECRET": SECRET}
        _, _, out, err, exit_ = self.run_main(["shop-api"], env, fake=Failing())
        self.assertIn("422", str(exit_))
        self.assertNotIn(SECRET, str(exit_) + out + err)
        self.assertIn("***", str(exit_))

    def test_given_help_when_run_then_the_usage_is_printed_and_nothing_is_asked_of_the_server(self):
        fake, made, out, _, exit_ = self.run_main(["--help"], {})
        self.assertEqual(exit_.code, 0)
        self.assertIn("usage:", out)
        for word in ("--dry-run", "FORGE_TOKEN", "FORGE_JENKINS_URL", "FORGE_WEBHOOK_SECRET", "GH_TOKEN", "mirror tags"):
            self.assertIn(word, out)
        self.assertEqual((made, fake.calls), ([], []))

    def test_given_help_on_the_real_command_when_run_then_it_exits_zero(self):
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "rules.py"), "--help"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


class PerRepoForgejo:
    """FakeForgejo per repository, for a run over the whole catalog (one FakeForgejo keeps one repository)."""

    def __init__(self, make=None):
        self.make = make or FakeForgejo
        self.repos = {}

    def request(self, method, path, body=None):
        return self.repos.setdefault(path.split("/")[3], self.make()).request(method, path, body)

    def writes(self):
        return [w for fake in self.repos.values() for w in fake.writes()]


MIRROR = "artur-rios-mirror"
CATALOG_MIRROR = {**copy.deepcopy(CATALOG), "mirror": {"github": MIRROR, "releases": False}}


class MirrorTagsTests(unittest.TestCase):
    """The GitHub copies of a Forgejo installation with a `mirror`: one ruleset, `mirror tags`, so that
    nobody moves or deletes a version tag there and they stay the record to repair Forgejo's from."""

    def test_given_a_release_repository_when_built_then_its_copy_gets_one_tag_ruleset_nobody_bypasses(self):
        for repo, pattern in (("shop-api", "v*"), ("toolkit", "[0-9]*")):
            payloads = rules.github_mirror_rulesets(by_repo()[repo])
            self.assertEqual(payloads, [{
                "name": "mirror tags", "target": "tag", "enforcement": "active", "bypass_actors": [],
                "conditions": {"ref_name": {"include": [f"refs/tags/{pattern}"], "exclude": []}},
                # No creation rule: the mirror creates the tags. No branch rule: it force-pushes branches.
                "rules": [{"type": "update"}, {"type": "deletion"}]}], repo)

    def test_given_a_basic_repository_when_built_then_its_copy_gets_nothing(self):
        # A basic repository has no version tags.
        self.assertEqual(rules.github_mirror_rulesets(by_repo()["scratch"]), [])

    def test_given_a_copy_when_applied_then_only_the_ruleset_is_written_under_the_mirror_owner(self):
        api = FakeGitHub()
        lines = rules.apply_mirror(api, MIRROR, by_repo()["shop-api"], dry_run=False)
        # No settings change and no branch ruleset: the mirror owns the copy's branches.
        self.assertEqual([(m, p, b["name"]) for m, p, b in api.writes()],
                         [("POST", f"/repos/{MIRROR}/shop-api/rulesets", "mirror tags")])
        self.assertEqual(lines[0], f"{MIRROR}/shop-api (the GitHub copy)")
        self.assertIn("created ruleset 'mirror tags'", lines[1])

    def test_given_a_copy_applied_twice_when_nothing_changed_then_the_ruleset_is_updated_in_place(self):
        api = FakeGitHub(existing=["something of the owner's"])
        r = by_repo()["toolkit"]
        rules.apply_mirror(api, MIRROR, r, dry_run=False)
        api.calls.clear()
        rules.apply_mirror(api, MIRROR, r, dry_run=False)
        self.assertEqual([(m, p) for m, p, _ in api.writes()], [("PUT", f"/repos/{MIRROR}/toolkit/rulesets/2")])
        self.assertEqual(sorted(r["name"] for r in api.rulesets.values()), ["mirror tags", "something of the owner's"])

    def test_given_a_basic_repository_when_applied_then_github_is_not_even_asked(self):
        # Nothing to apply, so no request at all: a basic repository's copy may not exist, or be one GitHub
        # refuses rulesets on, and that must cost nothing.
        api = FakeGitHub(existing=["mirror tags"])
        self.assertEqual(rules.apply_mirror(api, MIRROR, by_repo()["scratch"], dry_run=False),
                         [f"{MIRROR}/scratch (the GitHub copy)", "  no version tags (rules: basic): nothing to apply"])
        self.assertEqual(api.calls, [])

    def test_given_dry_run_when_applied_then_the_payload_is_shown_and_nothing_is_written(self):
        api = FakeGitHub()
        lines = rules.apply_mirror(api, MIRROR, by_repo()["shop-api"], dry_run=True)
        self.assertEqual(api.writes(), [])
        self.assertIn(f"  would create ruleset 'mirror tags': POST /repos/{MIRROR}/shop-api/rulesets", lines)
        self.assertIn('"bypass_actors": []', "\n".join(lines))


class MirrorCommandTests(unittest.TestCase):
    ENV = {"FORGE_TOKEN": "fg-tok-123"}

    def run_main(self, args, env, catalog=CATALOG_MIRROR, gh=None):
        """The command with a fake Forgejo and a fake GitHub (by the scheme each Api is made with), the
        environment given and nothing of the real one; `gh` is what `gh auth token` answers (None: no gh)."""
        fakes = {"token": FakeForgejo(), "Bearer": FakeGitHub()}
        made = []

        def make_api(base, token, scheme, headers=None):
            made.append((base, token, scheme))
            return fakes[scheme]

        def run(command, **_):
            if gh is None:
                raise FileNotFoundError("gh")
            return subprocess.CompletedProcess(command, 0 if gh else 1, stdout=gh, stderr="")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rules.catalog_module, "load", return_value=catalog), \
                mock.patch.dict(os.environ, env, clear=True), mock.patch.object(rules, "Api", make_api), \
                mock.patch("subprocess.run", run), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                rules.main(args)
                exit_ = None
            except SystemExit as raised:
                exit_ = raised
        return fakes, made, out.getvalue(), err.getvalue(), exit_

    def test_given_a_mirror_and_a_github_token_when_run_then_forgejo_and_the_copy_are_both_applied(self):
        fakes, made, out, err, exit_ = self.run_main(["shop-api"], {**self.ENV, "GH_TOKEN": "gh-tok-456"})
        self.assertIsNone(exit_, err)
        self.assertEqual(made, [("https://git.example.com/api/v1", "fg-tok-123", "token"),
                                ("https://api.github.com", "gh-tok-456", "Bearer")])
        self.assertEqual([(m, p) for m, p, _ in fakes["Bearer"].writes()], [("POST", f"/repos/{MIRROR}/shop-api/rulesets")])
        self.assertTrue(fakes["token"].writes())
        # Forgejo first, then its copy.
        self.assertLess(out.index("artur-rios/shop-api"), out.index(f"{MIRROR}/shop-api (the GitHub copy)"))
        self.assertNotIn("gh-tok-456", out + err)

    def test_given_a_mirror_and_gh_logged_in_when_run_then_its_token_is_used(self):
        _, made, _, _, exit_ = self.run_main(["--dry-run", "shop-api"], self.ENV, gh="from-gh\n")
        self.assertIsNone(exit_)
        self.assertEqual(made[1], ("https://api.github.com", "from-gh", "Bearer"))

    def test_given_a_mirror_without_a_github_token_when_run_then_forgejo_is_applied_and_it_exits_1(self):
        # The catalog asks for GitHub copies; leaving them unprotected without failing would read as done.
        fakes, made, out, err, exit_ = self.run_main(["shop-api"], self.ENV, gh="")
        self.assertEqual([scheme for _, _, scheme in made], ["token"])
        self.assertTrue(fakes["token"].writes())
        self.assertEqual(fakes["Bearer"].calls, [])
        self.assertIn("mirror tags not applied", out)
        # sys.exit with a message: exit status 1, the message on stderr.
        self.assertIsInstance(exit_.code, str)
        self.assertIn("mirror tags were not applied", exit_.code)
        self.assertIn("GH_TOKEN", str(exit_.code))

    def test_given_no_gh_at_all_when_run_with_a_mirror_then_it_is_the_same_as_no_token(self):
        fakes, _, out, _, exit_ = self.run_main(["--dry-run", "shop-api"], self.ENV, gh=None)
        self.assertIn("mirror tags not applied", out)
        self.assertIsNotNone(exit_)
        self.assertEqual(fakes["Bearer"].calls, [])

    def test_given_no_mirror_when_run_then_github_is_never_asked(self):
        fakes, made, out, _, exit_ = self.run_main(["shop-api"], self.ENV, catalog=CATALOG, gh=None)
        self.assertIsNone(exit_)
        self.assertEqual([scheme for _, _, scheme in made], ["token"])
        self.assertNotIn("mirror", out)

    def test_given_dry_run_with_a_mirror_when_run_then_the_copys_payload_is_shown_and_nothing_written(self):
        fakes, _, out, _, exit_ = self.run_main(["--dry-run"], {**self.ENV, "GH_TOKEN": "gh-tok-456"})
        self.assertIsNone(exit_)
        self.assertEqual(fakes["Bearer"].writes(), [])
        self.assertEqual(fakes["token"].writes(), [])
        self.assertIn(f"would create ruleset 'mirror tags': POST /repos/{MIRROR}/toolkit/rulesets", out)
        self.assertIn('"include": ["refs/tags/[0-9]*"]', out)
        # The basic repository's copy is only looked at.
        self.assertIn(f"{MIRROR}/scratch (the GitHub copy)", out)

    def run_with_github(self, github, args=()):
        """main() with a fake Forgejo and the given fake GitHub: (forgejo, out, the SystemExit or None)."""
        fakes = {"token": PerRepoForgejo(), "Bearer": github}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rules.catalog_module, "load", return_value=CATALOG_MIRROR), \
                mock.patch.dict(os.environ, {**self.ENV, "GH_TOKEN": "gh-tok-456"}, clear=True), \
                mock.patch.object(rules, "Api", lambda base, token, scheme, headers=None: fakes[scheme]), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                rules.main(list(args))
                exit_ = None
            except SystemExit as raised:
                exit_ = raised
        return fakes["token"], out.getvalue(), exit_

    def test_given_the_copy_missing_on_github_when_run_then_the_later_repositories_still_get_their_rules(self):
        # A copy the push mirror has not made yet (404), or a private one on a free plan (403): each copy is
        # reported, the run goes on with every later repository's Forgejo rules, and fails at the end.
        class Missing(FakeGitHub):
            def request(self, method, path, body=None):
                self.calls.append((method, path, body))
                raise rules.ApiError(method, path, 404, "Not Found")

        github = Missing()
        forgejo, out, exit_ = self.run_with_github(github)
        for repo in ("shop-api", "storefront", "toolkit", "scratch", "notes"):
            self.assertIn(("POST", f"/repos/{OWNER}/{repo}/branch_protections"), [(m, p) for m, p, _ in forgejo.writes()],
                          repo)
        for repo in ("shop-api", "storefront", "toolkit", "notes"):
            self.assertIn(f"{repo} (GitHub copy {MIRROR}/{repo}): not applied: GET /repos/{MIRROR}/{repo}/rulesets"
                          f"?per_page=100: 404 Not Found", out)
        # The basic repository's copy is never asked.
        self.assertNotIn(f"/repos/{MIRROR}/scratch/rulesets?per_page=100", [p for _, p, _ in github.calls])
        self.assertIsInstance(exit_.code, str)
        self.assertIn("the mirror tags were not applied to 4 GitHub copies", exit_.code)
        self.assertIn("shop-api, storefront, toolkit, notes", exit_.code)

    def test_given_github_refusing_one_copy_when_run_then_the_others_are_applied_and_it_exits_1(self):
        class RefusesToolkit(FakeGitHub):
            def request(self, method, path, body=None):
                if f"/{MIRROR}/toolkit/" in path:
                    self.calls.append((method, path, body))
                    raise rules.ApiError(method, path, 403, "Upgrade to GitHub Pro or make this repository public")
                return super().request(method, path, body)

        github = RefusesToolkit()
        _, out, exit_ = self.run_with_github(github)
        # (One fake for every copy: the first creates `mirror tags`, the next ones find it and update it.)
        written = [p.split("/")[3] for _, p, _ in github.writes()]
        self.assertEqual(written, ["shop-api", "storefront", "notes"])
        self.assertIn(f"toolkit (GitHub copy {MIRROR}/toolkit): not applied: GET", out)
        self.assertIn("403 Upgrade to GitHub Pro", out)
        self.assertIn("the mirror tags were not applied to 1 GitHub copy: toolkit", exit_.code)

    def test_given_a_forgejo_error_when_run_then_it_still_stops_the_run(self):
        # Only a copy's failure is carried on from: one of Forgejo's own still stops the run, as before.
        class Failing(FakeForgejo):
            def request(self, method, path, body=None):
                if method != "GET" and "/storefront" in path:
                    raise rules.ApiError(method, path, 500, "boom")
                return super().request(method, path, body)

        fakes = {"token": PerRepoForgejo(Failing), "Bearer": FakeGitHub()}
        with mock.patch.object(rules.catalog_module, "load", return_value=CATALOG_MIRROR), \
                mock.patch.dict(os.environ, {**self.ENV, "GH_TOKEN": "gh-tok-456"}, clear=True), \
                mock.patch.object(rules, "Api", lambda base, token, scheme, headers=None: fakes[scheme]), \
                mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                rules.main([])
        self.assertTrue(str(raised.exception).startswith("storefront: "), raised.exception)


class ApiTests(unittest.TestCase):
    """rules.Api against a real HTTP server on localhost."""

    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen.append((self.command, self.path, {k.title(): v for k, v in self.headers.items()},
                             self.rfile.read(length).decode("utf-8")))
                status, body = {"/missing": (404, '{"message": "Not Found"}'),
                                "/empty": (204, ""),
                                "/html": (502, "<html>bad gateway</html>")}.get(self.path, (200, '{"ok": true}'))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_given_a_token_when_a_request_is_sent_then_it_is_only_in_the_authorization_header(self):
        api = rules.Api(self.base, "tok-123", "token")
        self.assertEqual(api.request("POST", "/repos/o/r?x=1", {"a": 1}), {"ok": True})
        method, path, headers, body = self.seen[0]
        self.assertEqual((method, path, json.loads(body)), ("POST", "/repos/o/r?x=1", {"a": 1}))
        self.assertEqual(headers["Authorization"], "token tok-123")
        self.assertNotIn("tok-123", path + body)
        self.assertEqual(sum("tok-123" in v for v in headers.values()), 1)

    def test_given_the_github_scheme_when_a_request_is_sent_then_it_uses_bearer_and_the_given_headers(self):
        github_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        rules.Api(self.base, "tok-abc", "Bearer", github_headers).request("GET", "/x")
        headers = self.seen[0][2]
        self.assertEqual((headers["Authorization"], headers["Accept"], headers["X-Github-Api-Version"]),
                         ("Bearer tok-abc", "application/vnd.github+json", "2022-11-28"))

    def test_given_an_empty_answer_when_requested_then_the_result_is_none(self):
        self.assertIsNone(rules.Api(self.base, "tok-abc", "Bearer").request("DELETE", "/empty"))

    def test_given_an_error_status_when_requested_then_the_servers_message_is_raised(self):
        with self.assertRaises(rules.ApiError) as raised:
            rules.Api(self.base, "tok-123", "Bearer").request("GET", "/missing")
        error = raised.exception
        self.assertEqual((error.method, error.path, error.status, error.message), ("GET", "/missing", 404, "Not Found"))
        self.assertNotIn("tok-123", str(error))

    def test_given_a_non_json_error_when_requested_then_the_text_is_the_message(self):
        with self.assertRaises(rules.ApiError) as raised:
            rules.Api(self.base, "tok-abc", "Bearer").request("GET", "/html")
        self.assertEqual(raised.exception.status, 502)
        self.assertIn("bad gateway", raised.exception.message)

    def test_given_a_timeout_when_requested_then_the_server_is_waited_for_that_long_and_a_minute_by_default(self):
        # A repository migration answers when the copy is done, minutes later: migrate.py asks for more.
        api = rules.Api(self.base, "tok-abc", "Bearer")
        with mock.patch.object(rules.urllib.request, "urlopen", wraps=rules.urllib.request.urlopen) as opened:
            api.request("GET", "/x")
            api.request("GET", "/x", timeout=1800)
        self.assertEqual([call.kwargs["timeout"] for call in opened.call_args_list], [60, 1800])

    def test_given_no_server_when_requested_then_it_is_an_api_error_without_a_status(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(rules.ApiError) as raised:
            rules.Api(self.base, "tok-abc", "Bearer").request("GET", "/x")
        self.assertEqual(raised.exception.status, 0)


class GitHubTokenTests(unittest.TestCase):
    def test_given_gh_token_when_read_then_gh_is_not_asked(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "from-env"}), mock.patch("subprocess.run") as run:
            self.assertEqual(rules.github_token(), "from-env")
        run.assert_not_called()

    def test_given_no_gh_token_when_read_then_gh_auth_token_supplies_it(self):
        env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
        done = subprocess.CompletedProcess(["gh"], 0, stdout="from-gh\n", stderr="")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("subprocess.run", return_value=done) as run:
            self.assertEqual(rules.github_token(), "from-gh")
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "token"])

    def test_given_neither_when_read_then_it_says_what_to_set(self):
        env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
        done = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="not logged in")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("subprocess.run", return_value=done):
            with self.assertRaises(SystemExit) as raised:
                rules.github_token()
        self.assertIn("GH_TOKEN", str(raised.exception))


class ShimTests(unittest.TestCase):
    def test_given_the_old_rulesets_py_when_run_then_it_runs_rules_py(self):
        # An unknown repository name is refused by rules.py before any network call, naming it.
        done = subprocess.run([sys.executable, str(ROOT / "github" / "rulesets.py"), "--dry-run", "no-such-repo"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertIn("scripts/rules.py", done.stderr)
        self.assertIn("no-such-repo", done.stderr)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
