"""Tests for scripts/ygg.sh's commands that need no prompt, against a fake `docker` on PATH.

    python3 -m unittest discover -s scripts

The fake answers from a list of containers (Compose project, id, state) and logs every call, so these
run without a Docker engine and never touch a real container. They use the repository's own
catalog.yaml: development and homologation on demand, production not, local not on this host.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "ygg.sh"

FAKE_DOCKER = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # A stand-in for the docker CLI: logs each call, answers from $FAKE_DOCKER_DIR/containers, whose
    # lines are "<compose project> <id> <state>".
    set -euo pipefail
    state=$FAKE_DOCKER_DIR
    echo "$*" >>"$state/calls"
    case "$1" in
      info) ;;
      ps)
        all="" project=""
        for argument in "$@"; do
          case $argument in
            -a | --all) all=1 ;;
            label=com.docker.compose.project=*) project=${argument#label=com.docker.compose.project=} ;;
          esac
        done
        while read -r p id s; do
          [[ -z "$project" || "$p" == "$project" ]] || continue
          [[ -n "$all" || "$s" == running ]] || continue
          echo "$id"
        done <"$state/containers"
        ;;
      start | stop) ;;
      *) echo "fake docker: unexpected call: $*" >&2; exit 64 ;;
    esac
""").lstrip()


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class EnvironmentCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.state = self.temp / "state"
        self.state.mkdir()
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.secrets = self.temp / "secrets"
        self.secrets.mkdir()
        self.platform_env("ENVIRONMENTS=development,homologation,production\n")
        env = {k: v for k, v in os.environ.items() if k != "YGG_ENVIRONMENT"}
        self.env = dict(env, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        YGG_SECRETS_DIR=str(self.secrets), FAKE_DOCKER_DIR=str(self.state))
        self.containers(
            "heimdall-api-development d1 exited",
            "heimdall-ui-development d2 exited",
            "heimdall-api-production p1 running",
            "heimdall-ui-production p2 running",
        )

    def platform_env(self, text):
        (self.secrets / "platform.env").write_text(text)

    def containers(self, *lines):
        (self.state / "containers").write_text("".join(line + "\n" for line in lines))

    def ygg(self, *arguments, **env):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=dict(self.env, **env),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def calls(self, command):
        path = self.state / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if line.split(" ")[0] == command]

    def test_given_a_stopped_on_demand_environment_when_started_then_its_containers_start(self):
        result = self.ygg("env", "start", "development")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("start"), ["start d1", "start d2"])
        self.assertIn("fortuna-api: not deployed in development", result.stdout)

    def test_given_an_on_demand_environment_when_stopped_then_only_its_running_containers_stop(self):
        self.containers("heimdall-api-development d1 running", "heimdall-ui-development d2 exited",
                        "heimdall-api-production p1 running")
        result = self.ygg("env", "stop", "development")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("stop"), ["stop d1"])

    def test_given_an_environment_that_is_not_on_demand_when_stopped_then_it_is_refused(self):
        result = self.ygg("env", "stop", "production")
        self.assertEqual(result.returncode, 1)
        self.assertIn("production is not on demand", result.stderr)
        self.assertEqual(self.calls("stop"), [])

    def test_given_force_when_an_environment_that_is_not_on_demand_is_stopped_then_it_stops(self):
        result = self.ygg("env", "stop", "production", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("stop"), ["stop p1", "stop p2"])

    def test_given_an_environment_of_another_host_when_started_then_it_is_refused(self):
        result = self.ygg("env", "start", "local")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'local' is not an environment of this host", result.stderr)
        self.assertEqual(self.calls("start"), [])

    def test_given_the_host_environments_when_env_status_then_one_line_each_in_catalog_order(self):
        result = self.ygg("env", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.split() for line in result.stdout.splitlines() if line.startswith("  ")]
        self.assertEqual([line[0] for line in lines[1:]], ["development", "homologation", "production"])
        self.assertIn("stopped", lines[1])
        self.assertIn("deployed", lines[2])  # not deployed
        self.assertIn("running", lines[3])

    def test_given_only_the_legacy_environment_when_env_status_then_it_is_the_host_environment(self):
        self.platform_env("ENVIRONMENT=production\n")
        result = self.ygg("env", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.split() for line in result.stdout.splitlines() if line.startswith("  ")]
        self.assertEqual([line[0] for line in lines[1:]], ["production"])

    def test_given_ygg_environment_when_env_status_then_it_overrides_platform_env(self):
        result = self.ygg("env", "status", YGG_ENVIRONMENT="homologation")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.split() for line in result.stdout.splitlines() if line.startswith("  ")]
        self.assertEqual([line[0] for line in lines[1:]], ["homologation"])

    def test_given_an_unknown_environment_in_platform_env_when_env_status_then_it_is_refused(self):
        self.platform_env("ENVIRONMENTS=production,staging\n")
        result = self.ygg("env", "status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'staging'", result.stderr)

    def test_given_a_bad_usage_when_env_then_it_says_how(self):
        result = self.ygg("env", "start", "development", "production")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage: scripts/ygg.sh env", result.stderr)


class VariablesStoreTests(EnvironmentCommandTests):
    def use_store(self):
        import sys
        sys.path.insert(0, str(SCRIPT.parent))
        import vars as v
        (self.secrets / "platform.env").unlink()
        v.init(self.secrets, confirm=lambda prompt: "saved")
        with v.Store.open(self.secrets) as store:
            store.set("platform", "ENVIRONMENTS", "homologation,production", None, "set")

    def test_given_a_store_when_env_status_then_environments_come_from_it(self):
        self.use_store()
        result = self.ygg("env", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("homologation", result.stdout)
        self.assertNotIn("development", result.stdout)

    def test_given_vars_when_run_then_it_passes_through_to_vars_py(self):
        self.use_store()
        self.assertEqual(self.ygg("vars", "set", "platform", "DOMAIN=example.com").returncode, 0)
        self.assertEqual(self.ygg("vars", "get", "platform", "DOMAIN").stdout, "example.com\n")


if __name__ == "__main__":
    unittest.main()
