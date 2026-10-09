"""Tests for the variables screens (scripts/yggcli/variables.py) against a scratch store: listing,
show value, change (with the scope choice), secret flag, remove, history and rollback, add, a broken
key, references, and the Applications screen. Commands are recorded, or run for real where the test
checks the store afterwards."""

import os
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yggcli import context, screens, variables  # noqa: E402
from test_ygg_screens import drive  # noqa: E402
from test_ygg_tree import StoreFixture  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parent


class Executor:
    """Runs each command for real against the scratch store, as app.Runner would, without pausing."""

    def __init__(self, env):
        self.env, self.ran = env, []

    def __call__(self, effect, crumbs):
        self.ran.append(effect)
        program = [sys.executable, str(SCRIPTS / "ygg.py")] if effect.program == "ygg" else ["bash", str(SCRIPTS / "host.sh")]
        return subprocess.run([*program, *effect.argv], input=effect.stdin, text=True, env=self.env,
                              capture_output=True).returncode


class VariablesScreenTests(StoreFixture):
    def setUp(self):
        super().setUp()
        v = context.vars_module()
        with v.Store.open(self.dir) as store:
            store.set("app:heimdall-api", "LOCALE", "en-US", None, "set")
            store.set("app:fortuna-api@production", "FORTUNA_SECRET", "${ref:heimdall-api:HEIMDALL_MASTER_USER_PASSWORD}", True, "set")
        self.env = dict(os.environ, YGG_SECRETS_DIR=str(self.dir), USER="tester")

    def screen(self, scope="heimdall-api@production"):
        return variables.VariablesScreen(self.ctx, ("yggdrasil", "Applications", "heimdall-api", "production"), scope)

    def get(self, scope, key):
        v = context.vars_module()
        with v.Store.open(self.dir, readonly=True) as store:
            return store.get(v.parse_scope(scope, None), key)

    def run_for_real(self, screen, keys):
        from yggcli import app
        from test_ygg_screens import FakeUI
        ui, executor = FakeUI(keys), Executor(self.env)
        app.App(ui, screen, executor).run()
        return ui, executor

    def test_the_list_is_the_resolved_set_with_origins_and_secrets_masked(self):
        rows = {r.label: r for r in self.screen().view().rows}
        self.assertEqual(rows["POSTGRES_HOST"].note, "@production")
        self.assertEqual(rows["LOCALE"].note, "heimdall-api")
        secret = rows["HEIMDALL_MASTER_USER_PASSWORD"]
        self.assertNotIn("s3cr3t-value", secret.detail)
        self.assertIn("secret", secret.note)
        self.assertIn("+ Add a variable", rows)

    def test_r_reveals_and_hides_every_secret(self):
        self.ctx.full_screen = True
        screen = self.screen()
        screen.key("r")
        self.assertIn("s3cr3t-value", {r.label: r for r in screen.view().rows}["HEIMDALL_MASTER_USER_PASSWORD"].detail)
        self.assertIn("--reveal", screen.view().command)
        screen.key("r")
        self.assertNotIn("s3cr3t-value", {r.label: r for r in screen.view().rows}["HEIMDALL_MASTER_USER_PASSWORD"].detail)

    def test_in_the_numbered_menu_show_value_says_it_prints_and_r_asks_first(self):
        self.ctx.full_screen = False
        screen = self.screen()
        _, ui, _ = drive(screen, [("text", "HEIMDALL_MASTER_USER_PASSWORD")])
        row = next(r for r in ui.views[-1].rows if r.label == "Show value")
        self.assertIn("prints", row.detail)
        self.assertNotIn("on this screen only", row.detail)
        for answer, revealed in (("No", False), ("Yes", True)):
            screen = self.screen()
            _, ui, _ = drive(screen, ["r", ("text", answer)])
            self.assertIn("scrollback", ui.views[1].crumbs[-1])
            self.assertEqual(screen.reveal, revealed)
        history = variables.HistoryScreen(self.ctx, ("h",), ["heimdall-api@production"], None)
        drive(history, ["r", ("text", "No")])
        self.assertFalse(history.reveal)

    def test_full_screen_show_value_is_drawn_on_this_screen_only(self):
        self.ctx.full_screen = True
        _, ui, _ = drive(self.screen(), [("text", "HEIMDALL_MASTER_USER_PASSWORD")])
        row = next(r for r in ui.views[-1].rows if r.label == "Show value")
        self.assertIn("on this screen only", row.detail)

    def test_show_value_draws_the_secret_and_runs_nothing(self):
        _, ui, runner = drive(self.screen(), [("text", "HEIMDALL_MASTER_USER_PASSWORD"), ("text", "Show value")])
        self.assertEqual(ui.views[-1].text, "s3cr3t-value")
        self.assertEqual(runner.ran, [])

    def test_change_value_of_a_secret_is_hidden_and_goes_on_stdin(self):
        ui, executor = self.run_for_real(self.screen(), [
            ("text", "HEIMDALL_MASTER_USER_PASSWORD"), ("text", "Change value"), ("text", "n3w-value"),
            ("pick", 1)])
        self.assertTrue(any(v.hidden for v in ui.views))
        effect = executor.ran[0]
        self.assertEqual(effect.argv, ["vars", "set", "heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD=-"])
        self.assertEqual(effect.stdin, "n3w-value\n")
        self.assertEqual(self.get("heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD")[0], "n3w-value")

    def test_changing_an_inherited_value_offers_the_shared_scope_first_or_an_override(self):
        screen = self.screen()
        variable = next(v for v in screen.variables if v.key == "POSTGRES_HOST")
        targets = screen.targets(variable)
        self.assertEqual(targets[0][0], "@production")
        self.assertIn("shared", targets[0][1])
        self.assertIn(("heimdall-api@production", "override it for heimdall-api in production only"), targets)
        self.run_for_real(self.screen(), [("text", "POSTGRES_HOST"), ("text", "Change value"), ("text", "db2"),
                                          ("text", "heimdall-api@production")])
        self.assertEqual(self.get("heimdall-api@production", "POSTGRES_HOST")[0], "db2")
        self.assertEqual(self.get("@production", "POSTGRES_HOST")[0], "postgres")

    def test_mark_as_not_secret_keeps_the_value(self):
        self.run_for_real(self.screen(), [("text", "HEIMDALL_MASTER_USER_PASSWORD"), ("text", "Mark as not secret")])
        self.assertEqual(self.get("heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD"), ("s3cr3t-value", False))

    def test_remove_asks_first_and_then_goes_back_to_the_list(self):
        _, executor = self.run_for_real(self.screen(), [("text", "LOCALE"), ("text", "Remove"), ("text", "No")])
        self.assertEqual(executor.ran, [])
        screen = self.screen()
        from yggcli import app
        from test_ygg_screens import FakeUI
        application = app.App(FakeUI([("text", "LOCALE"), ("text", "Remove"), ("text", "Yes")]), screen, Executor(self.env))
        application.run()
        self.assertIsNone(self.get("heimdall-api", "LOCALE"))

    def test_history_rolls_a_change_back(self):
        self.run_for_real(self.screen(), [("text", "LOCALE"), ("text", "Change value"), ("text", "pt-BR"),
                                          ("text", "heimdall-api")])
        self.assertEqual(self.get("heimdall-api", "LOCALE")[0], "pt-BR")
        v = context.vars_module()
        with v.Store.open(self.dir, readonly=True) as store:
            newest = store.history("app:heimdall-api", "LOCALE", 1)[0]["id"]
        self.run_for_real(self.screen(), [("text", "LOCALE"), ("text", "History"), ("text", f"#{newest}"),
                                          ("text", "Roll back"), ("text", "Yes")])
        self.assertEqual(self.get("heimdall-api", "LOCALE")[0], "en-US")

    def test_add_a_variable_asks_name_kind_value_and_scope(self):
        self.run_for_real(self.screen(), [("text", "+ Add a variable"), ("text", "NEW_TOKEN"), ("text", "secret"),
                                          ("text", "t0k"), ("text", "heimdall-api@production")])
        self.assertEqual(self.get("heimdall-api@production", "NEW_TOKEN"), ("t0k", True))

    def test_a_reference_shows_where_it_points_and_its_value(self):
        screen = self.screen("fortuna-api@production")
        variable = next(v for v in screen.variables if v.key == "FORTUNA_SECRET")
        self.assertEqual(variable.resolved, "s3cr3t-value")
        _, ui, _ = drive(screen, [("text", "FORTUNA_SECRET"), ("text", "Show value")])
        self.assertIn("s3cr3t-value", ui.views[-1].text)
        self.assertIn("${ref:heimdall-api:HEIMDALL_MASTER_USER_PASSWORD}", ui.views[-1].text)

    def test_a_wrong_key_is_explained_and_offers_the_check(self):
        (self.dir / "vars.key").write_text("not a key\n")
        screen = self.screen()
        view = screen.view()
        self.assertIn("can't be read", view.message)
        self.assertEqual([r.label for r in view.rows], ["Check the store", "Read about the store"])
        _, _, runner = drive(self.screen(), [("text", "Check the store")])
        self.assertEqual(runner.ran[0].argv, ["vars", "check", "--usable"])

    def test_d_deploys_and_asks_first_for_an_environment_that_stays_up(self):
        _, _, runner = drive(self.screen(), ["d", ("text", "Yes")])
        self.assertEqual(runner.ran[0].argv, ["deploy", "production", "heimdall-api"])

    def test_e_edits_the_scope_and_h_shows_its_history(self):
        _, _, runner = drive(self.screen(), ["e"])
        self.assertEqual(runner.ran[0].argv, ["vars", "edit", "heimdall-api@production"])
        _, ui, _ = drive(self.screen(), ["h"])
        self.assertTrue(any(r.label.startswith("#") for r in ui.views[-1].rows))

    def test_copy_command_lists_the_command_lines(self):
        _, ui, _ = drive(self.screen(), [("text", "LOCALE"), ("text", "Copy command")])
        self.assertIn("ygg vars get heimdall-api LOCALE", ui.views[-1].text)


class ApplicationsScreenTests(StoreFixture):
    def apps(self):
        return variables.ApplicationsScreen(self.ctx, ("yggdrasil", "Applications"))

    def test_it_lists_this_host_s_applications_and_the_group_s_commands(self):
        labels = [r.label for r in self.apps().view().rows]
        self.assertIn("heimdall-api", labels)
        self.assertIn("deploy", labels)
        self.assertIn("add", labels)

    def test_an_application_with_variables_opens_its_screen(self):
        _, ui, _ = drive(self.apps(), [("text", "heimdall-api"), ("text", "production")])
        self.assertIn("HEIMDALL_MASTER_USER_PASSWORD", [r.label for r in ui.views[-1].rows])

    def test_an_application_without_variables_offers_to_create_them(self):
        _, _, runner = drive(self.apps(), [("text", "heimdall-api"), ("text", "development"), ("text", "Yes")])
        self.assertEqual((runner.ran[0].program, runner.ran[0].argv), ("host", ["create-variables", "heimdall-api", "development"]))

    def test_without_a_store_it_is_host_sh_config(self):
        (self.dir / "vars.db").unlink()
        _, _, runner = drive(self.apps(), [("text", "heimdall-api"), ("text", "production")])
        self.assertEqual((runner.ran[0].program, runner.ran[0].argv), ("host", ["config", "heimdall-api", "production"]))


class VariablesMenuTests(StoreFixture):
    def test_browse_a_scope_and_the_vars_commands(self):
        menu = variables.VariablesMenu(self.ctx, ("yggdrasil", "Variables and secrets"))
        labels = [r.label for r in menu.view().rows]
        self.assertEqual(labels[0], "Browse a scope")
        self.assertIn("vars get", labels)
        _, ui, _ = drive(menu, [("text", "Browse a scope"), ("text", "platform")])
        self.assertIn("DOMAIN", [r.label for r in ui.views[-1].rows])

    def test_without_a_store_it_offers_to_create_one(self):
        (self.dir / "vars.db").unlink()
        menu = variables.VariablesMenu(self.ctx, ("yggdrasil", "Variables and secrets"))
        _, _, runner = drive(menu, [("text", "Create the variables store")], statuses=[0, 0])
        self.assertEqual([r.argv for r in runner.ran], [["vars", "init"], ["vars", "import", "--all"]])


if __name__ == "__main__":
    unittest.main()
