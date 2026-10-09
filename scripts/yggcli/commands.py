"""ygg's own command line: the parser of every command but `vars` and `env`, whose words go as they
are to vars.py and scripts/host.sh (which parse them; their parsers are mounted in the command tree,
tree.py), and the `env` parser that describes `ygg env` for the help, the menu and the completion."""

import argparse

RAW = argparse.RawDescriptionHelpFormatter

DESCRIPTION = ("yggdrasil's command line app for this host. Without a command, in a terminal, it opens the\n"
               "menu, where every command below is too.")
EPILOG = ("`ygg help` lists the commands by task; `ygg help <command>` (or `ygg <command> --help`) explains\n"
          "one. Guide: docs/cli.md. Exit status: 0 success, 1 a command failed, 2 a usage error.")


def _command(sub, name, summary, description=None, **kwargs):
    return sub.add_parser(name, help=summary, formatter_class=RAW,
                          description=description or summary[:1].upper() + summary[1:] + ".", **kwargs)


def parser():
    p = argparse.ArgumentParser(prog="ygg", formatter_class=RAW, description=DESCRIPTION, epilog=EPILOG)
    # Not dest="version": `deploy`'s positional of that name would overwrite it.
    p.add_argument("--version", action="store_true", dest="show_version", help="print the version and exit")
    sub = p.add_subparsers(dest="command", metavar="<command>", title="commands")

    _command(sub, "status", "what runs on this host, per environment and application",
             "Shows each environment of this host with its applications: state, health, version, commit and\n"
             "deploy time; then offers their logs, restarts and the environments.")
    s = _command(sub, "config", "an application's variables in an environment, and a redeploy",
                 "Shows and changes an application's variables in one of this host's environments (the\n"
                 "variables store, or its env file on a machine without one), and redeploys it. In a terminal\n"
                 "with a variables store it opens the menu's variables screen.")
    s.add_argument("application", nargs="?", help="an application id of catalog.yaml (asked when left out)")
    s.add_argument("environment", nargs="?", help="one of this host's environments (asked when left out)")
    s = _command(sub, "deploy", "build and deploy an application from its checkout",
                 "Builds and deploys an application in one of this host's environments from a checkout of its\n"
                 "repository (scripts/deploy.sh), and rolls back if it does not become healthy.")
    s.add_argument("environment", help="one of this host's environments")
    s.add_argument("application", help="an application id of catalog.yaml that deploys there")
    s.add_argument("app_dir", nargs="?", metavar="app-dir",
                   help="the checkout to build from (default $YGG_APPS_DIR/<application>; asked when missing)")
    s.add_argument("version", nargs="?",
                   help="the version to label the image with (asked; default <latest tag>-<commit> of the checkout)")
    s.add_argument("--leave-running", action="store_true",
                   help="leave a stopped on-demand application running after the deploy (asked in a terminal)")
    _command(sub, "add", "set up a new API, web front end or worker",
             "Sets up a new application: its catalog entry, stack files, variables, checkout and first\n"
             "deploy, asking for each part.")
    s = _command(sub, "env", "start, stop or show this host's environments", add_help=False)
    s.add_argument("arguments", nargs=argparse.REMAINDER)
    s = _command(sub, "vars", "the variables store: variables and secrets", add_help=False)
    s.add_argument("arguments", nargs=argparse.REMAINDER)
    s = _command(sub, "platform", "this host's platform stack (proxy, status, observability, Jenkins)",
                 "Starts, updates or stops this host's platform stack (scripts/platform.sh). It reads the\n"
                 "settings from the variables store, or with --last-good from the copy the last good `up` kept.")
    actions = s.add_subparsers(dest="action", metavar="<action>", required=True, title="actions")
    for name, summary in (("up", "start or update the platform"), ("down", "stop the platform"),
                          ("ps", "list the platform's containers"),
                          ("config", "print the platform's resolved Compose file"),
                          ("logs", "follow the platform's logs")):
        a = _command(actions, name, summary)
        a.add_argument("--last-good", action="store_true",
                       help="read the settings the last successful `up` kept, not the variables store")
        if name == "logs":
            a.add_argument("service", nargs="?", help="only this service of platform/compose.yml")
    _command(sub, "check", "which tools and host pieces are there, which are missing")
    _command(sub, "install", "install what is missing (Ubuntu; asks before each part)")
    _command(sub, "self-install", "install the ygg command and its tab completion",
             "Links /usr/local/bin/ygg to this checkout's scripts/ygg and writes the bash completion to\n"
             "/etc/bash_completion.d/ygg, with sudo when needed. Safe to run again.")
    s = _command(sub, "catalog", "read catalog.yaml: environments, systems, applications",
                 "Reads catalog.yaml (scripts/catalog.py): what it declares, with the defaults applied.")
    actions = s.add_subparsers(dest="action", metavar="<action>", required=True, title="actions")
    _command(actions, "validate", "check catalog.yaml and count what it declares")
    a = _command(actions, "environments", "the environment ids, in promotion order")
    a.add_argument("application", nargs="?", help="only those this application deploys to")
    a = _command(actions, "applications", "the application ids")
    a.add_argument("--deployable", action="store_true", help="only those with a stack to deploy")
    _command(actions, "systems", "the systems: id and name")
    a = _command(actions, "show", "an application's fields and its system (JSON)")
    a.add_argument("application", help="an application id")
    a = _command(actions, "plan", "an application's environments, options resolved (JSON)")
    a.add_argument("application", help="an application id")
    a = _command(actions, "environment", "an environment's options, defaults applied (JSON)")
    a.add_argument("environment", help="an environment id")
    a.add_argument("option", nargs="?", help="only this option's value")
    a = _command(actions, "get", "one option of an application in an environment")
    a.add_argument("application", help="an application id")
    a.add_argument("environment", help="an environment it deploys to")
    a.add_argument("option", help="the option's name")
    _command(actions, "owner", "the GitHub owner of the repositories")
    _command(actions, "repository", "this repository's name")
    _command(sub, "version", "the yggdrasil version, commit and checkout of this ygg")
    s = _command(sub, "help", "the commands, or one command's help")
    s.add_argument("topic", nargs="*", metavar="command", help="a command, e.g. `vars get` (all of them when left out)")
    s = _command(sub, "completion", "print the tab completion script",
                 "Prints the bash completion script (ygg self-install installs it). To try it in this shell:\n"
                 "source <(ygg completion bash)")
    s.add_argument("shell", choices=("bash",), help="the shell")
    return p


def env_parser():
    """The words of `ygg env`, for the help, the menu and the completion: scripts/host.sh reads them."""
    p = argparse.ArgumentParser(prog="ygg env", formatter_class=RAW,
                                description="Starts, stops or shows this host's environments. Without an action, in a\n"
                                            "terminal, it shows them and asks what to do.")
    sub = p.add_subparsers(dest="action", metavar="<action>", title="actions")
    _command(sub, "status", "each environment: on demand or not, running or stopped")
    a = _command(sub, "start", "start every application of an environment")
    a.add_argument("environment", help="one of this host's environments")
    a = _command(sub, "stop", "stop every application of an environment",
                 "Stops every application of an environment. Refuses one that is not onDemand (catalog.yaml)\n"
                 "unless --force.")
    a.add_argument("environment", help="one of this host's environments")
    a.add_argument("--force", action="store_true", help="stop an environment that is not on demand too")
    return p
