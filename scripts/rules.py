#!/usr/bin/env python3
"""Applies the branching model's rules to every repository in catalog.yaml, on GitHub or Forgejo.

    python3 scripts/rules.py [--dry-run] [<repo>...]

This file holds the model: what the rules are, worked out from the catalog (desired_rules), and
the git servers' sides that apply it: GitHub rulesets (github_rulesets, apply_github) and, in a
later change, Forgejo branch and tag protections. Importing it needs no network or token; the
token is read when the command runs (GitHub: GH_TOKEN, else `gh auth token`) and goes only in the
Authorization header, never in a process argument, a log or the --dry-run output. --dry-run still
reads (GET) from the server, so it needs the token, but sends no write.

Per repository the model is:

  develop        no direct or force push, no deletion; changes through a pull request whose checks
                 pass (branch-policy, the catalog's checks, and its actionsChecks); merged by the
                 owner and forge.developers
  main           the same, with branch-policy, checks and deploy/<environment> for each release
                 environment instead of actionsChecks; merged only by the Jenkins user for an
                 application, by the owner for a repository that tags its own releases
  release/*      only the owner pushes
  version tags   created by the Jenkins user (applications) or the owner (owner-tag repositories)
  Jenkinsfile    protected on develop and main (Forgejo)

A repository with `rules: basic` gets develop and main without force push or deletion, and nothing else.

GitHub cannot restrict who may merge a given file, so `protected_files` (the Jenkinsfile) is not
applied there; Jenkins' trust rule (plan 3) covers it. Nor does GitHub get a release/* rule: there
is none today.
"""

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import catalog as catalog_module  # noqa: E402  (scripts/catalog.py)

# The check that limits a pull request's source branch; every protected branch requires it first.
BRANCH_POLICY = "branch-policy"


@dataclasses.dataclass(frozen=True)
class RepoRules:
    repo: str                    # repository name under the owner
    model: str                   # "release" | "basic"
    develop_checks: tuple        # required on develop (release model): branch-policy, checks, actionsChecks
    main_checks: tuple           # required on main: branch-policy, checks, deploy/<env>...
    actions_checks: frozenset    # the subset GitHub binds to GitHub Actions
    develop_mergers: tuple       # owner + forge.developers
    main_mergers: tuple          # (forge.jenkinsUser,) for applications, (owner,) otherwise
    release_pushers: tuple       # (owner,)
    tag_pattern: str             # "v*" or the repository's `tags`
    tag_creators: tuple          # (forge.jenkinsUser,) for applications, (owner,) for owner-tag repositories
    protected_files: tuple       # ("Jenkinsfile",)
    # With model "basic" the checks, mergers and protected_files are empty, and the release rules
    # (release_pushers, tag_pattern, tag_creators) keep their defaults but are not applied.


def _unique(*groups):
    """The names of every group, in order, each once."""
    seen = {}
    for group in groups:
        for name in group:
            seen.setdefault(name, None)
    return tuple(seen)


def _rules(repo, model, owner, checks, actions_checks, deploys, main_merger, tag_pattern, tag_creator, developers):
    if model == "basic":
        return RepoRules(repo, model, (), (), frozenset(), (), (), (owner,), tag_pattern, (tag_creator,), ())
    return RepoRules(
        repo=repo,
        model=model,
        # branch-policy first, then the catalog's order; a name listed twice (or listed as
        # branch-policy itself) is required once.
        develop_checks=_unique([BRANCH_POLICY], checks, actions_checks),
        main_checks=_unique([BRANCH_POLICY], checks, [f"deploy/{environment}" for environment in deploys]),
        actions_checks=frozenset(actions_checks),
        develop_mergers=_unique([owner], developers),
        main_mergers=(main_merger,),
        release_pushers=(owner,),
        tag_pattern=tag_pattern,
        tag_creators=(tag_creator,),
        protected_files=("Jenkinsfile",),
    )


def desired_rules(catalog):
    """The rules of every repository of the catalog: applications first (catalog order), then the
    `repositories` entries. Applications are deployable ones only (platform components live in this
    repository and have none)."""
    owner = catalog["owner"]
    forge = catalog_module.forge(catalog)
    jenkins, developers = forge["jenkinsUser"], forge["developers"]

    # An application's repository is its `repository`, else its id. Applications that share one are
    # a single repository: their checks and release environments are required together.
    applications = {}
    for _, app in catalog_module.applications(catalog, deployable=True):
        entry = applications.setdefault(app.get("repository") or app["id"], {"checks": [], "actions": [], "deploys": []})
        entry["checks"] += app.get("checks") or []
        entry["actions"] += app.get("actionsChecks") or []
        entry["deploys"] += [e["id"] for e in catalog_module.resolve(catalog, app["id"]) if e["trigger"] == "release"]

    modelled = [_rules(repo, "release", owner, _unique(e["checks"]), _unique(e["actions"]), _unique(e["deploys"]),
                       jenkins, "v*", jenkins, developers)
                for repo, e in applications.items()]
    for entry in catalog_module.repositories(catalog):
        # The owner releases these by hand: they merge main and create the tags themselves.
        owner_tag = entry["release"] == "owner-tag"
        modelled.append(_rules(entry["id"], entry["rules"], owner, entry["checks"], entry["actionsChecks"], (),
                               owner, entry["tags"] if owner_tag else "v*", owner, developers))
    return modelled


# --- Talking to the git servers ---------------------------------------------------------------------

class ApiError(Exception):
    """A request the server answered with a non-2xx status (status 0: no answer at all)."""

    def __init__(self, method, path, status, message):
        super().__init__(f"{method} {path}: {status} {message}")
        self.method, self.path, self.status, self.message = method, path, status, message


class Api:
    """A small JSON client for one git server's REST API. The token only ever goes in the
    Authorization header (`<scheme> <token>`: Bearer for GitHub, token for Forgejo), never in the URL,
    a process argument or an error message."""

    def __init__(self, base, token, scheme, headers=None):
        self.base = base.rstrip("/")
        self._token = token
        self._headers = {"Accept": "application/json", **(headers or {}), "Authorization": f"{scheme} {token}"}

    def request(self, method, path, body=None):
        """The parsed JSON answer, or None when the answer is empty. `path` starts with a slash."""
        headers = dict(self._headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise ApiError(method, path, error.code, self._message(error.read())) from None
        except (urllib.error.URLError, OSError) as error:
            raise ApiError(method, path, 0, self._scrub(str(getattr(error, "reason", error)))) from None
        return json.loads(text) if text.strip() else None

    def _scrub(self, text):
        return text.replace(self._token, "***") if self._token else text

    def _message(self, raw):
        """The server's explanation: the `message` of a JSON answer, else the (short) text."""
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("message"):
                text = str(parsed["message"])
        except ValueError:
            pass
        return self._scrub(text[:500])


def github_token():
    """GH_TOKEN, else what `gh auth token` prints (read from its output; the token is never passed
    to a process)."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    try:
        done = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, encoding="utf-8")
        token = done.stdout.strip() if done.returncode == 0 else ""
    except OSError:
        token = ""
    if not token:
        sys.exit("no GitHub token: set GH_TOKEN, or log in with `gh auth login`")
    return token


# --- GitHub: rulesets ---------------------------------------------------------------------------------

# GitHub Actions' app id. Only the actionsChecks are bound to it, so a status someone posts under
# the same name from elsewhere cannot satisfy them. Jenkins' statuses (branch-policy, checks,
# deploy/<environment>) are left unbound: Jenkins posts them under whatever identity the installation
# uses, and that identity can be replaced without editing this.
GITHUB_ACTIONS = 15368

GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# Repository administrators (the owner) bypass every ruleset -- for emergencies.
GITHUB_ADMIN_BYPASS = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]


def _status_checks(names, bound=frozenset()):
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": [
                {"context": name, **({"integration_id": GITHUB_ACTIONS} if name in bound else {})} for name in names],
        },
    }


def _pull_request(methods):
    return {
        "type": "pull_request",
        "parameters": {
            # A single maintainer cannot approve their own pull request, and Jenkins merges release
            # pull requests itself: the checks are the gate, not reviews.
            "required_approving_review_count": 0,
            "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_review_thread_resolution": False,
            "allowed_merge_methods": methods,
        },
    }


def _ruleset(name, target, ref, rule_list):
    return {
        "name": name,
        "target": target,
        "enforcement": "active",
        "bypass_actors": GITHUB_ADMIN_BYPASS,
        "conditions": {"ref_name": {"include": [ref], "exclude": []}},
        "rules": rule_list,
    }


def github_rulesets(r):
    """The ruleset payloads of one repository: `develop`, `main` and (release model) `release tags`.
    The names are the ones github/rulesets.py always used, so existing rulesets are updated in place."""
    if r.model == "basic":
        # Protection from deletion and force push, nothing else: no pull request, checks or tags.
        basic = [{"type": "deletion"}, {"type": "non_fast_forward"}]
        return [_ruleset("develop", "branch", "refs/heads/develop", basic),
                _ruleset("main", "branch", "refs/heads/main", list(basic))]

    # A version names one commit forever: its tag cannot be moved or deleted.
    tag_rules = [{"type": "update"}, {"type": "deletion"}]
    if r.tag_creators == r.release_pushers:
        # An owner-tag repository: only the owner (an administrator, who bypasses the ruleset) creates
        # its tags. An application's tags are created by Jenkins, which must not be locked out.
        tag_rules.insert(0, {"type": "creation"})

    return [
        _ruleset("develop", "branch", "refs/heads/develop", [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            _pull_request(["squash", "merge"]),
            _status_checks(r.develop_checks, r.actions_checks),
        ]),
        _ruleset("main", "branch", "refs/heads/main", [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            # Merge commits only: squashing a release would give main commits develop never had, and
            # the next release branch would conflict.
            _pull_request(["merge"]),
            _status_checks(r.main_checks),
        ]),
        _ruleset("release tags", "tag", f"refs/tags/{r.tag_pattern}", tag_rules),
    ]


def _github_steps(api, owner, r, dry_run):
    """What applying one repository does, as lines, as it happens (a generator, so a failure halfway
    still shows what was done before it)."""
    base = f"/repos/{owner}/{r.repo}"
    yield f"{owner}/{r.repo}"

    def write(method, path, body, done, would):
        if dry_run:
            yield f"  would {would}: {method} {path}"
            if body is not None:
                yield "    " + json.dumps(body)
        else:
            api.request(method, path, body)
            yield f"  {done}"

    settings = {"default_branch": "develop", "delete_branch_on_merge": True}
    yield from write("PATCH", base, settings, "default branch develop, delete head branches on merge",
                     "set the default branch to develop and delete head branches on merge")

    existing = {entry["name"]: entry["id"] for entry in api.request("GET", f"{base}/rulesets?per_page=100") or []}
    for payload in github_rulesets(r):
        name = payload["name"]
        if name in existing:
            yield from write("PUT", f"{base}/rulesets/{existing[name]}", payload,
                             f"updated ruleset '{name}'", f"update ruleset '{name}'")
        else:
            yield from write("POST", f"{base}/rulesets", payload, f"created ruleset '{name}'", f"create ruleset '{name}'")

    # The previous, ad-hoc protection of the default branch: the rulesets above replace it, and it
    # would otherwise start protecting develop, with its own different rules, once develop is the default.
    if "default" in existing:
        yield from write("DELETE", f"{base}/rulesets/{existing['default']}", None,
                         "removed ruleset 'default'", "remove ruleset 'default'")


def apply_github(api, owner, r, dry_run):
    """Sets one repository's settings and rulesets on GitHub; rulesets are matched by name and
    updated in place. Returns the lines of what it did (or, with dry_run, would do: no write is sent)."""
    return list(_github_steps(api, owner, r, dry_run))


# --- Command line -------------------------------------------------------------------------------------

USAGE = "usage: python3 scripts/rules.py [--dry-run] [<repo>...]"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    bad_options = [a for a in argv if a.startswith("-") and a != "--dry-run"]
    if bad_options:
        sys.exit(f"unknown option: {', '.join(bad_options)}\n{USAGE}")
    dry_run = "--dry-run" in argv
    only = [a for a in argv if not a.startswith("-")]

    try:
        catalog = catalog_module.load()
    except catalog_module.CatalogError as error:
        sys.exit(str(error))
    modelled = desired_rules(catalog)

    unknown = set(only) - {r.repo for r in modelled}
    if unknown:
        sys.exit(f"not repositories in catalog.yaml: {', '.join(sorted(unknown))}")

    forge = catalog_module.forge(catalog)
    if forge["type"] != "github":
        print("the Forgejo side arrives in a later task", file=sys.stderr)
        sys.exit(2)

    api = Api(forge["api"], github_token(), "Bearer", GITHUB_HEADERS)
    for r in modelled:
        if only and r.repo not in only:
            continue
        try:
            for line in _github_steps(api, catalog["owner"], r, dry_run):
                print(line, flush=True)
        except ApiError as error:
            sys.exit(f"{r.repo}: {error}")


if __name__ == "__main__":
    main()
