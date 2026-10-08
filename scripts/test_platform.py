"""Tests for scripts/platform.sh's checks, against a fake `docker` on PATH.

    python3 -m unittest discover -s scripts
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "platform.sh"


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="platform-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text('#!/usr/bin/env bash\necho "docker $*" >>"$FAKE_DOCKER_LOG"\n')
        docker.chmod(0o755)
        self.log = self.temp / "docker.log"
        self.secrets = self.temp / "secrets"
        self.secrets.mkdir()
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        YGG_SECRETS_DIR=str(self.secrets), FAKE_DOCKER_LOG=str(self.log))

    def up(self, platform_env):
        (self.secrets / "platform.env").write_text(platform_env)
        return subprocess.run(["bash", str(SCRIPT), "up"], env=self.env, capture_output=True, text=True)

    def docker_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_given_an_environment_of_the_catalog_when_up_then_compose_brings_it_up(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_several_environments_of_the_catalog_when_up_then_compose_brings_it_up(self):
        result = self.up("ENVIRONMENTS=development,homologation,production\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_only_the_legacy_environment_when_up_then_it_counts_as_a_list_of_one(self):
        result = self.up("ENVIRONMENT=production\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_both_when_up_then_environments_wins(self):
        result = self.up("ENVIRONMENT=production\nENVIRONMENTS=production,nowhere\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'nowhere' (ENVIRONMENTS", result.stderr)

    def test_given_one_unknown_environment_among_several_when_up_then_it_is_refused(self):
        result = self.up("ENVIRONMENTS=development,staging\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'staging' (ENVIRONMENTS", result.stderr)
        self.assertIn("is not an environment in catalog.yaml", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_no_environment_when_up_then_it_is_refused(self):
        result = self.up("COMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ENVIRONMENTS must be set", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_an_environment_pattern_when_up_then_it_is_refused(self):
        # A regular expression that matches "production" is still not an environment id.
        result = self.up("ENVIRONMENTS='prod.*'\nCOMPOSE_PROFILES=\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not an environment in catalog.yaml", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_the_agent_profile_without_its_variables_when_up_then_it_is_refused(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=agent\nJENKINS_URL=https://jenkins.example.com/\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("JENKINS_AGENT_NAME must be set", result.stderr)
        self.assertEqual(self.docker_calls(), "")


if __name__ == "__main__":
    unittest.main()
