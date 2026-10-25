"""Tests for scripts/catalog.py.   python3 -m unittest discover -s scripts"""

import contextlib
import copy
import io
import unittest
from unittest import mock

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


def run_cli(value, *argv):
    """main(argv) against `value` instead of the repository's catalog: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    out.reconfigure = lambda **_: None  # main() asks stdout for plain LF line endings
    with mock.patch.object(c, "load", return_value=value), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = c.main(list(argv))
    return status, out.getvalue(), err.getvalue()


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

    def test_given_two_applications_with_the_same_project_name_when_validated_then_it_is_an_error(self):
        envs = copy.deepcopy(BASE["environments"]) + [{"id": "api-dev", "name": "API dev"}]
        value = catalog(environments=envs)
        # shop-api in dev and shop in api-dev would both be the Compose project shop-api-dev.
        value["systems"][0]["applications"].append(
            {"id": "shop", "name": "Shop", "kind": "worker", "health": "http://shop:8080/healthz"})
        self.assertTrue(any("Compose project 'shop-api-dev'" in e for e in c.validate(value)))

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

    def test_given_valid_host_suffix_and_on_demand_when_validated_then_there_are_no_errors(self):
        value = catalog()
        value["environments"][1].update(hostSuffix="-hml", onDemand=True)
        value["systems"][0]["applications"][0].update(host="shop-api", environments={"prod": {"hostSuffix": "", "onDemand": False}})
        self.assertEqual(c.validate(value), [])

    def test_given_malformed_host_suffix_and_on_demand_when_validated_then_each_is_reported(self):
        for suffix in ("-dev-", "-Dev", ".dev", 3, None):
            with self.subTest(suffix=suffix):
                value = catalog()
                value["environments"][0]["hostSuffix"] = suffix
                self.assertTrue(any("'dev'.hostSuffix" in e for e in c.validate(value)), suffix)
        value = catalog()
        value["environments"][0]["onDemand"] = "yes"
        value["systems"][0]["applications"][1]["environments"]["prod"]["onDemand"] = 1
        errors = c.validate(value)
        self.assertTrue(any("'dev'.onDemand" in e for e in errors), errors)
        self.assertTrue(any("environments.prod.onDemand" in e for e in errors), errors)

    def test_given_a_host_that_would_not_stay_one_label_with_the_suffix_when_validated_then_it_is_an_error(self):
        value = catalog()
        value["environments"][1]["hostSuffix"] = "-hml"
        value["systems"][0]["applications"][0]["host"] = "a" * 60
        errors = c.validate(value)
        self.assertTrue(any("'shop-api'.host" in e and "'staging'" in e for e in errors), errors)
        value["systems"][0]["applications"][0]["host"] = "api.shop"
        self.assertTrue(any("'shop-api'.host" in e for e in c.validate(value)))
        # Without a suffix in its environments, a host keeps its own rules.
        value["systems"][0]["applications"][0]["environments"] = {"prod": {}}
        self.assertEqual(c.validate(value), [])

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
        app.update(metrics="shop-api", metricsPath="metrics", host="Shop.API", repository="shop api", checks=["test", " "],
                   releaseArtifacts="yes")
        errors = c.validate(value)
        for field in ("metrics", "metricsPath", "host", "repository", "checks", "releaseArtifacts"):
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

    def test_given_host_suffix_and_on_demand_when_resolved_then_default_environment_then_override(self):
        value = catalog()
        value["environments"][1].update(hostSuffix="-hml", onDemand=True)
        value["systems"][0]["applications"][1]["environments"]["staging"] = {"onDemand": False}
        api = c.resolve(value, "shop-api")
        self.assertEqual([(e["hostSuffix"], e["onDemand"]) for e in api], [("", False), ("-hml", True), ("", False)])
        web = c.resolve(value, "shop-web")
        self.assertEqual([(e["hostSuffix"], e["onDemand"]) for e in web], [("-hml", False), ("", False)])

    def test_given_an_environment_when_its_options_are_asked_then_defaults_apply_without_overrides(self):
        value = catalog()
        value["environments"][1]["onDemand"] = True
        staging = c.environment_options(value, "staging")
        self.assertEqual((staging["onDemand"], staging["hostSuffix"], staging["agent"]), (True, "", "staging"))
        self.assertEqual(staging["branches"], ["release/*"])
        with self.assertRaises(c.CatalogError):
            c.environment_options(value, "qa")

    def test_given_an_unknown_application_when_resolved_then_it_raises(self):
        with self.assertRaises(c.CatalogError):
            c.resolve(catalog(), "nope")

    def test_given_branch_globs_when_matched_then_fnmatch_semantics(self):
        staging = c.resolve(catalog(), "shop-api")[1]
        self.assertTrue(c.matches_branch(staging, "release/1.2.3"))
        self.assertFalse(c.matches_branch(staging, "develop"))


class ChimeraCatalogTests(unittest.TestCase):
    def test_given_repository_catalog_when_chimera_is_resolved_then_its_contract_is_complete(self):
        value = c.load()
        application = c.application(value, "chimera")
        system = c.system_of(value, "chimera")

        self.assertEqual(system["id"], "chimera")
        self.assertEqual(application["kind"], "web")
        self.assertEqual(application["health"], "http://chimera:8080/healthz")
        self.assertEqual(application["host"], "chimera")
        self.assertEqual(application["container"], {"service": "web"})
        self.assertTrue(application["releaseArtifacts"])
        self.assertEqual(application["checks"], [
            "Analyze and test",
            "Cross-platform",
            "Container",
            "Accessibility and performance",
        ])

        plan = c.resolve(value, "chimera")
        self.assertEqual(
            [environment["id"] for environment in plan],
            ["local", "development", "homologation", "production"],
        )
        self.assertTrue(plan[-1]["approval"])

    def test_given_release_pipeline_when_loaded_then_the_whole_application_release_is_serialized(self):
        root = c.CATALOG.parent
        pipeline = (root / "jenkins/library/vars/yggdrasilPipeline.groovy").read_text(encoding="utf-8")
        plugins = (root / "platform/jenkins/controller/plugins.txt").read_text(encoding="utf-8")

        lock = pipeline.index('lock(resource: "release-${app}")')
        # Both release paths (GitHub without yggdrasil.yml, and the rest) run under the lock.
        self.assertLess(lock, pipeline.index("release(app, environments, releaseArtifacts, ctx.repo)"))
        self.assertLess(lock, pipeline.index("releasePullRequest(ctx, state, environments)"))
        self.assertIn("lockable-resources", plugins.splitlines())

    def test_given_chimera_stacks_when_loaded_then_runtime_is_hardened_and_routable(self):
        stacks = c.CATALOG.parent / "stacks"
        base = c.yaml.safe_load((stacks / "chimera.yml").read_text(encoding="utf-8"))
        proxy = c.yaml.safe_load((stacks / "chimera.proxy.yml").read_text(encoding="utf-8"))
        ports = c.yaml.safe_load((stacks / "chimera.ports.yml").read_text(encoding="utf-8"))

        web = base["services"]["web"]
        self.assertEqual(web["build"]["dockerfile"], "deploy/web/Dockerfile")
        self.assertTrue(web["read_only"])
        self.assertEqual(web["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", web["security_opt"])
        self.assertEqual(web["tmpfs"], ["/tmp"])
        self.assertEqual(
            proxy["services"]["web"]["networks"]["edge"]["aliases"],
            ["chimera.${YGG_ENVIRONMENT:?YGG_ENVIRONMENT is set by scripts/deploy.sh}"],
        )
        self.assertIn(
            "traefik.http.services.chimera-${YGG_ENVIRONMENT}.loadbalancer.server.port=8080",
            proxy["services"]["web"]["labels"],
        )
        self.assertEqual(
            ports["services"]["web"]["ports"],
            ["${CHIMERA_PORT:-127.0.0.1:8083}:8080"],
        )


class ForgeTests(unittest.TestCase):
    """The git server: forge (forgejo or github, github when absent) and mirror (forgejo -> GitHub)."""

    FORGEJO = {"type": "forgejo", "url": "https://git.techcraftsmen.net"}

    def errors(self, **changes):
        return c.validate(catalog(**changes))

    def run_cli(self, value, *argv):
        return run_cli(value, *argv)

    def test_given_no_forge_block_when_read_then_it_is_github_as_before(self):
        self.assertEqual(c.forge(catalog())["type"], "github")
        self.assertEqual(c.forge(catalog())["api"], "https://api.github.com")
        self.assertEqual(c.forge(catalog()), {"type": "github", "url": "https://github.com",
                                             "api": "https://api.github.com", "jenkinsUser": "jenkins",
                                             "developers": [], "publishAgent": "vps"})

    def test_given_the_repository_catalog_when_read_then_it_is_github_without_a_mirror(self):
        self.assertEqual(c.forge(c.load())["type"], "github")
        self.assertIsNone(c.mirror(c.load()))

    def test_given_forgejo_when_read_then_the_api_is_under_its_url(self):
        v = catalog(forge={"type": "forgejo", "url": "https://git.techcraftsmen.net/"})
        self.assertEqual(c.forge(v)["url"], "https://git.techcraftsmen.net")
        self.assertEqual(c.forge(v)["api"], "https://git.techcraftsmen.net/api/v1")

    def test_given_github_with_a_forge_block_when_read_then_the_urls_are_githubs(self):
        v = catalog(forge={"type": "github", "jenkinsUser": "ci", "developers": ["ann"]})
        self.assertEqual(c.validate(v), [])
        self.assertEqual(c.forge(v), {"type": "github", "url": "https://github.com",
                                      "api": "https://api.github.com", "jenkinsUser": "ci", "developers": ["ann"],
                                      "publishAgent": "vps"})

    def test_given_forgejo_without_a_url_when_validated_then_it_is_an_error(self):
        self.assertTrue(any("forge.url: required" in e for e in self.errors(forge={"type": "forgejo"})))

    def test_given_a_url_for_github_when_validated_then_it_is_an_error(self):
        errors = self.errors(forge={"type": "github", "url": "https://github.example.com"})
        self.assertTrue(any("forge.url" in e and "Enterprise" in e for e in errors))

    def test_given_a_plain_http_forgejo_url_when_validated_then_it_is_an_error(self):
        errors = self.errors(forge={"type": "forgejo", "url": "http://git.techcraftsmen.net"})
        self.assertTrue(any("forge.url" in e and "https" in e for e in errors))

    def test_given_a_forgejo_url_with_a_path_or_no_host_when_validated_then_it_is_an_error(self):
        for url in ("https://example.com/forgejo", "https://example.com/?a=1", "https://", "example.com", 3):
            with self.subTest(url=url):
                self.assertTrue(any("forge.url" in e for e in self.errors(forge={"type": "forgejo", "url": url})))

    def test_given_a_forgejo_url_with_a_port_or_a_lone_slash_when_validated_then_it_is_valid(self):
        for url in ("https://git.example.com:3000", "https://git.example.com/"):
            with self.subTest(url=url):
                self.assertEqual(self.errors(forge={"type": "forgejo", "url": url}), [])

    def test_given_an_unknown_or_missing_forge_type_when_validated_then_it_is_an_error(self):
        self.assertTrue(any("forge.type" in e for e in self.errors(forge={"type": "gitlab"})))
        self.assertTrue(any("forge.type" in e for e in self.errors(forge={})))

    def test_given_a_forge_that_is_not_a_mapping_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(e.startswith("forge:") for e in self.errors(forge="forgejo")))

    def test_given_an_unknown_forge_key_when_validated_then_it_is_an_error(self):
        self.assertTrue(any("forge" in e and "token" in e for e in self.errors(forge=dict(self.FORGEJO, token="x"))))

    def test_given_a_number_as_a_key_when_validated_then_it_is_reported_not_raised(self):
        # YAML reads `1: x` as the int 1: next to the str keys, sorting them must not raise TypeError.
        errors = self.errors(forge={**self.FORGEJO, 1: "x", "token": "y"},
                             mirror={"github": "acme", 2: True})
        self.assertIn("forge: unknown key(s) [1, 'token']", errors)
        self.assertIn("mirror: unknown key(s) [2]", errors)

    def test_given_bad_user_names_when_validated_then_the_fields_are_named(self):
        errors = self.errors(forge=dict(self.FORGEJO, jenkinsUser="-bad user", developers=["ok", "no way"]))
        self.assertTrue(any("forge.jenkinsUser" in e for e in errors))
        self.assertTrue(any("forge.developers" in e for e in errors))
        self.assertTrue(any("forge.developers" in e for e in self.errors(forge=dict(self.FORGEJO, developers="claude"))))

    def test_given_github_when_the_jenkins_user_is_an_apps_bot_login_then_it_is_valid(self):
        for user in ("my-app[bot]", "My.App_1[bot]", "jenkins", "ci"):
            with self.subTest(user=user):
                v = catalog(forge={"type": "github", "jenkinsUser": user})
                self.assertEqual(c.validate(v), [])
                self.assertEqual(c.forge(v)["jenkinsUser"], user)

    def test_given_github_when_the_jenkins_user_is_a_malformed_bot_login_then_it_is_an_error(self):
        for user in ("[bot]", "app[bot]x", "app[bot][bot]", "app[Bot]", "app[bot]\n", "app[bots]", "-app[bot]",
                     "app [bot]", "app[bot"):
            with self.subTest(user=user):
                errors = self.errors(forge={"type": "github", "jenkinsUser": user})
                self.assertTrue(any("forge.jenkinsUser" in e for e in errors), errors)

    def test_given_forgejo_when_the_jenkins_user_has_a_bot_suffix_then_it_is_an_error(self):
        errors = self.errors(forge=dict(self.FORGEJO, jenkinsUser="my-app[bot]"))
        self.assertTrue(any("forge.jenkinsUser" in e for e in errors), errors)

    def test_given_github_when_a_developer_has_a_bot_suffix_then_it_is_still_an_error(self):
        errors = self.errors(forge={"type": "github", "developers": ["my-app[bot]"]})
        self.assertTrue(any("forge.developers" in e for e in errors), errors)

    def test_given_a_mirror_on_github_when_validated_then_it_is_an_error(self):
        self.assertTrue(any("mirror" in e and "forgejo" in e for e in self.errors(mirror={"github": "acme"})))
        errors = self.errors(forge={"type": "github"}, mirror={"github": "acme"})
        self.assertTrue(any("mirror" in e and "forgejo" in e for e in errors))

    def test_given_a_mirror_without_a_valid_github_owner_when_validated_then_it_is_an_error(self):
        errors = self.errors(forge=self.FORGEJO, mirror={"releases": True})
        self.assertTrue(any("mirror.github: required" in e for e in errors))
        self.assertTrue(any("mirror.github" in e for e in self.errors(forge=self.FORGEJO, mirror={"github": "a b"})))

    def test_given_a_mirror_with_a_bad_releases_when_validated_then_it_is_an_error(self):
        errors = self.errors(forge=self.FORGEJO, mirror={"github": "acme", "releases": "yes"})
        self.assertTrue(any("mirror.releases" in e for e in errors))

    def test_given_an_unknown_mirror_key_when_validated_then_it_is_an_error(self):
        errors = self.errors(forge=self.FORGEJO, mirror={"github": "acme", "tags": True})
        self.assertTrue(any("mirror" in e and "tags" in e for e in errors))

    def test_given_a_mirror_when_read_then_releases_default_to_true(self):
        v = catalog(forge=self.FORGEJO, mirror={"github": "acme"})
        self.assertEqual(c.validate(v), [])
        self.assertEqual(c.mirror(v), {"github": "acme", "releases": True})
        v["mirror"]["releases"] = False
        self.assertEqual(c.mirror(v), {"github": "acme", "releases": False})

    def test_given_no_mirror_when_read_then_it_is_none(self):
        self.assertIsNone(c.mirror(catalog(forge=self.FORGEJO)))

    def test_given_developers_when_read_then_they_are_listed(self):
        v = catalog(forge=dict(self.FORGEJO, developers=["claude"]))
        self.assertEqual(c.forge(v)["developers"], ["claude"])
        self.assertEqual(c.forge(v)["jenkinsUser"], "jenkins")

    def test_given_the_cli_when_asked_for_the_forge_type_then_it_prints_it(self):
        v = catalog(forge=dict(self.FORGEJO, jenkinsUser="ci"))
        self.assertEqual(self.run_cli(v, "forge", "type"), (0, "forgejo\n", ""))
        self.assertEqual(self.run_cli(v, "forge", "url")[1], "https://git.techcraftsmen.net\n")
        self.assertEqual(self.run_cli(v, "forge", "api")[1], "https://git.techcraftsmen.net/api/v1\n")
        self.assertEqual(self.run_cli(v, "forge", "jenkins-user")[1], "ci\n")

    def test_given_the_cli_when_asked_for_the_forge_then_it_prints_json(self):
        status, out, _ = self.run_cli(catalog(), "forge")
        self.assertEqual(status, 0)
        self.assertEqual(c.json.loads(out), c.forge(catalog()))

    def test_given_the_cli_when_asked_for_an_unknown_forge_field_then_it_fails(self):
        self.assertEqual(self.run_cli(catalog(), "forge", "token")[0], 2)

    def test_given_a_publish_agent_when_read_then_it_wins_over_the_release_environment(self):
        # staging (trigger branch) has an agent of its own, named by its id.
        v = catalog(forge=dict(self.FORGEJO, publishAgent="staging"))
        self.assertEqual(c.validate(v), [])
        self.assertEqual(c.forge(v)["publishAgent"], "staging")

    def test_given_a_publish_agent_on_the_ci_or_release_agent_when_validated_then_it_is_an_error(self):
        # ci runs the repositories' checks and release builds their files: neither may hold the write
        # token, and agents.groovy makes both whatever the catalog says.
        for name in ("ci", "release"):
            with self.subTest(name=name):
                errors = self.errors(forge=dict(self.FORGEJO, publishAgent=name))
                self.assertTrue(any("forge.publishAgent" in e and "ci and release" in e for e in errors), errors)

    def test_given_a_publish_agent_no_environment_has_when_validated_then_it_is_an_error(self):
        # agents.groovy makes an agent only for an environment's `agent` (or the id of one that deploys
        # by branch or release): a name it never makes would leave every release waiting for a node.
        # dev deploys by hand with no agent set, so it has none either.
        for name in ("publisher", "dev"):
            with self.subTest(name=name):
                errors = self.errors(forge=dict(self.FORGEJO, publishAgent=name))
                self.assertTrue(any("forge.publishAgent" in e and "not an environment's agent" in e
                                    for e in errors), errors)

    def test_given_an_application_override_naming_an_agent_when_validated_then_it_may_publish(self):
        v = catalog(forge=dict(self.FORGEJO, publishAgent="late"))
        v["systems"][0]["applications"][1]["environments"]["prod"]["agent"] = "late"
        self.assertEqual(c.validate(v), [])

    def test_given_a_malformed_catalog_and_a_publish_agent_when_validated_then_it_only_reports_errors(self):
        for broken in ({"environments": "x"}, {"environments": [3, {"id": 4}]}, {"systems": [{"applications": [3]}, 5]},
                       {"systems": [{"applications": [{"id": "a", "environments": "x"}]}]}):
            with self.subTest(broken=broken):
                self.assertTrue(self.errors(forge=dict(self.FORGEJO, publishAgent="vps"), **broken))

    def test_given_no_publish_agent_when_read_then_it_is_the_last_release_environments_agent(self):
        # prod (trigger release) is the only release environment; its agent is vps.
        self.assertEqual(c.forge(catalog())["publishAgent"], "vps")

    def test_given_several_applications_when_read_then_the_last_release_environment_in_catalog_order_wins(self):
        v = catalog()
        # shop-web is the last deployable application with a release environment: its override counts.
        v["systems"][0]["applications"][1]["environments"]["prod"]["agent"] = "late"
        self.assertEqual(c.forge(v)["publishAgent"], "late")

    def test_given_no_release_environment_when_read_then_there_is_no_publish_agent(self):
        v = catalog()
        v["environments"][2]["trigger"] = "manual"
        self.assertIsNone(c.forge(v)["publishAgent"])

    def test_given_a_release_environment_without_an_agent_when_read_then_its_id_is_the_agent(self):
        v = catalog()
        del v["environments"][2]["agent"]
        self.assertEqual(c.forge(v)["publishAgent"], "prod")

    def test_given_a_bad_publish_agent_when_validated_then_it_is_an_error(self):
        for bad in ("", "Bad Agent", 3, ["vps"]):
            with self.subTest(bad=bad):
                errors = self.errors(forge=dict(self.FORGEJO, publishAgent=bad))
                self.assertTrue(any("forge.publishAgent" in e for e in errors), errors)

    def test_given_the_cli_when_asked_for_the_publish_agent_then_it_prints_it(self):
        self.assertEqual(self.run_cli(catalog(), "forge", "publish-agent"), (0, "vps\n", ""))

    def test_given_the_cli_and_no_publish_agent_when_asked_then_it_exits_with_one(self):
        v = catalog()
        v["environments"][2]["trigger"] = "manual"
        status, out, err = self.run_cli(v, "forge", "publish-agent")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("no publish agent", err)

    def test_given_the_cli_when_asked_for_the_mirror_then_it_prints_it(self):
        v = catalog(forge=self.FORGEJO, mirror={"github": "acme", "releases": False})
        self.assertEqual(self.run_cli(v, "mirror", "github"), (0, "acme\n", ""))
        self.assertEqual(self.run_cli(v, "mirror", "releases")[1], "false\n")
        self.assertEqual(c.json.loads(self.run_cli(v, "mirror")[1]), {"github": "acme", "releases": False})

    def test_given_the_cli_and_no_mirror_when_asked_then_it_exits_with_one(self):
        status, out, err = self.run_cli(catalog(), "mirror")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("catalog.yaml has no mirror", err)


class RepositoryTests(unittest.TestCase):
    """`repositories`: git repositories that are not applications (libraries, CI-only ones, yggdrasil
    itself), and the application fields the site and the git server's rules read."""

    def errors(self, **changes):
        return c.validate(catalog(**changes))

    def app_errors(self, **fields):
        value = catalog()
        value["systems"][0]["applications"][1].update(fields)
        return c.validate(value)

    def test_given_a_repository_entry_when_read_then_its_defaults_are_filled(self):
        v = catalog(repositories=[{"id": "dotnet-util"}])
        self.assertEqual(c.repositories(v), [{"id": "dotnet-util", "checks": [], "actionsChecks": [],
                                              "credentials": [], "releaseCredentials": [], "release": "none", "tags": "v*",
                                              "rules": "release"}])

    def test_given_a_library_entry_when_read_then_its_tag_pattern_is_kept(self):
        v = catalog(repositories=[{"id": "dotnet-util", "release": "owner-tag", "tags": "[0-9]*"}])
        self.assertEqual(c.repositories(v)[0]["tags"], "[0-9]*")
        self.assertEqual(c.validate(v), [])

    def test_given_an_entry_with_every_field_when_read_then_they_are_kept(self):
        entry = {"id": "ci-only", "checks": ["build"], "actionsChecks": ["test"], "credentials": ["nuget-key"],
                 "releaseCredentials": ["sign-key"], "release": "owner-tag", "tags": "v*", "rules": "basic"}
        v = catalog(repositories=[entry])
        self.assertEqual(c.validate(v), [])
        self.assertEqual(c.repositories(v), [entry])

    def test_given_no_repositories_when_read_then_there_are_none(self):
        self.assertEqual(c.repositories(catalog()), [])

    def test_given_the_repository_catalog_when_read_then_it_has_no_repositories_yet(self):
        self.assertEqual(c.repositories(c.load()), [])

    def test_given_applications_and_repositories_when_names_are_asked_then_every_git_repository_is_listed_once(self):
        # What a tool that visits every repository (Renovate) needs: an application's `repository`, else its
        # id, once even when two applications share one; no platform component (it lives in this
        # repository's own entry); then the `repositories` entries.
        value = catalog(repositories=[{"id": "dotnet-util"}])
        value["systems"][0]["applications"][0]["repository"] = "shop"
        value["systems"][0]["applications"][1]["repository"] = "shop"
        self.assertEqual(c.repository_names(value), ["shop", "dotnet-util"])
        status, out, _ = run_cli(catalog(repositories=[{"id": "dotnet-util"}]), "repository-names")
        self.assertEqual((status, out), (0, "shop-api\nshop-web\ndotnet-util\n"))

    def test_given_no_repositories_when_names_are_asked_then_only_the_applications_are_listed(self):
        self.assertEqual(c.repository_names(catalog()), ["shop-api", "shop-web"])

    def test_given_repositories_that_is_not_a_list_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(e.startswith("repositories:") for e in self.errors(repositories={"id": "x"})))

    def test_given_an_entry_that_is_not_a_mapping_when_validated_then_it_is_an_error(self):
        self.assertTrue(any("repositories[0]" in e for e in self.errors(repositories=["x"])))

    def test_given_a_repository_with_a_bad_id_when_validated_then_it_is_an_error(self):
        for bad in (None, "", "a b", "a/b", 3):
            with self.subTest(bad=bad):
                self.assertTrue(any("repositories[0].id" in e for e in self.errors(repositories=[{"id": bad}])))

    def test_given_a_repository_named_like_an_application_when_validated_then_it_is_an_error(self):
        # Each names a Jenkins job (the Job DSL): two jobs of one name cannot both exist.
        value = catalog(repositories=[{"id": "shop-frontend"}, {"id": "shop-web"}])
        value["systems"][0]["applications"][1]["repository"] = "shop-frontend"
        errors = c.validate(value)
        # shop-frontend is shop-web's repository, which a repositories entry may share; shop-web is the
        # application's id, which no repositories entry may take.
        self.assertEqual([e for e in errors if "repositories[" in e],
                         ["repositories[1].id: shop-web is already an application's id (each names a Jenkins "
                          "job): rename one of them"])

    def test_given_a_library_and_its_docs_application_on_one_repository_when_validated_then_it_is_valid(self):
        value = catalog(repositories=[{"id": "dotnet-util", "release": "owner-tag", "tags": "[0-9]*",
                                       "checks": ["unit", "coverage"]}])
        value["systems"][0]["applications"].append(
            {"id": "dotnet-util-docs", "name": "Docs", "kind": "web", "health": "http://dotnet-util-docs:8080/",
             "repository": "dotnet-util", "checks": [], "environments": {"staging": {}}})
        self.assertEqual(c.validate(value), [])
        self.assertEqual(c.repository_names(value), ["shop-api", "shop-web", "dotnet-util"])

    def test_given_an_application_and_its_docs_application_on_one_repository_when_validated_then_it_is_valid(self):
        # heimdall-api releases (prod: trigger release); its docs site deploys on a branch only.
        value = catalog()
        value["systems"][0]["applications"].append(
            {"id": "shop-api-docs", "name": "Docs", "kind": "web", "health": "http://shop-api-docs:8080/",
             "repository": "shop-api", "environments": {"staging": {}}})
        self.assertEqual(c.validate(value), [])
        self.assertEqual(c.repository_names(value), ["shop-api", "shop-web"])

    def test_given_two_releasing_entries_on_one_repository_when_validated_then_it_is_an_error_naming_both(self):
        # Who merges main and who creates the version tags would be two answers for one repository.
        both_apps = catalog()
        both_apps["systems"][0]["applications"][1]["repository"] = "shop-api"  # shop-web releases on prod
        app_and_library = catalog(repositories=[{"id": "shop-web", "release": "owner-tag"}])
        app_and_library["systems"][0]["applications"][1]["id"] = "shop-site"
        app_and_library["systems"][0]["applications"][1]["repository"] = "shop-web"
        for value, wanted in (
                (both_apps, "repository shop-api: application shop-api and application shop-web both release"),
                (app_and_library, "repository shop-web: application shop-site and repositories entry shop-web "
                                  "both release")):
            with self.subTest(wanted=wanted):
                errors = [e for e in c.validate(value) if "both release" in e]
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith(wanted), errors)

    def test_given_one_releasing_entry_and_a_repositories_entry_without_releases_when_validated_then_it_is_valid(self):
        # release: none releases nothing: shop-web (prod, trigger release) is the one releasing entry.
        value = catalog(repositories=[{"id": "shop-frontend", "checks": ["lint"]}])
        value["systems"][0]["applications"][1]["repository"] = "shop-frontend"
        self.assertEqual(c.validate(value), [])
        value["repositories"][0]["release"] = "owner-tag"
        self.assertTrue(any("repository shop-frontend: application shop-web and repositories entry shop-frontend "
                            "both release" in e for e in c.validate(value)))

    def test_given_two_repositories_with_the_same_id_when_validated_then_it_is_an_error(self):
        errors = self.errors(repositories=[{"id": "dotnet-util"}, {"id": "dotnet-util"}])
        self.assertTrue(any("repositories[1].id" in e and "duplicate" in e for e in errors), errors)

    def test_given_a_repository_with_a_dot_when_validated_then_it_is_valid(self):
        self.assertEqual(self.errors(repositories=[{"id": "reveal.js"}]), [])

    def test_given_an_unknown_release_kind_when_validated_then_it_is_an_error(self):
        errors = self.errors(repositories=[{"id": "lib", "release": "tag"}])
        self.assertTrue(any("repositories[0].release" in e and "owner-tag" in e for e in errors), errors)

    def test_given_an_unknown_rules_kind_when_validated_then_it_is_an_error(self):
        errors = self.errors(repositories=[{"id": "lib", "rules": "strict"}])
        self.assertTrue(any("repositories[0].rules" in e for e in errors), errors)

    def test_given_a_bad_tag_pattern_when_validated_then_it_is_an_error(self):
        for bad in ("", "v* ", "v/*", 3, None):
            with self.subTest(bad=bad):
                errors = self.errors(repositories=[{"id": "lib", "release": "owner-tag", "tags": bad}])
                self.assertTrue(any("repositories[0].tags" in e for e in errors), errors)

    def test_given_bad_check_lists_when_validated_then_each_is_an_error(self):
        errors = self.errors(repositories=[{"id": "lib", "checks": ["ok", " "], "actionsChecks": "test",
                                            "credentials": ["a b"], "releaseCredentials": "sign-key"}])
        for field in ("checks", "actionsChecks", "credentials", "releaseCredentials"):
            self.assertTrue(any(f"repositories[0].{field}" in e for e in errors), (field, errors))

    def test_given_an_unknown_repository_key_when_validated_then_it_is_an_error(self):
        errors = self.errors(repositories=[{"id": "lib", "branch": "main"}])
        self.assertTrue(any("repositories[0]" in e and "branch" in e for e in errors), errors)

    def test_given_a_number_as_a_repository_or_option_key_when_validated_then_it_is_reported_not_raised(self):
        value = catalog(repositories=[{"id": "lib", 3: "x", "branch": "main"}])
        value["environments"][0][4] = "y"
        value["environments"][0]["replicas"] = 2
        errors = c.validate(value)
        self.assertIn("repositories[0]: unknown key(s) [3, 'branch']", errors)
        self.assertIn("environment 'dev': unknown option(s) [4, 'replicas']", errors)

    def test_given_the_application_site_fields_when_validated_then_they_are_valid(self):
        self.assertEqual(self.app_errors(domain="shop.example.com", path="/shop/app", apex=True,
                                         actionsChecks=["test"], credentials=["nuget-key", "a.b_c-1"]), [])

    def test_given_an_application_path_without_a_leading_slash_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(".path" in e for e in self.app_errors(path="shop")))

    def test_given_an_application_path_with_a_trailing_slash_when_validated_then_it_is_an_error(self):
        for bad in ("/shop/", "/"):
            with self.subTest(bad=bad):
                self.assertTrue(any(".path" in e for e in self.app_errors(path=bad)))

    def test_given_an_application_path_with_dot_dot_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(".path" in e for e in self.app_errors(path="/a/../b")))

    def test_given_an_application_domain_without_a_dot_when_validated_then_it_is_an_error(self):
        for bad in ("localhost", "Shop.example.com", "a b.com", 3):
            with self.subTest(bad=bad):
                self.assertTrue(any(".domain" in e for e in self.app_errors(domain=bad)))

    def test_given_an_apex_that_is_not_a_boolean_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(".apex" in e for e in self.app_errors(apex="yes")))

    def test_given_a_credential_id_with_a_space_when_validated_then_it_is_an_error(self):
        for bad in ("nuget key", "-key", "", 3):
            with self.subTest(bad=bad):
                self.assertTrue(any(".credentials" in e for e in self.app_errors(credentials=[bad])))
        self.assertTrue(any(".credentials" in e for e in self.app_errors(credentials="nuget-key")))

    def test_given_release_credentials_when_validated_then_they_are_credential_ids(self):
        self.assertEqual(self.app_errors(releaseCredentials=["sign-key"]), [])
        for bad in (["sign key"], "sign-key", [3]):
            with self.subTest(bad=bad):
                self.assertTrue(any(".releaseCredentials" in e for e in self.app_errors(releaseCredentials=bad)))

    def test_given_an_id_in_both_credential_lists_when_validated_then_it_is_an_error(self):
        # One id in both would let any branch's check bind what only the release build may.
        errors = self.app_errors(credentials=["nuget-key", "sign-key"], releaseCredentials=["sign-key"])
        self.assertTrue(any(".releaseCredentials" in e and "'sign-key'" in e and "also in credentials" in e
                            for e in errors), errors)
        errors = self.errors(repositories=[{"id": "lib", "credentials": ["k"], "releaseCredentials": ["k"]}])
        self.assertTrue(any("repositories[0].releaseCredentials" in e and "also in credentials" in e
                            for e in errors), errors)

    def test_given_bad_actions_checks_when_validated_then_it_is_an_error(self):
        self.assertTrue(any(".actionsChecks" in e for e in self.app_errors(actionsChecks=["test", ""])))
        self.assertTrue(any(".actionsChecks" in e for e in self.app_errors(actionsChecks="test")))

    def test_given_the_cli_when_asked_for_the_repositories_then_it_prints_their_ids(self):
        v = catalog(repositories=[{"id": "dotnet-util"}, {"id": "reveal.js"}])
        self.assertEqual(run_cli(v, "repositories"), (0, "dotnet-util\nreveal.js\n", ""))

    def test_given_the_cli_and_no_repositories_when_asked_then_it_prints_nothing(self):
        self.assertEqual(run_cli(catalog(), "repositories"), (0, "", ""))

    def test_given_the_cli_when_asked_for_a_repository_entry_then_it_prints_it_as_json(self):
        v = catalog(repositories=[{"id": "dotnet-util", "release": "owner-tag", "tags": "[0-9]*"}])
        status, out, _ = run_cli(v, "repository-entry", "dotnet-util")
        self.assertEqual(status, 0)
        self.assertEqual(c.json.loads(out), c.repositories(v)[0])
        self.assertEqual(c.json.loads(out)["tags"], "[0-9]*")

    def test_given_the_cli_when_asked_for_an_unknown_repository_entry_then_it_exits_with_one(self):
        status, out, err = run_cli(catalog(repositories=[{"id": "a"}]), "repository-entry", "b")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("'b' is not a repository in catalog.yaml", err)

    def test_given_the_cli_when_asked_for_a_repository_entry_without_an_id_then_it_is_a_usage_error(self):
        self.assertEqual(run_cli(catalog(), "repository-entry")[0], 2)


class PublicHostTests(unittest.TestCase):
    """public-host and public-path: the full host name and URL path a site's stack file needs."""

    def site(self, **fields):
        """BASE with one more application, `site`, and a -dev suffix on the dev environment."""
        value = catalog()
        value["environments"][0]["hostSuffix"] = "-dev"
        value["systems"][0]["applications"].append(
            {"id": "site", "name": "Site", "kind": "web", "health": "http://site:8080/healthz", **fields})
        return value

    def ask(self, value, *argv):
        return run_cli(value, *argv)

    def test_given_a_host_and_a_suffix_when_asked_then_the_host_is_under_the_default_domain(self):
        value = self.site(host="docs")
        self.assertEqual(self.ask(value, "public-host", "site", "dev", "example.com"), (0, "docs-dev.example.com\n", ""))
        self.assertEqual(self.ask(value, "public-host", "site", "prod", "example.com"), (0, "docs.example.com\n", ""))

    def test_given_a_domain_when_asked_then_the_host_is_under_it(self):
        value = self.site(host="docs", domain="sites.example.org")
        self.assertEqual(self.ask(value, "public-host", "site", "dev", "example.com"),
                         (0, "docs-dev.sites.example.org\n", ""))

    def test_given_an_environment_override_of_the_suffix_when_asked_then_the_override_wins(self):
        value = self.site(host="docs", environments={"dev": {"hostSuffix": "-x"}})
        self.assertEqual(self.ask(value, "public-host", "site", "dev", "example.com")[1], "docs-x.example.com\n")

    def test_given_apex_without_host_when_asked_then_it_is_the_bare_domain(self):
        value = self.site(apex=True)
        self.assertEqual(self.ask(value, "public-host", "site", "dev", "example.com"), (0, "example.com\n", ""))
        value = self.site(apex=True, domain="docs.example.org")
        self.assertEqual(self.ask(value, "public-host", "site", "dev", "example.com"), (0, "docs.example.org\n", ""))

    def test_given_apex_with_a_host_when_asked_then_the_host_wins(self):
        value = self.site(apex=True, host="docs")
        self.assertEqual(self.ask(value, "public-host", "site", "prod", "example.com")[1], "docs.example.com\n")

    def test_given_neither_host_nor_apex_when_asked_then_nothing_is_printed(self):
        self.assertEqual(self.ask(self.site(), "public-host", "site", "dev", "example.com"), (0, "", ""))

    def test_given_no_domain_at_all_when_asked_then_nothing_is_printed(self):
        value = self.site(host="docs")
        self.assertEqual(self.ask(value, "public-host", "site", "dev", ""), (0, "", ""))

    def test_given_an_unknown_application_or_environment_when_asked_then_it_is_an_error(self):
        value = self.site(host="docs")
        self.assertEqual(self.ask(value, "public-host", "nope", "dev", "example.com")[0], 1)
        status, out, err = self.ask(value, "public-host", "site", "nope", "example.com")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("does not deploy to 'nope'", err)

    def test_given_the_wrong_number_of_arguments_when_asked_then_it_is_a_usage_error(self):
        self.assertEqual(self.ask(self.site(), "public-host", "site", "dev")[0], 2)
        self.assertEqual(self.ask(self.site(), "public-path")[0], 2)

    def test_given_a_path_when_asked_then_it_is_printed(self):
        self.assertEqual(self.ask(self.site(path="/docs"), "public-path", "site"), (0, "/docs\n", ""))

    def test_given_no_path_when_asked_then_nothing_is_printed(self):
        self.assertEqual(self.ask(self.site(), "public-path", "site"), (0, "", ""))


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
