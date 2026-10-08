"""Tests for scripts/deploy.sh, against a fake `docker` on PATH.   python3 -m unittest discover -s scripts

The fake records every call and answers from files in its state directory, so these run anywhere
bash and git do, without a Docker engine. They use heimdall-ui in development (ports mode) from the
repository's own catalog.yaml.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts" / "deploy.sh"
STACK = "heimdall-ui"
ENVIRONMENT = "development"

FAKE_DOCKER = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # A stand-in for the docker CLI: logs each call, answers from $FAKE_DOCKER_DIR.
    set -euo pipefail
    state=$FAKE_DOCKER_DIR
    printf '%s IMAGE_TAG=%s\n' "$*" "${IMAGE_TAG:-}" >>"$state/calls"

    if [[ "$1" == compose ]]; then
      shift
      files=()
      while (($#)); do
        case $1 in
          --project-name | --env-file) shift 2 ;;
          -f) files+=("$2"); shift 2 ;;
          *) break ;;
        esac
      done
      case "$1 ${2:-}" in
        "config --services") cat "$state/services" ;;
        "config --images")
          while read -r repository; do echo "$repository:$IMAGE_TAG"; done <"$state/repositories"
          cat "$state/other_images" 2>/dev/null || true
          ;;
        up*)
          count=$(($(cat "$state/ups" 2>/dev/null || echo 0) + 1))
          echo "$count" >"$state/ups"
          # The labels file deploy.sh generates is always the last -f.
          cp "${files[-1]}" "$state/up$count.labels"
          echo "$IMAGE_TAG" >"$state/up$count.tag"
          if command -v flock >/dev/null; then
            status=0
            flock --nonblock --conflict-exit-code 75 "$APP_ENV_FILE" true || status=$?
            echo "$status" >"$state/up$count.lock"
          fi
          exit "$(sed -n "${count}p" "$state/up_results" 2>/dev/null | grep . || echo 0)"
          ;;
      esac
      exit 0
    fi

    case "$1" in
      ps)
        format=${*: -1}
        while read -r id image; do
          case $format in
            *.ID*.Image*) echo "$id $image" ;;
            *.Image*) echo "$image" ;;
            *.ID*) echo "$id" ;;
          esac
        done <"$state/ps"
        ;;
      inspect)
        label=$(sed -n 's/.*"yggdrasil\.\([a-z_]*\)".*/\1/p' <<<"$3")
        sed -n "s/^$label=//p" "$state/labels.$4" 2>/dev/null || true
        ;;
      image) ;;
      network) ;;
      *) echo "fake docker: unexpected call: $*" >&2; exit 64 ;;
    esac
""").lstrip()


@unittest.skipUnless(shutil.which("bash") and shutil.which("git") and os.name == "posix",
                     "needs bash and git on a POSIX system")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="deploy-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)

        self.state = self.temp / "state"
        self.state.mkdir()
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)

        secrets = self.temp / "secrets"
        (secrets / ENVIRONMENT).mkdir(parents=True)
        self.env_file = secrets / ENVIRONMENT / f"{STACK}.env"
        self.env_file.write_text("HEIMDALL_API_BASE_URL=http://localhost:8080\n")

        self.app = self.temp / "app"
        self.app.mkdir()
        git = ["git", "-C", str(self.app), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git[:3] + ["init", "--quiet"], check=True)
        subprocess.run(git + ["commit", "--quiet", "--allow-empty", "-m", "app"], check=True)
        self.commit = subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True,
                                     capture_output=True, text=True).stdout.strip()[:7]

        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        FAKE_DOCKER_DIR=str(self.state), YGG_SECRETS_DIR=str(secrets))
        self.given("services", "ui")
        self.given("repositories", STACK)
        self.given("ps", "")

    def given(self, name, text):
        (self.state / name).write_text(text + ("\n" if text else ""))

    def read(self, name):
        path = self.state / name
        return path.read_text() if path.exists() else ""

    def deploy(self, version=None):
        version = version or f"2.0.0-{self.commit}"
        return subprocess.run(["bash", str(DEPLOY), ENVIRONMENT, STACK, str(self.app), version],
                              env=self.env, capture_output=True, text=True)

    def test_given_a_healthy_release_when_deployed_then_labelled_and_not_rolled_back(self):
        self.given("ps", "c1 heimdall-ui:1.0.0-aaaaaaa")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("ups").strip(), "1")
        self.assertEqual(self.read("up1.tag").strip(), f"2.0.0-{self.commit}")
        labels = self.read("up1.labels")
        self.assertIn('yggdrasil.version: "2.0.0"', labels)
        self.assertIn(f'yggdrasil.commit: "{self.commit}"', labels)

    def test_given_an_unhealthy_release_when_deployed_then_it_rolls_back_to_the_previous_image_and_labels(self):
        self.given("ps", "c1 heimdall-ui:1.0.0-aaaaaaa")
        self.given("labels.c1", "version=1.0.0\ncommit=aaaaaaa\ndeployed_at=2026-01-01T00:00:00Z")
        self.given("up_results", "1\n0")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn("rolling back to 1.0.0-aaaaaaa", result.stderr)
        self.assertEqual(self.read("up2.tag").strip(), "1.0.0-aaaaaaa")
        labels = self.read("up2.labels")
        self.assertIn('yggdrasil.version: "1.0.0"', labels)
        self.assertIn('yggdrasil.commit: "aaaaaaa"', labels)
        self.assertIn('yggdrasil.deployed_at: "2026-01-01T00:00:00Z"', labels)

    def test_given_a_stack_that_also_runs_a_database_when_rolling_back_then_the_application_image_is_the_target(self):
        # docker ps lists the newest container first, and here that is the database's: its tag ("16")
        # is not a version of the application.
        self.given("services", "db\nui")
        self.given("other_images", "postgres:16")
        self.given("ps", "d1 postgres:16\nc1 heimdall-ui:1.0.0-aaaaaaa")
        self.given("labels.c1", "version=1.0.0\ncommit=aaaaaaa\ndeployed_at=2026-01-01T00:00:00Z")
        self.given("up_results", "1\n0")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("up2.tag").strip(), "1.0.0-aaaaaaa", result.stderr)
        self.assertIn('yggdrasil.version: "1.0.0"', self.read("up2.labels"))

    def test_given_only_other_images_running_when_the_release_fails_then_there_is_nothing_to_roll_back_to(self):
        self.given("other_images", "postgres:16")
        self.given("ps", "d1 postgres:16")
        self.given("up_results", "1")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("ups").strip(), "1", result.stderr)

    def test_given_a_first_deploy_that_fails_when_deployed_then_there_is_no_rollback(self):
        self.given("up_results", "1")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("ups").strip(), "1")
        self.assertNotIn("rolling back", result.stderr)

    @unittest.skipUnless(shutil.which("flock"), "needs flock (util-linux)")
    def test_given_a_deploy_when_it_runs_then_it_holds_the_lock_on_the_env_file(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        # 75: another process could not take the lock while compose up ran.
        self.assertEqual(self.read("up1.lock").strip(), "75")

    def test_given_an_unreadable_env_file_when_deployed_then_it_stops_before_docker(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        self.env_file.chmod(0)
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or unreadable env file", result.stderr)
        self.assertEqual(self.read("calls"), "")


if __name__ == "__main__":
    unittest.main()
