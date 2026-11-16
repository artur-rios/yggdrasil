"""Tests for scripts/run-check.sh, against a fake `docker` on PATH.   python3 -m unittest discover -s scripts

The fake docker writes one argument per line to $FAKE_DOCKER_LOG and exits with $FAKE_DOCKER_EXIT (default 0),
the way `docker run` exits with its command's exit code. Nothing here needs a real engine: what is tested is
the `docker run` the script builds, which is what decides where the workspace and the credentials end up.
"""

import hashlib
import os
import pathlib
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
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
class RunCheckHarness(unittest.TestCase):
    """The fake docker and the helpers; the tests are in the classes below."""

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


class RunCheckTests(RunCheckHarness):
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


# What a check's container is given with --toolchains: the cached SDK as an overlay at FLUTTER_IN_CONTAINER.
SDK_IN_CONTAINER = "/opt/yggdrasil/flutter"
VERSION = "3.50.0"
OTHER_VERSION = "3.51.0"


def flutter_archive(path, marker="the pinned release"):
    """A tar.xz shaped like Flutter's release archive (flutter/bin/flutter, executable). Returns its SHA-256."""
    with tempfile.TemporaryDirectory() as staging:
        bin_dir = pathlib.Path(staging) / "flutter" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "flutter").write_text(f"#!/bin/sh\necho {marker}\n")
        (bin_dir / "flutter").chmod(0o755)
        with tarfile.open(path, "w:xz") as archive:
            archive.add(pathlib.Path(staging) / "flutter", arcname="flutter")
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("xz") and shutil.which("flock"), "needs xz and flock, as the ci agent has")
class ToolchainCacheTests(RunCheckHarness):
    """--toolchains: the pinned Flutter SDK fetched into the cache once and given to each check as an overlay.

    Flutter's archive is a small one served from a file:// URL (YGG_FLUTTER_ARCHIVE_URL, a format with %s for the
    version), so curl, sha256sum, tar and flock are the real ones: only docker is a fake.
    """

    def setUp(self):
        super().setUp()
        self.cache = self.root / "toolchains"
        self.cache.mkdir()
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.sum = flutter_archive(self.archives / f"flutter_linux_{VERSION}-stable.tar.xz")
        self.env["YGG_FLUTTER_ARCHIVE_URL"] = f"file://{self.archives}/flutter_linux_%s-stable.tar.xz"
        self.pin(VERSION, self.sum)

    def pin(self, version, checksum, file="ci/flutter-env.sh", checkout=None):
        """Writes the two pin lines the way the repositories' ci/flutter-env.sh has them."""
        path = (checkout or self.workspace) / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# shellcheck shell=bash\n# renovate: datasource=github-tags depName=flutter/flutter\n"
                        f"FLUTTER_VERSION={version}\nFLUTTER_SHA256={checksum}\n\n"
                        "FLUTTER_SDK_DIR=${FLUTTER_SDK_DIR:-${TMPDIR:-/tmp}/flutter-$FLUTTER_VERSION}\n")

    def other_release(self):
        """Another release in Flutter's archive; returns its SHA-256."""
        return flutter_archive(self.archives / f"flutter_linux_{OTHER_VERSION}-stable.tar.xz", marker="another")

    def run_with_cache(self, *options, **env):
        return self.run_check("unit", "img", str(self.workspace), "--toolchains", str(self.cache), *options,
                              "--", "sh", "-c", "ci/test.sh", **env)

    def sdk(self, version=VERSION, checksum=None):
        return self.cache / "flutter" / f"{version}-{checksum or self.sum}"

    def cached(self):
        """The SDK directories in the cache (not the lock, the stamps or a fetch in progress)."""
        flutter = self.cache / "flutter"
        return sorted(p.name for p in flutter.iterdir() if p.is_dir() and not p.name.startswith(".")) \
            if flutter.exists() else []

    def overlay(self):
        """The overlay options of the --mount run-check.sh gave docker, as a dict, or None without one."""
        arguments = self.docker_arguments()
        if "--mount" not in arguments:
            return None
        field = arguments[arguments.index("--mount") + 1]
        options = field[field.index('"volume-opt=o=') + len('"volume-opt=o='):-1]
        # A flag (userxattr) is an option without a value.
        return dict((option.split("=", 1) + [True])[:2] for option in options.split(","))

    def test_given_a_pinned_flutter_and_an_empty_cache_when_run_then_the_sdk_is_fetched_and_given_as_an_overlay(self):
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.sdk() / "bin" / "flutter").is_file())
        arguments = self.docker_arguments()
        field = arguments[arguments.index("--mount") + 1]
        self.assertTrue(field.startswith(f"type=volume,dst={SDK_IN_CONTAINER},volume-driver=local,"
                                         "volume-opt=type=overlay,volume-opt=device=overlay,"), field)
        overlay = self.overlay()
        # The cached SDK is only ever the overlay's lower, read-only, layer: the check writes to its own directory.
        self.assertEqual(overlay["lowerdir"], str(self.sdk()))
        # userxattr: without it an overlay in a user namespace (a rootless engine) cannot delete a lower directory.
        self.assertIs(overlay["userxattr"], True)
        self.assertTrue(overlay["upperdir"].startswith(f"{self.cache}/runs/"), overlay)
        self.assertEqual(pathlib.Path(overlay["workdir"]).parent, pathlib.Path(overlay["upperdir"]).parent)
        self.assertIn(["-e", f"FLUTTER_SDK_DIR={SDK_IN_CONTAINER}"], self.pairs())
        self.assertNotIn(f"{self.sdk()}:", "\n".join(arguments), "the cache itself is never bind-mounted")
        self.assertEqual(arguments[arguments.index("img"):], ["img", "sh", "-c", "ci/test.sh"])
        self.assertIn(f"Flutter {VERSION} from the toolchain cache", result.stderr)

    def test_given_a_check_that_ended_when_run_with_an_overlay_then_its_run_directory_is_gone(self):
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(pathlib.Path(self.overlay()["upperdir"]).parent.exists())
        self.assertEqual(list((self.cache / "runs").iterdir()), [])
        self.assertFalse([p for p in (self.cache / "flutter").iterdir() if p.name.startswith(".incoming")])
        self.assertTrue(pathlib.Path(f"{self.sdk()}.used").is_file())

    def test_given_an_overlay_that_left_its_work_directory_closed_when_the_check_ends_then_it_is_still_removed(self):
        # The kernel leaves <workdir>/work with mode 000; the ci user, its owner, can't descend it as it is.
        # set -e: a fake that could not make the directory would fail the run, not pass the test for nothing.
        self.fake("docker", "#!/usr/bin/env bash\nset -e\n"
                            "printf '%s\\n' \"$@\" >\"$FAKE_DOCKER_LOG\"\n"
                            "for a in \"$@\"; do\n"
                            "  case $a in type=volume,*) w=${a#*workdir=}; w=${w%%,*}; w=${w%\\\"}; u=${a#*upperdir=}; u=${u%%,*} ;; esac\n"
                            "done\n"
                            "echo written >\"$u/lockfile\"; mkdir \"$w/work\"; touch \"$w/work/index\"; chmod 000 \"$w/work\"\n")
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.cache / "runs").iterdir()), [])

    def test_given_a_run_directory_a_killed_agent_left_when_a_check_runs_then_it_is_removed_after_a_day(self):
        self.assertEqual(self.run_with_cache().returncode, 0)
        old, recent = self.cache / "runs" / "oldrun01", self.cache / "runs" / "newrun01"
        for run in (old, recent):
            (run / "upper").mkdir(parents=True)
            (run / "work" / "work").mkdir(parents=True)
            (run / "work" / "work" / "index").touch()
            (run / "work" / "work").chmod(0)
        self.addCleanup(lambda: recent.exists() and (recent / "work" / "work").chmod(0o700))
        two_days_ago = time.time() - 2 * 86400
        os.utime(old, (two_days_ago, two_days_ago))
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists(), "a run directory of a check that may still run is kept")

    def test_given_a_failing_check_when_run_with_an_overlay_then_its_exit_code_is_returned(self):
        result = self.run_with_cache(FAKE_DOCKER_EXIT="3")
        self.assertEqual(result.returncode, 3)
        self.assertIsNotNone(self.overlay())
        self.assertEqual(list((self.cache / "runs").iterdir()), [])

    def test_given_a_cached_sdk_when_run_again_then_nothing_is_downloaded(self):
        self.assertEqual(self.run_with_cache().returncode, 0)
        # Without the archive a second fetch would fail, and the check would run without the overlay.
        shutil.rmtree(self.archives)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overlay()["lowerdir"], str(self.sdk()))
        self.assertNotIn("fetching", result.stderr)

    def test_given_an_archive_that_is_not_the_pinned_one_when_run_then_nothing_is_cached_and_the_check_fetches_its_own(self):
        self.pin(VERSION, "0" * 64)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())
        self.assertNotIn("FLUTTER_SDK_DIR", "\n".join(self.docker_arguments()))
        self.assertEqual(self.cached(), [])
        self.assertIn("not the pinned", result.stderr)

    def test_given_an_archive_that_cannot_be_downloaded_when_run_then_the_check_fetches_its_own(self):
        shutil.rmtree(self.archives)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())
        self.assertEqual(self.cached(), [])
        self.assertIn("could not download", result.stderr)

    def test_given_a_branch_pinning_another_release_when_run_then_the_sdk_other_repositories_use_is_untouched(self):
        # Every repository pinning a release gets the same SDK. A branch that pins another one gets a directory
        # of its own, fetched only from Flutter's archive and kept only when its checksum is the pinned one: never
        # a different SDK under the name another pin uses.
        self.assertEqual(self.run_with_cache().returncode, 0)
        before = (self.sdk() / "bin" / "flutter").read_bytes()
        other = self.other_release()
        self.pin(OTHER_VERSION, other)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overlay()["lowerdir"], str(self.sdk(OTHER_VERSION, other)))
        self.assertEqual((self.sdk() / "bin" / "flutter").read_bytes(), before)
        self.assertEqual(self.cached(), sorted([self.sdk().name, self.sdk(OTHER_VERSION, other).name]))

    def test_given_a_branch_pinning_a_known_release_with_another_checksum_when_run_then_nothing_is_cached(self):
        # The checksum, not the version, decides: a pin of the cached version with a made-up checksum names no
        # SDK, and its fetch fails the checksum, so the check fetches its own (and fails it the same way).
        self.assertEqual(self.run_with_cache().returncode, 0)
        self.pin(VERSION, "f" * 64)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())
        self.assertEqual(self.cached(), [self.sdk().name])

    def test_given_the_pin_in_ci_pins_sh_when_run_then_it_is_used(self):
        # alexandria-ui keeps its pins in ci/pins.sh, which its ci/flutter-env.sh sources.
        (self.workspace / "ci" / "flutter-env.sh").write_text(". ci/pins.sh\n")
        self.pin(VERSION, self.sum, file="ci/pins.sh")
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overlay()["lowerdir"], str(self.sdk()))

    def test_given_a_quoted_pin_with_windows_line_ends_when_run_then_it_is_used(self):
        (self.workspace / "ci" / "flutter-env.sh").write_text(
            f'FLUTTER_VERSION="{VERSION}"\r\nFLUTTER_SHA256=\'{self.sum}\'\r\n')
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overlay()["lowerdir"], str(self.sdk()))

    def test_given_a_pin_that_is_not_plain_data_when_run_then_the_check_runs_without_the_cache(self):
        # The checkout is the branch's: its pin is read, never run, and anything but a plain value is no pin.
        marker = self.root / "ran"
        for text in (f"FLUTTER_VERSION=$(touch {marker}; echo {VERSION})\nFLUTTER_SHA256={self.sum}\n",
                     f"FLUTTER_VERSION=${{OTHER:-{VERSION}}}\nFLUTTER_SHA256={self.sum}\n",
                     f"FLUTTER_VERSION={VERSION}/../../x\nFLUTTER_SHA256={self.sum}\n",
                     f"FLUTTER_VERSION={VERSION}\nFLUTTER_SHA256={self.sum.upper()}\n",
                     f"FLUTTER_VERSION=\"{VERSION}'\nFLUTTER_SHA256={self.sum}\n",
                     f"FLUTTER_SHA256={self.sum}\n"):
            with self.subTest(text=text):
                (self.workspace / "ci" / "flutter-env.sh").write_text(text)
                result = self.run_with_cache()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.overlay())
                self.assertFalse(marker.exists())
                self.assertEqual(self.cached(), [])

    def test_given_two_files_pinning_different_releases_when_run_then_the_check_runs_without_the_cache(self):
        self.pin(OTHER_VERSION, self.sum, file="ci/pins.sh")
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())

    def test_given_a_pin_file_that_is_a_link_when_run_then_it_is_not_read(self):
        # A link could lead the agent to read a file outside the checkout.
        real = self.root / "elsewhere.sh"
        (self.workspace / "ci" / "flutter-env.sh").rename(real)
        (self.workspace / "ci" / "flutter-env.sh").symlink_to(real)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())

    def test_given_no_pin_when_run_then_the_check_runs_as_without_the_option(self):
        (self.workspace / "ci" / "flutter-env.sh").unlink()
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())
        self.assertNotIn("FLUTTER_SDK_DIR", "\n".join(self.docker_arguments()))

    def test_given_a_checkout_under_the_workspace_when_run_then_its_pin_is_read(self):
        # The branch policy mounts the whole workspace and runs in its app/.
        app = self.workspace / "app"
        (self.workspace / "ci" / "flutter-env.sh").unlink()
        self.pin(VERSION, self.sum, checkout=app)
        result = self.run_with_cache("--checkout", str(app))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overlay()["lowerdir"], str(self.sdk()))

    def test_given_a_cache_path_that_cannot_be_an_overlay_option_when_run_then_the_check_runs_without_it(self):
        odd = self.root / "tool,chains"
        odd.mkdir()
        result = self.run_check("unit", "img", str(self.workspace), "--toolchains", str(odd), "--", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())
        self.assertIn("running unit without it", result.stderr)

    def test_given_an_sdk_no_check_used_for_a_month_when_another_is_fetched_then_it_is_removed(self):
        self.assertEqual(self.run_with_cache().returncode, 0)
        old = self.sdk()
        month_ago = time.time() - 31 * 86400
        os.utime(f"{old}.used", (month_ago, month_ago))
        stale = self.cache / "flutter" / ".incoming.abcdef"
        stale.mkdir()
        two_days_ago = time.time() - 2 * 86400
        os.utime(stale, (two_days_ago, two_days_ago))
        other = self.other_release()
        self.pin(OTHER_VERSION, other)
        result = self.run_with_cache()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cached(), [self.sdk(OTHER_VERSION, other).name])
        self.assertFalse(pathlib.Path(f"{old}.used").exists())
        self.assertFalse(stale.exists())

    def test_given_an_sdk_used_this_month_when_another_is_fetched_then_it_stays(self):
        self.assertEqual(self.run_with_cache().returncode, 0)
        other = self.other_release()
        self.pin(OTHER_VERSION, other)
        self.assertEqual(self.run_with_cache().returncode, 0)
        self.assertIn(self.sdk().name, self.cached())

    def test_given_another_check_fetching_when_the_wait_runs_out_then_the_check_runs_without_the_cache(self):
        (self.cache / "flutter").mkdir()
        # In a session of its own, so the cleanup ends the sleep (which holds the lock) along with flock, and with no
        # output of ours to keep open.
        holder = subprocess.Popen(["flock", str(self.cache / "flutter" / ".lock"), "sleep", "30"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(holder.wait)
        self.addCleanup(lambda: holder.poll() is None and os.killpg(holder.pid, signal.SIGKILL))
        deadline = time.time() + 10
        # Taken once a non-blocking flock of the same file fails.
        while subprocess.run(["flock", "-n", str(self.cache / "flutter" / ".lock"), "true"]).returncode == 0:
            self.assertLess(time.time(), deadline, "the lock was never taken")
            time.sleep(0.05)
        result = self.run_with_cache(YGG_TOOLCHAIN_LOCK_WAIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.overlay())
        self.assertEqual(self.cached(), [])

    def test_given_a_stop_while_the_check_runs_when_run_with_an_overlay_then_docker_gets_it_and_the_run_directory_goes(self):
        # Jenkins stops a build with TERM: docker must still get it (it passes it on to the container), as when the
        # script exec'd docker, and the run directory must not stay behind.
        started = self.root / "started"
        self.fake("docker", "#!/usr/bin/env bash\n"
                            "printf '%s\\n' \"$@\" >\"$FAKE_DOCKER_LOG\"\n"
                            f"trap 'echo stopped >>\"{started}\"; exit 143' TERM\n"
                            f"echo running >\"{started}\"\n"
                            "sleep 30 &\n"
                            "wait\n")
        process = subprocess.Popen(["bash", str(SCRIPT), "unit", "img", str(self.workspace), "--toolchains",
                                    str(self.cache), "--", "true"], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(started.exists(), "docker never started")
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 143, stderr)
        self.assertIn("stopped", started.read_text())
        self.assertEqual(list((self.cache / "runs").iterdir()), [])

    def test_given_bad_toolchain_options_when_run_then_it_is_a_usage_error(self):
        workspace = str(self.workspace)
        for arguments in (["unit", "img", workspace, "--toolchains", "--", "x"],
                          ["unit", "img", workspace, "--checkout", workspace, "--", "x"],   # only with --toolchains
                          ["unit", "img", workspace, "--toolchains", str(self.cache), "--checkout", str(self.root),
                           "--", "x"],                                                     # outside the workspace
                          ["unit", "img", workspace, "--toolchains", str(self.cache), "--checkout",
                           f"{workspace}/../elsewhere", "--", "x"]):
            with self.subTest(arguments=arguments):
                result = self.run_check(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr)
                self.assertFalse(self.log.exists(), "docker must not run")


# A docker that answers `image inspect` with $FAKE_IMAGE_ENV (the image's Env, one per line) and, for `run`, logs
# its arguments and does what a check that downloads does: $FAKE_DOWNLOAD (a path) written into the upper directory
# of the overlay at /opt/yggdrasil/cache, $FAKE_NDK_DOWNLOAD into the one at .../ndk. Exits $FAKE_DOCKER_EXIT.
PACKAGE_DOCKER = r"""#!/usr/bin/env bash
if [[ "$1" == image ]]; then printf '%s\n' "${FAKE_IMAGE_ENV:-PATH=/usr/bin}"; exit 0; fi
if [[ "$1" == pull ]]; then exit 0; fi
printf '%s\n' "$@" >"$FAKE_DOCKER_LOG"
for argument in "$@"; do
  case $argument in
    type=volume,dst=/opt/yggdrasil/cache,*) packages=${argument#*upperdir=}; packages=${packages%%,*} ;;
    type=volume,dst=*/ndk,*) ndk=${argument#*upperdir=}; ndk=${ndk%%,*} ;;
  esac
done
if [[ -n "${FAKE_DOWNLOAD:-}" ]]; then mkdir -p "$(dirname "$packages/$FAKE_DOWNLOAD")"; echo downloaded >"$packages/$FAKE_DOWNLOAD"; fi
if [[ -n "${FAKE_NDK_DOWNLOAD:-}" ]]; then mkdir -p "$(dirname "$ndk/$FAKE_NDK_DOWNLOAD")"; echo ndk >"$ndk/$FAKE_NDK_DOWNLOAD"; fi
exit "${FAKE_DOCKER_EXIT:-0}"
"""


@unittest.skipUnless(shutil.which("python3"), "needs python3, as the ci agent has")
class PackageCacheRunTests(RunCheckHarness):
    """--scope and --promote: the repository's package cache, as an overlay, and what a passing check keeps."""

    def setUp(self):
        super().setUp()
        self.fake("docker", PACKAGE_DOCKER)
        self.cache = self.root / "toolchains"
        self.cache.mkdir()

    def run_with(self, *options, **env):
        return self.run_check("unit", "img", str(self.workspace), "--toolchains", str(self.cache), *options,
                              "--", "sh", "-c", "ci/test.sh", **env)

    def mounts(self):
        """{destination in the container: overlay options as a dict} of every --mount docker got."""
        arguments = self.docker_arguments()
        found = {}
        for i, argument in enumerate(arguments):
            if argument == "--mount":
                field = arguments[i + 1]
                destination = field.split(",")[1].removeprefix("dst=")
                options = field[field.index('"volume-opt=o=') + len('"volume-opt=o='):-1]
                found[destination] = dict((option.split("=", 1) + [True])[:2] for option in options.split(","))
        return found

    def current(self, scope="orpheus"):
        link = self.cache / "packages" / scope / "current"
        return (link.parent / os.readlink(link)) if os.path.islink(link) else None

    def test_given_a_scope_when_run_then_the_repository_s_snapshot_is_an_overlay_and_the_tools_point_into_it(self):
        result = self.run_with("--scope", "orpheus")
        self.assertEqual(result.returncode, 0, result.stderr)
        overlay = self.mounts()["/opt/yggdrasil/cache"]
        self.assertEqual(overlay["lowerdir"], str(self.current()))
        self.assertTrue(overlay["upperdir"].startswith(f"{self.cache}/runs/"))
        for setting in ("PUB_CACHE=/opt/yggdrasil/cache/pub", "GRADLE_USER_HOME=/opt/yggdrasil/cache/gradle",
                        "NUGET_PACKAGES=/opt/yggdrasil/cache/nuget", "npm_config_cache=/opt/yggdrasil/cache/npm",
                        "APT_CONFIG=/opt/yggdrasil/apt.conf"):
            self.assertIn(["-e", setting], self.pairs(), setting)
        apt = next(b for a, b in self.pairs() if a == "-v" and b.endswith(":/opt/yggdrasil/apt.conf:ro"))
        self.assertTrue(apt.startswith(f"{self.cache}/runs/"))
        self.assertNotIn(f"{self.cache}/packages", " ".join(b for a, b in self.pairs() if a == "-v"),
                         "the cache itself is never bind-mounted")
        self.assertIn("orpheus's package cache at /opt/yggdrasil/cache", result.stderr)
        # Removed with the run: but what it said is what apt was told.
        self.assertEqual(list((self.cache / "runs").iterdir()), [])

    def test_given_the_apt_configuration_when_a_check_runs_then_apt_keeps_its_packages_in_the_cache(self):
        self.fake("docker", PACKAGE_DOCKER.replace('printf \'%s\\n\' "$@" >"$FAKE_DOCKER_LOG"',
                                                   'printf \'%s\\n\' "$@" >"$FAKE_DOCKER_LOG"\n'
                                                   'for a in "$@"; do case $a in *:/opt/yggdrasil/apt.conf:ro) '
                                                   'cp "${a%%:*}" "$FAKE_DOCKER_LOG.apt" ;; esac; done'))
        self.assertEqual(self.run_with("--scope", "orpheus").returncode, 0)
        self.assertEqual(pathlib.Path(f"{self.log}.apt").read_text(),
                         'Dir::Cache::Archives "/opt/yggdrasil/cache/apt/archives/";\n')

    def test_given_an_image_with_an_android_sdk_when_run_then_its_ndk_directory_is_an_overlay_of_the_cache(self):
        result = self.run_with("--scope", "orpheus",
                               FAKE_IMAGE_ENV="PATH=/usr/bin\nANDROID_HOME=/opt/android-sdk-linux\nLANG=C")
        self.assertEqual(result.returncode, 0, result.stderr)
        overlay = self.mounts()["/opt/android-sdk-linux/ndk"]
        self.assertEqual(overlay["lowerdir"], f"{self.current()}/android-ndk")
        self.assertIn("and /opt/android-sdk-linux/ndk", result.stderr)

    def test_given_an_image_with_only_android_sdk_root_when_run_then_that_one_is_used(self):
        self.assertEqual(self.run_with("--scope", "orpheus", FAKE_IMAGE_ENV="ANDROID_SDK_ROOT=/sdk/").returncode, 0)
        self.assertIn("/sdk/ndk", self.mounts())

    def test_given_an_image_without_an_android_sdk_or_with_an_odd_one_when_run_then_no_ndk_overlay(self):
        for image_env in ("PATH=/usr/bin", "ANDROID_HOME=relative/sdk", "ANDROID_HOME=/opt/a,b",
                          "ANDROID_HOME=/opt/../etc"):
            with self.subTest(image_env=image_env):
                self.assertEqual(self.run_with("--scope", "orpheus", FAKE_IMAGE_ENV=image_env).returncode, 0)
                self.assertEqual(list(self.mounts()), ["/opt/yggdrasil/cache"])

    def test_given_a_passing_check_of_a_trusted_build_when_run_then_what_it_downloaded_is_in_the_next_snapshot(self):
        result = self.run_with("--scope", "orpheus", "--promote", FAKE_DOWNLOAD="pub/hosted/pub.dev/http-1.0.0/x",
                               FAKE_NDK_DOWNLOAD="27.0.1/source.properties",
                               FAKE_IMAGE_ENV="ANDROID_HOME=/opt/android-sdk-linux")
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.current()
        self.assertEqual((first / "pub/hosted/pub.dev/http-1.0.0/x").read_text(), "downloaded\n")
        self.assertEqual((first / "android-ndk/27.0.1/source.properties").read_text(), "ndk\n")
        # The next check starts from it.
        self.assertEqual(self.run_with("--scope", "orpheus").returncode, 0)
        self.assertEqual(self.mounts()["/opt/yggdrasil/cache"]["lowerdir"], str(first))
        self.assertEqual(list((self.cache / "runs").iterdir()), [])

    def test_given_a_failing_check_of_a_trusted_build_when_run_then_nothing_is_kept_and_its_code_is_returned(self):
        self.assertEqual(self.run_with("--scope", "orpheus").returncode, 0)
        before = self.current()
        result = self.run_with("--scope", "orpheus", "--promote", FAKE_DOWNLOAD="pub/x", FAKE_DOCKER_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.current(), before)
        self.assertFalse((before / "pub/x").exists())

    def test_given_a_pull_request_s_check_when_run_then_what_it_downloaded_is_thrown_away(self):
        # Without --promote (the library gives it to pushes of develop, main and master only).
        self.assertEqual(self.run_with("--scope", "orpheus").returncode, 0)
        before = self.current()
        self.assertEqual(self.run_with("--scope", "orpheus", FAKE_DOWNLOAD="pub/evil").returncode, 0)
        self.assertEqual(self.current(), before)
        self.assertFalse((before / "pub/evil").exists())
        self.assertEqual(list((self.cache / "runs").iterdir()), [])

    def test_given_another_repository_when_its_check_runs_then_it_never_sees_this_one_s_cache(self):
        self.assertEqual(self.run_with("--scope", "orpheus", "--promote", FAKE_DOWNLOAD="pub/x").returncode, 0)
        self.assertEqual(self.run_with("--scope", "chimera").returncode, 0)
        lower = self.mounts()["/opt/yggdrasil/cache"]["lowerdir"]
        self.assertTrue(lower.startswith(f"{self.cache}/packages/chimera/"), lower)
        self.assertFalse((pathlib.Path(lower) / "pub/x").exists())

    def test_given_a_scope_that_cannot_name_a_cache_when_run_then_the_check_runs_without_one(self):
        for scope in (".hidden", "a/b", "a,b", "x" * 101):
            with self.subTest(scope=scope):
                result = self.run_with("--scope", scope)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.mounts(), {})
                self.assertNotIn("PUB_CACHE", "\n".join(self.docker_arguments()))
        self.assertFalse((self.cache / "packages").exists())

    def test_given_a_scope_and_a_flutter_pin_when_run_then_both_overlays_share_one_run_directory(self):
        archives = self.root / "archives"
        archives.mkdir()
        checksum = flutter_archive(archives / f"flutter_linux_{VERSION}-stable.tar.xz")
        (self.workspace / "ci").mkdir()
        (self.workspace / "ci" / "flutter-env.sh").write_text(f"FLUTTER_VERSION={VERSION}\nFLUTTER_SHA256={checksum}\n")
        if not (shutil.which("xz") and shutil.which("flock")):
            self.skipTest("needs xz and flock")
        result = self.run_with("--scope", "orpheus",
                               YGG_FLUTTER_ARCHIVE_URL=f"file://{archives}/flutter_linux_%s-stable.tar.xz")
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.mounts()
        self.assertEqual(set(mounts), {SDK_IN_CONTAINER, "/opt/yggdrasil/cache"})
        runs = {pathlib.Path(m["upperdir"]).parent.parent for m in mounts.values()}
        self.assertEqual(len(runs), 1, mounts)

    def test_given_bad_package_options_when_run_then_it_is_a_usage_error(self):
        workspace = str(self.workspace)
        for arguments in (["unit", "img", workspace, "--scope", "orpheus", "--", "x"],      # only with --toolchains
                          ["unit", "img", workspace, "--toolchains", str(self.cache), "--promote", "--", "x"],
                          ["unit", "img", workspace, "--toolchains", str(self.cache), "--scope", "--", "x"]):
            with self.subTest(arguments=arguments):
                result = self.run_check(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr)
                self.assertFalse(self.log.exists(), "docker must not run")


if __name__ == "__main__":
    unittest.main()
