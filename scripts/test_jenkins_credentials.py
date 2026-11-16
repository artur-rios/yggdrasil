"""Tests for scripts/jenkins_credentials.py: the repositories' Jenkins credentials, as Configuration as Code.

    python3 -m unittest discover -s scripts
"""

import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import catalog
import jenkins_credentials

SCRIPT = pathlib.Path(__file__).resolve().parent / "jenkins_credentials.py"


class JenkinsCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="jenkins-credentials-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.dir = self.temp / "jenkins-credentials"
        self.dir.mkdir()
        self.dir.chmod(0o755)

    def secret(self, name, value="s3cr3t-value", mode=0o644):
        path = self.dir / name
        path.write_bytes(value.encode())
        path.chmod(mode)
        return path

    def render(self):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.dir)], capture_output=True, text=True)

    def test_given_allowed_ids_when_rendered_then_each_is_a_secret_text_read_from_its_file(self):
        self.secret("reportgenerator-license")
        self.secret("orpheus-android-key-alias", "upload")
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        casc = yaml.safe_load(result.stdout)
        strings = [c["string"] for c in casc["credentials"]["system"]["domainCredentials"][0]["credentials"]]
        self.assertEqual([s["id"] for s in strings], ["orpheus-android-key-alias", "reportgenerator-license"])
        for s in strings:
            self.assertEqual(s["scope"], "GLOBAL")
            self.assertEqual(s["secret"], "${readFile:/run/secrets/jenkins-credentials/" + s["id"] + "}")
        # The secret stays in its file: Jenkins reads it there, and it is never in the rendered configuration.
        self.assertNotIn("s3cr3t-value", result.stdout)
        self.assertNotIn("upload", result.stdout.replace("orpheus-android-key-alias", ""))

    def test_given_no_files_when_rendered_then_it_prints_nothing(self):
        (self.dir / ".gitkeep").write_text("")
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_given_a_name_no_entry_allows_when_rendered_then_it_is_refused_naming_the_allowed_ids(self):
        self.secret("reportgenerator-licence")
        result = self.render()
        self.assertEqual(result.returncode, 1)
        self.assertIn("reportgenerator-licence", result.stderr)
        self.assertIn("reportgenerator-license", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_given_a_forge_or_nuget_id_when_rendered_then_it_is_refused(self):
        # Those are the controller's own (casc.forgejo.yaml, casc.nuget.yaml), never a repository's.
        for name in ("forge-write", "github-app", "nuget-api-key"):
            path = self.secret(name)
            result = self.render()
            self.assertEqual(result.returncode, 1, name)
            self.assertIn(name, result.stderr)
            path.unlink()

    def test_given_a_trailing_newline_when_rendered_then_it_is_refused_with_how_to_write_it(self):
        self.secret("reportgenerator-license", "key\n")
        result = self.render()
        self.assertEqual(result.returncode, 1)
        self.assertIn("trailing newline", result.stderr)
        self.assertIn("printf '%s'", result.stderr)

    def test_given_an_empty_file_when_rendered_then_it_is_refused(self):
        self.secret("reportgenerator-license", "")
        result = self.render()
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty", result.stderr)

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_given_a_file_the_controller_cannot_read_when_rendered_then_it_is_refused_with_the_mode(self):
        # The controller runs as uid 1000 in its container: on a rootless engine it reads the files as "other".
        self.secret("reportgenerator-license", mode=0o600)
        result = self.render()
        self.assertEqual(result.returncode, 1)
        self.assertIn("chmod 644", result.stderr)

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_given_a_directory_the_controller_cannot_enter_when_rendered_then_it_is_refused(self):
        self.secret("reportgenerator-license")
        self.dir.chmod(0o750)
        result = self.render()
        self.assertEqual(result.returncode, 1)
        self.assertIn("chmod 755", result.stderr)

    def test_given_a_missing_directory_when_rendered_then_it_is_refused(self):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.temp / "nope")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a directory", result.stderr)

    def test_given_a_directory_named_as_an_allowed_id_when_rendered_then_it_is_refused_as_not_a_file(self):
        allowed = jenkins_credentials.allowed_ids(catalog.load())
        (self.dir / allowed[0]).mkdir()
        result = self.render()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{allowed[0]}: not a file", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_given_no_directory_or_two_when_run_then_it_is_a_usage_error(self):
        for argv in ([], [str(self.dir), str(self.dir)]):
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("jenkins_credentials.py", result.stderr)

    def test_given_a_catalog_that_cannot_be_read_when_rendered_then_it_exits_with_one_naming_the_error(self):
        err = io.StringIO()
        broken = catalog.CatalogError("catalog.yaml: broken")
        with mock.patch.object(catalog, "load", side_effect=broken), contextlib.redirect_stderr(err):
            status = jenkins_credentials.main([str(self.dir)])
        self.assertEqual(status, 1)
        self.assertEqual(err.getvalue(), "jenkins_credentials: catalog.yaml: broken\n")


if __name__ == "__main__":
    unittest.main()
