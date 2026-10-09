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

    def test_given_env_help_after_an_environment_then_it_is_that_action_s_help(self):
        result = self.run_ygg("env", "stop", "production", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ygg env stop", result.stdout)

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


class PromptFallbackTests(unittest.TestCase):
    def test_given_no_terminal_then_prompt_refuses_with_2(self):
        result = ygg("prompt", "choose", "Which?", "a", "b")
        self.assertEqual(result.returncode, 2)
        self.assertIn("terminal", result.stderr)


class InterruptTests(unittest.TestCase):
    def test_ctrl_c_outside_a_command_exits_130_without_a_traceback(self):
        import contextlib
        import io
        from unittest import mock
        from yggcli import main
        with mock.patch.object(main, "run", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main([]), 130)


class MissingPackagesTests(unittest.TestCase):
    """Without PyYAML, what needs the catalog says what to install instead of a traceback."""

    def test_given_no_pyyaml_then_the_menu_and_the_help_say_how_to_install_it(self):
        hidden = pathlib.Path(tempfile.mkdtemp(prefix="ygg-hidden-"))
        self.addCleanup(shutil.rmtree, hidden, ignore_errors=True)
        (hidden / "yaml").mkdir()
        (hidden / "yaml" / "__init__.py").write_text("raise ImportError('hidden by the test')\n")
        env = dict(os.environ, PYTHONPATH=str(hidden))
        for arguments in ((), ("help",), ("--help",), ("help", "vars", "get")):
            result = ygg(*arguments, env=env)
            self.assertEqual(result.returncode, 1, arguments)
            self.assertNotIn("Traceback", result.stderr, arguments)
            self.assertIn("PyYAML is missing", result.stderr, arguments)
            self.assertIn("ygg.sh install", result.stderr, arguments)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        from yggcli import complete, context
        self.complete = complete
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ygg-complete-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        v = context.vars_module()
        v.init(self.dir, confirm=lambda prompt: "saved")
        with v.Store.open(self.dir) as store:
            store.set("platform", "DOMAIN", "example.com", None, "set")
            store.set("app:heimdall-api@production", "HEIMDALL_X", "1", None, "set")
        self.ctx = context.Context(environ={"YGG_SECRETS_DIR": str(self.dir)})

    def words(self, line):
        return self.complete.complete(line, self.ctx)

    def test_commands_and_subcommands(self):
        self.assertIn("vars", self.words("ygg "))
        self.assertEqual(self.words("ygg va"), ["vars"])
        self.assertEqual(self.words("ygg vars g"), ["get"])
        self.assertEqual(self.words("ygg completion "), ["bash"])

    def test_scopes_are_completed_after_the_last_word_break(self):
        self.assertEqual(self.words("ygg vars get heimdall-api@pr"), ["production"])
        self.assertIn("production", self.words("ygg vars get heimdall-api@"))

    def test_keys_of_the_scope_typed_before(self):
        self.assertEqual(self.words("ygg vars get heimdall-api@production "), ["HEIMDALL_X"])
        self.assertEqual(self.words("ygg vars set platform DOM"), ["DOMAIN="])

    def test_options_and_typed_values(self):
        self.assertEqual(self.words("ygg vars get platform DOMAIN --re"), ["--reveal"])
        self.assertEqual(self.words("ygg vars history --limit "), [])
        self.assertIn("traefik", self.words("ygg platform logs "))

    def test_the_script_is_valid_bash_and_registers_ygg(self):
        result = ygg("completion", "bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("complete -o default -F _ygg ygg", result.stdout)
        self.assertEqual(subprocess.run(["bash", "-n"], input=result.stdout, text=True).returncode, 0)

    def test_complete_entry_point_prints_one_word_per_line(self):
        self.assertEqual(ygg("__complete", "ygg vars g").stdout, "get\n")


class SelfInstallTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ygg-install-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "bin").mkdir()
        self.env = dict(os.environ, YGG_BIN_DIR=str(self.dir / "bin"), YGG_COMPLETION_DIR=str(self.dir / "completion"))

    def test_it_links_and_writes_then_says_it_is_done(self):
        first = ygg("self-install", env=self.env)
        self.assertEqual(first.returncode, 0, first.stderr)
        link = self.dir / "bin" / "ygg"
        self.assertEqual(link.resolve(), (SCRIPTS / "ygg").resolve())
        self.assertIn("complete -o default -F _ygg ygg", (self.dir / "completion" / "ygg").read_text())
        second = ygg("self-install", env=self.env)
        self.assertIn("already runs", second.stdout)
        self.assertIn("up to date", second.stdout)

    def test_a_file_in_the_way_is_not_replaced(self):
        (self.dir / "bin" / "ygg").write_text("#!/bin/sh\n")
        result = ygg("self-install", env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a link", result.stderr)

    def test_without_sudo_it_says_so_and_fails(self):
        import contextlib
        import io
        from unittest import mock
        from yggcli import context, install
        missing = FileNotFoundError(2, "No such file or directory", "sudo")
        err = io.StringIO()
        with mock.patch.object(install.subprocess, "run", side_effect=missing), contextlib.redirect_stderr(err):
            status = install.self_install(context.Context(), environ=self.env)
        self.assertEqual(status, 1)
        self.assertIn("sudo", err.getvalue())


if __name__ == "__main__":
    unittest.main()
