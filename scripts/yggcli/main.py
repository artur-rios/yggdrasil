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


def on_this_host(ctx, application, environment):
    """Whether `ygg config`'s application and environment, those given, are of this host."""
    if not application:
        return True
    try:
        environments = ctx.app_host_environments(application)
    except Exception:  # not in the catalog, or the catalog can't be read
        return False
    return bool(environments) and (not environment or environment in environments)


def missing_packages():
    """The Python packages the catalog and the variables store need that are not installed."""
    missing = []
    for module, package in (("yaml", "PyYAML"), ("cryptography", "cryptography")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def main(argv):
    try:
        return run(argv)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except ImportError:
        # The menu, the help and the commands that read the catalog import PyYAML; `check` and
        # `install` don't, so they can still report and install it.
        missing = missing_packages()
        if not missing:
            raise
        print(f"ygg: {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing: "
              "run scripts/ygg.sh install", file=sys.stderr)
        return 1


def run(argv):
    if argv[:1] == ["__complete"]:
        from . import complete
        return complete.main(argv[1:])
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
            # The help of the action the words name: what follows it (an environment) is no command.
            command, _ = tree.deepest(["env"] + [word for word in rest if not word.startswith("-")])
            return tree.print_help(list(command.path) if command else ["env"])
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
    if command == "completion":
        from . import complete
        print(complete.script(), end="")
        return 0
    if command == "self-install":
        from . import install
        return install.self_install(ctx)
    if command in ("status", "check", "install", "add"):
        host(command)
    if command == "config":
        # The menu's variables screen, for an application and environment of this host; host.sh's
        # config says why any other is refused.
        if interactive() and ctx.has_store() and on_this_host(ctx, args.application, args.environment):
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
    raise AssertionError(f"no dispatch for {command}")  # every command of commands.parser() is handled above
