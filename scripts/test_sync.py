"""Tests for scripts/sync.py, against real git repositories in a temporary directory.   python3 -m unittest discover -s scripts

The source is a checkout made with `git init`, the target a bare repository with `main` on it, reached through
a file URL: no network and no forge. sync's main() runs in-process, so its exit code, stdout and stderr are
what the Jenkins library would see.
"""

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import sync

REPOSITORY = "heimdall-api"
FORGE_URL = "https://forge.example.test:3000"
AUTHOR = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false"]
PATHS = [
    {"from": "out/report", "to": "static/report"},
    {"from": "docs/req", "to": "content/req"},
]


def git(directory, *args):
    return subprocess.run(
        ["git", *AUTHOR, *args], cwd=directory, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(root, relative, text):
    path = pathlib.Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(
            os.environ,
            {"FORGE_USER": "ygg-bot", "FORGE_URL": FORGE_URL, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FORGE_TOKEN", None)

        self.app = self.tmp / "app"
        self.app.mkdir()
        write(self.app, "out/report/index.html", "<h1>report</h1>\n")
        write(self.app, "out/report/assets/site.css", "body {}\n")
        write(self.app, "docs/req/one.md", "one\n")
        write(self.app, "docs/req/sub/two.md", "two\n")
        write(self.app, "src/ignored.txt", "not synced\n")
        git(self.app, "init", "--quiet", "-b", "main")
        git(self.app, "add", "-A")
        git(self.app, "commit", "--quiet", "-m", "feat: build the report")

        seed = self.tmp / "seed"
        seed.mkdir()
        write(seed, "README.md", "target\n")
        write(seed, "static/old.txt", "old\n")
        write(seed, "content/req/gone.md", "gone\n")
        git(seed, "init", "--quiet", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "docs: seed")
        self.target = self.tmp / "target.git"
        git(self.tmp, "clone", "--quiet", "--bare", str(seed), str(self.target))
        self.url = self.target.as_uri()

        self.spec = self.tmp / "spec.json"
        self.spec.write_text(json.dumps({"repository": "docs", "paths": PATHS}), encoding="utf-8")
        self.out = self.tmp / "out-dir"
        self.message = self.tmp / "message.txt"
        self.message.write_text("docs: sync heimdall-api v4.3.0\n\n- out/report -> static/report\n", encoding="utf-8")

    def run_sync(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.main([str(part) for part in argv])
        return code, out.getvalue(), err.getvalue()

    def gather(self):
        return self.run_sync("gather", self.spec, self.app, self.out)

    def push(self):
        return self.run_sync("push", self.out, self.url, self.message)

    def main_sha(self):
        return git(self.target, "rev-parse", "main")

    def target_file(self, relative, ref="main"):
        return git(self.target, "show", f"{ref}:{relative}")

    def target_files(self, ref="main"):
        return git(self.target, "ls-tree", "-r", "--name-only", ref).splitlines()


class GatherTests(SyncTestCase):
    def test_given_files_when_gathered_then_each_from_is_staged_by_index_with_a_manifest(self):
        code, _, err = self.gather()

        self.assertEqual(code, 0, err)
        self.assertEqual((self.out / "0" / "index.html").read_text(encoding="utf-8"), "<h1>report</h1>\n")
        self.assertEqual((self.out / "0" / "assets" / "site.css").read_text(encoding="utf-8"), "body {}\n")
        self.assertEqual((self.out / "1" / "sub" / "two.md").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(json.loads((self.out / "manifest.json").read_text(encoding="utf-8")), {"paths": PATHS})
        self.assertFalse((self.out / "2").exists())

    def test_given_a_file_from_when_gathered_then_the_file_is_staged(self):
        self.spec.write_text(json.dumps({"paths": [{"from": "docs/req/one.md", "to": "content/one.md"}]}))

        code, _, err = self.gather()

        self.assertEqual(code, 0, err)
        self.assertEqual((self.out / "0").read_text(encoding="utf-8"), "one\n")

    def test_given_a_missing_from_when_gathered_then_it_fails_naming_it(self):
        shutil.rmtree(self.app / "out" / "report")

        code, out, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn("sync:", err)
        self.assertIn("out/report", err)
        self.assertEqual(out, "")
        self.assertFalse((self.out / "manifest.json").exists())
        self.assertFalse((self.out / "1").exists(), "nothing is copied before everything is checked")

    def test_given_an_empty_directory_from_when_gathered_then_it_fails_naming_it(self):
        shutil.rmtree(self.app / "docs" / "req")
        (self.app / "docs" / "req" / "sub").mkdir(parents=True)

        code, _, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn("docs/req", err)
        self.assertFalse((self.out / "manifest.json").exists())

    def test_given_a_link_under_a_from_when_gathered_then_it_is_refused(self):
        self.link(self.app / "docs" / "req" / "sub" / "outside.md", self.tmp / "app" / "src" / "ignored.txt")

        code, _, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertIn("docs/req/sub/outside.md", err)
        self.assertFalse((self.out / "manifest.json").exists())
        self.assertFalse((self.out / "0").exists(), "nothing is staged")

    def test_given_a_from_that_is_a_link_when_gathered_then_it_is_refused(self):
        shutil.rmtree(self.app / "out" / "report")
        self.link(self.app / "out" / "report", self.app / "docs" / "req", directory=True)

        code, _, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertIn("out/report", err)
        self.assertFalse((self.out / "0").exists())

    def test_given_a_link_in_the_parent_of_a_from_when_gathered_then_it_is_refused(self):
        shutil.rmtree(self.app / "out")
        self.link(self.app / "out", self.app / "docs", directory=True)
        write(self.app, "docs/report/index.html", "inside the link\n")

        code, _, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertFalse((self.out / "0").exists())

    def test_given_an_embedded_repository_under_a_from_when_gathered_then_it_is_refused(self):
        embedded = self.app / "docs" / "req" / "embedded"
        embedded.mkdir()
        git(embedded, "init", "--quiet")
        write(embedded, "file.txt", "inside\n")

        code, _, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn(".git", err)
        self.assertIn("docs/req/embedded/.git", err)
        self.assertFalse((self.out / "manifest.json").exists())
        self.assertFalse((self.out / "0").exists(), "nothing is staged")

    def test_given_a_git_file_under_a_from_when_gathered_then_it_is_refused(self):
        write(self.app, "out/report/vendor/.GIT", "gitdir: ../nowhere\n")

        code, _, err = self.gather()

        self.assertEqual(code, 1)
        self.assertIn("git data", err)
        self.assertFalse((self.out / "0").exists())

    def link(self, path, target, directory=False):
        try:
            os.symlink(target, path, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"cannot make a symbolic link here: {error}")


class PushTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        code, _, err = self.gather()
        self.assertEqual(code, 0, err)
        # The message travels in the staged directory (one stash); push must never copy it into the target.
        shutil.copyfile(self.message, self.out / "message.txt")

    def test_given_new_content_when_pushed_then_main_has_one_commit_with_the_message(self):
        before = self.main_sha()

        code, out, err = self.push()

        self.assertEqual(code, 0, err)
        self.assertEqual(git(self.target, "rev-list", "--count", f"{before}..main"), "1")
        self.assertEqual(out.strip(), f"sync: pushed {self.main_sha()}")
        self.assertEqual(git(self.target, "log", "-1", "--format=%s", "main"), "docs: sync heimdall-api v4.3.0")
        self.assertIn("- out/report -> static/report", git(self.target, "log", "-1", "--format=%B", "main"))
        self.assertEqual(self.target_file("static/report/index.html"), "<h1>report</h1>")
        self.assertEqual(self.target_file("static/report/assets/site.css"), "body {}")
        self.assertEqual(self.target_file("content/req/one.md"), "one")
        self.assertEqual(self.target_file("content/req/sub/two.md"), "two")
        self.assertEqual(self.target_file("README.md"), "target")
        self.assertEqual(self.target_file("static/old.txt"), "old")
        self.assertNotIn("content/req/gone.md", self.target_files())
        self.assertNotIn("message.txt", self.target_files())
        self.assertNotIn("manifest.json", self.target_files())

    def test_given_a_push_when_it_commits_then_author_and_committer_are_the_forge_user(self):
        self.push()

        who = "ygg-bot <ygg-bot@noreply.forge.example.test>"
        self.assertEqual(git(self.target, "log", "-1", "--format=%an <%ae>", "main"), who)
        self.assertEqual(git(self.target, "log", "-1", "--format=%cn <%ce>", "main"), who)

    def test_given_no_forge_url_when_pushed_then_the_author_host_is_localhost(self):
        del os.environ["FORGE_URL"]

        self.push()

        self.assertEqual(
            git(self.target, "log", "-1", "--format=%ae", "main"), "ygg-bot@noreply.localhost"
        )

    def test_given_a_file_removed_at_the_source_when_pushed_then_it_is_removed_and_nothing_else_changes(self):
        self.push()
        (self.app / "docs" / "req" / "one.md").unlink()
        shutil.rmtree(self.out)
        self.gather()
        shutil.copyfile(self.message, self.out / "message.txt")

        code, _, err = self.push()

        self.assertEqual(code, 0, err)
        self.assertNotIn("content/req/one.md", self.target_files())
        self.assertIn("content/req/sub/two.md", self.target_files())
        changed = git(self.target, "diff", "--name-only", "main~1", "main").splitlines()
        self.assertEqual(changed, ["content/req/one.md"])
        self.assertEqual(self.target_file("README.md"), "target")

    def test_given_no_change_when_pushed_again_then_nothing_is_committed(self):
        self.push()
        pushed = self.main_sha()

        code, out, err = self.push()

        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_sha(), pushed)
        self.assertIn("nothing to change", out)

    def test_given_main_moved_after_the_clone_when_pushed_then_it_retries_and_keeps_both(self):
        real_push = sync.push_head
        calls = []

        def moved_first(clone, env):
            calls.append(clone)
            if len(calls) == 1:
                other = self.tmp / "other"
                git(self.tmp, "clone", "--quiet", self.url, str(other))
                write(other, "elsewhere.txt", "someone else\n")
                git(other, "add", "-A")
                git(other, "commit", "--quiet", "-m", "docs: someone else")
                git(other, "push", "--quiet", "origin", "HEAD:main")
            return real_push(clone, env)

        with mock.patch.object(sync, "push_head", side_effect=moved_first):
            code, out, err = self.push()

        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1], "the retry starts from a fresh clone")
        subjects = git(self.target, "log", "--format=%s", "main").splitlines()
        self.assertEqual(subjects[:2], ["docs: sync heimdall-api v4.3.0", "docs: someone else"])
        self.assertEqual(self.target_file("elsewhere.txt"), "someone else")
        self.assertEqual(self.target_file("static/report/index.html"), "<h1>report</h1>")

    def test_given_pushes_always_rejected_when_pushed_then_it_fails_after_three_attempts(self):
        before = self.main_sha()

        with mock.patch.object(sync, "push_head", side_effect=sync.Rejected("rejected")) as push_head:
            code, out, err = self.push()

        self.assertEqual(code, 1)
        self.assertEqual(push_head.call_count, 3)
        self.assertIn("3 attempts", err)
        self.assertTrue(err.startswith("sync: "))
        self.assertEqual(self.main_sha(), before)

    def test_given_a_token_when_pushing_then_it_never_reaches_arguments_or_output(self):
        os.environ["FORGE_TOKEN"] = "secret-xyz"
        real_run = subprocess.run
        recorded = []

        def recording(*args, **kwargs):
            recorded.append(args[0] if args else kwargs["args"])
            return real_run(*args, **kwargs)

        with mock.patch("subprocess.run", side_effect=recording):
            code, out, err = self.push()

        self.assertEqual(code, 0, err)
        self.assertTrue(recorded)
        self.assertNotIn("secret-xyz", out + err)
        self.assertNotIn("secret-xyz", "\n".join(" ".join(argv) for argv in recorded))
        # The helper reads the environment, so the secret is not in it; it is installed for every transfer.
        transfers = [argv for argv in recorded if "clone" in argv or "push" in argv]
        self.assertTrue(transfers)
        for argv in transfers:
            self.assertTrue(any(part.startswith("credential.helper=!") for part in argv), argv)
            self.assertTrue(any("$FORGE_TOKEN" in part for part in argv), argv)

    def test_given_no_token_when_pushing_then_no_credential_helper_is_installed(self):
        real_run = subprocess.run
        recorded = []

        def recording(*args, **kwargs):
            recorded.append(args[0])
            return real_run(*args, **kwargs)

        with mock.patch("subprocess.run", side_effect=recording):
            code, _, err = self.push()

        self.assertEqual(code, 0, err)
        self.assertFalse(any("credential.helper=!" in part for argv in recorded for part in argv))

    def test_given_a_manifest_outside_the_clone_when_pushed_then_it_is_refused_and_nothing_changes(self):
        before = self.main_sha()
        for unsafe in ("../escape", "/abs", ".git/hooks", "a/../../b", "static/.GIT/x", "a\\b", "", "."):
            with self.subTest(to=unsafe):
                manifest = {"paths": [{"from": "out/report", "to": unsafe}]}
                (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

                code, _, err = self.push()

                self.assertEqual(code, 1)
                self.assertIn("sync:", err)
                self.assertEqual(self.main_sha(), before)

    def test_given_a_staged_directory_with_a_link_when_pushed_then_it_is_refused(self):
        try:
            os.symlink(self.tmp, self.out / "0" / "escape", target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"cannot make a symbolic link here: {error}")
        before = self.main_sha()

        code, _, err = self.push()

        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertEqual(self.main_sha(), before)

    def test_given_a_staged_git_file_when_pushed_then_it_is_refused(self):
        write(self.out, "1/sub/.git", "gitdir: ../nowhere\n")
        before = self.main_sha()

        code, _, err = self.push()

        self.assertEqual(code, 1)
        self.assertIn("git data", err)
        self.assertIn("1/sub/.git", err)
        self.assertEqual(self.main_sha(), before)

    def test_given_a_missing_staged_entry_when_pushed_then_it_fails_and_nothing_changes(self):
        shutil.rmtree(self.out / "1")
        before = self.main_sha()

        code, _, err = self.push()

        self.assertEqual(code, 1)
        self.assertIn("sync:", err)
        self.assertEqual(self.main_sha(), before)

    def test_given_a_missing_message_when_pushed_then_it_fails(self):
        self.message.unlink()

        code, _, err = self.push()

        self.assertEqual(code, 1)
        self.assertIn("message", err)


class MessageTests(SyncTestCase):
    def message_lines(self):
        code, out, err = self.run_sync("message", REPOSITORY, self.app, self.spec)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def commit(self, subject):
        write(self.app, "src/next.txt", subject)
        git(self.app, "add", "-A")
        git(self.app, "commit", "--quiet", "-m", subject)
        return git(self.app, "rev-parse", "HEAD")

    def test_given_a_release_merge_when_the_message_is_made_then_it_names_the_version(self):
        self.commit("release: v4.3.0 (#12)")

        lines = self.message_lines()

        self.assertEqual(lines[0], "docs: sync heimdall-api v4.3.0")

    def test_given_any_other_subject_when_the_message_is_made_then_it_names_the_short_commit(self):
        for subject in ("feat: add a thing", "release: v4.3 (#1)", "release: v4.3.0x", "fix: release: v1.2.3"):
            with self.subTest(subject=subject):
                sha = self.commit(subject)

                self.assertEqual(self.message_lines()[0], f"docs: sync heimdall-api {sha[:7]}")

    def test_given_a_spec_when_the_message_is_made_then_it_lists_the_paths_and_the_source(self):
        sha = self.commit("release: v4.3.0 (#12)")

        lines = self.message_lines()

        self.assertEqual(
            lines,
            [
                "docs: sync heimdall-api v4.3.0",
                "",
                "- out/report -> static/report",
                "- docs/req -> content/req",
                "",
                f"Source: heimdall-api@{sha}",
            ],
        )

    def test_given_no_repository_in_app_dir_when_the_message_is_made_then_it_fails(self):
        code, _, err = self.run_sync("message", REPOSITORY, self.tmp, self.spec)

        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("sync: "))

    def test_given_no_spec_when_the_message_is_made_then_it_is_a_usage_error(self):
        code, out, err = self.run_sync("message", REPOSITORY, self.app)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("usage", err)


class CommandLineTests(unittest.TestCase):
    def test_given_a_wrong_command_line_when_run_then_it_exits_with_usage_and_no_traceback(self):
        script = pathlib.Path(sync.__file__)
        for argv in ([], ["nope"], ["gather", "only-one"], ["message", "repo", "dir"]):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    ["python3" if shutil.which("python3") else "python", str(script), *argv],
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.startswith("sync: "), result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
