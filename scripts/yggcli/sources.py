"""The values of the arguments that take one (tree.Arg.source): the pick lists the menu offers and
the completion completes, the defaults of typed values, and the checks typed text must pass."""

import collections
import pathlib
import re
import subprocess

from . import context

Choice = collections.namedtuple("Choice", "value detail note", defaults=("", ""))

# Typed, not picked: the menu offers a default, the completion leaves them to the shell.
TYPED = {"directory", "file", "version", "number"}
# Picked, but text that matches nothing is taken as typed (a new variable, a scope, a change id).
OPEN = {"scope", "key", "assignment", "change"}
KNOWN = TYPED | OPEN | {"application", "host-application", "app-of-environment", "environment",
                        "host-environment", "app-environment", "app-host-environment", "service",
                        "app-option", "environment-option", "command"}

KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_scope(text):
    vars_module = context.vars_module()
    try:
        vars_module.parse_scope(text, None)
    except vars_module.VarsError as error:
        return str(error)
    return None


def check_key(text):
    return None if KEY_NAME.match(text) else "a variable name: letters, digits and underscores, not starting with a digit"


def check_number(text):
    return None if text == "" or text.isdigit() else "a number"


def check_value(text):
    vars_module = context.vars_module()
    try:
        vars_module.validate_value(text)
    except vars_module.VarsError as error:
        return str(error)
    return None


def checker(source):
    """The check typed text for this source must pass, or None."""
    return {"scope": check_scope, "key": check_key, "assignment": check_key, "change": check_number,
            "number": check_number}.get(source)


def choices(source, ctx, values):
    """The choices of a picked source, given the form's values so far; [] when they can't be read."""
    try:
        return list(_CHOICES[source](ctx, values))
    except Exception:  # a missing catalog, an unreadable store or key, a typed source: nothing to offer
        return []


def _on_demand(ctx, environment):
    return "on demand" if ctx.on_demand(environment) else ""


def _scopes(ctx, values):
    yield Choice("platform", "the platform's settings (platform.env)")
    yield Choice("platform:acme", "the DNS provider's credentials (acme.env)")
    for environment in ctx.environments():
        yield Choice(f"@{environment}", f"every application in {environment}")
    for application in ctx.applications():
        yield Choice(application, f"{application} in every environment")
        for environment in ctx.app_environments(application):
            yield Choice(f"{application}@{environment}", f"{application} in {environment}")


def _keys(ctx, values):
    if not values.get("scope"):
        return
    vars_module = context.vars_module()
    stored = vars_module.parse_scope(values["scope"], None)
    with ctx.store() as store:
        items = store.items(stored)
    for key, _value, secret in items:
        yield Choice(key, "", "secret" if secret else "")


def _changes(ctx, values):
    vars_module = context.vars_module()
    with ctx.store() as store:
        rows = store.history(None, None, 50)
    for row in rows:
        yield Choice(str(row["id"]), f"{row['at']}  {row['actor']}  {row['command']}  "
                                     f"{vars_module.show_scope(row['scope'])} {row['key']}")


def _services(ctx, values):
    import yaml
    data = yaml.safe_load((ctx.root / "platform" / "compose.yml").read_text(encoding="utf-8"))
    for name in data.get("services") or {}:
        yield Choice(name)


def _applications(ctx, values):
    for application in ctx.applications():
        yield Choice(application)


def _host_applications(ctx, values):
    for application in ctx.host_applications():
        yield Choice(application, ", ".join(ctx.app_host_environments(application)))


def _apps_of_environment(ctx, values):
    environment = values.get("environment")
    for application in ctx.applications(deployable=True):
        if not environment or environment in ctx.app_environments(application):
            yield Choice(application)


def _environments(ctx, values):
    for environment in ctx.environments():
        yield Choice(environment, _on_demand(ctx, environment))


def _host_environments(ctx, values):
    for environment in ctx.host_environments():
        yield Choice(environment, _on_demand(ctx, environment))


def _app_environments(ctx, values):
    application = values.get("application")
    for environment in ctx.app_environments(application) if application else ctx.environments():
        yield Choice(environment, _on_demand(ctx, environment))


def _app_host_environments(ctx, values):
    application = values.get("application")
    for environment in ctx.app_host_environments(application) if application else ctx.host_environments():
        yield Choice(environment, _on_demand(ctx, environment))


def _environment_options(ctx, values):
    for option in ctx.environment_options(values["environment"]):
        yield Choice(option)


def _app_options(ctx, values):
    for option in ctx.app_options(values["application"], values["environment"]):
        if option != "id":
            yield Choice(option)


def _commands(ctx, values):
    from . import tree
    for command in tree.commands_list():
        yield Choice(command.name, command.summary)


_CHOICES = {
    "scope": _scopes, "key": _keys, "assignment": _keys, "change": _changes, "service": _services,
    "application": _applications, "host-application": _host_applications,
    "app-of-environment": _apps_of_environment, "environment": _environments,
    "host-environment": _host_environments, "app-environment": _app_environments,
    "app-host-environment": _app_host_environments, "environment-option": _environment_options,
    "app-option": _app_options, "command": _commands,
}


def default(source, ctx, values, arg):
    """The value a typed field starts with, or ''."""
    if source == "directory":
        if arg.dest == "app_dir":
            return str(ctx.apps_dir / values["application"]) if values.get("application") else ""
        if arg.flags:  # vars import --dir: another secrets directory
            return str(ctx.secrets)
        return str(pathlib.Path.home() / "yggdrasil-backups")
    if source == "version":
        directory = values.get("app_dir") or (str(ctx.apps_dir / values["application"]) if values.get("application") else "")
        return checkout_version(directory) if directory else ""
    if source == "number":
        return "" if arg.default is None else str(arg.default)
    if source == "file":
        scope = values.get("scope") or ""
        if "@" in scope and not scope.startswith("@"):
            application, environment = scope.split("@", 1)
            return str(ctx.secrets / environment / f"{application}.env")
    return ""


def checkout_version(directory):
    """<latest tag>-<commit> of a checkout, as Jenkins labels releases (host.sh's checkout_version);
    '' when it is not a git checkout."""
    def git(*arguments):
        return subprocess.run(["git", "-C", directory, *arguments], capture_output=True, text=True).stdout.strip()

    commit = git("rev-parse", "HEAD")[:7]
    if not commit:
        return ""
    tag = git("describe", "--tags", "--abbrev=0") or "dev"
    return f"{tag.removeprefix('v')}-{commit}"
