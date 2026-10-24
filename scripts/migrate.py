#!/usr/bin/env python3
"""Moves the repositories from GitHub to the self-hosted Forgejo, in one maintenance window.

    python3 scripts/migrate.py [--dry-run] [--github-owner <name>] <command> [--only <repo>...]

Commands (each one safe to run again, and to try with --dry-run; one that fails on a repository says so
and goes on to the next, so a re-run picks up where it stopped):

  inventory   every repository of the GitHub owner (forks included), with its visibility, its open pull
              requests and whether the catalog has it. Exits 1 while any has open pull requests: they
              are finished or closed before the move.

The move covers every repository of the GitHub owner, forks included. Only the catalog's repositories
(catalog.py repository-names) get the bots as collaborators and the webhook; `inventory` shows both.

The GitHub owner is the catalog's `mirror.github` when a mirror is set, else --github-owner. The Forgejo
owner is the catalog's `owner`, as for rules.py. Tokens: GitHub's as rules.py finds it (GH_TOKEN, else
`gh auth token`), Forgejo's in FORGE_TOKEN (the owner's, scope write:repository); they go only in the
Authorization header, never in a process argument, a URL or a line of output.

Exit status: 0 done, 1 something failed (each failure on its own line at the end), 2 usage (a missing token
or owner, an unknown option, command or repository).

A command is `cmd_<name>(ctx, args) -> list[str]`: it prints its lines as it goes and returns its failures,
one string each; COMMANDS lists them. Adding a command is a function and a line in COMMANDS.
"""

import argparse
import dataclasses
import functools
import os
import pathlib
import sys

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
    def gh(self):
        token = rules.find_github_token()
        if not token:
            raise UsageError("no GitHub token: set GH_TOKEN, or log in with `gh auth login`")
        return rules.Api(rules.GITHUB_API, token, "Bearer", rules.GITHUB_HEADERS)

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

    def catalog_repositories(self):
        return set(catalog_module.repository_names(self.catalog))


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
    has_wiki, open_pulls}. With `only` (names), just those; a name the owner has not is a UsageError.

    The open pull requests are counted per repository from its pulls list (`GET /repos/<o>/<r>/pulls
    ?state=open`), not with the search API: search is limited to 30 requests a minute and 1000 results,
    and its index lags a new pull request by seconds, which is not good enough for the check that says
    the move can start. The listing costs one request per repository (two when it has some), against the
    5000 an hour GitHub allows an authenticated token."""
    listed = _paged(gh, _list_path(gh, owner))
    if only:
        unknown = sorted(set(only) - {r["name"] for r in listed})
        if unknown:
            raise UsageError(f"not repositories of {owner} on GitHub: {', '.join(unknown)}")
        listed = [r for r in listed if r["name"] in only]
    return [{"name": r["name"], "private": r["private"], "fork": r["fork"], "archived": r["archived"],
             "default_branch": r["default_branch"], "has_wiki": r["has_wiki"],
             "open_pulls": len(_paged(gh, f"/repos/{owner}/{r['name']}/pulls?state=open"))} for r in listed]


# --- The commands -------------------------------------------------------------------------------------

def _plural(count, noun, many=None):
    return f"{count} {noun if count == 1 else many or noun + 's'}"


def cmd_inventory(ctx, args):
    """One line per repository, then totals. Read-only. Fails for each repository with open pull
    requests: they are finished or closed first."""
    repos = github_repositories(ctx.gh, ctx.github_owner, ctx.only)
    in_catalog = ctx.catalog_repositories()
    failures = []
    for r in repos:
        print("\t".join([r["name"], "private" if r["private"] else "public", "yes" if r["fork"] else "no",
                         str(r["open_pulls"]), "yes" if r["name"] in in_catalog else "no"]), flush=True)
        if r["open_pulls"]:
            failures.append(f"{r['name']}: {_plural(r['open_pulls'], 'open pull request')}: finish or close them first")

    # A catalog repository GitHub does not have would fail its import later: say so now. (Not with --only,
    # which looks at some of the repositories on purpose.)
    if not ctx.only:
        for name in sorted(in_catalog - {r["name"] for r in repos}):
            print(f"note: {name} is in the catalog but not on GitHub under {ctx.github_owner}", flush=True)

    with_pulls = [r for r in repos if r["open_pulls"]]
    print(f"{_plural(len(repos), 'repository', 'repositories')}: "
          f"{sum(r['private'] for r in repos)} private, {_plural(sum(r['fork'] for r in repos), 'fork')}, "
          f"{sum(r['archived'] for r in repos)} archived; "
          f"{sum(r['name'] in in_catalog for r in repos)} in the catalog, "
          f"{sum(r['name'] not in in_catalog for r in repos)} not; "
          f"{_plural(sum(r['open_pulls'] for r in with_pulls), 'open pull request')} "
          f"in {_plural(len(with_pulls), 'repository', 'repositories')}", flush=True)
    return failures


COMMANDS = {"inventory": cmd_inventory}


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
