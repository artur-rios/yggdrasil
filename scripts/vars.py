#!/usr/bin/env python3
"""The variables store: every application's and the platform's env variables and secrets, in one
SQLite database per machine, encrypted, layered and recorded.

    python3 scripts/vars.py init
    python3 scripts/vars.py set <scope> KEY=value [KEY=value ...] [--secret | --no-secret]
    python3 scripts/vars.py get <scope> KEY [--reveal]
    python3 scripts/vars.py list <scope> [--resolved] [--reveal] [--keys]
    python3 scripts/vars.py unset <scope> KEY [KEY ...]
    python3 scripts/vars.py history [<scope>] [KEY] [--limit N] [--reveal]
    python3 scripts/vars.py rollback <id> [--force]
    python3 scripts/vars.py import <scope> <file> [--replace]
    python3 scripts/vars.py import --all [--dir <secrets dir>] [--move-up ask|yes|no]
    python3 scripts/vars.py export <scope> [--resolved]
    python3 scripts/vars.py edit <scope> [--reveal]
    python3 scripts/vars.py render <application> <environment>
    python3 scripts/vars.py render-platform [--acme]
    python3 scripts/vars.py backup <dir>
    python3 scripts/vars.py check [<application> <environment>]

Scopes: platform, platform:acme, @<environment>, <application>, <application>@<environment>. An
application's variables in an environment resolve from <application>@<environment>, then
<application>, then @<environment>; a value that is exactly ${ref:<application>:<KEY>} takes that
application's KEY in the same environment. Reference: docs/variables.md.

The database is $YGG_SECRETS_DIR/vars.db (default /etc/yggdrasil) and its key vars.key next to it.
Needs the cryptography package (Ubuntu: apt install python3-cryptography) and PyYAML.
"""

import argparse
import datetime
import getpass
import os
import pathlib
import re
import sqlite3
import sys

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - reported at use
    Fernet = InvalidToken = None

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import catalog as catalog_module  # noqa: E402

SCHEMA_VERSION = 1
KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The same pattern as SECRET_NAME in scripts/ygg.sh: keep the two in step.
SECRET_NAME = re.compile(
    r"(^|_)(PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN|KEY|CREDENTIALS?)(_PREVIOUS)?$|(^|_)CONNECTION_?STRING$",
    re.IGNORECASE)
REFERENCE = re.compile(r"^\$\{ref:([a-z0-9]+(?:-[a-z0-9]+)*):([A-Za-z_][A-Za-z0-9_]*)\}$")
ID = r"[a-z0-9]+(?:-[a-z0-9]+)*"
MASK = "••••••"


class VarsError(Exception):
    """A failure to report to the person: printed as `vars: <message>`, exit 1."""


def secrets_dir():
    return pathlib.Path(os.environ.get("YGG_SECRETS_DIR") or "/etc/yggdrasil")


def actor():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or getpass.getuser()


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_secret_name(key):
    return bool(SECRET_NAME.search(key))


def mask(value):
    return MASK + value[-2:] if len(value) >= 12 else MASK


def validate_key(key):
    if not KEY_NAME.match(key or ""):
        raise VarsError(f"'{key}' is not a variable name: letters, digits and underscores, not starting with a digit")


def validate_value(value):
    if "'" in value or "\n" in value or "\r" in value:
        raise VarsError("values cannot contain a single quote or a line break (they are written single-quoted to the env file)")
    if value != value.strip():
        raise VarsError("values cannot start or end with whitespace")


def validate_entry(scope, key, value):
    validate_key(key)
    validate_value(value)
    if REFERENCE.match(value) and not scope.startswith("app:"):
        raise VarsError("references (${ref:<application>:<KEY>}) only work in application scopes")


def _restrict(path):
    os.chmod(path, 0o640)
    try:
        import grp
        os.chown(path, -1, grp.getgrnam("docker").gr_gid)
    except (ImportError, KeyError, PermissionError):
        pass


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE variables (
    scope TEXT NOT NULL, key TEXT NOT NULL, value BLOB NOT NULL, secret INTEGER NOT NULL,
    updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, PRIMARY KEY (scope, key));
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, command TEXT NOT NULL,
    scope TEXT NOT NULL, key TEXT NOT NULL, old_value BLOB, new_value BLOB, secret INTEGER NOT NULL);
"""


def init(directory, confirm):
    """Creates vars.db and vars.key. `confirm` gets the prompt and returns what the person typed;
    anything but `saved` removes both files again."""
    directory = pathlib.Path(directory)
    database, key_file = directory / "vars.db", directory / "vars.key"
    if database.exists() or key_file.exists():
        raise VarsError(f"{database} or {key_file} already exists")
    _require_crypto()
    key = Fernet.generate_key()
    old_umask = os.umask(0o027)
    try:
        key_file.write_bytes(key + b"\n")
        _restrict(key_file)
        conn = sqlite3.connect(database)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executemany("INSERT INTO meta VALUES (?, ?)",
                         [("schema_version", str(SCHEMA_VERSION)), ("created_at", now()),
                          ("key_check", Fernet(key).encrypt(b"yggdrasil").decode())])
        conn.commit()
        conn.close()
        _restrict(database)
    finally:
        os.umask(old_umask)
    prompt = (f"The key of this store is:\n\n    {key.decode()}\n\n"
              "Without it no value can be read again. Store it somewhere else (a password manager),\n"
              "then type 'saved' to finish: ")
    if confirm(prompt).strip() != "saved":
        database.unlink()
        key_file.unlink()
        raise VarsError("the key was not confirmed as saved; nothing was created")


def _require_crypto():
    if Fernet is None:
        raise VarsError("the cryptography package is missing (Ubuntu: apt install python3-cryptography; "
                        "elsewhere: pip install cryptography)")


class Store:
    def __init__(self, directory, conn, fernet):
        self.directory, self.conn, self.fernet = directory, conn, fernet

    @classmethod
    def open(cls, directory=None, readonly=False):
        directory = pathlib.Path(directory or secrets_dir())
        database, key_file = directory / "vars.db", directory / "vars.key"
        if not database.exists():
            raise VarsError(f"no variables store at {database}: create it with scripts/ygg.sh vars init")
        _require_crypto()
        try:
            fernet = Fernet(key_file.read_bytes().strip())
        except (OSError, ValueError) as error:
            raise VarsError(f"cannot read the key {key_file}: {error}") from None
        if readonly:
            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(database, timeout=10)
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=10000")
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version is None or int(version[0]) > SCHEMA_VERSION:
            raise VarsError(f"{database} has schema version {version and version[0]}, newer than this "
                            f"yggdrasil understands ({SCHEMA_VERSION}): update yggdrasil")
        check = conn.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()
        if check is not None:
            try:
                fernet.decrypt(check[0].encode())
            except InvalidToken:
                conn.close()
                raise VarsError(f"the key does not match this store: wrong or missing key ({key_file})") from None
        return cls(directory, conn, fernet)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.conn.close()

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode())

    def decrypt(self, blob, scope, key):
        try:
            return self.fernet.decrypt(blob).decode()
        except InvalidToken:
            raise VarsError(f"cannot decrypt {show_scope(scope)} {key}: wrong or missing key "
                            f"({self.directory / 'vars.key'})") from None

    def _record(self, command, scope, key, old, new, secret):
        self.conn.execute(
            "INSERT INTO history (at, actor, command, scope, key, old_value, new_value, secret) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now(), actor(), command, scope, key, None if old is None else self.encrypt(old),
             None if new is None else self.encrypt(new), int(secret)))

    def history(self, scope=None, key=None, limit=50):
        query, params = "SELECT * FROM history WHERE 1=1", []
        if scope is not None:
            query, params = query + " AND scope=?", params + [scope]
        if key is not None:
            query, params = query + " AND key=?", params + [key]
        rows = self.conn.execute(query + " ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        result = []
        for id_, at, who, command, scope_, key_, old, new, secret in rows:
            result.append({"id": id_, "at": at, "actor": who, "command": command, "scope": scope_, "key": key_,
                           "old": None if old is None else self.decrypt(old, scope_, key_),
                           "new": None if new is None else self.decrypt(new, scope_, key_), "secret": bool(secret)})
        return result

    def rollback(self, change_id, force):
        row = self.conn.execute("SELECT scope, key FROM history WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise VarsError(f"no change {change_id}")
        scope, key = row
        change = next(r for r in self.history(scope, key, 10**9) if r["id"] == change_id)
        later = self.conn.execute("SELECT COUNT(*) FROM history WHERE scope=? AND key=? AND id>?",
                                  (scope, key, change_id)).fetchone()[0]
        if later and not force:
            raise VarsError(f"{show_scope(scope)} {key} changed again after {change_id}: --force to roll back anyway")
        command = f"rollback {change_id}"
        if change["old"] is None:
            if self.get(scope, key) is not None:
                self.unset(scope, key, command)
        else:
            self.set(scope, key, change["old"], change["secret"], command)

    def apply(self, scope, text, previous, command):
        wanted = parse_env_text(text)
        for key, value in wanted.items():
            try:
                validate_entry(scope, key, value)
            except VarsError as error:
                raise VarsError(f"{key}: {error}") from None
        changed = []
        for key, value in wanted.items():
            old = previous.get(key)
            if old is not None and old[1] and value == mask(old[0]):
                continue
            if old is None or old[0] != value:
                self.set(scope, key, value, None, command)
                changed.append(key)
        for key in previous:
            if key not in wanted:
                self.unset(scope, key, command)
                changed.append(key)
        return changed

    def get(self, scope, key):
        row = self.conn.execute("SELECT value, secret FROM variables WHERE scope=? AND key=?", (scope, key)).fetchone()
        return None if row is None else (self.decrypt(row[0], scope, key), bool(row[1]))

    def items(self, scope):
        rows = self.conn.execute("SELECT key, value, secret FROM variables WHERE scope=? ORDER BY key", (scope,))
        return [(key, self.decrypt(value, scope, key), bool(secret)) for key, value, secret in rows]

    def set(self, scope, key, value, secret, command):
        validate_entry(scope, key, value)
        old = self.get(scope, key)
        if secret is None:
            secret = old[1] if old else is_secret_name(key)
        with self.conn:
            self.conn.execute(
                "INSERT INTO variables VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope, key) DO UPDATE SET "
                "value=excluded.value, secret=excluded.secret, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by",
                (scope, key, self.encrypt(value), int(secret), now(), actor()))
            self._record(command, scope, key, old[0] if old else None, value, secret)

    def unset(self, scope, key, command):
        old = self.get(scope, key)
        if old is None:
            raise VarsError(f"{show_scope(scope)} has no {key}")
        with self.conn:
            self.conn.execute("DELETE FROM variables WHERE scope=? AND key=?", (scope, key))
            self._record(command, scope, key, old[0], None, old[1])

    def layered(self, application, environment):
        """The three layers merged, references not followed: key -> (value, origin, secret)."""
        merged = {}
        for scope, origin in ((f"env:{environment}", "env"), (f"app:{application}", "app"),
                              (f"app:{application}@{environment}", "app@env")):
            for key, value, secret in self.items(scope):
                merged[key] = (value, origin, secret)
        return merged

    def resolve(self, application, environment):
        resolved = {}
        for key, (value, origin, secret) in self.layered(application, environment).items():
            match = REFERENCE.match(value)
            if match:
                target_app, target_key = match[1], match[2]
                target = self.layered(target_app, environment).get(target_key)
                where = f"{application}@{environment} {key} -> {target_app}:{target_key}"
                if target is None:
                    raise VarsError(f"{where}: {target_app} has no {target_key} in {environment}")
                if REFERENCE.match(target[0]):
                    raise VarsError(f"{where}: reference to a reference")
                value, origin, secret = target[0], f"ref → {target_app}:{target_key}", secret or target[2]
            resolved[key] = (value, origin, secret)
        return resolved


def _ids(cat):
    environments = {e["id"] for e in cat.get("environments") or []}
    applications = {a["id"] for s in cat.get("systems") or [] for a in s.get("applications") or []}
    return environments, applications


def parse_scope(text, cat):
    """CLI spelling -> stored form. With a catalog, application and environment ids must exist."""
    environments, applications = _ids(cat) if cat is not None else (None, None)

    def known(kind, value, ids):
        if ids is not None and value not in ids:
            raise VarsError(f"'{value}' is not an {kind} in catalog.yaml")

    if text in ("platform", "platform:acme"):
        return text
    match = re.fullmatch(rf"@({ID})", text or "")
    if match:
        known("environment", match[1], environments)
        return f"env:{match[1]}"
    match = re.fullmatch(rf"({ID})@({ID})", text or "")
    if match:
        known("application", match[1], applications)
        known("environment", match[2], environments)
        return f"app:{match[1]}@{match[2]}"
    match = re.fullmatch(rf"({ID})", text or "")
    if match:
        known("application", match[1], applications)
        return f"app:{match[1]}"
    raise VarsError(f"'{text}' is not a scope: platform, platform:acme, @<environment>, <application> or "
                    "<application>@<environment>")


def show_scope(stored):
    if stored.startswith("env:"):
        return "@" + stored[4:]
    if stored.startswith("app:"):
        return stored[4:]
    return stored


def load_catalog():
    return catalog_module.load()


# ---- Command line -----------------------------------------------------------------------------

def _shown(value, secret, reveal):
    return value if reveal or not secret else mask(value)


def _assignment(text):
    if "=" not in text:
        raise VarsError(f"'{text}' is not KEY=value")
    key, value = text.split("=", 1)
    if value == "-":
        value = getpass.getpass(f"{key}: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    return key, value


def parse_env_text(text):
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise VarsError(f"line {number}: '{raw}' is not KEY=value")
        key, value = line.split("=", 1)
        key = key.strip()
        validate_key(key)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            value = re.sub(r'\\(["\\])', r"\1", value[1:-1])
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def cmd_init(args):
    init(secrets_dir(), confirm=input)
    print(f"Created {secrets_dir() / 'vars.db'}")


def cmd_set(args):
    scope = parse_scope(args.scope, load_catalog())
    pairs = [_assignment(text) for text in args.assignments]
    with Store.open() as store:
        for key, value in pairs:
            store.set(scope, key, value, args.secret, "set")


def cmd_get(args):
    scope = parse_scope(args.scope, None)
    with Store.open(readonly=True) as store:
        found = store.get(scope, args.key)
    if found is None:
        raise VarsError(f"{args.scope} has no {args.key}")
    print(_shown(found[0], found[1], args.reveal))


def cmd_list(args):
    scope = parse_scope(args.scope, None)
    with Store.open(readonly=True) as store:
        if args.resolved:
            for key, (value, origin, secret) in sorted(_resolved_scope(store, scope).items()):
                print(key if args.keys else f"{key}={_shown(value, secret, args.reveal)}  ({origin})")
            return
        for key, value, secret in store.items(scope):
            print(key if args.keys else f"{key}={_shown(value, secret, args.reveal)}")


def cmd_unset(args):
    scope = parse_scope(args.scope, None)
    with Store.open() as store:
        for key in args.keys:
            store.unset(scope, key, "unset")


def cmd_history(args):
    scope = parse_scope(args.scope, None) if args.scope else None
    with Store.open(readonly=True) as store:
        rows = store.history(scope, args.key, args.limit)
    for r in rows:
        old = "-" if r["old"] is None else _shown(r["old"], r["secret"], args.reveal)
        new = "-" if r["new"] is None else _shown(r["new"], r["secret"], args.reveal)
        print(f"{r['id']:>5}  {r['at']}  {r['actor']:<12} {r['command']:<14} {show_scope(r['scope'])} {r['key']}: {old} -> {new}")


def cmd_rollback(args):
    with Store.open() as store:
        store.rollback(args.id, args.force)


def cmd_edit(args):
    import subprocess
    import tempfile
    scope = parse_scope(args.scope, load_catalog())
    with Store.open() as store:
        previous = {key: (value, secret) for key, value, secret in store.items(scope)}
        # Single-quoted: values cannot contain a quote, so this round-trips any stored value exactly.
        text = "".join(f"{key}='{_shown(value, secret, args.reveal)}'\n" for key, (value, secret) in previous.items())
        handle, path = tempfile.mkstemp(prefix="vars-edit-", suffix=".env")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(handle, "w") as file:
                file.write(f"# {args.scope}: one KEY=value per line. A secret left as {MASK}.. keeps its value;\n"
                           "# a deleted line removes the variable.\n" + text)
            editor = os.environ.get("EDITOR") or "nano"
            if subprocess.call([*editor.split(), path]) != 0:
                raise VarsError(f"{editor} failed; nothing changed")
            edited = pathlib.Path(path).read_text()
            try:
                changed = store.apply(scope, edited, previous, "edit")
            except VarsError as error:
                stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", args.scope)
                saved = pathlib.Path(path).with_name(f"vars-edit-{safe}-{stamp}.env")
                fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as file:
                    file.write(edited)
                raise VarsError(f"{error}; nothing changed, your text is saved in {saved}") from None
        finally:
            os.unlink(path)
    print(f"{len(changed)} change(s): {', '.join(changed)}" if changed else "No change.")


def render_lines(variables):
    return "".join(f"{key}='{variables[key]}'\n" for key in sorted(variables))


def _resolved_scope(store, scope):
    match = re.fullmatch(rf"app:({ID})@({ID})", scope)
    if not match:
        raise VarsError("--resolved needs an <application>@<environment> scope")
    return store.resolve(match[1], match[2])


def cmd_render(args):
    parse_scope(f"{args.application}@{args.environment}", load_catalog())
    with Store.open(readonly=True) as store:
        resolved = store.resolve(args.application, args.environment)
    sys.stdout.write(render_lines({key: value for key, (value, _, _) in resolved.items()}))


def cmd_render_platform(args):
    with Store.open(readonly=True) as store:
        items = store.items("platform:acme" if args.acme else "platform")
    sys.stdout.write(render_lines({key: value for key, value, _ in items}))


def cmd_export(args):
    scope = parse_scope(args.scope, None)
    with Store.open(readonly=True) as store:
        if args.resolved:
            values = {key: value for key, (value, _, _) in _resolved_scope(store, scope).items()}
        else:
            values = {key: value for key, value, _ in store.items(scope)}
    sys.stdout.write(render_lines(values))


def parser():
    p = argparse.ArgumentParser(prog="vars.py", description="The yggdrasil variables store (docs/variables.md).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init").set_defaults(run=cmd_init)
    s = sub.add_parser("set")
    s.add_argument("scope")
    s.add_argument("assignments", nargs="+", metavar="KEY=value")
    flag = s.add_mutually_exclusive_group()
    flag.add_argument("--secret", dest="secret", action="store_true", default=None)
    flag.add_argument("--no-secret", dest="secret", action="store_false")
    s.set_defaults(run=cmd_set)
    s = sub.add_parser("get")
    s.add_argument("scope")
    s.add_argument("key")
    s.add_argument("--reveal", action="store_true")
    s.set_defaults(run=cmd_get)
    s = sub.add_parser("list")
    s.add_argument("scope")
    s.add_argument("--reveal", action="store_true")
    s.add_argument("--keys", action="store_true")
    s.add_argument("--resolved", action="store_true")
    s.set_defaults(run=cmd_list)
    s = sub.add_parser("unset")
    s.add_argument("scope")
    s.add_argument("keys", nargs="+", metavar="KEY")
    s.set_defaults(run=cmd_unset)
    s = sub.add_parser("history")
    s.add_argument("scope", nargs="?")
    s.add_argument("key", nargs="?")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--reveal", action="store_true")
    s.set_defaults(run=cmd_history)
    s = sub.add_parser("rollback")
    s.add_argument("id", type=int)
    s.add_argument("--force", action="store_true")
    s.set_defaults(run=cmd_rollback)
    s = sub.add_parser("edit")
    s.add_argument("scope")
    s.add_argument("--reveal", action="store_true")
    s.set_defaults(run=cmd_edit)
    s = sub.add_parser("render")
    s.add_argument("application")
    s.add_argument("environment")
    s.set_defaults(run=cmd_render)
    s = sub.add_parser("render-platform")
    s.add_argument("--acme", action="store_true")
    s.set_defaults(run=cmd_render_platform)
    s = sub.add_parser("export")
    s.add_argument("scope")
    s.add_argument("--resolved", action="store_true")
    s.set_defaults(run=cmd_export)
    return p


def main(argv):
    args = parser().parse_args(argv)
    try:
        args.run(args)
    except VarsError as error:
        print(f"vars: {error}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as error:
        print(f"vars: the database failed: {error} (scripts/ygg.sh vars check)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
