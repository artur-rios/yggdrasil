# Variables store: env vars and secrets in SQLite

Status: approved design, 2026-10-08. Target release: yggdrasil 0.6.0.

## Goal

Replace hand-edited env files with a per-machine SQLite database that the yggdrasil CLI manages, so that:

1. single variables can be created, read, updated and deleted from the CLI;
2. a value shared by several applications or environments is defined once and inherited;
3. every value is encrypted at rest;
4. every change is recorded (who, when, what), and can be rolled back.

Scope: every application's variables in every environment, the platform's own settings (`platform.env`, `acme.env`),
and the developer workstation (the `local` environment on Windows), with one database per machine. Nothing is shared
between machines.

Non-goals: remote editing (the console stays read-only), sharing one database between hosts, a secrets service, key
rotation (a later release can add `vars rekey`).

## Storage

- `$YGG_SECRETS_DIR/vars.db` (default `/etc/yggdrasil/vars.db`) and `$YGG_SECRETS_DIR/vars.key`, both `root:docker`
  `0640`. On Windows, `env/vars.db` and `env/vars.key` in the yggdrasil checkout (`YGG_SECRETS_DIR=$PWD/env`); NTFS
  does not enforce these permissions, which the docs state.
- SQLite in its classic rollback-journal mode (`journal_mode=DELETE`), not WAL, so the Jenkins agent can open the
  database from a read-only mount (`file:...?mode=ro`) while the CLI writes on the host.
- Python: `scripts/vars.py`, using the standard library's `sqlite3` and the `cryptography` package
  (`python3-cryptography` on Debian/Ubuntu, `pip install cryptography` on Windows).

### Tables

| Table | Columns | Notes |
|---|---|---|
| `meta` | `key`, `value` | `schema_version` (starts at 1), `created_at`. `vars.py` migrates older schemas forward on write and refuses a newer one. |
| `variables` | `scope`, `key`, `value` (encrypted), `secret` (0/1), `updated_at`, `updated_by` | Primary key (`scope`, `key`). |
| `history` | `id`, `at`, `actor`, `command`, `scope`, `key`, `old_value` (encrypted, nullable), `new_value` (encrypted, nullable), `secret` | Append-only. `old_value` null = created; `new_value` null = deleted. |

### Scopes

| Scope (CLI spelling) | Stored as | Applies to |
|---|---|---|
| `platform` | `platform` | `platform.env` |
| `platform:acme` | `platform:acme` | `acme.env` (the DNS provider's credentials, kept apart so only Traefik gets them) |
| `@<environment>` | `env:<environment>` | every application in that environment |
| `<application>` | `app:<application>` | that application in every environment |
| `<application>@<environment>` | `app:<application>@<environment>` | that application in that environment |

Application and environment ids are validated against `catalog.yaml` on `set` and `import`; `vars check` reports
rows whose application or environment no longer exists, without failing on them.

### Encryption

- Every value is encrypted with Fernet (AES-128-CBC + HMAC-SHA256) under the key in `vars.key`, whatever its secret
  flag: no value's protection depends on classifying it correctly. Variable names are stored in clear, so they can be
  listed and searched.
- The `secret` flag only governs display: secret values are masked (`••••••` plus the last 2 characters for values of
  12 characters or more) in `get`, `list`, `history` and `config`, unless `--reveal` is given. It defaults to on for
  names containing `SECRET`, `PASSWORD`, `PASS`, `TOKEN` or `KEY`; `--secret` / `--no-secret` override it.
- What encryption protects: copies of the database (backups, a stray file). It does not protect against root on the
  host or the Jenkins agent, which must read the key to deploy (and already controls Docker).
- A missing or wrong key fails every command that reads values with `vars: cannot decrypt <scope> <KEY>: wrong or
  missing key ($YGG_SECRETS_DIR/vars.key)`.

## Resolution

For application `A` in environment `E`, the variables are the union of the scopes `env:E`, `app:A`, `app:A@E`; for a
key in more than one, the most specific wins: `app:A@E` > `app:A` > `env:E`. Platform scopes never mix into
applications.

### References

- A value of the exact form `${ref:<application>:<KEY>}` is replaced by `<KEY>` as resolved for `<application>` in the
  same environment `E`.
- One level only: the target's value is used as stored after its own resolution of layers, and a target that is
  itself a reference is an error (`vars: <scope> <KEY>: reference to a reference`). So no chains and no loops.
- A target that does not resolve is an error naming the reference; `render` and `check` fail on it.
- References are only valid in application scopes. `$$` in any value is a literal `$` (as in Compose).

### Rendering

`vars.py render <application> <environment>` prints the resolved variables as `KEY=value` lines, sorted by key.
`vars.py render-platform [--acme]` prints `platform` (or `platform:acme`).

Values that Compose's env-file parser would read differently are rejected at `set`/`import` time: newlines, a
leading or trailing quote, `#` preceded by whitespace. Keys must match `^[A-Za-z_][A-Za-z0-9_]*$`.

## Command line

`scripts/vars.py` implements everything; `scripts/ygg.sh vars <command>` passes through to it and adds the
interactive prompts. Every command that writes records history (actor `$SUDO_USER`, else `$USER`; command the CLI
verb).

| Command | Behaviour |
|---|---|
| `vars init` | Creates `vars.db` and `vars.key` (refuses if either exists). Prints the key once and requires typing `saved` to confirm it is stored somewhere else (a password manager) before finishing. |
| `vars set <scope> KEY=value [KEY=value ...] [--secret\|--no-secret]` | Creates or updates. `KEY=-` prompts for the value with echo off (`getpass`), so secrets stay out of shell history. |
| `vars get <scope> KEY [--reveal]` | One stored value of that scope (not resolved). Exit 1 when absent. |
| `vars list <scope> [--resolved] [--reveal]` | The scope's variables. `--resolved` (application@environment only) shows the effective set with, per key, the layer it came from (`app@env`, `app`, `env`) and `ref → <app>:<KEY>` for references. |
| `vars unset <scope> KEY [KEY ...]` | Deletes. Exit 1 when a key is absent. |
| `vars history [<scope>] [KEY] [--limit N] [--reveal]` | Newest first: id, time, actor, command, scope, key, old → new (masked per the secret flag). |
| `vars rollback <id>` | Restores the state before change `<id>` (re-creates, restores or deletes the key), recorded as a new change. Refuses when the key changed again after `<id>`, unless `--force`. |
| `vars import <scope> <file> [--replace]` | Reads env-file text into a scope. Without `--replace`, keys already set are left alone and reported. |
| `vars import --all [--dir <secrets dir>]` | Migration: imports `platform.env` → `platform`, `acme.env` → `platform:acme`, every `<environment>/<application>.env` → `app:<application>@<environment>`. Then lists keys whose value is identical in every environment of an application, or in every application of an environment, and offers (`y/N` per key) to move them up a layer. Renames each imported file to `*.env.imported`. |
| `vars export <scope> [--resolved]` | Env-file text on stdout (always revealed: it is meant for files). |
| `vars backup <dir>` | Consistent copy of the database (SQLite backup API) plus the key, into `<dir>/vars-<timestamp>.db` and `.key`, `0600`. |
| `vars check [<application> <environment>]` | `PRAGMA integrity_check`, schema version, every value decrypts, every reference resolves, unknown applications/environments listed as warnings. With arguments, only what that render needs. Exit 1 on any error. |

Existing commands:

- `ygg.sh config <application> [<environment>]` opens the `application@environment` scope as env-file text in
  `$EDITOR` (secret values masked; answering `y` to "reveal secrets in the editor?" shows them). On save, unchanged
  masked values are kept, and the difference is applied as `set`/`unset` (history command `config`). Then it offers to
  redeploy, as today.
- `ygg.sh add` writes the new application's variables to `application@environment` instead of a file.
- The interactive menu gains "Variables and secrets" (list, set, history, rollback, check, backup).
- `ygg.sh` reads `ENVIRONMENTS`, `DOMAIN` and the other platform values through `vars.py get platform` when the
  database exists.

## Integration

### deploy.sh

1. If `vars.db` exists: `vars.py check <application> <environment>`, then render into a private directory
   (`/run/yggdrasil` when writable, else `${TMPDIR:-/tmp}/yggdrasil-$UID`; `umask 077`, `mktemp`), removed by the exit
   trap. `APP_ENV_FILE` and Compose's `--env-file` point at it.
2. If it does not: the current `<secrets>/<environment>/<application>.env`, with a one-line notice:
   `deploy: reading <file>; move to the variables store with scripts/ygg.sh vars init && scripts/ygg.sh vars import --all`.
3. The deploy lock moves from the env file to `<secrets>/locks/<application>-<environment>.lock` (created on demand,
   group `docker`, `0660`).

### platform.sh

- With the database: `check`, then render `platform.env` and `acme.env` into the private directory; Compose gets
  `--env-file <rendered platform.env>` and `YGG_ACME_ENV_FILE=<rendered acme.env>` (compose.yml's Traefik `env_file`
  becomes `${YGG_ACME_ENV_FILE:-${YGG_SECRETS_DIR:-/etc/yggdrasil}/acme.env}`).
- After `up` succeeds, the two rendered files are copied to `<secrets>/last-good/` (`root`, `0600`).
- `platform.sh up --last-good` uses `<secrets>/last-good/` and never opens the database; it prints that it does so.
- Without the database: today's files, with the same notice as deploy.sh.

### Jenkins agent

- `platform/jenkins/agent/Dockerfile` installs `python3-cryptography`.
- `platform/compose.yml` keeps the read-only mount of the secrets directory (database and key) and adds
  `${YGG_SECRETS_DIR}/locks:/etc/yggdrasil/locks` read-write.

## Safeguards

- `vars check` before every deploy and platform start.
- Key backup confirmation at `init`; `vars backup` documented with a suggested weekly cron line (not installed).
- `platform.sh up --last-good` when the database or key is unusable.
- `vars export` to plain files at any time.
- Writes take an exclusive SQLite transaction; a second writer waits up to 10 s (`busy_timeout`), then fails clearly.

## Migration and compatibility

- Opt-in: until `vars init` runs, every script behaves as in 0.5 (with the notice). After `vars import --all`, the
  database is the only source; renamed `*.env.imported` files are ignored and can be deleted.
- `docs/examples/docker-desktop-and-vps/env/**` stay as templates, now described as `vars import` input.
- SemVer: 0.6.0 (new capability, nothing breaks without opting in).

## Testing

- `scripts/test_vars.py`: layer resolution; references (same environment, missing target, reference to a reference,
  `$$`); encryption (stored bytes differ from the value, wrong key fails clearly); secret-flag defaults and overrides;
  history and rollback (including the `--force` guard); import/export round trip; `import --all` over a temporary
  secrets tree, including the move-up offer; rejection of unsafe values and keys; `check` detecting corruption, an
  undecryptable value and a broken reference; a read-only reader while a writer holds a transaction.
- `test_deploy.py`: render from the database, fallback to files with the notice, the new lock path, the rendered file
  removed on exit (success and failure).
- `test_platform.py`: render, last-good copy written after success, `--last-good` never opens the database.
- `test_ygg.py`: `vars` passthrough, `config` round trip with a fake `$EDITOR` (masked values kept).
- CI: the Platform job installs `python3-cryptography`; the agent image build checks `import cryptography`.

## Documentation

- New `docs/variables.md`: model, scopes and layers, references, commands, masking, key handling, backup, recovery
  (`--last-good`, restoring a backup, lost key = re-enter every secret).
- `docs/setup.md` steps 9 and 11, `docs/cli.md`, `docs/catalog.md` (where an application's values come from),
  `README.md` (day to day), the example README, `env/*.example` headers, CHANGELOG with "Upgrading from 0.5 to 0.6".

## Rollout on the owner's VPS (after the 0.6.0 release)

1. `vars backup`-equivalent copy of `/etc/yggdrasil` (tar), as for 0.5.
2. `ygg.sh vars init`; the owner stores the key.
3. `ygg.sh vars import --all`; move up `DB_HOST`, `DB_PORT`, `HEIMDALL_TRUSTED_PROXIES`, `FORTUNA_LOCALE` and other
   identical values; set fortuna-api's `FORTUNA_AUTH_TOKEN_SECRET` to `${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}`
   in each environment.
4. `ygg.sh vars check`; `platform.sh up` (verifies the platform renders identically: `vars export platform --resolved`
   diffed against the imported file before deleting it).
5. Redeploy production heimdall-api and heimdall-ui; start and stop development once.
