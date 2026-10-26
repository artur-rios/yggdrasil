"""Tests for scripts/run-check.sh, against a fake `docker` on PATH.   python3 -m unittest discover -s scripts

The fake docker writes one argument per line to $FAKE_DOCKER_LOG and exits with $FAKE_DOCKER_EXIT (default 0),
the way `docker run` exits with its command's exit code. Nothing here needs a real engine: what is tested is
the `docker run` the script builds, which is what decides where the workspace and the credentials end up.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "run-check.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >"$FAKE_DOCKER_LOG"
exit "${FAKE_DOCKER_EXIT:-0}"
"""

# The agent image may not have iproute2, so the script falls back to the kernel's route table; the fake
# `ip` stands in for an image that does.
FAKE_IP = """#!/usr/bin/env bash
echo "default via 172.17.0.1 dev eth0"
echo "172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2"
"""

# An `ip` that finds no default route.
NO_ROUTE_IP = "#!/usr/bin/env bash\ntrue\n"

# What /proc/net/route holds for a default route via 192.168.65.1 (the gateway is little-endian hex).
ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0141A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t0041A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class RunCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="run-check-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.log"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake("docker", FAKE_DOCKER)
        # The route table of the machine running the tests must not leak into the gateway the script finds.
        self.route_table = self.root / "route"
        self.route_table.write_text(ROUTE_TABLE)
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                        FAKE_DOCKER_LOG=str(self.log), YGG_ROUTE_TABLE=str(self.route_table))
        for name in ("YGG_DOCKER_SOCKET", "TESTCONTAINERS_HOST_OVERRIDE"):
            self.env.pop(name, None)

    def fake(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def run_check(self, *arguments, **env):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def docker_arguments(self):
        return self.log.read_text().splitlines()

    def pairs(self):
        arguments = self.docker_arguments()
        return [arguments[i:i + 2] for i in range(len(arguments) - 1)]

    def test_given_a_check_when_run_then_the_workspace_is_mounted_at_the_same_path(self):
        # The agent's `docker run` is executed by the host's engine, so the path inside the container must be
        # the one the agent checked out into, or the check would see an empty directory.
        result = self.run_check("unit", "node:22", str(self.workspace), "--", "npm", "test")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_arguments()
        self.assertEqual(arguments[:2], ["run", "--rm"])
        self.assertIn("--init", arguments)
        for pair in (["--pull", "missing"], ["--user", "0:0"], ["-w", str(self.workspace)],
                     ["-v", f"{self.workspace}:{self.workspace}"]):
            self.assertIn(pair, self.pairs(), pair)
        # The image, then the command with its own arguments, last.
        self.assertEqual(arguments[arguments.index("node:22"):], ["node:22", "npm", "test"])

    def test_given_a_command_with_flags_when_run_then_they_reach_the_container_untouched(self):
        # Everything after `--` belongs to the check, including words that look like this script's options.
        result = self.run_check("unit", "img", str(self.workspace), "--", "sh", "-c", "echo --docker")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_arguments()
        self.assertEqual(arguments[arguments.index("img"):], ["img", "sh", "-c", "echo --docker"])
        self.assertNotIn("/var/run/docker.sock", " ".join(arguments))

    def test_given_docker_when_run_then_the_socket_and_testcontainers_settings_are_passed(self):
        self.fake("ip", FAKE_IP)
        result = self.run_check("it", "dotnet:10", str(self.workspace), "--docker", "--", "dotnet", "test",
                                YGG_DOCKER_SOCKET="/run/user/1001/docker.sock")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_arguments()
        # The host path of the socket: the engine resolves -v sources on the host, not in the agent.
        self.assertIn(["-v", "/run/user/1001/docker.sock:/var/run/docker.sock"], self.pairs())
        for setting in ("DOCKER_HOST=unix:///var/run/docker.sock",
                        "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock",
                        "TESTCONTAINERS_HOST_OVERRIDE=172.17.0.1",
                        "TESTCONTAINERS_RYUK_DISABLED=true"):
            self.assertIn(["-e", setting], self.pairs(), setting)
        self.assertEqual(arguments[arguments.index("dotnet:10"):], ["dotnet:10", "dotnet", "test"])

    def test_given_ip_without_a_default_route_when_run_then_the_gateway_comes_from_the_route_table(self):
        # What the script does too when the image has no `ip` at all.
        self.fake("ip", NO_ROUTE_IP)
        result = self.run_check("it", "img", str(self.workspace), "--docker", "--", "true",
                                YGG_DOCKER_SOCKET="/run/docker.sock")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["-e", "TESTCONTAINERS_HOST_OVERRIDE=192.168.65.1"], self.pairs())

    def test_given_a_host_override_when_run_with_docker_then_it_is_passed_as_given(self):
        self.fake("ip", FAKE_IP)
        result = self.run_check("it", "img", str(self.workspace), "--docker", "--", "true",
                                YGG_DOCKER_SOCKET="/run/docker.sock", TESTCONTAINERS_HOST_OVERRIDE="10.9.8.7")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_arguments()
        self.assertIn("TESTCONTAINERS_HOST_OVERRIDE=10.9.8.7", arguments)
        self.assertNotIn("TESTCONTAINERS_HOST_OVERRIDE=172.17.0.1", arguments)

    def test_given_an_empty_host_override_when_run_with_docker_then_the_gateway_is_passed(self):
        # What an agent whose host has no address gets from build-agent.yml (${TESTCONTAINERS_HOST_OVERRIDE:-}):
        # empty is no override, and the check falls back to the gateway, as before the agents had one.
        self.fake("ip", FAKE_IP)
        result = self.run_check("it", "img", str(self.workspace), "--docker", "--", "true",
                                YGG_DOCKER_SOCKET="/run/docker.sock", TESTCONTAINERS_HOST_OVERRIDE="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["-e", "TESTCONTAINERS_HOST_OVERRIDE=172.17.0.1"], self.pairs())
        self.assertNotIn("TESTCONTAINERS_HOST_OVERRIDE=", self.docker_arguments())

    def test_given_no_gateway_when_run_with_docker_then_the_check_runs_without_the_override(self):
        self.fake("ip", NO_ROUTE_IP)
        self.route_table.write_text("Iface\tDestination\tGateway\n")
        result = self.run_check("it", "img", str(self.workspace), "--docker", "--", "true",
                                YGG_DOCKER_SOCKET="/run/docker.sock")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gateway", result.stderr)
        self.assertFalse([a for a in self.docker_arguments() if a.startswith("TESTCONTAINERS_HOST_OVERRIDE")])

    def test_given_no_docker_when_run_then_no_socket_is_mounted(self):
        result = self.run_check("unit", "img", str(self.workspace), "--", "true",
                                YGG_DOCKER_SOCKET="/run/docker.sock")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = "\n".join(self.docker_arguments())
        for fragment in ("docker.sock", "DOCKER_HOST", "TESTCONTAINERS"):
            self.assertNotIn(fragment, text)

    def test_given_docker_without_a_socket_path_when_run_then_it_is_a_usage_error(self):
        result = self.run_check("it", "img", str(self.workspace), "--docker", "--", "true")
        self.assertEqual(result.returncode, 2)
        self.assertIn("YGG_DOCKER_SOCKET", result.stderr)
        self.assertFalse(self.log.exists(), "docker must not run")

    def test_given_an_env_file_when_run_then_no_value_is_in_the_arguments(self):
        env_file = self.root / "check.env"
        env_file.write_text("NUGET_TOKEN=s3cr3t-value\n")
        result = self.run_check("pack", "img", str(self.workspace), "--env-file", str(env_file), "--", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_arguments()
        self.assertEqual(arguments[arguments.index("--env-file") + 1], str(env_file))
        self.assertNotIn("s3cr3t-value", "\n".join(arguments))
        # Nor is it echoed.
        self.assertNotIn("s3cr3t-value", result.stdout + result.stderr)

    def test_given_a_missing_env_file_when_run_then_it_is_a_usage_error(self):
        result = self.run_check("pack", "img", str(self.workspace), "--env-file", str(self.root / "gone"),
                                "--", "true")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists(), "docker must not run")

    # --print-host-override: what scripts/prove-machine.sh runs inside a container (with busybox's sh, which
    # has no bash) to find the address a published port is reached at, the way a --docker check would be told.
    def test_given_a_default_route_when_the_host_override_is_printed_then_it_is_the_gateway(self):
        self.fake("ip", FAKE_IP)
        result = self.run_check("--print-host-override")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "172.17.0.1\n")
        self.assertFalse(self.log.exists(), "docker must not run")

    def test_given_no_ip_when_the_host_override_is_printed_then_it_comes_from_the_route_table(self):
        self.fake("ip", NO_ROUTE_IP)
        result = self.run_check("--print-host-override")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "192.168.65.1\n")

    def test_given_an_override_in_the_environment_when_it_is_printed_then_that_one_wins(self):
        self.fake("ip", FAKE_IP)
        result = self.run_check("--print-host-override", TESTCONTAINERS_HOST_OVERRIDE="10.9.8.7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "10.9.8.7\n")

    def test_given_an_empty_override_when_it_is_printed_then_it_is_the_gateway(self):
        # prove-machine.sh passes -e TESTCONTAINERS_HOST_OVERRIDE= when the host has no address, as the agent does.
        self.fake("ip", FAKE_IP)
        result = self.run_check("--print-host-override", TESTCONTAINERS_HOST_OVERRIDE="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "172.17.0.1\n")

    def test_given_no_gateway_when_the_host_override_is_printed_then_it_is_empty_and_the_exit_is_zero(self):
        self.fake("ip", NO_ROUTE_IP)
        self.route_table.write_text("Iface\tDestination\tGateway\n")
        result = self.run_check("--print-host-override")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_given_a_plain_sh_when_the_host_override_is_printed_then_it_works_without_bash(self):
        # The container prove-machine.sh starts has busybox, not bash: the early part of the script is plain
        # sh. dash and busybox's ash are the strict ones; a machine that has neither skips this.
        shells = {"dash": ["dash"], "busybox": ["busybox", "sh"]}
        for shell, command in shells.items():
            if not shutil.which(command[0]):
                continue
            with self.subTest(shell=shell):
                self.fake("ip", NO_ROUTE_IP)
                result = subprocess.run([*command, str(SCRIPT), "--print-host-override"], env=self.env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                if shell == "dash":
                    self.assertEqual(result.stdout, "192.168.65.1\n")
                else:
                    # busybox runs its own `ip` applet before any fake `ip` on PATH, so the gateway is the
                    # real machine's: all that is proved is that the script ran, without bash, to an address.
                    self.assertRegex(result.stdout, r"^\d+\.\d+\.\d+\.\d+\n$")

    def test_given_a_failing_command_when_run_then_its_exit_code_is_returned(self):
        result = self.run_check("unit", "img", str(self.workspace), "--", "false", FAKE_DOCKER_EXIT="7")
        self.assertEqual(result.returncode, 7)

    def test_given_bad_arguments_when_run_then_it_is_a_usage_error(self):
        workspace = str(self.workspace)
        for arguments in ([],
                          ["unit", "img", workspace],                        # no --
                          ["unit", "img", workspace, "--"],                  # nothing to run
                          ["unit", "img", workspace, "--bogus", "--", "x"],  # unknown option
                          ["unit", "img", "relative/path", "--", "x"],       # the path must mean the same on both sides
                          ["unit", "img", str(self.root / "nope"), "--", "x"],
                          ["unit", "img", workspace, "--env-file", "--", "x"]):
            with self.subTest(arguments=arguments):
                result = self.run_check(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr)
                self.assertFalse(self.log.exists(), "docker must not run")


if __name__ == "__main__":
    unittest.main()
