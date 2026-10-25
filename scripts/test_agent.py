"""Tests for scripts/agent.sh's checks, against a fake `docker` on PATH, and for the build agent's
Compose file.

    python3 -m unittest discover -s scripts
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

SCRIPT = pathlib.Path(__file__).resolve().parent / "agent.sh"
COMPOSE_FILE = pathlib.Path(__file__).resolve().parent.parent / "platform" / "build-agent.yml"


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
                          'if [[ "${1:-}" == info ]]; then\n'
                          '  echo "${FAKE_DOCKER_INFO:-$FAKE_ROOTLESS_INFO}"\n'
                          'fi\n')
        docker.chmod(0o755)
        self.log = self.temp / "docker.log"
        self.secrets = self.temp / "secrets"
        self.secrets.mkdir()
        self.workdir = self.temp / "agent"
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        YGG_SECRETS_DIR=str(self.secrets), FAKE_DOCKER_LOG=str(self.log),
                        FAKE_ROOTLESS_INFO='["name=seccomp","name=rootless"]',
                        DOCKER_HOST="unix:///run/user/1001/docker.sock")

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


if __name__ == "__main__":
    unittest.main()
