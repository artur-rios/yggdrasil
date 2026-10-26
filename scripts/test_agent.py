"""Tests for scripts/agent.sh's checks, against a fake `docker` on PATH, and for the build agent's
Compose file.

    python3 -m unittest discover -s scripts
"""

import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml

SCRIPT = pathlib.Path(__file__).resolve().parent / "agent.sh"
ENGINE = pathlib.Path(__file__).resolve().parent / "docker-engine.sh"
COMPOSE_FILE = pathlib.Path(__file__).resolve().parent.parent / "platform" / "build-agent.yml"

# What `ip -4 route get 1.1.1.1` prints on a host whose route out is via a gateway: the host's own address
# is the `src`.
IP_ROUTE_GET = "1.1.1.1 via 203.0.113.1 dev eth0 src 203.0.113.10 uid 1001 \n    cache \n"


def fake_ip(bin_dir, output, status=0):
    """An `ip` that logs its arguments next to it and prints `output` (to stderr when it fails)."""
    path = bin_dir / "ip"
    stream = "" if status == 0 else " >&2"
    path.write_text("#!/usr/bin/env bash\n"
                    f'echo "ip $*" >>"{bin_dir}/ip.log"\n'
                    f"printf '%s' {shlex.quote(output)}{stream}\n"
                    f"exit {status}\n")
    path.chmod(0o755)


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="agent-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        # Logs what it is given and the engine agent.sh points Docker at; answers `docker info` as a
        # rootless engine unless a test says otherwise.
        docker.write_text('#!/usr/bin/env bash\n'
                          'echo "docker $*" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "DOCKER_SOCKET=${DOCKER_SOCKET:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "DOCKER_HOST=${DOCKER_HOST:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_AGENT_WORKDIR=${YGG_AGENT_WORKDIR:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "TESTCONTAINERS_HOST_OVERRIDE=${TESTCONTAINERS_HOST_OVERRIDE-<unset>}"'
                          ' >>"$FAKE_DOCKER_LOG"\n'
                          'if [[ "${1:-}" == info ]]; then\n'
                          '  echo "${FAKE_DOCKER_INFO:-$FAKE_ROOTLESS_INFO}"\n'
                          'fi\n')
        docker.chmod(0o755)
        # The host's route out, so the machine running the tests does not decide the agent's address.
        fake_ip(bin_dir, IP_ROUTE_GET)
        self.log = self.temp / "docker.log"
        self.secrets = self.temp / "secrets"
        self.secrets.mkdir()
        self.workdir = self.temp / "agent"
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        YGG_SECRETS_DIR=str(self.secrets), FAKE_DOCKER_LOG=str(self.log),
                        FAKE_ROOTLESS_INFO='["name=seccomp","name=rootless"]',
                        DOCKER_HOST="unix:///run/user/1001/docker.sock")
        self.env.pop("TESTCONTAINERS_HOST_OVERRIDE", None)
        self.bin = bin_dir

    def agent_env(self, **overrides):
        values = {"JENKINS_URL": "https://jenkins.example.com", "JENKINS_AGENT_NAME": "ci",
                  "JENKINS_AGENT_SECRET": "abc123", "YGG_AGENT_WORKDIR": str(self.workdir)}
        values.update(overrides)
        (self.secrets / "agent.env").write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))

    def run_agent(self, *arguments):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=self.env, capture_output=True, text=True)

    def docker_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_given_a_complete_agent_env_when_up_then_compose_runs_the_build_agent_project(self):
        self.agent_env()
        result = self.run_agent("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        compose_call = next(line for line in calls.splitlines() if line.startswith("docker compose"))
        self.assertIn("-p yggdrasil-agent-ci", compose_call)
        self.assertIn(f"-f {COMPOSE_FILE}", compose_call)
        self.assertIn(f"--env-file {self.secrets}/agent.env", compose_call)
        self.assertIn(" up ", compose_call + " ")
        self.assertIn("DOCKER_SOCKET=/run/user/1001/docker.sock", calls)
        self.assertIn("DOCKER_HOST=unix:///run/user/1001/docker.sock", calls)

    def test_given_a_complete_agent_env_when_up_then_the_workdir_is_created(self):
        self.agent_env()
        self.assertEqual(self.run_agent("up").returncode, 0)
        self.assertTrue(self.workdir.is_dir())

    def test_given_a_complete_agent_env_when_down_ps_or_logs_then_compose_runs_that_command(self):
        self.agent_env()
        for command in ("down", "ps", "logs"):
            self.log.unlink(missing_ok=True)
            result = self.run_agent(command)
            self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
            compose_call = next(line for line in self.docker_calls().splitlines()
                                if line.startswith("docker compose"))
            self.assertIn("-p yggdrasil-agent-ci", compose_call)
            self.assertIn(command, compose_call)

    def test_given_a_missing_secret_when_up_then_it_is_refused_naming_the_variable(self):
        for name in ("JENKINS_URL", "JENKINS_AGENT_NAME", "JENKINS_AGENT_SECRET", "YGG_AGENT_WORKDIR"):
            with self.subTest(name=name):
                self.log.unlink(missing_ok=True)
                self.agent_env(**{name: None})
                result = self.run_agent("up")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertNotIn("compose", self.docker_calls())

    def test_given_no_agent_env_when_up_then_it_is_refused_naming_the_file(self):
        result = self.run_agent("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{self.secrets}/agent.env", result.stderr)

    def test_given_an_unreadable_agent_env_when_up_then_it_is_refused_naming_the_file(self):
        (self.secrets / "agent.env").write_text("JENKINS_URL=https://x\nJENKINS_AGENT_NAME=(\n")
        result = self.run_agent("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read", result.stderr)
        self.assertIn(f"{self.secrets}/agent.env", result.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_given_the_env_template_when_copied_unedited_then_up_is_refused(self):
        template = COMPOSE_FILE.parent.parent / "env" / "agent.env.example"
        shutil.copy(template, self.secrets / "agent.env")
        result = self.run_agent("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be set", result.stderr)
        # The controller's address too is left empty, like the name and the workdir: an unedited copy
        # must not dial an example host.
        self.assertIn("JENKINS_URL", result.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_given_a_relative_workdir_when_up_then_it_is_refused(self):
        self.agent_env(YGG_AGENT_WORKDIR="agent")
        result = self.run_agent("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("YGG_AGENT_WORKDIR", result.stderr)
        self.assertIn("absolute", result.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_given_an_agent_name_that_is_no_project_name_when_up_then_it_is_refused(self):
        self.agent_env(JENKINS_AGENT_NAME="CI Agent")
        result = self.run_agent("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("JENKINS_AGENT_NAME", result.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_given_a_rootful_engine_when_up_then_it_is_refused(self):
        self.agent_env()
        self.env["FAKE_DOCKER_INFO"] = '["name=seccomp"]'
        result = self.run_agent("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runs as root", result.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_given_an_unknown_command_when_run_then_it_prints_the_usage(self):
        result = self.run_agent("restart")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr)

    # ---- TESTCONTAINERS_HOST_OVERRIDE: where a check's Testcontainers reaches a published port ----------

    def compose_override(self):
        """The TESTCONTAINERS_HOST_OVERRIDE in agent.sh's environment when it ran `docker compose`, which is
        what Compose interpolates into build-agent.yml."""
        lines = self.docker_calls().splitlines()
        compose = next(i for i, line in enumerate(lines) if line.startswith("docker compose"))
        values = [line for line in lines[compose:] if line.startswith("TESTCONTAINERS_HOST_OVERRIDE=")]
        return values[0].partition("=")[2]

    def test_given_no_override_in_agent_env_when_up_then_the_host_address_is_handed_to_compose(self):
        # The engines' slirp4netns port driver opens a published port on the host only: a check reaches it at
        # the host's own address, the `src` of its route out, not at the containers' gateway.
        self.agent_env()
        result = self.run_agent("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_override(), "203.0.113.10")
        self.assertIn("ip -4 route get 1.1.1.1", (self.bin / "ip.log").read_text())
        self.assertIn("203.0.113.10", result.stderr)

    def test_given_an_override_in_agent_env_when_up_then_it_wins_over_the_host_address(self):
        self.agent_env(TESTCONTAINERS_HOST_OVERRIDE="198.51.100.20")
        result = self.run_agent("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_override(), "198.51.100.20")

    def test_given_an_empty_override_in_agent_env_when_up_then_the_host_address_is_used(self):
        self.agent_env(TESTCONTAINERS_HOST_OVERRIDE="")
        result = self.run_agent("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_override(), "203.0.113.10")

    def test_given_one_in_the_callers_environment_when_up_then_only_agent_env_or_the_host_counts(self):
        self.agent_env()
        self.env["TESTCONTAINERS_HOST_OVERRIDE"] = "192.0.2.99"
        result = self.run_agent("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_override(), "203.0.113.10")

    def test_given_no_route_out_of_the_host_when_up_then_the_override_is_empty_and_the_agent_still_starts(self):
        # Empty, not missing: build-agent.yml passes it as ${TESTCONTAINERS_HOST_OVERRIDE:-}, and run-check.sh's
        # ${TESTCONTAINERS_HOST_OVERRIDE:-<gateway>} then falls back to the gateway, as before.
        self.agent_env()
        fake_ip(self.bin, "RTNETLINK answers: Network is unreachable\n", status=2)
        result = self.run_agent("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_override(), "")
        self.assertIn("gateway", result.stderr)


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class HostPublishedAddressTests(unittest.TestCase):
    """docker-engine.sh's host_published_address, which agent.sh and prove-machine.sh share, against a fake `ip`."""

    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="host-address-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")

    def address(self, env=None):
        # Under the scripts' own options, so a failing `ip` must not stop the caller.
        return subprocess.run([shutil.which("bash"), "-c", 'set -euo pipefail; source "$1"; host_published_address; echo "end"',
                               "bash", str(ENGINE)], env=env or self.env, capture_output=True, text=True,
                              timeout=30)

    def test_given_a_route_via_a_gateway_when_asked_then_it_is_the_source_address(self):
        fake_ip(self.bin, IP_ROUTE_GET)
        result = self.address()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "203.0.113.10\nend\n")
        self.assertEqual((self.bin / "ip.log").read_text(), "ip -4 route get 1.1.1.1\n")

    def test_given_a_direct_route_when_asked_then_it_is_the_source_address(self):
        # A host on the internet's own link has no `via`.
        fake_ip(self.bin, "1.1.1.1 dev eth0 src 200.141.30.178 uid 0 \n    cache \n")
        self.assertEqual(self.address().stdout, "200.141.30.178\nend\n")

    def test_given_no_route_when_asked_then_it_prints_nothing_and_succeeds(self):
        fake_ip(self.bin, "RTNETLINK answers: Network is unreachable\n", status=2)
        result = self.address()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "end\n")

    def test_given_a_route_without_a_source_when_asked_then_it_prints_nothing(self):
        for output in ("1.1.1.1 via 203.0.113.1 dev eth0 uid 1001\n", "1.1.1.1 dev eth0 src\n",
                       "1.1.1.1 dev eth0 src fe80::1 uid 0\n", ""):
            with self.subTest(output=output):
                fake_ip(self.bin, output)
                result = self.address()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "end\n")

    def test_given_no_ip_command_when_asked_then_it_prints_nothing_and_succeeds(self):
        # A PATH with only what the function needs besides `ip`.
        awk = shutil.which("awk")
        self.assertIsNotNone(awk)
        (self.bin / "awk").symlink_to(awk)
        result = self.address(env=dict(self.env, PATH=str(self.bin)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "end\n")


class BuildAgentFileTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load(COMPOSE_FILE.read_text())

    def test_given_the_build_agent_file_when_read_then_the_workdir_is_mounted_at_the_same_path(self):
        # A check's `docker run -v $WORKSPACE:...` goes to the host's engine, so the path must mean
        # the same directory inside the agent and on the host.
        volumes = self.compose["services"]["agent"]["volumes"]
        self.assertIn("${YGG_AGENT_WORKDIR}:${YGG_AGENT_WORKDIR}", volumes)
        self.assertEqual(self.compose["services"]["agent"]["environment"]["JENKINS_AGENT_WORKDIR"],
                         "${YGG_AGENT_WORKDIR}")

    def test_given_the_build_agent_file_when_read_then_no_service_publishes_a_port(self):
        for name, service in self.compose["services"].items():
            self.assertNotIn("ports", service, name)

    def test_given_the_build_agent_file_when_read_then_the_engine_socket_is_mounted(self):
        volumes = self.compose["services"]["agent"]["volumes"]
        self.assertIn("${DOCKER_SOCKET}:/var/run/docker.sock", volumes)

    def test_given_the_build_agent_file_when_read_then_the_host_socket_path_is_in_the_environment(self):
        # run-check.sh mounts the socket into a check's container with `docker run -v`, which the host's
        # engine resolves on the host: the agent has to know the socket's host path, not just mount it.
        environment = self.compose["services"]["agent"]["environment"]
        self.assertEqual(environment["YGG_DOCKER_SOCKET"], "${DOCKER_SOCKET}")

    def test_given_the_build_agent_file_when_read_then_the_host_override_is_in_the_environment(self):
        # From agent.sh, so run-check.sh (a step of the agent) has it to hand a --docker check. `:-`: an empty
        # or unset one is an empty value, which run-check.sh replaces with the gateway, never a Compose warning.
        environment = self.compose["services"]["agent"]["environment"]
        self.assertEqual(environment["TESTCONTAINERS_HOST_OVERRIDE"], "${TESTCONTAINERS_HOST_OVERRIDE:-}")


if __name__ == "__main__":
    unittest.main()
