#!/usr/bin/env python3
"""Deletes the release branches Forgejo keeps after a release.

    python3 scripts/prune_release_branches.py [--dry-run] [<repo>...]

Forgejo refuses to delete a branch a protection covers, to everyone, administrators included, and has no
setting to allow it, so the pipeline's `delete-branch` leaves every release/x.y.z behind. This does what the
owner would do by hand (docs/setup.md, "The git server commands"): for each repository with something to
delete it removes the release/* protection, deletes the branches, and puts the protection back as the
model in rules.py has it. The protection is off only for those moments, and only where a branch goes.

A branch is deleted only when all of these hold, and is otherwise kept and the run says why:

  - no open pull request comes from it or goes into it;
  - its tip is in main;
  - a version tag (the repository's tag pattern, as rules.py has it) contains its tip. Any such tag: a
    release named by date has no tag of its own name, and a later release's tag holds its commits.

It covers the catalog's release-model repositories (a basic one has no release/* rule), or those named.
FORGE_TOKEN must be the owner's: only a repository administrator may change a protection. It is read as
rules.py reads it, and goes only in the Authorization header. --dry-run reads from the server, so it needs
the token too, but sends no write.

A branch the server will not delete is reported, the others still go, the protection still comes back,
and the run exits 1 at the end. A protection that cannot be put back stops the run at once, so no further
repository is opened up, and names the command that restores it."""

import fnmatch
import os
import sys
import time
import urllib.parse

import rules

RELEASE_RULE = "release/*"
PAGE_SIZE = 50
# Putting a protection back is the step that must not be lost to a moment's network trouble.
RESTORE_ATTEMPTS = 3
RESTORE_WAIT = 2  # seconds

USAGE = __doc__


class NotRestored(Exception):
    """The release/* protection of a repository was removed and could not be put back."""


def _paged(api, path):
    """Every item of a list the server answers in pages. Asks until a page comes back empty: the server may
    cap a page below PAGE_SIZE, so a short page does not mean the last one."""
    separator = "&" if "?" in path else "?"
    page = 1
    while True:
        items = api.request("GET", f"{path}{separator}limit={PAGE_SIZE}&page={page}") or []
        if not items:
            return
        yield from items
        page += 1


def _contains(api, base, outer, inner):
    """Whether commit `inner` is in the history of commit `outer`: nothing is ahead of it. Compared by
    commit, not by name, so a tag such as Some.Package@1.0.0 needs no encoding."""
    return (api.request("GET", f"{base}/compare/{outer}...{inner}") or {}).get("total_commits") == 0


def _why_kept(api, base, name, tip, main, pull_refs, tag_commits):
    """Why a release branch must stay, or None when it can go."""
    if name in pull_refs:
        return "an open pull request uses it"
    if main is None:
        return "the repository has no main to compare it with"
    if not _contains(api, base, main, tip):
        return "it is not in main"
    if not any(_contains(api, base, commit, tip) for commit in tag_commits):
        return "no version tag contains it"
    return None


def _candidates(api, base, r, say):
    """The release branches of a repository that can go; says why each of the others stays."""
    branches = {b["name"]: b["commit"]["id"] for b in _paged(api, f"{base}/branches")}
    release = sorted(name for name in branches if name.startswith("release/"))
    if not release:
        return []
    pull_refs = set()
    for pull in _paged(api, f"{base}/pulls?state=open"):
        pull_refs.update((pull["head"]["ref"], pull["base"]["ref"]))
    # Several packages tagged on one commit (name@version) need asking about only once.
    tag_commits = list(dict.fromkeys(t["commit"]["sha"] for t in _paged(api, f"{base}/tags")
                                     if fnmatch.fnmatchcase(t["name"], r.tag_pattern)))
    doomed = []
    for name in release:
        reason = _why_kept(api, base, name, branches[name], branches.get("main"), pull_refs, tag_commits)
        if reason:
            say(f"  kept {name}: {reason}")
        else:
            doomed.append(name)
    return doomed


def _restore(api, base, r):
    """Puts the release/* protection back as the model has it."""
    payload = next(p for p in rules.forgejo_branch_protections(r) if p["rule_name"] == RELEASE_RULE)
    for attempt in range(1, RESTORE_ATTEMPTS + 1):
        try:
            api.request("POST", f"{base}/branch_protections", payload)
            return
        except rules.ApiError as error:
            if attempt == RESTORE_ATTEMPTS:
                raise NotRestored(str(error)) from None
            time.sleep(RESTORE_WAIT)


def prune_repository(api, owner, r, dry_run, say):
    """Deletes the release branches of one repository that can go. The ones the server refused, as a list."""
    base = f"/repos/{owner}/{r.repo}"
    say(f"{owner}/{r.repo}")
    doomed = _candidates(api, base, r, say)
    if not doomed:
        say("  nothing to delete")
        return []
    if dry_run:
        for name in doomed:
            say(f"  would delete {name}")
        return []

    protected = any((p.get("rule_name") or p.get("branch_name")) == RELEASE_RULE
                    for p in api.request("GET", f"{base}/branch_protections") or [])
    if protected:
        api.request("DELETE", f"{base}/branch_protections/{urllib.parse.quote(RELEASE_RULE, safe='')}")
    refused = []
    try:
        for name in doomed:
            try:
                api.request("DELETE", f"{base}/branches/{urllib.parse.quote(name, safe='')}")
                say(f"  deleted {name}")
            except rules.ApiError as error:
                refused.append(name)
                say(f"  NOT deleted {name}: {error}")
    finally:
        if protected:
            _restore(api, base, r)
            say(f"  protection of '{RELEASE_RULE}' restored")
    return refused


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        sys.exit(0)
    bad_options = [a for a in argv if a.startswith("-") and a != "--dry-run"]
    if bad_options:
        sys.exit(f"unknown option: {', '.join(bad_options)}\n{USAGE}")
    dry_run = "--dry-run" in argv
    only = [a for a in argv if not a.startswith("-")]

    try:
        catalog = rules.catalog_module.load()
    except rules.catalog_module.CatalogError as error:
        sys.exit(str(error))
    forge = rules.catalog_module.forge(catalog)
    if forge["type"] != "forgejo":
        sys.exit("this is for Forgejo: GitHub has no release/* rule, so the pipeline deletes the branches itself")

    modelled = [r for r in rules.desired_rules(catalog) if r.model == "release"]
    unknown = set(only) - {r.repo for r in modelled}
    if unknown:
        sys.exit(f"not release-model repositories in catalog.yaml: {', '.join(sorted(unknown))}")

    token = os.environ.get("FORGE_TOKEN", "").strip()
    if not token:
        sys.exit("no Forgejo token: set FORGE_TOKEN (the owner's: changing a protection needs an administrator)")
    api = rules.Api(forge["api"], token, "token")

    def say(line):
        print(line, flush=True)

    refused = []
    for r in modelled:
        if only and r.repo not in only:
            continue
        try:
            refused += [f"{r.repo}: {name}" for name in prune_repository(api, catalog["owner"], r, dry_run, say)]
        except NotRestored as error:
            sys.exit(f"{r.repo}: the {RELEASE_RULE} protection is OFF and could not be put back ({error}). "
                     f"Restore it with: python3 scripts/rules.py {r.repo}")
        except rules.ApiError as error:
            sys.exit(f"{r.repo}: {error}")
    if refused:
        sys.exit(f"not deleted (each one's error is above): {', '.join(refused)}")


if __name__ == "__main__":
    main()
