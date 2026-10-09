"""Tests for the menu's generic screens and forms (scripts/yggcli/screens.py, app.py), driven by keys
through app.App with a fake terminal and a runner that records the commands."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yggcli import app, context, screens, sources, tree  # noqa: E402
from test_ygg_tree import StoreFixture  # noqa: E402


class FakeUI:
    interactive = False

    def __init__(self, keys):
        self.keys, self.views = list(keys), []

    def draw(self, view):
        self.views.append(view)

    def read_key(self):
        return self.keys.pop(0) if self.keys else "eof"

    def suspend(self, action):
        return action()


class Recorder:
    """Records each Run and answers with the next status (0 when none is left)."""

    def __init__(self, statuses=()):
        self.ran, self.statuses = [], list(statuses)

    def __call__(self, effect, crumbs):
        self.ran.append(effect)
        return self.statuses.pop(0) if self.statuses else 0


def drive(screen, keys, statuses=()):
    ui, runner = FakeUI(keys), Recorder(statuses)
    application = app.App(ui, screen, runner)
    application.run()
    return application, ui, runner


class PickerTests(unittest.TestCase):
    def picker(self, **kwargs):
        return screens.Picker(("t",), [sources.Choice("alpha"), sources.Choice("beta", "second")], **kwargs)

    def test_typing_filters_and_enter_picks_the_first_match(self):
        picker = self.picker()
        for key in "be":
            picker.key(key)
        self.assertEqual([r.label for r in picker.view().rows], ["beta"])
        self.assertEqual(picker.key("enter").result, "beta")

    def test_arrows_move_and_esc_cancels(self):
        picker = self.picker()
        picker.key("down")
        self.assertEqual(picker.view().cursor, 1)
        self.assertIs(picker.key("esc").result, screens.CANCEL)

    def test_an_open_picker_takes_typed_text_after_its_check(self):
        picker = self.picker(open=True, check=sources.check_key)
        for key in "1x":
            picker.key(key)
        self.assertIsNone(picker.key("enter"))
        self.assertIn("variable name", picker.view().message)
        picker.key("backspace"); picker.key("backspace")
        for key in "NEW_KEY":
            picker.key(key)
        self.assertEqual(picker.key("enter").result, "NEW_KEY")

    def test_a_closed_picker_only_filters(self):
        picker = self.picker()
        self.assertIsNone(picker.key(("text", "zzz")))
        self.assertEqual(picker.view().rows, [])

    def test_numbers_and_exact_labels_pick(self):
        self.assertEqual(self.picker().key(("pick", 2)).result, "beta")
        self.assertEqual(self.picker().key(("text", "alpha")).result, "alpha")
        self.assertIsNone(self.picker().key(("pick", 9)))

    def test_q_types_in_a_picker(self):
        picker = self.picker(open=True)
        self.assertIsNone(picker.key("q"))
        self.assertEqual(picker.filter, "q")


class TextInputTests(unittest.TestCase):
    def test_typing_and_enter(self):
        field = screens.TextInput(("t",), "ab")
        field.key("backspace")
        field.key("c")
        self.assertEqual(field.key("enter").result, "ac")

    def test_hidden_input_is_marked_hidden_and_esc_cancels(self):
        field = screens.TextInput(("t",), hidden=True)
        self.assertTrue(field.view().hidden)
        self.assertIs(field.key("esc").result, screens.CANCEL)

    def test_an_empty_line_keeps_the_default_unless_hidden(self):
        self.assertEqual(screens.TextInput(("t",), "d").key(("text", "")).result, "d")
        self.assertEqual(screens.TextInput(("t",), "d", hidden=True).key(("text", "")).result, "")

    def test_a_failing_check_keeps_the_field_open(self):
        field = screens.TextInput(("t",), "it's", check=sources.check_value)
        self.assertIsNone(field.key("enter"))
        self.assertIn("single quote", field.view().message)


class ConfirmTests(unittest.TestCase):
    def test_default_is_no_and_y_n_answer(self):
        confirm = screens.ConfirmScreen(("t",), "Sure?")
        self.assertEqual(confirm.view().cursor, 1)
        self.assertIs(confirm.key("enter").result, False)
        self.assertIs(screens.ConfirmScreen(("t",), "Sure?").key("y").result, True)
        self.assertIs(screens.ConfirmScreen(("t",), "Sure?").key("n").result, False)
        self.assertEqual(screens.ConfirmScreen(("t",), "Sure?", default=True).view().cursor, 0)


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context.Context(environ={})

    def test_the_top_menu_is_the_groups(self):
        menu = screens.MenuScreen(self.ctx)
        self.assertEqual([r.label for r in menu.view().rows], list(tree.TOP))

    def test_a_group_of_one_command_without_arguments_runs_it(self):
        _, _, runner = drive(screens.MenuScreen(self.ctx), [("text", "Status")])
        self.assertEqual(runner.ran[0].argv, ["status"])

    def test_q_quits_and_question_mark_shows_the_overview(self):
        application, ui, _ = drive(screens.MenuScreen(self.ctx), ["?"])
        self.assertIn("Variables and secrets", ui.views[-1].text)
        application, _, _ = drive(screens.MenuScreen(self.ctx), ["q"])
        self.assertEqual(application.stack, [])


class FormTests(StoreFixture):
    def form(self, path):
        return screens.FormScreen(self.ctx, ("yggdrasil",), tree.find(path))

    def test_a_form_builds_the_command_line_from_picked_values(self):
        form = self.form(("vars", "get"))
        _, ui, runner = drive(form, [("text", "scope"), ("text", "platform"), ("text", "key"), ("text", "DOMAIN"),
                                     ("text", "[ ] --reveal"), ("text", "▶ Run")])
        self.assertEqual(runner.ran[0].argv, ["vars", "get", "platform", "DOMAIN", "--reveal"])
        self.assertIn("ygg vars get platform DOMAIN --reveal", [v.command for v in ui.views])

    def test_a_missing_argument_is_named_and_nothing_runs(self):
        _, ui, runner = drive(self.form(("vars", "get")), [("text", "▶ Run")])
        self.assertEqual(runner.ran, [])
        self.assertEqual(ui.views[-1].message, "scope is needed")

    def test_a_secret_assignment_is_typed_hidden_and_passed_on_stdin(self):
        _, ui, runner = drive(self.form(("vars", "set")), [
            ("text", "scope"), ("text", "heimdall-api@production"),
            ("text", "KEY=value"), ("text", "HEIMDALL_MASTER_USER_PASSWORD"), ("text", "n3w"),
            ("text", "▶ Run")])
        self.assertTrue(any(v.hidden for v in ui.views))
        effect = runner.ran[0]
        self.assertEqual(effect.argv, ["vars", "set", "heimdall-api@production", "HEIMDALL_MASTER_USER_PASSWORD=-"])
        self.assertEqual(effect.stdin, "n3w\n")
        self.assertNotIn("n3w", " ".join(v.command for v in ui.views))

    def test_a_plain_assignment_offers_the_current_value(self):
        _, ui, runner = drive(self.form(("vars", "set")), [
            ("text", "scope"), ("text", "platform"), ("text", "KEY=value"), ("text", "DOMAIN"), ("text", ""),
            ("text", "▶ Run")])
        self.assertEqual(runner.ran[0].argv, ["vars", "set", "platform", "DOMAIN=example.com"])

    def test_a_destructive_command_asks_and_no_runs_nothing(self):
        keys = [("text", "scope"), ("text", "platform"), ("text", "KEY"), ("text", "DOMAIN"), ("text", "▶ Run")]
        _, _, runner = drive(self.form(("vars", "unset")), keys + [("text", "No")])
        self.assertEqual(runner.ran, [])
        _, _, runner = drive(self.form(("vars", "unset")), keys + [("text", "Yes")])
        self.assertEqual(runner.ran[0].argv, ["vars", "unset", "platform", "DOMAIN"])

    def test_a_choice_option_and_a_group(self):
        _, _, runner = drive(self.form(("vars", "check")), [
            ("text", "--platform | --usable"), ("text", "--usable"), ("text", "▶ Run")])
        self.assertEqual(runner.ran[0].argv, ["vars", "check", "--usable"])
        _, _, runner = drive(self.form(("vars", "import")), [
            ("text", "[ ] --all"), ("text", "--move-up"), ("text", "no"), ("text", "▶ Run")])
        self.assertEqual(runner.ran[0].argv, ["vars", "import", "--all", "--move-up", "no"])

    def test_backspace_clears_the_field_under_the_cursor(self):
        form = self.form(("vars", "get"))
        drive(form, [("text", "scope"), ("text", "platform")])
        form.cursor = 0
        form.key("backspace")
        self.assertIsNone(form.values.get("scope"))

    def test_a_failed_run_says_so_on_the_form(self):
        _, ui, _ = drive(self.form(("vars", "get")), [("text", "scope"), ("text", "platform"), ("text", "key"),
                                                       ("text", "NOPE"), ("text", "▶ Run")], statuses=[1])
        self.assertIn("exit 1", ui.views[-1].message)


if __name__ == "__main__":
    unittest.main()
