"""ygg's entry point: parses the command line and runs the command, or opens the menu. Commands that
other programs carry out replace this process (exec), so they keep the terminal, the exit status and
the signals as if they had been called directly."""

import os
import sys

from . import commands, context


def interactive():
    """Whether the menu and the prompts can use the terminal: both ends are one, and YGG_PLAIN is unset."""
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("YGG_PLAIN")


def _exec(argv, env=None):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(argv[0], argv, env if env is not None else os.environ)


def host(*arguments):
    _exec(["bash", str(context.SCRIPTS / "host.sh"), *arguments])


def python(script, *arguments, env=None):
    _exec([sys.executable, str(context.SCRIPTS / script), *arguments], env)


def catalog_words(args):
    words = [args.action]
    for name in ("application", "environment", "option"):
        value = getattr(args, name, None)
        if value:
            words.append(value)
    if getattr(args, "deployable", False):
        words.append("--deployable")
    return words


def main(argv):
    if argv[:1] == ["prompt"]:
        from . import prompt
        return prompt.main(argv[1:])
    if argv[:1] in (["-h"], ["--help"]):
        argv = ["help"]
    if argv[:1] == ["vars"]:
        python("vars.py", *(argv[1:] or ["--help"]), env=dict(os.environ, YGG_VARS_PROG="ygg vars"))
    if argv[:1] == ["env"]:
        rest = argv[1:]
        if "-h" in rest or "--help" in rest:
            from . import tree
            return tree.print_help(["env"] + [word for word in rest if not word.startswith("-")])
        host("env", *rest)
    args = commands.parser().parse_args(argv)
    if args.show_version:
        args.command = "version"
    ctx = context.Context()
    command = args.command
    if command is None:
        from . import app
        return app.menu(ctx)
    if command == "version":
        version, commit = ctx.version()
        print(f"yggdrasil {version} ({commit}) at {ctx.root}")
        return 0
    if command == "help":
        from . import tree
        return tree.print_help(args.topic)
    if command in ("status", "check", "install", "add"):
        host(command)
    if command == "config":
        if interactive() and ctx.has_store():
            from . import app
            return app.menu(ctx, start=(args.application, args.environment))
        host("config", *[word for word in (args.application, args.environment) if word])
    if command == "deploy":
        host("deploy-app", args.environment, args.application, args.app_dir or "", args.version or "",
             *(["--leave-running"] if args.leave_running else []))
    if command == "platform":
        words = [args.action] + (["--last-good"] if args.last_good else [])
        if getattr(args, "service", None):
            words.append(args.service)
        _exec(["bash", str(context.SCRIPTS / "platform.sh"), *words])
    if command == "catalog":
        python("catalog.py", *catalog_words(args))
    print(f"ygg: '{command}' is not available yet", file=sys.stderr)
    return 1
