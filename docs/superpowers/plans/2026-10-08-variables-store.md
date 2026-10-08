# Variables Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move application and platform env vars and secrets from hand-edited env files into a per-machine, encrypted SQLite database managed by `scripts/vars.py` and `scripts/ygg.sh vars`, with inherited layers, references and history.

**Architecture:** `scripts/vars.py` owns the database (stdlib `sqlite3`, `cryptography` Fernet) and renders resolved env files on demand. `deploy.sh` and `platform.sh` render into a private temporary directory when `vars.db` exists and fall back to today's files when it doesn't. `ygg.sh` passes `vars` through and reads platform values and application configuration from the store.

**Tech Stack:** Python 3 (stdlib `sqlite3`, `argparse`, `getpass`; `cryptography` ≥ 3.4 for Fernet; PyYAML through `scripts/catalog.py`), bash, Docker Compose, unittest.

**Spec:** `docs/superpowers/specs/2026-10-08-variables-store-design.md`

## Global Constraints

- Database `$YGG_SECRETS_DIR/vars.db`, key `$YGG_SECRETS_DIR/vars.key`; `YGG_SECRETS_DIR` defaults to `/etc/yggdrasil`. Both files `0640`, group `docker` when that group exists.
- SQLite `journal_mode=DELETE` (never WAL), `busy_timeout` 10000 ms; read-only commands open `file:<path>?mode=ro` (URI).
- Every value encrypted with Fernet under `vars.key`; variable names in clear.
- Scope spellings: `platform`, `platform:acme`, `@<environment>`, `<application>`, `<application>@<environment>`; stored as `platform`, `platform:acme`, `env:<e>`, `app:<a>`, `app:<a>@<e>`.
- Resolution: `app:A@E` > `app:A` > `env:E`; platform scopes never mix into applications.
- References: a value that is exactly `${ref:<application>:<KEY>}`, same environment, one level, missing target or reference-to-reference is an error, application scopes only.
- Keys match `^[A-Za-z_][A-Za-z0-9_]*$`.
- Rendered files: `KEY='value'` lines sorted by key. **Plan-level refinement of the spec:** single-quoting every value makes `$` and `#` literal for Compose, so values may not contain `'`, a newline or a carriage return, and must not start or end with whitespace; the spec's `$$` escape is unnecessary and dropped.
- Secret flag default: the name matches ygg.sh's existing `SECRET_NAME` pattern `(^|_)(PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN|KEY|CREDENTIALS?)(_PREVIOUS)?$|(^|_)CONNECTION_?STRING$` (case-insensitive) — a refinement of the spec's list so the CLI and `config` agree.
- Mask: `••••••` followed by the value's last 2 characters when it has 12 or more characters, else `••••••`.
- History actor: `$SUDO_USER`, else `$USER`, else `getpass.getuser()`.
- Opt-in: without `vars.db`, every script behaves as in 0.5 plus a one-line notice.
- Python scripts must run on Ubuntu (python3 ≥ 3.10) and in Git Bash on Windows.
- No real domain, IP, e-mail or secret in any committed file (`example.com` only).
- Commits: lowercase Conventional Commits, subject ≤ 50 chars, body wrapped at 72, no trailers. Branch `feature/variables-store` (already exists, holds the spec).
- Release target 0.6.0: CHANGELOG `## [Unreleased]` only; no version bump in this plan.

## Review Focus

1. The Jenkins agent reads `vars.db` from a **read-only mount** while root writes on the host — a read must not fail or create journal files; `test_vars.py` gets a read-only-directory reader test (Task 4).
2. A value that renders but changes meaning in Compose (`$`, `#`, spaces, `=`) must reach the container verbatim — Task 2 adds a render round-trip test through a real `docker compose config` when Docker is available, and an always-run parser test.
3. `config`'s editor flow with **masked values left untouched** must keep the stored secrets, and a masked value deliberately deleted must be unset — Task 3 tests both through `vars.py edit` with a fake `$EDITOR`.
4. `import --all` run **twice** (or after a partial failure) must not duplicate, overwrite set values, or rename files it did not import — Task 4 tests a second run.
5. **Lost or wrong key**: every command that needs values must stop with the documented message, and `platform.sh up --last-good` must still work without the key — Tasks 1 and 6.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/vars.py` (create) | The store: schema, encryption, scopes, validation, resolution, rendering, history, import/export, backup, check, edit; the CLI. |
| `scripts/test_vars.py` (create) | Unit tests of vars.py through its functions and CLI. |
| `scripts/deploy.sh` (modify) | Render the app env file from the store (or fall back), new lock path. |
| `scripts/test_deploy.py` (modify) | Store and fallback paths, lock path, cleanup. |
| `scripts/platform.sh` (modify) | Render platform.env/acme.env from the store, last-good copy, `--last-good`. |
| `scripts/test_platform.py` (modify) | Render, last-good, `--last-good`. |
| `platform/compose.yml` (modify) | `YGG_ACME_ENV_FILE`, the agent's read-write `locks` mount. |
| `platform/jenkins/agent/Dockerfile` (modify) | `python3-cryptography`. |
| `scripts/ygg.sh` (modify) | `vars` passthrough, platform values from the store, `config`/`add` on the store, menu entry. |
| `scripts/test_ygg.py` (modify) | `vars` passthrough, ENVIRONMENTS from the store. |
| `.github/workflows/ci.yml` (modify) | Install `cryptography`. |
| `docs/variables.md` (create), `docs/setup.md`, `docs/cli.md`, `docs/catalog.md`, `README.md`, `docs/examples/docker-desktop-and-vps/README.md`, `env/*.example`, `CHANGELOG.md` (modify) | Documentation. |

Run every Python test with `cd /root/repositories/yggdrasil && python3 -m unittest discover -s scripts` (PyYAML and cryptography are installed on this machine). Shell scripts must stay clean under `shellcheck scripts/*.sh` (a shellcheck binary is at `/tmp/claude-0/-root-repositories/c9525b5f-2c8e-4a23-9b69-8c4d43e8f84c/scratchpad/venv/bin/shellcheck`; install `shellcheck-py` in a venv if it is gone).

---

### Task 1: The store — schema, encryption, scopes, set/get/unset/list

**Files:**
- Create: `scripts/vars.py`
- Create: `scripts/test_vars.py`

**Interfaces:**
- Produces (Python, importable as `import vars as v` from `scripts/`):
  - `class VarsError(Exception)` — every user-facing failure; CLI prints `vars: <message>` to stderr, exit 1.
  - `secrets_dir() -> pathlib.Path` — `YGG_SECRETS_DIR` or `/etc/yggdrasil`.
  - `init(directory: Path, confirm: Callable[[str], str]) -> None`
  - `class Store` with `Store.open(directory: Path, readonly: bool) -> Store`, `.close()`, context manager; attributes `conn: sqlite3.Connection`, `fernet`, `directory`.
  - `parse_scope(text: str, catalog: dict | None) -> str` (stored form), `show_scope(stored: str) -> str` (CLI form).
  - `is_secret_name(key: str) -> bool`, `mask(value: str) -> str`.
  - `validate_key(key)`, `validate_value(value)` → raise `VarsError`.
  - `Store.set(scope, key, value, secret: bool | None, command: str) -> None` (history written in Task 3; in Task 1 `set`/`unset` call `self._record(...)`, which is a no-op stub until Task 3 replaces it).
  - `Store.get(scope, key) -> tuple[str, bool] | None` (value, secret).
  - `Store.unset(scope, key, command: str) -> None` (raises `VarsError` when absent).
  - `Store.items(scope) -> list[tuple[str, str, bool]]` (key, value, secret), sorted by key.
  - `main(argv: list[str]) -> int`.
- Consumes: `scripts/catalog.py` — `catalog.load()` (raises `SystemExit`/prints on invalid catalog), `catalog.applications(cat)` (ids), environment ids from `cat["environments"]`.

- [ ] **Step 1: Write the failing tests**

Create `scripts/test_vars.py`:

```python
"""Tests for scripts/vars.py, the variables store.   python3 -m unittest discover -s scripts"""

import io
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import vars as v

SCRIPT = pathlib.Path(__file__).resolve().parent / "vars.py"


class StoreTestCase(unittest.TestCase):
    """A fresh secrets directory with an initialised store; the catalog is the repository's own."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="vars-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.env = dict(os.environ, YGG_SECRETS_DIR=str(self.dir), USER="tester")
        self.env.pop("SUDO_USER", None)
        v.init(self.dir, confirm=lambda prompt: "saved")

    def store(self, readonly=False):
        return v.Store.open(self.dir, readonly=readonly)

    def cli(self, *arguments, input=None):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.env,
                              capture_output=True, text=True, input=input)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="vars-init-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_given_an_empty_directory_when_initialised_then_database_and_key_exist_with_mode_0640(self):
        v.init(self.dir, confirm=lambda prompt: "saved")
        for name in ("vars.db", "vars.key"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o640, name)

    def test_given_the_key_not_confirmed_when_initialised_then_nothing_is_left_behind(self):
        with self.assertRaises(v.VarsError):
            v.init(self.dir, confirm=lambda prompt: "no")
        self.assertFalse((self.dir / "vars.db").exists())
        self.assertFalse((self.dir / "vars.key").exists())

    def test_given_an_existing_store_when_initialised_again_then_it_is_refused(self):
        v.init(self.dir, confirm=lambda prompt: "saved")
        with self.assertRaises(v.VarsError):
            v.init(self.dir, confirm=lambda prompt: "saved")


class ScopeTests(unittest.TestCase):
    def test_given_each_spelling_when_parsed_then_the_stored_form(self):
        cat = {"environments": [{"id": "development"}], "systems": [{"applications": [{"id": "heimdall-api"}]}]}
        self.assertEqual(v.parse_scope("platform", cat), "platform")
        self.assertEqual(v.parse_scope("platform:acme", cat), "platform:acme")
        self.assertEqual(v.parse_scope("@development", cat), "env:development")
        self.assertEqual(v.parse_scope("heimdall-api", cat), "app:heimdall-api")
        self.assertEqual(v.parse_scope("heimdall-api@development", cat), "app:heimdall-api@development")

    def test_given_an_unknown_application_or_environment_when_parsed_then_it_is_refused(self):
        cat = {"environments": [{"id": "development"}], "systems": [{"applications": [{"id": "heimdall-api"}]}]}
        for text in ("nope", "@nope", "heimdall-api@nope", "nope@development", "", "a@b@c"):
            with self.assertRaises(v.VarsError, msg=text):
                v.parse_scope(text, cat)

    def test_given_a_stored_scope_when_shown_then_the_cli_spelling(self):
        for text in ("platform", "platform:acme", "@development", "heimdall-api", "heimdall-api@development"):
            self.assertEqual(v.show_scope(v.parse_scope(text, None)), text)


class ValueRuleTests(unittest.TestCase):
    def test_given_names_when_classified_then_secret_ones_are_detected(self):
        for name in ("DB_PASSWORD", "HEIMDALL_AUTH_TOKEN_SECRET", "HEIMDALL_AUTH_TOKEN_SECRET_PREVIOUS",
                     "MAILGUN_API_KEY", "CF_DNS_API_TOKEN", "FORTUNA_DATA_CONNECTIONSTRING"):
            self.assertTrue(v.is_secret_name(name), name)
        for name in ("DB_HOST", "PUBLIC_HOST", "KEYBOARD_LAYOUT", "ASPNETCORE_ENVIRONMENT"):
            self.assertFalse(v.is_secret_name(name), name)

    def test_given_values_when_masked_then_only_long_ones_show_their_last_two_characters(self):
        self.assertEqual(v.mask("abcdefghijkl"), "••••••kl")
        self.assertEqual(v.mask("short"), "••••••")

    def test_given_unsafe_values_or_keys_when_validated_then_they_are_refused(self):
        for value in ("it's", "a\nb", "a\rb", " lead", "trail "):
            with self.assertRaises(v.VarsError, msg=repr(value)):
                v.validate_value(value)
        for value in ("", "a b", "x#y", "a $b", "p=q", "$2y$05$abc"):
            v.validate_value(value)
        for key in ("1A", "A-B", "", "A B"):
            with self.assertRaises(v.VarsError, msg=key):
                v.validate_key(key)


class SetGetTests(StoreTestCase):
    def test_given_a_value_when_set_then_get_returns_it_and_the_file_does_not_contain_it(self):
        with self.store() as s:
            s.set("app:heimdall-api@development", "DB_PASSWORD", "hunter2-very-secret", None, "set")
            self.assertEqual(s.get("app:heimdall-api@development", "DB_PASSWORD"), ("hunter2-very-secret", True))
        self.assertNotIn(b"hunter2-very-secret", (self.dir / "vars.db").read_bytes())

    def test_given_a_secret_override_when_set_then_the_flag_follows_it(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "example.com", True, "set")
            s.set("platform", "GRAFANA_ADMIN_PASSWORD", "x" * 16, False, "set")
            self.assertEqual(s.get("platform", "DOMAIN"), ("example.com", True))
            self.assertEqual(s.get("platform", "GRAFANA_ADMIN_PASSWORD"), ("x" * 16, False))

    def test_given_a_key_when_unset_then_it_is_gone_and_a_second_unset_fails(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "example.com", None, "set")
            s.unset("platform", "DOMAIN", "unset")
            self.assertIsNone(s.get("platform", "DOMAIN"))
            with self.assertRaises(v.VarsError):
                s.unset("platform", "DOMAIN", "unset")

    def test_given_a_wrong_key_file_when_reading_then_it_fails_naming_the_key_file(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "example.com", None, "set")
        (self.dir / "vars.key").write_text(v.Fernet.generate_key().decode() + "\n")
        with self.store(readonly=True) as s, self.assertRaises(v.VarsError) as caught:
            s.get("platform", "DOMAIN")
        self.assertIn("vars.key", str(caught.exception))

    def test_given_no_key_file_when_opening_then_it_fails_naming_the_key_file(self):
        (self.dir / "vars.key").unlink()
        with self.assertRaises(v.VarsError) as caught:
            self.store()
        self.assertIn("vars.key", str(caught.exception))


class CliBasicsTests(StoreTestCase):
    def test_given_set_then_get_and_list_mask_secrets_unless_revealed(self):
        self.assertEqual(self.cli("set", "platform", "DOMAIN=example.com", "GRAFANA_ADMIN_PASSWORD=abcdefghijkl").returncode, 0)
        self.assertEqual(self.cli("get", "platform", "DOMAIN").stdout, "example.com\n")
        self.assertEqual(self.cli("get", "platform", "GRAFANA_ADMIN_PASSWORD").stdout, "••••••kl\n")
        self.assertEqual(self.cli("get", "platform", "GRAFANA_ADMIN_PASSWORD", "--reveal").stdout, "abcdefghijkl\n")
        listed = self.cli("list", "platform").stdout
        self.assertIn("DOMAIN=example.com", listed)
        self.assertIn("GRAFANA_ADMIN_PASSWORD=••••••kl", listed)

    def test_given_a_dash_value_when_set_then_it_is_read_from_the_prompt(self):
        result = self.cli("set", "platform", "GRAFANA_ADMIN_PASSWORD=-", input="from-stdin-value\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli("get", "platform", "GRAFANA_ADMIN_PASSWORD", "--reveal").stdout, "from-stdin-value\n")

    def test_given_an_absent_key_when_get_then_exit_1(self):
        result = self.cli("get", "platform", "NOPE")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vars:", result.stderr)

    def test_given_list_keys_then_only_names(self):
        self.cli("set", "platform", "A=1", "B=2")
        self.assertEqual(self.cli("list", "platform", "--keys").stdout, "A\nB\n")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/repositories/yggdrasil && python3 -m unittest scripts.test_vars -v 2>&1 | tail -5` (from `scripts/`: `python3 -m unittest test_vars`)
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'vars'`.

- [ ] **Step 3: Implement `scripts/vars.py` (store, scopes, rules, set/get/unset/list, CLI skeleton)**

```python
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
                         [("schema_version", str(SCHEMA_VERSION)), ("created_at", now())])
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
        """History; replaced in Task 3."""

    def get(self, scope, key):
        row = self.conn.execute("SELECT value, secret FROM variables WHERE scope=? AND key=?", (scope, key)).fetchone()
        return None if row is None else (self.decrypt(row[0], scope, key), bool(row[1]))

    def items(self, scope):
        rows = self.conn.execute("SELECT key, value, secret FROM variables WHERE scope=? ORDER BY key", (scope,))
        return [(key, self.decrypt(value, scope, key), bool(secret)) for key, value, secret in rows]

    def set(self, scope, key, value, secret, command):
        validate_key(key)
        validate_value(value)
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
        for key, value, secret in store.items(scope):
            print(key if args.keys else f"{key}={_shown(value, secret, args.reveal)}")


def cmd_unset(args):
    scope = parse_scope(args.scope, None)
    with Store.open() as store:
        for key in args.keys:
            store.unset(scope, key, "unset")


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
    s.set_defaults(run=cmd_list)
    s = sub.add_parser("unset")
    s.add_argument("scope")
    s.add_argument("keys", nargs="+", metavar="KEY")
    s.set_defaults(run=cmd_unset)
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
```

Note: `catalog.load()` must exist in `scripts/catalog.py`; it does (`def load(path=CATALOG)`), and exits the process with the problems printed when the catalog is invalid — acceptable for `set`/`import`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars -v 2>&1 | tail -5`
Expected: all tests of `InitTests`, `ScopeTests`, `ValueRuleTests`, `SetGetTests`, `CliBasicsTests` PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/repositories/yggdrasil
git add scripts/vars.py scripts/test_vars.py
git commit -F - <<'MSG'
feat: add the variables store core

Create scripts/vars.py: an encrypted SQLite store of env variables
per scope, with init, set, get, list and unset.
MSG
```

---

### Task 2: Resolution, references, rendering and export

**Files:**
- Modify: `scripts/vars.py`
- Modify: `scripts/test_vars.py`

**Interfaces:**
- Consumes: `Store.items`, `parse_scope`, `show_scope`, `VarsError`, `load_catalog` (Task 1).
- Produces:
  - `Store.resolve(application: str, environment: str) -> dict[str, tuple[str, str, bool]]` — key → (value, origin, secret); origin is `app@env`, `app`, `env` or `ref → <app>:<KEY>`.
  - `render_lines(variables: dict[str, str]) -> str` — `KEY='value'\n` per key, sorted.
  - CLI: `render <application> <environment>`, `render-platform [--acme]`, `list <scope> --resolved`, `export <scope> [--resolved]`.

- [ ] **Step 1: Write the failing tests** (append to `scripts/test_vars.py`)

```python
class ResolveTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        with self.store() as s:
            s.set("env:development", "DB_HOST", "host.docker.internal", None, "set")
            s.set("env:development", "LOCALE", "pt-BR", None, "set")
            s.set("app:heimdall-api", "LOCALE", "en-US", None, "set")
            s.set("app:heimdall-api@development", "HEIMDALL_AUTH_TOKEN_SECRET", "s" * 40, None, "set")
            s.set("app:heimdall-api@development", "LOCALE", "es-ES", None, "set")
            s.set("app:fortuna-api@development", "FORTUNA_AUTH_TOKEN_SECRET",
                  "${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}", None, "set")

    def test_given_three_layers_when_resolved_then_the_most_specific_wins(self):
        with self.store(readonly=True) as s:
            resolved = s.resolve("heimdall-api", "development")
        self.assertEqual(resolved["LOCALE"][:2], ("es-ES", "app@env"))
        self.assertEqual(resolved["DB_HOST"][:2], ("host.docker.internal", "env"))
        with self.store(readonly=True) as s:
            self.assertEqual(s.resolve("heimdall-api", "homologation"), {})

    def test_given_an_application_layer_only_when_resolved_then_it_beats_the_environment(self):
        with self.store() as s:
            s.unset("app:heimdall-api@development", "LOCALE", "unset")
            self.assertEqual(s.resolve("heimdall-api", "development")["LOCALE"][:2], ("en-US", "app"))

    def test_given_a_reference_when_resolved_then_the_target_value_in_the_same_environment(self):
        with self.store(readonly=True) as s:
            value, origin, secret = s.resolve("fortuna-api", "development")["FORTUNA_AUTH_TOKEN_SECRET"]
        self.assertEqual(value, "s" * 40)
        self.assertEqual(origin, "ref → heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET")
        self.assertTrue(secret)

    def test_given_a_reference_to_a_missing_key_when_resolved_then_it_fails_naming_it(self):
        with self.store() as s:
            s.set("app:fortuna-api@development", "X", "${ref:heimdall-api:NOPE}", None, "set")
            with self.assertRaises(v.VarsError) as caught:
                s.resolve("fortuna-api", "development")
        self.assertIn("heimdall-api:NOPE", str(caught.exception))

    def test_given_a_reference_to_a_reference_when_resolved_then_it_fails(self):
        with self.store() as s:
            s.set("app:heimdall-api@development", "Y", "${ref:fortuna-api:FORTUNA_AUTH_TOKEN_SECRET}", None, "set")
            with self.assertRaises(v.VarsError) as caught:
                s.resolve("heimdall-api", "development")
        self.assertIn("reference to a reference", str(caught.exception))

    def test_given_a_reference_in_a_platform_or_environment_scope_when_set_then_it_is_refused(self):
        with self.store() as s:
            for scope in ("platform", "env:development"):
                with self.assertRaises(v.VarsError, msg=scope):
                    s.set(scope, "X", "${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}", None, "set")


class RenderTests(StoreTestCase):
    def test_given_values_with_shell_and_compose_characters_when_rendered_then_single_quoted_sorted_lines(self):
        self.assertEqual(v.render_lines({"B": "a $b #c", "A": "$2y$05$x=y"}), "A='$2y$05$x=y'\nB='a $b #c'\n")

    def test_given_render_when_run_then_resolved_lines_on_stdout(self):
        self.cli("set", "@development", "DB_HOST=host.docker.internal")
        self.cli("set", "heimdall-api@development", "PUBLIC_HOST=heimdall-api-dev.example.com")
        result = self.cli("render", "heimdall-api", "development")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DB_HOST='host.docker.internal'\nPUBLIC_HOST='heimdall-api-dev.example.com'\n")

    def test_given_render_platform_when_run_then_platform_or_acme_lines(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        self.cli("set", "platform:acme", "CF_DNS_API_TOKEN=abc")
        self.assertEqual(self.cli("render-platform").stdout, "DOMAIN='example.com'\n")
        self.assertEqual(self.cli("render-platform", "--acme").stdout, "CF_DNS_API_TOKEN='abc'\n")

    def test_given_list_resolved_then_each_key_with_its_origin_and_secrets_masked(self):
        self.cli("set", "@development", "DB_HOST=host.docker.internal")
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl")
        listed = self.cli("list", "heimdall-api@development", "--resolved").stdout
        self.assertIn("DB_HOST=host.docker.internal  (env)", listed)
        self.assertIn("DB_PASSWORD=••••••kl  (app@env)", listed)

    def test_given_export_then_revealed_env_file_text(self):
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl")
        self.assertEqual(self.cli("export", "heimdall-api@development").stdout, "DB_PASSWORD='abcdefghijkl'\n")

    @unittest.skipUnless(shutil.which("docker"), "needs the docker CLI")
    def test_given_a_rendered_file_when_compose_reads_it_then_every_value_is_verbatim(self):
        values = {"A": "$2y$05$abc", "B": "x #not a comment", "C": "p=q", "D": "${NOT_INTERPOLATED}"}
        env_file = self.dir / "rendered.env"
        env_file.write_text(v.render_lines(values))
        compose = self.dir / "compose.yml"
        compose.write_text("services:\n  s:\n    image: busybox\n    env_file: [rendered.env]\n")
        out = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--format", "json"],
                             capture_output=True, text=True, cwd=self.dir)
        if out.returncode != 0:
            self.skipTest(out.stderr)
        import json
        self.assertEqual(json.loads(out.stdout)["services"]["s"]["environment"], values)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars.ResolveTests test_vars.RenderTests 2>&1 | tail -3`
Expected: FAIL/ERROR (`AttributeError: 'Store' object has no attribute 'resolve'`, unknown commands).

- [ ] **Step 3: Implement**

In `Store.set`, after `validate_value(value)`, refuse references outside application scopes:

```python
        if REFERENCE.match(value) and not scope.startswith("app:"):
            raise VarsError("references (${ref:<application>:<KEY>}) only work in application scopes")
```

Add to `Store`:

```python
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
```

Add module functions and commands:

```python
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
```

Replace `cmd_list` with:

```python
def cmd_list(args):
    scope = parse_scope(args.scope, None)
    with Store.open(readonly=True) as store:
        if args.resolved:
            for key, (value, origin, secret) in sorted(_resolved_scope(store, scope).items()):
                print(key if args.keys else f"{key}={_shown(value, secret, args.reveal)}  ({origin})")
            return
        for key, value, secret in store.items(scope):
            print(key if args.keys else f"{key}={_shown(value, secret, args.reveal)}")
```

In `parser()`: add `s.add_argument("--resolved", action="store_true")` to `list`, and:

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars -v 2>&1 | tail -3`
Expected: OK (the Compose round-trip test runs here: Docker is installed).

- [ ] **Step 5: Commit**

```bash
git add scripts/vars.py scripts/test_vars.py
git commit -F - <<'MSG'
feat: resolve, render and export stored variables

Merge the environment, application and application-in-environment
layers, follow one level of ${ref:app:KEY} references, and render
single-quoted env files for Compose.
MSG
```

---

### Task 3: History, rollback and the editor flow

**Files:**
- Modify: `scripts/vars.py`
- Modify: `scripts/test_vars.py`

**Interfaces:**
- Consumes: Task 1/2 `Store`.
- Produces:
  - `Store._record(command, scope, key, old, new, secret)` writes a `history` row (replaces the stub).
  - `Store.history(scope: str | None, key: str | None, limit: int) -> list[dict]` with keys `id, at, actor, command, scope, key, old, new, secret`.
  - `Store.rollback(change_id: int, force: bool) -> None`.
  - `Store.apply(scope, text: str, previous: dict[str, tuple[str, bool]], command: str) -> list[str]` — applies env-file text as the new full content of `scope`: a value equal to `mask(previous value)` keeps the previous value; keys missing from the text are unset; returns the changed keys.
  - CLI `history`, `rollback`, `edit <scope> [--reveal]` (`$EDITOR`, default `nano`, on a `0600` temporary file).
  - `parse_env_text(text: str) -> dict[str, str]` — Compose env-file subset: `KEY=value`, optional `export `, `'single'` literal, `"double"` with `\"` and `\\` unescaped, unquoted trimmed with a ` #comment` removed; blank and `#` lines skipped. Raises `VarsError` naming the line for anything else.

- [ ] **Step 1: Write the failing tests**

```python
class HistoryTests(StoreTestCase):
    def test_given_changes_when_listed_then_newest_first_with_actor_and_values(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            s.set("platform", "DOMAIN", "b.example.com", None, "set")
            s.unset("platform", "DOMAIN", "unset")
            rows = s.history("platform", "DOMAIN", 10)
        self.assertEqual([(r["old"], r["new"], r["command"]) for r in rows],
                         [("b.example.com", None, "unset"), ("a.example.com", "b.example.com", "set"),
                          (None, "a.example.com", "set")])
        self.assertEqual(rows[0]["actor"], "tester")

    def test_given_a_change_when_rolled_back_then_the_value_before_it_returns_and_it_is_recorded(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            s.set("platform", "DOMAIN", "b.example.com", None, "set")
            change = s.history("platform", "DOMAIN", 1)[0]["id"]
            s.rollback(change, force=False)
            self.assertEqual(s.get("platform", "DOMAIN")[0], "a.example.com")
            self.assertEqual(s.history("platform", "DOMAIN", 1)[0]["command"], f"rollback {change}")

    def test_given_a_creation_when_rolled_back_then_the_key_is_deleted(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            s.rollback(s.history("platform", "DOMAIN", 1)[0]["id"], force=False)
            self.assertIsNone(s.get("platform", "DOMAIN"))

    def test_given_a_later_change_when_rolling_back_an_earlier_one_then_it_needs_force(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            first = s.history("platform", "DOMAIN", 1)[0]["id"]
            s.set("platform", "DOMAIN", "b.example.com", None, "set")
            with self.assertRaises(v.VarsError):
                s.rollback(first, force=False)
            s.rollback(first, force=True)
            self.assertIsNone(s.get("platform", "DOMAIN"))

    def test_given_history_on_the_cli_then_secret_values_are_masked_unless_revealed(self):
        self.cli("set", "platform", "GRAFANA_ADMIN_PASSWORD=abcdefghijkl")
        self.assertIn("••••••kl", self.cli("history").stdout)
        self.assertNotIn("abcdefghijkl", self.cli("history").stdout)
        self.assertIn("abcdefghijkl", self.cli("history", "--reveal").stdout)

    def test_given_sudo_when_changing_then_the_sudo_user_is_the_actor(self):
        self.env["SUDO_USER"] = "admin-person"
        self.cli("set", "platform", "DOMAIN=example.com")
        self.assertIn("admin-person", self.cli("history").stdout)


class EnvTextTests(unittest.TestCase):
    def test_given_env_file_text_when_parsed_then_compose_semantics(self):
        text = ("# comment\n\nexport A=plain\nB='lit $x #y'\nC=\"q \\\"x\\\" \\\\\"\n"
                "D=value # trailing comment\nE=\n")
        self.assertEqual(v.parse_env_text(text),
                         {"A": "plain", "B": "lit $x #y", "C": 'q "x" \\', "D": "value", "E": ""})

    def test_given_a_line_without_equals_when_parsed_then_it_fails_naming_the_line(self):
        with self.assertRaises(v.VarsError) as caught:
            v.parse_env_text("A=1\nnot a variable\n")
        self.assertIn("line 2", str(caught.exception))


class EditTests(StoreTestCase):
    def editor(self, sed_script):
        script = self.dir / "editor.sh"
        script.write_text(f"#!/bin/sh\nsed -i '{sed_script}' \"$1\"\n")
        script.chmod(0o755)
        self.env["EDITOR"] = str(script)

    def test_given_masked_secrets_left_untouched_when_edited_then_they_keep_their_values(self):
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl", "DB_HOST=old")
        self.editor("s/^DB_HOST=.*/DB_HOST=new/")
        result = self.cli("edit", "heimdall-api@development")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli("get", "heimdall-api@development", "DB_PASSWORD", "--reveal").stdout, "abcdefghijkl\n")
        self.assertEqual(self.cli("get", "heimdall-api@development", "DB_HOST").stdout, "new\n")

    def test_given_a_line_deleted_when_edited_then_the_key_is_unset(self):
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl", "DB_HOST=old")
        self.editor("/^DB_PASSWORD=/d")
        self.cli("edit", "heimdall-api@development")
        self.assertEqual(self.cli("get", "heimdall-api@development", "DB_PASSWORD").returncode, 1)
        self.assertIn("edit", self.cli("history").stdout)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars.HistoryTests test_vars.EnvTextTests test_vars.EditTests 2>&1 | tail -3`
Expected: FAIL/ERROR.

- [ ] **Step 3: Implement**

Replace the `_record` stub and add methods to `Store`:

```python
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
```

Module functions and commands:

```python
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
        text = "".join(f"{key}={_shown(value, secret, args.reveal)}\n" for key, (value, secret) in previous.items())
        handle, path = tempfile.mkstemp(prefix="vars-edit-", suffix=".env")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(handle, "w") as file:
                file.write(f"# {args.scope}: one KEY=value per line. A secret left as {MASK}.. keeps its value;\n"
                           "# a deleted line removes the variable.\n" + text)
            editor = os.environ.get("EDITOR") or "nano"
            if subprocess.call([*editor.split(), path]) != 0:
                raise VarsError(f"{editor} failed; nothing changed")
            changed = store.apply(scope, pathlib.Path(path).read_text(), previous, "edit")
        finally:
            os.unlink(path)
    print(f"{len(changed)} change(s): {', '.join(changed)}" if changed else "No change.")
```

Parser additions:

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars -v 2>&1 | tail -3`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add scripts/vars.py scripts/test_vars.py
git commit -F - <<'MSG'
feat: record, roll back and edit stored variables

Every change goes to an encrypted history that can be rolled back;
vars edit opens a scope in $EDITOR and keeps masked secrets.
MSG
```

---

### Task 4: Import, migration, backup and check

**Files:**
- Modify: `scripts/vars.py`
- Modify: `scripts/test_vars.py`

**Interfaces:**
- Consumes: `parse_env_text`, `Store.set`, `Store.items`, `Store.resolve`, `load_catalog`.
- Produces:
  - `Store.import_text(scope, text, replace: bool, command: str) -> tuple[list[str], list[str]]` — (set keys, kept keys).
  - `import_all(store, directory: Path, cat: dict, move_up: str) -> list[str]` — report lines; `move_up` is `ask`, `yes` or `no`.
  - `backup(directory: Path, target: Path) -> tuple[Path, Path]`.
  - `check(store, cat, application=None, environment=None) -> tuple[list[str], list[str]]` — (errors, warnings).
  - CLI `import`, `import --all`, `backup`, `check`.

- [ ] **Step 1: Write the failing tests**

```python
class ImportTests(StoreTestCase):
    def tree(self):
        (self.dir / "platform.env").write_text("ENVIRONMENTS=development,homologation\nDOMAIN=example.com\n")
        (self.dir / "acme.env").write_text("CF_DNS_API_TOKEN=abc\n")
        for env in ("development", "homologation"):
            (self.dir / env).mkdir()
            (self.dir / env / "heimdall-api.env").write_text(
                f"DB_HOST=host.docker.internal\nLOCALE=pt-BR\nDB_NAME=heimdall_{env}\n")
            (self.dir / env / "fortuna-api.env").write_text(f"DB_HOST=host.docker.internal\nFORTUNA_X={env}\n")

    def test_given_a_file_when_imported_then_existing_keys_are_kept_unless_replace(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "kept.example.com", None, "set")
            set_keys, kept = s.import_text("platform", "DOMAIN=new.example.com\nA=1\n", False, "import")
            self.assertEqual((set_keys, kept), (["A"], ["DOMAIN"]))
            self.assertEqual(s.get("platform", "DOMAIN")[0], "kept.example.com")
            s.import_text("platform", "DOMAIN=new.example.com\n", True, "import")
            self.assertEqual(s.get("platform", "DOMAIN")[0], "new.example.com")

    def test_given_a_secrets_tree_when_imported_all_then_every_file_lands_and_is_renamed(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "no")
            self.assertEqual(s.get("platform", "DOMAIN")[0], "example.com")
            self.assertEqual(s.get("platform:acme", "CF_DNS_API_TOKEN")[0], "abc")
            self.assertEqual(s.get("app:heimdall-api@homologation", "DB_NAME")[0], "heimdall_homologation")
        self.assertTrue((self.dir / "platform.env.imported").exists())
        self.assertTrue((self.dir / "development" / "heimdall-api.env.imported").exists())
        self.assertFalse((self.dir / "development" / "heimdall-api.env").exists())

    def test_given_move_up_yes_when_imported_all_then_shared_values_move_to_the_wider_layer(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "yes")
            # identical in every environment of heimdall-api -> application layer
            self.assertEqual(s.get("app:heimdall-api", "LOCALE")[0], "pt-BR")
            self.assertIsNone(s.get("app:heimdall-api@development", "LOCALE"))
            # identical in every application of an environment -> environment layer
            self.assertEqual(s.get("env:development", "DB_HOST")[0], "host.docker.internal")
            self.assertIsNone(s.get("app:fortuna-api@development", "DB_HOST"))
            # what resolves is unchanged
            self.assertEqual(s.resolve("heimdall-api", "development")["DB_NAME"][0], "heimdall_development")
            self.assertEqual(s.resolve("fortuna-api", "homologation")["DB_HOST"][0], "host.docker.internal")

    def test_given_import_all_run_twice_then_the_second_run_imports_nothing_and_renames_nothing(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "no")
            before = len(s.history(None, None, 10**6))
            report = v.import_all(s, self.dir, v.load_catalog(), "no")
            self.assertEqual(len(s.history(None, None, 10**6)), before)
        self.assertIn("nothing to import", " ".join(report))

    def test_given_an_env_file_of_an_unknown_application_when_imported_all_then_it_is_left_alone(self):
        self.tree()
        (self.dir / "development" / "unknown-app.env").write_text("A=1\n")
        with self.store() as s:
            report = v.import_all(s, self.dir, v.load_catalog(), "no")
        self.assertTrue((self.dir / "development" / "unknown-app.env").exists())
        self.assertIn("unknown-app", " ".join(report))


class BackupCheckTests(StoreTestCase):
    def test_given_a_backup_when_restored_then_values_read_with_its_key(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        target = self.dir / "backups"
        result = self.cli("backup", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        database = next(target.glob("vars-*.db"))
        key = next(target.glob("vars-*.key"))
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        restored = self.dir / "restored"
        restored.mkdir()
        shutil.copy(database, restored / "vars.db")
        shutil.copy(key, restored / "vars.key")
        with v.Store.open(restored, readonly=True) as s:
            self.assertEqual(s.get("platform", "DOMAIN")[0], "example.com")

    def test_given_a_healthy_store_when_checked_then_exit_0(self):
        self.cli("set", "heimdall-api@development", "A=1")
        result = self.cli("check")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_given_a_broken_reference_when_checked_then_exit_1_naming_it(self):
        self.cli("set", "fortuna-api@development", "X=${ref:heimdall-api:NOPE}")
        result = self.cli("check", "fortuna-api", "development")
        self.assertEqual(result.returncode, 1)
        self.assertIn("heimdall-api:NOPE", result.stdout + result.stderr)

    def test_given_an_undecryptable_value_when_checked_then_exit_1(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        conn = sqlite3.connect(self.dir / "vars.db")
        conn.execute("UPDATE variables SET value=? WHERE key='DOMAIN'", (b"garbage",))
        conn.commit()
        conn.close()
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("DOMAIN", result.stdout + result.stderr)

    def test_given_a_corrupted_file_when_checked_then_exit_1(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        data = bytearray((self.dir / "vars.db").read_bytes())
        data[100:4096] = b"\xff" * (4096 - 100)
        (self.dir / "vars.db").write_bytes(bytes(data))
        self.assertEqual(self.cli("check").returncode, 1)

    def test_given_a_value_of_an_unknown_application_when_checked_then_a_warning_not_an_error(self):
        conn = sqlite3.connect(self.dir / "vars.db")
        with self.store() as s:
            s.set("app:gone-app@development", "A", "1", None, "set")
        conn.close()
        result = self.cli("check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("gone-app", result.stdout + result.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_given_a_read_only_directory_when_reading_then_it_works_and_writes_nothing(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        os.chmod(self.dir, 0o555)
        self.addCleanup(os.chmod, self.dir, 0o755)
        self.assertEqual(self.cli("get", "platform", "DOMAIN").stdout, "example.com\n")
        self.assertEqual(self.cli("render-platform").returncode, 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("vars.db")), ["vars.db"])

    def test_given_a_writer_holding_a_transaction_when_reading_read_only_then_the_reader_waits_and_succeeds(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        writer = sqlite3.connect(self.dir / "vars.db")
        writer.execute("BEGIN IMMEDIATE")
        try:
            result = self.cli("get", "platform", "DOMAIN")
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(result.stdout, "example.com\n")
```

Note on the root-only test: this machine runs tests as root, so the read-only-directory test is skipped locally and runs in CI (`ubuntu-latest` runs as `runner`). The second reader test runs everywhere.

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars.ImportTests test_vars.BackupCheckTests 2>&1 | tail -3`
Expected: FAIL/ERROR.

- [ ] **Step 3: Implement**

```python
    # in Store
    def import_text(self, scope, text, replace, command):
        set_keys, kept = [], []
        for key, value in parse_env_text(text).items():
            if self.get(scope, key) is not None and not replace:
                kept.append(key)
                continue
            self.set(scope, key, value, None, command)
            set_keys.append(key)
        return set_keys, kept
```

```python
def _ask(question):
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def import_all(store, directory, cat, move_up):
    environments, applications = _ids(cat)
    report, imported = [], []
    sources = [(directory / "platform.env", "platform"), (directory / "acme.env", "platform:acme")]
    for environment in sorted(environments):
        folder = directory / environment
        for file in sorted(folder.glob("*.env")) if folder.is_dir() else []:
            if file.stem in applications:
                sources.append((file, f"app:{file.stem}@{environment}"))
            else:
                report.append(f"left alone {file}: {file.stem} is not an application in catalog.yaml")
    for file, scope in sources:
        if not file.is_file():
            continue
        set_keys, kept = store.import_text(scope, file.read_text(), False, "import")
        report.append(f"{file} -> {show_scope(scope)}: {len(set_keys)} set"
                      + (f", kept existing {', '.join(kept)}" if kept else ""))
        imported.append(file)
    if not imported:
        return report + ["nothing to import"]
    if move_up != "no":
        report += _move_up(store, cat, move_up)
    for file in imported:
        file.rename(file.with_name(file.name + ".imported"))
    return report


def _move_up(store, cat, mode):
    environments, applications = _ids(cat)
    report = []

    def offer(description):
        return mode == "yes" or _ask(f"Move {description}?")

    # The environment layer first: a value every application of an environment shares (DB_HOST) belongs
    # there, not copied into each application's layer by step 2.
    # 1. Same value in every application of an environment -> env:<environment>.
    for environment in sorted(environments):
        scopes = [f"app:{a}@{environment}" for a in sorted(applications) if store.items(f"app:{a}@{environment}")]
        if len(scopes) < 2:
            continue
        values = [{k: val for k, val, _ in store.items(s)} for s in scopes]
        for key in sorted(set.intersection(*(set(x) for x in values))):
            if len({x[key] for x in values}) == 1 and store.get(f"env:{environment}", key) is None \
                    and offer(f"{key} (same in {len(scopes)} applications of {environment}) to @{environment}"):
                store.set(f"env:{environment}", key, values[0][key], None, "import move-up")
                for scope in scopes:
                    store.unset(scope, key, "import move-up")
                report.append(f"moved {key} to @{environment}")
    # 2. Same value in every environment of an application -> app:<application>.
    for application in sorted(applications):
        scopes = [f"app:{application}@{e}" for e in sorted(environments) if store.items(f"app:{application}@{e}")]
        if len(scopes) < 2:
            continue
        values = [{k: val for k, val, _ in store.items(s)} for s in scopes]
        for key in sorted(set.intersection(*(set(x) for x in values))):
            if len({x[key] for x in values}) == 1 and store.get(f"app:{application}", key) is None \
                    and offer(f"{key} of {application} (same in {len(scopes)} environments) to {application}"):
                store.set(f"app:{application}", key, values[0][key], None, "import move-up")
                for scope in scopes:
                    store.unset(scope, key, "import move-up")
                report.append(f"moved {key} to {application}")
    return report


def backup(directory, target):
    target = pathlib.Path(target)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    database, key = target / f"vars-{stamp}.db", target / f"vars-{stamp}.key"
    source = sqlite3.connect(f"file:{pathlib.Path(directory) / 'vars.db'}?mode=ro", uri=True)
    copy = sqlite3.connect(database)
    with copy:
        source.backup(copy)
    copy.close()
    source.close()
    key.write_bytes((pathlib.Path(directory) / "vars.key").read_bytes())
    for path in (database, key):
        os.chmod(path, 0o600)
    return database, key


def check(store, cat, application=None, environment=None):
    errors, warnings = [], []
    integrity = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        return [f"integrity check: {integrity}"], warnings
    environments, applications = _ids(cat)
    scopes = [row[0] for row in store.conn.execute("SELECT DISTINCT scope FROM variables ORDER BY scope")]
    if application is not None:
        wanted = {f"env:{environment}", f"app:{application}", f"app:{application}@{environment}"}
        scopes = [s for s in scopes if s in wanted]
    for scope in scopes:
        for key, blob in store.conn.execute("SELECT key, value FROM variables WHERE scope=?", (scope,)):
            try:
                store.decrypt(blob, scope, key)
            except VarsError as error:
                errors.append(str(error))
        match = re.fullmatch(rf"(?:app:({ID})(?:@({ID}))?|env:({ID}))", scope)
        if match and ((match[1] and match[1] not in applications) or (match[2] and match[2] not in environments)
                      or (match[3] and match[3] not in environments)):
            warnings.append(f"{show_scope(scope)}: not in catalog.yaml any more")
    pairs = [(application, environment)] if application else [
        (a, e) for a in sorted(applications) for e in sorted(environments)]
    if not errors:
        for app, env in pairs:
            try:
                store.resolve(app, env)
            except VarsError as error:
                errors.append(str(error))
    return errors, warnings


def cmd_import(args):
    cat = load_catalog()
    with Store.open() as store:
        if args.all:
            for line in import_all(store, pathlib.Path(args.dir or secrets_dir()), cat, args.move_up):
                print(line)
            return
        if not args.scope or not args.file:
            raise VarsError("import needs <scope> <file>, or --all")
        scope = parse_scope(args.scope, cat)
        set_keys, kept = store.import_text(scope, pathlib.Path(args.file).read_text(), args.replace, "import")
        print(f"{len(set_keys)} set" + (f"; kept existing (--replace to overwrite): {', '.join(kept)}" if kept else ""))


def cmd_backup(args):
    database, key = backup(secrets_dir(), args.dir)
    print(f"Wrote {database} and {key}")


def cmd_check(args):
    if bool(args.application) != bool(args.environment):
        raise VarsError("check takes no argument, or <application> <environment>")
    with Store.open(readonly=True) as store:
        errors, warnings = check(store, load_catalog(), args.application, args.environment)
    for line in warnings:
        print(f"warning: {line}")
    for line in errors:
        print(f"error: {line}")
    if errors:
        raise VarsError(f"{len(errors)} problem(s) in {secrets_dir() / 'vars.db'}")
    print("vars.db: ok")
```

Parser additions:

```python
    s = sub.add_parser("import")
    s.add_argument("scope", nargs="?")
    s.add_argument("file", nargs="?")
    s.add_argument("--replace", action="store_true")
    s.add_argument("--all", action="store_true")
    s.add_argument("--dir")
    s.add_argument("--move-up", choices=("ask", "yes", "no"), default="ask")
    s.set_defaults(run=cmd_import)
    s = sub.add_parser("backup")
    s.add_argument("dir")
    s.set_defaults(run=cmd_backup)
    s = sub.add_parser("check")
    s.add_argument("application", nargs="?")
    s.add_argument("environment", nargs="?")
    s.set_defaults(run=cmd_check)
```

`check` on a corrupted file may raise `sqlite3.DatabaseError` before `integrity_check` returns; `main` already turns that into exit 1 with a message.

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_vars -v 2>&1 | tail -3`
Expected: OK (one skip as root).

- [ ] **Step 5: Commit**

```bash
git add scripts/vars.py scripts/test_vars.py
git commit -F - <<'MSG'
feat: import, back up and check the variables store

import --all migrates the secrets directory's env files and offers to
move shared values up a layer; check runs before every use.
MSG
```

---

### Task 5: deploy.sh reads the store

**Files:**
- Modify: `scripts/deploy.sh` (env file resolution at the top, lock block lines ~85-100, exit trap)
- Modify: `scripts/test_deploy.py`

**Interfaces:**
- Consumes: `vars.py check <app> <env>`, `vars.py render <app> <env>` (exit 0, env-file text on stdout).
- Produces: `APP_ENV_FILE` = the rendered file (store) or `<secrets>/<env>/<app>.env` (fallback); lock file `<secrets>/locks/<app>-<env>.lock`.

- [ ] **Step 1: Write the failing tests** (in `scripts/test_deploy.py`)

Change the fake docker's lock probe from `$APP_ENV_FILE` to the lock file, so it checks the new location:

```python
            flock --nonblock --conflict-exit-code 75 "$YGG_SECRETS_DIR/locks/$PROJECT_UNDER_TEST.lock" true || status=$?
```

and set `PROJECT_UNDER_TEST` in `DeployTests.deploy()`'s environment to `f"{STACK}-{environment}"`. Then add:

```python
    def use_store(self, values):
        """Moves this test's env files into a variables store holding `values` for every environment."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import vars as v
        secrets = pathlib.Path(self.env["YGG_SECRETS_DIR"])
        v.init(secrets, confirm=lambda prompt: "saved")
        with v.Store.open(secrets) as store:
            for environment in (ENVIRONMENT, ON_DEMAND):
                for key, value in values.items():
                    store.set(f"app:{STACK}@{environment}", key, value, None, "set")
                (secrets / environment / f"{STACK}.env").unlink()

    def test_given_a_store_when_deployed_then_compose_reads_the_rendered_file_and_it_is_removed(self):
        self.use_store({"HEIMDALL_API_BASE_URL": "https://heimdall.example.com"})
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        env_files = [line.split("--env-file ")[1].split(" ")[0] for line in self.read("calls").splitlines()
                     if "--env-file" in line]
        self.assertTrue(env_files)
        self.assertTrue(all(not pathlib.Path(p).exists() for p in env_files), env_files)
        self.assertNotIn("move to the variables store", result.stderr)

    def test_given_a_store_with_a_broken_reference_when_deployed_then_it_stops_before_docker(self):
        self.use_store({"X": "${ref:heimdall-api:NOPE}"})
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("heimdall-api:NOPE", result.stderr + result.stdout)
        self.assertNotIn("compose", self.read("calls"))

    def test_given_no_store_when_deployed_then_the_env_file_is_used_with_a_notice(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("move to the variables store", result.stderr)
        self.assertIn(f"--env-file {self.env_file}", self.read("calls"))

    def test_given_a_deploy_when_it_runs_then_it_holds_the_project_lock(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("up1.lock").strip(), "75")
```

(Delete the old `test_given_a_deploy_when_it_runs_then_it_holds_the_lock_on_the_env_file`, replaced by the last test. Add `import sys` at the top of the file.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_deploy 2>&1 | tail -3`
Expected: FAIL on the four new tests.

- [ ] **Step 3: Implement in `scripts/deploy.sh`**

Replace the env file block (from `env_file="$secrets/$environment/$stack.env"` and its `[[ -f "$env_file" ...]] || die` check) with:

```bash
# The application's variables: rendered from the variables store when this machine has one
# (docs/variables.md), else today's env file. The rendered file lives in a private directory and is
# removed when this script exits.
cleanup_paths=()
trap 'rm -rf "${cleanup_paths[@]}"' EXIT
if [[ -f "$secrets/vars.db" ]]; then
  python3 "$root/scripts/vars.py" check "$stack" "$environment" >&2 \
    || die "the variables store failed its check (scripts/ygg.sh vars check)"
  run_base=/run/yggdrasil
  mkdir -p "$run_base" 2>/dev/null && [[ -w "$run_base" ]] || run_base="${TMPDIR:-/tmp}/yggdrasil-$(id -u)"
  mkdir -p -m 700 "$run_base"
  render_dir=$(mktemp -d "$run_base/deploy.XXXXXX")
  cleanup_paths+=("$render_dir")
  env_file="$render_dir/$stack-$environment.env"
  (umask 077 && python3 "$root/scripts/vars.py" render "$stack" "$environment" >"$env_file") \
    || die "could not render the variables of $stack in $environment"
else
  env_file="$secrets/$environment/$stack.env"
  [[ -f "$env_file" && -r "$env_file" ]] || die "missing or unreadable env file $env_file: create it (docs/setup.md step 11); under Jenkins it must be readable by the agent (uid 1000, or the docker group)"
  echo "deploy: reading $env_file; move to the variables store with scripts/ygg.sh vars init && scripts/ygg.sh vars import --all" >&2
fi
```

Replace the lock block (`exec {lock}<"$env_file"` ...) with:

```bash
# One deploy of a stack to an environment at a time. The lock is a file in <secrets>/locks, which the
# Jenkins agent mounts read-write, so the agent and a deploy by hand on the host exclude each other.
lock_file="$secrets/locks/$stack-$environment.lock"
if command -v flock >/dev/null 2>&1 && { mkdir -p "$secrets/locks" 2>/dev/null; touch "$lock_file" 2>/dev/null; }; then
  exec {lock}<"$lock_file"
  locked=0
  flock --nonblock --conflict-exit-code 75 "$lock" || locked=$?
  if ((locked == 75)); then
    echo "deploy: another deploy of $stack to $environment is running; waiting for it to finish" >&2
    flock "$lock"
  elif ((locked != 0)); then
    echo "deploy: cannot lock $lock_file; going on unlocked" >&2
  fi
elif command -v flock >/dev/null 2>&1; then
  echo "deploy: cannot create $lock_file; going on unlocked" >&2
fi
```

(Without `flock` — Git Bash on Windows — it goes on unlocked silently, as before.)

Change the labels trap (`trap 'rm -f "$labels_file"' EXIT`) to add to the same list: `cleanup_paths+=("$labels_file")` (and delete that `trap` line). Update the header comment's "The env file is ..." paragraph to: "The variables come from the variables store when `<secrets>/vars.db` exists (rendered into a private temporary file), else from `<secrets>/<environment>/<stack>.env`. Its path is exported as APP_ENV_FILE ...".

- [ ] **Step 4: Run tests and shellcheck**

Run: `cd /root/repositories/yggdrasil && python3 -m unittest discover -s scripts 2>&1 | tail -2 && /tmp/claude-0/-root-repositories/c9525b5f-2c8e-4a23-9b69-8c4d43e8f84c/scratchpad/venv/bin/shellcheck scripts/*.sh && echo clean`
Expected: `OK (skipped=...)` and `clean`.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.sh scripts/test_deploy.py
git commit -F - <<'MSG'
feat: deploy from the variables store

Render the application's variables into a private temporary file
when vars.db exists, else read the env file with a notice; lock on
<secrets>/locks/<app>-<environment>.lock.
MSG
```

---

### Task 6: platform.sh, compose.yml and the Jenkins agent

**Files:**
- Modify: `scripts/platform.sh`
- Modify: `scripts/test_platform.py`
- Modify: `platform/compose.yml` (Traefik `env_file`, agent `volumes`)
- Modify: `platform/jenkins/agent/Dockerfile`

**Interfaces:**
- Consumes: `vars.py check`, `vars.py render-platform [--acme]`.
- Produces: `platform.sh up [--last-good]`; files `<secrets>/last-good/platform.env` and `acme.env` (0600); env var `YGG_ACME_ENV_FILE` for Compose; `<secrets>/locks` (mode 2770, group docker) created by `up`.

- [ ] **Step 1: Write the failing tests** (in `scripts/test_platform.py`)

```python
    def use_store(self, platform, acme):
        import sys
        sys.path.insert(0, str(SCRIPT.parent))
        import vars as v
        v.init(self.secrets, confirm=lambda prompt: "saved")
        with v.Store.open(self.secrets) as store:
            for key, value in platform.items():
                store.set("platform", key, value, None, "set")
            for key, value in acme.items():
                store.set("platform:acme", key, value, None, "set")

    def run_platform(self, *arguments):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=self.env, capture_output=True, text=True)

    def test_given_a_store_when_up_then_compose_reads_rendered_files_and_last_good_is_saved(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": ""}, {"CF_DNS_API_TOKEN": "abc"})
        result = self.run_platform("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"--env-file {self.secrets}/platform.env", self.docker_calls())
        last_good = self.secrets / "last-good"
        self.assertEqual((last_good / "platform.env").read_text(), "COMPOSE_PROFILES=''\nENVIRONMENTS='production'\n")
        self.assertEqual((last_good / "acme.env").read_text(), "CF_DNS_API_TOKEN='abc'\n")
        self.assertEqual((last_good / "platform.env").stat().st_mode & 0o777, 0o600)

    def test_given_last_good_when_up_then_the_store_is_not_opened(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": ""}, {})
        self.assertEqual(self.run_platform("up").returncode, 0)
        (self.secrets / "vars.key").unlink()
        result = self.run_platform("up", "--last-good")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("last-good", result.stdout + result.stderr)
        self.assertIn(f"--env-file {self.secrets}/last-good/platform.env", self.docker_calls())

    def test_given_a_store_without_its_key_when_up_then_it_stops_suggesting_last_good(self):
        self.use_store({"ENVIRONMENTS": "production"}, {})
        (self.secrets / "vars.key").unlink()
        result = self.run_platform("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--last-good", result.stderr)

    def test_given_no_store_when_up_then_platform_env_with_a_notice(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("move to the variables store", result.stderr)
```

(`docker_calls()` already returns the fake docker's log; the fake exits 0 for every call, so `compose up` "succeeds".)

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_platform 2>&1 | tail -3`
Expected: FAIL on the four new tests.

- [ ] **Step 3: Implement**

`scripts/platform.sh` — replace the top (from `env_file="$secrets/platform.env"` through the `compose()` definition) with:

```bash
last_good=""
[[ "${1:-}" == up && "${2:-}" == --last-good ]] && last_good=1
render_dir=""
trap '[[ -n "$render_dir" ]] && rm -rf "$render_dir"' EXIT

# The platform's settings: rendered from the variables store when this host has one
# (docs/variables.md), else platform.env and acme.env. --last-good starts from the copy the last
# successful `up` saved, without opening the store: for when the store or its key is unusable.
if [[ -n "$last_good" ]]; then
  env_file="$secrets/last-good/platform.env"
  acme_file="$secrets/last-good/acme.env"
  [[ -f "$env_file" ]] || die "no last-good copy in $secrets/last-good (one is saved by every successful 'platform.sh up' from the store)"
  echo "platform: starting from the last-good copy in $secrets/last-good, not the variables store" >&2
elif [[ -f "$secrets/vars.db" ]]; then
  python3 "$root/scripts/vars.py" check >&2 \
    || die "the variables store failed its check; fix it (scripts/ygg.sh vars check) or run 'platform.sh up --last-good'"
  run_base=/run/yggdrasil
  mkdir -p "$run_base" 2>/dev/null && [[ -w "$run_base" ]] || run_base="${TMPDIR:-/tmp}/yggdrasil-$(id -u)"
  mkdir -p -m 700 "$run_base"
  render_dir=$(mktemp -d "$run_base/platform.XXXXXX")
  env_file="$render_dir/platform.env"
  acme_file="$render_dir/acme.env"
  (umask 077 && python3 "$root/scripts/vars.py" render-platform >"$env_file" \
    && python3 "$root/scripts/vars.py" render-platform --acme >"$acme_file") \
    || die "could not render the platform's variables; 'platform.sh up --last-good' starts from the last copy that worked"
else
  env_file="$secrets/platform.env"
  acme_file="$secrets/acme.env"
  [[ -f "$env_file" ]] || die "missing $env_file (template: env/platform.env.example)"
  echo "platform: reading $env_file; move to the variables store with scripts/ygg.sh vars init && scripts/ygg.sh vars import --all" >&2
fi
export YGG_ACME_ENV_FILE="$acme_file"

compose() { docker compose --env-file "$env_file" -f "$root/platform/compose.yml" "$@"; }
```

In the `up)` branch, before `compose up`, create the agent's lock directory, and after `compose ps`, save the last-good copy:

```bash
    install -d -m 2770 "$secrets/locks"
    getent group docker >/dev/null && chgrp docker "$secrets/locks" 2>/dev/null || true
    compose up --detach --build --remove-orphans --wait
    compose ps
    if [[ -n "$render_dir" ]]; then
      install -d -m 700 "$secrets/last-good"
      install -m 600 "$env_file" "$acme_file" "$secrets/last-good/"
    fi
```

Make the usage line read `platform.sh up [--last-good] | down | ps | logs [service] | config`, and update the header comment accordingly.

`platform/compose.yml`:
- Traefik `env_file`: `- ${YGG_ACME_ENV_FILE:-${YGG_SECRETS_DIR:-/etc/yggdrasil}/acme.env}` (comment: "rendered by platform.sh from the variables store, or acme.env next to platform.env").
- Agent `volumes`, after the read-only secrets mount:

```yaml
      # Deploy locks (scripts/deploy.sh), shared with deploys by hand on the host: read-write, unlike
      # the rest of the secrets directory.
      - ${YGG_SECRETS_DIR:-/etc/yggdrasil}/locks:/etc/yggdrasil/locks
```

`platform/jenkins/agent/Dockerfile`: add `python3-cryptography` to the `apt-get install` list after `python3-yaml`, and after the install `RUN` add:

```dockerfile
# vars.py (the variables store) needs it; fail the build rather than the first deploy.
RUN python3 -c "import cryptography, sqlite3"
```

(Place it before `USER jenkins`.)

- [ ] **Step 4: Run tests, shellcheck and the CI Compose validation**

Run:
```bash
cd /root/repositories/yggdrasil && python3 -m unittest discover -s scripts 2>&1 | tail -2
/tmp/claude-0/-root-repositories/c9525b5f-2c8e-4a23-9b69-8c4d43e8f84c/scratchpad/venv/bin/shellcheck scripts/*.sh && echo clean
s=$(mktemp -d); printf 'ENVIRONMENTS=production\nDOMAIN=example.com\nACME_EMAIL=ci@example.com\nACME_DNS_PROVIDER=cloudflare\nTRAEFIK_DASHBOARD_USERS=x\nGRAFANA_ADMIN_PASSWORD=x\nYGGDRASIL_STATUS_TOKEN=0000000000000000000000000000000000000000\nDOCKER_GID=999\nYGG_SECRETS_DIR=%s\n' "$s" > "$s/platform.env"; touch "$s/acme.env"; GITHUB_OWNER=x docker compose --env-file "$s/platform.env" -f platform/compose.yml config --quiet && echo compose-ok
```
Expected: `OK`, `clean`, `compose-ok`.

- [ ] **Step 5: Commit**

```bash
git add scripts/platform.sh scripts/test_platform.py platform/compose.yml platform/jenkins/agent/Dockerfile
git commit -F - <<'MSG'
feat: start the platform from the variables store

Render platform.env and acme.env from vars.db, keep a last-good copy
after every successful up, add up --last-good, and give the Jenkins
agent the cryptography package and a read-write locks mount.
MSG
```

---

### Task 7: ygg.sh on the store

**Files:**
- Modify: `scripts/ygg.sh` (helpers near `env_value` ~line 133; `host_environments` ~160; `check_report` ~327-355; `create_env_file` ~589; `add_application` ~807-815; `configure_app` ~1095; `menu` ~1183; the `case` at the end)
- Modify: `scripts/test_ygg.py`

**Interfaces:**
- Consumes: `vars.py get/list/set/unset/edit/import/history/rollback/check/backup/init`.
- Produces: `scripts/ygg.sh vars <vars.py arguments>`; helpers `has_store`, `vars_py`, `platform_value <NAME>`, `app_has_variables <app> <env>`.

- [ ] **Step 1: Write the failing tests** (in `scripts/test_ygg.py`)

```python
class VariablesStoreTests(EnvironmentCommandTests):
    def use_store(self):
        import sys
        sys.path.insert(0, str(SCRIPT.parent))
        import vars as v
        (self.secrets / "platform.env").unlink()
        v.init(self.secrets, confirm=lambda prompt: "saved")
        with v.Store.open(self.secrets) as store:
            store.set("platform", "ENVIRONMENTS", "homologation,production", None, "set")

    def test_given_a_store_when_env_status_then_environments_come_from_it(self):
        self.use_store()
        result = self.ygg("env", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("homologation", result.stdout)
        self.assertNotIn("development", result.stdout)

    def test_given_vars_when_run_then_it_passes_through_to_vars_py(self):
        self.use_store()
        self.assertEqual(self.ygg("vars", "set", "platform", "DOMAIN=example.com").returncode, 0)
        self.assertEqual(self.ygg("vars", "get", "platform", "DOMAIN").stdout, "example.com\n")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/repositories/yggdrasil/scripts && python3 -m unittest test_ygg 2>&1 | tail -3`
Expected: FAIL (`unknown command 'vars'`; environments still from the deleted platform.env → the menu would ask).

- [ ] **Step 3: Implement in `scripts/ygg.sh`**

After `env_value()` add:

```bash
# The variables store (docs/variables.md): when this machine has one, every platform and application
# value comes from it instead of platform.env and <environment>/<app>.env.
has_store() { [[ -f "$secrets/vars.db" ]]; }
vars_py() { python3 "$root/scripts/vars.py" "$@"; }

# platform_value <NAME>: a platform setting, from the store or platform.env; empty when unset.
platform_value() {
  if has_store; then
    vars_py get platform "$1" --reveal 2>/dev/null || true
  else
    env_value "$secrets/platform.env" "$1"
  fi
}

# Whether an application has variables of its own in an environment on this machine.
app_has_variables() {
  if has_store; then
    [[ -n "$(vars_py list "$1@$2" --keys 2>/dev/null)" ]]
  else
    [[ -f "$secrets/$2/$1.env" ]]
  fi
}
```

Replace every `env_value "$secrets/platform.env" <NAME>` call (lines ~167, 168, 347, 352, 601) with `platform_value <NAME>`, and the messages that name `$secrets/platform.env` there with `"$(has_store && echo "the variables store" || echo "$secrets/platform.env")"`.

In `check_report`, where it loops `for file in platform.env acme.env`, wrap it:

```bash
  if has_store; then
    if vars_py check >/dev/null 2>&1; then row ok "variables store" "$secrets/vars.db"; else row warn "variables store" "fails its check: $0 vars check"; fi
  else
    # (the existing platform.env / acme.env loop, unchanged)
  fi
```

`create_env_file` — keep building the text, but write it to the store when there is one. Replace `} >"$file"` / `chmod 640 "$file"` / `say "Wrote $file"` with:

```bash
  } >"$file.new"
  if has_store; then
    vars_py import "$id@$environment" "$file.new"
    rm -f "$file.new"
    say "Stored $id's variables in $environment (scripts/ygg.sh vars list $id@$environment)"
  else
    mv "$file.new" "$file"
    chmod 640 "$file"
    say "Wrote $file"
  fi
```

and only create `$dir` (`install -d -m 2750 "$dir"`) in the file branch's absence of a store (`has_store || [[ -d "$dir" ]] || install -d -m 2750 "$dir"`).

`add_application` (~807-815) and `configure_app` (~1110-1116): replace `[[ -f "$secrets/$environment/$id.env" ]]` tests with `app_has_variables "$id" "$environment"`; in `add_application`'s "Edit it now?" use `has_store && vars_py edit "$id@$environment" || "${EDITOR:-nano}" "$secrets/$environment/$id.env"`.

`configure_app` with a store — at the top of the `while true` loop body branch on `has_store`:

```bash
    if has_store; then
      title "$id in $environment (variables store)"
      vars_py list "$id@$environment" --resolved ${reveal:+--reveal}
      say ""
      choose pick "Then:" \
        "Set a variable" "Remove a variable" "Edit in ${EDITOR:-nano}" \
        "$([[ -n "$reveal" ]] && echo "Hide secret values" || echo "Show secret values")" \
        "History" "Apply: redeploy $id${changed:+ (changed)}" "Back"
      case $pick in
        Set*)
          ask_match name "Variable name" '^[A-Za-z_][A-Za-z0-9_]*$' "letters, digits and underscores"
          if [[ "${name^^}" =~ $SECRET_NAME ]]; then vars_py set "$id@$environment" "$name=-"
          else ask value "Value" "$(vars_py get "$id@$environment" "$name" 2>/dev/null || true)"; vars_py set "$id@$environment" "$name=$value"; fi
          changed=1 ;;
        Remove*)
          local names
          mapfile -t names < <(vars_py list "$id@$environment" --keys)
          ((${#names[@]})) || continue
          choose name "Which variable?" "${names[@]}"
          vars_py unset "$id@$environment" "$name"; changed=1 ;;
        Edit*) vars_py edit "$id@$environment" ${reveal:+--reveal}; changed=1 ;;
        Show*) reveal=yes ;;
        Hide*) reveal="" ;;
        History) vars_py history "$id@$environment" --limit 20; pause ;;
        Apply*) if deploy "$id" "$environment"; then changed=""; fi; pause ;;
        Back) [[ -n "$changed" ]] && warn "Changes reach $id on its next deploy (Apply, Jenkins, or scripts/deploy.sh)."; return 0 ;;
      esac
      continue
    fi
```

(The existing file-based body follows unchanged; the "file not readable" checks before the loop move inside `has_store || { ... }`.)

Add a menu entry "Variables and secrets" before "Quit", with `Variables*) run variables_menu ;;` and:

```bash
variables_menu() {
  if ! has_store; then
    say "This machine keeps its variables in env files under $secrets."
    if confirm "Create the variables store and import them now?" n; then
      vars_py init
      vars_py import --all
    fi
    return 0
  fi
  local pick scope
  choose pick "Variables and secrets:" "List a scope" "Set a variable" "Edit a scope in ${EDITOR:-nano}" "History" "Roll a change back" "Check the store" "Back up the store" "Back"
  case $pick in
    List*) ask scope "Scope (platform, @<environment>, <application>, <application>@<environment>)" "platform"; vars_py list "$scope" ;;
    Set*) ask scope "Scope" "platform"; ask_match pick "KEY=value (KEY=- to type a hidden value)" '^[A-Za-z_][A-Za-z0-9_]*=' "KEY=value"; vars_py set "$scope" "$pick" ;;
    Edit*) ask scope "Scope" "platform"; vars_py edit "$scope" ;;
    History) vars_py history --limit 30 ;;
    Roll*) ask_match pick "Change id (from History)" '^[0-9]+$' "a number"; vars_py rollback "$pick" ;;
    Check*) vars_py check ;;
    Back\ up*) ask scope "Into which directory?" "/root/yggdrasil-backups"; vars_py backup "$scope" ;;
    Back) return 0 ;;
  esac
  pause
}
```

In the final `case`: add `vars) shift; vars_py "$@" ;;` and add `vars <vars.py command>` to the usage string in the `*)` branch and to the header comment's command list.

- [ ] **Step 4: Run tests and shellcheck**

Run: `cd /root/repositories/yggdrasil && python3 -m unittest discover -s scripts 2>&1 | tail -2 && /tmp/claude-0/-root-repositories/c9525b5f-2c8e-4a23-9b69-8c4d43e8f84c/scratchpad/venv/bin/shellcheck scripts/*.sh && echo clean`
Expected: `OK`, `clean`.

- [ ] **Step 5: Commit**

```bash
git add scripts/ygg.sh scripts/test_ygg.py
git commit -F - <<'MSG'
feat: manage variables through ygg.sh

Add ygg.sh vars, read platform settings from the store, and run
config and add against it, with a Variables and secrets menu.
MSG
```

---

### Task 8: CI and documentation

**Files:**
- Modify: `.github/workflows/ci.yml` (Platform job's `pip install`)
- Create: `docs/variables.md`
- Modify: `docs/setup.md` (steps 9.2, 10, 11 and "Where Jenkins and the hosts read the catalog"), `docs/cli.md`, `docs/catalog.md`, `README.md`, `docs/examples/docker-desktop-and-vps/README.md`, `env/platform.env.example`, `env/acme.env.example`, `CHANGELOG.md`

- [ ] **Step 1: CI**

In `.github/workflows/ci.yml`, Platform job: `- run: pip install --quiet pyyaml cryptography`.

- [ ] **Step 2: Write `docs/variables.md`**

Sections (each with the exact commands from Tasks 1–7):
1. *What it is* — one database per machine, `vars.db` + `vars.key`, what is stored, opt-in.
2. *Scopes and layers* — the scope table from the spec; resolution order with a worked heimdall-api/fortuna-api example (`@development DB_HOST`, `heimdall-api LOCALE`, `heimdall-api@development DB_NAME`) and the `list --resolved` output it prints.
3. *References* — `${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}` for fortuna-api; the one-level rule; errors.
4. *Commands* — the CLI table (including `edit`), `KEY=-` for hidden input.
5. *Secrets and masking* — everything encrypted; the secret flag; what encryption does and doesn't protect.
6. *Values the store refuses* — single quotes, line breaks, leading/trailing whitespace, and why (single-quoted rendering).
7. *Moving to the store* — `ygg.sh vars init`, store the key, `ygg.sh vars import --all`, move-up offers, `vars check`, `platform.sh up`, redeploy one application, delete `*.env.imported` when satisfied. Windows workstation: same with `YGG_SECRETS_DIR=$PWD/env`; NTFS permissions note.
8. *Backup and recovery* — `vars backup /root/yggdrasil-backups` and a cron example (`0 3 * * 0 root /opt/yggdrasil/scripts/ygg.sh vars backup /root/yggdrasil-backups`); `platform.sh up --last-good`; restoring a backup (copy both files back, `vars check`); a lost key means every secret must be entered again.
9. *How deploys use it* — rendering to `/run/yggdrasil`, removed on exit; the lock files; the Jenkins agent's read-only access.

- [ ] **Step 3: Update the other docs**

- `docs/setup.md`: step 9.2 and 10 — after filling `platform.env`, run `scripts/ygg.sh vars init` and `scripts/ygg.sh vars import --all` (or skip to keep files); step 11 — application variables with `scripts/ygg.sh vars set <app>@<environment> ...` / `vars import`; "Where … read the catalog" — note that variables are read per deploy, no restart needed.
- `docs/cli.md`: the `vars` command and the menu entry; `config` with a store.
- `docs/catalog.md`: one sentence in *Application fields* that an application's env values come from the variables store or its env file (link `variables.md`).
- `README.md`: Concepts table row "Variables store"; Day to day rows `scripts/ygg.sh vars set heimdall-api@development KEY=value` and `vars history`; Layout row `scripts/vars.py`; docs list adds `docs/variables.md`.
- `docs/examples/docker-desktop-and-vps/README.md`: the env templates are `vars import` input (`scripts/ygg.sh vars import heimdall-api@development docs/examples/.../env/development/heimdall-api.env.example`, then `vars edit`), and the move-up of `DB_HOST`/the fortuna reference.
- `env/platform.env.example`, `env/acme.env.example`: header line "With the variables store (docs/variables.md) these values are imported with `scripts/ygg.sh vars import --all`; this file is then only the template."
- `CHANGELOG.md` `## [Unreleased]`: `### Added` — the variables store (`scripts/vars.py`, `ygg.sh vars`, layers, references, encryption, history, import/export, backup, check); `platform.sh up --last-good`. `### Changed` — deploy.sh/platform.sh/ygg.sh read the store when `vars.db` exists; deploy lock moved to `<secrets>/locks/`; the Jenkins agent image installs `python3-cryptography` and mounts `locks` read-write. `### Upgrading from 0.5 to 0.6` — optional; nothing changes until `vars init`; steps from docs/variables.md §7; after upgrading the platform once (`platform.sh up`, for the agent's new mount and package) deploys work with either source.

- [ ] **Step 4: Verify**

Run:
```bash
cd /root/repositories/yggdrasil && python3 /tmp/claude-0/-root-repositories/c9525b5f-2c8e-4a23-9b69-8c4d43e8f84c/scratchpad/linkcheck.py README.md CHANGELOG.md docs/*.md docs/examples/docker-desktop-and-vps/README.md
grep -rnE '[0-9]{1,3}(\.[0-9]{1,3}){3}' docs/variables.md | grep -vE '127\.0\.0\.1|172\.16\.0\.0' || echo no-ips
```
Expected: `broken: 0`, `no-ips`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml docs README.md env CHANGELOG.md
git commit -F - <<'MSG'
docs: document the variables store

Add docs/variables.md and the setup, CLI, example and upgrade notes
for moving env files into the store; install cryptography in CI.
MSG
```

---

## After the plan (not tasks)

Push `feature/variables-store`, open the PR into `develop`, wait for CI, merge; release 0.6.0 like 0.5.x (changelog PR, `release/0.6.0`, tag). The rollout on the VPS (spec, "Rollout") needs the owner present for `vars init` (the key must be stored by them) — do it together, not unattended.
