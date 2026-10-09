"""The menu's screens: state machines that know nothing of the terminal. A screen turns a key into an
effect (open a screen, go back with a result, run a command, quit) and describes itself as a View,
which ui.py draws full screen or as numbered lines. Keys are names ("up", "enter", "esc",
"backspace", "ctrl-c", ...), a printable character, or, from the numbered fallback, ("pick", n) and
("text", s)."""

import dataclasses

from . import context, sources, tree

CANCEL = object()  # what a screen left with Esc returns


@dataclasses.dataclass
class Row:
    label: str
    detail: str = ""
    note: str = ""
    value: object = None


@dataclasses.dataclass
class View:
    crumbs: list
    rows: list = dataclasses.field(default_factory=list)
    cursor: int = 0
    filter: str | None = None   # None: no filter line
    text: str | None = None     # a block of text instead of rows
    scroll: int = 0
    command: str = ""           # the command line in the footer
    keys: str = ""              # the keys of this screen
    message: str = ""           # an error or a notice
    input: str | None = None    # a text field's content
    hidden: bool = False        # the text field is a secret


@dataclasses.dataclass
class Push:
    screen: object


@dataclasses.dataclass
class Pop:
    result: object = CANCEL


@dataclasses.dataclass
class Run:
    argv: list
    stdin: str | None = None
    program: str = "ygg"        # "ygg" (scripts/ygg.py) or "host" (scripts/host.sh)
    then: object = None         # called with the exit status; may return another effect


@dataclasses.dataclass
class Quit:
    pass


class Screen:
    crumbs = ()
    pending = None

    def view(self):
        raise NotImplementedError

    def key(self, key):
        raise NotImplementedError

    def ask(self, screen, then):
        """Opens `screen`; `then` gets what it returns (CANCEL when it was left with Esc) and may
        return the next effect."""
        self.pending = then
        return Push(screen)

    def on_result(self, result):
        then, self.pending = self.pending, None
        return then(result) if then else None

    def refresh(self):
        """Reloads what it shows: called after a command ran."""


def _printable(key):
    return isinstance(key, str) and len(key) == 1 and key.isprintable()


class ListScreen(Screen):
    """A list with a cursor and a filter. Typing filters (/ starts a filter where letters are
    shortcuts); Enter or → chooses; Esc or ← clears the filter, then goes back. With the filter empty,
    the screen's shortcuts work: q quits and ? shows the help, unless a screen says otherwise."""

    keys_hint = "↑↓ move · Enter choose · type to filter · ? help · Esc back · q quit"
    help = ""

    def __init__(self):
        self.filter = ""
        self.filtering = False
        self.cursor = 0
        self.message = ""

    def rows(self):
        raise NotImplementedError

    def choose(self, row):
        raise NotImplementedError

    def command(self):
        return ""

    def typed(self, text):
        """What typed text that is no row's label does (the numbered fallback); None: it filters."""
        return None

    def shortcuts(self):
        return {"q": Quit, "?": self.show_help}

    def show_help(self):
        return Push(TextScreen(self.crumbs + ("help",), self.help or "No help here."))

    def visible(self):
        needle = self.filter.lower()
        return [r for r in self.rows() if needle in r.label.lower() or needle in r.detail.lower()]

    def view(self):
        rows = self.visible()
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        return View(list(self.crumbs), rows, self.cursor, self.filter, command=self.command(),
                    keys=self.keys_hint, message=self.message)

    def key(self, key):
        rows = self.visible()
        if isinstance(key, tuple):
            return self.line(key, rows)
        self.message = ""
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(len(rows) - 1, self.cursor + 1)
        elif key == "pgup":
            self.cursor = max(0, self.cursor - 10)
        elif key == "pgdn":
            self.cursor = min(len(rows) - 1, self.cursor + 10)
        elif key in ("enter", "right"):
            if rows:
                return self.choose(rows[self.cursor])
        elif key in ("esc", "left", "ctrl-c"):
            if self.filter or self.filtering:
                self.filter, self.filtering, self.cursor = "", False, 0
            else:
                return Pop()
        elif key == "backspace":
            self.filter = self.filter[:-1]
        elif key == "/" and not self.filter:
            self.filtering = True
        elif not self.filter and not self.filtering and key in self.shortcuts():
            return self.shortcuts()[key]()
        elif _printable(key):
            self.filter += key
            self.filtering = True
            self.cursor = 0
        return None

    def line(self, key, rows):
        kind, value = key
        if kind == "pick":
            if 1 <= value <= len(rows):
                self.message = ""
                return self.choose(rows[value - 1])
            self.message = f"Pick a number from 1 to {len(rows)}."
            return None
        if value == "":
            return Pop()
        exact = [r for r in self.rows() if r.label == value]
        if exact:
            return self.choose(exact[0])
        typed = self.typed(value)
        if typed is not None:
            return typed
        self.filter, self.cursor = value, 0
        return None


class Picker(ListScreen):
    """A value from a list, or (open) typed: text that matches no row is offered as itself, after
    its check."""

    keys_hint = "↑↓ move · Enter choose · type to filter or to write a value · ? help · Esc back"

    def __init__(self, crumbs, choices, open=False, check=None, help=""):
        super().__init__()
        self.crumbs, self.choices, self.open, self.check, self.help = tuple(crumbs), choices, open, check, help

    def shortcuts(self):
        return {"?": self.show_help}

    def rows(self):
        rows = [Row(c.value, c.detail, c.note, c.value) for c in self.choices]
        if self.open and self.filter and not any(r.label == self.filter for r in rows):
            rows.append(Row(f'use "{self.filter}"', "as typed", value=("typed", self.filter)))
        return rows

    def choose(self, row):
        value = row.value
        if isinstance(value, tuple) and value[:1] == ("typed",):
            value = value[1]
        problem = self.check(value) if self.check else None
        if problem:
            self.message = problem
            return None
        return Pop(value)

    def typed(self, text):
        return self.choose(Row(text, value=("typed", text))) if self.open else None


class TextInput(Screen):
    """One line of text: a value, a path, a secret (hidden). Enter takes it; Esc cancels."""

    def __init__(self, crumbs, default="", hidden=False, check=None, help=""):
        self.crumbs, self.text, self.hidden, self.check, self.help = tuple(crumbs), default, hidden, check, help
        self.message = ""

    def view(self):
        keys = "type · Enter accept · Ctrl-U clear · Esc cancel"
        return View(list(self.crumbs), input=self.text, hidden=self.hidden, message=self.message,
                    keys=keys + (" · hidden as you type" if self.hidden else ""))

    def key(self, key):
        if isinstance(key, tuple):
            if key[1] or self.hidden:
                self.text = key[1]
            return self.accept()
        if key == "enter":
            return self.accept()
        if key in ("esc", "ctrl-c"):
            return Pop()
        if key == "backspace":
            self.text = self.text[:-1]
        elif key == "ctrl-u":
            self.text = ""
        elif _printable(key):
            self.text += key
        return None

    def accept(self):
        problem = self.check(self.text) if self.check else None
        if problem:
            self.message = problem
            return None
        return Pop(self.text)


class TextScreen(Screen):
    """A block of text (a help, a value, commands): ↑↓ scroll; any other key goes back."""

    def __init__(self, crumbs, text):
        self.crumbs, self.text, self.scroll = tuple(crumbs), text, 0

    def view(self):
        return View(list(self.crumbs), text=self.text, scroll=self.scroll, keys="↑↓ scroll · any other key: back")

    def key(self, key):
        last = self.text.count("\n")
        if key == "up":
            self.scroll = max(0, self.scroll - 1)
        elif key == "down":
            self.scroll = min(last, self.scroll + 1)
        elif key == "pgup":
            self.scroll = max(0, self.scroll - 10)
        elif key == "pgdn":
            self.scroll = min(last, self.scroll + 10)
        elif key != "resize":
            return Pop(None)
        return None


class ConfirmScreen(ListScreen):
    """Yes or no, the cursor on the default (no, unless said); y and n answer; Esc is no."""

    keys_hint = "↑↓ move · Enter choose · y yes · n no · Esc no"

    def __init__(self, crumbs, question, default=False):
        super().__init__()
        self.crumbs = tuple(crumbs) + (question,)
        self.cursor = 0 if default else 1

    def rows(self):
        return [Row("Yes", value=True), Row("No", value=False)]

    def choose(self, row):
        return Pop(row.value)

    def shortcuts(self):
        return {"y": lambda: Pop(True), "n": lambda: Pop(False)}

    def key(self, key):
        if key in ("esc", "left", "ctrl-c"):
            return Pop(False)
        return super().key(key)


class MenuScreen(ListScreen):
    """The top menu (the groups), or one group's commands."""

    def __init__(self, ctx, crumbs=("yggdrasil",), group=None):
        super().__init__()
        self.ctx, self.crumbs, self.group = ctx, tuple(crumbs), group
        self.help = tree.overview()

    def rows(self):
        if self.group is None:
            return [Row(name, tree.GROUP_SUMMARIES[name], value=("group", name)) for name in tree.TOP]
        return [Row(c.name, c.summary, value=c) for c in tree.commands_list() if c.menu == self.group]

    def choose(self, row):
        if isinstance(row.value, tuple):
            return open_group(self.ctx, self.crumbs, row.value[1])
        return open_command(self.ctx, self.crumbs, row.value)


def open_group(ctx, crumbs, group):
    crumbs = tuple(crumbs) + (group,)
    if group in ("Applications", "Variables and secrets"):
        from . import variables  # the variables screens are built on these; imported only when opened
        if group == "Applications":
            return Push(variables.ApplicationsScreen(ctx, crumbs))
        return Push(variables.VariablesMenu(ctx, crumbs))
    members = [c for c in tree.commands_list() if c.menu == group]
    if len(members) == 1:
        return open_command(ctx, crumbs[:-1], members[0])
    return Push(MenuScreen(ctx, crumbs, group))


def open_command(ctx, crumbs, command):
    """A command's form, or, for one without arguments or options, the command itself."""
    if command.args:
        return Push(FormScreen(ctx, crumbs, command))
    return Run(list(command.path))


class FormScreen(ListScreen):
    """A command's form, built from its parser: a field per argument, per option and per mutually
    exclusive group, then Run. The footer shows the command line the values make."""

    keys_hint = "↑↓ move · Enter change · Backspace clear · ? help · Esc back"

    def __init__(self, ctx, crumbs, command, values=None):
        super().__init__()
        self.ctx, self.target = ctx, command
        self.crumbs = tuple(crumbs) + (command.name,)
        self.values = dict(values or {})
        self.stdin = []  # the hidden values of KEY=- assignments, in their order
        self.help = command.help_text()
        for arg in command.args:
            if arg.choices and not arg.positional and arg.field not in self.values:
                self.values[arg.field] = arg.default

    def fields(self):
        result, seen = [], set()
        for arg in self.target.args:
            if arg.group is None:
                result.append(("arg", arg))
            elif arg.group not in seen:
                seen.add(arg.group)
                result.append(("group", [a for a in self.target.args if a.group == arg.group]))
        return result

    def rows(self):
        rows = []
        for kind, item in self.fields():
            if kind == "group":
                label = " | ".join(a.flag for a in item)
                rows.append(Row(label, self.values.get(item[0].field) or "(neither)",
                                "; ".join(a.help for a in item), value=(kind, item)))
            elif item.is_flag:
                mark = "[x]" if self.values.get(item.field) else "[ ]"
                rows.append(Row(f"{mark} {item.flag}", "", item.help, value=(kind, item)))
            else:
                value = self.values.get(item.field)
                shown = " ".join(value) if isinstance(value, list) else (value or "")
                if not shown and item.required:
                    shown = "(needed)"
                rows.append(Row(item.metavar if item.positional else item.flag, shown, item.help, value=(kind, item)))
        rows.append(Row("▶ Run", self.command(), value=("run", None)))
        return rows

    def command(self):
        return tree.command_line(tree.argv_for(self.target, self.values))

    def key(self, key):
        if key == "backspace" and not self.filter:
            rows = self.visible()
            if rows:
                self.clear(rows[self.cursor].value)
            return None
        return super().key(key)

    def clear(self, value):
        kind, item = value
        if kind == "group":
            self.values[item[0].field] = None
        elif kind == "arg":
            if item.is_flag:
                self.values[item.field] = False
            elif item.source == "assignment":
                entries = self.values.get(item.field) or []
                if entries:
                    if entries[-1].endswith("=-") and self.stdin:
                        self.stdin.pop()
                    self.values[item.field] = entries[:-1]
            elif item.many:
                self.values[item.field] = (self.values.get(item.field) or [])[:-1]
            else:
                self.values[item.field] = item.default if item.choices else None

    def choose(self, row):
        kind, item = row.value
        if kind == "run":
            return self.run()
        if kind == "group":
            choices = [sources.Choice("(neither)")] + [sources.Choice(a.flag, a.help) for a in item]
            return self.ask(Picker(self.crumbs + (row.label,), choices), lambda flag: self.set_group(item, flag))
        if item.is_flag:
            self.values[item.field] = not self.values.get(item.field)
            return None
        if item.choices:
            return self.ask(Picker(self.crumbs + (row.label,), [sources.Choice(str(c)) for c in item.choices]),
                            lambda value: self.set(item, value))
        return self.edit(item, row.label)

    def set_group(self, members, flag):
        if flag is not CANCEL:
            self.values[members[0].field] = None if flag == "(neither)" else flag
            if self.secret_chosen():
                self.hide_assignments()
        return None

    def secret_chosen(self):
        """Whether the form says --secret (`vars set`): every value it sets is a secret then."""
        return any(a.flag == "--secret" and self.values.get(a.field) == "--secret" for a in self.target.args)

    def hide_assignments(self):
        """Turns the KEY=value entries into KEY=-, their values on stdin in the order of the
        command line, so a value that became a secret leaves the arguments."""
        hidden, stdin = iter(self.stdin), []
        for kind, item in self.fields():
            if kind != "arg" or item.source != "assignment":
                continue
            entries = []
            for entry in self.values.get(item.field) or []:
                key, _, value = entry.partition("=")
                stdin.append(next(hidden) if value == "-" else value)
                entries.append(f"{key}=-")
            self.values[item.field] = entries
        self.stdin = stdin

    def edit(self, arg, label):
        crumbs = self.crumbs + (label,)
        source = arg.source
        if source == "assignment":
            return self.ask(Picker(crumbs, sources.choices("key", self.ctx, self.values), open=True,
                                   check=sources.check_key, help=arg.help),
                            lambda key: self.assignment_value(arg, key))
        if source is None or source in sources.TYPED:
            current = self.values.get(arg.field)
            default = current if isinstance(current, str) and current else sources.default(source, self.ctx, self.values, arg)
            return self.ask(TextInput(crumbs, default or "", check=sources.checker(source), help=arg.help),
                            lambda text: self.set(arg, text))
        return self.ask(Picker(crumbs, sources.choices(source, self.ctx, self.values), open=source in sources.OPEN,
                               check=sources.checker(source), help=arg.help),
                        lambda value: self.set(arg, value))

    def set(self, arg, value):
        if value is CANCEL:
            return None
        if arg.many:
            self.values[arg.field] = (self.values.get(arg.field) or []) + [value]
        else:
            self.values[arg.field] = value
        return None

    def stored(self, key):
        """(value, secret) of a key in the form's scope, or None."""
        vars_module = context.vars_module()
        try:
            stored = vars_module.parse_scope(self.values.get("scope") or "", None)
            with self.ctx.store() as store:
                return store.get(stored, key)
        except Exception:  # no scope yet, no store, an unreadable key: nothing stored to offer
            return None

    def assignment_value(self, arg, key):
        if key is CANCEL:
            return None
        found = self.stored(key)
        secret = self.secret_chosen() or context.vars_module().is_secret_name(key) or bool(found and found[1])
        default = "" if secret or not found else found[0]
        return self.ask(TextInput(self.crumbs + (key,), default, hidden=secret, check=sources.check_value),
                        lambda value: self.add_assignment(arg, key, value, secret))

    def add_assignment(self, arg, key, value, secret):
        if value is CANCEL:
            return None
        # `-` is how vars.py asks for a value on stdin, so a plain "-" travels that way too.
        if secret or value == "-":
            self.stdin.append(value)
            entry = f"{key}=-"
        else:
            entry = f"{key}={value}"
        self.values[arg.field] = (self.values.get(arg.field) or []) + [entry]
        return None

    def run(self):
        problem = tree.missing(self.target, self.values)
        if problem:
            self.message = problem
            return None
        effect = Run(tree.argv_for(self.target, self.values), "".join(f"{v}\n" for v in self.stdin) or None,
                     then=self.ran)
        question = tree.confirmation(self.target, self.values, self.ctx)
        if question:
            return self.ask(ConfirmScreen(self.crumbs, question), lambda yes: effect if yes is True else None)
        return effect

    def ran(self, status):
        self.message = "Done." if status == 0 else f"It failed (exit {status}): its output says why."
        # Hidden values are not kept once used.
        for kind, item in self.fields():
            if kind == "arg" and item.source == "assignment":
                self.values[item.field] = [e for e in self.values.get(item.field) or [] if not e.endswith("=-")]
        self.stdin = []
        return None
