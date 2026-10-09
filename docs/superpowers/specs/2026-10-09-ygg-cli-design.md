# `ygg`: one command line app for the host

Status: approved design, 2026-10-09. Target release: yggdrasil 0.7.0.

## Goal

Turn the host helper (`scripts/ygg.sh` and the scripts it drives) into a command line app that feels like one:

1. **Parity**: everything that can be done by calling a host script with parameters can be done from the interactive
   menu, and the other way around. The host scripts are `ygg.sh`, `vars.py`, `platform.sh`, `deploy.sh` and the read
   commands of `catalog.py`.
2. **Arrow-key menus** with breadcrumbs, filtering and help, instead of numbered choices.
3. **Type or pick**: every prompt offers a list to pick from and accepts typed text; every command keeps working with
   its arguments on the command line.
4. **An installed `ygg` command**, with `ygg --version`.
5. **Tab completion** for commands, options, applications, environments, scopes and keys.
6. **Variables and secrets per application**: pick an application and an environment, see its variables as a list,
   select one and show (reveal), change, remove or roll it back. Reading a secret from the menu is what started this.

Non-goals: `github.sh` (repository rules, run from a workstation) and `catalog.py add-application` (internal: `ygg add`
is its interface); Windows outside WSL; a remote or web interface (the console stays read-only); new Python packages.

## Architecture

```
ygg  (/usr/local/bin/ygg → <checkout>/scripts/ygg)
 └─ scripts/ygg.py   the command tree: grammar, menus, prompts, completion, help
     ├─ check · install · add · status · config [<app>] [<env>]   → scripts/host.sh
     ├─ env status | start <env> | stop <env> [--force]            → scripts/host.sh
     ├─ vars …         vars.py's own parser, mounted as is
     ├─ platform up | down | ps | config | logs [<service>]  [--last-good] → scripts/platform.sh
     ├─ deploy <env> <app> [<app-dir>] [<version>]                 → scripts/deploy.sh
     ├─ catalog validate | environments [<app>] | applications [--deployable] | systems | show <app>
     │          | plan <app> | environment <env> [<option>] | get <app> <env> <option>   → scripts/catalog.py
     └─ version · help [<command>] · completion bash · self-install
scripts/ygg.sh       exec python3 ygg.py "$@"   (every existing invocation keeps working)
scripts/ygg          the launcher /usr/local/bin/ygg links to (resolves its own symlink, runs ygg.py)
```

- **`scripts/ygg.py`** (Python 3.10+, standard library plus the PyYAML and `cryptography` already required) owns the
  interface and no host work. Each command runs what does the work today: a `scripts/host.sh` operation, `vars.py`
  (imported, so its parser and errors are the same objects), `platform.sh`, `deploy.sh` or `catalog.py`.
- **`scripts/host.sh`** is today's `ygg.sh` body: check, install, add, status, config and env, unchanged in behavior,
  minus the menu and the `vars` passthrough (both move to `ygg.py`). It is called as `host.sh <operation> [args]`.
- **One command tree.** Every command is declared once, as an argparse parser, with two pieces of metadata the menu
  needs: where it sits in the menu, and, for each argument, where its pick list comes from (below). The CLI grammar,
  the menu, the completion and the help are all read from that tree. `vars` mounts `vars.parser()`, so a new `vars`
  option appears in the menu and the completion without a change to `ygg.py` (and the parity test fails until its
  argument has a pick list, when it needs one).
- **Shared prompts.** The bash prompts of `host.sh` (`choose`, `ask`, `ask_match`, `confirm`) call
  `ygg.py prompt choose|ask|confirm …` when stdin and stdout are a terminal, so the add wizard and the environment
  screens get the same picker as the rest. The picker draws on `/dev/tty` and writes the answer to file descriptor 3
  (`answer=$(python3 ygg.py prompt … 3>&1 1>/dev/tty)`); without a terminal the bash prompts stay as they are.

## The menu

`ygg` with no arguments, in a terminal at least 60×12, opens a full-screen menu (curses). In a smaller terminal, or
without one, it falls back to numbered prompts, as today.

```
 yggdrasil › Variables and secrets › get                        prod-vps · 0.7.0
 ─────────────────────────────────────────────────────────────────────────────
   Scope   heimdall-api@production
 › Key     ▏HEIMDALL_MAS                                    ← type to filter
           HEIMDALL_MASTER_USER_EMAIL
         ▸ HEIMDALL_MASTER_USER_PASSWORD   (secret)
   [x] --reveal  print a secret in clear
 ─────────────────────────────────────────────────────────────────────────────
  ygg vars get heimdall-api@production HEIMDALL_MASTER_USER_PASSWORD --reveal
  ↑↓ move · Enter choose · Esc back · ? help · q quit
```

- **Header**: breadcrumbs, host name, version. **Footer**: the equivalent command line, kept up to date as the form
  fills, and the keys of the current screen.
- **Top level**, by task, each entry with the command's one-line summary: Status · Applications (variables, deploy,
  add) · Environments · Variables and secrets · Platform · Host (check, install) · Catalog · Help and version.
- **Keys**: ↑↓ move; Enter or → choose or open; Esc or ← back; typing filters the list; Backspace edits the filter; `?`
  shows the command's full `--help`; `q` quits (outside a text field); Ctrl-C cancels the running command and returns
  to the menu.

### Forms

Every command of the tree is reachable from the menu as a form generated from its parser:

- **Each positional argument is a picker** fed by its pick list: applications and environments (the catalog and this
  host's `ENVIRONMENTS`), scopes (`platform`, `platform:acme`, `@<env>`, `<app>`, `<app>@<env>`), the keys of the scope
  already chosen (secrets marked), history entries (id, time, scope, key), compose services, directories (with the
  usual default). **Type or pick**: typing filters; text that matches nothing is used as typed (a new key name, a path).
- **Each option**: a flag (`--reveal`, `--force`, `--resolved`) is a checkbox; an option with choices (`--move-up
  ask|yes|no`) is a choice; a value (`--limit`, `--dir`) is a text field. Defaults are the command line's.
- **Mutually exclusive options** (argparse groups, e.g. `--secret`/`--no-secret`, `--platform`/`--usable`) are one
  choice with "neither".
- **Running**: the menu leaves full-screen mode and runs the command in the normal terminal (so `$EDITOR`, hidden
  input and long output work), then waits for Enter unless the command printed nothing. A failing command shows its
  error and exit code; the menu stays.
- **Confirmation**, default no, before: `vars unset`, `vars rollback`, `vars import --replace`, `env stop`,
  `platform down`, and `deploy` to an environment that is not `onDemand`.

### Variables screens

**Applications › `<app>` › `<environment>`** (and **Variables and secrets › `<scope>`** for any scope) is a list, not
a form:

```
 yggdrasil › Applications › heimdall-api › production          prod-vps · 0.7.0
 ─────────────────────────────────────────────────────────────────────────────
   ▏                                                ← type to filter
   HEIMDALL_CORS_ALLOWED_ORIGINS   https://heimdall.…          heimdall-api@production
 ▸ HEIMDALL_MASTER_USER_PASSWORD   ••••••••  secret            heimdall-api@production
   POSTGRES_HOST                   postgres                    @production
   …
   + Add a variable
 ─────────────────────────────────────────────────────────────────────────────
  Enter: actions · r reveal/hide all · e edit in $EDITOR · h history · d deploy · Esc back
```

- The rows are the resolved variables (`vars list --resolved`): value (secrets masked), and the scope it comes from.
- **Selecting a variable** opens its actions:

  | Action | Does |
  |---|---|
  | Show value | Reveals it on screen until a key is pressed, then masks it again; it is drawn in the curses screen, not printed, so it stays out of the scrollback |
  | Change value | Hidden input for a secret, the current value pre-filled otherwise; asks which scope to write: this application in this environment (the default), the application in every environment, or the environment's shared scope |
  | Mark as secret / not secret | Toggles the flag (`vars set --secret / --no-secret` with the same value) |
  | Remove | `vars unset` in the scope the value comes from, after a confirmation (default no) |
  | History | This variable's changes; selecting one offers to roll it back (`vars rollback`, confirmed) |
  | Copy command | Prints the `ygg vars …` command line for this variable |

- A value inherited from a shared scope (`@production`, `heimdall-api`) says so; changing it asks whether to change
  the shared value or override it for this application only.
- **+ Add a variable**: name (typed), secret or not (from the name, as `vars set` decides, changeable), value (hidden
  for a secret), scope (as Change value).
- `e` runs `vars edit` on the scope; `h` the scope's history; `d` (shown once something changed) deploys the
  application in that environment, as today's Apply.
- On a machine without a variables store, the screen reads and writes the env file, as `config` does today; the
  actions that need the store (history, scope choice, secret flag) are not offered.
- When the store cannot be opened (no key, wrong key), the Variables screens say why and offer `vars check --usable`
  and the docs, instead of failing.

## `ygg` command, completion, version, help

- **`ygg self-install`** links `/usr/local/bin/ygg` to the checkout's `scripts/ygg` and writes the completion to
  `/etc/bash_completion.d/ygg` (with `sudo` when not root); `ygg install` runs it too. It reports what it changed and
  is safe to run again. The installed `ygg` runs the checkout that installed it (`/opt/yggdrasil` on the VPS).
- **`ygg completion bash`** prints the completion script; it calls `ygg __complete <cword> <words…>`, which completes
  from the same tree as the menu: commands, options, choices, and the pick lists (applications, environments, scopes,
  keys of the scope typed before). A pick list that cannot be read (no catalog, no access to the store's key) offers
  nothing, silently.
- **`ygg --version` / `ygg version`**: `yggdrasil <version> (<commit>) at <checkout>`, from `git describe --tags`, else
  the newest released version in `CHANGELOG.md`.
- **`ygg help`**: the top-level overview (commands by task, as the menu groups them, with their summaries);
  `ygg help <command…>` and `ygg <command…> --help`: that command's full help; `?` in the menu shows the same text.
- Exit status: 0 success, 1 a command failed, 2 a usage error (as `vars.py`).

## Compatibility

- `scripts/ygg.sh <anything>` keeps working, with the same output and exit status: `check`, `install`, `add`,
  `status`, `config [<app>] [<env>]`, `env …`, `vars …`, `help`. `scripts/ygg.sh` with no arguments opens the new menu.
- `deploy.sh`, `platform.sh`, `vars.py` and `catalog.py` keep their own command lines (Jenkins and the docs call
  them directly).
- Environment variables keep their meaning: `YGG_SECRETS_DIR`, `YGG_APPS_DIR`, `YGG_ENVIRONMENT`, `EDITOR`.

## Testing

- **Parity** (`test_ygg_cli.py`): walks the tree and fails when a command has no menu placement, or a positional
  argument has neither a pick list nor an explicit "typed" marker; it reads the usage lines of `platform.sh`,
  `deploy.sh` and `catalog.py` and fails when a command of theirs (except `add-application`) is not in the tree.
- **Menu logic**: the screens are state machines (state + key → new state + effect), separate from drawing; tests
  drive them with key sequences: navigation, filtering, type-or-pick, forms producing the expected argv, the variables
  screen (show value, change with scope choice, override of an inherited value, remove with confirmation, history then
  rollback), confirmations defaulting to no.
- **Fallback**: numbered-prompt runs with stdin scripts against a scratch store and catalog, end to end.
- **Prompts from bash**: `ygg.py prompt` returns the answer on fd 3 and falls back without a terminal.
- **Completion**: `__complete` for each word position, including keys of a scratch store.
- **Existing suites** (`test_ygg.py` through `scripts/ygg.sh`, `test_vars.py`, `test_deploy.py`, `test_platform.py`,
  `test_catalog.py`) pass unchanged.

## Documentation

- `docs/cli.md`: rewritten around `ygg`: installing it, the menu (screens, keys, the variables screens and their
  actions), completion, every command with its menu path (a command ↔ menu table).
- `README.md`: quick start with `ygg`.
- `docs/variables.md`: recipes show the `ygg vars …` command and the menu path.
- `docs/setup.md`: `ygg self-install` in the host setup.
- `CHANGELOG.md`: 0.7.0 entry with upgrade notes ("run `scripts/ygg.sh self-install` once").

## Rollout

`feature/ygg-cli` → PR into `develop` → release 0.7.0 (changelog PR, `release/0.7.0` into `main`, GitHub release).
On the VPS: update `/opt/yggdrasil` to v0.7.0 and run `scripts/ygg.sh self-install`. No container is touched.
