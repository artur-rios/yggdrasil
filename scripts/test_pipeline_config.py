"""Tests for scripts/pipeline_config.py.   python3 -m unittest discover -s scripts -p test_pipeline_config.py"""

import contextlib
import copy
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import catalog as cat
import pipeline_config as pc

CATALOG = {
    "owner": "someone",
    "forge": {"type": "forgejo", "url": "https://git.example.com", "publishAgent": "release"},
    "environments": [
        {"id": "dev", "name": "Dev", "trigger": "manual"},
        {"id": "prod", "name": "Prod", "trigger": "release", "agent": "vps"},
    ],
    "systems": [
        {"id": "shop", "name": "Shop", "applications": [
            {"id": "shop-api", "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz",
             "checks": ["unit", "lint"], "actionsChecks": ["windows"], "credentials": ["nuget-key", "sign-key"]},
            {"id": "shop-site", "name": "Site", "kind": "web", "health": "http://shop-site:8080/healthz",
             "repository": "shop-frontend", "environments": {"dev": {}}},
        ]},
    ],
    "repositories": [
        {"id": "dotnet-util", "release": "owner-tag", "tags": "[0-9]*", "checks": ["unit"],
         "credentials": ["nuget-key"]},
        {"id": "shop-specs", "checks": ["lint"]},
    ],
}

# Every field of the schema once: the file the validator must accept.
FULL = """\
checks:
  unit:
    image: mcr.microsoft.com/dotnet/sdk:10.0
    run: ci/test.sh
    docker: true
    credentials:
      NUGET_KEY: nuget-key
    paths: ["src/**", "tests/**"]
    junit: "**/TestResults/*.xml"
    archive: "out/**"
  lint:
    image: node:22
    run: npm run lint
branchPolicy:
  image: alpine/git
  run: scripts/branch-policy.sh
deploy:
  inputs: [unit]
release:
  build:
    image: debian:12
    run: ci/release.sh
    files: ["dist/*.zip"]
    credentials:
      SIGN_KEY: sign-key
  windows:
    workflow: windows.yml
    files: ["Setup.exe"]
  validate:
    image: debian:12
    run: ci/validate.sh
publish:
  nuget:
    image: mcr.microsoft.com/dotnet/sdk:10.0
    run: ci/pack.sh
    packages: "out/*.nupkg"
"""


def catalog():
    return copy.deepcopy(CATALOG)


def repo(text=None):
    """A scratch repository directory, with yggdrasil.yml when `text` is given."""
    directory = pathlib.Path(tempfile.mkdtemp(prefix="ygg-pipeline-"))
    if text is not None:
        (directory / "yggdrasil.yml").write_text(text, encoding="utf-8")
    return directory


def parsed(text):
    return pc.load_config(repo(text))


def run_cli(*argv):
    """main(argv) against CATALOG instead of the repository's catalog: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    out.reconfigure = lambda **_: None  # main() asks stdout for plain LF line endings
    with mock.patch.object(cat, "load", return_value=catalog()), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = pc.main(list(argv))
    return status, out.getvalue(), err.getvalue()


class LoadTests(unittest.TestCase):
    def test_given_no_yggdrasil_yml_when_loaded_then_there_is_no_config(self):
        self.assertIsNone(pc.load_config(repo()))

    def test_given_a_file_with_every_field_when_loaded_then_it_is_returned(self):
        config = parsed(FULL)
        self.assertEqual(config["checks"]["unit"]["credentials"], {"NUGET_KEY": "nuget-key"})
        self.assertEqual(config["deploy"], {"inputs": ["unit"]})
        self.assertEqual(config["publish"]["nuget"]["packages"], "out/*.nupkg")

    def test_given_an_empty_file_when_loaded_then_it_is_a_config_with_nothing_in_it(self):
        self.assertEqual(parsed(""), {})

    def test_given_a_check_with_an_unknown_field_when_loaded_then_it_names_the_field(self):
        with self.assertRaises(pc.ConfigError) as caught:
            parsed("checks:\n  unit:\n    image: x\n    run: y\n    dockr: true\n")
        self.assertIn("checks.unit.dockr", str(caught.exception))

    def test_given_an_unknown_key_at_any_level_when_loaded_then_it_names_the_field(self):
        for text, field in (
            ("tests: {}\n", "tests"),
            ("branchPolicy:\n  image: x\n  run: y\n  also: z\n", "branchPolicy.also"),
            ("deploy:\n  input: [a]\n", "deploy.input"),
            ("release:\n  sign: {}\n", "release.sign"),
            ("release:\n  build:\n    image: x\n    run: y\n    file: [a]\n", "release.build.file"),
            ("release:\n  windows:\n    workflow: w\n    flow: z\n", "release.windows.flow"),
            ("release:\n  validate:\n    image: x\n    run: y\n    files: [a]\n", "release.validate.files"),
            ("publish:\n  npm: {}\n", "publish.npm"),
            ("publish:\n  nuget:\n    image: x\n    run: y\n    package: a\n", "publish.nuget.package"),
        ):
            with self.subTest(field=field), self.assertRaises(pc.ConfigError) as caught:
                parsed(text)
            self.assertIn(field, str(caught.exception))

    def test_given_wrong_types_when_loaded_then_each_names_its_field(self):
        for text, field in (
            ("checks: [unit]\n", "checks"),
            ("checks:\n  unit: ci/test.sh\n", "checks.unit"),
            ("checks:\n  unit:\n    image: 3\n    run: y\n", "checks.unit.image"),
            ("checks:\n  unit:\n    image: x\n    run: [a, b]\n", "checks.unit.run"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    docker: yes please\n", "checks.unit.docker"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    paths: src\n", "checks.unit.paths"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    paths: [3]\n", "checks.unit.paths"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    junit: [a]\n", "checks.unit.junit"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    credentials: [a]\n", "checks.unit.credentials"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    credentials: {KEY: 3}\n", "checks.unit.credentials.KEY"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    credentials: {bad key: a}\n",
             "checks.unit.credentials"),
            ("checks:\n  unit:\n    image: x\n    run: y\n    credentials: {KEY: a b}\n",
             "checks.unit.credentials.KEY"),
            ("branchPolicy: scripts/branch-policy.sh\n", "branchPolicy"),
            ("deploy:\n  inputs: unit\n", "deploy.inputs"),
            ("deploy: [unit]\n", "deploy"),
            ("release:\n  build:\n    image: x\n    run: y\n    files: dist\n", "release.build.files"),
            ("release:\n  windows:\n    workflow: [w]\n", "release.windows.workflow"),
            ("publish:\n  nuget:\n    image: x\n    run: y\n    packages: [a]\n", "publish.nuget.packages"),
            ("- checks\n", "yggdrasil.yml"),
        ):
            with self.subTest(field=field), self.assertRaises(pc.ConfigError) as caught:
                parsed(text)
            self.assertIn(field, str(caught.exception))

    def test_given_missing_required_fields_when_loaded_then_each_is_named(self):
        for text, field in (
            ("checks:\n  unit:\n    run: y\n", "checks.unit.image"),
            ("checks:\n  unit:\n    image: x\n", "checks.unit.run"),
            ("branchPolicy:\n  image: x\n", "branchPolicy.run"),
            ("release:\n  build:\n    run: y\n", "release.build.image"),
            ("release:\n  windows:\n    files: [a]\n", "release.windows.workflow"),
            ("release:\n  validate:\n    image: x\n", "release.validate.run"),
            ("publish:\n  nuget:\n    image: x\n", "publish.nuget.run"),
        ):
            with self.subTest(field=field), self.assertRaises(pc.ConfigError) as caught:
                parsed(text)
            self.assertIn(field, str(caught.exception))

    def test_given_several_problems_when_loaded_then_all_are_reported(self):
        with self.assertRaises(pc.ConfigError) as caught:
            parsed("checks:\n  unit:\n    image: x\n    run: y\n    dockr: true\nbranchPolicy:\n  image: x\n")
        self.assertEqual(len(caught.exception.errors), 2, caught.exception.errors)

    def test_given_invalid_yaml_when_loaded_then_it_is_a_config_error(self):
        with self.assertRaises(pc.ConfigError) as caught:
            parsed("checks: [unclosed\n")
        self.assertIn("yggdrasil.yml", str(caught.exception))

    def test_given_yaml_that_is_not_plain_data_when_loaded_then_it_is_refused(self):
        # yaml.safe_load must be in use: a python tag may not construct objects.
        with self.assertRaises(pc.ConfigError):
            parsed("checks: !!python/object/apply:os.getcwd []\n")


class ValidateAgainstTests(unittest.TestCase):
    def entry(self, job_id="shop-api"):
        return pc.entry_for(catalog(), job_id)

    def errors(self, text, job_id="shop-api"):
        return pc.validate_against(parsed(text), self.entry(job_id))

    def test_given_a_file_matching_the_catalog_when_validated_then_there_are_no_errors(self):
        self.assertEqual(self.errors(FULL.split("publish:")[0]), [])

    def test_given_a_credential_the_catalog_does_not_allow_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  unit:\n    image: x\n    run: y\n    credentials:\n      TOKEN: github-app\n"
                             "  lint:\n    image: x\n    run: y\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("checks.unit.credentials.TOKEN", errors[0])
        self.assertIn("github-app", errors[0])

    def test_given_a_release_credential_the_catalog_does_not_allow_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                             "release:\n  build:\n    image: x\n    run: y\n    credentials: {KEY: forge-write}\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("release.build.credentials.KEY", errors[0])

    def test_given_an_entry_without_credentials_when_validated_then_any_credential_is_an_error(self):
        errors = self.errors("checks:\n  lint:\n    image: x\n    run: y\n    credentials: {KEY: nuget-key}\n",
                             "shop-specs")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("checks.lint.credentials.KEY", errors[0])

    def test_given_a_catalog_check_missing_from_the_file_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  unit:\n    image: x\n    run: y\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("lint", errors[0])

    def test_given_a_file_without_checks_when_the_catalog_requires_some_then_each_is_an_error(self):
        self.assertEqual(len(self.errors("")), 2)

    def test_given_deploy_inputs_naming_an_unknown_check_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                             "deploy:\n  inputs: [unit, build]\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("deploy.inputs", errors[0])
        self.assertIn("build", errors[0])

    def test_given_publish_on_an_application_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                             "publish:\n  nuget: {image: x, run: y}\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("publish", errors[0])

    def test_given_publish_on_a_repository_that_is_not_owner_tagged_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  lint: {image: x, run: y}\npublish:\n  nuget: {image: x, run: y}\n",
                             "shop-specs")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("publish", errors[0])

    def test_given_publish_on_an_owner_tagged_repository_when_validated_then_it_is_valid(self):
        self.assertEqual(self.errors("checks:\n  unit: {image: x, run: y}\n"
                                     "publish:\n  nuget: {image: x, run: y}\n", "dotnet-util"), [])

    def test_given_release_on_a_repository_with_no_releases_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  lint: {image: x, run: y}\n"
                             "release:\n  build: {image: x, run: y}\n", "shop-specs")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("release", errors[0])

    def test_given_release_on_an_application_without_a_release_environment_when_validated_then_it_is_an_error(self):
        errors = pc.validate_against(parsed("release:\n  build: {image: x, run: y}\n"), self.entry("shop-site"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("release", errors[0])

    def test_given_release_on_a_deploying_application_or_an_owner_tagged_repository_when_validated_then_it_is_valid(self):
        text = "checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\nrelease:\n  build: {image: x, run: y}\n"
        self.assertEqual(self.errors(text), [])
        self.assertEqual(self.errors("checks:\n  unit: {image: x, run: y}\nrelease:\n  build: {image: x, run: y}\n",
                                     "dotnet-util"), [])


class EntryTests(unittest.TestCase):
    def test_given_a_library_entry_when_described_then_it_is_owner_tagged_with_its_pattern(self):
        self.assertEqual(pc.entry_for(catalog(), "dotnet-util"), {
            "kind": "repository", "id": "dotnet-util", "repository": "dotnet-util",
            "credentials": ["nuget-key"], "checks": ["unit"], "actionsChecks": [],
            "release": "owner-tag", "tags": "[0-9]*"})

    def test_given_a_repository_without_a_release_when_described_then_it_releases_nothing(self):
        entry = pc.entry_for(catalog(), "shop-specs")
        self.assertEqual((entry["release"], entry["tags"]), ("none", "v*"))

    def test_given_an_application_with_a_release_environment_when_described_then_it_deploys_on_release(self):
        self.assertEqual(pc.entry_for(catalog(), "shop-api"), {
            "kind": "application", "id": "shop-api", "repository": "shop-api",
            "credentials": ["nuget-key", "sign-key"], "checks": ["unit", "lint"], "actionsChecks": ["windows"],
            "release": "deploy", "tags": "v*"})

    def test_given_an_application_without_a_release_environment_when_described_then_it_releases_nothing(self):
        entry = pc.entry_for(catalog(), "shop-site")
        self.assertEqual((entry["release"], entry["repository"]), ("none", "shop-frontend"))
        self.assertEqual((entry["checks"], entry["credentials"], entry["actionsChecks"]), ([], [], []))

    def test_given_an_unknown_job_when_described_then_it_is_a_config_error(self):
        with self.assertRaises(pc.ConfigError) as caught:
            pc.entry_for(catalog(), "nope")
        self.assertIn("nope", str(caught.exception))


class PublishAgentTests(unittest.TestCase):
    def test_given_no_publish_agent_when_read_then_it_is_the_last_release_environments_agent(self):
        value = catalog()
        del value["forge"]["publishAgent"]
        self.assertEqual(cat.forge(value)["publishAgent"], "vps")
        value["forge"]["publishAgent"] = "release"
        self.assertEqual(cat.forge(value)["publishAgent"], "release")


class CliTests(unittest.TestCase):
    def test_given_the_cli_when_describing_then_it_prints_one_json_document(self):
        status, out, err = run_cli("describe", "dotnet-util", str(repo("checks:\n  unit: {image: x, run: y}\n")))
        self.assertEqual((status, err), (0, ""))
        document = json.loads(out)  # one document, nothing else on stdout
        self.assertEqual(set(document), {"entry", "config", "errors"})
        self.assertEqual(document["entry"]["release"], "owner-tag")
        self.assertEqual(document["config"]["checks"]["unit"]["run"], "y")
        self.assertEqual(document["errors"], [])

    def test_given_a_repository_without_a_file_when_described_then_the_config_is_null_and_it_is_not_an_error(self):
        status, out, _ = run_cli("describe", "shop-api", str(repo()))
        self.assertEqual(status, 0)
        document = json.loads(out)
        self.assertIsNone(document["config"])
        self.assertEqual(document["errors"], [])

    def test_given_a_file_that_breaks_the_catalog_rules_when_described_then_the_errors_are_listed_and_it_exits_with_one(self):
        status, out, _ = run_cli("describe", "shop-api", str(repo("checks:\n  unit: {image: x, run: y}\n")))
        self.assertEqual(status, 1)
        document = json.loads(out)
        self.assertEqual(document["entry"]["id"], "shop-api")
        self.assertTrue(any("lint" in e for e in document["errors"]), document)

    def test_given_an_invalid_file_when_described_then_the_config_is_null_and_it_exits_with_one(self):
        status, out, _ = run_cli("describe", "shop-api", str(repo("checks:\n  unit:\n    dockr: true\n")))
        self.assertEqual(status, 1)
        document = json.loads(out)
        self.assertIsNone(document["config"])
        self.assertTrue(any("checks.unit.dockr" in e for e in document["errors"]), document)

    def test_given_an_unknown_job_when_described_then_it_exits_with_one_and_says_so(self):
        status, out, err = run_cli("describe", "nope", str(repo()))
        self.assertEqual((status, out), (1, ""))
        self.assertIn("nope", err)

    def test_given_a_missing_directory_when_described_then_it_exits_with_one(self):
        status, out, err = run_cli("describe", "shop-api", "/no/such/directory")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("/no/such/directory", err)

    def test_given_the_wrong_arguments_when_run_then_it_is_a_usage_error(self):
        self.assertEqual(run_cli()[0], 2)
        self.assertEqual(run_cli("describe", "shop-api")[0], 2)


if __name__ == "__main__":
    unittest.main()
