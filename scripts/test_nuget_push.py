"""Tests for scripts/nuget-push.sh, against a fake `docker` on PATH.   python3 -m unittest discover -s scripts

The fake docker appends each call's arguments to $FAKE_DOCKER_LOG, one per line, a "--" line after each call,
and the NUGET_API_KEY its own environment had to $FAKE_DOCKER_ENV: what is tested is the `docker run` the
script builds -- where the packages, the key and the source end up -- not the SDK. It exits with
$FAKE_DOCKER_EXIT (default 0) on the call whose package name contains $FAKE_DOCKER_FAIL_ON (every call when
that is unset).
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "nuget-push.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" -- >>"$FAKE_DOCKER_LOG"
printf 'NUGET_API_KEY=%s\\n' "${NUGET_API_KEY:-}" >>"$FAKE_DOCKER_ENV"
if [[ -z "${FAKE_DOCKER_FAIL_ON:-}" || "$*" == *"$FAKE_DOCKER_FAIL_ON"* ]]; then
  exit "${FAKE_DOCKER_EXIT:-0}"
fi
"""

KEY = "oy2-a-throwaway-nuget-key"
NUGET_ORG = "https://api.nuget.org/v3/index.json"


@unittest.skipUnless(shutil.which("bash") and shutil.which("realpath") and os.name == "posix",
                     "needs bash and realpath on a POSIX system")
class NugetPushTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="nuget-push-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        self.log = self.root / "docker.log"
        self.docker_env = self.root / "docker.env"
        self.packages = self.root / "packages"
        (self.packages / "src" / "Lib" / "bin").mkdir(parents=True)
        (self.packages / "Lib.1.2.3.nupkg").write_bytes(b"zip")
        (self.packages / "src" / "Lib" / "bin" / "Lib.Extra.1.2.3.nupkg").write_bytes(b"zip")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "NUGET_", "YGG_"))}
        self.env = dict(base, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_DOCKER_LOG=str(self.log),
                        FAKE_DOCKER_ENV=str(self.docker_env), NUGET_API_KEY=KEY)

    def push(self, *arguments, **env):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

    def glob(self, pattern="**/*.nupkg"):
        return f"{self.packages}/{pattern}"

    def docker_calls(self):
        """Each `docker` call's arguments."""
        if not self.log.exists():
            return []
        calls, current = [], []
        for line in self.log.read_text().splitlines():
            if line == "--":
                calls.append(current)
                current = []
            else:
                current.append(line)
        return calls

    def test_given_packages_when_pushed_then_each_is_pushed_once_with_skip_duplicate_to_nuget_org(self):
        result = self.push(self.glob())
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertEqual(len(calls), 2, calls)
        pushed = []
        for call in calls:
            self.assertEqual(call[:2], ["run", "--rm"])
            self.assertIn(f"{self.packages.resolve()}:/packages:ro", call)
            # The pushed package and the source are the arguments of the container's command; the command
            # is `dotnet nuget push` and nothing else (no repository code runs here).
            script = call[call.index("-c") + 1]
            self.assertEqual(script, 'exec dotnet nuget push "$1" --api-key "$NUGET_API_KEY" --source "$2" --skip-duplicate')
            self.assertEqual(call[-3], "sh")
            pushed.append(call[-2])
            self.assertEqual(call[-1], NUGET_ORG)
        self.assertEqual(sorted(pushed), ["/packages/Lib.1.2.3.nupkg", "/packages/src/Lib/bin/Lib.Extra.1.2.3.nupkg"])
        self.assertIn("Lib.1.2.3.nupkg", result.stderr)

    def test_given_the_key_when_pushed_then_it_reaches_docker_only_through_the_environment(self):
        result = self.push(self.glob())
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.docker_calls():
            self.assertNotIn(KEY, "\n".join(call))
            # -e NAME (no value): docker takes the value from its own environment.
            self.assertIn(["-e", "NUGET_API_KEY"], [call[i:i + 2] for i in range(len(call) - 1)])
        self.assertEqual(self.docker_env.read_text().splitlines(), [f"NUGET_API_KEY={KEY}"] * 2)

    def test_given_the_image_when_read_then_it_is_the_dotnet_sdk_pinned_by_version_and_digest(self):
        self.assertEqual(self.push(self.glob()).returncode, 0)
        images = {call[call.index("sh") - 1] for call in self.docker_calls()}
        image, = images
        self.assertRegex(image, r"^mcr\.microsoft\.com/dotnet/sdk:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")

    def test_given_a_glob_matching_nothing_when_pushed_then_it_is_refused_and_nothing_runs(self):
        result = self.push(self.glob("**/*.missing.nupkg"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("no package matches", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_given_no_key_when_pushed_then_it_is_refused_and_nothing_runs(self):
        for key in ("", None):
            env = dict(self.env)
            env.pop("NUGET_API_KEY")
            if key is not None:
                env["NUGET_API_KEY"] = key
            result = subprocess.run(["bash", str(SCRIPT), self.glob()], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2, key)
            self.assertIn("NUGET_API_KEY", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_given_a_link_among_the_packages_when_pushed_then_none_is_pushed(self):
        outside = self.root / "Secret.1.0.0.nupkg"
        outside.write_bytes(b"not ours")
        for name, target in (("Link.1.0.0.nupkg", outside), ("Inside.1.0.0.nupkg", "Lib.1.2.3.nupkg")):
            link = self.packages / name
            link.symlink_to(target)
            result = self.push(self.glob())
            self.assertEqual(result.returncode, 3, name)
            self.assertIn(name, result.stderr)
            self.assertIn("link", result.stderr)
            self.assertEqual(self.docker_calls(), [], name)
            link.unlink()

    def test_given_a_linked_directory_when_pushed_then_its_packages_are_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "Other.1.0.0.nupkg").write_bytes(b"zip")
        (self.packages / "linked").symlink_to(elsewhere)
        result = self.push(self.glob("l*/*.nupkg"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("outside", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_given_a_glob_leaving_the_directory_when_pushed_then_it_is_refused(self):
        (self.root / "Up.1.0.0.nupkg").write_bytes(b"zip")
        for pattern in ("../*.nupkg", "src/../../*.nupkg", "./*.nupkg"):
            result = self.push(self.glob(pattern))
            self.assertEqual(result.returncode, 3, pattern)
            self.assertIn("outside", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_given_a_file_that_is_not_a_package_when_pushed_then_it_is_refused(self):
        (self.packages / "notes.txt").write_text("hello\n")
        result = self.push(self.glob("*"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("notes.txt", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_given_another_source_when_pushed_then_packages_go_there_and_only_over_https(self):
        result = self.push(self.glob(), "--source", "https://feed.example/v3/index.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({call[-1] for call in self.docker_calls()}, {"https://feed.example/v3/index.json"})
        self.log.unlink()
        # The key never travels in the clear.
        for source in ("http://feed.example/v3/index.json", "feed.example", "https://a b/", ""):
            result = self.push(self.glob(), "--source", source)
            self.assertEqual(result.returncode, 2, source)
            self.assertEqual(self.docker_calls(), [], source)

    def test_given_a_ca_file_when_pushed_then_the_container_trusts_it_for_the_source(self):
        ca = self.root / "ca.crt"
        ca.write_text("-----BEGIN CERTIFICATE-----\n")
        result = self.push(self.glob(), "--ca-file", str(ca))
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.docker_calls():
            pairs = [call[i:i + 2] for i in range(len(call) - 1)]
            self.assertIn(["-v", f"{ca.resolve()}:/ygg/ca.crt:ro"], pairs)
            self.assertIn(["-e", "SSL_CERT_FILE=/ygg/ca.crt"], pairs)
        self.log.unlink()
        result = self.push(self.glob(), "--ca-file", str(self.root / "missing.crt"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.docker_calls(), [])

    def test_given_a_push_that_fails_when_pushed_then_it_stops_naming_the_package(self):
        result = self.push(self.glob(), FAKE_DOCKER_EXIT="1", FAKE_DOCKER_FAIL_ON="Lib.1.2.3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nuget-push: pushing Lib.1.2.3.nupkg failed (exit 1)", result.stderr)
        # Sorted: Lib.1.2.3.nupkg comes first, so the second package is never pushed.
        self.assertEqual(len(self.docker_calls()), 1)

    def test_given_a_wrong_command_line_when_run_then_the_usage_is_shown_and_it_exits_2(self):
        for arguments in ((), (self.glob(), "--bogus"), (self.glob(), "--source"), (self.glob(), self.glob()),
                          (f"{self.root}/missing/*.nupkg",), ("a b/*.nupkg",)):
            result = self.push(*arguments)
            self.assertEqual(result.returncode, 2, arguments)
            self.assertIn("nuget-push", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_given_the_script_when_read_then_the_key_is_never_expanded_into_an_argument(self):
        code = "\n".join(line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))
        # "$NUGET_API_KEY" appears only inside the single-quoted container command, expanded there by the
        # container's own shell; the script itself only tests whether it is set.
        code = code.replace("""'exec dotnet nuget push "$1" --api-key "$NUGET_API_KEY" --source "$2" --skip-duplicate'""", "")
        code = code.replace('"${NUGET_API_KEY:-}"', "")
        self.assertNotRegex(code, r"\$\{?NUGET_API_KEY")


if __name__ == "__main__":
    unittest.main()
