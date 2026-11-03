#!/usr/bin/env python3
"""Copies paths of a repository into another catalog repository, the `sync` section of its yggdrasil.yml.

    python3 scripts/sync.py gather <spec.json> <app-dir> <out-dir>
    python3 scripts/sync.py message <source-repo> <app-dir> <spec.json>
    python3 scripts/sync.py push <out-dir> <target-url> <message-file>

Each command exits 0, or 1 with `sync: <message>` on stderr. The spec is the validated section as a JSON file,
{"repository": str, "paths": [{"from": str, "to": str}]} (pipeline_config.py checked the paths: relative, no
`..` and no `.git` part, none inside another). A `from` is a file or a directory of <app-dir>; a `to` is where
it lands in the target, which is always its `main` branch. Nothing outside the listed `to` paths is touched,
and each is replaced wholesale: a file deleted at the source disappears from the target.

gather copies every `from` to <out-dir>/<i> (i: the path's position in the spec) and writes
<out-dir>/manifest.json, {"paths": [...]}, the spec's paths in order. It runs where the repository's own code
runs (the CI agent), so it checks everything before copying anything: a `from` that is missing, that holds
no file, that holds a path named .git (a nested repository cannot be replicated: git would store a bare
gitlink), or that is or sits under a symbolic link (a link could name a file outside the checkout, such as a
credential) fails the build.

message prints the commit message: `docs: sync <source-repo> <version>`, where the version is `v<x.y.z>` when
the subject of <app-dir>'s HEAD is a release merge (`release: vX.Y.Z ...`, what mergeAndTag gives) and the
7-character commit otherwise, then a blank line, a `- <from> -> <to>` line per path of the spec (a manifest
works too) and `Source: <source-repo>@<full commit>`.

push runs where the forge's write token is: the publish agent, which holds nothing else the repository's code
could reach and runs no command the repository names, only this script and git. <out-dir> arrives as a stash
and is not trusted: push uses the manifest's entries only (so a message.txt beside them is never copied),
rechecks them (a `to` that is not a plain relative path, or a link or a .git in what was staged, is refused) and never
writes outside its clone, nor through a link of the target (a `to` under one is refused). It clones the
target's `main` (depth 1), replaces each `to` with its staged copy, commits what changed (ignored files too:
a `to` is replaced wholesale) with the message of <message-file> and pushes. Nothing to commit is success,
and says so. A transfer under 1000 bytes a second for a minute gives up. A push the target rejects (main
moved since the clone) starts again from a fresh clone, 3 attempts in all. The author and the committer are
$FORGE_USER <$FORGE_USER@noreply.<host of $FORGE_URL>>.

The token reaches git as git's own credential helper reads it, from $FORGE_USER and $FORGE_TOKEN in the
environment: never an argument, the URL or the output. Needs git; stdlib only.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

ATTEMPTS = 3
# A `to` as pipeline_config.py accepts it: relative, `/`-separated, plain names. Rechecked by push, since the
# manifest crosses from the agent that runs the repository's code.
PATH = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
# The subject mergeAndTag gives a release merge.
RELEASE = re.compile(r"release: v(\d+\.\d+\.\d+)\b")
# git's credential helper, as checkoutTools() installs it: answers `get` from the environment.
HELPER = '!f() { test "$1" = get || exit 0; echo "username=$FORGE_USER"; echo "password=$FORGE_TOKEN"; }; f'
# Settings a clone of the target must not take from the machine: line endings or signing would change what
# is committed.
GIT_SETTINGS = ["-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "core.hooksPath="]
# git has no network timeout: a stalled clone or push would hold the publish agent up to the build's limit.
# Under 1000 bytes a second for a minute, it gives up (a clone fails the build, a push is retried).
STALL_SETTINGS = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"]


class SyncError(Exception):
    """Something the sync cannot go on with; str() is the message after `sync: `."""


class Rejected(SyncError):
    """The target refused the push (main moved, or the push was refused for another reason)."""


# ---- The paths ----


def read_paths(file):
    """The `paths` of a spec or manifest file: a list of (from, to), each a string."""
    try:
        data = json.loads(pathlib.Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SyncError(f"cannot read {file}: {error}") from error
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list) or not paths:
        raise SyncError(f"{file}: needs a non-empty list of paths")
    result = []
    for item in paths:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("from", "to")):
            raise SyncError(f"{file}: every path needs a string `from` and `to`")
        result.append((item["from"], item["to"]))
    return result


def check_relative(role, path):
    """`path` is a plain relative path: no `..`, no `.`, no `.git` part, nothing but names."""
    parts = path.split("/")
    if not PATH.fullmatch(path) or any(part in (".", "..") or part.lower() == ".git" for part in parts):
        raise SyncError(f"`{role}` {path!r} is not a plain relative path")


def links_in(path):
    """Every symbolic link at `path` or under it, as pathlib paths."""
    if path.is_symlink():
        return [path]
    found = []
    if path.is_dir():
        for directory, names, files in os.walk(path):
            found += [pathlib.Path(directory, name) for name in names + files if pathlib.Path(directory, name).is_symlink()]
    return found


def git_data_in(path):
    """Every file or directory named .git (any case) under `path`: a nested repository would be committed as a
    bare gitlink, its files never reaching the target, and a .git file is dropped by git add."""
    found = []
    if path.is_dir() and not path.is_symlink():
        for directory, names, files in os.walk(path):
            found += [pathlib.Path(directory, name) for name in names + files if name.lower() == ".git"]
    return found


def holds_a_file(path):
    return path.is_file() or any(files for _, _, files in os.walk(path))


# ---- gather ----


def gather(spec, app, out):
    paths = read_paths(spec)
    app, out = pathlib.Path(app), pathlib.Path(out)
    sources = []
    for source, _ in paths:
        check_relative("from", source)
        # A link anywhere on the way (out -> /etc, out/report -> a file) would name something else.
        here = app
        for part in source.split("/"):
            here = here / part
            if here.is_symlink():
                raise SyncError(f"{source} is or sits under a link: {here.relative_to(app).as_posix()}")
        if not here.exists():
            raise SyncError(f"{source} does not exist in the checkout")
        inside = links_in(here)
        if inside:
            raise SyncError(f"{source} holds a link: {inside[0].relative_to(app).as_posix()}")
        # Nor can a nested repository be replicated: git add would store it as a gitlink.
        nested = git_data_in(here)
        if nested:
            raise SyncError(f"{source} holds git data: {nested[0].relative_to(app).as_posix()}")
        # An empty `from` would delete the whole `to` in the target, a build that produced nothing.
        if not holds_a_file(here):
            raise SyncError(f"{source} holds no file")
        sources.append(here)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.json"
    manifest.unlink(missing_ok=True)
    for index, here in enumerate(sources):
        stage(here, out / str(index))
    manifest.write_text(json.dumps({"paths": [{"from": a, "to": b} for a, b in paths]}) + "\n", encoding="utf-8")


def stage(source, destination):
    """Copies a file or a directory to `destination`, replacing what is there. A link that appeared after
    the check is copied as a link, never followed."""
    remove(destination)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


# ---- message ----


def git_output(directory, *args):
    result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SyncError(f"git {args[0]} failed in {directory}: {result.stderr.strip()}")
    return result.stdout.strip()


def message(repository, app, paths_file):
    full = git_output(app, "rev-parse", "HEAD")
    release = RELEASE.match(git_output(app, "log", "-1", "--format=%s", "HEAD"))
    version = f"v{release.group(1)}" if release else full[:7]
    lines = [f"docs: sync {repository} {version}", ""]
    lines += [f"- {source} -> {target}" for source, target in read_paths(paths_file)] + [""]
    lines.append(f"Source: {repository}@{full}")
    return "\n".join(lines) + "\n"


# ---- push ----


def git_environment():
    user = os.environ.get("FORGE_USER") or "yggdrasil"
    host = urllib.parse.urlparse(os.environ.get("FORGE_URL", "")).hostname or "localhost"
    email = f"{user}@noreply.{host}"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = user
        env[f"GIT_{role}_EMAIL"] = email
    return env


def transfer_settings():
    """The settings of a clone or a push: the stall limit, and the credential helper when there is a token."""
    # An empty helper first drops the machine's own, so only ours answers.
    if os.environ.get("FORGE_USER") and os.environ.get("FORGE_TOKEN"):
        return [*STALL_SETTINGS, "-c", "credential.helper=", "-c", f"credential.helper={HELPER}"]
    return list(STALL_SETTINGS)


def scrub(text):
    token = os.environ.get("FORGE_TOKEN")
    return text.replace(token, "***") if token else text


def git(clone, env, *args, settings=(), check=True):
    result = subprocess.run(
        ["git", *GIT_SETTINGS, *settings, "--literal-pathspecs", *args],
        cwd=clone, env=env, capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise SyncError(scrub(f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"))
    return result


def push_head(clone, env):
    """Pushes the clone's HEAD to the target's main; Rejected when the target says no."""
    result = git(clone, env, "push", "--quiet", "origin", "HEAD:main", settings=transfer_settings(), check=False)
    if result.returncode != 0:
        raise Rejected(scrub(f"git push failed: {result.stderr.strip() or result.stdout.strip()}"))


def replace_in_clone(clone, target, staged):
    """Replaces <clone>/<target> by the staged file or directory, never writing outside the clone or into its
    .git. The last part is replaced, not followed."""
    check_parents(clone, target)
    destination = clone / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    check_parents(clone, target)
    stage(staged, destination)


def check_parents(clone, target):
    """The parents of `target` in the clone are the directories they name: a link among them (the target
    repository's own, such as `static -> .git`, which would have `static/config` replace the clone's
    .git/config) is refused, not followed."""
    root = clone.resolve()
    parents = target.split("/")[:-1]
    resolved = (clone / target).parent.resolve()
    if os.path.normcase(resolved) != os.path.normcase(root.joinpath(*parents)):
        here, link = clone, "/".join(parents)
        for part in parents:
            here = here / part
            if here.is_symlink():
                link = here.relative_to(clone).as_posix()
                break
        raise SyncError(f"`to` {target} sits under a link of the target: {link}")
    if parents and parents[0].lower() == ".git":
        raise SyncError(f"`to` {target} would write into the clone's .git")


def attempt(entries, out, url, message_text, env):
    """One try on a fresh clone; the pushed commit, or None when nothing changed."""
    with tempfile.TemporaryDirectory(prefix="sync-") as parent:
        clone = pathlib.Path(parent) / "clone"
        result = subprocess.run(
            ["git", *GIT_SETTINGS, *transfer_settings(),
             "clone", "--quiet", "--depth", "1", "--branch", "main", "--", url, str(clone)],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SyncError(scrub(f"cannot clone the target's main: {result.stderr.strip()}"))
        targets = []
        for index, (_, target) in enumerate(entries):
            replace_in_clone(clone, target, out / str(index))
            targets.append(target)
        # -f: a .gitignore (the target's, or one copied in) keeps nothing of a `to` out.
        git(clone, env, "add", "-A", "-f", "--", *targets)
        if git(clone, env, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        message_file = pathlib.Path(parent) / "message"
        message_file.write_text(message_text, encoding="utf-8")
        git(clone, env, "commit", "--quiet", "--cleanup=verbatim", "-F", str(message_file))
        commit = git(clone, env, "rev-parse", "HEAD").stdout.strip()
        push_head(clone, env)
        return commit


def push(out, url, message_file):
    out = pathlib.Path(out)
    entries = read_paths(out / "manifest.json")
    try:
        message_text = pathlib.Path(message_file).read_text(encoding="utf-8")
    except OSError as error:
        raise SyncError(f"cannot read the commit message {message_file}: {error}") from error
    if not message_text.strip():
        raise SyncError(f"the commit message {message_file} is empty")
    # Everything is checked before the target is touched.
    for index, (_, target) in enumerate(entries):
        check_relative("to", target)
        staged = out / str(index)
        if not staged.exists() and not staged.is_symlink():
            raise SyncError(f"the staged copy for {target} is missing ({index})")
        inside = links_in(staged)
        if inside:
            raise SyncError(f"the staged copy for {target} holds a link: {inside[0].relative_to(out).as_posix()}")
        nested = git_data_in(staged)
        if nested:
            raise SyncError(f"the staged copy for {target} holds git data: {nested[0].relative_to(out).as_posix()}")
        if not holds_a_file(staged):
            raise SyncError(f"the staged copy for {target} holds no file")
    env = git_environment()
    problem = None
    for _ in range(ATTEMPTS):
        try:
            commit = attempt(entries, out, url, message_text, env)
        except Rejected as rejected:
            problem = rejected
            continue
        return f"sync: pushed {commit}" if commit else "sync: nothing to change"
    raise SyncError(f"the push failed after {ATTEMPTS} attempts: {problem}")


# ---- The command line ----

USAGE = (
    "usage: sync.py gather <spec.json> <app-dir> <out-dir> | message <source-repo> <app-dir> <spec.json>"
    " | push <out-dir> <target-url> <message-file>"
)


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else (None, [])
    try:
        if command == "gather" and len(args) == 3:
            gather(*args)
        elif command == "message" and len(args) == 3:
            sys.stdout.write(message(*args))
        elif command == "push" and len(args) == 3:
            print(push(*args))
        else:
            raise SyncError(USAGE)
    except SyncError as error:
        print(f"sync: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"sync: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
