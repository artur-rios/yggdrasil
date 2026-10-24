#!/usr/bin/env python3
"""Moves the repositories from GitHub to the self-hosted Forgejo, in one maintenance window.

    python3 scripts/migrate.py [--dry-run] [--github-owner <name>] <command> [--only <repo>...]

Commands (each one safe to run again, and to try with --dry-run; one that fails on a repository says so
and goes on to the next, so a re-run picks up where it stopped):

  inventory   every repository of the GitHub owner (forks included), with its visibility, its open pull
              requests and whether the catalog has it. Exits 1 while any has open pull requests: they
              are finished or closed before the move.
  import      every repository of the GitHub owner that Forgejo does not have yet, through Forgejo's
              GitHub migration: issues, labels, milestones, releases, pull requests and the wiki (when
              there is one), private as on GitHub. One Forgejo has already is "already imported" and is
              left alone -- except one that exists but looks unfinished, which is reported as a failure:
              delete it on Forgejo yourself, then run import again; this script never deletes anything.
              Unfinished is a copy flagged as a pull mirror, or an empty one whose GitHub repository has
              commits. An empty copy of a repository that is empty on GitHub too is a finished import
              ("already imported"). A copy that is both empty and a mirror is reported as a mirror: it
              would keep pulling from GitHub, so being empty does not make it finished. Refuses to start
              while any repository has open pull requests, unless --allow-open-pulls. A migration may
              take long: the wait for each one is 30 minutes, MIGRATE_TIMEOUT (seconds) changes it. After
              a client timeout Forgejo may still be copying: wait for it to finish (watch the repository
              on Forgejo) before running import again, or the half-copied repository is reported as
              unfinished.
  prepare-github
              gets each GitHub repository ready to become a read-only push-mirror copy of Forgejo's, for
              the repositories Forgejo has (one it does not have is a failure: "not on Forgejo: run import
              first"). On Forgejo, Dependabot's config file (.github/dependabot.yml, and .yaml) is deleted
              on the default branch, in a commit by the token's owner; none there is "no dependabot file".
              On GitHub, every ruleset but `mirror tags` (rules.py makes that one) is deleted, issues are
              turned off, the description is set to `Mirror of <forgejo url>/<owner>/<repo> -- contribute
              there` (with an em dash), and automated security fixes are switched off. It only sends what
              is not so yet, so a second run writes nothing. A repository whose Forgejo step fails is not
              touched on GitHub. A repository archived on GitHub is left as it is there (a note, not a
              failure; its Dependabot file is still deleted on Forgejo). A private repository whose
              rulesets GitHub will not list (403: a plan without them) has none to delete: a note. With --dry-run it prints what it would do and sends no write.
              Runbook order: import, prepare-github, mirrors, collaborators, rules.py, verify.

The move covers every repository of the GitHub owner, forks included. Only the catalog's repositories
(catalog.py repository-names) get the bots as collaborators and the webhook; `inventory` shows both.

The GitHub owner is the catalog's `mirror.github` when a mirror is set, else --github-owner. The Forgejo
owner is the catalog's `owner`, as for rules.py. Tokens: GitHub's as rules.py finds it (GH_TOKEN, else
`gh auth token`), Forgejo's in FORGE_TOKEN (the owner's, scope write:repository); they go only in the
Authorization header, never in a process argument, a URL or a line of output. The one exception is
GitHub's, which Forgejo's migration API takes in the request body (`auth_token`): import sends it there
and shows it as *** in a --dry-run. The Forgejo token has to own the target namespace (`repo_owner` is
the catalog's owner): the owner's.

Exit status: 0 done, 1 something failed (each failure on its own line at the end), 2 usage (a missing token
or owner, an unknown option, command or repository).

A command is `cmd_<name>(ctx, args) -> list[str]`: it prints its lines as it goes and returns its failures,
one string each; COMMANDS lists them. Adding a command is a function and a line in COMMANDS. Every command
fails the same way: each repository's requests run inside `try/except rules.ApiError`, the failure is printed
and kept under the repository's name, and the run goes on to the next repository.
"""

import argparse
import dataclasses
import functools
import json
import os
import pathlib
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import catalog as catalog_module  # noqa: E402  (scripts/catalog.py)
import rules  # noqa: E402  (scripts/rules.py: the HTTP client and the GitHub token lookup)


class UsageError(Exception):
    """Something the person running this has to change (exit 2): a missing token or owner, a wrong name."""


class MigrateError(Exception):
    """A run that cannot go on although it was asked right (exit 1)."""


# --- The shared context -------------------------------------------------------------------------------

@dataclasses.dataclass
class Context:
    """What every command works over. The API clients are made on first use, so a command only needs
    the tokens it talks with: inventory never asks for FORGE_TOKEN."""
    catalog: dict
    dry_run: bool
    only: list
    github_owner: str

    @property
    def forgejo_owner(self):
        """Where the repositories live on Forgejo: the catalog's owner, as rules.py applies its rules."""
        return self.catalog["owner"]

    @functools.cached_property
    def github_token(self):
        token = rules.find_github_token()
        if not token:
            raise UsageError("no GitHub token: set GH_TOKEN, or log in with `gh auth login`")
        return token

    @functools.cached_property
    def gh(self):
        return rules.Api(rules.GITHUB_API, self.github_token, "Bearer", rules.GITHUB_HEADERS)

    @functools.cached_property
    def forgejo(self):
        """The Forgejo client, built like rules.py's _forgejo_connection does, minus the webhook."""
        forge = catalog_module.forge(self.catalog)
        if forge["type"] != "forgejo":
            raise UsageError("catalog.yaml has no `forge: {type: forgejo, url: ...}` block: there is no Forgejo to talk to")
        token = os.environ.get("FORGE_TOKEN", "").strip()
        if not token:
            raise UsageError("no Forgejo token: set FORGE_TOKEN (the owner's, scope write:repository)")
        return rules.Api(forge["api"], token, "token")

    @functools.cached_property
    def migrate_timeout(self):
        """Seconds Forgejo may take to answer one migration (it answers when the repository is copied)."""
        value = os.environ.get("MIGRATE_TIMEOUT", "").strip()
        if not value:
            return MIGRATE_TIMEOUT
        try:
            seconds = int(value)
        except ValueError:
            seconds = 0
        if seconds < 1:
            raise UsageError(f"MIGRATE_TIMEOUT must be a number of seconds above 0, not '{value}'")
        return seconds

    def catalog_repositories(self):
        return set(catalog_module.repository_names(self.catalog))


# Half an hour for one migration, in seconds: a large repository with its issues and pull requests is copied
# through GitHub's API, which is slow. MIGRATE_TIMEOUT changes it.
MIGRATE_TIMEOUT = 30 * 60


# --- GitHub: the owner's repositories -----------------------------------------------------------------

# Safety stop for a server that ignores `page` and would answer the same page for ever.
MAX_PAGES = 200


def _paged(api, path):
    """Every item of a list endpoint. Pages are asked until an empty one: a server that serves fewer
    than the per_page asked for (GitHub's cap is 100) would otherwise lose the rest."""
    items = []
    for page in range(1, MAX_PAGES + 1):
        batch = api.request("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        if not batch:
            return items
        items += batch
    raise MigrateError(f"{path}: more than {MAX_PAGES} pages; stopped")


def _list_path(gh, owner):
    """The endpoint that lists the owner's repositories, private ones included. /users/<o>/repos shows
    only the public ones, so a user's are listed through /user/repos, which means the token has to be
    that user's: anything else would quietly leave the private repositories out of the move."""
    kind = gh.request("GET", f"/users/{owner}")["type"]
    if kind == "Organization":
        return f"/orgs/{owner}/repos?type=all"
    login = gh.request("GET", "/user")["login"]
    if login.lower() != owner.lower():
        raise MigrateError(f"the GitHub token is {login}'s, not {owner}'s, so {owner}'s private repositories "
                           f"cannot be listed: use {owner}'s token")
    return "/user/repos?affiliation=owner"


def github_repositories(gh, owner, only=None):
    """Every repository of the owner, forks included, as {name, private, fork, archived, default_branch,
    has_wiki, size} (size: GitHub's, in KB, which lags a fresh push). With `only` (names), just those; a name the owner has not is a UsageError. The open pull
    requests are counted apart (open_pull_requests), one repository at a time."""
    listed = _paged(gh, _list_path(gh, owner))
    if only:
        unknown = sorted(set(only) - {r["name"] for r in listed})
        if unknown:
            raise UsageError(f"not repositories of {owner} on GitHub: {', '.join(unknown)}")
        listed = [r for r in listed if r["name"] in only]
    return [{"name": r["name"], "private": r["private"], "fork": r["fork"], "archived": r["archived"],
             "default_branch": r["default_branch"], "has_wiki": r["has_wiki"], "size": r["size"]} for r in listed]


def open_pull_requests(gh, owner, name):
    """How many open pull requests the repository has, from its pulls list (`GET /repos/<o>/<r>/pulls
    ?state=open`), not with the search API: search is limited to 30 requests a minute and 1000 results,
    and its index lags a new pull request by seconds, which is not good enough for the check that says
    the move can start. It costs one request per repository (two when it has some), against the 5000 an
    hour GitHub allows an authenticated token."""
    return len(_paged(gh, f"/repos/{owner}/{name}/pulls?state=open"))


# --- The commands -------------------------------------------------------------------------------------

def _plural(count, noun, many=None):
    return f"{count} {noun if count == 1 else many or noun + 's'}"


def _count_open_pulls(ctx, repo):
    """Sets repo["open_pulls"] (None when the count failed: the run goes on to the next repository) and
    returns the failure to report for it, or None when it has no open pull requests."""
    try:
        repo["open_pulls"] = open_pull_requests(ctx.gh, ctx.github_owner, repo["name"])
    except rules.ApiError as error:
        repo["open_pulls"] = None
        return f"{repo['name']}: could not count the open pull requests: {error}"
    if repo["open_pulls"]:
        return f"{repo['name']}: {_plural(repo['open_pulls'], 'open pull request')}: finish or close them first"
    return None


def cmd_inventory(ctx, args):
    """One line per repository, then totals. Read-only. Fails for each repository with open pull
    requests (they are finished or closed first) and for each whose pull requests could not be counted."""
    repos = github_repositories(ctx.gh, ctx.github_owner, ctx.only)
    in_catalog = ctx.catalog_repositories()
    failures = []
    for r in repos:
        # Counted here, one repository at a time, so its line is out before the next count and one that
        # fails (its line shows "?") does not hide the rest.
        failure = _count_open_pulls(ctx, r)
        print("\t".join([r["name"], "private" if r["private"] else "public", "yes" if r["fork"] else "no",
                         "?" if r["open_pulls"] is None else str(r["open_pulls"]),
                         "yes" if r["name"] in in_catalog else "no"]), flush=True)
        if failure:
            failures.append(failure)

    # A catalog repository GitHub does not have would fail its import later: say so now. (Not with --only,
    # which looks at some of the repositories on purpose.)
    if not ctx.only:
        for name in sorted(in_catalog - {r["name"] for r in repos}):
            print(f"note: {name} is in the catalog but not on GitHub under {ctx.github_owner}", flush=True)

    with_pulls = [r for r in repos if r["open_pulls"]]
    not_counted = [r for r in repos if r["open_pulls"] is None]
    print(f"{_plural(len(repos), 'repository', 'repositories')}: "
          f"{sum(r['private'] for r in repos)} private, {_plural(sum(r['fork'] for r in repos), 'fork')}, "
          f"{sum(r['archived'] for r in repos)} archived; "
          f"{sum(r['name'] in in_catalog for r in repos)} in the catalog, "
          f"{sum(r['name'] not in in_catalog for r in repos)} not; "
          f"{_plural(sum(r['open_pulls'] for r in with_pulls), 'open pull request')} "
          f"in {_plural(len(with_pulls), 'repository', 'repositories')}"
          + (f"; {_plural(len(not_counted), 'repository', 'repositories')} not counted" if not_counted else ""),
          flush=True)
    return failures


# --- import ---------------------------------------------------------------------------------------------

def migration_payload(ctx, repo):
    """The body of Forgejo's `POST /api/v1/repos/migrate` for one GitHub repository: a one-time copy (not a
    mirror) with everything GitHub's API can give -- issues, labels, milestones, releases, pull requests, the
    wiki when there is one -- visibility as on GitHub, no LFS (the repositories have none). `auth_token` is
    GitHub's: Forgejo reads it from the body, so this body never goes to a log (see _shown)."""
    return {"service": "github", "clone_addr": f"https://github.com/{ctx.github_owner}/{repo['name']}.git",
            "auth_token": ctx.github_token, "repo_owner": ctx.forgejo_owner, "repo_name": repo["name"],
            "private": repo["private"], "mirror": False, "issues": True, "labels": True, "milestones": True,
            "releases": True, "pull_requests": True, "wiki": repo["has_wiki"], "lfs": False}


def _shown(payload):
    """The payload as it may be printed: the token hidden."""
    return {**payload, "auth_token": "***"}


def _on_forgejo(ctx, name):
    """The repository as Forgejo has it (200), or None when it has no such repository (404). Any other
    answer raises the ApiError: unsure whether it is there is not a reason to migrate it."""
    try:
        # `or {}`: a 200 with an empty body (the client answers None) still means the repository is there.
        return ctx.forgejo.request("GET", f"/repos/{ctx.forgejo_owner}/{name}") or {}
    except rules.ApiError as error:
        if error.status == 404:
            return None
        raise


def _unfinished(found):
    """Why a repository Forgejo has does not look like a finished migration, or None. A failed or interrupted
    migration leaves an empty repository (Forgejo's `empty`), or one still flagged as a mirror (a pull
    mirror: `mirror`; the import asks for none). Checked on Forgejo 16's repository JSON in the live suite."""
    # The mirror is looked at first: a copy that is both is a mirror before anything else (see the module
    # docstring), so it is never taken for a finished empty import.
    if found.get("mirror"):
        return "mirror"
    if found.get("empty"):
        return "empty"
    return None


def _report(ctx, failure):
    """Prints a failure where it happened (it is listed again at the end) and returns it, with the GitHub
    token hidden: a server's error text may echo what it was sent. No token (empty, None) hides nothing:
    str.replace on "" would put *** between every character."""
    token = ctx.github_token
    if token:
        failure = failure.replace(token, "***")
    print(failure, flush=True)
    return failure


def _empty_on_github_too(ctx, repo):
    """Whether GitHub's repository has no commit either, so that Forgejo's empty copy is a finished import
    (deleting it and importing again would give the same empty repository). `size` 0 is only the hint to
    ask: it is in KB and lags a fresh push, so a just-pushed repository can show 0. The commits list is
    the proof: GitHub answers 409 for a repository without a commit. Raises ApiError when it cannot tell."""
    if repo["size"] != 0:
        return False
    try:
        ctx.gh.request("GET", f"/repos/{ctx.github_owner}/{repo['name']}/commits?per_page=1")
    except rules.ApiError as error:
        if error.status == 409:
            return True
        raise
    return False


def _import_one(ctx, repo):
    """Imports one repository; returns the failure to report, or None. Prints its line."""
    name = repo["name"]
    try:
        found = _on_forgejo(ctx, name)
    except rules.ApiError as error:
        return _report(ctx, f"{name}: could not look for it on Forgejo: {error}")
    if found is not None:
        why = _unfinished(found)
        if why == "empty":
            try:
                if _empty_on_github_too(ctx, repo):
                    print(f"{name}: already imported (empty on GitHub too)", flush=True)
                    return None
            except rules.ApiError as error:
                return _report(ctx, f"{name}: could not tell whether it is empty on GitHub too: {error}")
        if why:
            return _report(ctx, f"{name}: exists but looks unfinished ({why}): "
                                "delete it on Forgejo and run import again")
        print(f"{name}: already imported", flush=True)
        return None
    payload = migration_payload(ctx, repo)
    if ctx.dry_run:
        print(f"{name}: would import {json.dumps(_shown(payload))}", flush=True)
        return None
    try:
        ctx.forgejo.request("POST", "/repos/migrate", payload, timeout=ctx.migrate_timeout)
    except rules.ApiError as error:
        return _report(ctx, f"{name}: import failed: {error}")
    print(f"{name}: imported", flush=True)
    return None


def cmd_import(ctx, args):
    """Imports the repositories Forgejo does not have, with their issues, pull requests and releases. Refuses
    while any has open pull requests, unless --allow-open-pulls."""
    ctx.forgejo, ctx.migrate_timeout  # a missing token or a wrong timeout is a usage error before any output
    repos = github_repositories(ctx.gh, ctx.github_owner, ctx.only)
    failures = []
    if not args.allow_open_pulls:
        # Everything is counted before anything is sent: one repository with open pull requests stops the
        # whole run, and one that could not be counted is not imported (its pull requests are unknown).
        found = False
        for r in repos:
            failure = _count_open_pulls(ctx, r)
            if failure:
                failures.append(_report(ctx, failure))
                found = found or r["open_pulls"] is not None
        if found:
            failures.append("nothing imported: finish or close the open pull requests, "
                            "or run again with --allow-open-pulls")
            return failures
        repos = [r for r in repos if r["open_pulls"] is not None]
    for r in repos:
        failure = _import_one(ctx, r)
        if failure:
            failures.append(failure)
    return failures


cmd_import.arguments = lambda sub: sub.add_argument(
    "--allow-open-pulls", action="store_true", help="import although repositories have open pull requests")

# --- prepare-github ---------------------------------------------------------------------------------------

# Dependabot reads either name; both are valid, so both are looked for.
DEPENDABOT_FILES = (".github/dependabot.yml", ".github/dependabot.yaml")
DEPENDABOT_MESSAGE = "chore: stop dependabot; renovate replaces it"


def mirror_description(ctx, name):
    """The GitHub description of a copy: it points at Forgejo, built from the catalog's forge url and the
    owner (not a fixed host). The em dash (U+2014) is part of the text."""
    return f"Mirror of {catalog_module.forge(ctx.catalog)['url']}/{ctx.forgejo_owner}/{name} \u2014 contribute there"


def _write(ctx, name, would, done, send):
    """One write: with --dry-run only the line "would ..." is printed and `send` is not called; else `send()`
    goes out first and the "done" line is printed once it has succeeded (a failure raises before it)."""
    if ctx.dry_run:
        print(f"{name}: would {would}", flush=True)
        return
    send()
    print(f"{name}: {done}", flush=True)


def _delete_dependabot_files(ctx, name, found, github_repo):
    """Deletes Dependabot's config file on Forgejo's default branch (one commit per file that is there).
    Raises ApiError. An empty copy has no branch to look at: nothing to delete."""
    if found.get("empty"):
        print(f"{name}: no dependabot file (empty repository)", flush=True)
        return
    # The copy's own default branch; GitHub's is the same after an import, and the fallback if Forgejo omits it.
    branch = found.get("default_branch") or github_repo["default_branch"]
    base = f"/repos/{ctx.forgejo_owner}/{name}/contents"
    deleted = False
    for filepath in DEPENDABOT_FILES:
        try:
            current = ctx.forgejo.request("GET", f"{base}/{filepath}?ref={urllib.parse.quote(branch, safe='')}")
        except rules.ApiError as error:
            if error.status == 404:  # no such file on that branch
                continue
            raise
        sha = current.get("sha") if isinstance(current, dict) else None
        if not sha:
            raise rules.ApiError("GET", f"{base}/{filepath}", 200, "the answer has no sha: not a file?")
        deleted = True
        _write(ctx, name, f"delete {filepath} on {branch}", f"deleted {filepath} on {branch}",
               lambda filepath=filepath, sha=sha: ctx.forgejo.request(
                   "DELETE", f"{base}/{filepath}", {"sha": sha, "branch": branch, "message": DEPENDABOT_MESSAGE}))
    if not deleted:
        print(f"{name}: no dependabot file", flush=True)


def _delete_security_fixes(ctx, base):
    """204 is the answer; 404 says there is nothing to switch off, which is the goal too."""
    try:
        ctx.gh.request("DELETE", f"{base}/automated-security-fixes")
    except rules.ApiError as error:
        if error.status != 404:
            raise


def _github_rulesets(ctx, name, private):
    """The repository's rulesets on GitHub. A private repository on a plan without rulesets answers 403 to
    the list: there is nothing to delete, which is a note and not a failure. (A public repository's 403
    is a real refusal and stays one.)"""
    try:
        return ctx.gh.request("GET", f"/repos/{ctx.github_owner}/{name}/rulesets?per_page=100") or []
    except rules.ApiError as error:
        if private and error.status == 403:
            print(f"{name}: rulesets not available here: nothing to delete", flush=True)
            return []
        raise


def _prepare_github_side(ctx, name, private):
    """Old rulesets deleted (but `mirror tags`), issues off, description set, automated security fixes
    off: each only when it is not so yet. Raises ApiError."""
    base = f"/repos/{ctx.github_owner}/{name}"
    changed = False
    for ruleset in _github_rulesets(ctx, name, private):
        if ruleset["name"] == rules.MIRROR_TAGS:
            print(f"{name}: kept ruleset '{ruleset['name']}'", flush=True)
        else:
            changed = True
            _write(ctx, name, f"delete ruleset '{ruleset['name']}'", f"deleted ruleset '{ruleset['name']}'",
                   lambda ruleset=ruleset: ctx.gh.request("DELETE", f"{base}/rulesets/{ruleset['id']}"))

    now = ctx.gh.request("GET", base) or {}
    wanted = mirror_description(ctx, name)
    if now.get("has_issues") is not False or now.get("description") != wanted:
        changed = True
        _write(ctx, name, "set issues off and the description", "issues are off and the description is set",
               lambda: ctx.gh.request("PATCH", base, {"has_issues": False, "description": wanted}))

    # `enabled` is read first so that a repository that is already off costs no write. A 404 on the read
    # means the feature is not there to switch off (GitHub answers it for a repository without it).
    try:
        fixes = (ctx.gh.request("GET", f"{base}/automated-security-fixes") or {}).get("enabled")
    except rules.ApiError as error:
        if error.status != 404:
            raise
        fixes = False
    if fixes:
        changed = True
        _write(ctx, name, "switch off automated security fixes", "automated security fixes are off",
               lambda: _delete_security_fixes(ctx, base))
    if not changed:
        print(f"{name}: already prepared on GitHub", flush=True)


def _prepare_one(ctx, repo):
    """Prepares one repository; returns the failure to report, or None. Prints its lines. Forgejo's side
    goes first and a failure there stops the repository (its GitHub side waits for the re-run)."""
    name = repo["name"]
    try:
        found = _on_forgejo(ctx, name)
    except rules.ApiError as error:
        return _report(ctx, f"{name}: could not look for it on Forgejo: {error}")
    if found is None:
        return _report(ctx, f"{name}: not on Forgejo: run import first")
    try:
        _delete_dependabot_files(ctx, name, found, repo)
        if repo["archived"]:
            # Read-only on GitHub: it takes no setting and no ruleset change, so it is not even read.
            print(f"{name}: archived on GitHub: left as it is", flush=True)
        else:
            _prepare_github_side(ctx, name, repo["private"])
    except rules.ApiError as error:
        return _report(ctx, f"{name}: could not prepare it: {error}")
    return None


def cmd_prepare_github(ctx, args):
    """Gets the GitHub copies ready to be mirrors: Dependabot's file deleted on Forgejo; on GitHub the old
    rulesets (not `mirror tags`) deleted, issues off, the description set, automated security fixes off."""
    ctx.forgejo  # a missing token is a usage error before any output
    failures = []
    for repo in github_repositories(ctx.gh, ctx.github_owner, ctx.only):
        failure = _prepare_one(ctx, repo)
        if failure:
            failures.append(failure)
    return failures


COMMANDS = {"inventory": cmd_inventory, "import": cmd_import, "prepare-github": cmd_prepare_github}


# --- The command line ---------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _parser():
    parser = _Parser(prog="migrate.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="send no write (every command still reads)")
    parser.add_argument("--github-owner",
                        help="the GitHub user or organisation to move (the catalog's mirror.github, when it has one)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, command in COMMANDS.items():
        sub = commands.add_parser(name, help=(command.__doc__ or "").strip().split("\n\n")[0])
        sub.add_argument("--only", nargs="+", metavar="<repo>", default=[], help="act on these repositories alone")
        if hasattr(command, "arguments"):
            command.arguments(sub)
    return parser


def _github_owner(catalog, option):
    mirror = catalog_module.mirror(catalog)
    if mirror is None:
        if not option:
            raise UsageError("whose repositories? The catalog has no `mirror.github`: pass --github-owner <name>")
        return option
    if option and option != mirror["github"]:
        raise UsageError(f"--github-owner {option} is not the catalog's mirror.github ({mirror['github']}): "
                         "leave the option out, or change the catalog")
    return mirror["github"]


def main(argv=None):
    """Runs a command; returns the exit status (0 done, 1 failures, 2 usage)."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = _parser().parse_args(argv)
        try:
            catalog = catalog_module.load()
        except catalog_module.CatalogError as error:
            raise UsageError(str(error)) from None
        ctx = Context(catalog=catalog, dry_run=args.dry_run, only=args.only,
                      github_owner=_github_owner(catalog, args.github_owner))
        failures = COMMANDS[args.command](ctx, args)
    except UsageError as error:
        print(f"migrate.py: {error}", file=sys.stderr)
        return 2
    except (rules.ApiError, MigrateError) as error:
        failures = [str(error)]
    if failures:
        print(f"{_plural(len(failures), 'failure')}:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
