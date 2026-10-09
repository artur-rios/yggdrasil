"""What ygg reads about this machine: the catalog, the host's environments, the variables store, the
version of this checkout. catalog.py and vars.py are imported on first use: `ygg check` and
`ygg install` must run on a host without PyYAML or cryptography, to report and install them."""

import os
import pathlib
import re
import socket
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def catalog_module():
    import catalog
    return catalog


def vars_module():
    import vars
    return vars


class Context:
    def __init__(self, environ=None):
        environ = os.environ if environ is None else environ
        self.root, self.scripts = ROOT, SCRIPTS
        self.secrets = pathlib.Path(environ.get("YGG_SECRETS_DIR") or "/etc/yggdrasil")
        self.apps_dir = pathlib.Path(environ.get("YGG_APPS_DIR") or pathlib.Path.home() / "yggdrasil-apps")
        self.host_env = environ.get("YGG_ENVIRONMENT", "")
        self._catalog = None
        self._version = None
        # Whether the menu draws full screen, where a value shown is gone with the screen; the
        # numbered menu prints it, and the terminal's scrollback keeps it. Set by app.menu.
        self.full_screen = False

    # ---- The catalog

    def catalog(self):
        if self._catalog is None:
            self._catalog = catalog_module().load()
        return self._catalog

    def environments(self):
        return [e["id"] for e in self.catalog()["environments"]]

    def applications(self, deployable=False):
        return [a["id"] for _, a in catalog_module().applications(self.catalog(), deployable=deployable)]

    def app_environments(self, application):
        return [e["id"] for e in catalog_module().resolve(self.catalog(), application)]

    def environment_options(self, environment):
        return catalog_module().environment_options(self.catalog(), environment)

    def app_options(self, application, environment):
        for options in catalog_module().resolve(self.catalog(), application):
            if options["id"] == environment:
                return options
        return {}

    def on_demand(self, environment):
        try:
            return bool(self.environment_options(environment).get("onDemand"))
        except Exception:  # an unknown environment, an unreadable catalog: not on demand
            return False

    # ---- The variables store

    def has_store(self):
        return (self.secrets / "vars.db").exists()

    def store(self):
        """The store, read-only. Raises vars.VarsError when it can't be opened (no key, wrong key)."""
        return vars_module().Store.open(self.secrets, readonly=True)

    def platform_value(self, name):
        """A platform setting, from the store or platform.env; '' when unset."""
        if self.has_store():
            with self.store() as store:
                found = store.get("platform", name)
            return found[0] if found else ""
        try:
            lines = (self.secrets / "platform.env").read_text().splitlines()
        except OSError:
            return ""
        value = ""
        for line in lines:
            if line.startswith(name + "="):
                value = line[len(name) + 1:].strip().strip("'\"")
        return value

    def host_environments(self):
        """The environments this host runs, in catalog order: $YGG_ENVIRONMENT, else ENVIRONMENTS (or
        the ENVIRONMENT of a platform.env from before 0.5); every environment of the catalog when none
        says (scripts/host.sh asks then)."""
        known = self.environments()
        configured = self.host_env
        if not configured:
            try:
                configured = self.platform_value("ENVIRONMENTS") or self.platform_value("ENVIRONMENT")
            except vars_module().VarsError:
                configured = ""
        listed = set(configured.replace(",", " ").split())
        return [e for e in known if e in listed] or known

    def host_applications(self):
        """The deployable applications that deploy to one of this host's environments (host.sh's
        applications_in): the platform's own services are catalog entries too, without a stack."""
        hosts = set(self.host_environments())
        return [a for a in self.applications(deployable=True) if hosts & set(self.app_environments(a))]

    def app_host_environments(self, application):
        hosts = self.host_environments()
        return [e for e in self.app_environments(application) if e in hosts]

    # ---- This machine and this checkout

    def hostname(self):
        return socket.gethostname()

    def version(self):
        if self._version is None:
            self._version = read_version(self.root)
        return self._version


def read_version(root):
    """(version, commit) of a checkout: its tag (0.7.0, or 0.7.0+3 three commits after it), else the
    newest release in CHANGELOG.md; the commit is 'unknown' outside git."""
    root = pathlib.Path(root)

    def git(*arguments):
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                              check=True).stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    if commit != "unknown":
        try:
            described = git("describe", "--tags", "--match", "v[0-9]*")
        except (OSError, subprocess.CalledProcessError):
            described = ""
        if described:
            tag, separator, rest = described.rpartition("-g")
            if separator and "-" in tag:
                tag, _, ahead = tag.rpartition("-")
                return f"{tag.removeprefix('v')}+{ahead}", commit
            return described.removeprefix("v"), commit
    try:
        for line in (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^## \[(\d+\.\d+\.\d+)\]", line)
            if match:
                return match[1], commit
    except OSError:
        pass
    return "unknown", commit
