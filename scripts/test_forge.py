"""Tests for scripts/forge.sh, against a fake `curl` on PATH.   python3 -m unittest discover -s scripts

The fake answers each request from the queue of responses given for its path, so these need bash
and jq (which the Jenkins agent image has) but no network. It also keeps the config curl was given on
stdin, where the headers (and so the token) travel.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "forge.sh"

FAKE_CURL = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # A stand-in for curl: the response to <path> is the next of $FAKE_CURL_DIR/<key>.<n>, where
    # <key> is the path's last segment without its query, and each holds "<exit code> <body>".
    # What it was asked is kept: the arguments in <key>.request.<n>, the config on stdin in <key>.config.<n>.
    url=${*: -1}
    key=${url%%\?*}
    key=${key##*/}
    count_file="$FAKE_CURL_DIR/$key.count"
    n=$(($(cat "$count_file" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$count_file"
    printf '%s\n' "$*" >"$FAKE_CURL_DIR/$key.request.$n"
    cat >"$FAKE_CURL_DIR/$key.config.$n"
    response="$FAKE_CURL_DIR/$key.$n"
    [[ -f "$response" ]] || response="$FAKE_CURL_DIR/$key.last"
    read -r status body <"$response"
    printf '%s' "$body"
    exit "$status"
""").lstrip()

# What catalog.py would print, for the test that leaves the environment empty.
FAKE_PYTHON = textwrap.dedent(r"""
    #!/usr/bin/env bash
    [[ "$1" == */scripts/catalog.py ]] || { echo "unexpected: $*" >&2; exit 2; }
    shift
    case "$*" in
      "forge type") echo github ;;
      "forge api") echo https://catalog.example/api ;;
      "owner") echo catalogowner ;;
      *) echo "unexpected: $*" >&2; exit 2 ;;
    esac
""").lstrip()

TOKEN = "token-secret"


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and os.name == "posix",
                     "needs bash and jq on a POSIX system")
class ForgeGitHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="forge-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.fake("curl", FAKE_CURL)
        self.state = self.temp / "state"
        self.state.mkdir()
        # Nothing of the machine's own environment may answer for the test.
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("FORGE_", "GITHUB_")) and k != "GH_TOKEN"}
        self.base = dict(base, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_CURL_DIR=str(self.state))
        self.env = dict(self.base, FORGE_TYPE="github", FORGE_API_URL="https://api.example", FORGE_OWNER="someone",
                        FORGE_TOKEN=TOKEN)

    def fake(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def respond(self, key, *responses, last=None):
        for n, (status, body) in enumerate(responses, start=1):
            (self.state / f"{key}.{n}").write_text(f"{status} {json.dumps(body) if body is not None else ''}\n")
        if last is not None:
            (self.state / f"{key}.last").write_text(f"{last[0]} {json.dumps(last[1])}\n")

    def forge(self, *arguments, env=None):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=env or self.env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

    def request(self, key, number=1):
        return (self.state / f"{key}.request.{number}").read_text()

    def config(self, key, number=1):
        return (self.state / f"{key}.config.{number}").read_text()

    def test_given_github_when_setting_a_status_then_it_posts_to_the_commit(self):
        self.respond("abc123", (0, {"id": 1}))
        result = self.forge("set-status", "app", "abc123", "success", "deploy/production", "Deployed to production",
                            "https://jenkins/job/1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        request = self.request("abc123")
        self.assertIn("--request POST", request)
        self.assertTrue(request.strip().endswith("https://api.example/repos/someone/app/statuses/abc123"), request)
        body = json.loads(request.split("--data ", 1)[1].rsplit(" https://", 1)[0])
        self.assertEqual(body, {"state": "success", "context": "deploy/production",
                                "description": "Deployed to production", "target_url": "https://jenkins/job/1"})

    def test_given_an_unknown_state_when_setting_a_status_then_it_is_refused_before_any_request(self):
        result = self.forge("set-status", "app", "abc123", "great", "ctx", "description")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown state", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_given_github_when_reading_statuses_then_the_latest_per_context_is_printed(self):
        self.respond("status", (0, {"state": "failure", "statuses": [
            {"context": "branch-policy", "state": "success"}, {"context": "test", "state": "failure"}]}))
        result = self.forge("get-statuses", "app", "abc123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "branch-policy\tsuccess\ntest\tfailure\n")
        self.assertIn("https://api.example/repos/someone/app/commits/abc123/status", self.request("status"))

    def test_given_github_when_merging_then_it_prints_the_merge_commit(self):
        self.respond("merge", (0, {"sha": "def456", "merged": True}))
        result = self.forge("merge-pr", "app", "7", "abc123", "release: v1.0.0 (#7)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "def456\n")
        request = self.request("merge")
        self.assertIn("--request PUT", request)
        self.assertIn("/repos/someone/app/pulls/7/merge", request)
        body = json.loads(request.split("--data ", 1)[1].rsplit(" https://", 1)[0])
        self.assertEqual(body, {"merge_method": "merge", "sha": "abc123", "commit_title": "release: v1.0.0 (#7)"})

    def test_given_github_when_creating_a_tag_then_it_posts_the_ref(self):
        self.respond("refs", (0, {"ref": "refs/tags/v1.2.3"}))
        result = self.forge("create-tag", "app", "v1.2.3", "abc123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        request = "".join(self.request("refs").split())
        self.assertIn('"ref":"refs/tags/v1.2.3"', request)
        self.assertIn('"sha":"abc123"', request)

    def test_given_github_when_creating_a_release_without_notes_then_github_writes_them(self):
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "17\thttps://example/release/17\n")
        request = "".join(self.request("releases").split())
        self.assertIn('"generate_release_notes":true', request)
        self.assertIn('"draft":false', request)
        self.assertIn('"tag_name":"v1.2.3"', request)
        self.assertIn('"target_commitish":"abc123"', request)
        self.assertIn('"name":"v1.2.3"', request)

    def test_given_a_notes_file_when_creating_a_release_then_its_text_is_the_body(self):
        notes = self.temp / "notes.md"
        notes.write_text("Fixes the thing.\n")
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3", "--notes-file", str(notes))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(self.request("releases").split("--data ", 1)[1].rsplit(" https://", 1)[0])
        self.assertEqual(body["body"], "Fixes the thing.\n")
        self.assertNotIn("generate_release_notes", body)

    def test_given_github_when_creating_a_draft_release_then_it_prints_its_id_and_url(self):
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3", "--draft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "17\thttps://example/release/17\n")
        self.assertIn('"draft":true', "".join(self.request("releases").split()))

    def test_given_the_draft_comes_back_public_when_creating_a_draft_release_then_it_fails(self):
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3", "--draft")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unpublished draft", result.stderr)

    def test_given_github_when_uploading_a_file_then_it_goes_to_the_uploads_host_with_its_name(self):
        artifact = self.temp / "my app.zip"
        artifact.write_bytes(b"zip bytes")
        self.respond("assets", (0, {"browser_download_url": "https://example/download/my%20app.zip"}))
        result = self.forge("upload-release-file", "app", "17", str(artifact))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "https://example/download/my%20app.zip\n")
        request = self.request("assets")
        self.assertTrue(request.strip().endswith(
            "https://uploads.github.com/repos/someone/app/releases/17/assets?name=my%20app.zip"), request)
        self.assertIn("--request POST", request)
        self.assertIn("Content-Type: application/octet-stream", request)
        self.assertIn(f"--data-binary @{artifact}", request)

    def test_given_a_missing_file_when_uploading_then_it_fails_before_any_request(self):
        result = self.forge("upload-release-file", "app", "17", str(self.temp / "nothing.zip"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no file", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_given_github_when_publishing_then_the_draft_is_turned_off(self):
        self.respond("17", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("publish-release", "app", "17")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "https://example/release/17\n")
        request = self.request("17")
        self.assertIn("--request PATCH", request)
        self.assertIn("/repos/someone/app/releases/17", request)
        self.assertIn('"draft": false', request)

    def test_given_a_branch_with_a_slash_when_deleting_on_github_then_the_ref_path_keeps_it(self):
        self.respond("1.0.0", (0, None))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.request("1.0.0")
        self.assertIn("--request DELETE", request)
        self.assertTrue(request.strip().endswith("/repos/someone/app/git/refs/heads/release/1.0.0"), request)

    def test_given_odd_characters_in_a_branch_when_deleting_then_each_segment_is_encoded(self):
        self.respond("b", (0, None))
        result = self.forge("delete-branch", "app", "feature/a b#1/b")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.request("b").strip().endswith("/git/refs/heads/feature/a%20b%231/b"))

    def test_given_a_branch_already_gone_when_deleting_then_it_succeeds_with_a_note_on_stderr(self):
        self.respond("1.0.0", (22, {"message": "Reference does not exist"}))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("already deleted", result.stderr)

    def test_given_a_branch_github_refuses_to_delete_when_deleting_then_it_fails(self):
        self.respond("1.0.0", (22, {"message": "Resource not accessible by integration"}))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not accessible", result.stderr)
        self.assertNotIn("already deleted", result.stderr)

    def test_given_a_repository_with_a_dot_when_calling_github_then_the_path_is_right(self):
        self.respond("abc123", (0, {}))
        result = self.forge("set-status", "my.app-2_x", "abc123", "pending", "ctx", "description")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.request("abc123").strip().endswith("/repos/someone/my.app-2_x/statuses/abc123"))

    def test_given_any_command_when_run_then_the_token_is_not_in_curls_arguments(self):
        self.respond("abc123", (0, {}))
        self.respond("status", (0, {"statuses": []}))
        self.respond("merge", (0, {"sha": "def456"}))
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "u"}))
        self.respond("17", (0, {"draft": False, "html_url": "u"}))
        self.respond("1.0.0", (0, None))
        for arguments in (("set-status", "app", "abc123", "success", "ctx", "description"),
                          ("get-statuses", "app", "abc123"),
                          ("merge-pr", "app", "7", "abc123", "title"),
                          ("create-release", "app", "v1", "abc123", "v1", "--draft"),
                          ("publish-release", "app", "17"),
                          ("delete-branch", "app", "release/1.0.0")):
            self.assertEqual(self.forge(*arguments).returncode, 0, arguments)
        requests = sorted(self.state.glob("*.request.*"))
        configs = sorted(self.state.glob("*.config.*"))
        self.assertEqual(len(requests), 6)
        for path in requests:
            self.assertNotIn(TOKEN, path.read_text(), path.name)
        for path in configs:
            self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', path.read_text(), path.name)
            self.assertIn('header = "Accept: application/vnd.github+json"', path.read_text(), path.name)

    def test_given_github_refuses_when_merging_then_the_reason_is_shown_and_it_fails(self):
        self.respond("merge", (22, {"message": "Required status check \"deploy/production\" is expected."}))
        result = self.forge("merge-pr", "app", "7", "abc123", "release: v1.0.0 (#7)")
        self.assertEqual(result.returncode, 1)
        self.assertIn("forge: merge-pr #7 failed: Required status check \"deploy/production\"", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("null", result.stderr)

    def test_given_an_answer_that_is_not_json_when_a_request_fails_then_it_is_shown_as_it_is(self):
        (self.state / "merge.1").write_text("22 <html>Bad Gateway</html>\n")
        result = self.forge("merge-pr", "app", "7", "abc123", "title")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed: <html>Bad Gateway</html>", result.stderr)

    def test_given_no_token_when_run_then_it_is_refused_before_any_request(self):
        env = {k: v for k, v in self.env.items() if k != "FORGE_TOKEN"}
        result = self.forge("set-status", "app", "abc123", "success", "ctx", "description", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORGE_TOKEN is not set", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_given_a_token_that_could_break_the_config_when_run_then_it_is_refused(self):
        env = dict(self.env, FORGE_TOKEN='abc"\nurl = "https://evil.example')
        result = self.forge("set-status", "app", "abc123", "success", "ctx", "description", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("characters a token does not have", result.stderr)
        self.assertNotIn("evil", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_given_the_github_variables_when_run_then_they_stand_in_for_the_forge_ones(self):
        env = dict(self.base, FORGE_TYPE="github", GH_TOKEN=TOKEN, GITHUB_OWNER="octo",
                   GITHUB_API_URL="https://ghe.example/api/v3/")
        self.respond("abc123", (0, {}))
        result = self.forge("set-status", "app", "abc123", "success", "ctx", "description", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.request("abc123").strip().endswith("https://ghe.example/api/v3/repos/octo/app/statuses/abc123"))
        self.assertIn(f"Bearer {TOKEN}", self.config("abc123"))

    def test_given_no_forge_type_in_the_environment_when_run_then_the_catalogs_is_used(self):
        self.fake("python3", FAKE_PYTHON)
        env = dict(self.base, FORGE_TOKEN=TOKEN)
        self.respond("abc123", (0, {}))
        result = self.forge("set-status", "app", "abc123", "success", "ctx", "description", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.request("abc123").strip().endswith(
            "https://catalog.example/api/repos/catalogowner/app/statuses/abc123"))

    def test_given_forgejo_without_an_implementation_when_run_then_it_says_so(self):
        env = dict(self.env, FORGE_TYPE="forgejo")
        result = self.forge("get-statuses", "app", "abc123", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("forgejo: get-statuses is not implemented yet", result.stderr)

    def test_given_a_wrong_command_or_arguments_when_run_then_the_usage_is_shown(self):
        result = self.forge("frobnicate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown command 'frobnicate'", result.stderr)
        result = self.forge("merge-pr", "app", "7")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage: forge.sh merge-pr <repo> <number> <sha> <title>", result.stderr)


if __name__ == "__main__":
    unittest.main()
