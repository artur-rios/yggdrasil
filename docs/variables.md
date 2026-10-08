# The variables store

Each machine can keep its environment variables and secrets in one encrypted database, managed from
the command line, instead of hand-edited env files. A value shared by several applications or
environments is defined once, every change is recorded and can be rolled back, and no value sits in
a file in clear.

It is opt-in: until you run `scripts/ygg.sh vars init`, every script reads the env files as before.
Reference for everything below is `scripts/vars.py`, which `scripts/ygg.sh vars <command>` runs.

- [What it is](#what-it-is)
- [Scopes and layers](#scopes-and-layers)
- [References](#references)
- [Commands](#commands)
- [Secrets and masking](#secrets-and-masking)
- [Values the store refuses](#values-the-store-refuses)
- [Moving to the store](#moving-to-the-store)
- [Backup and recovery](#backup-and-recovery)
- [How deploys use it](#how-deploys-use-it)

## What it is

One database per machine, nothing shared between machines:

| File | Default location | What |
|---|---|---|
| `vars.db` | `/etc/yggdrasil/vars.db` | SQLite: the variables and their history |
| `vars.key` | `/etc/yggdrasil/vars.key` | The key every value is encrypted with |

Both are `root:docker`, mode `0640` (`YGG_SECRETS_DIR` moves them, as for the env files). It holds:

- the **platform's** settings: `platform.env` and `acme.env`;
- every **application's** variables in every environment of the machine;
- the **history**: who changed what, when, from which value to which.

It needs Python 3.10 or later with PyYAML and the `cryptography` package
(`apt install python3-cryptography` on Ubuntu, `pip install cryptography` on Windows). The Jenkins
agent image installs it.

The database uses SQLite's classic rollback journal, not WAL, so the Jenkins agent can open it from
a read-only mount while you write on the host.

## Scopes and layers

Every variable lives in a **scope**:

| Scope | Applies to |
|---|---|
| `platform` | `platform.env`: the platform stack of this machine |
| `platform:acme` | `acme.env`: the DNS provider's credentials, apart so that only Traefik gets them |
| `@<environment>` | Every application in that environment |
| `<application>` | That application in every environment |
| `<application>@<environment>` | That application in that environment |

Application and environment ids are checked against `catalog.yaml` when you set or import.

An application's variables in an environment are the union of three scopes. For a key in more than
one, the most specific wins:

1. `<application>@<environment>`
2. `<application>`
3. `@<environment>`

The platform scopes never mix into applications.

For example, with these values:

```bash
scripts/ygg.sh vars set @development DB_HOST=postgres.example.com
scripts/ygg.sh vars set heimdall-api LOCALE=en-US
scripts/ygg.sh vars set heimdall-api@development DB_NAME=heimdall_dev DB_PASSWORD=-
```

`--resolved` shows what `heimdall-api` gets in development, and where each value comes from:

```console
$ scripts/ygg.sh vars list heimdall-api@development --resolved
DB_HOST=postgres.example.com  (env)
DB_NAME=heimdall_dev  (app@env)
DB_PASSWORD=••••••r2  (app@env)
LOCALE=en-US  (app)
```

`fortuna-api` in development gets `DB_HOST` from the same `@development` value, and sets its own
`DB_NAME`. Change `DB_HOST` once, in `@development`, and both applications follow at their next
deploy. An application can still override it with its own `DB_HOST` in either narrower scope.

## References

A value that is exactly `${ref:<application>:<KEY>}` is replaced by that application's `<KEY>`, as
resolved in the **same environment**. This is how fortuna-api shares heimdall-api's token secret:

```bash
scripts/ygg.sh vars set fortuna-api FORTUNA_AUTH_TOKEN_SECRET='${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}'
```

```console
$ scripts/ygg.sh vars list fortuna-api@development --resolved
FORTUNA_AUTH_TOKEN_SECRET=••••••23  (ref → heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET)
```

Rules:

- **One level.** The target is used after its own layers are resolved, but a target that is itself a
  reference is an error (`reference to a reference`). So there are no chains and no loops.
- **Applications only.** In `platform` or `platform:acme` a reference is refused.
- **The whole value.** `prefix-${ref:...}` is a literal string, not a reference.
- **The target must exist** in that environment. If not, `vars check` and the deploy of that
  application fail and name it (an environment this machine does not run is only a warning):

  ```text
  error: fortuna-api@production A -> heimdall-api:NOPE: heimdall-api has no NOPE in production
  ```

## Commands

Run them as `scripts/ygg.sh vars <command>` (or `python3 scripts/vars.py <command>`). Every command
that writes records a history entry with the user (`$SUDO_USER`, else `$USER`), the time and the
command.

| Command | Does |
|---|---|
| `vars init` | Creates `vars.db` and `vars.key` (refuses if either exists). Prints the key once and waits until you type `saved`: store it in a password manager first. Any other answer, end of input, Ctrl-C or a failure removes both files again |
| `vars set <scope> KEY=value [KEY=value ...] [--secret\|--no-secret]` | Creates or updates variables. `KEY=-` asks for the value with echo off, so a secret stays out of your shell history |
| `vars get <scope> KEY [--reveal]` | One stored value of that scope (not resolved). Exit 1 when absent |
| `vars list <scope> [--resolved] [--reveal] [--keys]` | The scope's variables. `--resolved` (for `<application>@<environment>`) shows the effective set and where each value came from. `--keys` prints names only |
| `vars unset <scope> KEY [KEY ...]` | Deletes variables. Exit 1 when one is absent |
| `vars edit <scope> [--reveal]` | Opens the scope as `KEY='value'` lines in `$EDITOR` (default `nano`). Secrets are masked; a masked line left alone keeps its value, a deleted line removes the variable. The whole edit is validated before anything is written; a rejected edit writes nothing and keeps your text in a `0600` file whose path the error names. `ygg.sh config` uses it |
| `vars history [<scope>] [KEY] [--limit N] [--reveal]` | Newest first: id, time, user, command, scope, key, old → new |
| `vars rollback <id> [--force]` | Restores the state before change `<id>` (re-creates, restores or deletes the key), recorded as a new change. Refuses when the key changed again after `<id>`, unless `--force` |
| `vars import <scope> <file> [--replace]` | Reads env-file text into a scope, each value as Compose read it from the file: single-quoted literally, `$$` as `$` when unquoted or double-quoted, where any other `$` (Compose interpolation) is refused naming the line. Without `--replace`, keys already set are left alone and reported |
| `vars import --all [--dir <secrets dir>] [--move-up ask\|yes\|no]` | Imports every env file of the machine: see [Moving to the store](#moving-to-the-store) |
| `vars export <scope> [--resolved]` | Env-file text on stdout, **always revealed**: it is meant for files |
| `vars backup <dir>` | Writes `vars-<UTC timestamp>.db` and `.key` into `<dir>`, both `0600`: see [Backup](#backup-and-recovery) |
| `vars check [<application> <environment> \| --platform \| --usable]` | Integrity, schema version, that every value decrypts, that every reference resolves; applications and environments no longer in the catalog are warnings. References are errors for what this machine runs (an `<application>@<environment>` scope of its own, or an environment of the platform's `ENVIRONMENTS` the application deploys to) and warnings for the catalog's other pairs. With arguments, only what that deploy needs, strictly (`deploy.sh`). `--platform`: only the `platform` and `platform:acme` values (`platform.sh`). `--usable`: only that the store opens with its key and is intact (`ygg.sh`). Exit 1 on any error |

The interactive menu has the same under **Variables and secrets** (list, set, edit, history, roll
back, check, back up), and offers to create the store if the machine has none. `scripts/ygg.sh
config <application> [<environment>]` works on the store when there is one
([cli.md](cli.md#change-the-configuration)).

Examples:

```bash
scripts/ygg.sh vars set heimdall-api@production HEIMDALL_JWT_ISSUER=https://heimdall.example.com
scripts/ygg.sh vars set heimdall-api@production DB_PASSWORD=-       # typed hidden
scripts/ygg.sh vars history heimdall-api@production --limit 10
scripts/ygg.sh vars rollback 42
```

## Secrets and masking

**Every value is encrypted**, whatever its name, with Fernet (AES-128-CBC with an HMAC-SHA256) under
`vars.key`, so no value's protection depends on classifying it correctly. Variable names are stored
in clear, so they can be listed and searched.

The **secret flag** only governs display: `get`, `list`, `history` and `edit` show a secret as
`••••••` plus its last two characters (just `••••••` under 12 characters), unless you pass
`--reveal`. It is on by default when the name ends in `PASSWORD`, `PASSWD`, `PASS`, `PWD`, `SECRET`,
`TOKEN`, `KEY`, `CREDENTIAL` or `CREDENTIALS`, optionally followed by `_PREVIOUS`, or is
`CONNECTION_STRING` / `CONNECTIONSTRING` (the same rule as `ygg.sh config`). `--secret` and
`--no-secret` override it, and the flag is kept when the value is updated or moved.

What encryption protects: a copy of the database on its own (a backup, a stray file). It does not
protect against root on the host, or the Jenkins agent, which reads the key to deploy (and already
controls Docker). `vars export` and the rendered files hold values in clear by design.

## Values the store refuses

Deploys and the platform get their variables as `KEY='value'` lines, single-quoted, which Compose
reads literally: `$`, `#`, `"`, `=`, backslashes and inner spaces reach the container as written,
and there is no `$$` escape. In return the store refuses, at `set`, `edit` and `import`:

| Refused | Why |
|---|---|
| A value containing a single quote `'` | It would end the quoted value |
| A value containing a line break | One variable is one line |
| A value starting or ending with whitespace | Parsers disagree about trimming it |
| A key not matching `[A-Za-z_][A-Za-z0-9_]*` | Not a valid variable name |
| At `import` and `edit`, a `$` other than `$$` in an unquoted or double-quoted value | Compose would have interpolated it; single-quote the value, or write `$$` |

Nothing is stored when one value in a command or edit is refused. For a password, use letters and
digits (`openssl rand -hex 16`), or any characters but `'`.

## Moving to the store

On the host, once, for the files of [setup.md](setup.md) steps 9 to 11 or an existing installation.
Take a copy of `/etc/yggdrasil` first (`tar czf`), as for any upgrade.

1. **Create the store.** `scripts/ygg.sh vars init` prints the key and waits for `saved`. Put it in
   your password manager first: without it nothing can be read again.
2. **Import the files.** `scripts/ygg.sh vars import --all` reads `platform.env` into `platform`,
   `acme.env` into `platform:acme` and every `<environment>/<application>.env` into
   `<application>@<environment>`, then renames each imported file to `*.env.imported`. Running it
   again never overwrites a stored value, and only renames the files it imported. A file whose
   name is not an application of the catalog is left alone and reported. `--dir` reads another
   secrets directory. Every file is read and checked first, and one refused line (below) imports
   nothing.

   Values are stored as Compose read them: `PASSWORD=ab$$cd` (or `"ab$$cd"`) is stored as `ab$cd`,
   and a single-quoted `'ab$$cd'` as it is. A `$` that Compose would have replaced with another
   variable (`PASSWORD=ab$OTHER`, `${OTHER}`) is refused, naming the file, the line and the key:
   write `$$` for a literal `$`, or the value Compose actually used, and import again.
3. **Move shared values up.** After importing, it offers (`y/N` per key, or `--move-up yes|no` for
   all) to define a value once:
   - first in the **environment** layer: a key with the same value in every application imported
     for that environment, unless an application already has an `<application>` value for it;
   - then in the **application** layer: a key with the same value in every environment imported
     for that application.

   Only values identical in every scope imported in that run are offered. Each move is one
   transaction (set above, unset below) and keeps the secret flag. Typically `DB_HOST`, `DB_PORT`
   and a locale go to `@<environment>`.
4. **Use references** where applications share a secret, e.g. fortuna-api's
   `FORTUNA_AUTH_TOKEN_SECRET` as `${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}` in each
   environment ([References](#references)).
5. **Check.** `scripts/ygg.sh vars check`. Compare what the platform will read with the file you
   imported: `scripts/ygg.sh vars export platform | diff - /etc/yggdrasil/platform.env.imported`
   (the quoting may differ; the values must not).
6. **Restart the platform** to read it from the store: `scripts/platform.sh up`.
7. **Redeploy one application** (`scripts/ygg.sh config <application> <environment>`, then
   apply, or a Jenkins build) and check it is healthy. Others follow at their next deploy.
8. **Delete the `*.env.imported` files** when you are satisfied. They hold the secrets in clear.

After step 2 the store is the only source; the `*.env.imported` files are ignored.

**On a Windows workstation** (the `local` environment), the same with the store inside your
checkout:

```bash
export YGG_SECRETS_DIR=$PWD/env
scripts/ygg.sh vars init
scripts/ygg.sh vars import --all
```

NTFS does not enforce the `0640` mode, so the files protect nothing against other accounts of that
machine. The repository's `.gitignore` keeps `env/vars.db`, `env/vars.key`, `env/locks/`, the
`*.env.imported` files and `vars-*.db` / `vars-*.key` backups out of git, but a file outside `env/`
is yours to protect.

## Backup and recovery

**Back up** the database and its key together, to a directory only root reads:

```bash
scripts/ygg.sh vars backup /root/yggdrasil-backups
```

It writes `vars-<UTC timestamp>.db` (a consistent copy, taken while the store is in use) and
`vars-<UTC timestamp>.key`, both `0600`. A weekly cron line, in `/etc/cron.d/yggdrasil-vars`:

```text
0 3 * * 0 root /opt/yggdrasil/scripts/ygg.sh vars backup /root/yggdrasil-backups
```

Nothing installs it for you. A backup next to the key is as sensitive as the store: keep copies
off the machine only where both are protected, and the key's own copy in your password manager.

**Check** at any time with `scripts/ygg.sh vars check`. A wrong or missing key is found when the
store is opened: every command stops with `wrong or missing key (<dir>/vars.key)`.

**The store is unusable and the platform must start.** The last successful `platform.sh up` from
the store saved its rendered files in `<secrets>/last-good/` (`root`, `0600`). Start from them,
without opening the store:

```bash
scripts/platform.sh up --last-good
```

Every `platform.sh` command takes `--last-good` right after it, to read the platform without the
store: `platform.sh down --last-good`, `ps --last-good`, `logs --last-good <service>`,
`config --last-good`.

`ygg.sh` stops with a clear message, carrying the check's errors, when the store can't be used
(`vars check --usable`: a lost or wrong key, a damaged file), and points to this.

**Restore a backup.** Stop writing, copy both files back, and check:

```bash
install -m 640 -g docker /root/yggdrasil-backups/vars-<timestamp>.db  /etc/yggdrasil/vars.db
install -m 640 -g docker /root/yggdrasil-backups/vars-<timestamp>.key /etc/yggdrasil/vars.key
scripts/ygg.sh vars check
```

**Lost key.** The values cannot be decrypted by anyone: restoring the key from your password
manager is the only fix. If it is gone and no backup has it, every secret must be entered again:
move `vars.db` and `vars.key` aside, `vars init`, and set the variables from their sources (the
`last-good` files hold the platform's values in clear, and a running container shows its own
environment with `docker inspect`).

**Leave the store.** `scripts/ygg.sh vars export <scope> [--resolved] > file` writes any scope as
an env file, to use files again: delete `vars.db` and the scripts read the files as before.

## How deploys use it

- **`deploy.sh`**, when `vars.db` exists: runs `vars check <application> <environment>`, then
  renders the application's resolved variables as `KEY='value'` lines into a private directory
  (`/run/yggdrasil` when writable, else `${TMPDIR:-/tmp}/yggdrasil-$UID`; base directory `0700`, the file `0600`).
  `APP_ENV_FILE` and Compose's `--env-file` point at it, and the directory is removed when the script
  exits, whether the deploy succeeded or not. An application with no variables at all in that
  environment is not deployed, as a missing env file was not: `deploy.sh` stops and names
  `scripts/ygg.sh config <application> <environment>`. Without `vars.db` it reads
  `<secrets>/<environment>/<application>.env` with a one-line notice pointing here.
- **The deploy lock** is a file per application and environment,
  `<secrets>/locks/<application>-<environment>.lock`, created `0664` when missing and opened
  read-only. `platform.sh up` creates the `locks` directory (mode `2770`, group `docker`) and the
  Jenkins agent mounts it read-write, so a hand deploy and the agent's wait for each other.
- **`platform.sh up`**, when `vars.db` exists: checks the platform's values (`vars check --platform`:
  an application's broken reference does not stop the platform), renders `platform.env` and
  `acme.env` the same way and hands them to Compose (Traefik's `env_file` follows
  `YGG_ACME_ENV_FILE`), then saves them in `<secrets>/last-good/`.
- **The Jenkins agent** mounts the secrets directory read-only: it opens the database read-only,
  reads the key, renders and deploys. It never writes the store. Upgrade the platform once
  (`platform.sh up`) so the agent has the `locks` mount and `python3-cryptography`.
- **Changes reach applications at their next deploy**: containers read their variables when
  created. `ygg.sh config` offers to redeploy after an edit.
