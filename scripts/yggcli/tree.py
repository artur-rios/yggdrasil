"""The command tree: every command of `ygg` -- its own (commands.py) and the mounted parsers of
vars.py and `env` -- as one list that the menu, the completion and the help read. Each command has
its place in the menu; each argument that takes a value, the pick list it is chosen from (sources.py).
A command added to a parser is in the menu, the completion and the help with no other change; the
parity tests (test_ygg_tree.py) fail until a new argument has a pick list."""

import argparse
import dataclasses
import shlex
import sys

from . import commands, context

# Mounted parsers: these commands' words go to another program, which parses them itself.
MOUNTS = {("vars",): lambda: context.vars_module().parser("ygg vars"), ("env",): commands.env_parser}

TOP = ("Status", "Applications", "Environments", "Variables and secrets", "Platform", "Host", "Catalog",
       "Help and version")
GROUP_SUMMARIES = {
    "Status": "what runs on this host, per environment",
    "Applications": "each application's variables and secrets; deploy; set up a new one",
    "Environments": "start, stop and list this host's environments",
    "Variables and secrets": "any scope of the variables store, and every vars command",
    "Platform": "the platform stack: up, down, ps, logs, config",
    "Host": "check and install the tools, and the ygg command",
    "Catalog": "what catalog.yaml declares",
    "Help and version": "the commands, the version, tab completion",
}
# Where a command sits in the menu: the longest prefix of its path listed here.
MENU = {("status",): "Status", ("config",): "Applications", ("deploy",): "Applications",
        ("add",): "Applications", ("env",): "Environments", ("vars",): "Variables and secrets",
        ("platform",): "Platform", ("check",): "Host", ("install",): "Host", ("self-install",): "Host",
        ("catalog",): "Catalog", ("version",): "Help and version", ("help",): "Help and version",
        ("completion",): "Help and version"}

# The pick list of an argument that takes a value, by its name; SOURCE_OVERRIDES wins for one command.
SOURCE_BY_DEST = {"scope": "scope", "key": "key", "keys": "key", "assignments": "assignment",
                  "application": "application", "environment": "environment", "id": "change",
                  "dir": "directory", "file": "file", "service": "service", "app_dir": "directory",
                  "version": "version", "limit": "number", "topic": "command"}
SOURCE_OVERRIDES = {
    ("config", "application"): "host-application",
    ("config", "environment"): "app-host-environment",
    ("deploy", "environment"): "host-environment",
    ("deploy", "application"): "app-of-environment",
    ("env", "start", "environment"): "host-environment",
    ("env", "stop", "environment"): "host-environment",
    ("vars", "render", "environment"): "app-environment",
    ("vars", "check", "environment"): "app-environment",
    ("catalog", "get", "environment"): "app-environment",
    ("catalog", "get", "option"): "app-option",
    ("catalog", "environment", "option"): "environment-option",
}

FLAG_ACTIONS = (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._StoreConstAction)


@dataclasses.dataclass
class Arg:
    dest: str
    flags: tuple
    help: str
    nargs: object
    choices: tuple | None
    default: object
    const: object
    is_flag: bool
    group: int | None
    metavar: str
    source: str | None = None

    @property
    def positional(self):
        return not self.flags

    @property
    def many(self):
        return self.nargs in ("+", "*")

    @property
    def required(self):
        return self.positional and self.nargs not in ("?", "*")

    @property
    def field(self):
        """The key of its value in a form: a mutually exclusive group's members share their group's."""
        return f"group:{self.group}" if self.group is not None else self.dest

    @property
    def flag(self):
        return self.flags[-1] if self.flags else ""


@dataclasses.dataclass
class Command:
    path: tuple
    summary: str
    parser: argparse.ArgumentParser
    args: list
    menu: str | None

    @property
    def name(self):
        return " ".join(self.path)

    def help_text(self):
        return self.parser.format_help()

    def usage(self):
        words = list(self.path)
        for arg in self.args:
            if arg.positional:
                word = f"<{arg.metavar}>" + ("..." if arg.many else "")
                words.append(word if arg.required else f"[{word}]")
        if any(not arg.positional for arg in self.args):
            words.append("[options]")
        return " ".join(words)


def _subparsers(parser):
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def _args(path, parser):
    groups = {}
    for index, group in enumerate(parser._mutually_exclusive_groups):
        for action in group._group_actions:
            groups[id(action)] = index
    result = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)) or action.nargs == argparse.REMAINDER:
            continue
        is_flag = isinstance(action, FLAG_ACTIONS)
        arg = Arg(dest=action.dest, flags=tuple(action.option_strings), help=action.help or "",
                  nargs=action.nargs, choices=tuple(action.choices) if action.choices else None,
                  default=action.default, const=action.const if is_flag else None, is_flag=is_flag,
                  group=groups.get(id(action)), metavar=action.metavar or action.dest)
        if not is_flag and not arg.choices:
            arg.source = SOURCE_OVERRIDES.get(path + (arg.dest,), SOURCE_BY_DEST.get(arg.dest))
        result.append(arg)
    return result


def _menu_of(path):
    for length in range(len(path), 0, -1):
        if path[:length] in MENU:
            return MENU[path[:length]]
    return None


def _summaries(sub):
    return {action.dest: action.help or "" for action in sub._choices_actions}


_COMMANDS = None


def commands_list():
    """Every command (a leaf of the tree), in the parsers' order."""
    global _COMMANDS
    if _COMMANDS is None:
        result = []

        def walk(parser, path, summary):
            if path in MOUNTS:
                parser = MOUNTS[path]()
            sub = _subparsers(parser)
            if sub is None:
                result.append(Command(path, summary, parser, _args(path, parser), _menu_of(path)))
                return
            summaries = _summaries(sub)
            for name, child in sub.choices.items():
                walk(child, path + (name,), summaries.get(name, ""))

        root = _subparsers(commands.parser())
        summaries = _summaries(root)
        for name, child in root.choices.items():
            walk(child, (name,), summaries.get(name, ""))
        _COMMANDS = result
    return _COMMANDS


def find(path):
    path = tuple(path)
    return next((c for c in commands_list() if c.path == path), None)


def deepest(words):
    """The command its leading words name, and how many words that took; (None, 0) when they name none."""
    best, used = None, 0
    for command in commands_list():
        length = len(command.path)
        if length > used and tuple(words[:length]) == command.path:
            best, used = command, length
    return best, used


def parser_at(path):
    """The parser of a command or of a group of commands; None when there is none."""
    parser = commands.parser()
    for depth, word in enumerate(path):
        sub = _subparsers(parser)
        if sub is None or word not in sub.choices:
            return None
        parser = sub.choices[word]
        if tuple(path[:depth + 1]) in MOUNTS:
            parser = MOUNTS[tuple(path[:depth + 1])]()
    return parser


def help_text(path):
    if not path:
        return overview()
    parser = parser_at(tuple(path))
    if parser is None:
        raise KeyError(" ".join(path))
    return parser.format_help()


def overview():
    lines = [commands.DESCRIPTION, ""]
    width = 34
    for group in TOP:
        lines.append(f"{group}: {GROUP_SUMMARIES[group]}")
        for command in (c for c in commands_list() if c.menu == group):
            usage = command.usage()
            if len(usage) > width:
                lines += [f"  {usage}", f"  {'':{width}}  {command.summary}"]
            else:
                lines.append(f"  {usage:{width}}  {command.summary}")
        lines.append("")
    lines.append(commands.EPILOG)
    return "\n".join(lines) + "\n"


def print_help(topic):
    words = [word for item in topic or [] for word in item.split()]
    try:
        text = help_text(tuple(words))
    except KeyError:
        print(f"ygg: no command '{' '.join(words)}' (ygg help lists them)", file=sys.stderr)
        return 2
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def argv_for(command, values):
    """The command line of a form: positionals in order, then the options that differ from their
    defaults. `values` maps Arg.field to a string, a list (an argument that takes several), a bool (a
    flag) or, for a mutually exclusive group, the chosen flag."""
    argv = list(command.path)
    for arg in command.args:
        if arg.positional:
            value = values.get(arg.dest)
            if isinstance(value, list):
                argv += value
            elif value:
                argv.append(value)
    for arg in command.args:
        if arg.positional:
            continue
        value = values.get(arg.field)
        if arg.group is not None:
            if value == arg.flag:
                argv.append(arg.flag)
        elif arg.is_flag:
            if value:
                argv.append(arg.flag)
        elif value not in (None, "") and str(value) != str(arg.default):
            argv += [arg.flag, str(value)]
    return argv


def missing(command, values):
    """What keeps a form from running, or None."""
    left_out = None
    for arg in command.args:
        if not arg.positional:
            continue
        empty = values.get(arg.dest) in (None, "", [])
        if empty and arg.required:
            return f"{arg.metavar} is needed"
        if empty:
            left_out = left_out or arg.metavar
        elif left_out:
            return f"{arg.metavar} needs {left_out} first"
    return None


def command_line(argv):
    return "ygg " + shlex.join(argv)


def confirmation(command, values, ctx):
    """The question to answer before running what deletes, overwrites or stops something; None otherwise."""
    path, v = command.path, values
    if path == ("vars", "unset"):
        return f"Delete {', '.join(v.get('keys') or [])} from {v.get('scope')}?"
    if path == ("vars", "rollback"):
        return f"Roll back change {v.get('id')}?"
    if path == ("vars", "import") and v.get("replace"):
        return "Overwrite the variables that are already set?"
    if path == ("env", "stop"):
        return f"Stop every application of {v.get('environment')}?"
    if path == ("platform", "down"):
        return "Stop this host's platform (proxy, status, observability, Jenkins)?"
    if path == ("deploy",) and v.get("environment") and not ctx.on_demand(v["environment"]):
        return f"Deploy {v.get('application')} to {v['environment']}, which stays up?"
    return None
