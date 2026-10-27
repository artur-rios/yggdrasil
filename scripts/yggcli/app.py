"""The menu's loop: draws the screen on top, hands it a key, carries out the effect. A command runs
outside the full screen, in the terminal, as `ygg <argv>` through scripts/ygg.py (the command line
the footer showed) or as scripts/host.sh <argv>."""

import os
import shlex
import signal
import subprocess
import sys

from . import context, screens, tree


def _ignore(signum, frame):
    """While a command runs, Ctrl-C is the command's: the menu stays."""


PROMPT = "Enter to return to the menu "


def _wait_for_return():
    """The pause after a command: Enter, Ctrl-C or the end of input return to the menu.

    On a terminal the keys are read one by one with the terminal's signals off, so Ctrl-C is a key, as in
    the menu (ui.py), not a SIGINT: a SIGINT that came after the prompt but before input() was reading
    went unnoticed, and the pause went on waiting, Ctrl-C lost. Elsewhere (input piped in), input()."""
    if not sys.stdin.isatty():
        try:
            input(PROMPT)
        except EOFError:
            pass
        except KeyboardInterrupt:
            print(flush=True)
        return
    import termios  # a terminal's, POSIX only; input() above needs none
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
    mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
    try:
        # The signals go off before the prompt shows: from the moment it can be answered, Ctrl-C is a key.
        termios.tcsetattr(fd, termios.TCSADRAIN, mode)
        try:
            print(PROMPT, end="", flush=True)
            # Enter (\r, or \n through ICRNL), Ctrl-C, Ctrl-D, or the terminal gone; any other key is ignored.
            while os.read(fd, 1) not in (b"", b"\r", b"\n", b"\x03", b"\x04"):
                pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except KeyboardInterrupt:  # Ctrl-C before the switch, while the command was ending
        pass
    print(flush=True)


class Runner:
    def __init__(self, pause=True):
        self.pause = pause

    def __call__(self, effect, crumbs):
        if effect.program == "host":
            argv = ["bash", str(context.SCRIPTS / "host.sh"), *effect.argv]
            shown = "scripts/host.sh " + shlex.join(effect.argv)
        else:
            argv = [sys.executable, str(context.SCRIPTS / "ygg.py"), *effect.argv]
            shown = tree.command_line(effect.argv)
        print(f"\n$ {shown}", flush=True)
        previous = signal.signal(signal.SIGINT, _ignore)
        try:
            status = subprocess.run(argv, input=effect.stdin, text=True,
                                    env=dict(os.environ, YGG_CRUMBS=" › ".join(crumbs))).returncode
        finally:
            signal.signal(signal.SIGINT, previous)
        if status != 0:
            print(f"(exit {status})", flush=True)
        if self.pause:
            _wait_for_return()  # Ctrl-C at the pause returns too: the menu stays
        return status


class App:
    def __init__(self, ui, screen, runner):
        self.ui, self.stack, self.runner = ui, [screen], runner

    def run(self):
        while self.stack:
            top = self.stack[-1]
            self.ui.draw(top.view())
            key = self.ui.read_key()
            if key == "eof":
                return 0
            effect = top.key(key)
            while effect is not None:
                effect = self.apply(effect)
        return 0

    def apply(self, effect):
        if isinstance(effect, screens.Push):
            self.stack.append(effect.screen)
            return None
        if isinstance(effect, screens.Pop):
            self.stack.pop()
            return self.stack[-1].on_result(effect.result) if self.stack else None
        if isinstance(effect, screens.Run):
            crumbs = self.stack[-1].crumbs if self.stack else ()
            status = self.ui.suspend(lambda: self.runner(effect, crumbs))
            for screen in self.stack:
                screen.refresh()
            return effect.then(status) if effect.then else None
        if isinstance(effect, screens.Quit):
            self.stack.clear()
            return None
        raise TypeError(f"not an effect: {effect!r}")


def menu(ctx, start=None):
    """The menu, from the top; with start=(application, environment) (`ygg config`), at that
    application's variables, asking for what is not given."""
    from . import ui as terminal, variables
    top = screens.MenuScreen(ctx)  # before the full screen: it fails here when PyYAML is missing
    with terminal.make_ui(ctx) as ui:
        ctx.full_screen = isinstance(ui, terminal.CursesUI)
        application = App(ui, top, Runner(pause=ui.interactive))
        if start is not None:
            apps = variables.ApplicationsScreen(ctx, ("yggdrasil", "Applications"))
            application.stack.append(apps)
            effect = apps.open_app(*start) if start[0] else None
            while effect is not None:
                effect = application.apply(effect)
        return application.run()
