"""The menu's variables screens: an application's variables in an environment (or any scope's) as a
list, a variable's actions, its history, the Applications screen and the Variables menu. They read
the store in this process, read-only, and change it through `ygg vars ...`: the command line the
footer shows, with a hidden value as KEY=- and the value on stdin, never in the arguments."""

import dataclasses

from . import context, sources, tree
from .screens import (CANCEL, ConfirmScreen, ListScreen, Picker, Pop, Push, Row, Run, TextInput,
                      TextScreen, open_command)

HELP = """An application's variables in an environment: what it gets from each layer (its own
<application>@<environment> values, then <application>, then @<environment>), and where each comes from.

Enter on a variable: show its value (on this screen only; the numbered menu prints it), change it
(hidden for a secret; choose the scope: this application in this environment, every environment, or
the environment's shared scope), mark it secret or not, remove it, its history and rollback, the
commands for it.

r reveals or hides every secret (the numbered menu asks first); e edits the scope in $EDITOR; h
shows the scope's history; d deploys the application, so the changes reach it; / starts a filter."""

PRINTS = "This prints the secrets in the terminal, and its scrollback keeps them. Show them?"


def toggle_reveal(screen):
    """r on a screen that masks secrets: in the numbered menu, which prints them where the
    terminal's scrollback keeps them, only after asking."""
    if screen.reveal or screen.ctx.full_screen:
        screen.reveal = not screen.reveal
        return None

    def answered(yes):
        screen.reveal = yes is True
        return None

    return screen.ask(ConfirmScreen(screen.crumbs, PRINTS), answered)


@dataclasses.dataclass
class Variable:
    key: str
    value: str              # as stored: a reference as written
    secret: bool
    scope: str              # where it comes from, as the CLI spells it
    resolved: str | None = None
    problem: str = ""


def _app_env(scope_text):
    if "@" in scope_text and not scope_text.startswith("@"):
        application, environment = scope_text.split("@", 1)
        return application, environment
    return None


def load(ctx, scope_text):
    """What a scope's screen lists: for <application>@<environment>, the resolved set, each with the
    scope it comes from and a reference followed; for any other scope, its own variables."""
    v = context.vars_module()
    stored = v.parse_scope(scope_text, None)
    app_env = _app_env(scope_text)
    with ctx.store() as store:
        if app_env is None:
            return [Variable(key, value, secret, scope_text) for key, value, secret in store.items(stored)]
        application, environment = app_env
        origins = {"env": f"@{environment}", "app": application, "app@env": scope_text}
        result = []
        for key, (value, origin, secret) in sorted(store.layered(application, environment).items()):
            variable = Variable(key, value, secret, origins[origin])
            match = v.REFERENCE.match(value)
            if match:
                target = store.layered(match[1], environment).get(match[2])
                if target is None:
                    variable.problem = f"{match[1]} has no {match[2]} in {environment}"
                else:
                    variable.resolved, variable.secret = target[0], secret or target[2]
            result.append(variable)
        return result


class VariablesScreen(ListScreen):
    keys_hint = "Enter actions · r reveal/hide · e edit · h history · d deploy · / filter · ? help · Esc back"

    def __init__(self, ctx, crumbs, scope_text):
        super().__init__()
        self.ctx, self.crumbs, self.scope = ctx, tuple(crumbs), scope_text
        self.app_env = _app_env(scope_text)
        self.reveal, self.changed = False, False
        self.variables, self.error = [], ""
        self.help = HELP
        self.refresh()

    def refresh(self):
        v = context.vars_module()
        try:
            self.variables, self.error = load(self.ctx, self.scope), ""
        except v.VarsError as error:
            self.variables, self.error = [], str(error)

    def shown(self, variable):
        v = context.vars_module()
        if variable.problem:
            return f"{variable.value}  ✗ {variable.problem}"
        if variable.resolved is not None:
            value = variable.resolved if self.reveal or not variable.secret else v.mask(variable.resolved)
            return f"{variable.value} → {value}"
        return variable.value if self.reveal or not variable.secret else v.mask(variable.value)

    def rows(self):
        if self.error:
            return [Row("Check the store", "ygg vars check --usable", value="check"),
                    Row("Read about the store", "docs/variables.md", value="docs")]
        rows = [Row(v.key, self.shown(v), v.scope + ("  secret" if v.secret else ""), value=v) for v in self.variables]
        rows.append(Row("+ Add a variable", value="add"))
        if self.app_env and self.changed:
            rows.append(Row(f"Deploy {self.app_env[0]} to {self.app_env[1]}", "so the changes reach it", value="deploy"))
        return rows

    def view(self):
        view = super().view()
        if self.error:
            view.message = f"The store can't be read: {self.error}"
        view.command = tree.command_line(["vars", "list", self.scope] + (["--resolved"] if self.app_env else [])
                                         + (["--reveal"] if self.reveal else []))
        return view

    def shortcuts(self):
        result = dict(super().shortcuts())
        result.update({"r": self.toggle_reveal, "e": self.edit, "h": self.history})
        if self.app_env:
            result["d"] = self.deploy
        return result

    def toggle_reveal(self):
        return toggle_reveal(self)

    def edit(self):
        return Run(["vars", "edit", self.scope], then=self.ran)

    def history(self):
        return Push(HistoryScreen(self.ctx, self.crumbs + ("history",), self.layer_scopes(), None))

    def deploy(self):
        application, environment = self.app_env
        effect = Run(["deploy", environment, application], then=self.deployed)
        if self.ctx.on_demand(environment):
            return effect
        return self.ask(ConfirmScreen(self.crumbs, f"Deploy {application} to {environment}, which stays up?"),
                        lambda yes: effect if yes is True else None)

    def deployed(self, status):
        if status == 0:
            self.changed = False
        return None

    def ran(self, status):
        if status == 0:
            self.changed = True
        return None

    def layer_scopes(self):
        if not self.app_env:
            return [self.scope]
        application, environment = self.app_env
        return [self.scope, application, f"@{environment}"]

    def targets(self, variable=None):
        """Where a value can be written, (scope, what it means), the default first."""
        if not self.app_env:
            return [(self.scope, "")]
        application, environment = self.app_env
        options = [(self.scope, f"only {application} in {environment}"),
                   (application, f"{application} in every environment"),
                   (f"@{environment}", f"every application in {environment}")]
        if variable is not None and variable.scope != self.scope:
            shared = next(o for o in options if o[0] == variable.scope)
            options.remove(shared)
            options = [(s, f"override it for {application} in {environment} only" if s == self.scope else d)
                       for s, d in options]
            options.insert(0, (shared[0], f"change the shared value ({shared[1]})"))
        return options

    def write(self, asker, key, secret, flag=None, variable=None, default=""):
        """Asks the value (hidden for a secret), then the scope when there is a choice, and runs
        `ygg vars set`. `asker` is the screen on top, which gets the answers."""
        crumbs = asker.crumbs

        def with_value(value):
            if value is CANCEL:
                return None
            targets = self.targets(variable)

            def with_scope(scope):
                if scope is CANCEL:
                    return None
                return self.set_value(scope, key, value, secret, flag)

            if len(targets) == 1:
                return with_scope(targets[0][0])
            return asker.ask(Picker(crumbs + ("where",), [sources.Choice(s, d) for s, d in targets]), with_scope)

        return asker.ask(TextInput(crumbs + ("value",), "" if secret else default, hidden=secret,
                                   check=sources.check_value), with_value)

    def set_value(self, scope, key, value, secret, flag=None):
        if secret or value == "-":
            argv, stdin = ["vars", "set", scope, f"{key}=-"], value + "\n"
        else:
            argv, stdin = ["vars", "set", scope, f"{key}={value}"], None
        return Run(argv + ([flag] if flag else []), stdin, then=self.ran)

    def choose(self, row):
        if row.value == "check":
            return Run(["vars", "check", "--usable"])
        if row.value == "docs":
            text = (self.ctx.root / "docs" / "variables.md").read_text(encoding="utf-8")
            return Push(TextScreen(self.crumbs + ("docs/variables.md",), text))
        if row.value == "add":
            return self.add()
        if row.value == "deploy":
            return self.deploy()
        return Push(VariableScreen(self, row.value))

    def add(self):
        v = context.vars_module()
        crumbs = self.crumbs + ("new variable",)

        def with_name(key):
            if key is CANCEL:
                return None
            existing = next((x for x in self.variables if x.key == key), None)
            if existing is not None:
                return Push(VariableScreen(self, existing))
            kinds = [sources.Choice("secret", "hidden when typed, masked when shown"), sources.Choice("not secret")]
            if not v.is_secret_name(key):
                kinds.reverse()

            def with_kind(kind):
                if kind is CANCEL:
                    return None
                secret = kind == "secret"
                return self.write(self, key, secret, flag="--secret" if secret else "--no-secret")

            return self.ask(Picker(crumbs + (key,), kinds), with_kind)

        return self.ask(TextInput(crumbs, check=sources.check_key), with_name)


class VariableScreen(ListScreen):
    """One variable's actions."""

    def __init__(self, owner, variable):
        super().__init__()
        self.owner, self.variable = owner, variable
        self.crumbs = owner.crumbs + (variable.key,)

    def refresh(self):
        self.variable = next((x for x in self.owner.variables if x.key == self.variable.key), self.variable)

    def rows(self):
        v = self.variable
        shown = ("on this screen only, until a key is pressed" if self.owner.ctx.full_screen
                 else "prints it in the terminal, whose scrollback keeps it")
        return [Row("Show value", shown, value=self.show),
                Row("Change value", "typed hidden" if v.secret else v.value, value=self.change),
                Row("Mark as not secret" if v.secret else "Mark as secret", "", value=self.toggle_secret),
                Row("Remove", f"from {v.scope}", value=self.remove),
                Row("History", "its changes; roll one back", value=self.history),
                Row("Copy command", "the ygg vars commands for it", value=self.copy)]

    def view(self):
        view = super().view()
        v = self.variable
        view.message = (f"{v.key} comes from {v.scope}" + (", secret" if v.secret else "")
                        + (f": {v.problem}" if v.problem else ""))
        view.command = tree.command_line(["vars", "get", v.scope, v.key] + (["--reveal"] if v.secret else []))
        return view

    def choose(self, row):
        return row.value()

    def show(self):
        v = self.variable
        text = v.value if v.resolved is None else f"{v.resolved}\n\n(the reference {v.value})"
        return Push(TextScreen(self.crumbs + ("value",), text))

    def change(self):
        v = self.variable
        return self.owner.write(self, v.key, v.secret, variable=v, default=v.value)

    def toggle_secret(self):
        v = self.variable
        return Run(["vars", "set", v.scope, f"{v.key}=-", "--no-secret" if v.secret else "--secret"],
                   v.value + "\n", then=self.owner.ran)

    def remove(self):
        v = self.variable

        def removed(status):
            self.owner.ran(status)
            return Pop(None) if status == 0 else None

        effect = Run(["vars", "unset", v.scope, v.key], then=removed)
        return self.ask(ConfirmScreen(self.crumbs, f"Delete {v.key} from {v.scope}?"),
                        lambda yes: effect if yes is True else None)

    def history(self):
        return Push(HistoryScreen(self.owner.ctx, self.crumbs + ("history",), self.owner.layer_scopes(),
                                  self.variable.key))

    def copy(self):
        v = self.variable
        lines = [tree.command_line(["vars", "get", v.scope, v.key, "--reveal"]) + "    # print the value",
                 tree.command_line(["vars", "set", v.scope, f"{v.key}=-"]) + "    # change it, typed hidden",
                 tree.command_line(["vars", "unset", v.scope, v.key]) + "    # remove it",
                 tree.command_line(["vars", "history", v.scope, v.key]) + "    # its changes"]
        return Push(TextScreen(self.crumbs + ("commands",), "\n".join(lines)))


class HistoryScreen(ListScreen):
    keys_hint = "Enter roll back · r reveal/hide · / filter · Esc back"

    def __init__(self, ctx, crumbs, scopes, key):
        super().__init__()
        self.ctx, self.crumbs, self.scopes, self.key_name = ctx, tuple(crumbs), scopes, key
        self.reveal, self.entries, self.error = False, [], ""
        self.refresh()

    def refresh(self):
        v = context.vars_module()
        try:
            entries = []
            with self.ctx.store() as store:
                for scope in self.scopes:
                    entries += store.history(v.parse_scope(scope, None), self.key_name, 50)
            self.entries, self.error = sorted(entries, key=lambda e: e["id"], reverse=True)[:50], ""
        except v.VarsError as error:
            self.entries, self.error = [], str(error)

    def rows(self):
        v = context.vars_module()

        def shown(value, secret):
            return "-" if value is None else (value if self.reveal or not secret else v.mask(value))

        return [Row(f"#{e['id']}", f"{e['at']}  {e['actor']}  {v.show_scope(e['scope'])} {e['key']}: "
                                   f"{shown(e['old'], e['secret'])} → {shown(e['new'], e['secret'])}",
                    e["command"], value=e) for e in self.entries]

    def view(self):
        view = super().view()
        view.message = self.error or ("" if self.entries else "No changes recorded.")
        view.command = tree.command_line(["vars", "history", self.scopes[0]] + ([self.key_name] if self.key_name else []))
        return view

    def shortcuts(self):
        result = dict(super().shortcuts())
        result["r"] = self.toggle_reveal
        return result

    def toggle_reveal(self):
        return toggle_reveal(self)

    def choose(self, row):
        v = context.vars_module()
        entry = row.value
        actions = [sources.Choice("Roll back", "restore the value from before this change"),
                   sources.Choice("Roll back even if it changed since", "--force")]

        def with_action(action):
            if action is CANCEL:
                return None
            argv = ["vars", "rollback", str(entry["id"])] + (["--force"] if action.endswith("since") else [])
            effect = Run(argv)
            question = f"Roll back change #{entry['id']} ({v.show_scope(entry['scope'])} {entry['key']})?"
            return self.ask(ConfirmScreen(self.crumbs, question), lambda yes: effect if yes is True else None)

        return self.ask(Picker(self.crumbs + (f"#{entry['id']}",), actions), with_action)


class ApplicationsScreen(ListScreen):
    """This host's applications (each opens its variables in one of its environments), then the
    group's commands: deploy, add, config."""

    def __init__(self, ctx, crumbs):
        super().__init__()
        self.ctx, self.crumbs = ctx, tuple(crumbs)
        self.help = HELP

    def rows(self):
        try:
            applications = self.ctx.host_applications()
        except Exception as error:  # an invalid catalog: say so, keep the commands
            applications, self.message = [], f"catalog.yaml can't be read: {error}"
        rows = [Row(a, ", ".join(self.ctx.app_host_environments(a)), "variables and secrets", value=("app", a))
                for a in applications]
        rows += [Row(c.name, c.summary, value=c) for c in tree.commands_list() if c.menu == "Applications"]
        return rows

    def choose(self, row):
        if isinstance(row.value, tree.Command):
            return open_command(self.ctx, self.crumbs, row.value)
        return self.open_app(row.value[1])

    def open_app(self, application, environment=None):
        if environment:
            return self.open_env(application, environment)
        environments = self.ctx.app_host_environments(application)
        if not environments:
            self.message = f"{application} deploys to none of this host's environments"
            return None
        if len(environments) == 1:
            return self.open_env(application, environments[0])
        choices = [sources.Choice(e, "on demand" if self.ctx.on_demand(e) else "") for e in environments]
        return self.ask(Picker(self.crumbs + (application,), choices),
                        lambda e: None if e is CANCEL else self.open_env(application, e))

    def open_env(self, application, environment):
        if not self.ctx.has_store():
            return Run(["config", application, environment], program="host")
        screen = VariablesScreen(self.ctx, self.crumbs + (application, environment), f"{application}@{environment}")
        if screen.error or screen.variables:
            return Push(screen)

        def created(status):
            screen.refresh()
            return Push(screen)

        def answered(yes):
            if yes is True:
                return Run(["create-variables", application, environment], program="host", then=created)
            return Push(screen)

        question = f"{application} has no variables in {environment} yet. Create them from its stack files?"
        return self.ask(ConfirmScreen(self.crumbs + (application, environment), question, default=True), answered)


class VariablesMenu(ListScreen):
    """Variables and secrets: browse any scope, and every `vars` command as a form."""

    def __init__(self, ctx, crumbs):
        super().__init__()
        self.ctx, self.crumbs = ctx, tuple(crumbs)
        self.help = tree.help_text(("vars",))

    def rows(self):
        if self.ctx.has_store():
            rows = [Row("Browse a scope", "any scope's variables: show, change, remove, history", value="browse")]
        else:
            rows = [Row("Create the variables store", "vars init, then vars import --all", value="init")]
        return rows + [Row(c.name, c.summary, value=c) for c in tree.commands_list() if c.menu == "Variables and secrets"]

    def choose(self, row):
        if row.value == "init":
            return Run(["vars", "init"], then=lambda status: Run(["vars", "import", "--all"]) if status == 0 else None)
        if row.value == "browse":
            return self.ask(Picker(self.crumbs + ("scope",), sources.choices("scope", self.ctx, {}), open=True,
                                   check=sources.check_scope),
                            lambda scope: None if scope is CANCEL else
                            Push(VariablesScreen(self.ctx, self.crumbs + (scope,), scope)))
        return open_command(self.ctx, self.crumbs, row.value)
