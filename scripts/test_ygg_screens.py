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


class RunnerTests(unittest.TestCase):
    """app.Runner: the command line it runs, and the pause after it, which Ctrl-C ends too."""

    def run_effect(self, effect, pause=True, answer=None):
        from unittest import mock
        done = mock.Mock(returncode=3)
        with mock.patch.object(app.subprocess, "run", return_value=done) as run, \
                mock.patch("builtins.input", side_effect=answer or [""]) as typed, \
                mock.patch("builtins.print"):
            status = app.Runner(pause=pause)(effect, ("yggdrasil", "Host"))
        return status, run, typed

    def test_a_ygg_command_runs_ygg_py_with_the_crumbs(self):
        status, run, _ = self.run_effect(screens.Run(["vars", "get", "platform", "DOMAIN"], "x\n"))
        self.assertEqual(status, 3)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], [str(context.SCRIPTS / "ygg.py"), "vars", "get", "platform", "DOMAIN"])
        self.assertEqual(run.call_args.kwargs["input"], "x\n")
        self.assertEqual(run.call_args.kwargs["env"]["YGG_CRUMBS"], "yggdrasil › Host")

    def test_a_host_command_runs_host_sh(self):
        _, run, _ = self.run_effect(screens.Run(["config", "heimdall-api", "production"], program="host"))
        self.assertEqual(run.call_args.args[0], ["bash", str(context.SCRIPTS / "host.sh"), "config", "heimdall-api",
                                                 "production"])

    def test_without_pause_it_does_not_wait(self):
        _, _, typed = self.run_effect(screens.Run(["version"]), pause=False)
        typed.assert_not_called()

    def test_ctrl_c_at_the_pause_returns_to_the_menu(self):
        status, _, typed = self.run_effect(screens.Run(["version"]), answer=KeyboardInterrupt)
        typed.assert_called_once()
        self.assertEqual(status, 3)


class LineUITests(unittest.TestCase):
    def test_ctrl_c_at_a_numbered_prompt_goes_back_and_end_of_input_leaves(self):
        from unittest import mock
        from yggcli import ui
        stdin = mock.Mock(isatty=lambda: False, readline=mock.Mock(side_effect=[KeyboardInterrupt, ""]))
        line = ui.LineUI(stdin=stdin, stdout=mock.Mock())
        self.assertEqual(line.read_key(), "esc")
        self.assertEqual(line.read_key(), "eof")


class CursesUITests(unittest.TestCase):
    def test_a_terminal_too_small_ignores_every_key_but_resize_esc_and_q(self):
        import curses
        from unittest import mock
        from yggcli import ui
        terminal = ui.CursesUI(None)
        terminal.window = mock.Mock(getmaxyx=lambda: (5, 30))
        terminal.draw(screens.View(["yggdrasil"]))  # says the terminal is too small, and nothing else
        terminal.window.get_wch = mock.Mock(side_effect=["\r", "x", curses.KEY_DOWN, "q"])
        self.assertEqual(terminal.read_key(), "q")
        terminal.window.get_wch = mock.Mock(side_effect=["\r", curses.KEY_RESIZE])
        self.assertEqual(terminal.read_key(), "resize")
        terminal.window.get_wch = mock.Mock(side_effect=["\r", "\x1b"])
        self.assertEqual(terminal.read_key(), "esc")


    def test_a_failure_after_initscr_ends_curses_before_it_is_raised(self):
        import curses
        from unittest import mock
        from yggcli import ui
        with mock.patch.object(ui, "curses", wraps=curses) as fake, mock.patch.object(ui.locale, "setlocale"):
            fake.initscr.return_value = mock.Mock()
            fake.raw.side_effect = curses.error("raw")
            for name in ("noecho", "echo", "noraw", "endwin", "curs_set", "has_colors"):
                setattr(fake, name, mock.Mock())
            with self.assertRaises(curses.error):
                ui.CursesUI(None).__enter__()
            fake.endwin.assert_called_once()

    def test_make_ui_falls_back_to_numbered_lines_without_terminfo(self):
        import curses
        from unittest import mock
        from yggcli import ui
        with mock.patch.object(ui, "fits", return_value=True), \
                mock.patch.object(ui.sys, "stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch.object(ui.sys, "stdout", mock.Mock(isatty=lambda: True)), \
                mock.patch.dict(ui.os.environ, {"TERM": "xterm-ghostty"}), \
                mock.patch.object(ui.curses, "setupterm", side_effect=curses.error("setupterm")):
            ui.os.environ.pop("YGG_PLAIN", None)
            self.assertIsInstance(ui.make_ui(None), ui.LineUI)


class InlineUITests(unittest.TestCase):
    """What ui.InlineUI draws for a view (the pty tests in test_ygg_tui.py drive it for real)."""

    def render(self, view, rows=30, cols=60):
        from unittest import mock
        from yggcli import ui
        terminal = ui.InlineUI()
        with mock.patch.object(terminal, "size", return_value=(rows, cols)):
            return terminal.render(view)

    def plain(self, lines):
        import re
        return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]

    def test_a_hidden_value_is_drawn_as_bullets_and_left_as_hidden(self):
        from yggcli import prompt
        screen = screens.TextInput(("Password",), "s3cr3t", hidden=True)
        lines = self.plain(self.render(screen.view()))
        self.assertNotIn("s3cr3t", "".join(lines))
        self.assertIn("••••••", "".join(lines))
        args = prompt.parser().parse_args(["ask", "Password"])
        self.assertEqual(prompt.shown(args, screen, "s3cr3t"), "(hidden)")

    def test_a_long_question_wraps_and_the_rows_follow_the_cursor(self):
        question = "word " * 40
        picker = screens.Picker((question,), [sources.Choice(f"option-{n}") for n in range(30)])
        for _ in range(25):
            picker.key("down")
        lines = self.plain(self.render(picker.view()))
        self.assertTrue(all(len(line) < 60 for line in lines))
        self.assertEqual(" ".join(" ".join(lines).split()).count("word"), 40)
        options = [line for line in lines if "option-" in line]
        self.assertEqual(len(options), 10)
        self.assertIn("▸ option-25", options[-1])
        self.assertIn("26/30", lines[-1])


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

    def test_with_secret_chosen_first_any_value_is_typed_hidden_and_passed_on_stdin(self):
        _, ui, runner = drive(self.form(("vars", "set")), [
            ("text", "scope"), ("text", "heimdall-api@production"),
            ("text", "--secret | --no-secret"), ("text", "--secret"),
            ("text", "KEY=value"), ("text", "STRIPE_LIVE_VALUE"), ("text", "sk_live_123"),
            ("text", "▶ Run")])
        self.assertTrue(ui.views[-3].hidden)
        effect = runner.ran[0]
        self.assertEqual(effect.argv, ["vars", "set", "heimdall-api@production", "STRIPE_LIVE_VALUE=-", "--secret"])
        self.assertEqual(effect.stdin, "sk_live_123\n")
        self.assertNotIn("sk_live_123", " ".join(v.command for v in ui.views))

    def test_choosing_secret_after_plain_values_moves_them_to_stdin_in_order(self):
        _, ui, runner = drive(self.form(("vars", "set")), [
            ("text", "scope"), ("text", "heimdall-api@production"),
            ("text", "KEY=value"), ("text", "STRIPE_LIVE_VALUE"), ("text", "sk_live_123"),
            ("text", "KEY=value"), ("text", "HEIMDALL_MASTER_USER_PASSWORD"), ("text", "n3w"),
            ("text", "KEY=value"), ("text", "OTHER_VALUE"), ("text", "plain-2"),
            ("text", "--secret | --no-secret"), ("text", "--secret"),
            ("text", "▶ Run")])
        effect = runner.ran[0]
        self.assertEqual(effect.argv, ["vars", "set", "heimdall-api@production", "STRIPE_LIVE_VALUE=-",
                                       "HEIMDALL_MASTER_USER_PASSWORD=-", "OTHER_VALUE=-", "--secret"])
        self.assertEqual(effect.stdin, "sk_live_123\nn3w\nplain-2\n")
        after = ui.views[ui.views.index(next(v for v in ui.views if "--secret" in v.command)):]
        self.assertNotIn("sk_live_123", " ".join(v.command + " ".join(r.detail for r in v.rows) for v in after))

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
