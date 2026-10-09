"""Tests for the command tree (scripts/yggcli/tree.py): parity with the host scripts, the menu
placement and pick list of every argument, the command lines forms build, confirmations, the help."""

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
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


class StoreFixture(unittest.TestCase):
    """A scratch secrets directory with a store: platform ENVIRONMENTS, a secret and a plain value."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ygg-sources-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        v = context.vars_module()
        v.init(self.dir, confirm=lambda prompt: "saved")
        with v.Store.open(self.dir) as store:
            store.set("platform", "ENVIRONMENTS", "development,production", None, "set")
            store.set("platform", "DOMAIN", "example.com", None, "set")
            store.set("app:heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD", "s3cr3t-value", None, "set")
            store.set("env:production", "POSTGRES_HOST", "postgres", None, "set")
        self.ctx = context.Context(environ={"YGG_SECRETS_DIR": str(self.dir), "YGG_APPS_DIR": str(self.dir / "apps")})


class SourceTests(StoreFixture):
    def values(self, source, **values):
        return [c.value for c in sources.choices(source, self.ctx, values)]

    def test_scopes_cover_every_layer(self):
        scopes = self.values("scope")
        for scope in ("platform", "platform:acme", "@production", "heimdall-api", "heimdall-api@production"):
            self.assertIn(scope, scopes)

    def test_keys_are_those_of_the_chosen_scope_with_secrets_marked(self):
        choices = sources.choices("key", self.ctx, {"scope": "heimdall-api@production"})
        self.assertEqual([(c.value, c.note) for c in choices], [("HEIMDALL_MASTER_USER_PASSWORD", "secret")])
        self.assertEqual(self.values("key"), [])

    def test_host_environments_come_from_the_store(self):
        self.assertEqual(self.values("host-environment"), ["development", "production"])

    def test_an_application_s_environments_on_this_host(self):
        self.assertEqual(self.values("app-host-environment", application="heimdall-api"), ["development", "production"])

    def test_applications_of_an_environment_are_deployable_ones_that_deploy_there(self):
        self.assertIn("heimdall-api", self.values("app-of-environment", environment="production"))
        self.assertNotIn("traefik", self.values("app-of-environment", environment="production"))

    def test_this_host_s_applications_are_the_deployable_ones(self):
        self.assertEqual(self.values("host-application"), ["heimdall-api", "heimdall-ui", "fortuna-api", "fortuna-ui"])

    def test_changes_are_the_history_newest_first(self):
        changes = self.values("change")
        self.assertEqual(changes, sorted(changes, key=int, reverse=True))
        self.assertEqual(len(changes), 4)

    def test_services_are_the_platform_compose_services(self):
        self.assertIn("traefik", self.values("service"))

    def test_options_of_an_environment_and_of_an_application(self):
        self.assertIn("onDemand", self.values("environment-option", environment="development"))
        self.assertIn("mode", self.values("app-option", application="heimdall-api", environment="development"))

    def test_commands_are_the_tree_s(self):
        self.assertIn("vars get", self.values("command"))

    def test_a_list_that_cannot_be_read_is_empty(self):
        (self.dir / "vars.key").write_text("not a key\n")
        self.assertEqual(self.values("key", scope="platform"), [])

    def test_defaults_of_typed_values(self):
        deploy = tree.find(("deploy",))
        app_dir = next(a for a in deploy.args if a.dest == "app_dir")
        self.assertEqual(sources.default("directory", self.ctx, {"application": "heimdall-api"}, app_dir),
                         str(self.dir / "apps" / "heimdall-api"))
        backup = next(a for a in tree.find(("vars", "backup")).args if a.dest == "dir")
        self.assertTrue(sources.default("directory", self.ctx, {}, backup).endswith("yggdrasil-backups"))
        limit = next(a for a in tree.find(("vars", "history")).args if a.dest == "limit")
        self.assertEqual(sources.default("number", self.ctx, {}, limit), "50")
        file = next(a for a in tree.find(("vars", "import")).args if a.dest == "file")
        self.assertEqual(sources.default("file", self.ctx, {"scope": "heimdall-api@production"}, file),
                         str(self.dir / "production" / "heimdall-api.env"))

    def test_the_version_of_a_checkout_is_its_tag_and_commit(self):
        repo = self.dir / "repo"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run([*git, "tag", "v1.2.3"], check=True)
        commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout[:7]
        self.assertEqual(sources.checkout_version(str(repo)), f"1.2.3-{commit}")


if __name__ == "__main__":
    unittest.main()
