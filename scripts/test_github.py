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

SCRIPT = pathlib.Path(__file__).resolve().parent / "github.sh"

FAKE_CURL = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # A stand-in for curl: the response to <path> is the next of $FAKE_CURL_DIR/<key>.<n>, where
    # <key> is the path's last segment without its query, and each holds "<exit code> <body>".
    url=${*: -1}
    key=${url%%\?*}
    key=${key##*/}
    count_file="$FAKE_CURL_DIR/$key.count"
    n=$(($(cat "$count_file" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$count_file"
    response="$FAKE_CURL_DIR/$key.$n"
    [[ -f "$response" ]] || response="$FAKE_CURL_DIR/$key.last"
    read -r status body <"$response"
    printf '%s' "$body"
    exit "$status"
""").lstrip()

SUITES_DONE = {"check_suites": [{"app": {"slug": "github-actions"}, "status": "completed"}]}


def runs(*checks):
    return {"check_runs": [{"app": {"slug": "github-actions"}, "name": name, "status": "completed",
                            "conclusion": conclusion} for name, conclusion in checks]}


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
                        GH_TOKEN="token", GITHUB_OWNER="someone", GITHUB_POLL_SECONDS="0")

    def respond(self, key, *responses, last=None):
        for n, (status, body) in enumerate(responses, start=1):
            (self.state / f"{key}.{n}").write_text(f"{status} {json.dumps(body) if body is not None else ''}\n")
        if last is not None:
            (self.state / f"{key}.last").write_text(f"{last[0]} {json.dumps(last[1])}\n")

    def github(self, *arguments):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=self.env, capture_output=True, text=True,
                              timeout=60)

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

    def test_given_a_branch_github_already_deleted_when_deleting_then_it_succeeds(self):
        self.respond("1.0.0", (22, {"message": "Reference does not exist"}))
        result = self.github("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already deleted", result.stdout)

    def test_given_a_branch_github_refuses_to_delete_when_deleting_then_it_fails(self):
        self.respond("1.0.0", (22, {"message": "Resource not accessible by integration"}))
        result = self.github("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not accessible", result.stderr)


if __name__ == "__main__":
    unittest.main()
