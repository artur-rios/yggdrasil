"""The ci agent's package caches: what a repository's checks download (pub, Gradle, NuGet, npm, apt's packages, the
Android NDK), kept between its checks. Called by scripts/run-check.sh (--toolchains with --scope), in the agent:

    python3 scripts/package_cache.py snapshot <cache> <scope>          prints the snapshot a check starts from
    python3 scripts/package_cache.py promote <cache> <scope> <run>     keeps what a passing check downloaded

Why a cache per repository, written only by trusted builds. Unlike a pinned SDK (scripts/toolchains.sh), these are
written by the tools themselves, inside the check, so they are as trustworthy as the check that wrote them: pub and
Gradle never check a cached package again, so a check could leave one that a later check runs. So:

- a cache belongs to one repository (<scope>, the catalog id): nothing a check of another repository did reaches it;
- every check gets its repository's current snapshot as an overlay (scripts/run-check.sh), so what it writes stays in
  its own upper directory and never touches the snapshot;
- only a check that passed on a push to develop, main or master (`promote`, which the pipeline library asks for only
  then) has its upper directory kept, in a new snapshot that becomes the current one. A pull request (Renovate's
  included) never writes the cache, not even its own repository's: what lands there is what was merged.

A release never uses any of it (the release agent has no cache).

The layout, under <cache>/packages/<scope>/:

    current -> snapshots/<id>     the snapshot new checks start from (a symbolic link, replaced atomically)
    snapshots/<id>/               a snapshot: pub/ gradle/ nuget/ npm/ apt/archives/ android-ndk/. Never written once
                                  it is there: a new one is made of hard links to the last one plus what the check added
    snapshots/<id>.born           when its lineage started (seconds since the epoch); after LINEAGE_DAYS the lineage is
                                  dropped and the next check starts from nothing, so what nothing uses any more goes
    snapshots/<id>.retired        when it stopped being current; removed RETIRED_HOURS later (a check may still use it)
    snapshots/.incoming-<id>/     a snapshot being made
    .lock, .used                  the scope's lock; touched at every check (a scope unused for SCOPE_DAYS is removed)

A run directory (<run>, made by run-check.sh) has packages/upper (the check's writes to /opt/yggdrasil/cache) and,
for an image with ANDROID_HOME, ndk/upper (its writes to $ANDROID_HOME/ndk). An upper directory is an overlay's: a
deleted path is a character device 0/0 (a whiteout), a directory replaced as a whole is marked opaque (an xattr).
"""

import contextlib
import fcntl
import os
import secrets
import shutil
import stat
import sys
import time

# What a snapshot holds; the container sees it at /opt/yggdrasil/cache (run-check.sh sets PUB_CACHE and the others
# to these). android-ndk is also mounted at $ANDROID_HOME/ndk on its own.
KINDS = ("pub", "gradle", "nuget", "npm", "apt/archives/partial", "android-ndk")
# Written by every run and of no use to the next: never kept, and never a reason to make a snapshot.
NOISE = ("gradle/daemon", "gradle/.tmp", "npm/_logs", "npm/_update-notifier-last-checked", "pub/active_roots",
         "apt/archives/partial", "apt/archives/lock")
LINEAGE_DAYS = 30
RETIRED_HOURS = 24
SCOPE_DAYS = 30
OPAQUE = (b"y",)
OPAQUE_XATTRS = ("trusted.overlay.opaque", "user.overlay.opaque")


def note(message):
    print(f"package_cache.py: {message}", file=sys.stderr)


def scope_dir(cache, scope):
    return os.path.join(cache, "packages", scope)


@contextlib.contextmanager
def locked(directory):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def current(directory):
    """The current snapshot's path, or None."""
    link = os.path.join(directory, "current")
    if not os.path.islink(link):
        return None
    target = os.path.join(directory, os.readlink(link))
    return target if os.path.isdir(target) else None


def born(snapshot):
    try:
        with open(f"{snapshot}.born") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return 0.0


def touch(path):
    with open(path, "a"):
        pass
    os.utime(path, None)


def is_whiteout(info):
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == 0


def is_opaque(path):
    for name in OPAQUE_XATTRS:
        try:
            if os.getxattr(path, name, follow_symlinks=False) in OPAQUE:
                return True
        except OSError:
            continue
    return False


def is_noise(relative, prefix=""):
    path = os.path.join(prefix, relative) if prefix else relative
    return any(path == noise or path.startswith(noise + "/") for noise in NOISE)


def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


def link_file(source, destination):
    """destination as a hard link to source (a copy where a link can't be made): never a write into an existing file."""
    remove(destination)
    if os.path.islink(source):
        os.symlink(os.readlink(source), destination)
        return
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError:
        shutil.copy2(source, destination, follow_symlinks=False)


def link_tree(source, destination):
    """destination: a tree of hard links to source's files (directories made anew, with their modes)."""
    os.makedirs(destination, exist_ok=True)
    for root, directories, files in os.walk(source):
        relative = os.path.relpath(root, source)
        target = destination if relative == "." else os.path.join(destination, relative)
        for name in list(directories):
            path = os.path.join(root, name)
            if os.path.islink(path):
                directories.remove(name)
                files.append(name)
                continue
            os.makedirs(os.path.join(target, name), exist_ok=True)
            os.chmod(os.path.join(target, name), stat.S_IMODE(os.lstat(path).st_mode))
        for name in files:
            path = os.path.join(root, name)
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                link_file(path, os.path.join(target, name))


def walk_upper(upper, prefix=""):
    """(relative path, absolute path, lstat) of every entry of an overlay's upper directory, parents first, but the
    NOISE (relative to the snapshot: `prefix` is where this upper directory is in it)."""
    for root, directories, files in os.walk(upper):
        relative_root = os.path.relpath(root, upper)
        for name in sorted(directories + files):
            relative = name if relative_root == "." else os.path.join(relative_root, name)
            path = os.path.join(root, name)
            if is_noise(relative, prefix):
                if name in directories:
                    directories.remove(name)
                continue
            info = os.lstat(path)
            if name in directories and stat.S_ISLNK(info.st_mode):
                directories.remove(name)
            yield relative, path, info


def adds_something(upper, snapshot, prefix=""):
    """Whether the upper directory holds a file or a link the snapshot has not got. A file the check only rewrote (a
    lock, an index) is not worth a new snapshot; one it downloaded is."""
    if not os.path.isdir(upper):
        return False
    base = os.path.join(snapshot, prefix) if snapshot and prefix else snapshot
    for relative, _, info in walk_upper(upper, prefix):
        if stat.S_ISDIR(info.st_mode) or is_whiteout(info):
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            continue
        if base is None or not os.path.lexists(os.path.join(base, relative)):
            return True
    return False


def apply_upper(upper, destination, prefix=""):
    """What the check saw at its end, in destination (a tree of links to the snapshot it started from, or the newer
    current one): its deletions (whiteouts) and replaced directories (opaque) applied, its files linked in."""
    if not os.path.isdir(upper):
        return
    for relative, path, info in walk_upper(upper, prefix):
        target = os.path.join(destination, relative)
        if is_whiteout(info):
            remove(target)
        elif stat.S_ISDIR(info.st_mode):
            if os.path.lexists(target) and (os.path.islink(target) or not os.path.isdir(target) or is_opaque(path)):
                remove(target)
            os.makedirs(target, exist_ok=True)
            os.chmod(target, stat.S_IMODE(info.st_mode))
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            link_file(path, target)


def make_skeleton(snapshot):
    for kind in KINDS:
        os.makedirs(os.path.join(snapshot, kind), exist_ok=True)


def install(directory, incoming, lineage_start, previous):
    """incoming becomes snapshots/<id> and the current one; the previous one is retired."""
    snapshot_id = os.path.basename(incoming)[len(".incoming-"):]
    snapshot = os.path.join(directory, "snapshots", snapshot_id)
    os.rename(incoming, snapshot)
    with open(f"{snapshot}.born", "w") as handle:
        handle.write(f"{lineage_start:.0f}\n")
    link = os.path.join(directory, f"current.{snapshot_id}")
    os.symlink(os.path.join("snapshots", snapshot_id), link)
    os.replace(link, os.path.join(directory, "current"))
    if previous:
        touch(f"{previous}.retired")
    return snapshot


def new_incoming(directory):
    snapshots = os.path.join(directory, "snapshots")
    os.makedirs(snapshots, exist_ok=True)
    incoming = os.path.join(snapshots, f".incoming-{secrets.token_hex(4)}")
    os.mkdir(incoming)
    return incoming


def gc(cache, scope):
    """Removes, with the scope's lock held: snapshots retired RETIRED_HOURS ago, snapshots half made a killed agent
    left a day ago, and other repositories' caches no check used for SCOPE_DAYS."""
    now = time.time()
    snapshots = os.path.join(scope_dir(cache, scope), "snapshots")
    for name in os.listdir(snapshots) if os.path.isdir(snapshots) else ():
        path = os.path.join(snapshots, name)
        if name.endswith(".retired") and now - os.path.getmtime(path) > RETIRED_HOURS * 3600:
            snapshot = path[:-len(".retired")]
            note(f"removing {scope}'s snapshot {os.path.basename(snapshot)}: retired for a day")
            remove(snapshot)
            for stamp in (".born", ".retired"):
                remove(snapshot + stamp)
        elif name.startswith(".incoming-") and now - os.path.getmtime(path) > 24 * 3600:
            remove(path)
    packages = os.path.join(cache, "packages")
    for name in os.listdir(packages):
        used = os.path.join(packages, name, ".used")
        if name != scope and os.path.isfile(used) and now - os.path.getmtime(used) > SCOPE_DAYS * 86400:
            note(f"removing {name}'s package cache: no check used it for {SCOPE_DAYS} days")
            remove(os.path.join(packages, name))


def snapshot(cache, scope):
    """The snapshot a check of the scope starts from: the current one, or a new, empty one."""
    directory = scope_dir(cache, scope)
    with locked(directory):
        touch(os.path.join(directory, ".used"))
        found = current(directory)
        if found:
            return found
        incoming = new_incoming(directory)
        make_skeleton(incoming)
        return install(directory, incoming, time.time(), None)


def promote(cache, scope, run):
    """Keeps what a passing check of a trusted build added: a new snapshot of the current one with the run's upper
    directories applied. Nothing when the check added no file; nothing but a fresh start when the lineage is old."""
    directory = scope_dir(cache, scope)
    uppers = [(os.path.join(run, "packages", "upper"), ""),
              (os.path.join(run, "ndk", "upper"), "android-ndk")]
    with locked(directory):
        base = current(directory)
        if not any(adds_something(upper, base, prefix) for upper, prefix in uppers):
            note(f"nothing new for {scope}'s package cache")
            return None
        if base and time.time() - born(base) > LINEAGE_DAYS * 86400:
            # What nothing uses any more stays in a lineage for ever: every LINEAGE_DAYS the next check starts over.
            note(f"{scope}'s package cache is {LINEAGE_DAYS} days old: the next check starts from an empty one")
            os.unlink(os.path.join(directory, "current"))
            touch(f"{base}.retired")
            gc(cache, scope)
            return None
        started = time.time()
        incoming = new_incoming(directory)
        try:
            if base:
                link_tree(base, incoming)
            for upper, prefix in uppers:
                apply_upper(upper, os.path.join(incoming, prefix) if prefix else incoming, prefix)
            make_skeleton(incoming)
            made = install(directory, incoming, born(base) if base else time.time(), base)
        except BaseException:
            remove(incoming)
            raise
        note(f"kept what the check added in {scope}'s package cache ({time.time() - started:.0f} s)")
        gc(cache, scope)
        return made


def main(arguments):
    usage = "usage: package_cache.py snapshot <cache> <scope> | promote <cache> <scope> <run>"
    if len(arguments) < 3 or arguments[0] not in ("snapshot", "promote") \
            or len(arguments) != (3 if arguments[0] == "snapshot" else 4):
        print(usage, file=sys.stderr)
        return 2
    command, cache, scope = arguments[:3]
    if not os.path.isabs(cache) or not os.path.isdir(cache) or scope in (".", "..") or "/" in scope or not scope:
        print(usage, file=sys.stderr)
        return 2
    if command == "snapshot":
        print(snapshot(cache, scope))
    else:
        promote(cache, scope, arguments[3])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
