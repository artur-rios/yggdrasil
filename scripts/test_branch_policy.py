"""Tests for scripts/branch-policy.sh, against scratch git repositories.   python3 -m unittest discover -s scripts

The script runs in a checkout where the pipeline has fetched origin/develop, origin/main and the tags.
These tests stand in for that: each builds a repository with `git init` and writes the refs/remotes/origin/*
refs and the tags directly, so they need bash and git but no network and no real remote.

The history every test starts from is the one a released project has: main carries a release merge commit
that develop does not have (the thing a branch cut from main would drag along), and develop has moved on
since.

    C0 --- D1 ------------- D2      origin/develop
            \\
             +------- M             origin/main (M merges D1 into main, as a release does)
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "branch-policy.sh"


@unittest.skipUnless(shutil.which("bash") and shutil.which("git") and os.name == "posix",
                     "needs bash and git on a POSIX system")
class BranchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.repository = pathlib.Path(tempfile.mkdtemp(prefix="branch-policy-test-"))
        self.addCleanup(shutil.rmtree, self.repository, ignore_errors=True)
        # Commits need an identity, and the host's git config (signing, templates) must not leak in.
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        self.git("init", "--quiet", "--initial-branch=develop")
        self.commit("C0")
        # main starts at C0; D1 lands on develop; the release then merges D1 into main with a merge
        # commit, which is the one commit main has that develop does not.
        self.git("branch", "main")
        self.commit("D1")
        self.git("checkout", "--quiet", "main")
        self.git("merge", "--quiet", "--no-ff", "-m", "M", "develop")
        self.git("checkout", "--quiet", "develop")
        self.commit("D2")
        self.fetch()

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.repository, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name):
        (self.repository / name).write_text(name)
        self.git("add", name)
        self.git("commit", "--quiet", "-m", name)

    def fetch(self):
        """What the pipeline's `git fetch origin develop main` leaves behind."""
        for branch in ("develop", "main"):
            self.git("update-ref", f"refs/remotes/origin/{branch}", branch)

    def branch(self, name, start, *commits):
        """Cut <name> from <start> with the given commits on it; returns its head sha."""
        self.git("checkout", "--quiet", "-b", name, start)
        for commit in commits:
            self.commit(commit)
        sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "--quiet", "develop")
        return sha

    def check(self, *arguments):
        return subprocess.run(["bash", str(SCRIPT), *arguments], cwd=self.repository, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def assert_passes(self, result, head, base):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"'{head}' -> '{base}' follows the branching model.")

    def assert_refused(self, result, *reason):
        self.assertEqual(result.returncode, 1, result.stdout)
        # The reason goes to stderr, so Jenkins shows it as the failure and a caller's stdout stays clean.
        self.assertEqual(result.stdout, "")
        for part in reason:
            self.assertIn(part, result.stderr)

    def test_given_a_feature_branch_from_develop_when_checked_into_develop_then_it_passes(self):
        sha = self.branch("feature/new-thing", "develop", "F1")
        self.assert_passes(self.check("feature/new-thing", "develop", sha), "feature/new-thing", "develop")

    def test_given_a_fix_branch_from_develop_when_checked_into_develop_then_it_passes(self):
        sha = self.branch("fix/a_bug.2", "develop", "F1")
        self.assert_passes(self.check("fix/a_bug.2", "develop", sha), "fix/a_bug.2", "develop")

    def test_given_a_renovate_branch_when_checked_into_develop_then_it_passes(self):
        sha = self.branch("renovate/jq-1.x", "develop", "R1")
        self.assert_passes(self.check("renovate/jq-1.x", "develop", sha), "renovate/jq-1.x", "develop")

    def test_given_a_dependabot_branch_when_checked_into_develop_then_it_is_refused(self):
        # Renovate replaced Dependabot, so its prefix no longer gets through.
        sha = self.branch("dependabot/npm/left-pad", "develop", "R1")
        self.assert_refused(self.check("dependabot/npm/left-pad", "develop", sha),
                            "Pull requests into develop must come from feature/<name> or fix/<name>",
                            "'dependabot/npm/left-pad'")

    def test_given_a_badly_named_branch_when_checked_into_develop_then_it_is_refused(self):
        sha = self.branch("feature/Upper", "develop", "F1")
        self.assert_refused(self.check("feature/Upper", "develop", sha), "not 'feature/Upper'")

    def test_given_a_feature_branch_cut_from_main_when_checked_into_develop_then_it_is_refused(self):
        sha = self.branch("feature/from-main", "main", "F1")
        self.assert_refused(self.check("feature/from-main", "develop", sha),
                            "'feature/from-main' was branched from main. Recreate it from develop.")

    def test_given_a_release_branch_that_is_a_develop_snapshot_when_checked_into_main_then_it_passes(self):
        sha = self.branch("release/1.1.0", "develop")
        self.assert_passes(self.check("release/1.1.0", "main", sha), "release/1.1.0", "main")

    def test_given_a_release_branch_with_its_own_commit_when_checked_into_main_then_it_is_refused(self):
        sha = self.branch("release/1.1.0", "develop", "own change")
        self.assert_refused(self.check("release/1.1.0", "main", sha),
                            "'release/1.1.0' has commits that are not on develop. A release is a snapshot of develop")

    def test_given_an_already_tagged_version_when_checked_into_main_then_it_is_refused(self):
        sha = self.branch("release/1.1.0", "develop")
        self.git("tag", "v1.1.0", "develop")
        self.assert_refused(self.check("release/1.1.0", "main", sha),
                            "Version 1.1.0 was already released (tag v1.1.0 exists). Bump the version.")

    def test_given_a_bare_version_tag_when_checked_into_main_then_it_is_refused(self):
        # An owner-tagged library tags its releases without the v (tags "[0-9]*"): its released version
        # must be refused too, or the release merges and the owner can no longer tag it.
        sha = self.branch("release/1.1.0", "develop")
        self.git("tag", "1.1.0", "develop")
        self.assert_refused(self.check("release/1.1.0", "main", sha),
                            "Version 1.1.0 was already released (tag 1.1.0 exists). Bump the version.")

    def test_given_another_versions_tag_when_checked_into_main_then_it_passes(self):
        sha = self.branch("release/1.1.0", "develop")
        self.git("tag", "v1.0.0", "develop")
        self.assert_passes(self.check("release/1.1.0", "main", sha), "release/1.1.0", "main")

    def test_given_a_feature_branch_when_checked_into_main_then_it_is_refused(self):
        sha = self.branch("feature/new-thing", "develop", "F1")
        self.assert_refused(self.check("feature/new-thing", "main", sha),
                            "Pull requests into main must come from release/<major>.<minor>.<patch>, "
                            "not 'feature/new-thing'.")

    def test_given_another_base_when_checked_then_there_is_nothing_to_check(self):
        sha = self.branch("whatever", "develop", "F1")
        result = self.check("whatever", "release/1.1.0", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to check", result.stdout)

    def test_given_no_fetched_main_when_checked_into_develop_then_it_is_refused(self):
        # A checkout the pipeline did not fetch into must fail the check, not pass it unexamined.
        sha = self.branch("feature/new-thing", "develop", "F1")
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assert_refused(self.check("feature/new-thing", "develop", sha), "fetch origin/main and origin/develop")

    def test_given_missing_arguments_when_run_then_it_prints_usage(self):
        result = self.check("feature/x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
