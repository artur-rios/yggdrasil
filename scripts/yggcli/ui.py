"""The terminal side of the menu: CursesUI draws a screen's View full screen and reads keys raw;
LineUI prints it as numbered lines and reads answers line by line, for no terminal (a pipe, a test,
YGG_PLAIN) or one too small for the full screen."""

import curses
import getpass
import locale
import os
import shutil
import sys
import textwrap


class LineUI:
    """A number picks a row; a row's exact text picks it; other text is typed (a value, or a
    filter); an empty line goes back (or keeps a field's value); 0 goes back; q quits."""

    def __init__(self, stdin=None, stdout=None):
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.interactive = self.stdin.isatty()
        self.view = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def suspend(self, action):
        return action()

    def say(self, text=""):
        print(text, file=self.stdout)

    def draw(self, view):
        self.view = view
        self.say()
        self.say(f"== {' › '.join(view.crumbs)} ==")
        if view.text is not None:
            self.say(view.text)
        elif view.input is not None:
            if view.message:
                self.say(view.message)
            shown = "" if view.hidden or not view.input else f" [{view.input}]"
            print(f"{view.crumbs[-1]}{' (hidden)' if view.hidden else ''}{shown}: ", end="", file=self.stdout)
        else:
            for number, row in enumerate(view.rows, 1):
                line = f"  {number}) {row.label}"
                if row.detail:
                    line += f"  {row.detail}"
                if row.note:
                    line += f"  ({row.note})"
                self.say(line)
            self.say("  0) Back")
            if view.message:
                self.say(view.message)
        if view.command:
            self.say(f"  {view.command}")
        self.stdout.flush()

    def read_key(self):
        view = self.view
        if view is not None and view.hidden and self.stdin.isatty():
            line = getpass.getpass("")
        else:
            line = self.stdin.readline()
            if line == "":
                return "eof"
            line = line.rstrip("\n")
        if view is not None and view.text is not None:
            return "esc"
        if view is not None and view.input is not None:
            return ("text", line)
        if line == "0":
            return "esc"
        if line.isdigit():
            return ("pick", int(line))
        if len(line) == 1:
            return line
        return ("text", line)


class CursesUI:
    MIN_ROWS, MIN_COLS = 12, 60
    KEYS = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
            curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace", curses.KEY_DC: "backspace",
            curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn", curses.KEY_RESIZE: "resize"}
    CHARS = {"\n": "enter", "\r": "enter", "\x1b": "esc", "\x7f": "backspace", "\b": "backspace",
             "\x03": "ctrl-c", "\x15": "ctrl-u"}
    interactive = True

    def __init__(self, ctx):
        self.ctx, self.window = ctx, None

    def __enter__(self):
        os.environ.setdefault("ESCDELAY", "25")
        locale.setlocale(locale.LC_ALL, "")
        self.window = curses.initscr()
        curses.noecho()
        curses.raw()
        self.window.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_YELLOW, -1)
            except curses.error:
                pass
        return self

    def __exit__(self, *exc):
        curses.noraw()
        curses.echo()
        curses.endwin()
        return False

    def suspend(self, action):
        curses.def_prog_mode()
        curses.endwin()
        try:
            return action()
        finally:
            curses.reset_prog_mode()
            self.window.clear()
            self.window.refresh()

    def put(self, y, x, text, attr=0):
        rows, cols = self.window.getmaxyx()
        if 0 <= y < rows and x < cols - 1:
            try:
                self.window.addnstr(y, x, text, cols - x - 1, attr)
            except curses.error:
                pass

    def warning(self):
        return curses.color_pair(1) if curses.has_colors() else curses.A_BOLD

    def draw(self, view):
        window = self.window
        window.erase()
        rows, cols = window.getmaxyx()
        if rows < self.MIN_ROWS or cols < self.MIN_COLS:
            self.put(0, 0, f"The terminal is too small: the menu needs {self.MIN_COLS}×{self.MIN_ROWS}.", curses.A_BOLD)
            window.refresh()
            return
        right = f"{self.ctx.hostname()} · {self.ctx.version()[0]}"
        self.put(0, 1, " › ".join(view.crumbs)[: max(0, cols - len(right) - 4)], curses.A_BOLD)
        self.put(0, cols - len(right) - 2, right, curses.A_DIM)
        self.put(1, 0, "─" * cols)
        body_top, body_bottom = 2, rows - 4
        if view.text is not None:
            self.draw_text(view, body_top, body_bottom, cols)
        elif view.input is not None:
            self.put(body_top, 1, f"{view.crumbs[-1]}:")
            shown = "•" * len(view.input) if view.hidden else view.input
            self.put(body_top + 2, 3, shown + "▏")
        else:
            self.draw_rows(view, body_top, body_bottom, cols)
        if view.message:
            self.put(rows - 4, 1, view.message, self.warning())
        self.put(rows - 3, 0, "─" * cols)
        self.put(rows - 2, 1, view.command, curses.A_DIM)
        self.put(rows - 1, 1, view.keys, curses.A_DIM)
        window.refresh()

    def draw_text(self, view, top, bottom, cols):
        lines = []
        for line in view.text.splitlines() or [""]:
            lines += textwrap.wrap(line, cols - 3) or [""]
        start = min(view.scroll, max(0, len(lines) - (bottom - top)))
        for y, line in enumerate(lines[start:start + bottom - top], top):
            self.put(y, 1, line)

    def draw_rows(self, view, top, bottom, cols):
        y = top
        if view.filter is not None:
            if view.filter:
                self.put(y, 3, "▏" + view.filter)
            else:
                self.put(y, 3, "▏type to filter", curses.A_DIM)
            y += 1
        if not view.rows:
            self.put(y, 3, "(nothing here)", curses.A_DIM)
            return
        height = bottom - y
        start = max(0, view.cursor - height + 1)
        width = min(36, max(len(r.label) for r in view.rows) + 2)
        for index, row in enumerate(view.rows[start:start + height], start):
            selected = index == view.cursor
            note = f"  {row.note}" if row.note else ""
            text = f"{'▸ ' if selected else '  '}{row.label:<{width}}{row.detail}"
            room = cols - 3 - len(note)
            text = (text[: room - 1] + "…") if len(text) > room else text.ljust(room)
            self.put(y, 1, text, curses.A_REVERSE if selected else 0)
            if note:
                self.put(y, 1 + room, note, curses.A_DIM)
            y += 1

    def read_key(self):
        while True:
            try:
                key = self.window.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return "ctrl-c"
            if isinstance(key, int):
                if key in self.KEYS:
                    return self.KEYS[key]
                continue
            return self.CHARS.get(key, key)


def fits():
    size = shutil.get_terminal_size((0, 0))
    return size.lines >= CursesUI.MIN_ROWS and size.columns >= CursesUI.MIN_COLS


def make_ui(ctx):
    """CursesUI in a terminal big enough for it, else LineUI."""
    if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("YGG_PLAIN") \
            and os.environ.get("TERM", "dumb") != "dumb" and fits():
        return CursesUI(ctx)
    return LineUI()
