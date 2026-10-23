"""Tests for scripts/rules.py (the model).   python3 -m unittest discover -s scripts"""

import copy
import unittest

import rules

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
        self.assertEqual(r.protected_files, ("Jenkinsfile",))
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

    def test_given_the_repository_catalog_when_modelled_then_every_entry_is_valid(self):
        import catalog
        modelled = rules.desired_rules(catalog.load())
        self.assertTrue(all(r.model in ("release", "basic") for r in modelled))


if __name__ == "__main__":
    unittest.main()
