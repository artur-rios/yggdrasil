"""Tests for `ygg` (scripts/ygg.py): dispatch to the host scripts, help, version, completion,
self-install and the numbered menu, against fake docker and scratch stores.

    python3 -m unittest discover -s scripts
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_ygg

SCRIPTS = pathlib.Path(__file__).resolve().parent
YGG = SCRIPTS / "ygg.py"


def ygg(*arguments, env=None, input=None):
    return subprocess.run([sys.executable, str(YGG), *arguments], capture_output=True, text=True,
                          env=env, input=input, stdin=None if input is not None else subprocess.DEVNULL)


class DispatchTests(unittest.TestCase):
    """The harness of test_ygg (fake docker, a scratch secrets directory), through ygg.py. Its setUp
    and helpers are borrowed, not inherited, so test_ygg's own tests don't run twice."""

    setUp = test_ygg.EnvironmentCommandTests.setUp
    platform_env = test_ygg.EnvironmentCommandTests.platform_env
    containers = test_ygg.EnvironmentCommandTests.containers

    def run_ygg(self, *arguments, input=None):
        return ygg(*arguments, env=self.env, input=input)

    def test_given_env_status_when_run_through_ygg_then_host_sh_answers(self):
        result = self.run_ygg("env", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("development", result.stdout)

    def test_given_a_bad_env_usage_then_host_sh_says_how_and_exits_1(self):
        result = self.run_ygg("env", "start", "development", "production")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_given_env_help_then_the_env_parser_explains_it(self):
        result = self.run_ygg("env", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ygg env", result.stdout)

    def test_given_catalog_environments_then_it_is_catalog_py_output(self):
        expected = subprocess.run([sys.executable, str(SCRIPTS / "catalog.py"), "environments"],
                                  capture_output=True, text=True).stdout
        self.assertEqual(self.run_ygg("catalog", "environments").stdout, expected)

    def test_given_catalog_get_then_the_words_reach_catalog_py_in_order(self):
        result = self.run_ygg("catalog", "get", "heimdall-api", "development", "onDemand")
        self.assertEqual((result.returncode, result.stdout), (0, "true\n"), result.stderr)

    def test_given_an_unknown_command_then_it_is_a_usage_error(self):
        result = self.run_ygg("nonsense")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ygg", result.stderr)

    def test_given_deploy_without_arguments_then_it_is_a_usage_error(self):
        self.assertEqual(self.run_ygg("deploy").returncode, 2)

    def test_given_deploy_from_a_missing_checkout_then_host_sh_refuses_it(self):
        result = self.run_ygg("deploy", "development", "heimdall-api", str(self.temp / "nowhere"), "1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no such directory", result.stderr)

    def test_given_vars_alone_then_its_help_names_ygg_vars(self):
        result = self.run_ygg("vars")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ygg vars", result.stdout)

    def test_given_ygg_sh_then_it_is_the_same_program(self):
        result = subprocess.run(["bash", str(SCRIPTS / "ygg.sh"), "catalog", "owner"], capture_output=True,
                                text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ygg("catalog", "owner", env=self.env).stdout)

    def test_given_the_launcher_through_a_symlink_then_it_runs_its_checkout(self):
        link = self.temp / "ygg"
        link.symlink_to(SCRIPTS / "ygg")
        result = subprocess.run([str(link), "catalog", "owner"], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)


class HostShTests(unittest.TestCase):
    def test_given_an_unknown_operation_then_host_sh_points_to_ygg(self):
        result = subprocess.run(["bash", str(SCRIPTS / "host.sh"), "menu"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ygg help", result.stderr)


class VersionTests(unittest.TestCase):
    def test_given_version_then_it_names_version_commit_and_checkout(self):
        for arguments in (("--version",), ("version",)):
            result = ygg(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^yggdrasil \S+ \(\S+\) at /\S+\n$")

    def test_given_no_git_checkout_then_the_version_comes_from_the_changelog(self):
        from yggcli import context
        directory = pathlib.Path(tempfile.mkdtemp(prefix="ygg-version-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [9.8.7] - 2030-01-01\n")
        self.assertEqual(context.read_version(directory), ("9.8.7", "unknown"))


class HelpCommandTests(unittest.TestCase):
    def test_given_help_then_the_overview_groups_the_commands(self):
        for arguments in (("help",), ("--help",), ("-h",)):
            result = ygg(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Variables and secrets", result.stdout)
            self.assertIn("vars get", result.stdout)

    def test_given_help_of_a_command_then_it_is_that_command_s_help(self):
        result = ygg("help", "vars", "get")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ygg vars get", result.stdout)
        self.assertIn("--reveal", result.stdout)


class NumberedMenuTests(unittest.TestCase):
    """`ygg` without a terminal: the menu as numbered lines, answers read line by line."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        from yggcli import context
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ygg-menu-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        v = context.vars_module()
        v.init(self.dir, confirm=lambda prompt: "saved")
        with v.Store.open(self.dir) as store:
            store.set("platform", "ENVIRONMENTS", "development,homologation,production", None, "set")
            store.set("platform", "DOMAIN", "example.com", None, "set")
            store.set("app:heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD", "s3cr3t-value", None, "set")
        self.env = dict(os.environ, YGG_SECRETS_DIR=str(self.dir), USER="tester")
        self.env.pop("YGG_ENVIRONMENT", None)

    def menu(self, *lines):
        return ygg(env=self.env, input="".join(line + "\n" for line in lines))

    def test_a_secret_is_shown_only_when_asked(self):
        result = self.menu("Applications", "heimdall-api", "production", "HEIMDALL_MASTER_USER_PASSWORD",
                           "Show value", "", "q")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("s3cr3t-value"), 1)
        self.assertIn("yggdrasil › Applications › heimdall-api › production", result.stdout)

    def test_a_secret_changed_from_the_menu_never_reaches_the_output(self):
        result = self.menu("Applications", "heimdall-api", "production", "HEIMDALL_MASTER_USER_PASSWORD",
                           "Change value", "n3w-value", "1", "q")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("$ ygg vars set heimdall-api@production HEIMDALL_MASTER_USER_PASSWORD=-", result.stdout)
        self.assertNotIn("n3w-value", result.stdout)
        got = ygg("vars", "get", "heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD", "--reveal", env=self.env)
        self.assertEqual(got.stdout, "n3w-value\n")

    def test_a_command_form_runs_its_command_line(self):
        result = self.menu("Variables and secrets", "vars get", "scope", "platform", "key", "DOMAIN", "▶ Run", "", "q")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("$ ygg vars get platform DOMAIN", result.stdout)
        self.assertIn("example.com", result.stdout)

    def test_end_of_input_leaves_the_menu(self):
        self.assertEqual(self.menu("Catalog").returncode, 0)


if __name__ == "__main__":
    unittest.main()
