"""`ygg.py prompt`: the arrow-key prompts of scripts/host.sh (its choose, ask and confirm) in a
terminal. The prompt is drawn on the terminal; the answer goes to file descriptor 3, which the
caller reads:

    answer=$(python3 scripts/ygg.py prompt choose "Which?" alpha beta 3>&1 1>/dev/tty)

    ygg.py prompt choose <question> <option>...
    ygg.py prompt ask <question> [--default <text>]
    ygg.py prompt confirm <question> [--yes]

Exit status: 0 answered (Esc on confirm answers no), 1 cancelled, 2 a usage error or no terminal
big enough, when the caller asks with its own numbered prompt instead."""

import argparse
import os
import sys

from . import context, screens, sources, ui


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
    """Runs one screen until it is left: what it returned (CANCEL for Esc or q)."""
    while True:
        terminal.draw(screen.view())
        effect = screen.key(terminal.read_key())
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


def main(argv):
    args = parser().parse_args(argv)
    if not (sys.stdin.isatty() and sys.stdout.isatty() and ui.fits()):
        print("ygg: prompt needs a terminal of at least 60×12", file=sys.stderr)
        return 2
    crumbs = tuple(c for c in os.environ.get("YGG_CRUMBS", "yggdrasil").split(" › ") if c)
    if args.kind == "choose":
        screen = screens.Picker(crumbs + (args.question,), [sources.Choice(o) for o in args.options])
    elif args.kind == "ask":
        screen = screens.TextInput(crumbs + (args.question,), args.default)
    else:
        screen = screens.ConfirmScreen(crumbs, args.question, default=args.yes)
    with ui.CursesUI(context.Context()) as terminal:
        result = run_screen(terminal, screen)
    if args.kind == "confirm":
        answer("y" if result is True else "n")
        return 0
    if result is screens.CANCEL:
        return 1
    answer(str(result))
    return 0
