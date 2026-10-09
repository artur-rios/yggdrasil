"""`ygg.py prompt`: the arrow-key prompts of scripts/host.sh (its choose, ask and confirm) in a
terminal, drawn below what the operation printed before them (ui.InlineUI), which they leave as it
was, with one line for the question and its answer. The answer goes to file descriptor 3, which the
caller reads:

    answer=$(python3 scripts/ygg.py prompt choose -- "Which?" alpha beta 3>&1 1>/dev/tty)

    ygg.py prompt choose [--] <question> <option>...
    ygg.py prompt ask [--default=<text>] [--] <question>
    ygg.py prompt confirm [--yes] [--] <question>

Exit status: 0 answered (Esc on confirm answers no), 1 cancelled with Esc, 130 Ctrl-C, 2 a usage
error, no terminal, or any other failure: the caller then asks with its own numbered prompt."""

import argparse
import os
import sys

from . import screens, sources, ui


def parser():
    p = argparse.ArgumentParser(prog="ygg.py prompt")
    sub = p.add_subparsers(dest="kind", required=True)
    s = sub.add_parser("choose")
    s.add_argument("question")
    s.add_argument("options", nargs="+")
    s = sub.add_parser("ask")
    s.add_argument("question")
    s.add_argument("--default", default="")
    s = sub.add_parser("confirm")
    s.add_argument("question")
    s.add_argument("--yes", action="store_true")
    return p


def run_screen(terminal, screen):
    """Runs one screen until it is left: what it returned (CANCEL for Esc or q). Ctrl-C raises
    KeyboardInterrupt: it stops the operation, which Esc doesn't always."""
    while True:
        terminal.draw(screen.view())
        key = terminal.read_key()
        if key == "ctrl-c":
            raise KeyboardInterrupt
        if key == "eof":
            raise EOFError("the terminal closed")
        effect = screen.key(key)
        if isinstance(effect, screens.Pop):
            return effect.result
        if isinstance(effect, screens.Quit):
            return screens.CANCEL
        if isinstance(effect, screens.Push):  # the help: show it, then come back
            run_screen(terminal, effect.screen)


def answer(text):
    try:
        os.write(3, text.encode())
    except OSError:
        sys.stdout.write(text)


def answered(question, answer):
    """The line left in the terminal: `Then: stop`, `Display name: Shop`, `Sure? yes`."""
    return f"{question} {answer}" if question.endswith((":", "?")) else f"{question}: {answer}"


def shown(args, screen, result):
    """The answer as the line left in the terminal shows it; a hidden one never."""
    if result is screens.CANCEL:
        return "(Esc)"
    if args.kind == "confirm":
        return "yes" if result is True else "no"
    if getattr(screen, "hidden", False):
        return "(hidden)"
    return str(result) if result != "" else "(empty)"


def main(argv):
    args = parser().parse_args(argv)
    try:
        return ask(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # whatever it is, the caller still gets its answer, from its own prompt
        print(f"ygg: the arrow-key prompt failed ({error}); asking without it", file=sys.stderr)
        return 2


def in_a_terminal():
    """Whether stdin and stdout are a terminal that understands the few controls InlineUI sends
    (any but a dumb one) and has room for a prompt."""
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb" \
        and ui.fits_inline()


def ask(args):
    if not in_a_terminal():
        print(f"ygg: prompt needs a terminal of at least {ui.InlineUI.MIN_COLS}×{ui.InlineUI.MIN_ROWS}",
              file=sys.stderr)
        return 2
    crumbs = tuple(c for c in os.environ.get("YGG_CRUMBS", "yggdrasil").split(" › ") if c)
    if args.kind == "choose":
        screen = screens.Picker(crumbs + (args.question,), [sources.Choice(o) for o in args.options])
    elif args.kind == "ask":
        screen = screens.TextInput(crumbs + (args.question,), args.default)
    else:
        screen = screens.ConfirmScreen(crumbs, args.question, default=args.yes)
    with ui.InlineUI() as terminal:
        try:
            result = run_screen(terminal, screen)
        except KeyboardInterrupt:
            terminal.finish(answered(args.question, "(Ctrl-C)"))
            raise
        terminal.finish(answered(args.question, shown(args, screen, result)))
    if args.kind == "confirm":
        answer("y" if result is True else "n")
        return 0
    if result is screens.CANCEL:
        return 1
    answer(str(result))
    return 0
