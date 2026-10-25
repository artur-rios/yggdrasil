"""Tests for scripts/relink.py, against scratch git repositories.   python3 -m unittest discover -s scripts

A repository here is a real one made with git in a temporary directory, because the tool reads
`git ls-files`: what git tracks is what it may rewrite, so a stand-in for git would test nothing.

The old addresses are named by these two constants and the fixtures built from them; the tool skips this file
and relink.py when it runs over yggdrasil itself.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "relink.py"

PAGES = "artur-rios.github.io"
VERCEL = ".vercel.app"

needs_git = unittest.skipUnless(shutil.which("git"), "needs git")


@needs_git
class RelinkTests(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp(prefix="relink-test-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        # The machine's own git configuration (signing, hooks, autocrlf) must not reach the test.
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                        PYTHONIOENCODING="utf-8")
        # Task 6 adds the sites to catalog.yaml; until a test says otherwise the tool must work from its
        # fixed table, so these tests point it at a catalog that knows no site.
        self.catalog = self.repo.parent / (self.repo.name + "-catalog.yaml")
        self.addCleanup(lambda: self.catalog.unlink(missing_ok=True))
        self.catalog.write_text(textwrap.dedent("""\
            owner: artur-rios
            environments:
              - id: production
                name: Production
            systems:
              - id: other
                name: Other
                description: Not a site.
                applications:
                  - id: other-api
                    name: Other API
                    kind: api
                    health: http://other-api:8080/healthz
                    host: other
            """), encoding="utf-8")
        self.git("init", "--quiet", "--initial-branch=develop")

    def git(self, *arguments):
        done = subprocess.run(["git", "-C", str(self.repo), *arguments], env=self.env, capture_output=True,
                              text=True, check=False)
        self.assertEqual(done.returncode, 0, f"git {' '.join(arguments)}: {done.stderr}")
        return done.stdout.strip()

    def track(self, name, content, binary=False):
        """A file committed to the repository; `content` is str (written as given: no newline translation)
        or bytes."""
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        self.git("add", "-f", name)
        self.git("commit", "--quiet", "-m", f"add {name}")
        return path

    def read(self, name):
        return (self.repo / name).read_bytes().decode("utf-8")

    def relink(self, *flags, catalog=None):
        env = dict(self.env, YGG_CATALOG=str(catalog or self.catalog))
        return subprocess.run([sys.executable, str(SCRIPT), str(self.repo), *flags], capture_output=True,
                              text=True, encoding="utf-8", env=env)

    # -- the rewrites ------------------------------------------------------------------------------

    def test_given_a_pages_link_when_relinked_then_it_points_at_the_docs_host_and_path(self):
        self.track("README.md", f"Docs: https://{PAGES}/dotnet-data/docs/relational/ and [x]({PAGES}/heimdall-docs).\n")

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"),
                         "Docs: https://docs.artur-rios.tech/dotnet-data/docs/relational/ "
                         "and [x](docs.artur-rios.tech/heimdall-docs).\n")

    def test_given_every_docs_site_when_relinked_then_each_keeps_its_repository_path(self):
        repositories = ["heimdall-docs", "fortuna-docs", "alexandria-docs", "orpheus", "heimdall-api",
                        "dotnet-libraries", "dotnet-configuration", "dotnet-data", "dotnet-extensions",
                        "dotnet-jwt", "dotnet-logging", "dotnet-mediator", "dotnet-messaging", "dotnet-output",
                        "dotnet-test-util", "dotnet-util", "dotnet-validation", "dotnet-webapi-util"]
        self.track("docs.md", "".join(f"https://{PAGES}/{r}/x\n" for r in repositories))

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("docs.md"), "".join(f"https://docs.artur-rios.tech/{r}/x\n" for r in repositories))

    def test_given_a_vercel_link_when_relinked_then_it_points_at_the_sites_new_host(self):
        self.track("README.md",
                   f"[Live](https://website-placeholder-artur-rios{VERCEL})\n"
                   f"![Top](https://github-readme-stats-six-tau-89{VERCEL}/api/top-langs/?username=artur-rios)\n")

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"),
                         "[Live](https://placeholder.artur-rios.tech)\n"
                         "![Top](https://readme-stats.techcraftsmen.net/api/top-langs/?username=artur-rios)\n")

    def test_given_personal_lab_when_relinked_then_it_moves_to_the_root_of_its_own_host(self):
        self.track("README.md",
                   f"https://{PAGES}/personal-lab\n"
                   f"https://{PAGES}/personal-lab/\n"
                   f"https://{PAGES}/personal-lab/posts/first/\n")

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"),
                         "https://lab.artur-rios.tech\n"
                         "https://lab.artur-rios.tech/\n"
                         "https://lab.artur-rios.tech/posts/first/\n")

    def test_given_a_github_repository_link_when_relinked_then_it_is_left(self):
        text = ("[repo](https://github.com/artur-rios/dotnet-data)\n"
                f"[profile repo](https://github.com/artur-rios/{PAGES})\n"
                f"raw: https://raw.githubusercontent.com/artur-rios/{PAGES}/main/README.md\n"
                "git clone git@github.com:artur-rios/heimdall-api.git\n")
        self.track("README.md", text)

        done = self.relink("--check")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"), text)

    def test_given_other_peoples_github_io_sites_when_relinked_then_they_are_left(self):
        text = ("https://rapidfuzz.github.io/RapidFuzz/\n"
                "https://<owner>.github.io/<repo>/\n"
                "https://google.github.io/styleguide/\n"
                f"mail someone@{PAGES}\n")
        self.track("notes.md", text)

        done = self.relink("--check")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("notes.md"), text)

    def test_given_a_package_project_url_when_relinked_then_it_is_rewritten(self):
        self.track("src/Lib/Lib.csproj",
                   "<Project>\n  <PropertyGroup>\n"
                   f"    <PackageProjectUrl>https://{PAGES}/dotnet-util</PackageProjectUrl>\n"
                   "    <RepositoryUrl>https://github.com/artur-rios/dotnet-util</RepositoryUrl>\n"
                   "  </PropertyGroup>\n</Project>\n")
        self.track("Directory.Build.props",
                   f"<Project><PropertyGroup><PackageProjectUrl>https://{PAGES}/dotnet-data/"
                   "</PackageProjectUrl></PropertyGroup></Project>\n")

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("<PackageProjectUrl>https://docs.artur-rios.tech/dotnet-util</PackageProjectUrl>",
                      self.read("src/Lib/Lib.csproj"))
        self.assertIn("<RepositoryUrl>https://github.com/artur-rios/dotnet-util</RepositoryUrl>",
                      self.read("src/Lib/Lib.csproj"))
        self.assertIn("<PackageProjectUrl>https://docs.artur-rios.tech/dotnet-data/</PackageProjectUrl>",
                      self.read("Directory.Build.props"))

    # -- check mode and output ---------------------------------------------------------------------

    def test_given_check_mode_with_a_stale_link_when_run_then_it_exits_1_and_changes_nothing(self):
        text = f"https://{PAGES}/dotnet-data/\n"
        self.track("README.md", text)

        done = self.relink("--check")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"), text)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_given_check_mode_with_nothing_stale_when_run_then_it_exits_0(self):
        self.track("README.md", "https://docs.artur-rios.tech/dotnet-data/\n")

        done = self.relink("--check")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_given_a_change_when_run_then_it_prints_file_line_old_and_new(self):
        self.track("docs/index.md", f"first\nsee https://{PAGES}/dotnet-data/docs/x/ now\n")

        done = self.relink("--check")

        self.assertIn(f"docs/index.md:2: {PAGES}/dotnet-data/docs/x/ → docs.artur-rios.tech/dotnet-data/docs/x/",
                      done.stdout.splitlines())

    def test_given_a_second_run_when_the_first_wrote_then_nothing_is_left_to_change(self):
        self.track("README.md", f"https://{PAGES}/dotnet-data/\n")
        self.assertEqual(self.relink().returncode, 0)

        again = self.relink("--check")

        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    # -- addresses the table does not know ---------------------------------------------------------

    def test_given_an_unknown_pages_repository_when_relinked_then_it_is_reported_and_not_guessed(self):
        text = f"https://{PAGES}/demo-docs/\n"
        self.track("README.md", text)

        done = self.relink("--check")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f"README.md:1: unknown address {PAGES}/demo-docs/", done.stdout)
        self.assertEqual(self.read("README.md"), text)

    def test_given_an_unknown_vercel_project_when_relinked_then_it_is_reported_and_not_guessed(self):
        text = f"https://some-project{VERCEL}/x\n"
        self.track("README.md", text)

        done = self.relink("--check")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f"README.md:1: unknown address some-project{VERCEL}/x", done.stdout)

    def test_given_the_bare_pages_host_when_relinked_then_it_is_left_for_the_owner_to_decide(self):
        text = f"The profile page: https://{PAGES}\nand again {PAGES}/\n"
        self.track("README.md", text)

        done = self.relink()

        # Writing mode: the known part is written, this is not, and the exit says something is left.
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"), text)
        self.assertIn(f"README.md:1: unknown address {PAGES} (the profile page: the owner decides)", done.stdout)
        self.assertIn("README.md:2:", done.stdout)

    def test_given_a_placeholder_in_prose_when_relinked_then_it_is_reported_as_one(self):
        self.track("plan.md", f"links to {PAGES}/<repo> change\n")

        done = self.relink("--check")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f"plan.md:1: unknown address {PAGES}/ (a placeholder in prose, not an address)", done.stdout)

    def test_given_unknown_addresses_in_write_mode_when_run_then_the_known_ones_are_still_written(self):
        self.track("README.md", f"https://{PAGES}/demo-docs/ and https://{PAGES}/dotnet-util/\n")

        done = self.relink()

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"), f"https://{PAGES}/demo-docs/ and https://docs.artur-rios.tech/dotnet-util/\n")

    # -- what is never touched ---------------------------------------------------------------------

    def test_given_lock_files_binaries_and_untracked_files_when_relinked_then_they_are_untouched(self):
        stale = f"https://{PAGES}/dotnet-data/\n"
        self.track("package-lock.json", stale)
        self.track("pnpm-lock.yaml", stale)
        self.track("packages.lock.json", stale)
        self.track("Cargo.lock", stale)
        self.track("sub/yarn.lock", stale)
        blob = b"\x89PNG\r\n\x00\x00" + stale.encode()
        self.track("logo.png", blob)
        (self.repo / "untracked.md").write_bytes(stale.encode())
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "x.md").write_bytes(stale.encode())

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for name in ["package-lock.json", "pnpm-lock.yaml", "packages.lock.json", "Cargo.lock", "sub/yarn.lock",
                     "untracked.md", "node_modules/x.md"]:
            self.assertEqual(self.read(name), stale, name)
        self.assertEqual((self.repo / "logo.png").read_bytes(), blob)

    def test_given_a_line_marked_to_keep_when_relinked_then_it_is_left(self):
        text = f"old: https://{PAGES}/dotnet-data/  # relink:keep\nnew: https://{PAGES}/dotnet-data/\n"
        self.track("README.md", text)

        self.assertEqual(self.relink().returncode, 0)

        self.assertEqual(self.read("README.md"),
                         f"old: https://{PAGES}/dotnet-data/  # relink:keep\nnew: https://docs.artur-rios.tech/dotnet-data/\n")

    def test_given_the_tool_and_its_tests_in_the_repository_when_relinked_then_they_are_untouched(self):
        # Run over yggdrasil itself, the tool would otherwise rewrite the table that says what to rewrite.
        text = f"x = 'https://{PAGES}/dotnet-data/'\n"
        self.track("scripts/relink.py", text)
        self.track("scripts/test_relink.py", text)

        done = self.relink("--check")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_given_a_changelog_when_relinked_then_only_the_unreleased_section_changes(self):
        self.track("CHANGELOG.md", textwrap.dedent(f"""\
            # Changelog

            ## [Unreleased]

            - Docs moved from https://{PAGES}/dotnet-data/

            ## [1.2.0] - 2026-01-01

            - Added https://{PAGES}/dotnet-data/docs/x/
            - Vercel https://some-project{VERCEL}

            [Unreleased]: https://github.com/artur-rios/dotnet-data/compare/1.2.0...HEAD
            """))

        done = self.relink()

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)  # the unknown vercel project
        changelog = self.read("CHANGELOG.md")
        self.assertIn("- Docs moved from https://docs.artur-rios.tech/dotnet-data/\n", changelog)
        self.assertIn(f"- Added https://{PAGES}/dotnet-data/docs/x/\n", changelog)
        # The released sections are reported, not rewritten: the line, and that it is history.
        self.assertIn(f"CHANGELOG.md:9: kept (released section) {PAGES}/dotnet-data/docs/x/", done.stdout)

    def test_given_only_released_sections_with_stale_links_when_checked_then_the_exit_is_0(self):
        # Released sections are history: the tool cannot change them, so they must not fail the check forever.
        self.track("CHANGELOG.md", f"## [1.0.0]\n\n- https://{PAGES}/dotnet-data/\n")

        done = self.relink("--check")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("kept (released section)", done.stdout)

    # -- how files are written ---------------------------------------------------------------------

    def test_given_crlf_and_a_byte_order_mark_when_relinked_then_both_are_kept(self):
        self.track("README.md", b"\xef\xbb\xbfa\r\nhttps://" + f"{PAGES}/dotnet-util/".encode() + b"\r\nb\r\n")
        self.track("mixed.txt", f"x\nhttps://{PAGES}/dotnet-util/\r\ny\n".encode())

        self.assertEqual(self.relink().returncode, 0)

        self.assertEqual((self.repo / "README.md").read_bytes(),
                         b"\xef\xbb\xbfa\r\nhttps://docs.artur-rios.tech/dotnet-util/\r\nb\r\n")
        self.assertEqual((self.repo / "mixed.txt").read_bytes(),
                         b"x\nhttps://docs.artur-rios.tech/dotnet-util/\r\ny\n")

    def test_given_a_latin1_file_when_relinked_then_its_other_bytes_are_kept(self):
        self.track("notes.txt", "caf\xe9 ".encode("latin-1") + f"https://{PAGES}/dotnet-util/\n".encode())

        self.assertEqual(self.relink().returncode, 0)

        self.assertEqual((self.repo / "notes.txt").read_bytes(),
                         "caf\xe9 ".encode("latin-1") + b"https://docs.artur-rios.tech/dotnet-util/\n")

    # -- the catalog -------------------------------------------------------------------------------

    def test_given_a_catalog_entry_for_a_site_when_relinked_then_the_catalog_wins_over_the_fixed_table(self):
        self.catalog.write_text(textwrap.dedent("""\
            owner: artur-rios
            environments:
              - id: production
                name: Production
            systems:
              - id: sites
                name: Sites
                description: The sites.
                applications:
                  - id: dotnet-data-docs
                    name: dotnet-data docs
                    kind: web
                    health: http://dotnet-data-docs:8080/healthz
                    repository: dotnet-data
                    domain: docs.example.org
                    apex: true
                    path: /dotnet-data
                    environments: {production: {}}
                  - id: personal-lab
                    name: Lab
                    kind: web
                    health: http://personal-lab:8080/healthz
                    host: laboratory
                    domain: example.org
                    environments: {production: {}}
            """), encoding="utf-8")
        self.track("README.md", f"https://{PAGES}/dotnet-data/x\nhttps://{PAGES}/personal-lab/y\n"
                                f"https://{PAGES}/dotnet-util/z\n")

        done = self.relink()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read("README.md"),
                         "https://docs.example.org/dotnet-data/x\nhttps://laboratory.example.org/y\n"
                         "https://docs.artur-rios.tech/dotnet-util/z\n")

    # -- usage -------------------------------------------------------------------------------------

    def test_given_a_directory_that_is_not_a_repository_when_run_then_it_exits_2(self):
        plain = pathlib.Path(tempfile.mkdtemp(prefix="relink-plain-"))
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        env = dict(self.env, GIT_CEILING_DIRECTORIES=str(plain.parent))

        done = subprocess.run([sys.executable, str(SCRIPT), str(plain)], capture_output=True, text=True, env=env)

        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

    def test_given_no_arguments_when_run_then_it_prints_the_usage_and_exits_2(self):
        done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)

        self.assertEqual(done.returncode, 2)
        self.assertIn("relink.py <repo-dir> [--check]", done.stderr)


if __name__ == "__main__":
    unittest.main()
