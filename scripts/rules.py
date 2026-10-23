#!/usr/bin/env python3
"""Applies the branching model's rules to every repository in catalog.yaml, on GitHub or Forgejo.

    python3 scripts/rules.py [--dry-run] [<repo>...]

This file holds the model: what the rules are, worked out from the catalog (desired_rules). The
command line and the two git servers' APIs (GitHub rulesets, Forgejo branch and tag protections)
arrive with the GitHub and Forgejo sides; until then it only describes the rules and imports
without network or tokens.

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
"""

import dataclasses
import pathlib
import sys

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
