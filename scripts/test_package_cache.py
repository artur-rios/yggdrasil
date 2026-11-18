"""Tests for scripts/package_cache.py: a repository's package cache, its snapshots and what a passing check keeps.

    python3 -m unittest discover -s scripts -p "test_package_cache.py"

An overlay's upper directory is made by hand here: files and directories, a whiteout (a character device 0/0, which
Linux 5.8 and later lets anyone make) and an opaque directory (the xattr), each test skipped where the machine can't
make one.
"""

import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import package_cache as pc  # noqa: E402

SCRIPT = pathlib.Path(__file__).resolve().parent / "package_cache.py"
SCOPE = "orpheus"


@unittest.skipUnless(os.name == "posix", "needs a POSIX file system: hard links, character devices, xattrs")
class PackageCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="package-cache-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cache = self.root / "toolchains"
        self.cache.mkdir()
        self.runs = 0

    def scope_dir(self, scope=SCOPE):
        return self.cache / "packages" / scope

    def snapshot(self, scope=SCOPE):
        return pathlib.Path(pc.snapshot(str(self.cache), scope))

    def new_run(self, files=(), ndk=()):
        """A run directory whose packages/upper (and ndk/upper) hold these {relative path: text} files."""
        self.runs += 1
        run = self.root / f"run{self.runs}"
        for upper, entries in ((run / "packages" / "upper", files), (run / "ndk" / "upper", ndk)):
            upper.mkdir(parents=True)
            for relative, text in dict(entries).items():
                path = upper / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        return run

    def promote(self, run, scope=SCOPE):
        made = pc.promote(str(self.cache), scope, str(run))
        return pathlib.Path(made) if made else None

    def whiteout(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mknod(path, stat.S_IFCHR | 0o600, os.makedev(0, 0))
        except (PermissionError, OSError) as problem:
            self.skipTest(f"can't make a whiteout here: {problem}")

    def seeded(self, files):
        """The scope's current snapshot after a first promotion of these files."""
        self.snapshot()
        made = self.promote(self.new_run(files))
        self.assertIsNotNone(made)
        return made

    # ---- snapshot -------------------------------------------------------------------------------------------

    def test_given_no_cache_when_a_check_starts_then_it_gets_an_empty_snapshot_of_every_kind(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.parent, self.scope_dir() / "snapshots")
        for kind in ("pub", "gradle", "nuget", "npm", "apt/archives/partial", "android-ndk"):
            self.assertTrue((snapshot / kind).is_dir(), kind)
        self.assertEqual(os.readlink(self.scope_dir() / "current"), f"snapshots/{snapshot.name}")
        self.assertTrue((self.scope_dir() / ".used").is_file())

    def test_given_a_current_snapshot_when_a_check_starts_then_it_gets_that_one(self):
        self.assertEqual(self.snapshot(), self.snapshot())

    def test_given_two_repositories_when_their_checks_start_then_each_has_its_own_cache(self):
        self.assertNotEqual(self.snapshot("orpheus").parent.parent, self.snapshot("chimera").parent.parent)

    # ---- promote --------------------------------------------------------------------------------------------

    def test_given_a_check_that_downloaded_a_package_when_kept_then_the_next_snapshot_has_it_and_the_old_ones(self):
        first = self.seeded({"pub/hosted/pub.dev/http-1.0.0/lib/http.dart": "v1"})
        made = self.promote(self.new_run({"gradle/caches/modules-2/files-2.1/x/y.jar": "jar"}))
        self.assertIsNotNone(made)
        self.assertEqual((made / "gradle/caches/modules-2/files-2.1/x/y.jar").read_text(), "jar")
        self.assertEqual((made / "pub/hosted/pub.dev/http-1.0.0/lib/http.dart").read_text(), "v1")
        # A link to the old snapshot's file, not a copy: making a snapshot costs links, not bytes.
        self.assertEqual((made / "pub/hosted/pub.dev/http-1.0.0/lib/http.dart").stat().st_ino,
                         (first / "pub/hosted/pub.dev/http-1.0.0/lib/http.dart").stat().st_ino)
        self.assertEqual(pathlib.Path(pc.current(str(self.scope_dir()))), made)
        self.assertTrue(pathlib.Path(f"{first}.retired").is_file())

    def test_given_a_file_the_check_rewrote_when_kept_then_the_old_snapshot_still_has_the_old_one(self):
        # A snapshot is never written once it is there: a check that started from it may still be reading it.
        first = self.seeded({"pub/hosted/pub.dev/a-1.0.0/a.dart": "old", "pub/hosted/pub.dev/b-1.0.0/b.dart": "b"})
        made = self.promote(self.new_run({"pub/hosted/pub.dev/a-1.0.0/a.dart": "new", "pub/new.dart": "n"}))
        self.assertEqual((made / "pub/hosted/pub.dev/a-1.0.0/a.dart").read_text(), "new")
        self.assertEqual((first / "pub/hosted/pub.dev/a-1.0.0/a.dart").read_text(), "old")

    def test_given_a_check_that_only_rewrote_files_when_kept_then_no_snapshot_is_made(self):
        # A lock or an index rewritten on every run is not worth a new snapshot; a download is.
        first = self.seeded({"gradle/caches/modules-2/modules-2.lock": "1"})
        self.assertIsNone(self.promote(self.new_run({"gradle/caches/modules-2/modules-2.lock": "2"})))
        self.assertEqual(pathlib.Path(pc.current(str(self.scope_dir()))), first)

    def test_given_only_what_every_run_writes_when_kept_then_none_of_it_is(self):
        first = self.seeded({"npm/_cacache/index-v5/aa/bb": "entry"})
        noise = {"gradle/daemon/8.14/daemon-123.out.log": "log", "npm/_logs/debug.log": "log",
                 "pub/active_roots/ab/cd": "root", "apt/archives/partial/x.deb": "half", "apt/archives/lock": ""}
        self.assertIsNone(self.promote(self.new_run(noise)))
        made = self.promote(self.new_run({**noise, "apt/archives/clang_18_amd64.deb": "deb"}))
        self.assertEqual((made / "apt/archives/clang_18_amd64.deb").read_text(), "deb")
        for relative in noise:
            if relative != "apt/archives/lock":
                self.assertFalse((made / relative).exists(), relative)
        self.assertTrue((made / "apt/archives/partial").is_dir(), "apt needs its partial directory")
        self.assertTrue(first.is_dir())

    def test_given_an_ndk_the_check_installed_when_kept_then_it_is_under_android_ndk(self):
        self.snapshot()
        made = self.promote(self.new_run(ndk={"27.0.12077973/source.properties": "Pkg.Revision = 27.0.12077973"}))
        self.assertIsNotNone(made)
        self.assertTrue((made / "android-ndk/27.0.12077973/source.properties").is_file())

    def test_given_a_path_the_check_deleted_when_kept_then_the_next_snapshot_has_it_no_more(self):
        first = self.seeded({"npm/_cacache/content/old": "old", "npm/keep": "keep"})
        run = self.new_run({"npm/_cacache/content/new": "new"})
        self.whiteout(run / "packages" / "upper" / "npm/_cacache/content/old")
        made = self.promote(run)
        self.assertFalse((made / "npm/_cacache/content/old").exists())
        self.assertTrue((made / "npm/keep").exists())
        self.assertTrue((first / "npm/_cacache/content/old").exists(), "the old snapshot is never written")

    def test_given_only_deletions_when_kept_then_no_snapshot_is_made(self):
        self.seeded({"npm/a": "a"})
        run = self.new_run()
        self.whiteout(run / "packages" / "upper" / "npm/a")
        self.assertIsNone(self.promote(run))

    def test_given_a_directory_the_check_replaced_when_kept_then_its_old_files_are_gone(self):
        self.seeded({"gradle/caches/transforms-4/abc/old.jar": "old"})
        run = self.new_run({"gradle/caches/transforms-4/abc/new.jar": "new"})
        try:
            os.setxattr(run / "packages/upper/gradle/caches/transforms-4/abc", "user.overlay.opaque", b"y")
        except OSError as problem:
            self.skipTest(f"no user xattrs here: {problem}")
        made = self.promote(run)
        self.assertEqual(sorted(p.name for p in (made / "gradle/caches/transforms-4/abc").iterdir()), ["new.jar"])

    def test_given_a_file_where_the_snapshot_has_a_directory_when_kept_then_the_file_wins(self):
        self.seeded({"nuget/pkg/1.0.0/a.nupkg": "a"})
        run = self.new_run({"nuget/pkg/1.0.0b": "x"})
        (run / "packages/upper/nuget/pkg").rename(run / "packages/upper/nuget/tmp")
        (run / "packages/upper/nuget/pkg").write_text("now a file")
        made = self.promote(run)
        self.assertEqual((made / "nuget/pkg").read_text(), "now a file")

    def test_given_a_link_the_check_made_when_kept_then_it_is_kept_as_a_link(self):
        self.snapshot()
        run = self.new_run({"pub/bin/real": "x"})
        (run / "packages/upper/pub/bin/alias").symlink_to("real")
        made = self.promote(run)
        self.assertEqual(os.readlink(made / "pub/bin/alias"), "real")

    def test_given_a_lineage_a_month_old_when_kept_then_the_next_check_starts_from_nothing(self):
        first = self.seeded({"pub/a": "a"})
        pathlib.Path(f"{first}.born").write_text(f"{time.time() - 31 * 86400:.0f}\n")
        self.assertIsNone(self.promote(self.new_run({"pub/b": "b"})))
        self.assertFalse(os.path.lexists(self.scope_dir() / "current"))
        fresh = self.snapshot()
        self.assertNotEqual(fresh, first)
        self.assertFalse((fresh / "pub/a").exists())
        self.assertGreater(pc.born(str(fresh)), time.time() - 60)

    def test_given_a_lineage_when_kept_then_the_new_snapshot_keeps_its_start(self):
        first = self.seeded({"pub/a": "a"})
        started = time.time() - 5 * 86400
        pathlib.Path(f"{first}.born").write_text(f"{started:.0f}\n")
        made = self.promote(self.new_run({"pub/b": "b"}))
        self.assertAlmostEqual(pc.born(str(made)), started, delta=1)

    # ---- gc ---------------------------------------------------------------------------------------------------

    def test_given_a_snapshot_retired_a_day_ago_when_another_is_kept_then_it_is_removed(self):
        first = self.seeded({"pub/a": "a"})
        second = self.promote(self.new_run({"pub/b": "b"}))
        day_ago = time.time() - 25 * 3600
        os.utime(f"{first}.retired", (day_ago, day_ago))
        third = self.promote(self.new_run({"pub/c": "c"}))
        self.assertFalse(first.exists())
        self.assertFalse(pathlib.Path(f"{first}.born").exists())
        self.assertTrue(second.is_dir(), "retired an instant ago: a check may still read it")
        self.assertTrue(third.is_dir())

    def test_given_a_repository_no_check_used_for_a_month_when_another_keeps_then_its_cache_is_removed(self):
        self.snapshot("old-site")
        month_ago = time.time() - 31 * 86400
        os.utime(self.scope_dir("old-site") / ".used", (month_ago, month_ago))
        self.snapshot("recent")
        self.seeded({"pub/a": "a"})
        self.assertFalse(self.scope_dir("old-site").exists())
        self.assertTrue(self.scope_dir("recent").exists())

    def test_given_a_snapshot_a_killed_agent_left_half_made_when_another_is_kept_then_it_is_removed(self):
        self.snapshot()
        half = self.scope_dir() / "snapshots" / ".incoming-deadbeef"
        half.mkdir()
        day_ago = time.time() - 25 * 3600
        os.utime(half, (day_ago, day_ago))
        self.promote(self.new_run({"pub/a": "a"}))
        self.assertFalse(half.exists())

    # ---- the command line -------------------------------------------------------------------------------------

    def run_script(self, *arguments):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *arguments], capture_output=True, text=True,
                              timeout=60)

    def test_given_snapshot_when_run_then_it_prints_the_snapshot(self):
        result = self.run_script("snapshot", str(self.cache), SCOPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), pc.current(str(self.scope_dir())))

    def test_given_promote_when_run_then_the_run_is_kept(self):
        self.snapshot()
        run = self.new_run({"pub/a": "a"})
        result = self.run_script("promote", str(self.cache), SCOPE, str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((pathlib.Path(pc.current(str(self.scope_dir()))) / "pub/a").is_file())
        self.assertIn("kept what the check added", result.stderr)

    def test_given_bad_arguments_when_run_then_it_is_a_usage_error(self):
        for arguments in ([], ["snapshot", str(self.cache)], ["frob", str(self.cache), SCOPE],
                          ["snapshot", "relative", SCOPE], ["snapshot", str(self.cache), ".."],
                          ["snapshot", str(self.cache), "a/b"], ["promote", str(self.cache), SCOPE]):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr)
        self.assertFalse((self.cache / "packages").exists())


if __name__ == "__main__":
    unittest.main()
