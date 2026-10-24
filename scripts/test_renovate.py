"""Tests for scripts/renovate.sh (Renovate's daily run on the dev machine) and the systemd units that run it.

    python3 -m unittest discover -s scripts -p "test_renovate.py"

`docker` is a fake on PATH. It logs every call to $FAKE_LOG, answers `info` as a rootless engine, and for
`run` it keeps what the container would have been given: the --env-file's content, mode and path (the file
is removed by the time the script is done) in $FAKE_LOG.env, .mode and .path. $FAKE_RUN_STATUS is the run's
exit status. HOME and the XDG runtime directory are temporary, so the run-scoped env file lands in a
directory of ours. The machine step that installs the units is tested in test_machine.py.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "renovate.sh"
UNITS = SCRIPTS.parent / "platform" / "systemd"
TEMPLATE = SCRIPTS.parent / "env" / "renovate.env.example"

FAKES = {
    "docker": r"""
        echo "docker $*" >>"$FAKE_LOG"
        case "$1" in
          info) echo '["name=seccomp,profile=builtin","name=rootless"]' ;;
          run)
            while (($#)); do
              if [[ "$1" == --env-file ]]; then
                cp "$2" "$FAKE_LOG.env"
                stat -c %a "$2" >"$FAKE_LOG.mode"
                echo "$2" >"$FAKE_LOG.path"
              fi
              shift
            done
            exit "${FAKE_RUN_STATUS:-0}"
            ;;
        esac
    """,
}

TOKEN = "t0ken.Abc-123_xyz"
CATALOG = """\
owner: someone
forge:
  type: forgejo
  url: https://git.example.com/
environments:
  - {id: dev, name: Dev, mode: ports, trigger: manual}
systems:
  - id: shop
    name: Shop
    applications:
      - {id: shop-api, name: API, kind: api, health: "http://shop-api:8080/healthz"}
      - {id: shop-web, name: Web, kind: web, health: "http://shop-web:8080/healthz", repository: shop}
      - {id: shop-ui, name: UI, kind: web, health: "http://shop-ui:8080/healthz", repository: shop}
  - id: platform
    name: Platform
    applications:
      - {id: traefik, name: Traefik, kind: platform, health: "http://traefik:8082/ping"}
repositories:
  - id: dotnet-util
"""
STATIC = f"""\
# the owner's file
RENOVATE_TOKEN={TOKEN}
RENOVATE_ENDPOINT=https://git.example.com/api/v1

RENOVATE_PLATFORM=gitea
RENOVATE_AUTODISCOVER=false
"""


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class RenovateTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-renovate-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        self.secrets = self.temp / "secrets"
        self.runtime = self.temp / "run"
        for directory in (bin_dir, self.secrets, self.runtime):
            directory.mkdir()
        self.runtime.chmod(0o700)
        for name, body in FAKES.items():
            fake = bin_dir / name
            fake.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body).lstrip())
            fake.chmod(0o755)
        self.log = self.temp / "calls"
        self.log.write_text("")
        self.catalog = self.temp / "catalog.yaml"
        self.catalog.write_text(CATALOG)
        self.env_file = self.secrets / "renovate.env"
        self.env_file.write_text(STATIC)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.temp),
            "YGG_SECRETS_DIR": str(self.secrets),
            "YGG_CATALOG": str(self.catalog),
            "DOCKER_HOST": f"unix://{self.temp}/docker.sock",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "FAKE_LOG": str(self.log),
        }
        self.env.pop("FORGE_OWNER", None)

    def renovate(self, *arguments, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), *(arguments or ("run",))], env={**self.env, **env}, capture_output=True, text=True
        )

    def calls(self):
        return self.log.read_text().splitlines()

    def container_runs(self):
        return [call for call in self.calls() if call.startswith("docker run")]

    def run_call(self):
        self.assertEqual(len(self.container_runs()), 1, self.calls())
        return self.container_runs()[0]

    def container_env(self):
        """The variables the container got, from the env file docker saw: NAME -> value."""
        pairs = {}
        for line in pathlib.Path(f"{self.log}.env").read_text().splitlines():
            name, _, value = line.partition("=")
            pairs[name] = value
        return pairs

    def test_given_a_run_when_started_then_the_token_is_only_in_the_env_file(self):
        result = self.renovate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Not an argument of any docker call (arguments show in ps), not a -e value, not in anything printed.
        self.assertNotIn(TOKEN, "\n".join(self.calls()))
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotRegex(self.run_call(), r"(^| )(-e|--env)( |=)")
        self.assertIn("--env-file", self.run_call())
        self.assertEqual(self.container_env()["RENOVATE_TOKEN"], TOKEN)
        # The copy the container read is the owner's alone, in the user's runtime directory, and gone after.
        self.assertEqual(pathlib.Path(f"{self.log}.mode").read_text().strip(), "600")
        copy = pathlib.Path(pathlib.Path(f"{self.log}.path").read_text().strip())
        self.assertEqual(copy.parent, self.runtime)
        self.assertFalse(copy.exists())
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_given_a_github_com_token_when_run_then_it_reaches_renovate_only_through_the_env_file(self):
        github = "ghp_Fine-grained.token_123"
        self.env_file.write_text(STATIC + f"RENOVATE_GITHUB_COM_TOKEN={github}\n")
        result = self.renovate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Renovate reads GITHUB_COM_TOKEN; the owner's name for it is not passed on, so it is there once.
        container = self.container_env()
        self.assertEqual(container["GITHUB_COM_TOKEN"], github)
        self.assertNotIn("RENOVATE_GITHUB_COM_TOKEN", container)
        # Like the Forgejo token: not an argument (ps shows those), not a -e value, not in anything printed.
        self.assertNotIn(github, "\n".join(self.calls()))
        self.assertNotIn(github, result.stdout + result.stderr)
        self.assertNotRegex(self.run_call(), r"(^| )(-e|--env)( |=)")
        self.assertEqual(pathlib.Path(f"{self.log}.mode").read_text().strip(), "600")
        self.assertFalse(pathlib.Path(pathlib.Path(f"{self.log}.path").read_text().strip()).exists())

    def test_given_no_github_com_token_when_run_then_the_container_gets_none(self):
        for text in (STATIC, STATIC + "RENOVATE_GITHUB_COM_TOKEN=\n"):
            with self.subTest(text=text[-40:]):
                self.log.write_text("")
                self.env_file.write_text(text)
                result = self.renovate()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("GITHUB_COM_TOKEN", self.container_env())

    def test_given_a_github_com_token_with_odd_characters_when_run_then_it_is_refused_without_printing_it(self):
        bad = "ghp_has space"
        self.env_file.write_text(STATIC + f"RENOVATE_GITHUB_COM_TOKEN={bad}\n")
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RENOVATE_GITHUB_COM_TOKEN", result.stderr)
        self.assertNotIn(bad, result.stdout + result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_a_file_with_the_containers_own_github_name_when_run_then_it_is_refused(self):
        # GITHUB_COM_TOKEN is renovate.sh's to write, from RENOVATE_GITHUB_COM_TOKEN: a second source
        # would be a second place for the token to be.
        self.env_file.write_text(STATIC + "GITHUB_COM_TOKEN=abc\n")
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GITHUB_COM_TOKEN", result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_the_catalog_when_run_then_every_repository_is_listed_under_the_owner(self):
        result = self.renovate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # An application's `repository` (shared by two of them) or id, no platform component, then the
        # `repositories` entries: the same set rules.py gives rules to.
        self.assertEqual(
            self.container_env()["RENOVATE_REPOSITORIES"],
            "someone/shop-api,someone/shop,someone/dotnet-util",
        )
        # FORGE_OWNER, as forge.sh reads it, wins over the catalog's owner.
        result = self.renovate(FORGE_OWNER="forgejo-owner")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.container_env()["RENOVATE_REPOSITORIES"].startswith("forgejo-owner/shop-api,"))

    def test_given_a_run_when_started_then_renovate_gets_the_branch_policy_and_the_platform(self):
        self.assertEqual(self.renovate().returncode, 0)
        env = self.container_env()
        self.assertEqual(env["RENOVATE_PLATFORM"], "gitea")
        self.assertEqual(env["RENOVATE_AUTODISCOVER"], "false")
        self.assertEqual(env["RENOVATE_ENDPOINT"], "https://git.example.com/api/v1")
        config = json.loads(env["RENOVATE_CONFIG"])
        self.assertEqual(config["branchPrefix"], "renovate/")
        self.assertEqual(config["baseBranches"], ["develop"])
        # Repositories that carry no Renovate config of their own are left alone, not onboarded.
        self.assertIs(config["onboarding"], False)
        self.assertEqual(config["requireConfig"], "required")

    def test_given_no_endpoint_in_the_file_when_run_then_it_comes_from_the_catalog(self):
        self.env_file.write_text(STATIC.replace("RENOVATE_ENDPOINT=https://git.example.com/api/v1", "RENOVATE_ENDPOINT="))
        self.assertEqual(self.renovate().returncode, 0)
        self.assertEqual(self.container_env()["RENOVATE_ENDPOINT"], "https://git.example.com/api/v1")

    def test_given_a_run_when_started_then_the_image_is_pinned_and_runs_in_the_users_own_engine(self):
        self.assertEqual(self.renovate().returncode, 0)
        # An exact version and its digest, never a moving tag: what runs against the repositories with a push
        # token is chosen by a commit here.
        self.assertRegex(self.run_call(), r" renovate/renovate:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}( |$)")
        # The engine is checked to be rootless before anything runs in it.
        self.assertLess(self.calls().index("docker info --format {{json .SecurityOptions}}"),
                        self.calls().index(self.run_call()))

    def test_given_an_engine_that_is_not_rootless_when_run_then_nothing_runs(self):
        fake = self.temp / "bin" / "docker"
        fake.write_text(fake.read_text().replace("name=rootless", "name=seccomp"))
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rootless", result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_a_failing_container_when_run_then_the_script_fails_and_the_env_file_is_gone(self):
        result = self.renovate(FAKE_RUN_STATUS="3")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_given_no_env_file_when_run_then_it_fails_naming_the_template(self):
        self.env_file.unlink()
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("env/renovate.env.example", result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_an_empty_token_when_run_then_it_fails_without_running(self):
        self.env_file.write_text(STATIC.replace(TOKEN, ""))
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RENOVATE_TOKEN", result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_a_file_that_changes_what_renovate_visits_when_run_then_it_is_refused(self):
        for line in ("RENOVATE_AUTODISCOVER=true", "RENOVATE_PLATFORM=github", "RENOVATE_REPOSITORIES=a/b",
                     "RENOVATE_CONFIG={}"):
            with self.subTest(line=line):
                name = line.split("=")[0]
                kept = [text for text in STATIC.splitlines() if not text.startswith(name)]
                self.env_file.write_text("\n".join(kept) + "\n" + line + "\n")
                result = self.renovate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertNotIn(TOKEN, result.stderr)
                self.assertEqual(self.container_runs(), [])

    def test_given_a_line_that_is_not_a_setting_when_run_then_it_is_refused_without_printing_it(self):
        self.env_file.write_text(STATIC + f"export RENOVATE_TOKEN={TOKEN}\n")
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("line", result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_a_github_catalog_when_run_then_it_is_refused(self):
        self.catalog.write_text(CATALOG.replace("forge:\n  type: forgejo\n  url: https://git.example.com/\n", ""))
        result = self.renovate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forgejo", result.stderr)
        self.assertEqual(self.container_runs(), [])

    def test_given_an_unknown_command_when_started_then_it_prints_the_usage(self):
        result = self.renovate("later")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)

    def test_given_the_template_when_read_then_it_holds_what_the_script_needs_and_no_token(self):
        text = TEMPLATE.read_text()
        for name in ("RENOVATE_TOKEN=$", "RENOVATE_ENDPOINT=$", "RENOVATE_PLATFORM=gitea$", "RENOVATE_AUTODISCOVER=false$"):
            self.assertRegex(text, rf"(?m)^{name}")
        self.assertNotRegex(text, r"(?m)^RENOVATE_(REPOSITORIES|CONFIG)=")
        # The GitHub token is optional: in the template, empty.
        self.assertRegex(text, r"(?m)^RENOVATE_GITHUB_COM_TOKEN=$")

    def test_given_the_script_when_listed_by_git_then_it_is_executable(self):
        # `git update-index --chmod=+x`: a checkout on Linux must be able to run it, as the unit does.
        listing = subprocess.run(["git", "ls-files", "-s", "scripts/renovate.sh"], cwd=SCRIPTS.parent,
                                 capture_output=True, text=True).stdout
        if not listing:
            self.skipTest("not tracked by git (yet)")
        self.assertTrue(listing.startswith("100755"), listing)


class RenovateUnitTests(unittest.TestCase):
    def test_given_the_timer_when_read_then_it_fires_daily_at_five_with_catch_up_and_a_delay(self):
        text = (UNITS / "yggdrasil-renovate.timer").read_text()
        self.assertRegex(text, r"(?m)^OnCalendar=\*-\*-\* 05:00:00$")
        self.assertRegex(text, r"(?m)^Persistent=true$")
        self.assertRegex(text, r"(?m)^RandomizedDelaySec=30m$")
        self.assertRegex(text, r"(?m)^WantedBy=timers\.target$")

    def test_given_the_service_when_read_then_it_runs_the_script_after_the_users_engine(self):
        text = (UNITS / "yggdrasil-renovate.service").read_text()
        self.assertRegex(text, r"(?m)^After=docker\.service$")
        self.assertRegex(text, r"(?m)^Type=oneshot$")
        self.assertRegex(text, r"(?m)^ExecStart=/opt/yggdrasil/scripts/renovate\.sh run$")
        # A user unit doesn't read .profile: it names the user's own engine itself.
        self.assertRegex(text, r"(?m)^Environment=DOCKER_HOST=unix://%t/docker\.sock$")
        self.assertNotIn("/var/run/docker.sock", text)


if __name__ == "__main__":
    unittest.main()
