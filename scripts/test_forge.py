"""Tests for scripts/forge.sh, against a fake `curl` on PATH.   python3 -m unittest discover -s scripts

The fake answers each request from the queue of responses given for its path, so these need bash
and jq (which the Jenkins agent image has) but no network. It also keeps the config curl was given on
stdin, where the headers (and so the token) travel.

A response is "<exit code> <body>", or "<exit code> <HTTP status> <body>" for the tests that depend on
the status, which the fake prints after the body the way curl's --write-out does.
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
    # <key> is the path's last segment without its query, and each holds "<exit code> <body>" or
    # "<exit code> <HTTP status> <body>" (the second field is a status when it is exactly three digits;
    # without one the status is 200 after exit code 0 and 400 after any other).
    # What it was asked is kept: the arguments in <key>.request.<n>, the config on stdin in <key>.config.<n>.
    # Like curl given --write-out '\n%{http_code}', it ends its output with a newline and the status.
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
    read -r status rest <"$response"
    http=$((status == 0 ? 200 : 400))
    body=$rest
    if [[ "$rest" =~ ^([0-9]{3})([[:space:]](.*))?$ ]]; then
      http=${BASH_REMATCH[1]}
      body=${BASH_REMATCH[3]}
    fi
    # What curl itself would say on stderr ("curl: (22) ..."), when the test sets it.
    [[ -z "${FAKE_CURL_STDERR:-}" ]] || echo "$FAKE_CURL_STDERR" >&2
    printf '%s' "$body"
    [[ " $* " != *" --write-out "* ]] || printf '\n%s' "$http"
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


needs_bash_and_jq = unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and os.name == "posix",
                                        "needs bash and jq on a POSIX system")


@needs_bash_and_jq
class ForgeTestCase(unittest.TestCase):
    """The fake curl, the environment and the helpers; the tests are in the subclasses."""

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
        """Each response is (exit code, body) or (exit code, body, HTTP status)."""
        for n, response in enumerate(responses, start=1):
            self.write_response(f"{key}.{n}", *response)
        if last is not None:
            self.write_response(f"{key}.last", *last)

    def write_response(self, name, status, body, http=None):
        fields = [str(status)] + ([] if http is None else [str(http)]) + ([] if body is None else [json.dumps(body)])
        (self.state / name).write_text(" ".join(fields) + "\n")

    def forge(self, *arguments, env=None):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=env or self.env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

    def request(self, key, number=1):
        return (self.state / f"{key}.request.{number}").read_text()

    def config(self, key, number=1):
        return (self.state / f"{key}.config.{number}").read_text()

    def body(self, key, number=1):
        """The JSON body curl was given with --data."""
        return json.loads(self.request(key, number).split("--data ", 1)[1].rsplit(" https://", 1)[0])


@needs_bash_and_jq
class ForgeGitHubTests(ForgeTestCase):
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

    def test_given_a_wrong_command_or_arguments_when_run_then_the_usage_is_shown(self):
        result = self.forge("frobnicate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown command 'frobnicate'", result.stderr)
        result = self.forge("merge-pr", "app", "7")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage: forge.sh merge-pr <repo> <number> <sha> <title>", result.stderr)


@needs_bash_and_jq
class ForgeForgejoTests(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.env = dict(self.env, FORGE_TYPE="forgejo")

    def test_given_forgejo_when_setting_a_status_then_it_posts_to_the_commit(self):
        self.respond("abc123", (0, {"id": 1}))
        result = self.forge("set-status", "app", "abc123", "success", "deploy/production", "Deployed to production",
                            "https://jenkins/job/1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        request = self.request("abc123")
        self.assertIn("--request POST", request)
        self.assertTrue(request.strip().endswith("https://api.example/repos/someone/app/statuses/abc123"), request)
        self.assertEqual(self.body("abc123"), {"state": "success", "context": "deploy/production",
                                               "description": "Deployed to production",
                                               "target_url": "https://jenkins/job/1"})

    def test_given_forgejo_and_no_target_when_setting_a_status_then_no_target_url_is_sent(self):
        self.respond("abc123", (0, {"id": 1}))
        result = self.forge("set-status", "app", "abc123", "pending", "ctx", "description")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.body("abc123"), {"state": "pending", "context": "ctx", "description": "description"})

    def test_given_an_unknown_state_when_setting_a_status_on_forgejo_then_it_is_refused_before_any_request(self):
        result = self.forge("set-status", "app", "abc123", "great", "ctx", "description")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown state", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_given_forgejo_when_reading_statuses_then_the_status_field_is_printed_per_context(self):
        self.respond("status", (0, {"state": "failure", "statuses": [
            {"context": "branch-policy", "status": "success"}, {"context": "test", "status": "failure"}]}))
        result = self.forge("get-statuses", "app", "abc123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "branch-policy\tsuccess\ntest\tfailure\n")
        request = self.request("status")
        self.assertTrue(request.strip().endswith(
            "https://api.example/repos/someone/app/commits/abc123/status?limit=50&page=1"), request)
        self.assertFalse((self.state / "status.request.2").exists(), "a page that is not full is the last")

    def test_given_a_commit_without_statuses_when_reading_them_on_forgejo_then_nothing_is_printed(self):
        # What Forgejo 16 answers for a commit nobody posted a status on.
        self.respond("status", (0, {"state": "", "sha": "", "total_count": 0, "statuses": None}))
        result = self.forge("get-statuses", "app", "abc123")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_given_more_contexts_than_a_page_when_reading_statuses_on_forgejo_then_every_page_is_read(self):
        page = [{"context": f"deploy/env-{n:02}", "status": "success"} for n in range(50)]
        self.respond("status", (0, {"statuses": page}), (0, {"statuses": [{"context": "test", "status": "failure"}]}))
        result = self.forge("get-statuses", "app", "abc123")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 51)
        self.assertEqual(lines[-1], "test\tfailure")
        self.assertTrue(self.request("status", 2).strip().endswith("/commits/abc123/status?limit=50&page=2"))
        self.assertFalse((self.state / "status.request.3").exists())

    def test_given_forgejo_when_merging_then_the_merge_commit_comes_from_the_pull_request(self):
        self.respond("merge", (0, None))
        self.respond("7", (0, {"merged": True, "merge_commit_sha": "def456"}))
        result = self.forge("merge-pr", "app", "7", "abc123", "release: v1.0.0 (#7)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "def456\n")
        self.assertIn("--request POST", self.request("merge"))
        self.assertTrue(self.request("merge").strip().endswith("/repos/someone/app/pulls/7/merge"))
        self.assertEqual(self.body("merge"), {"Do": "merge", "MergeTitleField": "release: v1.0.0 (#7)",
                                              "head_commit_id": "abc123"})
        self.assertIn("--request GET", self.request("7"))
        self.assertTrue(self.request("7").strip().endswith("/repos/someone/app/pulls/7"))

    def test_given_forgejo_when_the_merge_is_refused_then_its_message_is_shown(self):
        self.respond("merge", (22, {"message": "Pull request is not mergeable"}, 405))
        result = self.forge("merge-pr", "app", "7", "abc123", "title")
        self.assertEqual(result.returncode, 1)
        self.assertIn("forge: merge-pr #7 failed: Pull request is not mergeable", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "7.count").exists(), "the pull request is not read after a refused merge")

    def test_given_forgejo_when_the_pull_request_has_no_merge_commit_then_merging_fails(self):
        self.respond("merge", (0, None))
        self.respond("7", (0, {"merged": True, "merge_commit_sha": None}))
        result = self.forge("merge-pr", "app", "7", "abc123", "title")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no merge commit", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_given_forgejo_still_checking_when_merging_then_it_tries_again(self):
        # Forgejo refuses a merge while it works out again whether the pull request merges, which a new
        # commit status sets off (seen on the real server right after the deploy statuses).
        self.fake("sleep", '#!/usr/bin/env bash\necho "$*" >>"$FAKE_CURL_DIR/sleeps"\n')
        self.respond("merge", (22, {"message": "Please try again later"}, 405),
                     (22, {"message": "Please try again later"}, 405), (0, None))
        self.respond("7", (0, {"merged": True, "merge_commit_sha": "def456"}))
        result = self.forge("merge-pr", "app", "7", "abc123", "title")
        self.assertEqual((result.returncode, result.stdout), (0, "def456\n"), result.stderr)
        self.assertEqual((self.state / "merge.count").read_text().strip(), "3")
        self.assertEqual((self.state / "sleeps").read_text(), "3\n3\n")

    def test_given_forgejo_checking_for_too_long_when_merging_then_it_gives_up(self):
        self.fake("sleep", "#!/usr/bin/env bash\ntrue\n")
        self.respond("merge", last=(22, {"message": "Please try again later"}, 405))
        result = self.forge("merge-pr", "app", "7", "abc123", "title")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("merge-pr #7 failed: Please try again later", result.stderr)
        self.assertEqual((self.state / "merge.count").read_text().strip(), "20")

    def test_given_forgejo_when_creating_a_tag_then_it_posts_the_name_and_target(self):
        self.respond("tags", (0, {"name": "v1.2.3"}))
        result = self.forge("create-tag", "app", "v1.2.3", "abc123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(self.request("tags").strip().endswith("/repos/someone/app/tags"))
        self.assertEqual(self.body("tags"), {"tag_name": "v1.2.3", "target": "abc123"})

    def test_given_forgejo_when_creating_a_release_without_notes_then_the_body_is_empty(self):
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "17\thttps://example/release/17\n")
        self.assertTrue(self.request("releases").strip().endswith("/repos/someone/app/releases"))
        self.assertEqual(self.body("releases"), {"tag_name": "v1.2.3", "target_commitish": "abc123", "name": "v1.2.3",
                                                 "body": "", "draft": False, "prerelease": False})

    def test_given_a_notes_file_when_creating_a_forgejo_release_then_its_text_is_the_body(self):
        notes = self.temp / "notes.md"
        notes.write_text("Fixes the thing.\n")
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3", "--notes-file", str(notes))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.body("releases")["body"], "Fixes the thing.\n")

    def test_given_forgejo_when_creating_a_draft_release_then_it_prints_its_id_and_url(self):
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3", "--draft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "17\thttps://example/release/17\n")
        self.assertTrue(self.body("releases")["draft"])

    def test_given_the_draft_comes_back_public_when_creating_a_forgejo_draft_release_then_it_fails(self):
        self.respond("releases", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("create-release", "app", "v1.2.3", "abc123", "v1.2.3", "--draft")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unpublished draft", result.stderr)

    def test_given_forgejo_when_uploading_then_the_file_is_sent_as_the_attachment_field(self):
        artifact = self.temp / "my app.zip"
        artifact.write_bytes(b"zip bytes")
        self.respond("assets", (0, {"browser_download_url": "https://example/download/my%20app.zip"}))
        result = self.forge("upload-release-file", "app", "17", str(artifact))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "https://example/download/my%20app.zip\n")
        request = self.request("assets")
        self.assertTrue(request.strip().endswith(
            "https://api.example/repos/someone/app/releases/17/assets?name=my%20app.zip"), request)
        self.assertIn("--request POST", request)
        self.assertIn(f"--form attachment=@{artifact}", request)
        self.assertNotIn("Content-Type", request, "curl must make the multipart Content-Type with its boundary")

    def test_given_a_missing_file_when_uploading_to_forgejo_then_it_fails_before_any_request(self):
        result = self.forge("upload-release-file", "app", "17", str(self.temp / "nothing.zip"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no file", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_given_forgejo_when_publishing_then_the_draft_is_turned_off(self):
        self.respond("17", (0, {"id": 17, "draft": False, "html_url": "https://example/release/17"}))
        result = self.forge("publish-release", "app", "17")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "https://example/release/17\n")
        request = self.request("17")
        self.assertIn("--request PATCH", request)
        self.assertTrue(request.strip().endswith("/repos/someone/app/releases/17"), request)
        self.assertEqual(self.body("17"), {"draft": False})

    def test_given_the_release_is_still_a_draft_when_publishing_on_forgejo_then_it_fails(self):
        self.respond("17", (0, {"id": 17, "draft": True, "html_url": "https://example/release/17"}))
        result = self.forge("publish-release", "app", "17")
        self.assertEqual(result.returncode, 1)
        self.assertIn("still a draft", result.stderr)

    def test_given_a_branch_with_a_slash_when_deleting_on_forgejo_then_the_path_keeps_it(self):
        self.respond("1.0.0", (0, None, 204))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        request = self.request("1.0.0")
        self.assertIn("--request DELETE", request)
        self.assertTrue(request.strip().endswith("/repos/someone/app/branches/release/1.0.0"), request)

    def test_given_odd_characters_in_a_branch_when_deleting_on_forgejo_then_each_segment_is_encoded(self):
        self.respond("b", (0, None, 204))
        result = self.forge("delete-branch", "app", "feature/a b#1/b")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.request("b").strip().endswith("/branches/feature/a%20b%231/b"))

    def test_given_a_missing_branch_when_deleting_on_forgejo_then_it_succeeds(self):
        # Forgejo 16 answers a 500 with an empty message (only administrators see "object does not
        # exist"): the branch is looked up, and is gone from a repository that is there.
        self.respond("1.0.0", (22, {"message": "", "url": "https://example/api/swagger"}, 500),
                     (22, {"message": "The target couldn't be found.", "errors": []}, 404))
        self.respond("app", (0, {"name": "app"}))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "forge: release/1.0.0 was already deleted\n")
        self.assertIn("--request GET", self.request("1.0.0", 2))
        self.assertTrue(self.request("1.0.0", 2).strip().endswith("/repos/someone/app/branches/release/1.0.0"))
        self.assertIn("--request GET", self.request("app"))
        self.assertTrue(self.request("app").strip().endswith("/repos/someone/app"))

    def test_given_a_missing_repository_when_deleting_on_forgejo_then_it_fails(self):
        # A repository that is not there, or that the token cannot see, is not "already deleted".
        missing = {"message": "The target couldn't be found.", "errors": []}
        self.respond("1.0.0", (22, missing, 404), (22, missing, 404))
        self.respond("app", (22, missing, 404))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("forge: delete-branch release/1.0.0 failed: The target couldn't be found.", result.stderr)
        self.assertNotIn("already deleted", result.stderr)

    def test_given_a_server_error_on_a_branch_that_is_there_when_deleting_on_forgejo_then_it_fails(self):
        self.respond("1.0.0", (22, {"message": ""}, 500), (0, {"name": "release/1.0.0"}))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("delete-branch release/1.0.0 failed: HTTP 500, without a message", result.stderr)

    def test_given_a_protected_branch_when_deleting_on_forgejo_then_it_exits_3_and_says_it_is_kept(self):
        self.respond("develop", (22, {"message": "branch protected"}, 403))
        result = self.forge("delete-branch", "app", "develop")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("kept: develop is protected", result.stderr)
        self.assertFalse((self.state / "develop.request.2").exists(), "a protected branch is not looked up")

    def test_given_forbidden_for_another_reason_when_deleting_on_forgejo_then_it_fails(self):
        self.respond("1.0.0", (22, {"message": "token does not have write access"}, 403), (0, {"name": "release/1.0.0"}))
        result = self.forge("delete-branch", "app", "release/1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("delete-branch release/1.0.0 failed: token does not have write access", result.stderr)

    def test_given_an_http_error_when_any_command_fails_then_curls_own_line_stays_out_of_the_log(self):
        self.respond("abc123", (22, {"message": "sha not found"}, 404))
        env = dict(self.env, FAKE_CURL_STDERR="curl: (22) The requested URL returned error: 404")
        result = self.forge("set-status", "app", "abc123", "success", "ctx", "description", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "forge: set-status on abc123 failed: sha not found\n")

    def test_given_no_answer_when_any_command_fails_then_curls_error_is_the_message(self):
        self.respond("abc123", (7, None, "000"))
        env = dict(self.env, FAKE_CURL_STDERR="curl: (7) Failed to connect to api.example port 443")
        result = self.forge("set-status", "app", "abc123", "success", "ctx", "description", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("set-status on abc123 failed: curl: (7) Failed to connect to api.example port 443", result.stderr)

    def test_given_forgejo_when_any_command_runs_then_the_token_is_not_in_curls_arguments(self):
        artifact = self.temp / "app.zip"
        artifact.write_bytes(b"zip bytes")
        self.respond("abc123", (0, {}))
        self.respond("status", (0, {"statuses": []}))
        self.respond("merge", (0, None))
        self.respond("7", (0, {"merge_commit_sha": "def456"}))
        self.respond("tags", (0, {}))
        self.respond("releases", (0, {"id": 17, "draft": True, "html_url": "u"}))
        self.respond("assets", (0, {"browser_download_url": "u"}))
        self.respond("17", (0, {"draft": False, "html_url": "u"}))
        self.respond("1.0.0", (0, None, 204))
        for arguments in (("set-status", "app", "abc123", "success", "ctx", "description"),
                          ("get-statuses", "app", "abc123"),
                          ("merge-pr", "app", "7", "abc123", "title"),
                          ("create-tag", "app", "v1", "abc123"),
                          ("create-release", "app", "v1", "abc123", "v1", "--draft"),
                          ("upload-release-file", "app", "17", str(artifact)),
                          ("publish-release", "app", "17"),
                          ("delete-branch", "app", "release/1.0.0")):
            self.assertEqual(self.forge(*arguments).returncode, 0, arguments)
        requests = sorted(self.state.glob("*.request.*"))
        configs = sorted(self.state.glob("*.config.*"))
        self.assertEqual(len(requests), 9)
        for path in requests:
            self.assertNotIn(TOKEN, path.read_text(), path.name)
        for path in configs:
            self.assertIn(f'header = "Authorization: token {TOKEN}"', path.read_text(), path.name)
            self.assertNotIn("Bearer", path.read_text(), path.name)


if __name__ == "__main__":
    unittest.main()
