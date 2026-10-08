"""Tests for scripts/deploy.sh, against a fake `docker` on PATH.   python3 -m unittest discover -s scripts

The fake records every call and answers from files in its state directory, so these run anywhere
bash and git do, without a Docker engine. They use heimdall-ui from the repository's own
catalog.yaml: in local (ports mode, always on) and in development (proxy mode, on demand).
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts" / "deploy.sh"
STACK = "heimdall-ui"
ENVIRONMENT = "local"
ON_DEMAND = "development"

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
            flock --nonblock --conflict-exit-code 75 "$YGG_SECRETS_DIR/locks/$PROJECT_UNDER_TEST.lock" true || status=$?
            echo "$status" >"$state/up$count.lock"
          fi
          exit "$(sed -n "${count}p" "$state/up_results" 2>/dev/null | grep . || echo 0)"
          ;;
      esac
      exit 0
    fi

    case "$1" in
      ps)
        # $state/ps: "<id> <image> [<state>]", newest first; the state defaults to running. Without
        # --all, only running containers, as docker ps does.
        format=${*: -1}
        all=""
        [[ " $* " == *" --all "* || " $* " == *" -a "* ]] && all=1
        while read -r id image container_state; do
          [[ -n "$all" || "${container_state:-running}" == running ]] || continue
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
      image)
        # $state/images: "<created><TAB><repository>:<tag>". image ls <repository> answers with
        # "<created><TAB><tag>", the format deploy.sh asks for.
        if [[ "$2" == ls ]]; then
          while IFS=$'\t' read -r created image; do
            [[ "${image%%:*}" == "$3" ]] && printf '%s\t%s\n' "$created" "${image#*:}"
          done <"$state/images" 2>/dev/null || true
        fi
        ;;
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
        for environment in (ENVIRONMENT, ON_DEMAND):
            (secrets / environment).mkdir(parents=True)
            (secrets / environment / f"{STACK}.env").write_text("HEIMDALL_API_BASE_URL=http://localhost:8080\n")
        self.env_file = secrets / ENVIRONMENT / f"{STACK}.env"

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

    def deploy(self, version=None, environment=ENVIRONMENT, **env):
        version = version or f"2.0.0-{self.commit}"
        return subprocess.run(["bash", str(DEPLOY), environment, STACK, str(self.app), version],
                              env=dict(self.env, PROJECT_UNDER_TEST=f"{STACK}-{environment}", **env), capture_output=True, text=True)

    def compose_calls(self, command):
        return [line for line in self.read("calls").splitlines()
                if line.startswith("compose ") and f" {command}" in line.split(" IMAGE_TAG=")[0]]

    def test_given_a_healthy_release_when_deployed_then_labelled_and_not_rolled_back(self):
        self.given("ps", "c1 heimdall-ui:local-1.0.0-aaaaaaa")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("ups").strip(), "1")
        self.assertEqual(self.read("up1.tag").strip(), f"local-2.0.0-{self.commit}")
        labels = self.read("up1.labels")
        self.assertIn('yggdrasil.environment: "local"', labels)
        self.assertIn('yggdrasil.version: "2.0.0"', labels)
        self.assertIn(f'yggdrasil.commit: "{self.commit}"', labels)

    def test_given_an_environment_when_deployed_then_the_project_is_named_after_the_stack_and_the_environment(self):
        result = self.deploy(environment=ON_DEMAND, DEPLOY_START="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        compose = [line for line in self.read("calls").splitlines() if line.startswith("compose ")]
        self.assertTrue(compose)
        for line in compose:
            self.assertIn(f"--project-name {STACK}-{ON_DEMAND} ", line)
        self.assertIn(f"-f {ROOT}/stacks/{STACK}.proxy.yml", compose[0])
        self.assertEqual(self.read("up1.tag").strip(), f"development-2.0.0-{self.commit}")
        ps = [line for line in self.read("calls").splitlines() if line.startswith("ps ")]
        self.assertTrue(all(f"label=com.docker.compose.project={STACK}-{ON_DEMAND}" in line for line in ps), ps)

    def test_given_an_unhealthy_release_when_deployed_then_it_rolls_back_to_the_previous_image_and_labels(self):
        self.given("ps", "c1 heimdall-ui:local-1.0.0-aaaaaaa")
        self.given("labels.c1", "version=1.0.0\ncommit=aaaaaaa\ndeployed_at=2026-01-01T00:00:00Z")
        self.given("up_results", "1\n0")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn("rolling back to local-1.0.0-aaaaaaa", result.stderr)
        self.assertEqual(self.read("up2.tag").strip(), "local-1.0.0-aaaaaaa")
        labels = self.read("up2.labels")
        self.assertIn('yggdrasil.version: "1.0.0"', labels)
        self.assertIn('yggdrasil.commit: "aaaaaaa"', labels)
        self.assertIn('yggdrasil.deployed_at: "2026-01-01T00:00:00Z"', labels)

    def test_given_a_stack_that_also_runs_a_database_when_rolling_back_then_the_application_image_is_the_target(self):
        # docker ps lists the newest container first, and here that is the database's: its tag ("16")
        # is not a version of the application.
        self.given("services", "db\nui")
        self.given("other_images", "postgres:16")
        self.given("ps", "d1 postgres:16\nc1 heimdall-ui:local-1.0.0-aaaaaaa")
        self.given("labels.c1", "version=1.0.0\ncommit=aaaaaaa\ndeployed_at=2026-01-01T00:00:00Z")
        self.given("up_results", "1\n0")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("up2.tag").strip(), "local-1.0.0-aaaaaaa", result.stderr)
        self.assertIn('yggdrasil.version: "1.0.0"', self.read("up2.labels"))

    def test_given_only_other_images_running_when_the_release_fails_then_there_is_nothing_to_roll_back_to(self):
        self.given("other_images", "postgres:16")
        self.given("ps", "d1 postgres:16")
        self.given("up_results", "1")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("ups").strip(), "1", result.stderr)

    def test_given_a_stopped_previous_deployment_when_the_release_fails_then_it_still_rolls_back_to_it(self):
        self.given("ps", "c1 heimdall-ui:local-1.0.0-aaaaaaa exited")
        self.given("labels.c1", "version=1.0.0\ncommit=aaaaaaa\ndeployed_at=2026-01-01T00:00:00Z")
        self.given("up_results", "1\n0")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("up2.tag").strip(), "local-1.0.0-aaaaaaa", result.stderr)

    def test_given_an_image_without_this_environments_tag_when_the_release_fails_then_it_is_not_a_rollback_target(self):
        # A container left from before tags carried the environment (or by a deploy by hand to
        # another tag scheme): not this environment's image, so nothing to come back to.
        self.given("ps", "c1 heimdall-ui:1.0.0-aaaaaaa")
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

    # On demand: development has onDemand: true in the catalog, local has not.

    def test_given_an_on_demand_environment_that_was_stopped_when_deployed_then_it_is_stopped_again(self):
        self.given("ps", "c1 heimdall-ui:development-1.0.0-aaaaaaa exited")
        result = self.deploy(environment=ON_DEMAND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("ups").strip(), "1")
        self.assertEqual(len(self.compose_calls("stop")), 1, self.read("calls"))
        self.assertIn("deploy: development is on demand and heimdall-ui was not running: stopped it again "
                      "(scripts/ygg.sh env start development to use it)", result.stdout)

    def test_given_an_on_demand_environment_first_deploy_when_deployed_then_it_is_stopped_after_becoming_healthy(self):
        result = self.deploy(environment=ON_DEMAND)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.read("calls")
        self.assertLess(calls.index(" up --detach"), calls.index(" stop "), calls)

    def test_given_an_on_demand_environment_that_was_running_when_deployed_then_it_keeps_running(self):
        self.given("ps", "c1 heimdall-ui:development-1.0.0-aaaaaaa")
        result = self.deploy(environment=ON_DEMAND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_calls("stop"), [])

    def test_given_deploy_start_when_an_on_demand_environment_was_stopped_then_it_is_left_running(self):
        self.given("ps", "c1 heimdall-ui:development-1.0.0-aaaaaaa exited")
        result = self.deploy(environment=ON_DEMAND, DEPLOY_START="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_calls("stop"), [])

    def test_given_an_environment_that_is_not_on_demand_when_it_was_stopped_then_it_is_left_running(self):
        self.given("ps", "c1 heimdall-ui:local-1.0.0-aaaaaaa exited")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_calls("stop"), [])

    def test_given_an_on_demand_environment_that_was_stopped_when_the_release_fails_then_it_rolls_back_and_stops(self):
        self.given("ps", "c1 heimdall-ui:development-1.0.0-aaaaaaa exited")
        self.given("labels.c1", "version=1.0.0\ncommit=aaaaaaa\ndeployed_at=2026-01-01T00:00:00Z")
        self.given("up_results", "1\n0")
        result = self.deploy(environment=ON_DEMAND)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read("up2.tag").strip(), "development-1.0.0-aaaaaaa", result.stderr)
        self.assertEqual(len(self.compose_calls("stop")), 1, self.read("calls"))

    def test_given_an_on_demand_environment_first_deploy_that_fails_when_deployed_then_it_is_stopped(self):
        self.given("up_results", "1")
        result = self.deploy(environment=ON_DEMAND)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("rolling back", result.stderr)
        self.assertEqual(len(self.compose_calls("stop")), 1, self.read("calls"))

    def test_given_images_of_several_environments_when_pruning_then_only_this_environments_old_ones_go(self):
        self.given("images", "\n".join([
            "2026-01-05\theimdall-ui:local-5.0.0-eeeeeee",
            "2026-01-04\theimdall-ui:development-4.0.0-ddddddd",
            "2026-01-03\theimdall-ui:local-3.0.0-ccccccc",
            "2026-01-02\theimdall-ui:local-2.0.0-bbbbbbb",
            "2026-01-01\theimdall-ui:development-1.0.0-aaaaaaa",
            "2026-01-01\theimdall-ui:local-1.0.0-aaaaaaa",
        ]))
        result = self.deploy(DEPLOY_KEEP_IMAGES="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        removed = [line.split(" IMAGE_TAG=")[0] for line in self.read("calls").splitlines() if line.startswith("image rm ")]
        self.assertEqual(removed, ["image rm heimdall-ui:local-2.0.0-bbbbbbb heimdall-ui:local-1.0.0-aaaaaaa"])

    @unittest.skipUnless(shutil.which("flock"), "needs flock (util-linux)")
    def test_given_a_deploy_when_it_runs_then_it_holds_the_project_lock(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        # 75: another process could not take the lock while compose up ran.
        self.assertEqual(self.read("up1.lock").strip(), "75")

    @unittest.skipUnless(shutil.which("flock"), "needs flock (util-linux)")
    def test_given_an_existing_lock_file_when_deployed_then_it_is_locked_without_being_written(self):
        lock = pathlib.Path(self.env["YGG_SECRETS_DIR"]) / "locks" / f"{STACK}-{ENVIRONMENT}.lock"
        lock.parent.mkdir()
        lock.touch(mode=0o444)
        os.utime(lock, (1_000_000_000, 1_000_000_000))
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cannot", result.stderr)
        self.assertEqual(lock.stat().st_mtime, 1_000_000_000)
        self.assertEqual(self.read("up1.lock").strip(), "75")

    def use_store(self, values):
        """Moves this test's env files into a variables store holding `values` for every environment."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import vars as v
        secrets = pathlib.Path(self.env["YGG_SECRETS_DIR"])
        v.init(secrets, confirm=lambda prompt: "saved")
        with v.Store.open(secrets) as store:
            for environment in (ENVIRONMENT, ON_DEMAND):
                for key, value in values.items():
                    store.set(f"app:{STACK}@{environment}", key, value, None, "set")
                (secrets / environment / f"{STACK}.env").unlink()

    def test_given_a_store_when_deployed_then_compose_reads_the_rendered_file_and_it_is_removed(self):
        self.use_store({"HEIMDALL_API_BASE_URL": "https://heimdall.example.com"})
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        env_files = [line.split("--env-file ")[1].split(" ")[0] for line in self.read("calls").splitlines()
                     if "--env-file" in line]
        self.assertTrue(env_files)
        self.assertTrue(all(not pathlib.Path(p).exists() for p in env_files), env_files)
        self.assertNotIn("move to the variables store", result.stderr)

    def test_given_a_store_when_the_deploy_fails_then_the_rendered_file_is_removed_too(self):
        self.use_store({"HEIMDALL_API_BASE_URL": "https://heimdall.example.com"})
        self.given("up_results", "1")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        env_files = {line.split("--env-file ")[1].split(" ")[0] for line in self.read("calls").splitlines()
                     if "--env-file" in line}
        self.assertTrue(env_files)
        self.assertTrue(all(not pathlib.Path(p).exists() for p in env_files), env_files)

    def test_given_a_store_without_variables_for_the_application_when_deployed_then_it_stops_before_docker(self):
        self.use_store({})
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{STACK} has no variables in {ENVIRONMENT}", result.stderr)
        self.assertIn(f"scripts/ygg.sh config {STACK} {ENVIRONMENT}", result.stderr)
        self.assertNotIn("compose", self.read("calls"))

    def test_given_a_store_with_a_broken_reference_when_deployed_then_it_stops_before_docker(self):
        self.use_store({"X": "${ref:heimdall-api:NOPE}"})
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("heimdall-api:NOPE", result.stderr + result.stdout)
        self.assertNotIn("compose", self.read("calls"))

    def test_given_no_store_when_deployed_then_the_env_file_is_used_with_a_notice(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("move to the variables store", result.stderr)
        self.assertIn(f"--env-file {self.env_file}", self.read("calls"))

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
