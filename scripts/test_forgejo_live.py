"""Tests of scripts/forge.sh and scripts/rules.py against a real Forgejo.

    YGG_FORGEJO_TESTS=1 python3 -m unittest discover -s scripts -p test_forgejo_live.py -v

test_forge.py and test_rules.py check what the two send against fakes; these check that the fakes are
right: that Forgejo 16.0.6 (the image platform/compose.yml pins) takes those requests, answers the way
the scripts read it, and that the protections rules.py applies really hold -- `claude` cannot merge a
Jenkinsfile change, push a release branch or create, move or delete a version tag, a `rules: basic`
repository refuses force pushes and deletions, and the owner, an administrator, can still merge past
failing checks. Seven tests check migrate.py against this server (below): import (two), prepare-github's
Forgejo side (Dependabot's file deleted), push mirrors, the guard before a mirror's first push (two: what
only GitHub has, and GitHub branches whose commit is not in Forgejo's branch of that name -- one
fast-forwarded to another branch's commit, one unrelated to an orphan branch -- whose unsynced mirror is
then deleted), and collaborators. One test pins a known gap instead: jenkins, which creates the version
tags, can also move and delete them (docs/setup.md, step 12).

Skipped unless YGG_FORGEJO_TESTS=1 (and Docker, bash, jq and git are there). The suite starts one
container of the pinned image, named ygg-forgejo-test-*, on a free local port, with SQLite and the
compose file's sign-in settings; creates the users artur-rios (the owner, an administrator), jenkins
and claude, with tokens, and the repositories shop-api (an application) and reveal.js (a `rules:
basic` repository, with a dot in its name), each with main, develop and a Jenkinsfile; applies the
rules once (one more test makes a repository of its own); and removes the container at the end, also
when the setup fails. It touches no other container. The passwords and tokens are throwaway values of
that container. CI runs it (job platform).
"""

import base64
import contextlib
import dataclasses
import io
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from unittest import mock

import catalog as catalog_module
import migrate
import rules
from test_migrate import FakeGitHub, repository as github_repository

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORGE_SH = ROOT / "scripts" / "forge.sh"


def pinned_image():
    """The Forgejo image of platform/compose.yml, version and digest: what production runs."""
    found = re.search(r"^\s*image:\s*(codeberg\.org/forgejo/forgejo:\S+)\s*$",
                      (ROOT / "platform" / "compose.yml").read_text(encoding="utf-8"), re.MULTILINE)
    if not found:
        raise AssertionError("platform/compose.yml has no codeberg.org/forgejo/forgejo image")
    return found.group(1)


OWNER, JENKINS, CLAUDE = "artur-rios", "jenkins", "claude"
APP, DOTTED = "shop-api", "reveal.js"
WEBHOOK = {"url": "http://jenkins.invalid/gitea-webhook/post", "secret": "a-throwaway-webhook-secret"}

JENKINSFILE = "pipeline {\n  agent any\n  stages { stage('test') { steps { sh 'true' } } }\n}\n"


def test_catalog():
    """The catalog of the installation under test: claude is a developer (it merges into develop),
    jenkins the Jenkins user; shop-api has a check, an Actions check and a release environment."""
    return {
        "owner": OWNER,
        # rules.py only reads forge.jenkinsUser and developers from it; the API is the container's.
        "forge": {"type": "forgejo", "url": "https://git.example.com", "jenkinsUser": JENKINS, "developers": [CLAUDE]},
        "environments": [
            {"id": "staging", "name": "Staging", "trigger": "branch", "branches": "release/*"},
            {"id": "production", "name": "Production", "trigger": "release"},
        ],
        "systems": [{"id": "shop", "name": "Shop", "applications": [
            {"id": APP, "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz",
             "checks": ["test"], "actionsChecks": ["lint"]},
        ]}],
        "repositories": [{"id": DOTTED, "rules": "basic"}],
    }


class GitHubOnForgejo(FakeGitHub):
    """The GitHub migrate.py asks, with its refs read from this very Forgejo: GitHub's `git/matching-refs/<prefix>`
    becomes Forgejo's `git/refs/<prefix>` on the repository that `stands_in` names (a repository's own name when
    it names none). GitHub's client cannot be pointed at Forgejo as it is (the two APIs differ), and this is
    the one route verify needs from it that Forgejo has; the rest -- the repository list, the rulesets, the
    issues -- is FakeGitHub's."""

    def __init__(self, repos, forgejo, stands_in=None, **fields):
        super().__init__(repos, **fields)
        self.forgejo, self.stands_in = forgejo, stands_in or {}

    def request(self, method, path, body=None):
        before, found, after = path.partition("/git/matching-refs/")
        if not found:
            return super().request(method, path, body)
        self.calls.append((method, path, body))
        _, _, name = before.rpartition("/")
        stand_in = self.stands_in.get(name, name)
        return self.forgejo.request(method, f"/repos/{OWNER}/{stand_in}/git/refs/{after}")


def docker(*arguments, check=True, timeout=300):
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=check, timeout=timeout)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RecordingApi(rules.Api):
    """rules.Api that keeps the method and path of every request, to see what a run sent."""

    def __init__(self, *arguments, **keywords):
        super().__init__(*arguments, **keywords)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        return super().request(method, path, body)

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def _can_run():
    return (os.environ.get("YGG_FORGEJO_TESTS") == "1" and os.name == "posix"
            and all(shutil.which(tool) for tool in ("docker", "bash", "jq", "git", "curl")))


class ForgejoClient:
    """What a live suite does on its Forgejo container as its users: health, admin commands, commits,
    branches, pull requests and git over HTTP(S). Shared with test_jenkins_live.py. The class using it
    sets `container`, `url` (the server as the tests reach it), `api` and `tokens` (by user) and `temp`;
    `git_ca`, when set, is the CA file git trusts (a Forgejo served over HTTPS)."""

    git_ca = None

    @classmethod
    def wait_for_health(cls, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(cls.url + "/api/healthz", timeout=5) as answer:
                    if json.loads(answer.read()).get("status") == "pass":
                        return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        logs = docker("logs", "--tail=50", cls.container, check=False)
        raise AssertionError(f"Forgejo did not become healthy:\n{logs.stdout}{logs.stderr}")

    @classmethod
    def admin_cli(cls, *arguments):
        done = docker("exec", cls.container, "forgejo", "admin", "user", *arguments, check=False, timeout=120)
        if done.returncode:
            raise AssertionError(f"forgejo admin user {arguments[0]} failed: {done.stdout}{done.stderr}")
        return done.stdout

    @classmethod
    def change_file(cls, user, repo, path, text, branch, new_branch=None):
        """A commit by `user` writing `path` on `branch` (or on `new_branch`, made from it); its sha."""
        api = cls.api[user]
        contents = f"/repos/{OWNER}/{repo}/contents/{urllib.parse.quote(path)}"
        try:
            existing = api.request("GET", f"{contents}?ref={urllib.parse.quote(branch, safe='')}")
        except rules.ApiError as error:
            if error.status != 404:
                raise
            existing = None
        body = {"content": base64.b64encode(text.encode()).decode(), "message": f"change {path}", "branch": branch}
        if new_branch:
            body["new_branch"] = new_branch
        if existing:
            answer = api.request("PUT", contents, {**body, "sha": existing["sha"]})
        else:
            answer = api.request("POST", contents, body)
        return answer["commit"]["sha"]

    def head(self, repo, branch):
        return self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/branches/{urllib.parse.quote(branch, safe='')}"
                                       )["commit"]["id"]

    def branch_exists(self, repo, branch):
        try:
            self.head(repo, branch)
            return True
        except rules.ApiError as error:
            if error.status == 404:
                return False
            raise

    def open_pull_request(self, user, repo, head, base):
        number = self.api[user].request("POST", f"/repos/{OWNER}/{repo}/pulls",
                                        {"head": head, "base": base, "title": f"{head} into {base}"})["number"]
        # Forgejo works out whether it merges in the background; merging before that is refused.
        deadline = time.monotonic() + 60
        while not self.pull_request(repo, number).get("mergeable"):
            if time.monotonic() > deadline:
                self.fail(f"pull request #{number} of {repo} never became mergeable")
            time.sleep(0.5)
        return number

    def pull_request(self, repo, number):
        return self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/pulls/{number}")

    def send(self, user, method, path, body=None):
        """The request, tried again while Forgejo is still working out whether a pull request can be
        merged (405 "Please try again later", which a new commit status sets off): a refusal must be
        the rule's, not that one."""
        deadline = time.monotonic() + 60
        while True:
            try:
                return self.api[user].request(method, path, body)
            except rules.ApiError as error:
                if error.status != 405 or "try again later" not in error.message or time.monotonic() > deadline:
                    raise
            time.sleep(1)

    def merge(self, user, repo, number, **fields):
        self.send(user, "POST", f"/repos/{OWNER}/{repo}/pulls/{number}/merge", {"Do": "merge", **fields})

    @classmethod
    def git(cls, user, *arguments, cwd=None, env=None):
        """git as `user` over HTTP(S). The credentials go in an extra header set through the environment
        (never an argument), and the machine's own git configuration is left out. `env` adds to the
        environment (GIT_COMMITTER_DATE, say)."""
        credentials = base64.b64encode(f"{user}:{cls.tokens[user]}".encode()).decode()
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
                           GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                           GIT_CONFIG_VALUE_0=f"Authorization: Basic {credentials}",
                           GIT_AUTHOR_NAME=user, GIT_AUTHOR_EMAIL=f"{user}@example.com",
                           GIT_COMMITTER_NAME=user, GIT_COMMITTER_EMAIL=f"{user}@example.com", **(env or {}))
        if cls.git_ca:
            environment["GIT_SSL_CAINFO"] = str(cls.git_ca)
        return subprocess.run(["git", *arguments], cwd=cwd, env=environment, capture_output=True, text=True, timeout=120)

    def clone(self, user, repo):
        target = pathlib.Path(tempfile.mkdtemp(dir=self.temp, prefix=f"{user}-"))
        done = self.git(user, "clone", "--quiet", f"{self.url}/{OWNER}/{repo}.git", str(target))
        self.assertEqual(done.returncode, 0, done.stderr)
        return target


@unittest.skipUnless(_can_run(), "set YGG_FORGEJO_TESTS=1 to run against a Forgejo container "
                                 "(needs Docker, bash, jq, git and curl on a POSIX system)")
class ForgejoLiveTests(ForgejoClient, unittest.TestCase):
    """One Forgejo for the whole class. Every test works on branches, tags and pull requests of its
    own, so they do not depend on each other's order."""

    @classmethod
    def setUpClass(cls):
        cls.container = f"ygg-forgejo-test-{uuid.uuid4().hex[:8]}"
        # Class cleanups also run when setUpClass fails halfway: the container never outlives the suite.
        cls.addClassCleanup(docker, "rm", "--force", "--volumes", cls.container, check=False)
        cls.temp = pathlib.Path(tempfile.mkdtemp(prefix="forgejo-live-"))
        cls.addClassCleanup(shutil.rmtree, cls.temp, ignore_errors=True)

        port = free_port()
        cls.url = f"http://127.0.0.1:{port}"
        docker("run", "--detach", f"--name={cls.container}", f"--publish=127.0.0.1:{port}:3000", "--memory=1g",
               "--env=FORGEJO__security__INSTALL_LOCK=true",
               "--env=FORGEJO__database__DB_TYPE=sqlite3",
               f"--env=FORGEJO__server__ROOT_URL={cls.url}/",
               # As in compose.yml: nothing is visible signed out, so a wrong token or repository is a 404.
               "--env=FORGEJO__service__DISABLE_REGISTRATION=true",
               "--env=FORGEJO__service__REQUIRE_SIGNIN_VIEW=true",
               # Only so that the migrate.py tests can migrate from this very server (GitHub itself is out
               # of reach here): Forgejo refuses a clone from a local address by default, and production
               # keeps that default. No other test migrates, so it changes nothing for them.
               "--env=FORGEJO__migrations__ALLOW_LOCALNETWORKS=true",
               pinned_image())
        cls.wait_for_health()

        cls.tokens = {}
        for user in (OWNER, JENKINS, CLAUDE):
            cls.admin_cli("create", "--username", user, "--password", f"throwaway-{uuid.uuid4().hex}",
                          "--email", f"{user}@example.com", *(["--admin"] if user == OWNER else []),
                          "--must-change-password=false")
            cls.tokens[user] = cls.admin_cli("generate-access-token", "--username", user, "--token-name", "tests",
                                             "--scopes", "all", "--raw").strip()
        cls.api = {user: rules.Api(cls.url + "/api/v1", token, "token") for user, token in cls.tokens.items()}

        for repo in (APP, DOTTED):
            cls.create_repository(repo)

        cls.rules = {r.repo: r for r in rules.desired_rules(test_catalog())}
        cls.first_run = {repo: rules.apply_forgejo(cls.api[OWNER], OWNER, r, WEBHOOK, False)
                         for repo, r in cls.rules.items()}

    @classmethod
    def create_repository(cls, repo):
        """A private repository of the owner's: main with a Jenkinsfile, develop from it, and jenkins
        and claude as collaborators with write access (Forgejo drops users without it from a
        protection's whitelists)."""
        owner = cls.api[OWNER]
        owner.request("POST", "/user/repos", {"name": repo, "auto_init": True, "default_branch": "main",
                                              "private": True})
        cls.change_file(OWNER, repo, "Jenkinsfile", JENKINSFILE, "main")
        owner.request("POST", f"/repos/{OWNER}/{repo}/branches", {"new_branch_name": "develop",
                                                                   "old_branch_name": "main"})
        for user in (JENKINS, CLAUDE):
            owner.request("PUT", f"/repos/{OWNER}/{repo}/collaborators/{user}", {"permission": "write"})

    # --- Helpers -----------------------------------------------------------------------------------

    def set_checks(self, repo, sha, contexts, state="success"):
        for context in contexts:
            self.api[JENKINS].request("POST", f"/repos/{OWNER}/{repo}/statuses/{sha}",
                                      {"state": state, "context": context, "description": state})

    def refused(self, user, method, path, body=None):
        """The ApiError of a request that must be refused."""
        with self.assertRaises(rules.ApiError, msg=f"{user}: {method} {path} was allowed") as caught:
            self.send(user, method, path, body)
        return caught.exception

    def download(self, url):
        # REQUIRE_SIGNIN_VIEW: a release file of a private repository needs a token too.
        request = urllib.request.Request(url, headers={"Authorization": f"token {self.tokens[JENKINS]}"})
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.read()

    def forge(self, user, *arguments):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FORGE_", "GITHUB_")) and k != "GH_TOKEN"}
        env.update(FORGE_TYPE="forgejo", FORGE_API_URL=f"{self.url}/api/v1", FORGE_OWNER=OWNER,
                   FORGE_TOKEN=self.tokens[user])
        return subprocess.run(["bash", str(FORGE_SH), *arguments], env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)

    # --- forge.sh ----------------------------------------------------------------------------------

    def test_given_a_commit_when_statuses_are_set_and_read_then_the_latest_per_context_comes_back(self):
        sha = self.change_file(OWNER, APP, "statuses.txt", "statuses\n", "develop", "test/statuses")

        # A commit without any status: Forgejo answers "statuses": null, which is nothing to print.
        result = self.forge(JENKINS, "get-statuses", APP, sha)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

        for state, context in (("pending", "build"), ("success", "build"), ("failure", "test")):
            result = self.forge(JENKINS, "set-status", APP, sha, state, context, f"{context} {state}",
                                "https://jenkins.example.com/job/1")
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        # More contexts than one page holds (Forgejo's largest is 50): none may be cut off.
        many = [f"deploy/env-{n:02}" for n in range(55)]
        self.set_checks(APP, sha, many)

        result = self.forge(JENKINS, "get-statuses", APP, sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        # The third column is the login that created the status: what reuse compares with jenkinsUser.
        self.assertEqual(sorted(lines), sorted([f"build\tsuccess\t{JENKINS}", f"test\tfailure\t{JENKINS}"]
                                               + [f"{c}\tsuccess\t{JENKINS}" for c in many]))

        # Someone else's status on the same commit carries their login, not Jenkins'.
        self.api[CLAUDE].request("POST", f"/repos/{OWNER}/{APP}/statuses/{sha}",
                                 {"state": "success", "context": "by-claude", "description": "x"})
        result = self.forge(JENKINS, "get-statuses", APP, sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"by-claude\tsuccess\t{CLAUDE}", result.stdout.splitlines())

    def test_given_a_release_flow_when_run_then_tag_draft_upload_and_publish_work(self):
        sha = self.head(APP, "main")
        result = self.forge(JENKINS, "create-tag", APP, "v1.0.0", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/tags/v1.0.0")["commit"]["sha"], sha)

        notes = self.temp / "notes.md"
        notes.write_text("The first release.\n")
        result = self.forge(JENKINS, "create-release", APP, "v1.0.0", sha, "v1.0.0", "--draft", "--notes-file", str(notes))
        self.assertEqual(result.returncode, 0, result.stderr)
        release_id, html_url = result.stdout.strip().split("\t")
        release = self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/releases/{release_id}")
        self.assertEqual((release["draft"], release["body"], release["tag_name"], release["name"]),
                         (True, "The first release.\n", "v1.0.0", "v1.0.0"))
        self.assertEqual(html_url, release["html_url"])

        # find-release finds the draft by its tag (GitHub's lookup by tag would not), tag-sha the commit
        # the tag points to, and a tag nobody made is exit 4, not an error.
        result = self.forge(JENKINS, "find-release", APP, "v1.0.0")
        self.assertEqual((result.returncode, result.stdout), (0, f"{release_id}\t{html_url}\ttrue\n"), result.stderr)
        result = self.forge(JENKINS, "find-release", APP, "v9.9.9")
        self.assertEqual((result.returncode, result.stdout), (4, ""), result.stderr)
        result = self.forge(JENKINS, "tag-sha", APP, "v1.0.0")
        self.assertEqual((result.returncode, result.stdout), (0, f"{sha}\n"), result.stderr)
        result = self.forge(JENKINS, "tag-sha", APP, "v9.9.9")
        self.assertEqual((result.returncode, result.stdout), (4, ""), result.stderr)
        # An annotated tag is peeled to its commit.
        self.api[JENKINS].request("POST", f"/repos/{OWNER}/{APP}/tags", {"tag_name": "v1.0.0-annotated", "target": sha,
                                                                         "message": "an annotated tag"})
        result = self.forge(JENKINS, "tag-sha", APP, "v1.0.0-annotated")
        self.assertEqual((result.returncode, result.stdout), (0, f"{sha}\n"), result.stderr)
        result = self.forge(JENKINS, "release-files", APP, release_id)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

        artifact = self.temp / "shop-api 1.0.0.zip"
        artifact.write_bytes(b"the release's bytes")
        result = self.forge(JENKINS, "upload-release-file", APP, release_id, str(artifact))
        self.assertEqual(result.returncode, 0, result.stderr)
        download = result.stdout.strip()
        # The upload's answer links the file as <server>/attachments/<uuid>; the release's list of files
        # links it as .../releases/download/<tag>/<name>, as GitHub does. Both serve it.
        assets = self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/releases/{release_id}/assets")
        self.assertEqual([(a["name"], a["size"]) for a in assets], [(artifact.name, artifact.stat().st_size)])
        self.assertRegex(download, rf"^{re.escape(self.url)}/attachments/[0-9a-f-]+$")
        for url in (download, assets[0]["browser_download_url"]):
            self.assertEqual(self.download(url), artifact.read_bytes(), url)
        result = self.forge(JENKINS, "release-files", APP, release_id)
        self.assertEqual((result.returncode, result.stdout), (0, f"{artifact.name}\t{artifact.stat().st_size}\n"),
                         result.stderr)

        # A draft is not among the release tags (what tag-check checks); published, it is, with its commit.
        result = self.forge(JENKINS, "release-tags", APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("v1.0.0", [line.split("\t")[0] for line in result.stdout.splitlines()])
        result = self.forge(JENKINS, "publish-release", APP, release_id)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, html_url), result.stderr)
        result = self.forge(JENKINS, "release-tags", APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"v1.0.0\t{sha}", result.stdout.splitlines())
        self.assertFalse(self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/releases/{release_id}")["draft"])
        result = self.forge(JENKINS, "find-release", APP, "v1.0.0")
        self.assertEqual((result.returncode, result.stdout), (0, f"{release_id}\t{html_url}\tfalse\n"), result.stderr)

    def test_given_a_pull_request_when_jenkins_merges_it_then_the_merge_commit_is_printed(self):
        head = self.change_file(OWNER, APP, "CHANGELOG.md", "## 3.0.0\n", "develop", "release/3.0.0")
        number = self.open_pull_request(OWNER, APP, "release/3.0.0", "main")
        self.set_checks(APP, head, self.rules[APP].main_checks)

        # The pull request moved on since that commit was deployed: refused, still open.
        stale = self.head(APP, "develop")
        result = self.forge(JENKINS, "merge-pr", APP, str(number), stale, "release: v3.0.0")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn(f"merge-pr #{number} failed: head out of date", result.stderr)
        self.assertFalse(self.pull_request(APP, number)["merged"])

        title = f"release: v3.0.0 (#{number})"
        result = self.forge(JENKINS, "merge-pr", APP, str(number), head, title)
        self.assertEqual(result.returncode, 0, result.stderr)
        merge = result.stdout.strip()
        self.assertEqual(merge, self.head(APP, "main"))
        commit = self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/git/commits/{merge}")
        # A merge commit (develop and main keep sharing history), titled as asked.
        self.assertEqual(len(commit["parents"]), 2)
        self.assertEqual(commit["commit"]["message"].splitlines()[0], title)

    def test_given_a_branch_with_a_slash_when_deleted_then_it_is_gone(self):
        # reveal.js has `rules: basic`: release/* is not protected there.
        self.api[OWNER].request("POST", f"/repos/{OWNER}/{DOTTED}/branches",
                                {"new_branch_name": "release/1.2.3", "old_branch_name": "develop"})
        result = self.forge(JENKINS, "delete-branch", DOTTED, "release/1.2.3")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(self.branch_exists(DOTTED, "release/1.2.3"))

        # Deleted already (as "delete head branch after merge" does): done, with a note.
        result = self.forge(JENKINS, "delete-branch", DOTTED, "release/1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release/1.2.3 was already deleted", result.stderr)

        # A repository that is not there (or that jenkins cannot see) is an error, not "already gone".
        result = self.forge(JENKINS, "delete-branch", "no-such-repository", "release/1.2.3")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("delete-branch release/1.2.3 failed", result.stderr)

    def test_given_a_protected_release_branch_when_jenkins_deletes_it_then_forge_sh_exits_3(self):
        self.api[OWNER].request("POST", f"/repos/{OWNER}/{APP}/branches",
                                {"new_branch_name": "release/2.0.0", "old_branch_name": "develop"})
        result = self.forge(JENKINS, "delete-branch", APP, "release/2.0.0")
        self.assertEqual((result.returncode, result.stdout), (3, ""), result.stderr)
        self.assertIn("kept: release/2.0.0 is protected", result.stderr)
        self.assertTrue(self.branch_exists(APP, "release/2.0.0"))
        # Nor can the owner delete it while the release/* protection exists (Forgejo refuses the
        # deletion of any protected branch, administrators included): docs/setup.md says how.
        self.assertEqual(self.refused(OWNER, "DELETE", f"/repos/{OWNER}/{APP}/branches/release%2F2.0.0").message,
                         "branch protected")
        pushed = self.git(OWNER, "push", "origin", ":refs/heads/release/2.0.0", cwd=self.clone(OWNER, APP))
        self.assertIn("branch release/2.0.0 is protected from deletion", pushed.stderr)
        self.assertTrue(self.branch_exists(APP, "release/2.0.0"))

    def test_given_a_repository_with_a_dot_when_the_commands_run_then_they_work(self):
        head = self.change_file(OWNER, DOTTED, "slides.md", "# Slides\n", "develop", "feature/slides")
        result = self.forge(JENKINS, "set-status", DOTTED, head, "success", "branch-policy", "ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.forge(JENKINS, "get-statuses", DOTTED, head)
        self.assertEqual((result.returncode, result.stdout), (0, f"branch-policy\tsuccess\t{JENKINS}\n"), result.stderr)

        number = self.open_pull_request(OWNER, DOTTED, "feature/slides", "develop")
        result = self.forge(JENKINS, "merge-pr", DOTTED, str(number), head, f"Slides (#{number})")
        self.assertEqual(result.returncode, 0, result.stderr)
        merge = result.stdout.strip()
        self.assertEqual(merge, self.head(DOTTED, "develop"))

        result = self.forge(JENKINS, "create-tag", DOTTED, "v0.1.0", merge)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.forge(JENKINS, "create-release", DOTTED, "v0.1.0", merge, "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        release_id = result.stdout.split("\t")[0]
        artifact = self.temp / "slides.pdf"
        artifact.write_bytes(b"%PDF slides")
        result = self.forge(JENKINS, "upload-release-file", DOTTED, release_id, str(artifact))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.download(result.stdout.strip()), artifact.read_bytes())
        result = self.forge(JENKINS, "publish-release", DOTTED, release_id)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.api[OWNER].request("POST", f"/repos/{OWNER}/{DOTTED}/branches",
                                {"new_branch_name": "feature/old", "old_branch_name": "develop"})
        result = self.forge(JENKINS, "delete-branch", DOTTED, "feature/old")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.branch_exists(DOTTED, "feature/old"))

    # --- rules.py ----------------------------------------------------------------------------------

    def test_given_rules_applied_when_read_back_then_protections_match_and_a_rerun_changes_nothing(self):
        owner = self.api[OWNER]
        r = self.rules[APP]
        self.assertIn("  protected branch 'release/*'", self.first_run[APP])
        self.assertIn("  created the Jenkins webhook", self.first_run[DOTTED])

        for repo in (APP, DOTTED):
            settings = owner.request("GET", f"/repos/{OWNER}/{repo}")
            self.assertEqual({k: settings[k] for k in rules.forgejo_settings()}, rules.forgejo_settings(), repo)

        protections = {p["rule_name"]: p for p in owner.request("GET", f"/repos/{OWNER}/{APP}/branch_protections")}
        self.assertEqual(set(protections), {"develop", "main", "release/*"})
        for name, mergers, checks in (("develop", r.develop_mergers, r.develop_checks),
                                      ("main", r.main_mergers, r.main_checks)):
            p = protections[name]
            self.assertEqual((p["enable_push"], p["enable_merge_whitelist"], p["enable_status_check"],
                              p["protected_file_patterns"], p["apply_to_admins"]),
                             (False, True, True, "Jenkinsfile;yggdrasil.yml" if name == "develop" else "", False),
                             name)
            self.assertEqual(set(p["merge_whitelist_usernames"]), set(mergers), name)
            self.assertEqual(set(p["status_check_contexts"]), set(checks), name)
        release = protections["release/*"]
        self.assertEqual((release["enable_push"], release["enable_push_whitelist"], release["push_whitelist_usernames"],
                          release["apply_to_admins"]), (True, True, [OWNER], False))
        tags = owner.request("GET", f"/repos/{OWNER}/{APP}/tag_protections")
        self.assertEqual([(t["name_pattern"], t["whitelist_usernames"]) for t in tags], [("v*", [JENKINS])])
        basic = {p["rule_name"]: p for p in owner.request("GET", f"/repos/{OWNER}/{DOTTED}/branch_protections")}
        self.assertEqual({name: (p["enable_push"], p["enable_merge_whitelist"], p["enable_status_check"])
                          for name, p in basic.items()}, {"develop": (True, False, False), "main": (True, False, False)})
        for repo in (APP, DOTTED):
            hooks = owner.request("GET", f"/repos/{OWNER}/{repo}/hooks")
            self.assertEqual([(h["type"], h["config"]["url"], h["config"]["content_type"], h["active"]) for h in hooks],
                             [("gitea", WEBHOOK["url"], "json", True)], repo)

        # A second run finds everything as it should be: it only reads.
        for repo in (APP, DOTTED):
            api = RecordingApi(self.url + "/api/v1", self.tokens[OWNER], "token")
            lines = rules.apply_forgejo(api, OWNER, self.rules[repo], WEBHOOK, False)
            self.assertEqual(api.writes(), [], f"{repo}: {lines}")

        # Something changed by hand is put back by updating it in place -- the release/* protection
        # through its encoded name, the tag protection and the hook by id -- and only that is written.
        owner.request("PATCH", f"/repos/{OWNER}/{APP}/branch_protections/release%2F%2A",
                      {"push_whitelist_usernames": [OWNER, CLAUDE]})
        owner.request("PATCH", f"/repos/{OWNER}/{APP}/tag_protections/{tags[0]['id']}",
                      {"name_pattern": "v*", "whitelist_usernames": [CLAUDE]})
        hook = owner.request("GET", f"/repos/{OWNER}/{APP}/hooks")[0]
        owner.request("PATCH", f"/repos/{OWNER}/{APP}/hooks/{hook['id']}", {"active": False})
        api = RecordingApi(self.url + "/api/v1", self.tokens[OWNER], "token")
        rules.apply_forgejo(api, OWNER, r, WEBHOOK, False)
        self.assertEqual(sorted(api.writes()), sorted([
            ("PATCH", f"/repos/{OWNER}/{APP}/branch_protections/release%2F%2A"),
            ("PATCH", f"/repos/{OWNER}/{APP}/tag_protections/{tags[0]['id']}"),
            ("PATCH", f"/repos/{OWNER}/{APP}/hooks/{hook['id']}"),
        ]))
        self.assertEqual(owner.request("GET", f"/repos/{OWNER}/{APP}/branch_protections/release%2F%2A"
                                       )["push_whitelist_usernames"], [OWNER])
        self.assertEqual(owner.request("GET", f"/repos/{OWNER}/{APP}/tag_protections/{tags[0]['id']}"
                                       )["whitelist_usernames"], [JENKINS])
        self.assertTrue(owner.request("GET", f"/repos/{OWNER}/{APP}/hooks/{hook['id']}")["active"])
        api = RecordingApi(self.url + "/api/v1", self.tokens[OWNER], "token")
        rules.apply_forgejo(api, OWNER, r, WEBHOOK, False)
        self.assertEqual(api.writes(), [])

    def test_given_users_without_write_access_when_rules_are_applied_then_the_run_names_them(self):
        # A repository nobody else was given access to: Forgejo leaves claude and jenkins off the
        # whitelists without an error, and rules.py says so.
        repo = "no-collaborators"
        self.api[OWNER].request("POST", "/user/repos", {"name": repo, "auto_init": True, "default_branch": "main",
                                                        "private": True})
        self.api[OWNER].request("POST", f"/repos/{OWNER}/{repo}/branches",
                                {"new_branch_name": "develop", "old_branch_name": "main"})
        lines = rules.apply_forgejo(self.api[OWNER], OWNER, dataclasses.replace(self.rules[APP], repo=repo), None, False)
        warnings = [line for line in lines if "warning" in line]
        self.assertEqual(len(warnings), 3, lines)
        self.assertIn("left claude off who may merge (branch 'develop')", warnings[0])
        self.assertIn("left jenkins off who may merge (branch 'main')", warnings[1])
        self.assertIn("left jenkins off who may create tags (tags 'v*')", warnings[2])
        merging = {p["rule_name"]: p["merge_whitelist_usernames"]
                   for p in self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/branch_protections")}
        self.assertEqual((merging["develop"], merging["main"]), ([OWNER], []))

    def test_given_an_owner_token_with_only_write_repository_when_rules_are_applied_then_it_is_enough(self):
        # What docs/setup.md asks the owner to create: no other scope. A repository of its own, so
        # that everything is written (settings, protections, tag protection, webhook), not "unchanged".
        repo = "scope-check"
        self.create_repository(repo)
        token = self.admin_cli("generate-access-token", "--username", OWNER, "--token-name", "scoped",
                               "--scopes", "write:repository", "--raw").strip()
        scoped = rules.Api(self.url + "/api/v1", token, "token")
        lines = rules.apply_forgejo(scoped, OWNER, dataclasses.replace(self.rules[APP], repo=repo), WEBHOOK, False)
        self.assertEqual([line for line in lines if "warning" in line], [], lines)
        self.assertIn("  protected branch 'release/*'", lines)
        self.assertIn("  protected tags 'v*'", lines)
        self.assertIn("  created the Jenkins webhook", lines)
        names = {p["rule_name"] for p in self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/branch_protections")}
        self.assertEqual(names, {"develop", "main", "release/*"})

    def test_given_a_release_repository_turned_basic_when_rules_are_applied_then_its_branch_rules_are_cleared(self):
        repo = "turned-basic"
        self.create_repository(repo)
        rules.apply_forgejo(self.api[OWNER], OWNER, dataclasses.replace(self.rules[APP], repo=repo), None, False)
        basic = dataclasses.replace(self.rules[DOTTED], repo=repo)
        lines = rules.apply_forgejo(self.api[OWNER], OWNER, basic, None, False)

        protections = {p["rule_name"]: p
                       for p in self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/branch_protections")}
        for name in ("develop", "main"):
            p = protections[name]
            self.assertEqual((p["enable_push"], p["enable_merge_whitelist"], p["enable_status_check"],
                              p["status_check_contexts"] or [], p["protected_file_patterns"]),
                             (True, False, False, [], ""), name)
        # Nothing is deleted: what the basic model has no place for stays, and the run says so.
        self.assertIn("release/*", protections)
        tags = self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/tag_protections")
        self.assertEqual([t["name_pattern"] for t in tags], ["v*"])
        notes = [line for line in lines if "note" in line]
        self.assertEqual(len(notes), 2, lines)
        self.assertIn("branch protection 'release/*'", notes[0])
        self.assertIn("tag protection 'v*'", notes[1])

        api = RecordingApi(self.url + "/api/v1", self.tokens[OWNER], "token")
        rules.apply_forgejo(api, OWNER, basic, None, False)
        self.assertEqual(api.writes(), [])

    def test_given_the_rules_when_claude_merges_a_pipeline_file_change_then_it_is_refused(self):
        checks = self.rules[APP].develop_checks
        changed = self.change_file(CLAUDE, APP, "Jenkinsfile", JENKINSFILE + "// claude was here\n", "develop",
                                   "feature/claude-jenkinsfile")
        number = self.open_pull_request(CLAUDE, APP, "feature/claude-jenkinsfile", "develop")
        self.set_checks(APP, changed, checks)
        error = self.refused(CLAUDE, "POST", f"/repos/{OWNER}/{APP}/pulls/{number}/merge", {"Do": "merge"})
        self.assertEqual((error.status, error.message), (405, "not allowed to merge [reason: Changed protected files]"))
        self.assertFalse(self.pull_request(APP, number)["merged"])

        # yggdrasil.yml (what Jenkins runs) is refused the same way.
        pipeline = self.change_file(CLAUDE, APP, "yggdrasil.yml", "checks: {}\n", "develop", "feature/claude-pipeline")
        number = self.open_pull_request(CLAUDE, APP, "feature/claude-pipeline", "develop")
        self.set_checks(APP, pipeline, checks)
        error = self.refused(CLAUDE, "POST", f"/repos/{OWNER}/{APP}/pulls/{number}/merge", {"Do": "merge"})
        self.assertEqual((error.status, error.message), (405, "not allowed to merge [reason: Changed protected files]"))
        self.assertFalse(self.pull_request(APP, number)["merged"])

        # The same developer merges any other change: it is the pipeline files that are refused.
        other = self.change_file(CLAUDE, APP, "docs/claude.md", "notes\n", "develop", "feature/claude-docs")
        number = self.open_pull_request(CLAUDE, APP, "feature/claude-docs", "develop")
        self.set_checks(APP, other, checks)
        self.merge(CLAUDE, APP, number)
        self.assertTrue(self.pull_request(APP, number)["merged"])

    def test_given_the_rules_when_jenkins_merges_a_release_changing_the_jenkinsfile_into_main_then_it_is_allowed(self):
        # The Jenkinsfile is protected on develop only: every change to it gets there through the owner,
        # and main only ever receives snapshots of develop, which Jenkins merges.
        changed = JENKINSFILE + "// changed through the owner\n"
        head = self.change_file(OWNER, APP, "Jenkinsfile", changed, "develop", "feature/jenkinsfile-by-owner")
        number = self.open_pull_request(OWNER, APP, "feature/jenkinsfile-by-owner", "develop")
        self.set_checks(APP, head, self.rules[APP].develop_checks)
        # A plain merge is refused even for the owner (a protected file is not covered by the owner
        # being unbound from the protection); the owner's forced merge ("Merge anyway") is what gets it in.
        self.assertEqual(self.refused(OWNER, "POST", f"/repos/{OWNER}/{APP}/pulls/{number}/merge", {"Do": "merge"}).message,
                         "not allowed to merge [reason: Changed protected files]")
        self.merge(OWNER, APP, number, force_merge=True)
        self.assertTrue(self.pull_request(APP, number)["merged"])

        self.api[OWNER].request("POST", f"/repos/{OWNER}/{APP}/branches",
                                {"new_branch_name": "release/7.0.0", "old_branch_name": "develop"})
        sha = self.head(APP, "release/7.0.0")
        number = self.open_pull_request(OWNER, APP, "release/7.0.0", "main")
        self.set_checks(APP, sha, self.rules[APP].main_checks)
        result = self.forge(JENKINS, "merge-pr", APP, str(number), sha, "release 7.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.pull_request(APP, number)["merged"])
        self.assertEqual(result.stdout.strip(), self.head(APP, "main"))
        contents = self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/contents/Jenkinsfile?ref=main")
        self.assertEqual(base64.b64decode(contents["content"]).decode(), changed)

    def test_given_the_rules_when_the_owner_merges_with_failing_checks_then_it_is_allowed(self):
        checks = self.rules[APP].develop_checks
        head = self.change_file(OWNER, APP, "hotfix.txt", "urgent\n", "develop", "fix/owner-override")
        number = self.open_pull_request(OWNER, APP, "fix/owner-override", "develop")
        self.set_checks(APP, head, [c for c in checks if c != "test"])
        self.set_checks(APP, head, ["test"], "failure")
        merge = f"/repos/{OWNER}/{APP}/pulls/{number}/merge"

        # The checks hold for a developer, who cannot force the merge ...
        failing = "not allowed to merge [reason: Not all required status checks successful]"
        for body in ({"Do": "merge"}, {"Do": "merge", "force_merge": True}):
            self.assertEqual(self.refused(CLAUDE, "POST", merge, body).message, failing, body)
        self.assertFalse(self.pull_request(APP, number)["merged"])
        # ... and the owner, an administrator the protection does not bind, can: by forcing it, so a
        # failing check is never merged past by accident.
        self.assertEqual(self.refused(OWNER, "POST", merge, {"Do": "merge"}).message, failing)
        self.merge(OWNER, APP, number, force_merge=True)
        self.assertTrue(self.pull_request(APP, number)["merged"])

    def test_given_the_rules_when_claude_creates_a_v_tag_then_it_is_refused(self):
        sha = self.head(APP, "develop")
        result = self.forge(CLAUDE, "create-tag", APP, "v9.9.9", sha)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("create-tag v9.9.9 failed: user not allowed to create protected tag", result.stderr)

        clone = self.clone(CLAUDE, APP)
        self.assertEqual(self.git(CLAUDE, "tag", "v9.9.8", cwd=clone).returncode, 0)
        pushed = self.git(CLAUDE, "push", "origin", "refs/tags/v9.9.8", cwd=clone)
        self.assertNotEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("Tag v9.9.8 is protected", pushed.stderr)

        tags = [t["name"] for t in self.api[OWNER].request("GET", f"/repos/{OWNER}/{APP}/tags")]
        self.assertNotIn("v9.9.9", tags)
        self.assertNotIn("v9.9.8", tags)
        # Only the version tags are Jenkins': claude may tag otherwise.
        result = self.forge(CLAUDE, "create-tag", APP, "claude-experiment", sha)
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- Version tags once they exist: who can move or delete them -------------------------------

    def tag_commit(self, repo, tag):
        """The commit a tag names, or None when there is no such tag."""
        try:
            tag = self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/tags/{urllib.parse.quote(tag)}")
            return tag["commit"]["sha"]
        except rules.ApiError as error:
            if error.status == 404:
                return None
            raise

    def change_tags(self, user, version, release=False):
        """What `user` can do to protected version tags that jenkins created on main: force-push
        v<version>.0 onto another commit, delete v<version>.1 with a push, delete v<version>.2 through the
        API (each with a published release when `release`). Returns main's commit, the other commit, the
        three tags, the two pushes' results and the API's refusal (None when it deleted the tag)."""
        main = self.head(APP, "main")
        other = self.change_file(OWNER, APP, f"tags-{version}.txt", "another commit\n", "develop", f"test/tags-{version}")
        tags = [f"v{version}.{n}" for n in range(3)]
        for tag in tags:
            result = self.forge(JENKINS, "create-tag", APP, tag, main)
            self.assertEqual(result.returncode, 0, result.stderr)
            if release:
                result = self.forge(JENKINS, "create-release", APP, tag, main, tag)
                self.assertEqual(result.returncode, 0, result.stderr)
        clone = self.clone(user, APP)
        self.assertEqual(self.git(user, "tag", "--force", tags[0], other, cwd=clone).returncode, 0)
        moved = self.git(user, "push", "--force", "origin", f"refs/tags/{tags[0]}", cwd=clone)
        deleted = self.git(user, "push", "origin", f":refs/tags/{tags[1]}", cwd=clone)
        try:
            self.api[user].request("DELETE", f"/repos/{OWNER}/{APP}/tags/{tags[2]}")
            refused = None
        except rules.ApiError as error:
            refused = error
        return main, other, tags, (moved, deleted), refused

    def test_given_a_protected_v_tag_when_claude_or_the_owner_moves_or_deletes_it_then_it_is_refused(self):
        # Only who is on the tag protection's list (jenkins, for an application) may change a v* tag
        # at all: not claude, nor the owner, whom the list does not name (an administrator is bound by
        # a tag protection, unlike by a branch protection with apply_to_admins false).
        for user, version in ((CLAUDE, "6.0"), (OWNER, "6.1")):
            main, _, tags, (moved, deleted), refused = self.change_tags(user, version)
            for result in (moved, deleted):
                self.assertNotEqual(result.returncode, 0, (user, result.stderr))
                self.assertIn("pre-receive hook declined", result.stderr, user)
            self.assertEqual((refused.status, refused.message), (422, "user not allowed to delete protected tag"), user)
            self.assertEqual([self.tag_commit(APP, tag) for tag in tags], [main] * 3, user)

    def test_given_a_protected_v_tag_when_jenkins_moves_or_deletes_it_then_forgejo_allows_it(self):
        # The known gap docs/setup.md (step 12) states: Forgejo's tag protection is one list of who may
        # change a matching tag in any way -- create, move, delete -- so the user who creates the
        # version tags (jenkins here; the owner in an owner-tag repository) can also move and delete them.
        main, other, tags, (moved, deleted), refused = self.change_tags(JENKINS, "6.2")
        self.assertEqual((moved.returncode, deleted.returncode), (0, 0), moved.stderr + deleted.stderr)
        self.assertIsNone(refused)
        self.assertEqual([self.tag_commit(APP, tag) for tag in tags], [other, None, None])
        self.assertNotEqual(main, other)

        # A tag with a release cannot be deleted through the API (by anyone: Forgejo's own rule) -- but
        # a push still deletes it, and a forced push still moves it.
        main, other, tags, (moved, deleted), refused = self.change_tags(JENKINS, "6.3", release=True)
        self.assertEqual((moved.returncode, deleted.returncode), (0, 0), moved.stderr + deleted.stderr)
        self.assertEqual((refused.status, refused.message),
                         (409, "a tag attached to a release cannot be deleted directly"))
        self.assertEqual([self.tag_commit(APP, tag) for tag in tags], [other, None, main])
        # The release of the deleted tag is turned into a draft once Forgejo has processed the push (in the
        # background: right after the push it may still read as published), naming a tag that is gone. A
        # draft is seen only with write access: not by the tag-check job's read credential, which is why that
        # job remembers the releases it has seen. The moved tag's release keeps the commit it was released
        # from as its target, and is still published.
        self.assertTrue(self.wait_for_draft(APP, tags[1]), f"the release of {tags[1]} is still published")
        result = self.forge(JENKINS, "release-tags", APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = result.stdout.splitlines()
        self.assertIn(f"{tags[0]}	{main}", listed)
        self.assertIn(f"{tags[2]}	{main}", listed)
        self.assertNotIn(tags[1], [line.split("	")[0] for line in listed])
        found = [self.forge(JENKINS, "tag-sha", APP, tag) for tag in tags]
        self.assertEqual([(r.returncode, r.stdout.strip()) for r in found], [(0, other), (4, ""), (0, main)])
        # Pushed back (the repair docs/setup.md describes), the tag is on its commit again and Forgejo
        # publishes its release again by itself.
        clone = self.clone(JENKINS, APP)
        self.assertEqual(self.git(JENKINS, "tag", tags[1], main, cwd=clone).returncode, 0)
        pushed = self.git(JENKINS, "push", "origin", f"refs/tags/{tags[1]}", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.tag_commit(APP, tags[1]), main)
        self.assertTrue(self.wait_for_draft(APP, tags[1], draft=False), f"the release of {tags[1]} is still a draft")
        result = self.forge(JENKINS, "release-tags", APP)
        self.assertIn(f"{tags[1]}	{main}", result.stdout.splitlines())

    def release_by_tag(self, repo, tag):
        """The release of the tag as the owner sees it (drafts included), or None."""
        found = [r for r in self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/releases?limit=50") or []
                 if r["tag_name"] == tag]
        return found[0] if found else None

    def wait_for_draft(self, repo, tag, seconds=60, draft=True):
        """Whether the tag's release is a draft (with draft=False: published) within `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            release = self.release_by_tag(repo, tag)
            if release is not None and release["draft"] is draft:
                return True
            time.sleep(1)
        return False

    def test_given_a_basic_repository_when_anyone_force_pushes_or_deletes_develop_or_main_then_it_is_refused(self):
        # rules: basic sends develop and main protections with pushing allowed and nothing else: Forgejo
        # refuses a force push and a deletion under any protection, even to the owner, whom the
        # protection does not otherwise bind. Pushing on top is still allowed.
        for user in (CLAUDE, JENKINS, OWNER):
            for branch in ("develop", "main"):
                before = self.head(DOTTED, branch)
                clone = self.clone(user, DOTTED)
                self.git(user, "checkout", "--quiet", "--orphan", "rewritten", cwd=clone)
                self.assertEqual(self.git(user, "commit", "--quiet", "--allow-empty", "-m", "rewritten",
                                          cwd=clone).returncode, 0)
                forced = self.git(user, "push", "--force", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
                self.assertNotEqual(forced.returncode, 0, (user, branch))
                self.assertIn(f"branch {branch} is protected from force push", forced.stderr, (user, branch))
                deleted = self.git(user, "push", "origin", f":refs/heads/{branch}", cwd=clone)
                self.assertNotEqual(deleted.returncode, 0, (user, branch))
                # develop is the default branch, which Forgejo refuses to delete before it asks the
                # protection; main shows the protection's own refusal.
                self.assertIn("is the default branch and cannot be deleted" if branch == "develop"
                              else "branch main is protected from deletion", deleted.stderr, (user, branch))
                error = self.refused(user, "DELETE", f"/repos/{OWNER}/{DOTTED}/branches/{branch}")
                self.assertEqual((error.status, error.message),
                                 (403, "can not delete default branch" if branch == "develop" else "branch protected"))
                self.assertEqual(self.head(DOTTED, branch), before, (user, branch))

        clone = self.clone(CLAUDE, DOTTED)
        self.git(CLAUDE, "checkout", "--quiet", "develop", cwd=clone)
        self.assertEqual(self.git(CLAUDE, "commit", "--quiet", "--allow-empty", "-m", "on top", cwd=clone).returncode, 0)
        pushed = self.git(CLAUDE, "push", "origin", "HEAD:refs/heads/develop", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

    def test_given_the_rules_when_claude_pushes_a_release_branch_then_it_is_refused(self):
        clone = self.clone(CLAUDE, APP)
        pushed = self.git(CLAUDE, "push", "origin", "HEAD:refs/heads/release/9.0.0", cwd=clone)
        self.assertNotEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("Not allowed to push to protected branch release/9.0.0", pushed.stderr)
        self.assertFalse(self.branch_exists(APP, "release/9.0.0"))
        # Nor through the API, which pushes on the user's behalf.
        self.refused(CLAUDE, "POST", f"/repos/{OWNER}/{APP}/branches",
                     {"new_branch_name": "release/9.0.1", "old_branch_name": "develop"})
        self.assertFalse(self.branch_exists(APP, "release/9.0.1"))

        # claude pushes its own branches, and nothing protects them.
        pushed = self.git(CLAUDE, "push", "origin", "HEAD:refs/heads/feature/claude-push", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertTrue(self.branch_exists(APP, "feature/claude-push"))

    # --- migrate.py --------------------------------------------------------------------------------

    def migrate_import(self, github_names, *arguments, private=True):
        """`migrate.py import` run for real against this Forgejo, with the GitHub side a fake that lists
        `github_names` (GitHub itself is not reachable from here).

        What this proves: the endpoint (`POST /repos/migrate`), `repo_owner`, `repo_name`, `private` and
        `mirror: false` are taken by Forgejo 16, which makes a finished copy of the source; its repository
        JSON has the `empty` and `mirror` fields import reads; a second run says "already imported".
        What it does not prove: the GitHub-specific part of the request (`service: github`, `clone_addr`,
        `auth_token`, and issues, labels, milestones, releases, pull requests and wiki being copied from
        GitHub) -- the migration's source is replaced here, so that shape is pinned by test_migrate.py
        alone. The replacement: `service` is `git`, the clone address this very server as the container
        itself reaches it (127.0.0.1:3000 inside, not the published port), and the owner's token goes as
        the git password, because Forgejo's `auth_token` is for the hosting services. Everything else in
        the body is what import sends. Returns (exit code, stdout, stderr)."""
        real_payload, real_api = migrate.migration_payload, rules.Api
        catalog = {**test_catalog(), "forge": {**test_catalog()["forge"], "url": self.url},
                   "mirror": {"github": OWNER}}
        github = FakeGitHub([github_repository(name, private=private, default_branch="main")
                             for name in github_names])

        def from_this_forgejo(ctx, repo):
            payload = real_payload(ctx, repo)
            del payload["auth_token"]
            payload.update(service="git", clone_addr=f"http://127.0.0.1:3000/{OWNER}/{APP}.git",
                           auth_username=OWNER, auth_password=self.tokens[OWNER])
            return payload

        def api(base, token, scheme, headers=None):
            return github if scheme == "Bearer" else real_api(base, token, scheme, headers)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(migrate.catalog_module, "load", return_value=catalog), \
                mock.patch.dict(os.environ, {"FORGE_TOKEN": self.tokens[OWNER]}), \
                mock.patch.object(migrate.rules, "Api", api), \
                mock.patch.object(migrate.rules, "find_github_token", return_value="not-a-real-github-token"), \
                mock.patch.object(migrate, "migration_payload", from_this_forgejo), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = migrate.main(list(arguments) or ["import"])
        return code, out.getvalue(), err.getvalue()

    def test_given_a_repository_when_migrate_imports_it_then_forgejo_copies_it_and_a_second_run_says_already_imported(self):
        copy = "shop-api-imported"
        code, out, err = self.migrate_import([copy])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"{copy}: imported"])

        # Forgejo took the request import sends (owner, name, private, mirror false and the rest of its
        # body; not the GitHub-specific service and clone address, see migrate_import) and made a
        # finished copy of the source: its branches, its Jenkinsfile, private as the (fake) GitHub says.
        owner = self.api[OWNER]
        found = owner.request("GET", f"/repos/{OWNER}/{copy}")
        self.assertEqual((found["private"], found["empty"], found["mirror"]), (True, False, False))
        # (The default branch is not asserted: a plain git migration picks its own; GitHub's carries over.)
        self.assertTrue(self.branch_exists(copy, "develop"))
        contents = owner.request("GET", f"/repos/{OWNER}/{copy}/contents/Jenkinsfile?ref=main")
        self.assertEqual(base64.b64decode(contents["content"]).decode(), JENKINSFILE)
        self.assertEqual(self.head(copy, "main"), self.head(APP, "main"))

        # Run again: the repository is there, finished, and import leaves it alone -- no write at all.
        api = RecordingApi(self.url + "/api/v1", self.tokens[OWNER], "token")
        with mock.patch.object(rules, "Api", lambda *a, **k: api):
            code, out, err = self.migrate_import([copy])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"{copy}: already imported"])
        self.assertEqual(api.calls, [("GET", f"/repos/{OWNER}/{copy}")])

    def test_given_an_empty_repository_when_migrate_imports_then_it_is_unfinished_and_never_deleted(self):
        # What an interrupted migration leaves: a repository with nothing in it. Forgejo 16 says so in
        # the repository's JSON (`empty`), which is what import reads.
        half = "half-migrated"
        self.api[OWNER].request("POST", "/user/repos", {"name": half, "private": True})
        self.assertTrue(self.api[OWNER].request("GET", f"/repos/{OWNER}/{half}")["empty"])
        code, out, err = self.migrate_import([half])
        self.assertEqual(code, 1, out)
        self.assertIn(f"  {half}: exists but looks unfinished (empty): delete it on Forgejo and run import again",
                      err.splitlines())
        self.assertTrue(self.api[OWNER].request("GET", f"/repos/{OWNER}/{half}")["empty"])  # still there, untouched

        # Deleted by hand, import goes through (the source is shop-api, as for every migration here).
        self.api[OWNER].request("DELETE", f"/repos/{OWNER}/{half}")
        code, out, err = self.migrate_import([half])
        self.assertEqual((code, err), (0, ""), out)
        self.assertFalse(self.api[OWNER].request("GET", f"/repos/{OWNER}/{half}")["empty"])

    def migrate_run(self, arguments, catalog, github_names=(), env=None, mirror_to=None, github=None):
        """migrate.py run for real against this Forgejo, with the GitHub side a fake that lists `github_names`
        (GitHub itself is not reachable from here). `mirror_to` replaces the address a push mirror points at
        (mirror_address): this very server stands in for GitHub, as the container itself reaches it
        (127.0.0.1:3000 inside, not the published port). `github` replaces the fake GitHub. Returns (exit code,
        stdout, stderr)."""
        real_api = rules.Api
        github = github or FakeGitHub([github_repository(name, default_branch="main") for name in github_names],
                                      login=OWNER)

        def api(base, token, scheme, headers=None):
            return github if scheme == "Bearer" else real_api(base, token, scheme, headers)

        out, err = io.StringIO(), io.StringIO()
        environment = {"FORGE_TOKEN": self.tokens[OWNER], **(env or {})}
        with mock.patch.object(migrate.catalog_module, "load", return_value=catalog), \
                mock.patch.dict(os.environ, environment), \
                mock.patch.object(migrate.rules, "Api", api), \
                mock.patch.object(migrate.rules, "find_github_token", return_value="not-a-real-github-token"), \
                mock.patch.object(migrate, "mirror_address",
                                  (lambda ctx, name: mirror_to(name)) if mirror_to else migrate.mirror_address), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = migrate.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def migrate_catalog(self, **fields):
        """The suite's catalog, pointing at this server, with the GitHub owner set to the same name (the
        fake GitHub lists that owner's repositories)."""
        return {**test_catalog(), "forge": {**test_catalog()["forge"], "url": self.url},
                "mirror": {"github": OWNER}, **fields}

    def eventually(self, check, what, seconds=120):
        """Forgejo pushes a mirror in the background: polls `check` until it is true, or fails at the deadline."""
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() > deadline:
                self.fail(f"{what}: not so after {seconds} seconds")
            time.sleep(1)

    def tags(self, repo):
        return [tag["name"] for tag in self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/tags") or []]

    def test_given_a_repository_when_migrate_mirrors_it_then_forgejo_pushes_its_branches_and_a_new_tag_to_the_target(self):
        # The second repository of this very server stands in for the GitHub copy. Forgejo only pushes to a
        # local address because the container allows it ([migrations] ALLOW_LOCALNETWORKS, set in setUpClass
        # for the import tests; production keeps the default, and its target is github.com).
        source, target = "mirror-source", "mirror-target"
        self.create_repository(source)
        owner = self.api[OWNER]
        owner.request("POST", "/user/repos", {"name": target, "private": True})
        catalog = self.migrate_catalog()
        address = f"http://127.0.0.1:3000/{OWNER}/{target}.git"
        env = {"MIRROR_GITHUB_USER": OWNER, "MIRROR_GITHUB_TOKEN": self.tokens[OWNER]}
        code, out, err = self.migrate_run(["mirrors", "--only", source], catalog, [source], env, lambda name: address)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"{source}: push mirror created", f"{source}: sync requested"])
        self.assertNotIn(self.tokens[OWNER], out + err)

        # What Forgejo lists: no credentials in the address, and what was asked for.
        (mirror,) = owner.request("GET", f"/repos/{OWNER}/{source}/push_mirrors")
        self.assertNotIn("@", mirror["remote_address"])
        self.assertTrue(migrate._same_address(mirror["remote_address"], address), mirror["remote_address"])
        self.assertEqual((mirror["interval"], mirror["sync_on_commit"]), ("8h0m0s", True))

        # The sync asked for pushes the branches that are there...
        self.eventually(lambda: self.branch_exists(target, "develop"), "the develop branch on the target")
        self.eventually(lambda: self.head(target, "main") == self.head(source, "main"), "main on the target")

        # ...and a tag pushed to the source later goes with the next sync.
        clone = self.clone(OWNER, source)
        self.assertEqual(self.git(OWNER, "tag", "v0.0.1", cwd=clone).returncode, 0)
        pushed = self.git(OWNER, "push", "origin", "v0.0.1", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        owner.request("POST", f"/repos/{OWNER}/{source}/push_mirrors-sync")
        self.eventually(lambda: "v0.0.1" in self.tags(target), "the tag on the target")

        # Run again: the mirror is there and has pushed, so nothing is written.
        code, out, err = self.migrate_run(["mirrors", "--only", source], catalog, [source], env, lambda name: address)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"{source}: push mirror already there"])

        # verify reads it back: the mirror Forgejo lists has pushed (last_update) and has no error, and the
        # refs of the target -- standing in for GitHub's copy, see GitHubOnForgejo -- are the source's.
        self.eventually(lambda: self.tags(target) == self.tags(source), "the tags on the target")
        default = owner.request("GET", f"/repos/{OWNER}/{source}")["default_branch"]
        github = GitHubOnForgejo([github_repository(source, default_branch=default, private=True)], owner, {source: target},
                                 login=OWNER, settings={source: {"has_issues": False}})
        code, out, err = self.migrate_run(["verify", "--only", source], catalog, env=env,
                                          mirror_to=lambda name: address, github=github)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([line.split("\t")[1:3] for line in out.splitlines()[:-1]],
                         [["on Forgejo", "ok"], ["default branch", "ok"], ["visibility", "ok"], ["push mirror", "ok"],
                          ["refs", "ok"], ["rulesets", "ok"], ["issues", "ok"]])
        self.assertEqual(out.splitlines()[-1], "1 repositories, 0 checks failed")
        # develop, main and the tag were compared, not nothing; and Forgejo's refs/pull/* did not count.
        self.assertIn("\t3 refs the same", out)
        self.assertEqual(github.writes(), [])

        # A tag only the target (GitHub's stand-in) has is a failure naming the tag and the side. (On the
        # target, which nothing syncs again: a tag pushed to the source could be mirrored before verify looks.)
        clone = self.clone(OWNER, target)
        self.assertEqual(self.git(OWNER, "tag", "v0.0.2", cwd=clone).returncode, 0)
        pushed = self.git(OWNER, "push", "origin", "v0.0.2", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        code, out, err = self.migrate_run(["verify", "--only", source], catalog, env=env,
                                          mirror_to=lambda name: address, github=github)
        self.assertEqual(code, 1, out)
        self.assertIn(f"{source}\trefs\tFAIL\trefs/tags/v0.0.2 only on GitHub", out.splitlines())
        self.assertEqual(github.writes(), [])

    def test_given_a_dependabot_file_when_migrate_prepares_github_then_forgejo_deletes_it_on_the_default_branch(self):
        # prepare-github's Forgejo side, against this server: the contents API finds the file on the
        # repository's default branch and deletes it in one commit by the token's owner. GitHub's side is the
        # fake's (test_migrate.py pins it); here it only has to let the run through.
        repo, path = "dependabot-lib", ".github/dependabot.yml"
        owner = self.api[OWNER]
        owner.request("POST", "/user/repos", {"name": repo, "private": True, "auto_init": True,
                                              "default_branch": "develop"})
        self.change_file(OWNER, repo, path, "version: 2\nupdates: []\n", "develop")
        catalog = self.migrate_catalog()
        github = FakeGitHub([github_repository(repo, default_branch="develop", private=True)], login=OWNER)

        def on_develop():
            try:
                return owner.request("GET", f"/repos/{OWNER}/{repo}/contents/{urllib.parse.quote(path)}?ref=develop")
            except rules.ApiError as error:
                if error.status == 404:
                    return None
                raise

        code, out, err = self.migrate_run(["--dry-run", "prepare-github", "--only", repo], catalog, github=github)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"{repo}: would delete {path} on develop", out.splitlines())
        self.assertIsNotNone(on_develop())  # a dry run deletes nothing
        self.assertEqual(github.writes(), [])

        code, out, err = self.migrate_run(["prepare-github", "--only", repo], catalog, github=github)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[0], f"{repo}: deleted {path} on develop")
        self.assertIsNone(on_develop())
        (latest,) = owner.request("GET", f"/repos/{OWNER}/{repo}/commits?sha=develop&limit=1&stat=false")[:1]
        self.assertEqual(latest["commit"]["message"].strip(), migrate.DEPENDABOT_MESSAGE)
        self.assertEqual(latest["commit"]["author"]["name"], OWNER)

        # Run again: no file left, GitHub prepared already; nothing is written.
        code, out, err = self.migrate_run(["prepare-github", "--only", repo], catalog, github=github)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"{repo}: no dependabot file", f"{repo}: already prepared on GitHub"])

    def test_given_what_only_the_target_has_when_migrate_mirrors_then_it_is_refused_until_allow_prune(self):
        # The guard before the first --mirror push, against this server's answers: the target (GitHub's
        # stand-in) starts as a copy of the source, then gets a branch of its own and a commit on main that
        # the source never had, while the source's develop moves on. Forgejo's compare must answer 404 for the
        # commit it lacks and count none for the one older in the source's develop.
        source, target = "prune-source", "prune-target"
        self.create_repository(source)
        owner = self.api[OWNER]
        owner.request("POST", "/user/repos", {"name": target, "private": True})
        clone = self.clone(OWNER, source)
        pushed = self.git(OWNER, "push", f"{self.url}/{OWNER}/{target}.git", "refs/remotes/origin/main:refs/heads/main",
                          "refs/remotes/origin/develop:refs/heads/develop", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        # Forgejo takes the first push into an empty repository in the background: until then its API still
        # calls it empty and refuses a branch made from develop.
        self.eventually(lambda: not owner.request("GET", f"/repos/{OWNER}/{target}")["empty"]
                        and self.branch_exists(target, "develop"), "the pushed develop on the target", seconds=60)
        self.change_file(OWNER, source, "ahead.txt", "the source moved on\n", "develop")
        owner.request("POST", f"/repos/{OWNER}/{target}/branches",
                      {"new_branch_name": "github-only", "old_branch_name": "develop"})
        lost = self.change_file(OWNER, target, "only-on-github.txt", "pushed to GitHub after the import\n", "main")

        catalog = self.migrate_catalog()
        address = f"http://127.0.0.1:3000/{OWNER}/{target}.git"
        env = {"MIRROR_GITHUB_USER": OWNER, "MIRROR_GITHUB_TOKEN": self.tokens[OWNER]}
        github = GitHubOnForgejo([github_repository(source, default_branch="main", private=True)], owner,
                                 {source: target}, login=OWNER)

        code, out, err = self.migrate_run(["mirrors", "--only", source], catalog, env=env,
                                          mirror_to=lambda name: address, github=github)
        self.assertEqual(code, 1, out)
        (failure,) = [line for line in err.splitlines() if line.startswith(f"  {source}: ")]
        self.assertIn("refs/heads/github-only only on GitHub", failure)
        self.assertIn(f"GitHub's main at {lost[:7]} is not in Forgejo's main ({self.head(source, 'main')[:7]}): "
                      "the first push would rewind it", failure)
        self.assertNotIn("develop", failure)  # the target's develop is older in the source's develop
        self.assertEqual(owner.request("GET", f"/repos/{OWNER}/{source}/push_mirrors"), [])
        self.assertTrue(self.branch_exists(target, "github-only"))

        code, out, err = self.migrate_run(["mirrors", "--only", source, "--allow-prune"], catalog, env=env,
                                          mirror_to=lambda name: address, github=github)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[1:], [f"{source}: push mirror created", f"{source}: sync requested"])
        self.assertTrue(out.splitlines()[0].startswith(f"{source}: --allow-prune: "), out)
        # What --allow-prune let go is gone from the target, and it holds the source's state.
        self.eventually(lambda: not self.branch_exists(target, "github-only"), "the target's own branch deleted")
        self.eventually(lambda: self.head(target, "main") == self.head(source, "main"), "main on the target")
        self.eventually(lambda: self.head(target, "develop") == self.head(source, "develop"), "develop on the target")

    def test_given_github_branches_outside_forgejos_same_branch_when_migrate_mirrors_then_they_are_refused_and_the_unsynced_mirror_goes(self):
        # The guard's ancestry, as Forgejo 16's compare decides it (two counts: GitHub's commit over Forgejo's
        # head, then Forgejo's head over GitHub's commit). The target (GitHub's stand-in) has main fast-forwarded
        # to the head of the source's develop -- a commit the source has, on develop only, so its main would be
        # set back --, pages at the source's main, a commit the source has but unrelated to the source's own
        # pages (an orphan branch), develop equal to the source's, and a feature branch one commit behind the
        # source's. Main and pages are refused; develop and feature are not.
        source, target = "ancestry-source", "ancestry-target"
        self.create_repository(source)
        owner = self.api[OWNER]
        develop = self.change_file(OWNER, source, "develop.txt", "on develop only\n", "develop")
        behind = self.change_file(OWNER, source, "feature.txt", "the feature starts\n", "develop", new_branch="feature")
        ahead = self.change_file(OWNER, source, "feature.txt", "the feature goes on\n", "feature")
        main = self.head(source, "main")
        self.assertNotEqual(main, develop)
        clone = self.clone(OWNER, source)
        for step in (("checkout", "--quiet", "--orphan", "pages"), ("commit", "--quiet", "-m", "pages: its own history"),
                     ("push", "--quiet", "origin", "pages")):
            done = self.git(OWNER, *step, cwd=clone)
            self.assertEqual(done.returncode, 0, done.stderr)
        orphan = self.head(source, "pages")
        owner.request("POST", "/user/repos", {"name": target, "private": True})
        pushed = self.git(OWNER, "push", f"{self.url}/{OWNER}/{target}.git", f"{develop}:refs/heads/main",
                          f"{develop}:refs/heads/develop", f"{behind}:refs/heads/feature", f"{main}:refs/heads/pages",
                          cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.eventually(lambda: not owner.request("GET", f"/repos/{OWNER}/{target}")["empty"]
                        and self.branch_exists(target, "pages"), "the pushed branches on the target", seconds=60)

        # What the guard relies on, straight from Forgejo: none for an ancestor or the head itself, and the head
        # counts at least one over its ancestor; one for a commit of another branch; none either way for two
        # unrelated commits (no merge base: why the second count is needed); 404 for an unknown commit.
        def counted(base, head):
            return owner.request("GET", f"/repos/{OWNER}/{source}/compare/{base}...{head}")["total_commits"]

        self.assertEqual((counted(ahead, behind), counted(behind, ahead), counted(develop, develop),
                          counted(main, develop)), (0, 1, 0, 1))
        self.assertEqual((counted(orphan, main), counted(main, orphan)), (0, 0))
        with self.assertRaises(rules.ApiError) as unknown:
            counted(main, "d4" * 20)
        self.assertEqual(unknown.exception.status, 404)

        catalog = self.migrate_catalog()
        address = f"http://127.0.0.1:3000/{OWNER}/{target}.git"
        env = {"MIRROR_GITHUB_USER": OWNER, "MIRROR_GITHUB_TOKEN": self.tokens[OWNER]}
        github = GitHubOnForgejo([github_repository(source, default_branch="main", private=True)], owner,
                                 {source: target}, login=OWNER)
        # The mirror a run that stopped before its sync leaves: made, never pushed. (Its own push is set off
        # by a commit or the interval, neither of which comes during this test.)
        owner.request("POST", f"/repos/{OWNER}/{source}/push_mirrors",
                      {"remote_address": address, "remote_username": OWNER, "remote_password": self.tokens[OWNER],
                       "interval": "8h0m0s", "sync_on_commit": False})
        (left,) = owner.request("GET", f"/repos/{OWNER}/{source}/push_mirrors")
        self.assertFalse(migrate._synced(left), left)

        code, out, err = self.migrate_run(["--dry-run", "mirrors", "--only", source], catalog, env=env,
                                          mirror_to=lambda name: address, github=github)
        self.assertEqual(code, 1, out)
        self.assertIn(f"{source}: would delete its unsynced push mirror so Forgejo can't push it on its own",
                      out.splitlines())
        self.assertEqual(len(owner.request("GET", f"/repos/{OWNER}/{source}/push_mirrors")), 1)

        code, out, err = self.migrate_run(["mirrors", "--only", source], catalog, env=env,
                                          mirror_to=lambda name: address, github=github)
        self.assertEqual(code, 1, out)
        (failure,) = [line for line in err.splitlines() if line.startswith(f"  {source}: ")]
        self.assertIn(f"GitHub's main at {develop[:7]} is not in Forgejo's main ({main[:7]}): "
                      "the first push would rewind it", failure)
        self.assertIn(f"GitHub's pages at {main[:7]} is not in Forgejo's pages ({orphan[:7]}): "
                      "the first push would rewind it", failure)
        self.assertNotIn("develop", failure)
        self.assertNotIn("feature", failure)
        # The refused mirror is gone (Forgejo 16 takes `DELETE .../push_mirrors/<remote_name>`), and nothing
        # was pushed: no sync was asked for.
        self.assertEqual(out.splitlines()[-1],
                         f"{source}: deleted its unsynced push mirror so Forgejo can't push it on its own")
        self.assertEqual(owner.request("GET", f"/repos/{OWNER}/{source}/push_mirrors"), [])
        self.assertEqual((self.head(target, "main"), self.head(target, "pages")), (develop, main))

    def test_given_a_catalog_repository_when_migrate_sets_the_collaborators_then_each_bot_reads_back_with_its_permission(self):
        repo = "bots-lib"
        owner = self.api[OWNER]
        # develop is its default branch, as rules.py leaves every catalog repository (verify expects it).
        owner.request("POST", "/user/repos", {"name": repo, "private": True, "auto_init": True,
                                              "default_branch": "develop"})
        catalog = self.migrate_catalog(systems=[], repositories=[{"id": repo}])

        def permission(user):
            try:
                found = owner.request("GET", f"/repos/{OWNER}/{repo}/collaborators/{user}/permission")["permission"]
            except rules.ApiError as error:
                if error.status == 404:
                    return None
                raise
            # Forgejo 16 answers 200 {"permission": "none"} for one who is not a collaborator.
            return None if found == "none" else found

        # Two of the bots have no account yet: each is named once, and the others are still set.
        self.assertEqual((permission("jenkins"), permission("renovate")), (None, None))
        code, out, err = self.migrate_run(["collaborators"], catalog)
        self.assertEqual(code, 1, out)
        self.assertEqual(err.splitlines()[:3], ["2 failures:", "  jenkins-ci: not on Forgejo: create it first (docs/setup.md 9.7)",
                                                "  renovate: not on Forgejo: create it first (docs/setup.md 9.7)"])
        # (A failure is printed where it happens, then listed again at the end.)
        self.assertEqual(out.splitlines()[2:], [f"{repo}: jenkins: write granted", f"{repo}: claude: write granted"])
        self.assertEqual((permission("jenkins"), permission("claude")), ("write", "write"))

        for user in ("jenkins-ci", "renovate"):
            self.admin_cli("create", "--username", user, "--password", f"throwaway-{uuid.uuid4().hex}",
                           "--email", f"{user}@example.com", "--must-change-password=false")
        code, out, err = self.migrate_run(["collaborators"], catalog)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"{repo}: jenkins: already write", f"{repo}: jenkins-ci: read granted",
                                            f"{repo}: renovate: write granted", f"{repo}: claude: already write"])
        self.assertEqual({user: permission(user) for user in ("jenkins", "jenkins-ci", "renovate", "claude")},
                         {"jenkins": "write", "jenkins-ci": "read", "renovate": "write", "claude": "write"})

        # A bot whose permission was changed is brought back; a second run changes nothing.
        owner.request("PUT", f"/repos/{OWNER}/{repo}/collaborators/claude", {"permission": "read"})
        code, out, err = self.migrate_run(["collaborators"], catalog)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"{repo}: claude: changed from read to write", out.splitlines())
        self.assertEqual(permission("claude"), "write")
        code, out, err = self.migrate_run(["collaborators"], catalog)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertTrue(all(": already " in line for line in out.splitlines()), out)

        # verify reads the webhook and the collaborators back from Forgejo's own lists. GitHub's side is this
        # repository itself (GitHubOnForgejo), so the refs agree; it has no push mirror, the one failure.
        jenkins = "http://jenkins.invalid"
        env = {"FORGE_JENKINS_URL": jenkins}

        def verified():
            code, out, err = self.migrate_run(["verify", "--only", repo], catalog, env=env, github=self.github_of(repo))
            return code, out, {line.split("\t")[1]: line.split("\t")[2:] for line in out.splitlines() if "\t" in line}

        code, out, rows = verified()
        self.assertEqual(rows["webhook"][0], "FAIL", out)
        self.assertIn(f"{jenkins}/gitea-webhook/post", rows["webhook"][1])
        self.assertEqual(rows["collaborators"], ["ok", "jenkins write, jenkins-ci read, renovate write, claude write"])
        owner.request("POST", f"/repos/{OWNER}/{repo}/hooks",
                      rules.forgejo_webhook({"url": f"{jenkins}/gitea-webhook/post", "secret": "a-throwaway-secret"}))
        code, out, rows = verified()
        self.assertEqual({check: row[0] for check, row in rows.items()},
                         {"on Forgejo": "ok", "default branch": "ok", "visibility": "ok", "push mirror": "FAIL", "refs": "ok",
                          "rulesets": "ok", "issues": "ok", "webhook": "ok", "collaborators": "ok"}, out)
        self.assertEqual((code, rows["push mirror"][1]), (1, "no push mirror"))

        # A bot taken off the repository is named: the collaborators list no longer has it.
        owner.request("DELETE", f"/repos/{OWNER}/{repo}/collaborators/renovate")
        code, out, rows = verified()
        self.assertEqual(rows["collaborators"], ["FAIL", "renovate: not a collaborator"], out)

    def github_of(self, repo):
        """A GitHub for verify whose copy of `repo` is the repository itself, prepared as verify wants it."""
        found = self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}")
        return GitHubOnForgejo([github_repository(repo, default_branch=found["default_branch"], private=found["private"])],
                               self.api[OWNER], login=OWNER,
                               settings={repo: {"has_issues": False}},
                               rulesets={repo: [{"id": 1, "name": "mirror tags", "enforcement": "active"}]})


class CatalogTests(unittest.TestCase):
    def test_given_the_live_suites_catalog_when_validated_then_it_is_a_valid_catalog(self):
        # So the rules applied above are the ones a real installation would get.
        self.assertEqual(catalog_module.validate(test_catalog()), [])


if __name__ == "__main__":
    unittest.main()
