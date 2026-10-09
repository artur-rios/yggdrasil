"""Tests for the command tree (scripts/yggcli/tree.py): parity with the host scripts, the menu
placement and pick list of every argument, the command lines forms build, confirmations, the help."""

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yggcli import context, sources, tree  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parent


class ParityTests(unittest.TestCase):
    def test_every_command_has_a_place_in_the_menu(self):
        for command in tree.commands_list():
            self.assertIn(command.menu, tree.TOP, command.name)

    def test_every_argument_that_takes_a_value_has_a_pick_list_or_is_typed(self):
        for command in tree.commands_list():
            for arg in command.args:
                if arg.is_flag or arg.choices:
                    continue
                self.assertIn(arg.source, sources.KNOWN, f"{command.name}: {arg.metavar}")

    def test_every_vars_command_is_mounted(self):
        sub = next(a for a in context.vars_module().parser()._actions if hasattr(a, "choices") and isinstance(a.choices, dict))
        paths = {c.path for c in tree.commands_list()}
        for name in sub.choices:
            self.assertIn(("vars", name), paths)

    def test_every_catalog_py_read_command_is_in_the_tree(self):
        paths = {c.path for c in tree.commands_list()}
        names = set()
        for line in context.catalog_module().__doc__.splitlines():
            match = re.match(r"\s+python3 scripts/catalog\.py ([a-z-]+)(?: \| ([a-z-]+))?", line)
            if match:
                names.update(n for n in match.groups() if n)
        self.assertIn("validate", names)
        for name in names - {"add-application"}:
            self.assertIn(("catalog", name), paths)

    def test_every_platform_sh_command_and_option_is_in_the_tree(self):
        text = (SCRIPTS / "platform.sh").read_text()
        usage = re.search(r'usage="usage: (.*)"', text)[1]
        names = set(re.findall(r"(?:platform\.sh |\| )([a-z]+)", usage))
        self.assertEqual(names, {"up", "down", "ps", "config", "logs"})
        for name in names:
            command = tree.find(("platform", name))
            self.assertIsNotNone(command, name)
            self.assertIn("--last-good", [f for a in command.args for f in a.flags])
        self.assertIn("service", [a.dest for a in tree.find(("platform", "logs")).args])

    def test_deploy_sh_arguments_are_in_the_tree(self):
        self.assertEqual([a.dest for a in tree.find(("deploy",)).args if a.positional],
                         ["environment", "application", "app_dir", "version"])

    def test_every_host_sh_operation_is_reachable(self):
        text = (SCRIPTS / "host.sh").read_text()
        operations = re.findall(r"^  ([a-z-]+)\) ", text.split("\ncase ${1:-} in\n")[-1], re.M)
        reach = {"deploy-app": ("deploy",), "create-variables": None}
        for operation in operations:
            path = reach.get(operation, (operation,))
            if path is None:
                continue
            self.assertTrue(any(c.path[:len(path)] == path for c in tree.commands_list()), operation)


class ArgvTests(unittest.TestCase):
    def test_given_positionals_and_a_flag_then_they_come_in_order(self):
        command = tree.find(("vars", "get"))
        argv = tree.argv_for(command, {"scope": "heimdall-api@production", "key": "K", "reveal": True})
        self.assertEqual(argv, ["vars", "get", "heimdall-api@production", "K", "--reveal"])

    def test_given_a_mutually_exclusive_choice_then_only_that_flag_is_written(self):
        command = tree.find(("vars", "set"))
        group = next(a for a in command.args if a.group is not None)
        argv = tree.argv_for(command, {"scope": "platform", "assignments": ["A=1"], group.field: "--no-secret"})
        self.assertEqual(argv, ["vars", "set", "platform", "A=1", "--no-secret"])

    def test_given_an_option_at_its_default_then_it_is_left_out(self):
        command = tree.find(("vars", "history"))
        self.assertEqual(tree.argv_for(command, {"limit": "50"}), ["vars", "history"])
        self.assertEqual(tree.argv_for(command, {"limit": "20"}), ["vars", "history", "--limit", "20"])

    def test_given_a_choice_option_then_its_value_follows_the_flag(self):
        command = tree.find(("vars", "import"))
        argv = tree.argv_for(command, {"all": True, "move_up": "yes"})
        self.assertEqual(argv, ["vars", "import", "--all", "--move-up", "yes"])

    def test_given_a_required_argument_missing_then_the_form_says_which(self):
        command = tree.find(("vars", "render"))
        self.assertEqual(tree.missing(command, {"application": "heimdall-api"}), "environment is needed")
        self.assertIsNone(tree.missing(command, {"application": "heimdall-api", "environment": "production"}))

    def test_given_a_later_optional_without_the_earlier_one_then_it_is_refused(self):
        command = tree.find(("vars", "history"))
        self.assertEqual(tree.missing(command, {"key": "K"}), "key needs scope first")

    def test_command_line_quotes_what_the_shell_would_split(self):
        self.assertEqual(tree.command_line(["vars", "set", "platform", "A=a b"]), "ygg vars set platform 'A=a b'")


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context.Context(environ={})

    def test_destructive_commands_ask(self):
        cases = [(("vars", "unset"), {"scope": "platform", "keys": ["A", "B"]}, "Delete A, B from platform?"),
                 (("vars", "rollback"), {"id": "7"}, "Roll back change 7?"),
                 (("vars", "import"), {"replace": True}, "Overwrite the variables that are already set?"),
                 (("env", "stop"), {"environment": "development"}, "Stop every application of development?"),
                 (("deploy",), {"environment": "production", "application": "heimdall-api"},
                  "Deploy heimdall-api to production, which stays up?")]
        for path, values, question in cases:
            self.assertEqual(tree.confirmation(tree.find(path), values, self.ctx), question, path)
        self.assertIn("platform", tree.confirmation(tree.find(("platform", "down")), {}, self.ctx))

    def test_harmless_commands_do_not_ask(self):
        self.assertIsNone(tree.confirmation(tree.find(("vars", "import")), {"replace": False}, self.ctx))
        self.assertIsNone(tree.confirmation(tree.find(("deploy",)), {"environment": "development"}, self.ctx))
        self.assertIsNone(tree.confirmation(tree.find(("vars", "get")), {}, self.ctx))


class HelpTests(unittest.TestCase):
    def test_overview_lists_every_group_and_command(self):
        text = tree.overview()
        for group in tree.TOP:
            self.assertIn(group, text)
        for command in tree.commands_list():
            self.assertIn(command.usage(), text)

    def test_help_of_a_mounted_command_names_ygg(self):
        self.assertIn("usage: ygg vars get", tree.help_text(("vars", "get")))
        self.assertIn("usage: ygg env stop", tree.help_text(("env", "stop")))

    def test_help_of_a_group_lists_its_commands(self):
        self.assertIn("logs", tree.help_text(("platform",)))

    def test_help_of_an_unknown_command_is_a_usage_error(self):
        self.assertEqual(tree.print_help(["nope"]), 2)

    def test_help_topics_written_as_one_word_are_split(self):
        self.assertEqual(tree.print_help(["vars get"]), 0)


class CheckTests(unittest.TestCase):
    def test_checks(self):
        self.assertIsNone(sources.check_scope("heimdall-api@production"))
        self.assertIn("is not a scope", sources.check_scope("a b"))
        self.assertIsNone(sources.check_key("DB_PASSWORD"))
        self.assertIsNotNone(sources.check_key("1X"))
        self.assertIsNone(sources.check_number(""))
        self.assertIsNotNone(sources.check_number("x"))
        self.assertIn("single quote", sources.check_value("it's"))


if __name__ == "__main__":
    unittest.main()
