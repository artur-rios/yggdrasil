"""Tests for scripts/pipeline_config.py.   python3 -m unittest discover -s scripts -p test_pipeline_config.py"""

import contextlib
import copy
import io
import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock

import catalog as cat
import pipeline_config as pc

CATALOG = {
    "owner": "someone",
    "forge": {"type": "forgejo", "url": "https://git.example.com", "publishAgent": "vps"},
    "environments": [
        {"id": "dev", "name": "Dev", "trigger": "manual"},
        {"id": "prod", "name": "Prod", "trigger": "release", "agent": "vps"},
    ],
    "systems": [
        {"id": "shop", "name": "Shop", "applications": [
            {"id": "shop-api", "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz",
             "checks": ["unit", "lint"], "actionsChecks": ["windows"], "credentials": ["nuget-key"],
             "releaseCredentials": ["sign-key"]},
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
  finalize:
    image: debian:12
    run: ci/sign.sh
    files: ["signed/*"]
    credentials:
      SIGN_KEY: sign-key
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
        self.assertEqual(config["release"]["finalize"], {"image": "debian:12", "run": "ci/sign.sh",
                                                         "files": ["signed/*"], "credentials": {"SIGN_KEY": "sign-key"}})

    def test_given_a_finalize_step_with_only_its_image_and_run_when_loaded_then_it_is_valid(self):
        # files and credentials are optional, as for release.build: a step that adds nothing is allowed.
        config = parsed("release:\n  finalize: {image: debian:12, run: ci/sign.sh}\n")
        self.assertEqual(config["release"]["finalize"], {"image": "debian:12", "run": "ci/sign.sh"})

    def test_given_an_empty_file_when_loaded_then_it_is_a_config_with_nothing_in_it(self):
        self.assertEqual(parsed(""), {})

    def test_given_empty_paths_when_loaded_then_it_names_the_field(self):
        # An empty list reads as "skip always" or "run always" depending on who looks: refuse it.
        with self.assertRaises(pc.ConfigError) as caught:
            parsed("checks:\n  unit:\n    image: x\n    run: y\n    paths: []\n")
        self.assertIn("checks.unit.paths: must list at least one glob", str(caught.exception))

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
            # finalize's shape is build's: no `paths`, no `docker`, nothing a check has besides.
            ("release:\n  finalize:\n    image: x\n    run: y\n    file: [a]\n", "release.finalize.file"),
            ("release:\n  finalize:\n    image: x\n    run: y\n    docker: true\n", "release.finalize.docker"),
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
            # finalize's files are validated as build's: a list of non-empty strings.
            ("release:\n  finalize:\n    image: x\n    run: y\n    files: signed\n", "release.finalize.files"),
            ("release:\n  finalize:\n    image: x\n    run: y\n    files: [\"\"]\n", "release.finalize.files"),
            ("release:\n  finalize:\n    image: x\n    run: y\n    credentials: [a]\n", "release.finalize.credentials"),
            ("release:\n  finalize:\n    image: x\n    run: y\n    credentials: {KEY: a b}\n",
             "release.finalize.credentials.KEY"),
            ("release:\n  finalize: ci/sign.sh\n", "release.finalize"),
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
            ("release:\n  finalize:\n    run: y\n", "release.finalize.image"),
            ("release:\n  finalize:\n    image: x\n    files: [a]\n", "release.finalize.run"),
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


class TemplateTests(unittest.TestCase):
    """templates/application/yggdrasil.yml, what an application repository starts from."""

    TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "templates" / "application"

    def test_given_the_template_when_loaded_then_it_is_a_valid_pipeline_file(self):
        config = pc.load_config(self.TEMPLATE)
        self.assertEqual(list(config["checks"]), ["test"])
        self.assertEqual(config["checks"]["test"]["run"], "ci/test.sh")

    def test_given_every_optional_line_of_the_template_when_uncommented_then_it_is_still_valid(self):
        # Below its header, a commented line is an example field: "<indent># <yaml>". Uncommented, they must
        # all stay valid, or the template teaches a field the pipeline refuses.
        lines = (self.TEMPLATE / "yggdrasil.yml").read_text(encoding="utf-8").splitlines()
        body = lines[lines.index("checks:"):]
        text = "\n".join(re.sub(r"^(\s*)# ", r"\1", line) for line in body) + "\n"
        config = parsed(text)
        self.assertEqual(set(config), {"checks", "deploy", "release"})
        self.assertEqual(config["checks"]["test"]["credentials"], {"NUGET_TOKEN": "nuget-feed-token"})
        self.assertEqual(config["release"]["build"]["credentials"], {"SIGN_KEY": "signing-key"})
        self.assertEqual(config["release"]["finalize"]["credentials"], {"SIGN_KEY": "signing-key"})

    def test_given_the_application_template_when_listed_then_it_has_no_jenkinsfile(self):
        # Jenkins never reads a repository's Jenkinsfile: yggdrasil.yml replaced it.
        self.assertFalse((self.TEMPLATE / "Jenkinsfile").exists())


class HardeningTests(unittest.TestCase):
    """Every validation must match the whole value: `$` also accepts a trailing newline, which would
    reach a shell command line or a docker argument."""

    def test_given_values_with_a_trailing_newline_when_loaded_then_each_is_refused(self):
        for text, field in (
            ('checks:\n  unit:\n    image: x\n    run: y\n    credentials: {"KEY\\n": a}\n', "checks.unit.credentials"),
            ('checks:\n  unit:\n    image: x\n    run: y\n    credentials: {KEY: "a\\n"}\n', "checks.unit.credentials.KEY"),
            ('checks:\n  unit:\n    image: "x\\n"\n    run: y\n', "checks.unit.image"),
            ('checks:\n  "unit\\n":\n    image: x\n    run: y\n', "checks.unit"),
        ):
            with self.subTest(field=field), self.assertRaises(pc.ConfigError) as caught:
                parsed(text)
            self.assertIn(field, str(caught.exception))

    def test_given_a_trailing_newline_when_the_catalog_patterns_match_then_they_refuse_it(self):
        for pattern in (cat.CREDENTIAL_ID, cat.AGENT_NAME, cat.ID, cat.USER_NAME, cat.REPOSITORY):
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.match("abc\n"))
                self.assertIsNotNone(pattern.match("abc"))
        self.assertIsNone(pc.ENV_VAR.fullmatch("KEY\n"))

    def test_given_bad_check_names_when_loaded_then_each_is_refused(self):
        for name in ("Unit", "-unit", ".unit", "un it", "unit/x", "unit:x", "u\nu"):
            with self.subTest(name=name), self.assertRaises(pc.ConfigError) as caught:
                parsed(f"checks:\n  {json.dumps(name)}: {{image: x, run: y}}\n")
            self.assertIn("checks.", str(caught.exception))

    def test_given_good_check_names_when_loaded_then_they_are_accepted(self):
        for name in ("unit", "unit-tests", "unit_tests", "dotnet.test", "9lives", "a"):
            with self.subTest(name=name):
                self.assertIn(name, parsed(f"checks:\n  {name}: {{image: x, run: y}}\n")["checks"])

    def test_given_images_that_are_not_references_when_loaded_then_each_is_refused(self):
        for image in ("--privileged", "-v /:/host", "node 22", "node:22 --rm", "node:22\n", "", "  ", "/node",
                      "no$de", "node;rm", "$(id)"):
            quoted = json.dumps(image)
            for text, field in (
                (f"checks:\n  unit: {{image: {quoted}, run: y}}\n", "checks.unit.image"),
                (f"branchPolicy: {{image: {quoted}, run: y}}\n", "branchPolicy.image"),
                (f"release:\n  build: {{image: {quoted}, run: y}}\n", "release.build.image"),
                (f"release:\n  validate: {{image: {quoted}, run: y}}\n", "release.validate.image"),
                (f"release:\n  finalize: {{image: {quoted}, run: y}}\n", "release.finalize.image"),
                (f"publish:\n  nuget: {{image: {quoted}, run: y}}\n", "publish.nuget.image"),
            ):
                with self.subTest(image=image, field=field), self.assertRaises(pc.ConfigError) as caught:
                    parsed(text)
                self.assertIn(field, str(caught.exception))

    def test_given_docker_image_references_when_loaded_then_they_are_accepted(self):
        for image in ("alpine", "node:22", "mcr.microsoft.com/dotnet/sdk:10.0", "localhost:5000/team/app:1.2-rc1",
                      "registry.example.com:443/a/b@sha256:" + "a" * 64, "debian:12", "x"):
            with self.subTest(image=image):
                self.assertEqual(parsed(f"branchPolicy: {{image: {image}, run: y}}\n")["branchPolicy"]["image"], image)


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

    def test_given_a_check_binding_a_release_credential_when_validated_then_it_is_an_error(self):
        # releaseCredentials are the release build's alone: a check runs any branch's code, so it would
        # hand a signing key to whoever opens a pull request.
        errors = self.errors("checks:\n  unit:\n    image: x\n    run: y\n    credentials: {SIGN_KEY: sign-key}\n"
                             "  lint: {image: x, run: y}\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("checks.unit.credentials.SIGN_KEY", errors[0])
        self.assertIn("'sign-key' is not allowed", errors[0])
        self.assertIn("credentials: ['nuget-key']", errors[0])

    def test_given_a_release_build_binding_a_check_credential_when_validated_then_it_is_an_error(self):
        errors = self.errors("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                             "release:\n  build:\n    image: x\n    run: y\n    credentials: {KEY: nuget-key}\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("release.build.credentials.KEY", errors[0])
        self.assertIn("releaseCredentials: ['sign-key']", errors[0])

    def test_given_a_finalize_credential_in_the_release_credentials_when_validated_then_it_is_allowed(self):
        # finalize signs the whole set: it binds the release's signing key, as release.build may.
        self.assertEqual(self.errors("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                                     "release:\n  finalize:\n    image: x\n    run: y\n"
                                     "    credentials: {SIGN_KEY: sign-key}\n"), [])

    def test_given_a_finalize_credential_outside_the_release_credentials_when_validated_then_it_is_an_error(self):
        # The same allow-list as release.build: a check's credential (nuget-key) is not the release's, and
        # an unlisted one (forge-write) is nobody's.
        for credential in ("nuget-key", "forge-write"):
            with self.subTest(credential=credential):
                errors = self.errors("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                                     "release:\n  finalize:\n    image: x\n    run: y\n"
                                     f"    credentials: {{KEY: {credential}}}\n")
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("release.finalize.credentials.KEY", errors[0])
                self.assertIn(f"credential '{credential}' is not allowed", errors[0])
                self.assertIn("releaseCredentials: ['sign-key']", errors[0])

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
            "credentials": ["nuget-key"], "releaseCredentials": [], "checks": ["unit"], "actionsChecks": [],
            "release": "owner-tag", "tags": "[0-9]*"})

    def test_given_a_repository_without_a_release_when_described_then_it_releases_nothing(self):
        entry = pc.entry_for(catalog(), "shop-specs")
        self.assertEqual((entry["release"], entry["tags"]), ("none", "v*"))

    def test_given_an_application_with_a_release_environment_when_described_then_it_deploys_on_release(self):
        self.assertEqual(pc.entry_for(catalog(), "shop-api"), {
            "kind": "application", "id": "shop-api", "repository": "shop-api",
            "credentials": ["nuget-key"], "releaseCredentials": ["sign-key"], "checks": ["unit", "lint"],
            "actionsChecks": ["windows"], "release": "deploy", "tags": "v*"})

    def test_given_an_application_without_a_release_environment_when_described_then_it_releases_nothing(self):
        entry = pc.entry_for(catalog(), "shop-site")
        self.assertEqual((entry["release"], entry["repository"]), ("none", "shop-frontend"))
        self.assertEqual((entry["checks"], entry["credentials"], entry["releaseCredentials"], entry["actionsChecks"]),
                         ([], [], [], []))

    def test_given_an_unknown_job_when_described_then_it_is_a_config_error(self):
        with self.assertRaises(pc.ConfigError) as caught:
            pc.entry_for(catalog(), "nope")
        self.assertIn("nope", str(caught.exception))


class PublishAgentTests(unittest.TestCase):
    def test_given_no_publish_agent_when_read_then_it_is_the_last_release_environments_agent(self):
        value = catalog()
        del value["forge"]["publishAgent"]
        self.assertEqual(cat.forge(value)["publishAgent"], "vps")
        # dev deploys by hand, but naming its agent makes one, which may publish.
        value["environments"][0]["agent"] = "publisher"
        value["forge"]["publishAgent"] = "publisher"
        self.assertEqual(cat.validate(value), [])
        self.assertEqual(cat.forge(value)["publishAgent"], "publisher")


class CliTests(unittest.TestCase):
    def test_given_the_cli_when_describing_then_it_prints_one_json_document(self):
        status, out, err = run_cli("describe", "dotnet-util", str(repo("checks:\n  unit: {image: x, run: y}\n")))
        self.assertEqual((status, err), (0, ""))
        document = json.loads(out)  # one document, nothing else on stdout
        self.assertEqual(set(document), {"entry", "config", "errors", "warnings", "repositoryChecks"})
        self.assertEqual((document["warnings"], document["repositoryChecks"]), ([], ["unit"]))
        self.assertEqual(document["entry"]["release"], "owner-tag")
        self.assertEqual(document["config"]["checks"]["unit"]["run"], "y")
        self.assertEqual(document["errors"], [])

    def test_given_a_finalize_step_when_described_then_the_config_shows_it_as_the_library_reads_it(self):
        # The Jenkins library reads release.finalize from describe's config: it must be there as written.
        text = ("checks:\n  unit: {image: x, run: y}\n  lint: {image: x, run: y}\n"
                "release:\n  build: {image: debian:12, run: ci/release.sh, files: [\"dist/*\"]}\n"
                "  finalize:\n    image: debian:12\n    run: ci/sign.sh\n    files: [\"signed/*\"]\n"
                "    credentials: {SIGN_KEY: sign-key}\n")
        status, out, err = run_cli("describe", "shop-api", str(repo(text)))
        self.assertEqual((status, err), (0, ""))
        document = json.loads(out)
        self.assertEqual(document["errors"], [])
        self.assertEqual(document["config"]["release"]["finalize"],
                         {"image": "debian:12", "run": "ci/sign.sh", "files": ["signed/*"],
                          "credentials": {"SIGN_KEY": "sign-key"}})

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


CHECKS = {
    "unit": {"image": "dotnet:10", "run": "ci/test.sh", "docker": True, "credentials": {"NUGET_KEY": "nuget-key"},
             "paths": ["src/**", "tests/**"], "junit": "**/TestResults/*.xml", "archive": "out/**"},
    "lint": {"image": "node:22", "run": "npm run lint"},
}


def plan(checks=None, statuses=(), changed=None, branch="feature/x", user="jenkins"):
    """plan_checks() over CHECKS (or `checks`), by name, for a job whose catalog entry names every one."""
    config = {"checks": copy.deepcopy(CHECKS if checks is None else checks)}
    entry = {"id": "shop-api", "checks": list(config["checks"])}
    return {entry["name"]: entry for entry in pc.plan_checks(config, entry, list(statuses), changed, branch, user)}


class PlanTests(unittest.TestCase):
    def test_given_a_success_by_jenkins_when_planned_then_the_check_is_reused(self):
        planned = plan(statuses=[("lint", "success", "jenkins")], changed=["README.md"])
        self.assertEqual(planned["lint"]["action"], "reuse")
        self.assertIn("jenkins", planned["lint"]["reason"])
        self.assertEqual(planned["unit"]["action"], "skip")

    def test_given_a_success_by_someone_else_when_planned_then_the_check_runs(self):
        # Anyone with write access may post a status: only Jenkins' own result is trusted.
        planned = plan(statuses=[("unit", "success", "mallory"), ("lint", "success", "")], changed=["src/a.cs"])
        self.assertEqual(planned["unit"]["action"], "run")
        self.assertEqual(planned["lint"]["action"], "run")
        self.assertIn("mallory", planned["unit"]["reason"])

    def test_given_a_success_by_a_login_that_only_resembles_jenkins_when_planned_then_the_check_runs(self):
        for creator in ("Jenkins", "jenkins ", "jenkins-bot", "jenkins[bot]"):
            with self.subTest(creator=creator):
                self.assertEqual(plan(statuses=[("lint", "success", creator)])["lint"]["action"], "run")
        self.assertEqual(plan(statuses=[("lint", "success", "my-app[bot]")], user="my-app[bot]")["lint"]["action"],
                         "reuse")

    def test_given_a_failure_by_jenkins_when_planned_then_the_check_runs_again(self):
        for state in ("failure", "error", "pending"):
            with self.subTest(state=state):
                planned = plan(statuses=[("lint", state, "jenkins")])
                self.assertEqual(planned["lint"]["action"], "run")
                self.assertIn(state, planned["lint"]["reason"])

    def test_given_a_check_without_a_status_when_planned_then_the_check_runs(self):
        self.assertEqual(plan()["lint"]["action"], "run")

    def test_given_statuses_of_other_contexts_when_planned_then_they_are_ignored(self):
        planned = plan(statuses=[("deploy/dev", "success", "jenkins"), ("branch-policy", "success", "jenkins")])
        self.assertEqual({entry["action"] for entry in planned.values()}, {"run"})

    def test_given_paths_and_no_matching_change_when_planned_then_it_is_skipped_with_the_globs(self):
        planned = plan(changed=["docs/readme.md", "README.md"])
        self.assertEqual(planned["unit"]["action"], "skip")
        self.assertEqual(planned["unit"]["reason"], "skipped: no changes under src/**, tests/**")
        self.assertEqual(planned["lint"]["action"], "run", "a check without paths always runs")

    def test_given_paths_and_a_matching_change_when_planned_then_it_runs(self):
        self.assertEqual(plan(changed=["README.md", "tests/a/b_test.cs"])["unit"]["action"], "run")

    def test_given_paths_and_only_the_pipeline_file_changed_when_planned_then_it_runs(self):
        # A Renovate bump of a check's image touches yggdrasil.yml alone: a skip would report the check passed
        # without ever running it on the new image.
        planned = plan(changed=["yggdrasil.yml"])
        self.assertEqual(planned["unit"]["action"], "run")
        self.assertIn("yggdrasil.yml changed", planned["unit"]["reason"])

    def test_given_paths_and_a_pipeline_file_in_a_subdirectory_when_planned_then_it_is_still_skipped(self):
        # Only the repository's own file at its root is the pipeline's; another one is just a file.
        self.assertEqual(plan(changed=["docs/yggdrasil.yml"])["unit"]["action"], "skip")

    def test_given_paths_and_no_changed_files_at_all_when_planned_then_it_is_skipped(self):
        # An empty list is a known answer (nothing changed), unlike None.
        self.assertEqual(plan(changed=[])["unit"]["action"], "skip")

    def test_given_paths_and_unknown_changes_when_planned_then_it_runs(self):
        planned = plan(changed=None)
        self.assertEqual(planned["unit"]["action"], "run")
        self.assertIn("unknown", planned["unit"]["reason"])

    def test_given_a_success_by_jenkins_and_no_matching_change_when_planned_then_it_is_reused(self):
        planned = plan(statuses=[("unit", "success", "jenkins")], changed=["README.md"])
        self.assertEqual(planned["unit"]["action"], "reuse")

    def test_given_a_renovate_branch_when_planned_then_no_credential_is_bound(self):
        self.assertEqual(plan(branch="renovate/nuget-1.x")["unit"]["credentials"], {})
        self.assertEqual(plan(branch="feature/x")["unit"]["credentials"], {"NUGET_KEY": "nuget-key"})
        # Only the renovate/ prefix counts.
        self.assertEqual(plan(branch="renovated/x")["unit"]["credentials"], {"NUGET_KEY": "nuget-key"})
        self.assertEqual(plan(branch="feature/renovate/x")["unit"]["credentials"], {"NUGET_KEY": "nuget-key"})

    def test_given_a_plan_when_made_then_each_entry_has_every_field_with_defaults(self):
        planned = plan()
        self.assertEqual(planned["unit"], {
            "name": "unit", "action": "run", "reason": planned["unit"]["reason"], "image": "dotnet:10",
            "run": "ci/test.sh", "docker": True, "credentials": {"NUGET_KEY": "nuget-key"},
            "junit": "**/TestResults/*.xml", "archive": "out/**"})
        self.assertEqual(planned["lint"], {
            "name": "lint", "action": "run", "reason": planned["lint"]["reason"], "image": "node:22",
            "run": "npm run lint", "docker": False, "credentials": {}, "junit": None, "archive": None})
        self.assertEqual(list(planned), ["unit", "lint"], "the file's order")

    def test_given_the_input_when_planned_then_it_is_not_changed(self):
        config = {"checks": copy.deepcopy(CHECKS)}
        entry = {"id": "shop-api", "checks": ["unit", "lint"]}
        pc.plan_checks(config, entry, [], None, "renovate/x", "jenkins")
        self.assertEqual((config["checks"], entry["checks"]), (CHECKS, ["unit", "lint"]))

    def test_given_no_config_or_no_checks_when_planned_then_nothing_runs(self):
        # No yggdrasil.yml: the repository behaves as before the file existed, whatever the catalog says.
        self.assertEqual(pc.plan_checks(None, {"id": "x", "checks": ["unit"]}, [], None, "feature/x", "jenkins"), [])
        self.assertEqual(pc.plan_checks({}, {"id": "x", "checks": []}, [], None, "feature/x", "jenkins"), [])

    def test_given_the_same_context_twice_when_planned_then_the_last_status_counts(self):
        planned = plan(statuses=[("lint", "failure", "jenkins"), ("lint", "success", "jenkins")])
        self.assertEqual(planned["lint"]["action"], "reuse")
        planned = plan(statuses=[("lint", "success", "jenkins"), ("lint", "failure", "jenkins")])
        self.assertEqual(planned["lint"]["action"], "run")


# A library and its docs site on one repository, ygg-lib: the library's `repositories` entry runs the
# tests and the coverage, the docs application runs nothing of its own and deploys with the coverage's
# archive (deploy.inputs). One yggdrasil.yml serves both jobs.
SHARED_FILE = """\
checks:
  unit: {image: dotnet:10, run: ci/test.sh}
  coverage: {image: dotnet:10, run: ci/coverage.sh, archive: "coverage/**"}
deploy:
  inputs: [coverage]
"""


def shared_catalog():
    value = catalog()
    value["repositories"].append({"id": "ygg-lib", "release": "owner-tag", "tags": "[0-9]*",
                                  "checks": ["unit", "coverage"]})
    value["systems"][0]["applications"].append(
        {"id": "ygg-lib-docs", "name": "Docs", "kind": "web", "health": "http://ygg-lib-docs:8080/",
         "repository": "ygg-lib", "checks": [], "environments": {"dev": {}}})
    return value


def run_shared(*argv, value=None):
    """run_cli() against shared_catalog() (or `value`)."""
    out, err = io.StringIO(), io.StringIO()
    out.reconfigure = lambda **_: None
    with mock.patch.object(cat, "load", return_value=shared_catalog() if value is None else value), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = pc.main(list(argv))
    return status, out.getvalue(), err.getvalue()


class OwnChecksTests(unittest.TestCase):
    """A job runs its own catalog entry's checks, and the deploy inputs when it deploys: never every
    check of the file, which other entries of the same repository may name."""

    def plan(self, job, text=SHARED_FILE, statuses="", deploying=False, value=None):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="ygg-plan-"))
        (directory / "statuses").write_text(statuses, encoding="utf-8", newline="")
        argv = ["plan", job, str(repo(text)), "--statuses", str(directory / "statuses"), "--changed", "-",
                "--branch", "main"] + (["--deploying"] if deploying else [])
        status, out, err = run_shared(*argv, value=value)
        self.assertEqual((status, err), (0, ""))
        return [(entry["name"], entry["action"]) for entry in json.loads(out)]

    def describe(self, job, text=SHARED_FILE, value=None):
        status, out, _ = run_shared("describe", job, str(repo(text)), value=value)
        return status, json.loads(out)

    def test_given_two_entries_on_one_repository_when_planned_then_each_runs_only_its_own_checks(self):
        self.assertEqual(self.plan("ygg-lib"), [("unit", "run"), ("coverage", "run")])
        # The docs site names no check of its own: when it does not deploy, it runs nothing.
        self.assertEqual(self.plan("ygg-lib-docs"), [])
        # Deploying changes nothing for the library: its file's inputs are its docs site's, not its own
        # catalog checks, and they are already among them.
        self.assertEqual(self.plan("ygg-lib", deploying=True), [("unit", "run"), ("coverage", "run")])

    def test_given_a_deploying_docs_job_when_planned_then_only_its_deploy_input_runs(self):
        self.assertEqual(self.plan("ygg-lib-docs", deploying=True), [("coverage", "run")])
        # The library job's success, by Jenkins, is on the commit: the plan reuses it (the pipeline library
        # forces a deploy input to run anyway when it deploys, since its archive goes into the image).
        statuses = "\t".join(["coverage", "success", "jenkins"]) + "\n" + "\t".join(["unit", "failure", "jenkins"])
        self.assertEqual(self.plan("ygg-lib-docs", statuses=statuses, deploying=True), [("coverage", "reuse")])

    def test_given_a_check_in_the_entry_and_in_the_deploy_inputs_when_planned_then_it_is_planned_once(self):
        config = {"checks": {**copy.deepcopy(CHECKS), "docs": {"image": "x", "run": "y"}},
                  "deploy": {"inputs": ["docs", "lint", "unit"]}}
        entry = {"id": "shop-api", "checks": ["lint"]}
        names = [e["name"] for e in pc.plan_checks(config, entry, [], None, "main", "jenkins", deploying=True)]
        # The entry's catalog order, then the inputs in theirs: not the file's order.
        self.assertEqual(names, ["lint", "docs", "unit"])
        names = [e["name"] for e in pc.plan_checks(config, entry, [], None, "main", "jenkins")]
        self.assertEqual(names, ["lint"])

    def test_given_a_needed_check_the_file_does_not_define_when_planned_then_it_is_an_error(self):
        config = {"checks": copy.deepcopy(CHECKS), "deploy": {"inputs": ["site"]}}
        with self.assertRaises(pc.ConfigError) as caught:
            pc.plan_checks(config, {"id": "shop-api", "checks": ["unit", "e2e"]}, [], None, "main", "jenkins")
        self.assertIn("e2e", str(caught.exception))
        with self.assertRaises(pc.ConfigError) as caught:
            pc.plan_checks(config, {"id": "shop-api", "checks": []}, [], None, "main", "jenkins", deploying=True)
        self.assertIn("site", str(caught.exception))

    def test_given_a_config_check_no_entry_names_when_described_then_it_is_a_warning(self):
        text = SHARED_FILE.replace("deploy:", "  lint: {image: node:22, run: npm run lint}\ndeploy:")
        for job in ("ygg-lib", "ygg-lib-docs"):
            with self.subTest(job=job):
                status, document = self.describe(job, text)
                # A warning, not an error: the build goes on, and describe exits 0.
                self.assertEqual((status, document["errors"]), (0, []))
                self.assertEqual(document["warnings"],
                                 ["checks.lint is not in any catalog entry of ygg-lib: it never runs"])
        status, document = self.describe("ygg-lib")
        self.assertEqual((status, document["warnings"]), (0, []))

    def test_given_a_check_another_entry_of_the_repository_names_when_described_then_it_is_not_a_warning(self):
        value = shared_catalog()
        value["systems"][0]["applications"][-1]["checks"] = ["links"]
        text = SHARED_FILE.replace("deploy:", "  links: {image: x, run: y}\ndeploy:")
        self.assertEqual(self.describe("ygg-lib", text, value)[1]["warnings"], [])
        self.assertEqual(self.plan("ygg-lib", text, value=value), [("unit", "run"), ("coverage", "run")])
        self.assertEqual(self.plan("ygg-lib-docs", text, value=value), [("links", "run")])
        # The same name on another repository's entry claims nothing here.
        value["systems"][0]["applications"][-1]["repository"] = "elsewhere"
        self.assertEqual(self.describe("ygg-lib", text, value)[1]["warnings"],
                         ["checks.links is not in any catalog entry of ygg-lib: it never runs"])

    def test_given_a_check_an_application_runs_only_as_a_deploy_input_when_described_then_it_is_not_a_warning(self):
        # shop-site (repository shop-frontend) names no check: its build check runs when it deploys.
        text = "checks:\n  build: {image: x, run: y, archive: out/**}\ndeploy:\n  inputs: [build]\n"
        status, out, _ = run_cli("describe", "shop-site", str(repo(text)))
        self.assertEqual((status, json.loads(out)["warnings"]), (0, []))
        # A repositories entry never deploys: its file's inputs run nowhere.
        status, out, _ = run_cli("describe", "shop-specs", str(repo(text.replace("build", "lint"))))
        self.assertEqual((status, json.loads(out)["warnings"]), (0, []))
        text = "checks:\n  lint: {image: x, run: y}\n  build: {image: x, run: y}\ndeploy:\n  inputs: [build]\n"
        status, out, _ = run_cli("describe", "shop-specs", str(repo(text)))
        self.assertEqual((status, json.loads(out)["warnings"]),
                         (0, ["checks.build is not in any catalog entry of shop-specs: it never runs"]))

    def test_given_a_repository_without_a_file_or_with_an_invalid_one_when_described_then_there_is_no_warning(self):
        self.assertEqual(self.describe("ygg-lib", None)[1]["warnings"], [])
        status, document = self.describe("ygg-lib", "checks:\n  unit: {image: x}\n")
        self.assertEqual((status, document["warnings"]), (1, []))

    def test_given_deploying_twice_or_out_of_place_when_planning_then_it_is_a_usage_error(self):
        for flags in (["--deploying", "--deploying"], ["--release", "--release"], ["--releasing"]):
            with self.subTest(flags=flags):
                self.assertEqual(run_shared("plan", "ygg-lib", "/tmp", "--statuses", "a", "--changed", "-", "--branch",
                                            "b", *flags)[0], 2)
        self.assertEqual(run_shared("plan", "ygg-lib", "/tmp", "--deploying", "--statuses", "a", "--changed", "-",
                                    "--branch")[0], 2)
        self.assertEqual(run_shared("describe", "ygg-lib", "/tmp", "--deploying")[0], 2)

    def test_given_two_entries_on_one_repository_when_described_then_the_repository_checks_are_every_entrys(self):
        value = shared_catalog()
        value["systems"][0]["applications"][-1]["checks"] = ["links", "unit"]
        text = SHARED_FILE.replace("deploy:", "  links: {image: x, run: y}\ndeploy:")
        for job in ("ygg-lib", "ygg-lib-docs"):
            with self.subTest(job=job):
                # The deciding entry's first (the library: it releases), then the docs site's; each once.
                self.assertEqual(self.describe(job, text, value)[1]["repositoryChecks"], ["unit", "coverage", "links"])
        # Without a file the document has them too: they are the catalog's.
        self.assertEqual(self.describe("ygg-lib", None, value)[1]["repositoryChecks"], ["unit", "coverage", "links"])


# The library template's shape on one repository: the library's checks bind ReportGenerator's licence (coverage
# only), the docs site deploys with coverage's archive, and the library publishes. One file, two jobs.
LIBRARY_FILE = """\
checks:
  unit: {image: dotnet:10, run: ci/test.sh}
  coverage: {image: dotnet:10, run: ci/coverage.sh, archive: "coverage/**",
             credentials: {REPORTGENERATOR_LICENSE: reportgenerator-license}}
deploy:
  inputs: [coverage]
publish:
  nuget: {image: dotnet:10, run: ci/pack.sh}
"""


def library_catalog(docs_credentials=("reportgenerator-license",)):
    value = shared_catalog()
    value["repositories"][-1]["credentials"] = ["reportgenerator-license"]
    value["systems"][0]["applications"][-1]["credentials"] = list(docs_credentials)
    return value


class SharedFileJudgementTests(unittest.TestCase):
    """One yggdrasil.yml serves every entry of its repository, and each job is judged on its own part of it: the
    credentials of the checks it runs, and the release sections only when it is the entry that releases."""

    def describe(self, job, text=LIBRARY_FILE, value=None):
        status, out, _ = run_shared("describe", job, str(repo(text)),
                                    value=library_catalog() if value is None else value)
        return status, json.loads(out)

    def test_given_a_librarys_docs_job_when_described_then_the_shared_file_passes(self):
        # Its deploy runs coverage, whose licence its entry allows; publish is the library's, not its.
        status, document = self.describe("ygg-lib-docs")
        self.assertEqual((status, document["errors"]), (0, []))
        status, document = self.describe("ygg-lib")
        self.assertEqual((status, document["errors"]), (0, []))

    def test_given_a_docs_job_whose_deploy_input_binds_a_credential_its_entry_lacks_when_described_then_it_is_refused(self):
        # The docs job does run coverage (a deploy input), so it must be allowed coverage's credential.
        status, document = self.describe("ygg-lib-docs", value=library_catalog(docs_credentials=()))
        self.assertEqual(status, 1)
        self.assertEqual(len(document["errors"]), 1, document["errors"])
        self.assertIn("checks.coverage.credentials.REPORTGENERATOR_LICENSE", document["errors"][0])
        self.assertIn("ygg-lib-docs", document["errors"][0])

    def test_given_a_credential_of_a_check_only_another_entry_runs_when_described_then_this_job_is_not_judged_on_it(self):
        # unit is the library's alone: the docs job never runs it, so it need not allow unit's credential.
        text = LIBRARY_FILE.replace("ci/test.sh}", "ci/test.sh, credentials: {TOKEN: test-token}}")
        value = library_catalog()
        value["repositories"][-1]["credentials"].append("test-token")
        self.assertEqual(self.describe("ygg-lib-docs", text, value)[1]["errors"], [])
        # ... while the library, which runs it, still must.
        value["repositories"][-1]["credentials"].remove("test-token")
        status, document = self.describe("ygg-lib", text, value)
        self.assertEqual(status, 1)
        self.assertIn("checks.unit.credentials.TOKEN", document["errors"][0])

    def test_given_the_releasing_entry_when_described_then_its_publish_and_release_are_still_judged(self):
        # The library releases (owner-tag): a release build's credential outside its releaseCredentials is refused
        # for it, and the docs job skips the release sections altogether.
        text = LIBRARY_FILE + "release:\n  build: {image: x, run: y, credentials: {SIGN: sign-key}}\n"
        status, document = self.describe("ygg-lib", text)
        self.assertEqual(status, 1)
        self.assertIn("release.build.credentials.SIGN", document["errors"][0])
        self.assertEqual(self.describe("ygg-lib-docs", text)[1]["errors"], [])
        # release.finalize's credentials are the releasing entry's to allow in the same way.
        text = LIBRARY_FILE + "release:\n  finalize: {image: x, run: y, credentials: {SIGN: sign-key}}\n"
        status, document = self.describe("ygg-lib", text)
        self.assertEqual(status, 1)
        self.assertIn("release.finalize.credentials.SIGN", document["errors"][0])
        self.assertEqual(self.describe("ygg-lib-docs", text)[1]["errors"], [])
        # A repository nobody releases: every entry is judged on publish, and refused.
        value = library_catalog()
        value["repositories"][-1]["release"] = "none"
        for job in ("ygg-lib", "ygg-lib-docs"):
            with self.subTest(job=job):
                status, document = self.describe(job, value=value)
                self.assertEqual(status, 1)
                self.assertTrue(any(error.startswith("publish:") for error in document["errors"]), document)

    def test_given_the_releasing_application_when_described_then_the_credentials_of_every_entrys_checks_are_judged(self):
        # Its release pull request runs every check main requires of the repository, a sibling's too.
        text = RELEASING_FILE.replace("links: {image: x, run: y}", "links: {image: x, run: y, credentials: {T: links-key}}")
        status, out, _ = run_shared("describe", "shop-api", str(repo(text)), value=releasing_catalog())
        document = json.loads(out)
        self.assertEqual(status, 1)
        self.assertIn("checks.links.credentials.T", document["errors"][0])

    def test_given_the_real_catalog_when_each_librarys_docs_job_is_described_against_the_template_then_it_passes(self):
        # Every docs application on a library's repository, against templates/library, as Task 9 copies it.
        value = cat.load()
        # The .NET libraries: the entries the library template is for (its test and coverage checks).
        libraries = {entry["id"] for entry in cat.repositories(value) if entry["checks"] == ["test", "coverage"]}
        self.assertEqual(len(libraries), 12)
        docs = [app["id"] for _, app in cat.applications(value, deployable=True) if app.get("repository") in libraries]
        self.assertEqual(len(docs), 12)
        template = pathlib.Path(__file__).resolve().parent.parent / "templates" / "library"
        for job in docs + sorted(libraries):
            with self.subTest(job=job):
                document = pc.describe(value, job, template)
                self.assertEqual(document["errors"], [])


# shop-api releases (prod: trigger release) and shares its repository with its docs site, which names a
# check of its own. One file for both.
RELEASING_FILE = """\
checks:
  unit: {image: x, run: y}
  lint: {image: x, run: y}
  links: {image: x, run: y}
  site: {image: x, run: y, archive: out/**}
deploy:
  inputs: [site]
"""


def releasing_catalog():
    value = catalog()
    value["systems"][0]["applications"].append(
        {"id": "shop-api-docs", "name": "Docs", "kind": "web", "health": "http://shop-api-docs:8080/",
         "repository": "shop-api", "checks": ["links"], "environments": {"dev": {}}})
    return value


class ReleasePlanTests(unittest.TestCase):
    """A release pull request deploys to every release environment before it merges: its releasing job runs
    every check main requires of the repository (catalog.repository_checks: each entry's), not only its own,
    or production would go out on a commit a sibling's check failed on and the merge would then be refused."""

    def plan(self, job, *flags, text=RELEASING_FILE):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="ygg-plan-"))
        (directory / "statuses").write_text("", encoding="utf-8")
        return run_shared("plan", job, str(repo(text)), "--statuses", str(directory / "statuses"), "--changed", "-",
                          "--branch", "release/1.0.0", *flags, value=releasing_catalog())

    def names(self, job, *flags):
        status, out, err = self.plan(job, *flags)
        self.assertEqual((status, err), (0, ""))
        return [entry["name"] for entry in json.loads(out)]

    def test_given_a_release_pull_request_when_the_releasing_job_plans_then_it_runs_every_entrys_checks(self):
        self.assertEqual(self.names("shop-api", "--deploying", "--release"), ["unit", "lint", "links", "site"])
        # The flags in either order.
        self.assertEqual(self.names("shop-api", "--release", "--deploying"), ["unit", "lint", "links", "site"])
        # A push that deploys: its own checks and the inputs, as before.
        self.assertEqual(self.names("shop-api", "--deploying"), ["unit", "lint", "site"])

    def test_given_a_release_pull_request_when_a_job_that_does_not_release_plans_then_it_keeps_its_own_checks(self):
        self.assertEqual(self.names("shop-api-docs"), ["links"])
        # --release is the releasing job's alone: anything else asking for it is refused, not widened.
        status, out, err = self.plan("shop-api-docs", "--release")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("shop-api-docs does not release", err)
        status, out, err = self.plan("shop-api-docs", "--release", text=RELEASING_FILE)
        self.assertEqual(status, 1)

    def test_given_a_sibling_check_the_file_does_not_define_when_the_release_is_planned_then_it_is_an_error(self):
        text = RELEASING_FILE.replace("  links: {image: x, run: y}\n", "")
        status, out, err = self.plan("shop-api", "--deploying", "--release", text=text)
        self.assertEqual((status, out), (1, ""))
        self.assertIn("links", err)

    def test_given_release_checks_when_planned_then_plan_checks_takes_them_instead_of_the_entrys(self):
        config = {"checks": {**copy.deepcopy(CHECKS), "links": {"image": "x", "run": "y"}},
                  "deploy": {"inputs": ["unit"]}}
        entry = {"id": "shop-api", "checks": ["lint"]}
        planned = pc.plan_checks(config, entry, [], None, "release/1.0.0", "jenkins", deploying=True,
                                 release_checks=["lint", "links"])
        self.assertEqual([e["name"] for e in planned], ["lint", "links", "unit"])


class GlobTests(unittest.TestCase):
    def test_given_globs_when_matched_then_double_star_crosses_directories_and_single_star_does_not(self):
        for glob, path, expected in (
            ("src/**", "src/a/b.cs", True),
            ("src/**", "src/a.cs", True),
            ("src/**", "other/src/a.cs", False),
            ("src/**", "srcs/a.cs", False),
            ("docs/*.md", "docs/a.md", True),
            ("docs/*.md", "docs/a/b.md", False),
            ("*.md", "README.md", True),
            ("*.md", "docs/README.md", False),
            ("**/*.md", "docs/a/README.md", True),
            ("**/*.md", "README.md", True),
            ("**/*.cs", "src/a/B.cs", True),
            ("**/*.cs", "src/a/B.csx", False),
            ("src/**/test.cs", "src/test.cs", True),
            ("src/**/test.cs", "src/a/b/test.cs", True),
            ("src/**/test.cs", "src/a/b/test.csx", False),
            ("a?c", "abc", True),
            ("a?c", "a/c", False),
            ("[ab].txt", "a.txt", True),
            ("[ab].txt", "c.txt", False),
            ("[!ab].txt", "c.txt", True),
            ("file[1", "file[1", True),
            ("a.b", "axb", False),
            ("a+b(c)", "a+b(c)", True),
            ("**", "any/thing/at/all", True),
        ):
            with self.subTest(glob=glob, path=path):
                self.assertEqual(pc.glob_match(glob, path), expected)

    def test_given_a_path_with_a_leading_dot_slash_when_matched_then_it_is_normalised(self):
        self.assertTrue(pc.glob_match("src/**", "./src/a.cs"))

    def test_given_a_trailing_newline_in_a_path_when_matched_then_it_still_has_to_match_whole(self):
        self.assertFalse(pc.glob_match("*.md", "a.md\n"))


TAB, NL = chr(9), chr(10)


class PlanCliTests(unittest.TestCase):
    # shop-api's catalog entry names both checks (unit, lint): the job plans each of them.
    CONFIG = ("checks:\n  unit:\n    image: x\n    run: y\n    paths: ['src/**']\n"
              "    credentials: {NUGET_KEY: nuget-key}\n  lint: {image: x, run: y}\n")

    def files(self, statuses, changed):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="ygg-plan-"))
        (directory / "statuses").write_text(statuses, encoding="utf-8", newline="")
        if changed is not None:
            (directory / "changed").write_text(changed, encoding="utf-8", newline="")
        return directory

    def plan(self, text=CONFIG, statuses="", changed="src/a.cs" + NL, branch="feature/x", job="shop-api", **override):
        directory = self.files(statuses, changed)
        arguments = {"--statuses": str(directory / "statuses"),
                     "--changed": "-" if changed is None else str(directory / "changed"), "--branch": branch}
        arguments.update(override)
        argv = ["plan", job, str(repo(text))] + [word for pair in arguments.items() for word in pair]
        status, out, err = run_cli(*argv)
        return status, (json.loads(out) if out else None), err

    def test_given_the_cli_when_planning_then_it_prints_a_json_list(self):
        status, planned, err = self.plan(statuses=TAB.join(["lint", "success", "jenkins"]) + NL)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual([(e["name"], e["action"]) for e in planned], [("unit", "run"), ("lint", "reuse")])
        self.assertEqual(planned[0]["credentials"], {"NUGET_KEY": "nuget-key"})

    def test_given_a_catalog_jenkins_user_when_planning_then_that_user_is_the_one_trusted(self):
        # CATALOG names no jenkinsUser: the default is jenkins.
        mine = TAB.join(["lint", "success", "jenkins"]) + NL
        yours = TAB.join(["lint", "success", "ci-bot"]) + NL
        self.assertEqual(self.plan(statuses=mine)[1][1]["action"], "reuse")
        self.assertEqual(self.plan(statuses=yours)[1][1]["action"], "run")
        value = catalog()
        value["forge"]["jenkinsUser"] = "ci-bot"
        out = io.StringIO()
        out.reconfigure = lambda **_: None
        directory = self.files(yours, "x" + NL)
        with mock.patch.object(cat, "load", return_value=value), contextlib.redirect_stdout(out):
            status = pc.main(["plan", "shop-api", str(repo(self.CONFIG)), "--statuses", str(directory / "statuses"),
                              "--changed", str(directory / "changed"), "--branch", "feature/x"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())[1]["action"], "reuse")

    def test_given_a_github_catalog_when_planning_then_the_default_jenkins_user_is_trusted(self):
        value = catalog()
        del value["forge"]
        out = io.StringIO()
        out.reconfigure = lambda **_: None
        directory = self.files(TAB.join(["lint", "success", "jenkins"]) + NL, "x" + NL)
        with mock.patch.object(cat, "load", return_value=value), contextlib.redirect_stdout(out):
            status = pc.main(["plan", "shop-api", str(repo(self.CONFIG)), "--statuses", str(directory / "statuses"),
                              "--changed", str(directory / "changed"), "--branch", "feature/x"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())[1]["action"], "reuse")

    def test_given_a_dash_for_the_changed_files_when_planning_then_the_changes_are_unknown(self):
        status, planned, _ = self.plan(changed=None)
        self.assertEqual((status, planned[0]["action"]), (0, "run"))

    def test_given_a_changed_file_without_a_match_when_planning_then_the_check_is_skipped(self):
        _, planned, _ = self.plan(changed="README.md" + NL + NL)
        self.assertEqual(planned[0]["action"], "skip")
        self.assertEqual(planned[0]["reason"], "skipped: no changes under src/**")

    def test_given_an_empty_changed_file_when_planning_then_nothing_changed(self):
        self.assertEqual(self.plan(changed="")[1][0]["action"], "skip")

    def test_given_statuses_with_crlf_and_blank_lines_when_planning_then_they_are_read(self):
        text = chr(13) + NL + TAB.join(["lint", "success", "jenkins"]) + chr(13) + NL
        self.assertEqual(self.plan(statuses=text)[1][1]["action"], "reuse")

    def test_given_a_status_without_a_creator_when_planning_then_it_is_read_with_an_empty_creator(self):
        text = TAB.join(["lint", "success", ""]) + NL + TAB.join(["unit", "success"]) + NL
        status, planned, _ = self.plan(statuses=text)
        self.assertEqual((status, [e["action"] for e in planned]), (0, ["run", "run"]))

    def test_given_a_renovate_branch_when_planning_then_no_credential_is_bound(self):
        self.assertEqual(self.plan(branch="renovate/x")[1][0]["credentials"], {})

    def test_given_a_repository_without_a_file_when_planning_then_the_plan_is_empty(self):
        status, out, _ = run_cli("plan", "shop-specs", str(repo()), "--statuses", "/dev/null", "--changed", "-",
                                 "--branch", "feature/x")
        self.assertEqual((status, json.loads(out)), (0, []))

    def test_given_an_invalid_file_when_planning_then_it_exits_with_one_and_names_the_errors(self):
        status, out, err = run_cli("plan", "dotnet-util", str(repo("checks:\n  unit: {image: x}\n")),
                                   "--statuses", "/dev/null", "--changed", "-", "--branch", "b")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("checks.unit.run", err)

    def test_given_a_file_that_breaks_the_catalog_rules_when_planning_then_it_exits_with_one(self):
        status, out, err = run_cli("plan", "dotnet-util", str(repo("checks:\n  other: {image: x, run: y}\n")),
                                   "--statuses", "/dev/null", "--changed", "-", "--branch", "b")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("unit", err)

    def test_given_missing_input_files_when_planning_then_it_exits_with_one(self):
        for override in ({"--statuses": "/no/such/file"}, {"--changed": "/no/such/file"}):
            with self.subTest(override=override):
                status, planned, err = self.plan(**override)
                self.assertEqual((status, planned), (1, None))
                self.assertIn("/no/such/file", err)

    def test_given_malformed_statuses_when_planning_then_it_exits_with_one(self):
        status, planned, err = self.plan(statuses="lint-with-no-state" + NL)
        self.assertEqual((status, planned), (1, None))
        self.assertIn("statuses", err)

    def test_given_an_unknown_job_when_planning_then_it_exits_with_one(self):
        self.assertEqual(self.plan(job="nope")[0], 1)

    def test_given_the_wrong_arguments_when_planning_then_it_is_a_usage_error(self):
        self.assertEqual(run_cli("plan", "dotnet-util", "/tmp")[0], 2)
        self.assertEqual(run_cli("plan", "dotnet-util", "/tmp", "--statuses", "a", "--changed", "-")[0], 2)
        self.assertEqual(run_cli("plan", "dotnet-util", "/tmp", "--statuses", "a", "--changed", "-", "--branch")[0], 2)
        self.assertEqual(run_cli("plan", "dotnet-util", "/tmp", "--statuses", "a", "--changed", "-", "--branch", "b",
                                 "--extra", "c")[0], 2)


if __name__ == "__main__":
    unittest.main()
