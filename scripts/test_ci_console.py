"""Tests for ci/console.sh, the console check.

    python3 -m unittest discover -s scripts

ci/console.sh runs in a copy of the repository's layout with stand-ins: ci/flutter-env.sh and ci/android-sdk.sh
fetch nothing, and fake `flutter` and `dart` commands do what the real ones leave behind. `flutter pub get` links
each plugin of the Windows build from the pub cache, outside the checkout
(console/windows/flutter/ephemeral/.plugin_symlinks), and Jenkins refuses to collect a check's results from a
checkout with a link that leads out of it (LINK_GUARD in jenkins/library/vars/yggdrasilPipeline.groovy).
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAKE_FLUTTER = """#!/usr/bin/env bash
set -euo pipefail
case "$1 ${2:-}" in
  "pub get")
    mkdir -p windows/flutter/ephemeral/.plugin_symlinks
    ln -s "$PUB_CACHE_DIR/some_plugin" windows/flutter/ephemeral/.plugin_symlinks/some_plugin ;;
  "test "*) exit "${FAKE_FLUTTER_TEST_STATUS:-0}" ;;
  "build apk")
    mkdir -p build/app/outputs/flutter-apk
    touch build/app/outputs/flutter-apk/app-release.apk ;;
esac
"""


@unittest.skipIf(shutil.which("bash") is None, "needs bash")
class ConsoleCheckTests(unittest.TestCase):
    def setUp(self):
        self.tree = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree, True)
        self.pub_cache = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.pub_cache, True)
        (self.pub_cache / "some_plugin").mkdir()

        (self.tree / "ci").mkdir()
        shutil.copy(ROOT / "ci" / "console.sh", self.tree / "ci" / "console.sh")
        (self.tree / "ci" / "flutter-env.sh").write_text("")
        (self.tree / "ci" / "android-sdk.sh").write_text("ensure_android_sdk() { :; }\n")
        (self.tree / "console" / "windows").mkdir(parents=True)

        self.bin = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin, True)
        for name, text in (("flutter", FAKE_FLUTTER), ("dart", "#!/usr/bin/env bash\n")):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)

    def run_check(self, test_status=0):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", PUB_CACHE_DIR=str(self.pub_cache),
                   FAKE_FLUTTER_TEST_STATUS=str(test_status))
        return subprocess.run(["bash", str(self.tree / "ci" / "console.sh")], env=env, capture_output=True, text=True)

    def links_out_of_the_tree(self):
        root = self.tree.resolve()
        return [str(path) for path in self.tree.rglob("*")
                if path.is_symlink() and root not in path.resolve().parents and path.resolve() != root]

    def test_given_flutter_links_the_windows_plugins_when_the_check_passes_then_no_link_leads_out_of_the_checkout(self):
        run = self.run_check()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.links_out_of_the_tree(), [])
        # What yggdrasil.yml archives is still there to collect.
        self.assertTrue((self.tree / "console/build/app/outputs/flutter-apk/app-release.apk").is_file())

    def test_given_flutter_links_the_windows_plugins_when_the_tests_fail_then_no_link_leads_out_of_the_checkout(self):
        run = self.run_check(test_status=1)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.links_out_of_the_tree(), [])


if __name__ == "__main__":
    unittest.main()
