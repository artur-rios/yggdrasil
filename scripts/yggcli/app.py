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
            try:
                input("Enter to return to the menu ")
            except EOFError:
                pass
            except KeyboardInterrupt:  # Ctrl-C at the pause returns too: the menu stays
                print(flush=True)
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
