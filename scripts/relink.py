#!/usr/bin/env python3
"""Rewrites a repository's links to the addresses its sites moved to. No redirects are left behind at the old
addresses, so every link in the repository has to follow.

    python3 scripts/relink.py <repo-dir> [--check]

It reads the files git tracks in <repo-dir> and changes, in each, the old addresses:

    artur-rios.github.io/<repo>[/...]    GitHub Pages: docs.artur-rios.tech/<repo>[/...] for the docs sites;
                                         lab.artur-rios.tech[/...] for personal-lab, which moves to the root
    <project>.vercel.app[/...]           Vercel: the site's new host (readme-stats.techcraftsmen.net, ...)

Each change is printed as `path:line: old -> new`. --check changes nothing and exits 1 when anything would
change or an address is unknown; without it the files are written (exit 1 too while unknown addresses are left).

Where the new address comes from. The catalog is asked first: an application with the old site's `path` (docs
sites) or id (personal-lab-site, the Vercel projects) gives `catalog.py public-host` + `public-path`: the
catalog lists every site, and it wins, so the addresses cannot drift from what is deployed. The fixed table
below applies only to a site a catalog does not have (another installation's, or a test's). YGG_CATALOG points
at another catalog, as for catalog.py.

An address this tool does not know is reported as `unknown address`, never guessed:
  - `artur-rios.github.io/<something not in the table>` and `<something>.vercel.app`;
  - the bare `artur-rios.github.io` (the profile page): whether it has a new home is the owner's decision.

Never touched: `github.com/artur-rios/...` links (they resolve through the mirror, and so does any `.../artur-rios/
<name>` path), other people's `*.github.io`, binary files, lock files (package-lock.json, pnpm-lock.yaml,
packages.lock.json, *.lock), anything git does not track (node_modules, .git), and the released sections of a
CHANGELOG.md: they are history, so only an `## [Unreleased]` section changes and the rest are reported as
`kept (released section)`; the files under docs/superpowers/ (specs and plans, which describe the move itself:
`kept (design document)`); and the placeholder `artur-rios.github.io/demo-docs` (`kept (placeholder)`). A line containing `relink:keep` is left as it is.

Files are handled as bytes, so each keeps its line endings, byte order mark and encoding.
"""

import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# A line that names an old address on purpose (a changelog-like note, a test) carries this marker.
KEEP_MARKER = b"relink:keep"
# This tool and its tests name the old addresses by design, so running it over yggdrasil leaves them alone.
SELF = {"scripts/relink.py", "scripts/test_relink.py"}

PAGES_HOST = "artur-rios.github.io"
DOCS_HOST = "docs.artur-rios.tech"
# The catalog environment whose address a site is known by.
ENVIRONMENT = "production"

# The docs sites, as the spec's "Sites" table lists them: GitHub Pages repositories that become
# docs.artur-rios.tech/<repository>. The names are the repositories (and so the old Pages paths).
# This list is the fallback. The catalog is the source: a docs application's `path` (/<repository>) says where
# its site is, and the catalog wins whenever both it and this list have an entry (a docs site only the catalog
# knows is found too).
DOCS_REPOSITORIES = [
    "heimdall-docs", "fortuna-docs", "alexandria-docs", "orpheus", "heimdall-api", "dotnet-libraries",
    "dotnet-configuration", "dotnet-data", "dotnet-extensions", "dotnet-jwt", "dotnet-logging",
    "dotnet-mediator", "dotnet-messaging", "dotnet-output", "dotnet-test-util", "dotnet-util",
    "dotnet-validation", "dotnet-webapi-util",
]

# (catalog application id, new host) of the sites that were on Pages at the root of their own path or on
# Vercel. A Pages path maps to the host's root (personal-lab served /personal-lab/, now lab.artur-rios.tech/).
PAGES_ROOT_SITES = {
    "personal-lab": ("personal-lab-site", "lab.artur-rios.tech"),
}
# Vercel's addresses are <project>.vercel.app, and only the projects found in the repositories are known: the
# others have no old address to rewrite. The key is the project's whole host.
VERCEL_SITES = {
    "github-readme-stats-six-tau-89.vercel.app": ("github-readme-stats", "readme-stats.techcraftsmen.net"),
    "website-placeholder-artur-rios.vercel.app": ("under-construction-template", "placeholder.artur-rios.tech"),
}

# Pages paths that were never a site: examples used by the docs repositories' own plan and test
# (fortuna-docs and heimdall-docs: scripts/test_check_site.py), so there is nothing to move them to. They are
# left as they are and reported once each as `kept (placeholder)`, not as unknown.
PLACEHOLDERS = {"demo-docs"}

# Files here describe the move itself (specs, plans), so the old addresses in them are history, as in a
# released CHANGELOG section: kept and reported, whether the address is known or not.
HISTORY_DIRECTORY = "docs/superpowers/"

LOCK_NAMES = {"package-lock.json", "pnpm-lock.yaml", "packages.lock.json", "yarn.lock"}

# What follows an address and is part of it, for display: a path, a query. Trailing punctuation is not.
TAIL = rb"[A-Za-z0-9._~%/+#?&=@:,-]*"
# Not preceded by a longer host or an address' own characters, nor by `<name>/`: that is a path segment, as in
# github.com/artur-rios/artur-rios.github.io (a repository, which stays), not a host.
BEFORE = rb"(?<![A-Za-z0-9._@-])(?<![^/]/)"
# `artur-rios.github.io`, then maybe a first path segment (a repository name, which may hold dots: reveal.js).
PAGES_PATTERN = re.compile(
    BEFORE + re.escape(PAGES_HOST.encode()) + rb"(?![A-Za-z0-9-])"
    rb"(?:/(?P<segment>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*))?(?P<tail>" + TAIL + rb")",
    re.IGNORECASE)
# `<project>.vercel.app`: one label.
VERCEL_PATTERN = re.compile(
    BEFORE + rb"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.vercel\.app)(?![A-Za-z0-9-])(?P<tail>" + TAIL + rb")",
    re.IGNORECASE)
# `## [Unreleased]`, `## Unreleased`, any case: the one CHANGELOG section that may change.
UNRELEASED = re.compile(rb"^##\s*\[?unreleased\]?", re.IGNORECASE)
HEADING = re.compile(rb"^##(?!#)")


class RelinkError(Exception):
    pass


class Targets:
    """Where each old address goes: the catalog's answer when it has the site, else the fixed table."""

    def __init__(self, module):
        self.module = module
        self.catalog = module.load()
        self.apps = [app for _, app in module.applications(self.catalog)]

    def _from_catalog(self, app):
        """(host, path) of an application, or None when the catalog cannot say (not deployed to production,
        no domain). The default domain is empty: an application of the catalog that is a site has its own."""
        try:
            host = self.module.public_host(self.catalog, app["id"], ENVIRONMENT, "")
        except self.module.CatalogError:
            return None
        return (host, app.get("path") or "") if host else None

    def pages(self, segment):
        """New prefix (host + path) for `artur-rios.github.io/<segment>`, or None when unknown."""
        for app in self.apps:  # the catalog first: a docs application with path /<segment>
            if app.get("path") == "/" + segment:
                found = self._from_catalog(app)
                if found:
                    return found[0] + found[1]
        if segment in DOCS_REPOSITORIES:
            return f"{DOCS_HOST}/{segment}"
        if segment in PAGES_ROOT_SITES:
            app_id, host = PAGES_ROOT_SITES[segment]
            return self._root_site(app_id, host)
        return None

    def vercel(self, host):
        entry = VERCEL_SITES.get(host.lower())
        return self._root_site(*entry) if entry else None

    def _root_site(self, app_id, fallback_host):
        for app in self.apps:
            if app["id"] == app_id:
                found = self._from_catalog(app)
                if found:
                    return found[0] + found[1]
        return fallback_host


def tracked_files(root):
    done = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if done.returncode != 0:
        raise RelinkError(f"{root} is not a git repository ({done.stderr.decode(errors='replace').strip()})")
    return [name.decode("utf-8", errors="surrogateescape") for name in done.stdout.split(b"\0") if name]


def skipped(name):
    if name in SELF:
        return True
    base = name.rsplit("/", 1)[-1].lower()
    return base in LOCK_NAMES or base.endswith(".lock")


def line_of(data, offset):
    return data.count(b"\n", 0, offset) + 1


def line_bounds(data, offset):
    start = data.rfind(b"\n", 0, offset) + 1
    end = data.find(b"\n", offset)
    return start, len(data) if end < 0 else end


def unreleased_lines(data):
    """For a CHANGELOG: the line numbers (1-based) inside an `## [Unreleased]` section, the only ones that may
    change. Lines before the first `##` heading and in every other section are history."""
    lines = data.split(b"\n")
    allowed, current = set(), None
    for number, line in enumerate(lines, 1):
        if HEADING.match(line):
            current = bool(UNRELEASED.match(line))
        if current:
            allowed.add(number)
    return allowed


def clean(rest):
    """An address' tail without the punctuation of the sentence around it."""
    return rest.rstrip(b".,:;?!")


def relink_file(name, data, targets):
    """(new data, changes, unknown, kept) for one file's bytes. changes are (line, old, new); unknown are
    (line, address, note); kept are (line, address, reason): addresses left on purpose (a released CHANGELOG
    section, a design document, a placeholder)."""
    changes, unknown, kept, edits = [], [], [], []  # edits: (start, end, replacement) of the prefix replaced
    allowed = unreleased_lines(data) if name.rsplit("/", 1)[-1].lower() == "changelog.md" else None

    history = name.startswith(HISTORY_DIRECTORY)

    def handle(start, prefix_end, tail_end, new_prefix, note=None, placeholder=False):
        """The address is data[start:tail_end]; data[start:prefix_end] is what is replaced by new_prefix (None:
        unknown), the rest of the address follows unchanged."""
        line = line_of(data, start)
        line_start, line_end = line_bounds(data, start)
        if KEEP_MARKER in data[line_start:line_end]:
            return
        address = data[start:tail_end]
        if placeholder:
            kept.append((line, address.decode(), "placeholder"))
        elif history:
            kept.append((line, address.decode(), "design document"))
        elif new_prefix is None:
            unknown.append((line, address.decode(), note))
        elif allowed is not None and line not in allowed:
            kept.append((line, address.decode(), "released section"))
        else:
            edits.append((start, prefix_end, new_prefix.encode()))
            changes.append((line, address.decode(), new_prefix + data[prefix_end:tail_end].decode()))

    for match in PAGES_PATTERN.finditer(data):
        segment = match.group("segment")
        if segment is None:
            tail_end = match.start("tail") + len(clean(match.group("tail")))
            placeholder = data[match.end():match.end() + 1] == b"<"  # artur-rios.github.io/<repo>, in prose
            handle(match.start(), match.start("tail"), tail_end, None,
                   "a placeholder in prose, not an address" if placeholder
                   else "the profile page: the owner decides")
            continue
        prefix_end = match.end("segment")
        tail_end = prefix_end + len(clean(match.group("tail")))
        handle(match.start(), prefix_end, tail_end, targets.pages(segment.decode()),
               placeholder=segment.decode() in PLACEHOLDERS)

    for match in VERCEL_PATTERN.finditer(data):
        prefix_end = match.end("host")
        handle(match.start(), prefix_end, prefix_end + len(clean(match.group("tail"))),
               targets.vercel(match.group("host").decode()))

    if not edits:
        return data, sorted(changes), unknown, kept
    out, position = [], 0
    for start, end, replacement in sorted(edits):
        out.append(data[position:start])
        out.append(replacement)
        position = end
    out.append(data[position:])
    return b"".join(out), sorted(changes), unknown, kept


def run(root, check, catalog_module):
    targets = Targets(catalog_module)
    changed_files = changed = unknown_total = kept_total = 0
    for name in sorted(tracked_files(root)):
        path = root / name
        if skipped(name) or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        # git's own test for a binary file: a NUL byte in the first 8000 bytes.
        if b"\0" in data[:8000]:
            continue
        lowered = data.lower()
        if b"github.io" not in lowered and b"vercel.app" not in lowered:
            continue
        new_data, changes, unknown, kept = relink_file(name, data, targets)
        for line, old, new in changes:
            print(f"{name}:{line}: {old} → {new}")
        for line, address, note in unknown:
            print(f"{name}:{line}: unknown address {address}" + (f" ({note})" if note else ""))
        for line, address, reason in kept:
            print(f"{name}:{line}: kept ({reason}) {address}")
        changed += len(changes)
        unknown_total += len(unknown)
        kept_total += len(kept)
        if changes:
            changed_files += 1
            if not check and new_data != data:
                path.write_bytes(new_data)
    verb = "would change" if check else "changed"
    print(f"{verb} {changed} address(es) in {changed_files} file(s); {unknown_total} unknown; "
          f"{kept_total} kept")
    return 1 if (check and changed) or unknown_total else 0


def main(argv):
    # Plain LF and UTF-8 on every platform: the arrow below is not in a Windows console's code page.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    arguments = [a for a in argv if a != "--check"]
    check = len(arguments) != len(argv)
    if len(arguments) != 1 or arguments[0].startswith("-"):
        print("usage: relink.py <repo-dir> [--check]", file=sys.stderr)
        return 2
    root = pathlib.Path(arguments[0])
    if not root.is_dir():
        print(f"relink.py: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        import catalog  # PyYAML, like every catalog reader; reported here, not as a traceback
    except ImportError as error:
        print(f"relink.py: needs PyYAML for the catalog ({error}); pip install pyyaml", file=sys.stderr)
        return 2
    try:
        return run(root.resolve(), check, catalog)
    except (RelinkError, catalog.CatalogError) as error:
        # A broken catalog stops the run: relinking to addresses it contradicts would be worse than not.
        print(f"relink.py: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
