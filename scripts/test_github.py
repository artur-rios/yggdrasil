"""Tests for scripts/github.sh, against a fake `curl` on PATH.   python3 -m unittest discover -s scripts

The fake answers each request from the queue of responses given for its path, so these need bash
and jq (which the Jenkins agent image has) but no network.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

SCRIPT = pathlib.Path(__file__).resolve().parent / "github.sh"

FAKE_CURL = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # A stand-in for curl: the response to <path> is the next of $FAKE_CURL_DIR/<key>.<n>, where
    # <key> is the path's last segment without its query, and each holds "<exit code> <body>". The
    # file a --data-binary @<file> sends (forge.sh's request bodies) is copied to <key>.body.<n>.
    #
    # The exit code may carry the HTTP status as "<exit code>:<status>" (default 200, or 500 for 22).
    # Asked for --write-out and not --fail (github.sh's raw requests), curl answers like the real one: exit
    # 0 whatever the status (but a real failure such as 6 still fails), the body, then the format with
    # %{http_code} and %{redirect_url} (from <key>.redirect.<n>) filled in. With --fail (forge.sh's
    # requests, which these tests have always answered without a status line) the format is ignored. A body "@<file>" is that file's bytes, which is
    # how a zip is served; --output <file> puts the body there instead of on stdout.
    url=${*: -1}
    key=${url%%\?*}
    key=${key##*/}
    count_file="$FAKE_CURL_DIR/$key.count"
    n=$(($(cat "$count_file" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$count_file"
    printf '%s\n' "$*" >"$FAKE_CURL_DIR/$key.request.$n"
    cat >"$FAKE_CURL_DIR/$key.config.$n"
    previous=""
    output="" write_out="" fail=""
    for argument in "$@"; do
      [[ "$previous" != --data-binary || "$argument" != @* ]] || cp "${argument#@}" "$FAKE_CURL_DIR/$key.body.$n"
      [[ "$argument" != --fail* ]] || fail=1
      [[ "$previous" != --output ]] || output=$argument
      [[ "$previous" != --write-out ]] || write_out=$argument
      previous=$argument
    done
    response="$FAKE_CURL_DIR/$key.$n"
    [[ -f "$response" ]] || response="$FAKE_CURL_DIR/$key.last"
    read -r status body <"$response"
    http=${status#*:}
    [[ "$http" != "$status" ]] || { http=200; [[ "$status" != 22 ]] || http=500; }
    status=${status%%:*}
    if [[ "$body" == @* ]]; then
      cp "${body#@}" "${output:-/dev/stdout}"
    elif [[ -n "$output" ]]; then
      printf '%s' "$body" >"$output"
    else
      printf '%s' "$body"
    fi
    if [[ -n "$write_out" && -z "$fail" ]]; then
      [[ "$status" != 22 ]] || status=0
      redirect=$(cat "$FAKE_CURL_DIR/$key.redirect.$n" 2>/dev/null || true)
      write_out=${write_out//%\{http_code\}/$http}
      write_out=${write_out//%\{redirect_url\}/$redirect}
      printf '%b' "$write_out"
    fi
    exit "$status"
""").lstrip()

SUITES_DONE = {"check_suites": [{"app": {"slug": "github-actions"}, "status": "completed"}]}


def runs(*checks):
    return {"check_runs": [{"app": {"slug": "github-actions"}, "name": name, "status": "completed",
                            "conclusion": conclusion} for name, conclusion in checks]}



def check_run(name, status="completed", conclusion="success", **more):
    """A check run as the check-runs list gives it, from GitHub Actions unless app= says otherwise."""
    return {"id": 1, "app": {"slug": "github-actions"}, "name": name, "status": status, "conclusion": conclusion,
            "started_at": "2026-10-23T10:00:00Z", "html_url": f"https://github.com/someone/app/runs/{name}", **more}


def now(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and os.name == "posix",
                     "needs bash and jq on a POSIX system")
class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="github-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.state = self.temp / "state"
        self.state.mkdir()
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_CURL_DIR=str(self.state),
                        GH_TOKEN="token", GITHUB_OWNER="someone", GITHUB_POLL_SECONDS="0",
                        GITHUB_DISPATCH_POLL_SECONDS="0", GITHUB_DISPATCH_TIMEOUT="1")

    def respond(self, key, *responses, last=None):
        for n, (status, body) in enumerate(responses, start=1):
            (self.state / f"{key}.{n}").write_text(f"{status} {json.dumps(body) if body is not None else ''}\n")
        if last is not None:
            (self.state / f"{key}.last").write_text(f"{last[0]} {json.dumps(last[1])}\n")

    def github(self, *arguments):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=self.env, capture_output=True, text=True,
                              timeout=60)

    def request(self, key, number=1):
        return (self.state / f"{key}.request.{number}").read_text()

    def body(self, key, number=1):
        """The JSON body forge.sh sent (from the file it gives curl with --data-binary)."""
        return json.loads((self.state / f"{key}.body.{number}").read_text())

    def test_given_every_check_passed_when_waiting_then_it_succeeds(self):
        self.respond("check-suites", last=(0, SUITES_DONE))
        self.respond("check-runs", last=(0, runs(("branch-policy", "success"), ("test", "skipped"))))
        result = self.github("wait-checks", "app", "abc123", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed", result.stdout)

    def test_given_a_failed_check_when_waiting_then_it_fails_naming_it(self):
        self.respond("check-suites", last=(0, SUITES_DONE))
        self.respond("check-runs", last=(0, runs(("branch-policy", "success"), ("test", "failure"))))
        result = self.github("wait-checks", "app", "abc123", "60")
        self.assertEqual(result.returncode, 1)
        self.assertIn("test: failure", result.stderr)

    def test_given_github_fails_to_answer_once_when_waiting_then_it_asks_again(self):
        self.respond("check-suites", (22, {"message": "Server Error"}), last=(0, SUITES_DONE))
        self.respond("check-runs", last=(0, runs(("branch-policy", "success"))))
        result = self.github("wait-checks", "app", "abc123", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Server Error", result.stderr)

    def test_given_github_never_answers_when_waiting_then_it_gives_up(self):
        self.respond("check-suites", last=(22, {"message": "Server Error"}))
        result = self.github("wait-checks", "app", "abc123", "60")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not answer", result.stderr)

    def test_given_any_request_when_made_then_the_token_travels_in_curls_config_not_its_arguments(self):
        self.respond("check-suites", last=(0, SUITES_DONE))
        self.respond("check-runs", last=(0, runs(("branch-policy", "success"))))
        self.respond("merge", (0, {"sha": "def456"}))
        for arguments in (("wait-checks", "app", "abc123", "60"), ("merge-pr", "app", "7", "abc123", "title")):
            self.assertEqual(self.github(*arguments).returncode, 0, arguments)
        for key in ("check-suites", "merge"):
            self.assertNotIn("token", self.request(key))
            self.assertIn('header = "Authorization: Bearer token"', (self.state / f"{key}.config.1").read_text())

    def test_given_a_merge_github_refuses_when_merging_then_its_reason_is_shown(self):
        self.respond("merge", (22, {"message": "Required status check \"deploy/production\" is expected."}))
        result = self.github("merge-pr", "app", "7", "abc123", "release: v1.0.0 (#7)")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deploy/production", result.stderr)
        self.assertNotIn("null", result.stdout)

    def test_given_a_merge_when_merged_then_it_prints_the_merge_commit(self):
        self.respond("merge", (0, {"sha": "def456", "merged": True}))
        result = self.github("merge-pr", "app", "7", "abc123", "release: v1.0.0 (#7)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "def456")

    def test_given_a_version_when_releasing_then_it_prints_the_release_url(self):
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.github("release", "app", "1.2.3", "abc123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "https://example/release/17\n")
        self.assertEqual(self.body("releases")["tag_name"], "v1.2.3")

    def test_given_a_status_when_set_then_forge_sh_posts_it_to_the_commit(self):
        self.respond("abc123", (0, {"id": 1}))
        result = self.github("set-status", "app", "abc123", "success", "deploy/production", "Deployed",
                             "https://jenkins/job/1")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        request = self.request("abc123")
        self.assertIn("--request POST", request)
        self.assertTrue(request.strip().endswith("https://api.github.com/repos/someone/app/statuses/abc123"), request)
        self.assertEqual(self.body("abc123"), {"state": "success", "context": "deploy/production",
                                               "description": "Deployed", "target_url": "https://jenkins/job/1"})
        self.assertIn('header = "Authorization: Bearer token"', (self.state / "abc123.config.1").read_text())

    def test_given_a_branch_github_already_deleted_when_deleting_then_it_succeeds(self):
        self.respond("1.0.0", (22, {"message": "Reference does not exist"}))
        result = self.github("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        # forge.sh's note, which belongs on stderr like every message that is not an answer.
        self.assertIn("already deleted", result.stderr)

    def test_given_a_branch_github_refuses_to_delete_when_deleting_then_it_fails(self):
        self.respond("1.0.0", (22, {"message": "Resource not accessible by integration"}))
        result = self.github("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not accessible", result.stderr)

    def test_given_a_release_when_publishing_then_it_stays_draft_until_the_workflow_finishes(self):
        self.respond("refs", (0, {"ref": "refs/tags/v1.2.3"}))
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "https://example/release/17"}))
        self.respond("dispatches", (0, None))
        self.respond(
            "17",
            (0, {"tag_name": "v1.2.3", "draft": True, "html_url": "https://example/release/17"}),
            (0, {"tag_name": "v1.2.3", "draft": False, "html_url": "https://example/release/17"}),
        )

        result = self.github("publish-release", "app", "1.2.3", "abc123", "60")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "https://example/release/17")
        dispatch_request = "".join(self.request("dispatches").split())
        self.assertEqual(self.body("refs"), {"ref": "refs/tags/v1.2.3", "sha": "abc123"})
        self.assertTrue(self.body("releases")["draft"])
        self.assertIn('"ref":"v1.2.3"', dispatch_request)
        self.assertIn('"release_id":"17"', dispatch_request)
        self.assertEqual((self.state / "17.count").read_text().strip(), "2")

    def test_given_a_draft_without_a_page_address_when_publishing_then_it_fails_before_the_workflow(self):
        self.respond("refs", (0, {"ref": "refs/tags/v1.2.3"}))
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": None}))
        self.respond("dispatches", (0, None))

        result = self.github("publish-release", "app", "1.2.3", "abc123", "60")

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("no release id and URL", result.stderr)
        self.assertFalse((self.state / "dispatches.count").exists())

    def test_given_workflow_dispatch_is_refused_when_publishing_then_the_draft_stays_private(self):
        self.respond("refs", (0, {"ref": "refs/tags/v1.2.3"}))
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "https://example/release/17"}))
        self.respond("dispatches", (22, {"message": "Resource not accessible by integration"}))

        result = self.github("publish-release", "app", "1.2.3", "abc123", "60")

        self.assertEqual(result.returncode, 1)
        self.assertIn("not accessible", result.stderr)
        self.assertFalse((self.state / "17.count").exists())

    def test_given_the_packaging_workflow_never_publishes_when_waiting_then_it_times_out(self):
        self.respond("refs", (0, {"ref": "refs/tags/v1.2.3"}))
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "https://example/release/17"}))
        self.respond("dispatches", (0, None))
        self.respond(
            "17",
            last=(0, {"tag_name": "v1.2.3", "draft": True, "html_url": "https://example/release/17"}),
        )

        result = self.github("publish-release", "app", "1.2.3", "abc123", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stderr)

    # -- actions-status: one look at the GitHub Actions check runs of a commit --------------------------

    def look(self, *names, sha="abc123"):
        return self.github("actions-status", "app", sha, *names)

    def test_given_named_checks_all_successful_when_looked_at_then_exit_0_with_their_urls(self):
        self.respond("check-runs", (0, {"check_runs": [check_run("test"), check_run("lint", conclusion="skipped"),
                                                       check_run("other")]}))
        result = self.look("test", "lint")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["test\tsuccess\thttps://github.com/someone/app/runs/test",
                                                      "lint\tsuccess\thttps://github.com/someone/app/runs/lint"])
        # One look: the commit's runs, fetched once, with the token in curl's config.
        self.assertEqual((self.state / "check-runs.count").read_text().strip(), "1")
        self.assertNotIn("token", self.request("check-runs"))
        self.assertIn('header = "Authorization: Bearer token"', (self.state / "check-runs.config.1").read_text())

    def test_given_a_named_check_not_there_yet_when_looked_at_then_exit_3(self):
        self.respond("check-runs", (0, {"check_runs": [check_run("test")]}))
        result = self.look("test", "windows")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], "windows\tpending\t")

    def test_given_a_check_still_running_when_looked_at_then_exit_3(self):
        self.respond("check-runs", (0, {"check_runs": [check_run("test", status="in_progress", conclusion=None)]}))
        result = self.look("test")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(result.stdout.startswith("test\tpending\t"), result.stdout)

    def test_given_a_commit_github_does_not_know_when_looked_at_then_exit_3(self):
        # The mirror has not pushed it yet: 422 "No commit found for SHA", or 404 for some paths.
        for status, message in (("22:422", "No commit found for SHA: abc123"), ("22:404", "Not Found")):
            with self.subTest(status=status):
                self.respond("check-runs", (status, {"message": message}))
                (self.state / "check-runs.count").unlink(missing_ok=True)
                result = self.look("test")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, "test\tpending\t\n")

    def test_given_a_failed_check_when_looked_at_then_exit_1_naming_it(self):
        # A failure is final even while other checks run, so the pipeline need not wait for them.
        for conclusion in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                self.respond("check-runs", (0, {"check_runs": [
                    check_run("test", conclusion=conclusion), check_run("lint", status="queued", conclusion=None)]}))
                (self.state / "check-runs.count").unlink(missing_ok=True)
                result = self.look("test", "lint")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("test", result.stderr)
                self.assertIn(conclusion, result.stderr)
                self.assertIn("test\tfailure\t", result.stdout)

    def test_given_github_answering_500_when_looked_at_then_exit_2(self):
        for status in ("22:500", "22:502", "22:401", "6"):
            with self.subTest(status=status):
                self.respond("check-runs", (status, {"message": "Server Error"}))
                (self.state / "check-runs.count").unlink(missing_ok=True)
                result = self.look("test")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_given_no_names_when_looked_at_then_every_actions_run_counts(self):
        self.respond("check-runs", (0, {"check_runs": [
            check_run("test"), check_run("lint", conclusion="neutral"),
            check_run("jenkins", app={"slug": "jenkins"}, conclusion="failure"),  # not GitHub Actions: ignored
            check_run("windows", status="in_progress", conclusion=None)]}))
        result = self.look()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual([line.split("\t")[:2] for line in result.stdout.splitlines()],
                         [["lint", "success"], ["test", "success"], ["windows", "pending"]])

    def test_given_no_names_and_no_runs_yet_when_looked_at_then_exit_3(self):
        # Nothing has started on the mirror: that is not "everything passed".
        self.respond("check-runs", (0, {"check_runs": []}))
        self.assertEqual(self.look().returncode, 3)

    def test_given_a_check_run_again_when_looked_at_then_the_latest_run_counts(self):
        self.respond("check-runs", (0, {"check_runs": [
            check_run("test", id=7, conclusion="failure"), check_run("test", id=9, conclusion="success"),
            check_run("lint", id=3, conclusion="success"), check_run("lint", id=2, conclusion="failure")]}))
        result = self.look("test", "lint")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("\n"), 2)

    def test_given_more_runs_than_a_page_when_looked_at_then_every_page_is_read(self):
        first = [check_run(f"c{i}", id=i) for i in range(100)]
        self.respond("check-runs", (0, {"check_runs": first}), (0, {"check_runs": [check_run("last", id=500)]}))
        result = self.look("c3", "last")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "check-runs.count").read_text().strip(), "2")

    # -- wait-ref: until the mirror has pushed a tag or branch ------------------------------------------

    def test_given_a_tag_not_yet_mirrored_when_waiting_for_it_then_it_times_out_with_2(self):
        self.respond("v1.2.0", last=("22:404", {"message": "Not Found"}))
        result = self.github("wait-ref", "app", "refs/tags/v1.2.0", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("v1.2.0", result.stderr)
        self.assertTrue(self.request("v1.2.0").strip().endswith("/repos/someone/app/git/ref/tags/v1.2.0"))

    def test_given_a_tag_that_appears_when_waiting_for_it_then_it_succeeds(self):
        self.respond("v1.2.0", ("22:404", {"message": "Not Found"}), ("22:500", {"message": "oops"}),
                     (0, {"ref": "refs/tags/v1.2.0"}))
        result = self.github("wait-ref", "app", "v1.2.0", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        # A bare name is a tag.
        self.assertTrue(self.request("v1.2.0", 3).strip().endswith("/repos/someone/app/git/ref/tags/v1.2.0"))

    def test_given_a_branch_ref_when_waiting_for_it_then_heads_are_asked(self):
        self.respond("1.0.0", (0, {"ref": "refs/heads/release/1.0.0"}))
        result = self.github("wait-ref", "app", "refs/heads/release/1.0.0", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.request("1.0.0").strip().endswith("/repos/someone/app/git/ref/heads/release/1.0.0"))

    # -- dispatch / run-status -------------------------------------------------------------------------

    def dispatch(self, *arguments, ref="v1.2.0"):
        return self.github("dispatch", "app", "release-windows.yml", ref, *arguments)

    def workflow_run(self, run_id, created, branch="v1.2.0", event="workflow_dispatch"):
        return {"id": run_id, "created_at": created, "head_branch": branch, "event": event}

    def dispatched_body(self):
        """The JSON api() sent with --data (it is on curl's command line, unlike the token)."""
        return json.loads(self.request("dispatches").split(" --data ", 1)[1].rsplit(" https://", 1)[0])

    def test_given_a_dispatch_when_its_run_appears_then_its_id_is_printed(self):
        self.respond("dispatches", ("0:204", None))
        self.respond("runs",
                     (0, {"workflow_runs": [self.workflow_run(40, now(-3600))]}),  # an older release's run
                     (0, {"workflow_runs": [self.workflow_run(43, now(0)), self.workflow_run(41, now(-3600))]}))
        result = self.dispatch("tag=v1.2.0", "publish=a=b")
        self.assertEqual((result.returncode, result.stdout), (0, "43\n"), result.stderr)
        self.assertEqual(self.dispatched_body(), {"ref": "v1.2.0", "inputs": {"tag": "v1.2.0", "publish": "a=b"}})
        self.assertTrue(self.request("dispatches").strip().endswith(
            "/repos/someone/app/actions/workflows/release-windows.yml/dispatches"))
        self.assertIn("event=workflow_dispatch", self.request("runs"))
        self.assertIn("branch=v1.2.0", self.request("runs"))
        self.assertEqual((self.state / "runs.count").read_text().strip(), "2")

    def test_given_a_full_tag_ref_when_dispatching_then_the_run_is_matched_by_the_tag_name(self):
        self.respond("dispatches", ("0:204", None))
        self.respond("runs", (0, {"workflow_runs": [self.workflow_run(43, now(0))]}))
        result = self.dispatch(ref="refs/tags/v1.2.0")
        self.assertEqual((result.returncode, result.stdout), (0, "43\n"), result.stderr)
        self.assertEqual(self.dispatched_body()["ref"], "refs/tags/v1.2.0")

    def test_given_a_run_of_another_ref_when_dispatching_then_it_is_not_taken(self):
        self.respond("dispatches", ("0:204", None))
        self.respond("runs", last=(0, {"workflow_runs": [self.workflow_run(50, now(0), branch="v9.9.9")]}))
        result = self.dispatch()
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("no run", result.stderr)

    def test_given_a_dispatch_github_refuses_when_dispatching_then_it_fails_without_looking_for_a_run(self):
        self.respond("dispatches", ("22:403", {"message": "Resource not accessible by integration"}))
        result = self.dispatch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not accessible", result.stderr)
        self.assertFalse((self.state / "runs.count").exists())

    def test_given_github_not_answering_a_dispatch_when_dispatching_then_exit_2(self):
        self.respond("dispatches", ("22:502", {"message": "Bad Gateway"}))
        self.assertEqual(self.dispatch().returncode, 2)

    def test_given_a_run_in_each_state_when_asked_then_it_maps_to_pending_success_or_failure(self):
        cases = [({"status": "in_progress", "conclusion": None}, 3, "pending"),
                 ({"status": "queued", "conclusion": None}, 3, "pending"),
                 ({"status": "completed", "conclusion": "success"}, 0, "success"),
                 ({"status": "completed", "conclusion": "skipped"}, 0, "success"),
                 ({"status": "completed", "conclusion": "failure"}, 1, "failure"),
                 ({"status": "completed", "conclusion": "cancelled"}, 1, "failure")]
        for run, code, state in cases:
            with self.subTest(run=run):
                self.respond("77", (0, {"id": 77, **run}))
                (self.state / "77.count").unlink(missing_ok=True)
                result = self.github("run-status", "app", "77")
                self.assertEqual((result.returncode, result.stdout), (code, state + "\n"), result.stderr)

    def test_given_github_not_answering_when_a_run_is_asked_then_exit_2(self):
        for status in ("22:500", "22:404", "6"):
            with self.subTest(status=status):
                self.respond("77", (status, {"message": "Server Error"}))
                (self.state / "77.count").unlink(missing_ok=True)
                result = self.github("run-status", "app", "77")
                self.assertEqual((result.returncode, result.stdout), (2, ""))

    # -- download-artifacts ----------------------------------------------------------------------------

    def zip_of(self, name, **files):
        path = self.temp / f"{name}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for member, content in files.items():
                archive.writestr(member.replace("__", "/"), content)
        return path

    def artifact(self, artifact_id, name, **more):
        return {"id": artifact_id, "name": name, "expired": False,
                "archive_download_url": f"https://api.github.com/repos/someone/app/actions/artifacts/{artifact_id}/zip",
                **more}

    def test_given_a_run_with_two_artifacts_when_downloaded_then_each_has_its_directory(self):
        self.respond("artifacts", (0, {"total_count": 2, "artifacts": [self.artifact(1, "win-x64"),
                                                                        self.artifact(2, "win-arm64")]}))
        self.respond("zip", ("0:302", None), ("0:302", None))
        (self.state / "zip.redirect.1").write_text("https://storage.example/blobs/one?sig=abc")
        (self.state / "zip.redirect.2").write_text("https://storage.example/blobs/two?sig=def")
        (self.state / "one.1").write_text(f"0 @{self.zip_of('one', **{'app.exe': 'x64', 'sub__readme.txt': 'hi'})}\n")
        (self.state / "two.1").write_text(f"0 @{self.zip_of('two', **{'app.exe': 'arm'})}\n")
        target = self.temp / "out"

        result = self.github("download-artifacts", "app", "99", str(target))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "win-x64" / "app.exe").read_text(), "x64")
        self.assertEqual((target / "win-x64" / "sub" / "readme.txt").read_text(), "hi")
        self.assertEqual((target / "win-arm64" / "app.exe").read_text(), "arm")
        self.assertIn("/repos/someone/app/actions/runs/99/artifacts?", self.request("artifacts"))

    def test_given_an_artifact_in_storage_when_downloaded_then_the_token_stays_with_github(self):
        self.respond("artifacts", (0, {"total_count": 1, "artifacts": [self.artifact(1, "win-x64")]}))
        self.respond("zip", ("0:302", None))
        (self.state / "zip.redirect.1").write_text("https://storage.example/blobs/one?sig=abc")
        (self.state / "one.1").write_text(f"0 @{self.zip_of('one', **{'app.exe': 'x64'})}\n")

        result = self.github("download-artifacts", "app", "99", str(self.temp / "out"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('header = "Authorization: Bearer token"', (self.state / "zip.config.1").read_text())
        # The storage request carries a signature in its URL and nothing of ours, and was never given the
        # config that holds the token.
        self.assertEqual((self.state / "one.config.1").read_text(), "")
        self.assertNotIn("token", self.request("one"))
        self.assertNotIn("Authorization", self.request("one"))
        self.assertNotIn("--location", self.request("zip"))

    def test_given_an_expired_artifact_when_downloaded_then_it_is_skipped_with_a_note(self):
        self.respond("artifacts", (0, {"total_count": 1, "artifacts": [self.artifact(1, "win-x64", expired=True)]}))
        result = self.github("download-artifacts", "app", "99", str(self.temp / "out"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("expired", result.stderr)
        self.assertFalse((self.state / "zip.count").exists())

    def test_given_an_artifact_name_that_climbs_out_when_downloaded_then_it_is_refused(self):
        self.respond("artifacts", (0, {"total_count": 1, "artifacts": [self.artifact(1, "..")]}))
        result = self.github("download-artifacts", "app", "99", str(self.temp / "out"))
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.state / "zip.count").exists())

    def test_given_a_download_url_on_another_host_when_downloaded_then_the_token_is_not_sent(self):
        evil = self.artifact(1, "win-x64", archive_download_url="https://evil.example/zip")
        self.respond("artifacts", (0, {"total_count": 1, "artifacts": [evil]}))
        result = self.github("download-artifacts", "app", "99", str(self.temp / "out"))
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.state / "zip.count").exists())

    def test_given_github_failing_the_download_when_downloaded_then_exit_2(self):
        self.respond("artifacts", (0, {"total_count": 1, "artifacts": [self.artifact(1, "win-x64")]}))
        self.respond("zip", ("22:503", None))
        result = self.github("download-artifacts", "app", "99", str(self.temp / "out"))
        self.assertEqual(result.returncode, 2)



if __name__ == "__main__":
    unittest.main()
