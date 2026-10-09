"""The terminal side of the menu: CursesUI draws a screen's View full screen and reads keys raw;
LineUI prints it as numbered lines and reads answers line by line, for no terminal (a pipe, a test,
YGG_PLAIN) or one too small for the full screen, or that curses doesn't know. InlineUI draws one
screen below what the terminal shows, for host.sh's prompts (`ygg.py prompt`)."""

import curses
import getpass
import locale
import os
import select
import shutil
import sys
import termios
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
        try:
            if view is not None and view.hidden and self.stdin.isatty():
                line = getpass.getpass("")
            else:
                line = self.stdin.readline()
                if line == "":
                    return "eof"
                line = line.rstrip("\n")
        except KeyboardInterrupt:  # Ctrl-C at a numbered prompt goes back, as Esc does
            self.say()
            return "esc"
        except EOFError:
            return "eof"
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
        self.too_small = False

    def __enter__(self):
        os.environ.setdefault("ESCDELAY", "25")
        locale.setlocale(locale.LC_ALL, "")
        self.window = curses.initscr()
        try:
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
        except BaseException:
            self.__exit__()  # the terminal goes back to normal before the error is reported
            raise
        return self

    def __exit__(self, *exc):
        try:
            curses.noraw()
            curses.echo()
        finally:
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
        self.too_small = rows < self.MIN_ROWS or cols < self.MIN_COLS
        if self.too_small:
            self.put(0, 0, f"The terminal is too small: the menu needs {self.MIN_COLS}×{self.MIN_ROWS}.", curses.A_BOLD)
            window.refresh()
            return
        right = f"{self.ctx.hostname()} · {self.ctx.version()[0]}"
        self.put(0, 1, " › ".join(view.crumbs)[: max(0, cols - len(right) - 4)], curses.A_BOLD)
        self.put(0, cols - len(right) - 2, right, curses.A_DIM)
        self.put(1, 0, "─" * cols)
        body_top, body_bottom = 2, rows - 4
        if view.question:
            # In the body, wrapped: the header has room for the start of a long question only.
            lines = textwrap.wrap(view.question, cols - 3) or [""]
            for line in lines[: max(1, body_bottom - body_top - 4)]:
                self.put(body_top, 1, line, curses.A_BOLD)
                body_top += 1
            body_top += 1
        if view.text is not None:
            self.draw_text(view, body_top, body_bottom, cols)
        elif view.input is not None:
            if not view.question:
                self.put(body_top, 1, f"{view.crumbs[-1]}:")
                body_top += 2
            shown = "•" * len(view.input) if view.hidden else view.input
            self.put(body_top, 3, shown + "▏")
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
                if key not in self.KEYS:
                    continue
                key = self.KEYS[key]
            else:
                key = self.CHARS.get(key, key)
            # A terminal too small shows no screen, so only what resizes or leaves it acts.
            if self.too_small and key not in ("resize", "esc", "ctrl-c", "q"):
                continue
            return key


class InlineUI:
    """One screen drawn below what the terminal already shows, for host.sh's prompts: the question
    wrapped to the terminal's width, then at most ROWS rows that scroll with the cursor, redrawn in
    place. Nothing above it is touched (no alternate screen, no clearing), so what the operation
    printed before the question stays in view. Keys are read raw from /dev/tty; finish() replaces
    the drawing with one line, the question and its answer."""

    ROWS = 10
    MIN_ROWS, MIN_COLS = 6, 20
    ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
    TILDE = {"5": "pgup", "6": "pgdn", "3": "backspace"}
    CHARS = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\b": "backspace", "\x03": "ctrl-c",
             "\x15": "ctrl-u"}
    BOLD, DIM, REVERSE, YELLOW, RESET = "\x1b[1m", "\x1b[2m", "\x1b[7m", "\x1b[33m", "\x1b[0m"
    interactive = True

    def __init__(self, path="/dev/tty"):
        self.path, self.fd, self.saved, self.height = path, None, None, 0

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
        try:
            self.saved = termios.tcgetattr(self.fd)
            mode = termios.tcgetattr(self.fd)
            # Keys one by one, unechoed, Ctrl-C a key (not a signal), Enter as \r; output as usual.
            mode[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR | termios.IXON)
            mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
            termios.tcsetattr(self.fd, termios.TCSADRAIN, mode)
            self.write("\x1b[?25l")  # the drawn ▏ is the cursor
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.saved is not None:
                self.erase()  # a drawing left by an error goes, so the numbered prompt starts clean
                self.write("\x1b[?25h")
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        finally:
            os.close(self.fd)
        return False

    def suspend(self, action):
        return action()

    def size(self):
        try:
            size = os.get_terminal_size(self.fd)
            return size.lines, size.columns
        except OSError:
            return 24, 80

    def write(self, text):
        data = text.encode()
        while data:
            data = data[os.write(self.fd, data):]

    def erase(self):
        """Back to the first line of the drawing, and clears from there to the end of the screen."""
        if self.height:
            self.write("\r" + (f"\x1b[{self.height - 1}A" if self.height > 1 else "") + "\x1b[J")
            self.height = 0

    def draw(self, view):
        lines = self.render(view)
        self.erase()
        self.write("\r\n".join(lines))
        self.height = len(lines)

    def finish(self, text):
        """Replaces the drawing with `text`, wrapped, and goes to the next line."""
        width = self.size()[1] - 1
        self.erase()
        self.write("\r\n".join(textwrap.wrap(text, width) or [""]) + "\r\n")

    def render(self, view):
        """The lines of a view, each narrower than the terminal, so that none wraps and erase()
        knows how many lines to go back."""
        rows, width = self.size()
        width -= 1

        def clip(text):
            return text if len(text) <= width else text[: width - 1] + "…"

        question = view.question or (view.crumbs[-1] if view.crumbs else "")
        lines = [self.BOLD + line + self.RESET for line in textwrap.wrap(question, width) or [""]]
        room = max(1, min(self.ROWS, rows - len(lines) - 3))
        if view.text is not None:
            text = []
            for line in view.text.splitlines() or [""]:
                text += textwrap.wrap(line, width - 2) or [""]
            start = min(view.scroll, max(0, len(text) - room))
            lines += ["  " + line for line in text[start:start + room]]
        elif view.input is not None:
            shown = "•" * len(view.input) if view.hidden else view.input
            field = "  › " + shown + "▏"
            lines.append(field if len(field) <= width else "  …" + field[len(field) - width + 3:])
        else:
            if view.filter:
                lines.append(clip("  ▏" + view.filter))
            if not view.rows:
                lines.append(self.DIM + "  (nothing matches)" + self.RESET)
            start = max(0, min(view.cursor - room + 1, len(view.rows) - room))
            label_width = min(36, max((len(r.label) for r in view.rows), default=0) + 2)
            for index, row in enumerate(view.rows[start:start + room], start):
                selected = index == view.cursor
                text = f"{'▸ ' if selected else '  '}{row.label:<{label_width}}{row.detail}"
                if row.note:
                    text += f"  {row.note}"
                text = clip(text.rstrip())
                lines.append(self.REVERSE + text + self.RESET if selected else text)
        if view.message:
            lines.append(self.YELLOW + clip(view.message) + self.RESET)
        keys = (f"{view.cursor + 1}/{len(view.rows)} · " if len(view.rows) > room else "") + view.keys
        lines.append(self.DIM + clip(keys) + self.RESET)
        return lines

    def read_byte(self, timeout=None):
        """One byte from the terminal; None at its end, or when `timeout` (seconds) passes first."""
        if timeout is not None and not select.select([self.fd], [], [], timeout)[0]:
            return None
        byte = os.read(self.fd, 1)
        return byte or None

    def read_key(self):
        while True:
            byte = self.read_byte()
            if byte is None:
                return "eof"
            if byte == b"\x1b":
                key = self.escape()
            elif byte[0] >= 0x80:
                more = 1 if byte[0] >> 5 == 0b110 else 2 if byte[0] >> 4 == 0b1110 else 3
                rest = b"".join(self.read_byte(0.05) or b"" for _ in range(more))
                key = (byte + rest).decode("utf-8", "replace")
            else:
                char = byte.decode()
                key = self.CHARS.get(char, char if char.isprintable() else None)
            if key is not None:
                return key

    def escape(self):
        """The key an escape sequence stands for: an arrow, PgUp, PgDn, Delete; a lone ESC is Esc.
        None for one this prompt has no use for."""
        start = self.read_byte(0.05)
        if start is None:
            return "esc"
        if start not in (b"[", b"O"):
            return None  # Alt and a key
        parameters = b""
        while True:
            byte = self.read_byte(0.05)
            if byte is None:
                return None
            if 0x40 <= byte[0] <= 0x7E:
                break
            parameters += byte
        final = byte.decode()
        if final in self.ARROWS:
            return self.ARROWS[final]
        if final == "~":
            return self.TILDE.get(parameters.decode())
        return None


def fits_inline():
    size = shutil.get_terminal_size((0, 0))
    return size.lines >= InlineUI.MIN_ROWS and size.columns >= InlineUI.MIN_COLS


def fits():
    size = shutil.get_terminal_size((0, 0))
    return size.lines >= CursesUI.MIN_ROWS and size.columns >= CursesUI.MIN_COLS


def has_terminfo():
    """Whether curses can drive this TERM: a terminal this machine has no terminfo entry for (a new
    terminal's own TERM over SSH, say) can't have the full screen."""
    try:
        curses.setupterm()
    except curses.error:
        return False
    return True


def make_ui(ctx):
    """CursesUI in a terminal big enough for it that curses knows, else LineUI."""
    if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("YGG_PLAIN") \
            and os.environ.get("TERM", "dumb") != "dumb" and fits() and has_terminfo():
        return CursesUI(ctx)
    return LineUI()
