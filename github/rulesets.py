#!/usr/bin/env python3
"""Applies the branching model's GitHub settings to every application repository in catalog.yaml.

    python github/rulesets.py                 # apply to every catalog repository
    python github/rulesets.py --dry-run       # print what would be sent
    python github/rulesets.py heimdall-api    # only the named repositories

Needs PyYAML (pip install pyyaml; Ubuntu: apt install python3-yaml).

Idempotent: rulesets are matched by name and updated in place. Needs an authenticated `gh` with
admin rights on the repositories. Per repository it sets:

  develop        default branch; no deletion, no force push; changes only through a pull request
                 (squash or merge) whose checks pass -- Branch Policy included, which is what limits
                 the source to feature/ and fix/ branches cut from develop
  main           no deletion, no force push; changes only through a pull request, merge commit
                 only, whose checks pass -- Branch Policy (release/x.y.z snapshots of develop only)
                 and deploy/production, the status Jenkins sets once the release is live. So main
                 cannot receive a release that has not been deployed.
  tags v*        cannot be moved or deleted once created: a version names one commit forever
  settings       head branches are deleted when their pull request merges

Repository administrators (the owner) bypass all three rulesets -- for emergencies.

Any ruleset named "default" (the previous, ad-hoc protection of the default branch) is removed,
since the rulesets above replace it and it would otherwise start protecting develop, with its own
different rules, as soon as develop becomes the default branch.
"""

import json
import pathlib
import subprocess
import sys

import yaml

CATALOG = pathlib.Path(__file__).resolve().parent.parent / "catalog.yaml"

# GitHub Actions' app id: required checks bound to it cannot be satisfied by a status someone posts
# under the same name from elsewhere.
GITHUB_ACTIONS = 15368

# The required checks of each repository come from the catalog's `checks`. Check names are job names
# (or a job's `name:`). Only checks that run on every pull request can be required -- a
# path-filtered workflow that does not run leaves its check pending forever, which is why
# heimdall-api's Check OpenAPI Document is not listed there. Jenkins still waits for every check
# that does run before deploying (scripts/github.sh wait-checks).
def load_catalog():
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    repositories = {}
    for system in catalog["systems"]:
        for app in system["applications"]:
            if app.get("kind") == "platform":
                continue  # not a repository of its own; deployed by scripts/platform.sh
            repositories[app.get("repository", app["id"])] = app.get("checks", [])
    return catalog["owner"], repositories


ADMIN_BYPASS = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]


def checks(names, deploy=False):
    required = [{"context": "branch-policy", "integration_id": GITHUB_ACTIONS}]
    required += [{"context": name, "integration_id": GITHUB_ACTIONS} for name in names]
    if deploy:
        # Posted by Jenkins through its GitHub App; not bound to an integration so the app can be
        # replaced without editing this.
        required.append({"context": "deploy/production"})
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": required,
        },
    }


def pull_request(methods):
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


def rulesets(names):
    return [
        {
            "name": "develop",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": ADMIN_BYPASS,
            "conditions": {"ref_name": {"include": ["refs/heads/develop"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                pull_request(["squash", "merge"]),
                checks(names),
            ],
        },
        {
            "name": "main",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": ADMIN_BYPASS,
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                # Merge commits only: squashing a release would give main commits develop never had,
                # and the next release branch would conflict.
                pull_request(["merge"]),
                checks(names, deploy=True),
            ],
        },
        {
            "name": "release tags",
            "target": "tag",
            "enforcement": "active",
            "bypass_actors": ADMIN_BYPASS,
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "update"}, {"type": "deletion"}],
        },
    ]


def gh(*args, body=None, dry_run=False):
    if dry_run and args[0] == "api" and any(a in ("POST", "PUT", "PATCH", "DELETE") for a in args):
        print("  would call:", " ".join(args))
        if body is not None:
            print("  " + json.dumps(body))
        return None
    result = subprocess.run(
        ["gh", *args] + (["--input", "-"] if body is not None else []),
        input=json.dumps(body) if body is not None else None,
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def apply(owner, repo, names, dry_run):
    print(f"{owner}/{repo}")
    base = f"repos/{owner}/{repo}"

    gh("api", "-X", "PATCH", base, body={"default_branch": "develop", "delete_branch_on_merge": True}, dry_run=dry_run)
    print("  default branch develop, delete head branches on merge")

    existing = {r["name"]: r["id"] for r in gh("api", f"{base}/rulesets") or []}
    for ruleset in rulesets(names):
        if ruleset["name"] in existing:
            gh("api", "-X", "PUT", f"{base}/rulesets/{existing[ruleset['name']]}", body=ruleset, dry_run=dry_run)
            print(f"  updated ruleset '{ruleset['name']}'")
        else:
            gh("api", "-X", "POST", f"{base}/rulesets", body=ruleset, dry_run=dry_run)
            print(f"  created ruleset '{ruleset['name']}'")

    if "default" in existing:
        gh("api", "-X", "DELETE", f"{base}/rulesets/{existing['default']}", dry_run=dry_run)
        print("  removed ruleset 'default'")


def main():
    dry_run = "--dry-run" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    owner, repositories = load_catalog()
    unknown = set(only) - set(repositories)
    if unknown:
        sys.exit(f"not application repositories in catalog.yaml: {', '.join(sorted(unknown))}")
    for repo, names in repositories.items():
        if not only or repo in only:
            apply(owner, repo, names, dry_run)


if __name__ == "__main__":
    main()
