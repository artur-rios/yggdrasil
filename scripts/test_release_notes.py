"""Tests for scripts/release_notes.py, against scratch git repositories.   python3 -m unittest discover -s scripts

A repository here is a real one made with git in a temporary directory: the merge commits the notes list are
the ones git makes, with the subjects the git servers give them (Forgejo's, GitHub's), not strings handed to a fake.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "release_notes.py"

needs_git = unittest.skipUnless(shutil.which("git"), "needs git")


@needs_git
class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp(prefix="release-notes-test-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        # The machine's own git configuration (signing, hooks, default branch) must not reach the test.
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.counter = 0
        self.git("init", "--quiet", "--initial-branch=develop")
        self.commit("first")

    def git(self, *arguments):
        done = subprocess.run(["git", "-C", str(self.repo), *arguments], env=self.env, capture_output=True,
                              text=True, check=False)
        self.assertEqual(done.returncode, 0, f"git {' '.join(arguments)}: {done.stderr}")
        return done.stdout.strip()

    def commit(self, message):
        self.counter += 1
        (self.repo / f"file{self.counter}.txt").write_text(message)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)

    def merge(self, branch, subject, body=None, into="develop"):
        """A branch with one commit of its own, merged into `into` with a merge commit (never fast-forward)
        carrying `subject` and, when given, `body`: what a git server's merge button makes."""
        self.git("checkout", "--quiet", into)
        self.git("checkout", "--quiet", "-b", branch)
        self.commit(f"work on {branch}")
        self.git("checkout", "--quiet", into)
        message = ["-m", subject] + (["-m", body] if body else [])
        self.git("merge", "--quiet", "--no-ff", *message, branch)

    def notes(self, version="1.4.0", previous=None):
        arguments = [sys.executable, str(SCRIPT), str(self.repo), version] + ([previous] if previous else [])
        return subprocess.run(arguments, capture_output=True, text=True)

    def changelog(self, text):
        (self.repo / "CHANGELOG.md").write_text(text)

    def test_given_a_changelog_section_when_noted_then_it_is_the_section_without_its_heading(self):
        self.changelog("# Changelog\n\n## [Unreleased]\n\n- later\n\n## [1.4.0] - 2026-10-23\n\n### Added\n\n"
                       "- the thing\n- another thing\n\n## [1.3.0] - 2026-09-01\n\n- old\n")
        self.merge("feature/x", "Merge pull request 'x' (#1) from feature/x into develop")
        result = self.notes("1.4.0")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "### Added\n\n- the thing\n- another thing\n")

    def test_given_a_version_with_a_leading_v_when_noted_then_the_section_is_found(self):
        self.changelog("## [1.4.0]\n\n- the thing\n")
        self.assertEqual(self.notes("v1.4.0").stdout, "- the thing\n")

    def test_given_the_last_section_when_noted_then_it_runs_to_the_end_of_the_file(self):
        self.changelog("## [1.4.0]\n\n- the thing\n- the end")
        self.assertEqual(self.notes("1.4.0").stdout, "- the thing\n- the end\n")

    def test_given_a_longer_version_with_the_same_start_when_noted_then_only_the_exact_one_matches(self):
        self.changelog("## [1.4.01]\n\n- wrong\n\n## [1.4.0]\n\n- right\n")
        self.assertEqual(self.notes("1.4.0").stdout, "- right\n")

    def test_given_no_section_when_noted_then_merged_pull_requests_since_the_last_tag_are_listed(self):
        self.changelog("## [1.3.0]\n\n- old\n")
        self.merge("feature/old", "Merge pull request 'Old work' (#1) from feature/old into develop")
        self.git("tag", "v1.3.0")
        self.merge("feature/a", "Merge pull request 'Add the thing' (#2) from feature/a into develop")
        self.merge("fix/b", "Merge pull request #3 from someone/fix/b", "Fix the other thing\n\nLonger text.")
        self.merge("feature/c", "Merge branch 'feature/c'")
        result = self.notes("1.4.0", "v1.3.0")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        # The newest first; the Forgejo title, the GitHub body's first line, the plain subject; nothing of v1.3.0.
        self.assertEqual(result.stdout, "- Merge branch 'feature/c'\n- Fix the other thing\n- Add the thing\n")

    def test_given_release_branch_merges_when_noted_then_they_are_skipped(self):
        self.merge("feature/a", "Merge pull request 'Add the thing' (#2) from feature/a into develop")
        self.merge("release/1.4.0", "Merge pull request 'Release 1.4.0' (#3) from release/1.4.0 into develop")
        self.merge("release/1.4.1", "Merge pull request #4 from someone/release/1.4.1", "Release 1.4.1")
        self.merge("release/1.4.2", "Merge branch 'release/1.4.2'")
        self.merge("release/1.4.3", "Merge remote-tracking branch 'origin/release/1.4.3'")
        self.merge("release/1.4.4", "release: v1.4.4 (#9)")
        result = self.notes("1.4.0")
        self.assertEqual(result.stdout, "- Add the thing\n")

    def test_given_a_title_that_mentions_release_when_noted_then_it_is_kept(self):
        self.merge("feature/a", "Merge pull request 'Prepare for release/2 handling' (#2) from feature/a into develop")
        self.assertEqual(self.notes("1.4.0").stdout, "- Prepare for release/2 handling\n")

    def test_given_merges_inside_a_release_merged_into_main_when_noted_then_they_are_found_beyond_the_first_parent(self):
        # main's first-parent history is only the release merges; the pull requests are on the other parent.
        self.git("branch", "main")
        self.git("tag", "v1.3.0")
        self.merge("feature/a", "Merge pull request 'Add the thing' (#2) from feature/a into develop")
        self.merge("fix/b", "Merge pull request 'Fix the other thing' (#3) from fix/b into develop")
        self.git("branch", "release/1.4.0")
        self.git("checkout", "--quiet", "main")
        self.git("merge", "--quiet", "--no-ff", "-m", "release: v1.4.0 (#4)", "release/1.4.0")
        result = self.notes("1.4.0", "v1.3.0")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "- Fix the other thing\n- Add the thing\n")

    def test_given_no_previous_tag_when_noted_then_every_merge_reachable_from_head_is_listed(self):
        self.merge("feature/a", "Merge pull request 'Add the thing' (#2) from feature/a into develop")
        self.git("tag", "v1.3.0")
        self.merge("feature/b", "Merge pull request 'Add more' (#3) from feature/b into develop")
        self.assertEqual(self.notes("1.4.0").stdout, "- Add more\n- Add the thing\n")

    def test_given_an_empty_section_when_noted_then_the_merged_pull_requests_are_listed(self):
        self.changelog("## [1.4.0]\n\n## [1.3.0]\n\n- old\n")
        self.merge("feature/a", "Merge pull request 'Add the thing' (#2) from feature/a into develop")
        self.assertEqual(self.notes("1.4.0").stdout, "- Add the thing\n")

    def test_given_a_github_merge_without_a_body_when_noted_then_the_subject_is_used(self):
        self.merge("feature/a", "Merge pull request #2 from someone/feature/a")
        self.assertEqual(self.notes("1.4.0").stdout, "- Merge pull request #2 from someone/feature/a\n")

    def test_given_nothing_merged_when_noted_then_no_changes_are_recorded(self):
        self.git("tag", "v1.3.0")
        self.commit("a plain commit")
        result = self.notes("1.4.0", "v1.3.0")
        self.assertEqual((result.returncode, result.stdout), (0, "No changes recorded.\n"))

    def test_given_only_release_merges_when_noted_then_no_changes_are_recorded(self):
        self.merge("release/1.4.0", "Merge pull request 'Release 1.4.0' (#3) from release/1.4.0 into develop")
        self.assertEqual(self.notes("1.4.0").stdout, "No changes recorded.\n")

    def test_given_an_unknown_previous_tag_when_noted_then_it_fails_naming_git(self):
        result = self.notes("1.4.0", "v0.0.1")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("release_notes: git log failed", result.stderr)

    def test_given_a_directory_that_is_no_repository_when_noted_then_it_fails(self):
        plain = pathlib.Path(tempfile.mkdtemp(prefix="release-notes-plain-"))
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        result = subprocess.run([sys.executable, str(SCRIPT), str(plain), "1.4.0"], capture_output=True, text=True,
                                env=dict(self.env, GIT_CEILING_DIRECTORIES=str(plain.parent)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("release_notes:", result.stderr)

    def test_given_a_wrong_command_line_when_run_then_the_usage_is_shown_and_it_exits_2(self):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo)], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("usage: release_notes.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
