"""Tests for scripts/catalog.py.   python3 -m unittest discover -s scripts"""

import copy
import unittest

import catalog as c

BASE = {
    "owner": "someone",
    "environments": [
        {"id": "dev", "name": "Dev", "mode": "ports", "trigger": "manual"},
        {"id": "staging", "name": "Staging", "trigger": "branch", "branches": "release/*"},
        {"id": "prod", "name": "Prod", "trigger": "release", "agent": "vps"},
    ],
    "systems": [
        {"id": "shop", "name": "Shop", "applications": [
            {"id": "shop-api", "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz"},
            {"id": "shop-web", "name": "Web", "kind": "web", "health": "http://shop-web:8080/healthz",
             "environments": {"staging": {}, "prod": {"approval": True, "waitTimeout": 900}}},
        ]},
        {"id": "platform", "name": "Platform", "applications": [
            {"id": "traefik", "name": "Traefik", "kind": "platform", "health": "http://traefik:8082/ping"},
        ]},
    ],
}


def catalog(**changes):
    value = copy.deepcopy(BASE)
    value.update(changes)
    return value


class ValidateTests(unittest.TestCase):
    def test_given_a_valid_catalog_when_validated_then_there_are_no_errors(self):
        self.assertEqual(c.validate(catalog()), [])

    def test_given_the_repository_catalog_when_loaded_then_it_is_valid(self):
        c.load()

    def test_given_no_environments_when_validated_then_it_is_an_error(self):
        self.assertTrue(any("environments" in e for e in c.validate(catalog(environments=[]))))

    def test_given_duplicate_environment_ids_when_validated_then_it_is_an_error(self):
        envs = copy.deepcopy(BASE["environments"]) + [{"id": "dev", "name": "Again"}]
        self.assertTrue(any("duplicate 'dev'" in e for e in c.validate(catalog(environments=envs))))

    def test_given_bad_mode_and_trigger_when_validated_then_both_are_reported(self):
        envs = [{"id": "x", "name": "X", "mode": "nope", "trigger": "never"}]
        errors = c.validate(catalog(environments=envs, systems=[]))
        self.assertTrue(any(".mode" in e for e in errors))
        self.assertTrue(any(".trigger" in e for e in errors))

    def test_given_branch_trigger_without_branches_when_validated_then_it_is_an_error(self):
        envs = [{"id": "x", "name": "X", "trigger": "branch"}]
        self.assertTrue(any("branches: required" in e for e in c.validate(catalog(environments=envs, systems=[]))))

    def test_given_an_override_for_an_unknown_environment_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["systems"][0]["applications"][0]["environments"] = {"qa": {}}
        self.assertTrue(any("environments.qa" in e for e in c.validate(value)))

    def test_given_an_override_renaming_the_environment_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["systems"][0]["applications"][0]["environments"] = {"dev": {"name": "Mine"}}
        self.assertTrue(any("cannot rename" in e for e in c.validate(value)))

    def test_given_an_unknown_option_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["environments"][0]["replicas"] = 2
        self.assertTrue(any("unknown option" in e for e in c.validate(value)))

    def test_given_a_non_positive_timeout_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["environments"][0]["waitTimeout"] = 0
        self.assertTrue(any("waitTimeout" in e for e in c.validate(value)))


class ResolveTests(unittest.TestCase):
    def test_given_no_overrides_when_resolved_then_every_environment_with_defaults(self):
        plan = c.resolve(catalog(), "shop-api")
        self.assertEqual([e["id"] for e in plan], ["dev", "staging", "prod"])
        self.assertEqual(plan[1]["mode"], "proxy")
        self.assertEqual(plan[1]["branches"], ["release/*"])
        self.assertEqual(plan[0]["agent"], "dev")
        self.assertEqual(plan[2]["agent"], "vps")
        self.assertEqual(plan[2]["waitTimeout"], 300)

    def test_given_overrides_when_resolved_then_only_those_environments_with_overrides_applied(self):
        plan = c.resolve(catalog(), "shop-web")
        self.assertEqual([e["id"] for e in plan], ["staging", "prod"])
        self.assertFalse(plan[0]["approval"])
        self.assertTrue(plan[1]["approval"])
        self.assertEqual(plan[1]["waitTimeout"], 900)
        self.assertEqual(plan[1]["trigger"], "release")

    def test_given_an_unknown_application_when_resolved_then_it_raises(self):
        with self.assertRaises(c.CatalogError):
            c.resolve(catalog(), "nope")

    def test_given_branch_globs_when_matched_then_fnmatch_semantics(self):
        staging = c.resolve(catalog(), "shop-api")[1]
        self.assertTrue(c.matches_branch(staging, "release/1.2.3"))
        self.assertFalse(c.matches_branch(staging, "develop"))


class DeployableTests(unittest.TestCase):
    def test_given_platform_components_when_listing_deployable_then_they_are_excluded(self):
        ids = [a["id"] for _, a in c.applications(catalog(), deployable=True)]
        self.assertEqual(ids, ["shop-api", "shop-web"])


if __name__ == "__main__":
    unittest.main()
