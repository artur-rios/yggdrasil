"""Tests for scripts/rules.py (the model).   python3 -m unittest discover -s scripts"""

import copy
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import threading
import unittest
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

    def test_given_an_owner_tag_repository_when_built_then_only_admins_create_its_tags(self):
        tags = ruleset(rules.github_rulesets(by_repo()["toolkit"]), "release tags")
        self.assertEqual(tags["conditions"]["ref_name"]["include"], ["refs/tags/[0-9]*"])
        self.assertEqual([r["type"] for r in tags["rules"]], ["creation", "update", "deletion"])
        self.assertEqual(tags["bypass_actors"], rules.GITHUB_ADMIN_BYPASS)

    def test_given_an_application_when_built_then_its_tags_may_still_be_created_by_jenkins(self):
        # Jenkins creates v* tags itself, so no `creation` rule: it would lock Jenkins out.
        tags = ruleset(rules.github_rulesets(by_repo()["shop-api"]), "release tags")
        self.assertEqual(tags["conditions"]["ref_name"]["include"], ["refs/tags/v*"])
        self.assertEqual([r["type"] for r in tags["rules"]], ["update", "deletion"])

    def test_given_a_basic_repository_when_built_then_only_deletion_and_force_push_are_blocked(self):
        payloads = rules.github_rulesets(by_repo()["scratch"])
        self.assertEqual([p["name"] for p in payloads], ["develop", "main"])
        for payload in payloads:
            self.assertEqual([r["type"] for r in payload["rules"]], ["deletion", "non_fast_forward"])

    def test_given_a_basic_repository_when_applied_then_no_tags_ruleset_is_sent(self):
        api = FakeGitHub()
        rules.apply_github(api, "artur-rios", by_repo()["scratch"], dry_run=False)
        self.assertEqual([b["name"] for m, _, b in api.writes() if m == "POST"], ["develop", "main"])

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

    def test_given_a_forgejo_catalog_when_run_then_the_command_says_that_side_is_not_there_yet(self):
        err = io.StringIO()
        with mock.patch.object(rules.catalog_module, "load", return_value=CATALOG), mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as raised:
                rules.main(["--dry-run"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Forgejo side arrives in a later task", err.getvalue())

    def test_given_todays_catalog_when_built_then_only_the_binding_changed(self):
        # The fixture is what github/rulesets.py built before it became a shim, for a catalog of two
        # applications: shop-api (checks test and docker, a production release environment) and
        # storefront (neither). The one difference: Jenkins' contexts no longer carry integration_id.
        fixture = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                              "github-rulesets-before-plan-2.json").read_text(encoding="utf-8"))
        catalog = copy.deepcopy(CATALOG_GITHUB)
        del catalog["repositories"]
        catalog["systems"][0]["applications"][0]["actionsChecks"] = []
        built = {r.repo: rules.github_rulesets(r) for r in rules.desired_rules(catalog)}
        self.assertEqual(sorted(built), sorted(fixture))
        for repo, old in fixture.items():
            for payload in old:
                for rule in payload["rules"]:
                    if rule["type"] == "required_status_checks":
                        for check in rule["parameters"]["required_status_checks"]:
                            check.pop("integration_id", None)
            self.assertEqual(built[repo], old, repo)


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
