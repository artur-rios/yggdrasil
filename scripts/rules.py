#!/usr/bin/env python3
"""Applies the branching model's rules to every repository in catalog.yaml, on GitHub or Forgejo.

    python3 scripts/rules.py [--dry-run] [<repo>...]

This file holds the model: what the rules are, worked out from the catalog (desired_rules), and
the git servers' sides that apply it: GitHub rulesets (github_rulesets, apply_github) and Forgejo
settings, branch and tag protections and the Jenkins webhook (apply_forgejo). Importing it needs no
network or token; the token is read when the command runs (GitHub: GH_TOKEN, else `gh auth token`;
Forgejo: FORGE_TOKEN) and goes only in the Authorization header, never in a process argument, a log
or the --dry-run output. --dry-run still reads (GET) from the server, so it needs the token, but
sends no write.

On Forgejo, JENKINS_URL and FORGE_WEBHOOK_SECRET (both or neither) add the webhook that tells
Jenkins about pushes, tags and pull requests: <JENKINS_URL>/gitea-webhook/post, one per repository,
updated in place. Forgejo never returns a webhook's secret, so it cannot be compared: an existing
hook is updated only when its url, events, content type, branch filter or active flag differ, and a
changed secret needs the hook deleted (the next run creates it again). The secret is never printed.

Per repository the model is:

  develop        no direct or force push, no deletion; changes through a pull request whose checks
                 pass (branch-policy, the catalog's checks, and its actionsChecks); merged by the
                 owner and forge.developers
  main           the same, with branch-policy, checks and deploy/<environment> for each release
                 environment instead of actionsChecks; merged only by the Jenkins user for an
                 application, by the owner for a repository that tags its own releases
  release/*      only the owner pushes
  version tags   created by the Jenkins user (applications) or the owner (owner-tag repositories)
  Jenkinsfile    protected on develop (Forgejo): only the owner merges a change to it

A repository with `rules: basic` gets develop and main without force push or deletion, and no other
rule. It still gets the repository settings (default branch develop, head branches deleted after
merge; on Forgejo the merge styles too) and, on Forgejo when configured, the Jenkins webhook: Jenkins
builds it too.

On Forgejo a user is kept on a whitelist (merging, pushing, creating tags) only while they have write
access to the repository: Forgejo leaves anyone else out without an error. The run says so, by name,
when the server's answer is missing someone it was sent -- give that user write access (a
collaborator) and run again.

GitHub cannot restrict who may merge a given file, so `protected_files` (the Jenkinsfile) is not
applied there; Jenkins' trust rule (plan 3) covers it. Nor does GitHub get a release/* rule: there
is none today. On GitHub only the actionsChecks are bound to GitHub Actions; Jenkins' contexts
(branch-policy, checks, deploy/<environment>) are bound to nobody.

On Forgejo the owner's emergency override is a forced merge (the web interface's "Merge anyway",
`force_merge` in the API): the protections do not apply to administrators, but a plain merge is
refused while a required check fails. Reference: docs/setup.md, step 12.
"""

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
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
    protected_files: tuple       # ("Jenkinsfile",): protected on develop only, see forgejo_branch_protections
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


# --- Forgejo: settings, protections, tags, webhook ----------------------------------------------------

# What Jenkins is told about: branches and tags appearing and disappearing, pushes, pull requests.
WEBHOOK_EVENTS = ["create", "delete", "push", "pull_request"]


def forgejo_settings():
    """The repository settings this script owns: develop is the default branch, merged branches are
    deleted, merge commits (and squashes, for develop) are allowed, rebasing is not."""
    return {"default_branch": "develop", "default_delete_branch_after_merge": True, "allow_merge_commits": True,
            "allow_squash_merge": True, "allow_rebase": False, "allow_rebase_explicit": False,
            "default_merge_style": "merge"}


def forgejo_branch_protections(r):
    """The branch protections of one repository, `rule_name` being the branch pattern. Every one leaves
    the owner (an administrator) unbound -- `apply_to_admins: false` -- for emergencies. Forgejo blocks
    force push and deletion under any protection."""
    if r.model == "basic":
        return [{"rule_name": name, "enable_push": True, "apply_to_admins": False} for name in ("develop", "main")]

    def guarded(name, mergers, checks, files):
        # Nobody pushes: a change gets in through a pull request that the checks passed, merged by
        # the listed users. `files` are protected files: a pull request that changes one can be merged
        # only by someone who may push to the branch (the owner), whoever else is on the merge list.
        return {"rule_name": name, "enable_push": False,
                "enable_merge_whitelist": True, "merge_whitelist_usernames": list(mergers),
                "enable_status_check": True, "status_check_contexts": list(checks),
                "required_approvals": 0, "protected_file_patterns": ";".join(files),
                "apply_to_admins": False}

    # The Jenkinsfile is protected on develop only: every change to it reaches develop through a merge
    # the owner makes. main receives only snapshots of develop, merged by Jenkins, which a protected
    # file there would stop whenever a release changes the Jenkinsfile.
    return [guarded("develop", r.develop_mergers, r.develop_checks, r.protected_files),
            guarded("main", r.main_mergers, r.main_checks, ()),
            # Release branches are pushed to directly, by the owner alone.
            {"rule_name": "release/*", "enable_push": True, "enable_push_whitelist": True,
             "push_whitelist_usernames": list(r.release_pushers), "enable_merge_whitelist": False,
             "apply_to_admins": False}]


def forgejo_tag_protection(r):
    """Who may create the repository's version tags (release model only: a basic repository has none)."""
    return {"name_pattern": r.tag_pattern, "whitelist_usernames": list(r.tag_creators)}


def forgejo_webhook(webhook):
    """The hook Jenkins' Gitea plugin listens to: `webhook` is {"url", "secret"}."""
    return {"type": "gitea", "active": True, "events": list(WEBHOOK_EVENTS), "branch_filter": "*",
            "config": {"url": webhook["url"], "content_type": "json", "secret": webhook["secret"]}}


def _differs(current, desired):
    """Whether the server's object differs from the desired one in a field the desired one sets. The
    server answers with many more fields, and lists (whitelists, events) in any order, so only the
    desired fields are compared, lists as sets."""
    for key, want in desired.items():
        have = current.get(key)
        if isinstance(want, list):
            if set(have or []) != set(want):
                return True
        elif have != want:
            return True
    return False


def _write_step(api, dry_run, method, path, body, done, would, shown=None, what=None):
    """One write as lines: sent (and `done` reported), or with dry_run only described. `shown` is the
    body to print when it differs from the one sent (so a secret can be masked). `what` names the
    protection, for the warning about users the server left off its whitelists."""
    if dry_run:
        yield f"  would {would}: {method} {path}"
        if body is not None:
            yield "    " + json.dumps(body if shown is None else shown)
    else:
        answer = api.request(method, path, body)
        yield f"  {done}"
        if what is not None:
            yield from _left_out(body, answer or {}, what)


# The whitelists of branch and tag protections: Forgejo keeps only users with write access on them.
WHITELISTS = {"merge_whitelist_usernames": "merge", "push_whitelist_usernames": "push",
              "whitelist_usernames": "create tags"}


def _left_out(sent, answer, what):
    """A warning for each whitelist the server's answer is missing a user of. Forgejo drops a user
    without write access to the repository without saying so; the protection would then look changed
    on every run, and the user could not do what the model says they do."""
    for field, verb in WHITELISTS.items():
        missing = sorted(set(sent.get(field) or []) - set(answer.get(field) or []))
        if missing:
            yield (f"  warning: Forgejo left {', '.join(missing)} off who may {verb} ({what}): "
                   "give them write access to the repository and run again")


def _forgejo_steps(api, owner, r, webhook, dry_run):
    """What applying one repository does, as lines, as it happens (a generator, like _github_steps).
    A write is sent only when the server's object differs in a field this script owns."""
    base = f"/repos/{owner}/{r.repo}"
    yield f"{owner}/{r.repo}"

    def write(*args, **kwargs):
        return _write_step(api, dry_run, *args, **kwargs)

    settings = forgejo_settings()
    if _differs(api.request("GET", base) or {}, settings):
        yield from write("PATCH", base, settings, "default branch develop, delete head branches on merge, merge settings",
                         "set the default branch to develop, delete head branches on merge and set the merge settings")
    else:
        yield "  repository settings unchanged"

    existing = {p.get("rule_name") or p.get("branch_name"): p
                for p in api.request("GET", f"{base}/branch_protections") or []}
    for payload in forgejo_branch_protections(r):
        name = payload["rule_name"]
        body = {k: v for k, v in payload.items() if k != "rule_name"}
        if name not in existing:
            yield from write("POST", f"{base}/branch_protections", payload, f"protected branch '{name}'",
                             f"protect branch '{name}'", what=f"branch '{name}'")
        elif _differs(existing[name], body):
            # The pattern goes in the path as one segment: release/* must not read as a sub-path.
            yield from write("PATCH", f"{base}/branch_protections/{urllib.parse.quote(name, safe='')}", body,
                             f"updated protection of branch '{name}'", f"update the protection of branch '{name}'",
                             what=f"branch '{name}'")
        else:
            yield f"  protection of branch '{name}' unchanged"

    if r.model == "release":
        tag = forgejo_tag_protection(r)
        found = {t["name_pattern"]: t for t in api.request("GET", f"{base}/tag_protections") or []}.get(r.tag_pattern)
        if found is None:
            yield from write("POST", f"{base}/tag_protections", tag, f"protected tags '{r.tag_pattern}'",
                             f"protect tags '{r.tag_pattern}'", what=f"tags '{r.tag_pattern}'")
        elif _differs(found, tag):
            yield from write("PATCH", f"{base}/tag_protections/{found['id']}", tag,
                             f"updated tag protection '{r.tag_pattern}'", f"update the tag protection '{r.tag_pattern}'",
                             what=f"tags '{r.tag_pattern}'")
        else:
            yield f"  tag protection '{r.tag_pattern}' unchanged"

    if webhook is not None:
        yield from _webhook_steps(api, base, webhook, write)


def _hook_events(answered, sent):
    """A hook's events as they were sent. Forgejo stores "pull_request" as the whole group and answers
    with it spelled out (pull_request, pull_request_assign, pull_request_sync, ...): an event that is a
    sent one's sub-event is folded back into it, so an unchanged hook does not look changed."""
    return [event for event in answered
            if event in sent or not any(event.startswith(f"{group}_") for group in sent)]


def _webhook_steps(api, base, webhook, write):
    desired = forgejo_webhook(webhook)
    # Shown instead of the body when describing a write: the secret is never printed.
    shown = {**desired, "config": {**desired["config"], "secret": "***"}}
    found = next((h for h in api.request("GET", f"{base}/hooks") or []
                  if (h.get("config") or {}).get("url") == webhook["url"]), None)
    if found is None:
        yield from write("POST", f"{base}/hooks", desired, "created the Jenkins webhook", "create the Jenkins webhook",
                         shown=shown)
        return
    # Forgejo does not return a hook's secret, so it cannot be compared: only the fields it does return
    # decide whether the hook is rewritten (and the secret with it).
    owned = {k: desired[k] for k in ("active", "events", "branch_filter")}
    owned["content_type"] = desired["config"]["content_type"]
    seen = {**found, "content_type": (found.get("config") or {}).get("content_type"),
            "events": _hook_events(found.get("events") or [], desired["events"])}
    if _differs(seen, owned):
        yield from write("PATCH", f"{base}/hooks/{found['id']}", desired, "updated the Jenkins webhook",
                         "update the Jenkins webhook", shown=shown)
    else:
        yield "  Jenkins webhook unchanged (its secret cannot be read: to change it, delete the hook and run again)"


def apply_forgejo(api, owner, r, webhook, dry_run):
    """Sets one repository's settings, branch protections, tag protection and (webhook given as
    {"url", "secret"}) Jenkins webhook on Forgejo. Returns the lines of what it did (or, with dry_run,
    would do: no write is sent). The secret is not in any of them."""
    return list(_forgejo_steps(api, owner, r, webhook, dry_run))


# --- Command line -------------------------------------------------------------------------------------

USAGE = """usage: python3 scripts/rules.py [--dry-run] [<repo>...]

Applies the branching model's rules to every repository of catalog.yaml (or only the <repo>s given) on
the catalog's git server. --dry-run reads from the server but sends no write.

  GitHub (no forge block, or forge.type github)
    GH_TOKEN         a token with repository administration; else `gh auth token` is used
  Forgejo (forge.type forgejo)
    FORGE_TOKEN      a token with write:repository (required)
    JENKINS_URL, FORGE_WEBHOOK_SECRET
                     both or neither: they add the Jenkins webhook <JENKINS_URL>/gitea-webhook/post;
                     with neither, webhooks are skipped. A hook's secret cannot be read back, so to
                     change it delete the hook in Forgejo and run again.

The tokens and the secret are never printed."""


def _forgejo_connection(forge):
    """(api, webhook, secret): the Forgejo client and, when JENKINS_URL and FORGE_WEBHOOK_SECRET are
    set, the webhook to apply (else None). Refuses before any request on a missing token or only one of
    the two webhook variables."""
    token = os.environ.get("FORGE_TOKEN", "").strip()
    if not token:
        sys.exit("no Forgejo token: set FORGE_TOKEN")
    jenkins = os.environ.get("JENKINS_URL", "").strip()
    secret = os.environ.get("FORGE_WEBHOOK_SECRET", "").strip()
    if bool(jenkins) != bool(secret):
        sys.exit("set JENKINS_URL and FORGE_WEBHOOK_SECRET together (or neither, to skip webhooks): "
                 + ("FORGE_WEBHOOK_SECRET" if jenkins else "JENKINS_URL") + " is missing")
    webhook = {"url": jenkins.rstrip("/") + "/gitea-webhook/post", "secret": secret} if jenkins else None
    return Api(forge["api"], token, "token"), webhook, secret


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
        catalog = catalog_module.load()
    except catalog_module.CatalogError as error:
        sys.exit(str(error))
    modelled = desired_rules(catalog)

    unknown = set(only) - {r.repo for r in modelled}
    if unknown:
        sys.exit(f"not repositories in catalog.yaml: {', '.join(sorted(unknown))}")

    forge = catalog_module.forge(catalog)
    secret = ""
    if forge["type"] == "forgejo":
        api, webhook, secret = _forgejo_connection(forge)
        if webhook is None:
            print("webhooks skipped: JENKINS_URL and FORGE_WEBHOOK_SECRET are not set", flush=True)

        def steps(r):
            return _forgejo_steps(api, catalog["owner"], r, webhook, dry_run)
    else:
        api = Api(forge["api"], github_token(), "Bearer", GITHUB_HEADERS)

        def steps(r):
            return _github_steps(api, catalog["owner"], r, dry_run)

    for r in modelled:
        if only and r.repo not in only:
            continue
        try:
            for line in steps(r):
                print(line, flush=True)
        except ApiError as error:
            # A server may echo what it was sent: the webhook secret is masked like the token is.
            sys.exit(f"{r.repo}: {str(error).replace(secret, '***') if secret else error}")


if __name__ == "__main__":
    main()
