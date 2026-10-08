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

    def test_given_a_boolean_for_a_number_when_validated_then_it_is_an_error(self):
        # YAML's `true` is a Python bool, which is an int: it must not pass as a timeout of 1.
        value = catalog()
        value["environments"][2]["checksTimeout"] = True
        value["systems"][0]["applications"][1]["environments"]["prod"]["keepImages"] = True
        errors = c.validate(value)
        self.assertTrue(any("prod'.checksTimeout" in e for e in errors), errors)
        self.assertTrue(any("environments.prod.keepImages" in e for e in errors), errors)

    def test_given_a_branch_override_with_no_branches_anywhere_when_validated_then_it_is_an_error(self):
        # Neither the override nor the environment names a branch: that environment would never deploy.
        value = catalog()
        value["systems"][0]["applications"][0]["environments"] = {"dev": {"trigger": "branch"}, "staging": {}}
        errors = c.validate(value)
        self.assertTrue(any("environments.dev.branches" in e for e in errors), errors)

    def test_given_a_branch_override_relying_on_the_environment_branches_when_validated_then_it_is_valid(self):
        value = catalog()
        value["systems"][0]["applications"][0]["environments"] = {"staging": {"trigger": "branch"}}
        self.assertEqual(c.validate(value), [])

    # The status API refuses a catalog that breaks any of these at start-up (CatalogLoader.cs), on every
    # host: validate, which CI runs on every pull request, has to refuse it first.

    def test_given_an_application_without_a_name_when_validated_then_it_is_an_error(self):
        value = catalog()
        del value["systems"][0]["applications"][0]["name"]
        self.assertTrue(any("'shop-api'.name" in e for e in c.validate(value)))

    def test_given_a_system_without_a_name_or_applications_when_validated_then_both_are_errors(self):
        value = catalog()
        del value["systems"][0]["name"]
        value["systems"][1]["applications"] = []
        errors = c.validate(value)
        self.assertTrue(any("system 'shop'.name" in e for e in errors), errors)
        self.assertTrue(any("system 'platform'.applications" in e for e in errors), errors)

    def test_given_no_systems_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(e.startswith("systems:") for e in c.validate(catalog(systems=[]))))

    def test_given_duplicate_system_ids_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["systems"][1]["id"] = "shop"
        self.assertTrue(any("duplicate" in e and "shop" in e for e in c.validate(value)))

    def test_given_malformed_application_fields_when_validated_then_each_is_reported(self):
        value = catalog()
        app = value["systems"][0]["applications"][0]
        app.update(metrics="shop-api", metricsPath="metrics", host="Shop.API", repository="shop api", checks=["test", " "])
        errors = c.validate(value)
        for field in ("metrics", "metricsPath", "host", "repository", "checks"):
            self.assertTrue(any(f"'shop-api'.{field}" in e for e in errors), (field, errors))

    def test_given_a_metrics_path_without_metrics_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["systems"][0]["applications"][0]["metricsPath"] = "/prometheus/"
        self.assertTrue(any("'shop-api'.metricsPath" in e for e in c.validate(value)))

    def test_given_valid_optional_application_fields_when_validated_then_there_are_no_errors(self):
        value = catalog()
        value["systems"][0]["applications"][0].update(
            metrics="shop-api:9464", metricsPath="/prometheus/", host="shop-api", repository="Shop.API_v2",
            checks=["test", "API client matches api/shop.json"], container={"project": "shop", "service": "api"})
        self.assertEqual(c.validate(value), [])

    def test_given_entries_of_the_wrong_type_when_validated_then_errors_not_exceptions(self):
        self.assertTrue(c.validate(catalog(systems=["shop"])))
        self.assertTrue(c.validate(catalog(systems=[{"id": "shop", "name": "Shop", "applications": ["shop-api"]}])))
        self.assertTrue(c.validate(catalog(systems={"shop": {}})))


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


TEXT = """\
# A comment that must survive.
owner: someone

environments:
  - id: dev
    name: Dev

systems:
  - id: shop
    name: Shop
    applications:
      - id: shop-api
        name: API
        kind: api
        health: http://shop-api:8080/healthz

  # The platform, kept last.
  - id: yggdrasil
    name: Yggdrasil
    applications:
      - id: traefik
        name: Traefik
        kind: platform
        health: http://traefik:8082/ping
"""

NEW = {"id": "shop-web", "name": "Web", "kind": "web", "health": "http://shop-web:8080/healthz",
       "host": "shop", "checks": ["Analyze and test", "lint"]}


class AddApplicationTests(unittest.TestCase):
    def test_given_an_existing_system_when_adding_then_the_application_ends_that_system(self):
        text = c.add_application(TEXT, {"id": "shop"}, NEW)
        parsed = c.yaml.safe_load(text)
        self.assertEqual([a["id"] for a in parsed["systems"][0]["applications"]], ["shop-api", "shop-web"])
        self.assertEqual(c.application(parsed, "shop-web"), NEW)

    def test_given_an_existing_system_when_adding_then_every_comment_stays(self):
        text = c.add_application(TEXT, {"id": "shop"}, NEW)
        self.assertIn("# A comment that must survive.", text)
        self.assertLess(text.index("id: shop-web"), text.index("# The platform, kept last."))

    def test_given_a_new_system_when_adding_then_it_goes_before_the_platform_and_its_comment(self):
        text = c.add_application(TEXT, {"id": "blog", "name": "Blog", "description": "Writing"}, dict(NEW, id="blog-web"))
        parsed = c.yaml.safe_load(text)
        self.assertEqual([s["id"] for s in parsed["systems"]], ["shop", "blog", "yggdrasil"])
        self.assertEqual(parsed["systems"][1]["description"], "Writing")
        self.assertLess(text.index("id: blog-web"), text.index("# The platform, kept last."))

    def test_given_no_platform_system_when_adding_a_system_then_it_is_appended(self):
        base = TEXT[:TEXT.index("  # The platform")]
        text = c.add_application(base, {"id": "blog"}, dict(NEW, id="blog-web"))
        self.assertEqual([s["id"] for s in c.yaml.safe_load(text)["systems"]], ["shop", "blog"])

    def test_given_an_invalid_application_when_adding_then_it_raises_and_names_the_problem(self):
        with self.assertRaisesRegex(c.CatalogError, "kind"):
            c.add_application(TEXT, {"id": "shop"}, dict(NEW, kind="database"))

    def test_given_values_needing_quotes_when_adding_then_they_round_trip(self):
        app = dict(NEW, name="Web: the UI", checks=["a, b", "#c"])
        text = c.add_application(TEXT, {"id": "shop"}, app)
        self.assertEqual(c.application(c.yaml.safe_load(text), "shop-web"), app)

    def test_given_the_repository_catalog_when_adding_then_it_still_loads(self):
        text = c.add_application(c.CATALOG.read_text(encoding="utf-8"), {"id": "new-system"}, dict(NEW, id="new-app"))
        self.assertEqual(c.system_of(c.yaml.safe_load(text), "new-app")["id"], "new-system")


if __name__ == "__main__":
    unittest.main()
