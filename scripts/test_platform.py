"""Tests for scripts/platform.sh's checks, against a fake `docker` on PATH.

    python3 -m unittest discover -s scripts
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "platform.sh"

# What every command needs to render Traefik's configuration (compose.yml requires the file on all of
# them, down and ps included), whatever else a test is about.
TRAEFIK_SETTINGS = {"DOMAIN": "techcraftsmen.net", "ACME_EMAIL": "ops@techcraftsmen.net",
                    "ACME_DNS_PROVIDER": "cloudflare"}
TRAEFIK_ENV = "".join(f"{key}={value}\n" for key, value in TRAEFIK_SETTINGS.items())

# A top-level `forge:` or `mirror:` block: the key and its indented lines.
FORGE_OR_MIRROR = re.compile(r"^(?:forge|mirror):[ \t]*\n(?:[ \t]+\S.*\n)*", re.M)


def github_catalog():
    """The repository's catalog.yaml without its forge and mirror blocks: a GitHub installation's, whatever
    git server this installation has moved to. The tests' baseline, which use_catalog adds a block to."""
    return FORGE_OR_MIRROR.sub("", (SCRIPT.parent.parent / "catalog.yaml").read_text())


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="platform-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        # Logs what compose is given, Traefik's rendered configuration (as it is when Compose runs) and
        # its hash included. Also the engine socket platform.sh points Docker at and the Prometheus file
        # it picks.
        docker.write_text('#!/usr/bin/env bash\n'
                          'echo "docker $*" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "DOCKER_SOCKET=${DOCKER_SOCKET:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "DOCKER_HOST=${DOCKER_HOST:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "PROMETHEUS_CONFIG_FILE=${PROMETHEUS_CONFIG_FILE:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_TRAEFIK_CONFIG_HASH=${YGG_TRAEFIK_CONFIG_HASH:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_CASC_CONFIG=${YGG_CASC_CONFIG:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "FORGE_URL=${FORGE_URL:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "FORGE_JENKINS_USER=${FORGE_JENKINS_USER:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "GITHUB_READ_APP_KEY_FILE=${GITHUB_READ_APP_KEY_FILE:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "NUGET_API_KEY_FILE=${NUGET_API_KEY_FILE:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "JENKINS_CREDENTIALS_DIR=${JENKINS_CREDENTIALS_DIR:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_JENKINS_CREDENTIALS_FILE=${YGG_JENKINS_CREDENTIALS_FILE:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_JENKINS_CREDENTIALS_HASH=${YGG_JENKINS_CREDENTIALS_HASH:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_JENKINS_CONFIG_HASH=${YGG_JENKINS_CONFIG_HASH:-}" >>"$FAKE_DOCKER_LOG"\n'
                          'echo "YGG_JENKINS_PLUGINS_REFRESHED=${YGG_JENKINS_PLUGINS_REFRESHED:-}" >>"$FAKE_DOCKER_LOG"\n'
                          '# The controller\'s image and its plugins, for update-plugins, when a test sets them up\n'
                          '# (use_jenkins_images): each tag is a file of $FAKE_STATE/tags holding its image.\n'
                          'state=${FAKE_STATE:-}\n'
                          'tag_file() { echo "$state/tags/${1//[\\/:]/_}"; }\n'
                          'if [[ -n "$state" ]]; then\n'
                          '  case " $* " in\n'
                          '    " image inspect "*) cat "$(tag_file "${@: -1}")" 2>/dev/null || exit 1 ;;\n'
                          '    " image rm "*) rm -f "$(tag_file "${@: -1}")" ;;\n'
                          '    *" build jenkins "*) cat "$state/built" >"$(tag_file yggdrasil/jenkins:local)" ;;\n'
                          '    " tag "*) cat "$(tag_file "$2")" >"$(tag_file "$3")" || exit 1 ;;\n'
                          '    " run --rm --entrypoint sh "*) cat "$state/plugins-$(cat "$(tag_file "$5")")" ;;\n'
                          '    *" ps --quiet jenkins "*) cat "$state/running" ;;\n'
                          '    # `up` recreates the controller when a test gave it a new container (running-after).\n'
                          '    *" up --detach "*) if [[ -f "$state/running-after" ]]; then\n'
                          '        cp "$state/running-after" "$state/running"; fi ;;\n'
                          '    # The job registration script (it reloads Configuration as Code) answers from register.<n>,\n'
                          '    # the last one again once they run out, and fails on one that says FAIL; any other\n'
                          '    # script is blocking_builds, answering from busy.\n'
                          '    *" jenkins-cli groovy = "*)\n'
                          '      if [[ $(cat) == *ConfigurationAsCode* ]]; then\n'
                          '        echo "register-jobs" >>"$FAKE_DOCKER_LOG"\n'
                          '        calls=$(( $(cat "$state/register-calls" 2>/dev/null || echo 0) + 1 ))\n'
                          '        echo "$calls" >"$state/register-calls"\n'
                          '        answer="$state/register.$calls"\n'
                          '        [[ -f "$answer" ]] \\\n'
                          '          || answer="$state/$(ls "$state" | grep "^register\\.[0-9]" | sort -t. -k2 -n | tail -n 1)"\n'
                          '        [[ $(cat "$answer") != FAIL ]] || exit 1\n'
                          '        cat "$answer"\n'
                          '      else\n'
                          '        head -n 1 "$state/busy"; [[ $(wc -l <"$state/busy") -le 1 ]] || sed -i 1d "$state/busy"\n'
                          '      fi ;;\n'
                          '  esac\n'
                          'fi\n'
                          'if [[ -n "${YGG_TRAEFIK_CONFIG_FILE:-}" ]]; then\n'
                          '  echo "traefik-config-file: $YGG_TRAEFIK_CONFIG_FILE" >>"$FAKE_DOCKER_LOG"\n'
                          '  cat "$YGG_TRAEFIK_CONFIG_FILE" >>"$FAKE_DOCKER_LOG"\n'
                          'fi\n'
                          '# The engine answers `docker info` as a rootless one unless a test says otherwise.\n'
                          'if [[ "${1:-}" == info ]]; then\n'
                          '  echo "${FAKE_DOCKER_INFO:-$FAKE_ROOTLESS_INFO}"\n'
                          'fi\n')
        docker.chmod(0o755)
        self.log = self.temp / "docker.log"
        self.secrets = self.temp / "secrets"
        self.secrets.mkdir()
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        YGG_SECRETS_DIR=str(self.secrets), FAKE_DOCKER_LOG=str(self.log),
                        FAKE_ROOTLESS_INFO='["name=seccomp","name=rootless"]',
                        # The user's own engine, whoever runs the tests; the lookup's tests unset it.
                        DOCKER_HOST="unix:///run/user/1000/docker.sock")
        self.use_catalog("")

    def up(self, platform_env):
        (self.secrets / "platform.env").write_text(TRAEFIK_ENV + platform_env)
        return subprocess.run(["bash", str(SCRIPT), "up"], env=self.env, capture_output=True, text=True)

    def docker_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def use_store(self, platform, acme):
        import sys
        sys.path.insert(0, str(SCRIPT.parent))
        import vars as v
        v.init(self.secrets, confirm=lambda prompt: "saved")
        with v.Store.open(self.secrets) as store:
            for key, value in {**TRAEFIK_SETTINGS, **platform}.items():
                store.set("platform", key, value, None, "set")
            for key, value in acme.items():
                store.set("platform:acme", key, value, None, "set")

    def run_platform(self, *arguments):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=self.env, capture_output=True, text=True)

    def test_given_a_store_when_up_then_compose_reads_rendered_files_and_last_good_is_saved(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps"}, {"CF_DNS_API_TOKEN": "abc"})
        result = self.run_platform("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"--env-file {self.secrets}/platform.env", self.docker_calls())
        last_good = self.secrets / "last-good"
        self.assertEqual((last_good / "platform.env").read_text(),
                         "ACME_DNS_PROVIDER='cloudflare'\nACME_EMAIL='ops@techcraftsmen.net'\n"
                         "COMPOSE_PROFILES='apps'\nDOMAIN='techcraftsmen.net'\nENVIRONMENTS='production'\n")
        self.assertEqual((last_good / "acme.env").read_text(), "CF_DNS_API_TOKEN='abc'\n")
        self.assertEqual((last_good / "platform.env").stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.geteuid() == 0 or os.access("/run/yggdrasil", os.W_OK), "this user writes /run/yggdrasil")
    def test_given_a_runtime_directory_when_a_user_runs_up_then_the_rendered_files_are_under_it(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps"}, {})
        runtime = self.temp / "runtime"
        runtime.mkdir(mode=0o700)
        self.env["XDG_RUNTIME_DIR"] = str(runtime)
        result = self.run_platform("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--env-file {runtime}/yggdrasil/platform.", self.docker_calls())

    def test_given_last_good_when_up_then_the_store_is_not_opened(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps"}, {})
        self.assertEqual(self.run_platform("up").returncode, 0)
        (self.secrets / "vars.key").unlink()
        result = self.run_platform("up", "--last-good")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("last-good", result.stdout + result.stderr)
        self.assertIn(f"--env-file {self.secrets}/last-good/platform.env", self.docker_calls())

    def test_given_last_good_without_the_key_when_down_ps_logs_or_config_then_compose_reads_the_copy(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps"}, {})
        self.assertEqual(self.run_platform("up").returncode, 0)
        (self.secrets / "vars.key").unlink()
        for arguments in (["down"], ["ps"], ["logs", "traefik"], ["config"]):
            self.log.unlink(missing_ok=True)
            result = self.run_platform(arguments[0], "--last-good", *arguments[1:])
            self.assertEqual(result.returncode, 0, f"{arguments}: {result.stderr}")
            calls = self.docker_calls()
            self.assertIn(f"--env-file {self.secrets}/last-good/platform.env", calls, arguments)
            self.assertIn(" ".join(arguments), calls.replace("logs --follow --tail 200", "logs"), arguments)

    def test_given_a_store_without_its_key_when_down_then_it_stops_suggesting_last_good(self):
        self.use_store({"ENVIRONMENTS": "production"}, {})
        (self.secrets / "vars.key").unlink()
        result = self.run_platform("down")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("platform.sh down --last-good", result.stderr)

    def test_given_a_store_without_its_key_when_up_then_it_stops_suggesting_last_good(self):
        self.use_store({"ENVIRONMENTS": "production"}, {})
        (self.secrets / "vars.key").unlink()
        result = self.run_platform("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--last-good", result.stderr)

    def test_given_a_broken_application_reference_when_up_then_the_platform_still_starts(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps"}, {})
        import vars as v
        with v.Store.open(self.secrets) as store:
            store.set("app:fortuna-api", "X", "${ref:heimdall-api:NOPE}", None, "set")
        result = self.run_platform("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_no_store_when_up_then_platform_env_with_a_notice(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("move to the variables store", result.stderr)

    def test_given_an_environment_of_the_catalog_when_up_then_compose_brings_it_up(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_several_environments_of_the_catalog_when_up_then_compose_brings_it_up(self):
        result = self.up("ENVIRONMENTS=development,homologation,production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_only_the_legacy_environment_when_up_then_it_counts_as_a_list_of_one(self):
        result = self.up("ENVIRONMENT=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_both_when_up_then_environments_wins(self):
        result = self.up("ENVIRONMENT=production\nENVIRONMENTS=production,nowhere\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'nowhere' (ENVIRONMENTS", result.stderr)

    def test_given_one_unknown_environment_among_several_when_up_then_it_is_refused(self):
        result = self.up("ENVIRONMENTS=development,staging\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'staging' (ENVIRONMENTS", result.stderr)
        self.assertIn("is not an environment in catalog.yaml", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_no_environment_when_up_then_it_is_refused(self):
        result = self.up("COMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ENVIRONMENTS must be set", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_an_environment_pattern_when_up_then_it_is_refused(self):
        # A regular expression that matches "production" is still not an environment id.
        result = self.up("ENVIRONMENTS='prod.*'\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not an environment in catalog.yaml", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def traefik_config_file(self):
        # The last call's: `up` checks the engine (`docker info`) before it publishes the live file.
        return re.findall(r"traefik-config-file: (\S+)", self.docker_calls())[-1]

    def test_given_an_env_file_when_up_then_compose_gets_a_rendered_traefik_config(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\nEXTRA_DOMAINS=artur-rios.tech\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        # Exported to Compose, in the secrets directory...
        self.assertIn(f"traefik-config-file: {self.secrets}/traefik.yml", calls)
        # ... and holding the rendered file, with the host's domain and the extra one.
        self.assertIn("main: techcraftsmen.net", calls)
        self.assertIn("main: artur-rios.tech", calls)
        # Still there afterwards: the running Traefik's bind mount needs it at every restart (a crash, the
        # engine's restart, a reboot), long after platform.sh is gone. Readable by this user only.
        path = pathlib.Path(self.traefik_config_file())
        self.assertIn("main: artur-rios.tech", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # No temporary file of the atomic write is left next to it.
        self.assertEqual(sorted(p.name for p in self.secrets.iterdir() if "traefik" in p.name), ["traefik.yml"])

    def test_given_two_runs_when_up_then_the_traefik_config_path_is_the_same(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        first = self.traefik_config_file()
        self.log.unlink()
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        # The same path, so Compose sees the same mount and doesn't recreate Traefik for nothing.
        self.assertEqual(self.traefik_config_file(), first)

    def config_hash(self):
        return re.search(r"YGG_TRAEFIK_CONFIG_HASH=(\S*)", self.docker_calls()).group(1)

    def test_given_a_changed_traefik_config_when_up_then_its_hash_for_compose_changes(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        first = self.config_hash()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.log.unlink()
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        # Unchanged settings, unchanged hash: Traefik is left running.
        self.assertEqual(self.config_hash(), first)
        self.log.unlink()
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\nEXTRA_DOMAINS=artur-rios.tech\n").returncode, 0)
        # The path stays, so only the hash (a label on the service) tells Compose to recreate Traefik.
        self.assertNotEqual(self.config_hash(), first)
        compose = (SCRIPT.parent.parent / "platform" / "compose.yml").read_text()
        self.assertIn("yggdrasil.traefik-config-hash: ${YGG_TRAEFIK_CONFIG_HASH:?", compose)

    def test_given_every_command_when_run_then_compose_gets_the_traefik_config(self):
        (self.secrets / "platform.env").write_text(TRAEFIK_ENV + "ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        for command in ("down", "ps", "logs", "config"):
            self.log.unlink(missing_ok=True)
            result = self.run_platform(command)
            self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
            self.assertIn("main: techcraftsmen.net", self.docker_calls(), command)

    def live_traefik_config(self):
        path = self.secrets / "traefik.yml"
        return path.read_text(), path.stat().st_ino, path.stat().st_mtime_ns

    def test_given_a_live_traefik_config_when_down_ps_logs_or_config_run_then_it_is_left_alone(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        live = self.live_traefik_config()
        # Settings changed since that up: only the next up may put them in front of the running Traefik.
        (self.secrets / "platform.env").write_text(
            TRAEFIK_ENV + "ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\nEXTRA_DOMAINS=artur-rios.tech\n")
        for arguments in (["down"], ["ps"], ["logs", "traefik"], ["config"]):
            self.log.unlink(missing_ok=True)
            result = self.run_platform(*arguments)
            self.assertEqual(result.returncode, 0, f"{arguments}: {result.stderr}")
            self.assertEqual(self.live_traefik_config(), live, arguments)
            # Compose still gets a configuration (it interpolates the whole file), rendered from the
            # current settings, but not the live one Traefik mounts.
            self.assertNotEqual(self.traefik_config_file(), str(self.secrets / "traefik.yml"), arguments)
            self.assertIn("main: artur-rios.tech", self.docker_calls(), arguments)
        # And nothing of those renders is left behind.
        self.assertEqual(sorted(p.name for p in self.secrets.iterdir() if "traefik" in p.name), ["traefik.yml"])

    def test_given_a_rootful_engine_when_up_then_the_live_traefik_config_is_untouched(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        live = self.live_traefik_config()
        self.env["FAKE_DOCKER_INFO"] = '["name=seccomp"]'
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\nEXTRA_DOMAINS=artur-rios.tech\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.live_traefik_config(), live)
        self.assertEqual(sorted(p.name for p in self.secrets.iterdir() if "traefik" in p.name), ["traefik.yml"])

    def test_given_root_when_any_command_runs_then_it_is_refused_before_touching_anything(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        live = self.live_traefik_config()
        # `sudo platform.sh …`: root's files in the secrets directory would be unreadable to the rootless
        # Traefik (its container root is ygg), so nothing may be written there.
        fake_id = pathlib.Path(self.env["PATH"].split(os.pathsep)[0]) / "id"
        fake_id.write_text('#!/usr/bin/env bash\nif [[ "$*" == -u ]]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
        fake_id.chmod(0o755)
        for arguments in (["up"], ["down"], ["ps"], ["logs"], ["config"], ["up", "--last-good"]):
            self.log.unlink(missing_ok=True)
            result = self.run_platform(*arguments)
            self.assertEqual(result.returncode, 1, arguments)
            self.assertIn("not as root", result.stderr, arguments)
            self.assertEqual(self.docker_calls(), "", arguments)
            self.assertEqual(self.live_traefik_config(), live, arguments)

    def test_given_cloudflare_ranges_when_proxied_then_the_rendered_config_trusts_them(self):
        (self.secrets / "cloudflare-ips").write_text("173.245.48.0/20\n2400:cb00::/32\n")
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\nCLOUDFLARE_PROXIED=true\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertIn("173.245.48.0/20", calls)
        self.assertIn("2400:cb00::/32", calls)

    def test_given_proxied_without_the_ranges_when_up_then_it_is_refused_before_compose_runs(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\nCLOUDFLARE_PROXIED=true\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cloudflare-ips", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_last_good_when_up_from_a_store_then_the_traefik_config_is_kept_too(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps", "EXTRA_DOMAINS": "artur-rios.tech"}, {})
        result = self.run_platform("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = self.secrets / "last-good" / "traefik.yml"
        self.assertIn("main: artur-rios.tech", kept.read_text())
        self.assertEqual(kept.stat().st_mode & 0o777, 0o600)

    def test_given_last_good_when_run_then_compose_gets_the_kept_traefik_config_not_a_new_one(self):
        self.use_store({"ENVIRONMENTS": "production", "COMPOSE_PROFILES": "apps", "EXTRA_DOMAINS": "artur-rios.tech"}, {})
        self.assertEqual(self.run_platform("up").returncode, 0)
        (self.secrets / "vars.key").unlink()
        self.log.unlink()
        result = self.run_platform("config", "--last-good")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertIn(f"traefik-config-file: {self.secrets}/last-good/traefik.yml", calls)
        self.assertIn("main: artur-rios.tech", calls)

    def test_given_an_env_file_when_up_then_no_traefik_config_is_kept_without_a_store(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        self.assertFalse((self.secrets / "last-good").exists())

    def test_given_docker_host_when_up_then_compose_mounts_that_socket(self):
        self.env["DOCKER_HOST"] = "unix:///run/user/1001/docker.sock"
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertIn("DOCKER_SOCKET=/run/user/1001/docker.sock\n", calls)
        self.assertNotIn("DOCKER_SOCKET=\n", calls)
        # The engine that was checked is the one Compose talks to.
        self.assertNotIn("DOCKER_HOST=unix:///run/user/1000", calls)

    def test_given_no_docker_host_when_the_runtime_directory_has_the_socket_then_compose_mounts_it(self):
        import socket
        runtime = self.temp / "runtime"
        runtime.mkdir(mode=0o700)
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(runtime / "docker.sock"))
        del self.env["DOCKER_HOST"]
        self.env["XDG_RUNTIME_DIR"] = str(runtime)
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"DOCKER_SOCKET={runtime}/docker.sock\n", self.docker_calls())
        self.assertIn(f"DOCKER_HOST=unix://{runtime}/docker.sock\n", self.docker_calls())

    def test_given_a_rootful_engine_when_up_then_it_is_refused(self):
        self.env["FAKE_DOCKER_INFO"] = '["name=seccomp"]'
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runs as root", result.stderr)
        self.assertIn("/run/user/1000/docker.sock", result.stderr)
        self.assertNotIn("docker compose", self.docker_calls())

    def test_given_a_rootful_engine_when_any_command_runs_then_it_is_refused(self):
        self.env["FAKE_DOCKER_INFO"] = '["name=seccomp"]'
        (self.secrets / "platform.env").write_text(TRAEFIK_ENV + "ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        for command in ("down", "ps", "logs", "config"):
            result = self.run_platform(command)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn("runs as root", result.stderr, command)
        self.assertNotIn("docker compose", self.docker_calls())

    def test_given_no_rootless_socket_when_up_then_it_names_ygg_install_and_never_falls_back(self):
        runtime = self.temp / "runtime"
        runtime.mkdir(mode=0o700)
        del self.env["DOCKER_HOST"]
        self.env["XDG_RUNTIME_DIR"] = str(runtime)
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no rootless Docker engine for", result.stderr)
        self.assertIn("run 'ygg install'", result.stderr)
        self.assertNotIn("/var/run/docker.sock", self.docker_calls())
        self.assertNotIn("docker compose", self.docker_calls())

    def test_given_environments_without_the_apps_profile_when_up_then_it_says_to_add_apps(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=jenkins\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ENVIRONMENTS is set but COMPOSE_PROFILES lacks apps", result.stderr)
        self.assertNotIn("docker compose", self.docker_calls())

    def test_given_the_dev_profiles_without_environments_when_up_then_it_starts(self):
        result = self.up("COMPOSE_PROFILES=forge,jenkins\nJENKINS_URL=https://jenkins.example.com/\n"
                         "JENKINS_ADMIN_PASSWORD=x\nGITHUB_APP_ID=1\nGITHUB_APP_KEY_FILE=/etc/yggdrasil/github-app.pem\n"
                         "YGGDRASIL_LIBRARY_VERSION=v1.0.0\n" + self.WORKDIRS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())
        self.assertIn("PROMETHEUS_CONFIG_FILE=prometheus-static.yml\n", self.docker_calls())

    WORKDIRS = "CI_AGENT_WORKDIR=/home/ci/agent\nRELEASE_AGENT_WORKDIR=/home/release/agent\n"
    JENKINS = ("COMPOSE_PROFILES=forge,jenkins\nJENKINS_URL=https://jenkins.example.com/\nJENKINS_ADMIN_PASSWORD=x\n"
               "YGGDRASIL_LIBRARY_VERSION=v1.0.0\n" + WORKDIRS)
    GITHUB_READ = "GITHUB_READ_APP_ID=2\nGITHUB_READ_APP_KEY_FILE=/etc/yggdrasil/github-read.pem\n"
    GITHUB_APP = "GITHUB_APP_ID=1\nGITHUB_APP_KEY_FILE=/etc/yggdrasil/github-app.pem\n"
    FORGE_FILES = ("FORGE_WRITE_TOKEN_FILE=/etc/yggdrasil/forge-write-token\n"
                   "FORGE_READ_TOKEN_FILE=/etc/yggdrasil/forge-read-token\n"
                   "FORGE_WEBHOOK_SECRET_FILE=/etc/yggdrasil/forge-webhook-secret\n")

    def use_catalog(self, forge_block):
        """Points the tooling at a copy of the catalog with this forge/mirror block (YAML text) in place of
        its own (none: a GitHub installation's, the default every test starts from)."""
        path = self.temp / "catalog.yaml"
        path.write_text(github_catalog() + "\n" + forge_block)
        self.env["YGG_CATALOG"] = str(path)

    # update-plugins

    OLD_PLUGINS = "git 5.1\njunit 1300\ntimestamper 1.27\n"
    NEW_PLUGINS = "git 5.2\njunit 1300\nprometheus 800\n"

    def use_jenkins_images(self, old, new, old_plugins, new_plugins, running="abc123\n", busy="0\n"):
        """The fake engine's controller image: `old` is tagged now ("" for none), a build tags `new`; each one's
        plugins; the running controller's container ("" for none); and the builds blocking a restart that each
        look reports, in turn (the last one again once they run out)."""
        state = self.temp / "state"
        (state / "tags").mkdir(parents=True)
        if old:
            (state / "tags" / "yggdrasil_jenkins_local").write_text(old + "\n")
        (state / "built").write_text(new + "\n")
        for image, plugins in ((old, old_plugins), (new, new_plugins)):
            if image:
                (state / f"plugins-{image}").write_text(plugins)
        (state / "running").write_text(running)
        (state / "busy").write_text(busy)
        self.env.update(FAKE_STATE=str(state), YGG_JENKINS_QUIET_POLL="0")
        return state

    def update_plugins(self, *options, platform_env=None):
        (self.secrets / "platform.env").write_text(TRAEFIK_ENV + (platform_env or self.JENKINS + self.GITHUB_APP))
        return self.run_platform("update-plugins", *options)

    def refreshed_stamp(self):
        path = self.secrets / "jenkins-plugins-refreshed"
        return path.read_text().strip() if path.exists() else None

    def test_given_changed_plugins_when_update_plugins_then_it_waits_for_the_builds_and_brings_jenkins_up(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.NEW_PLUGINS, busy="2\n0\n")
        result = self.update_plugins()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:4], ["Jenkins plugins that change:", "  git: 5.1 -> 5.2",
                                                          "  prometheus: new, 800",
                                                          "  timestamper: removed (was 1.27)"])
        self.assertIn("waiting for 2 build(s) that cannot survive a restart", result.stderr)
        calls = self.docker_calls()
        build, quiet, up = calls.index("build jenkins"), calls.index("jenkins-cli quiet-down"), calls.index("up --detach")
        self.assertLess(build, quiet)
        self.assertLess(quiet, up)
        self.assertNotIn("cancel-quiet-down", calls)
        stamp = self.refreshed_stamp()
        self.assertRegex(stamp, r"^\d{8}T\d{6}Z$")
        # The build that fetched them and the up after it both pass the new value.
        self.assertIn(f"YGG_JENKINS_PLUGINS_REFRESHED={stamp}\n", calls[build:quiet])
        self.assertIn(f"YGG_JENKINS_PLUGINS_REFRESHED={stamp}\n", calls[up:])
        self.assertEqual(self.tags(), {"yggdrasil_jenkins_local": "sha:new"})

    def tags(self):
        """The fake engine's tags of the controller's image, and the image each one names."""
        return {p.name: p.read_text().strip() for p in (self.temp / "state" / "tags").iterdir()}

    def test_given_changed_plugins_when_update_plugins_then_the_old_image_is_kept_under_a_tag_during_the_build(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.NEW_PLUGINS)
        self.assertEqual(self.update_plugins().returncode, 0)
        calls = self.docker_calls()
        self.assertLess(calls.index("docker tag yggdrasil/jenkins:local yggdrasil/jenkins:previous"),
                        calls.index("build jenkins"))
        self.assertIn("docker image rm yggdrasil/jenkins:previous", calls)

    def test_given_no_change_when_update_plugins_then_jenkins_keeps_running_on_its_image(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.OLD_PLUGINS)
        result = self.update_plugins()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already up to date", result.stderr)
        calls = self.docker_calls()
        self.assertNotIn("quiet-down", calls)
        self.assertNotIn("up --detach", calls)
        self.assertEqual(self.tags(), {"yggdrasil_jenkins_local": "sha:old"})
        self.assertIsNone(self.refreshed_stamp())

    def test_given_now_when_update_plugins_then_it_does_not_wait(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.NEW_PLUGINS, busy="3\n")
        result = self.update_plugins("--now")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertNotIn("quiet-down", calls)
        self.assertIn("up --detach", calls)
        self.assertIsNotNone(self.refreshed_stamp())

    def test_given_builds_that_outlast_the_wait_when_update_plugins_then_nothing_changes(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.NEW_PLUGINS, busy="1\n")
        self.env["YGG_JENKINS_QUIET_TIMEOUT"] = "0"
        (self.secrets / "jenkins-plugins-refreshed").write_text("20261001T000000Z\n")
        result = self.update_plugins()
        self.assertEqual(result.returncode, 1)
        self.assertIn("still running", result.stderr)
        calls = self.docker_calls()
        self.assertIn("jenkins-cli cancel-quiet-down", calls)
        self.assertNotIn("up --detach", calls)
        # Tagged back and the stamp kept: the next up builds the image Jenkins runs, from its cached layer.
        self.assertEqual(self.tags(), {"yggdrasil_jenkins_local": "sha:old"})
        self.assertEqual(self.refreshed_stamp(), "20261001T000000Z")

    def test_given_jenkins_answers_no_number_when_update_plugins_then_it_stops_and_cancels_the_quiet_down(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.NEW_PLUGINS, busy="ERROR: no\n")
        result = self.update_plugins()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Jenkins answered 'ERROR: no'", result.stderr)
        self.assertIn("jenkins-cli cancel-quiet-down", self.docker_calls())
        self.assertNotIn("up --detach", self.docker_calls())
        self.assertEqual(self.tags(), {"yggdrasil_jenkins_local": "sha:old"})

    def test_given_jenkins_not_running_when_update_plugins_then_it_brings_it_up_without_waiting(self):
        self.use_jenkins_images("sha:old", "sha:new", self.OLD_PLUGINS, self.NEW_PLUGINS, running="")
        result = self.update_plugins()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("quiet-down", self.docker_calls())
        self.assertIn("up --detach", self.docker_calls())

    def test_given_no_image_yet_when_update_plugins_then_it_builds_one_and_brings_jenkins_up(self):
        self.use_jenkins_images("", "sha:new", "", self.NEW_PLUGINS, running="")
        result = self.update_plugins()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("plugins that change", result.stdout)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_a_host_without_jenkins_when_update_plugins_then_it_is_refused(self):
        result = self.update_plugins(platform_env="COMPOSE_PROFILES=forge\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not run Jenkins", result.stderr)
        self.assertNotIn("build jenkins", self.docker_calls())

    # The jobs a recreated controller has on disk but did not load. Configuration as Code creates a new catalog
    # entry's job during the controller's startup, after Jenkins listed the job directories it loads, and core
    # then drops every item it did not load from disk: the job is back only at the next start. So once `up`
    # gave Jenkins a new container, it reloads Configuration as Code if a job directory has no job.

    def use_controller(self, before, after, *answers):
        """The fake engine's controller container before `up` and after it ("" for none), and what the
        registration script answers, in turn (the last one again once they run out; FAIL fails the call)."""
        state = self.temp / "state"
        (state / "tags").mkdir(parents=True, exist_ok=True)
        (state / "running").write_text(before)
        (state / "running-after").write_text(after)
        (state / "busy").write_text("0\n")
        for number, answer in enumerate(answers, 1):
            (state / f"register.{number}").write_text(answer)
        self.env.update(FAKE_STATE=str(state), YGG_JENKINS_START_POLL="0")

    def test_given_a_recreated_controller_with_unloaded_jobs_when_up_then_it_reloads_and_names_them(self):
        self.use_controller("old\n", "new\n", "ready\nregistered ballz\nregistered tabnews\n")
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_calls().count("register-jobs"), 1)
        self.assertIn("platform: Jenkins loaded the new job ballz", result.stderr)
        self.assertIn("platform: Jenkins loaded the new job tabnews", result.stderr)
        # After the stack is up, never before: it needs the new controller.
        calls = self.docker_calls()
        self.assertLess(calls.index("up --detach"), calls.index("register-jobs"))

    def test_given_a_recreated_controller_with_every_job_loaded_when_up_then_it_says_nothing(self):
        self.use_controller("old\n", "new\n", "ready\n")
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_calls().count("register-jobs"), 1)
        self.assertNotIn("new job", result.stderr)

    def test_given_the_same_controller_when_up_then_its_jobs_are_left_alone(self):
        self.use_controller("same\n", "same\n", "ready\nregistered ballz\n")
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("register-jobs", self.docker_calls())

    def test_given_a_controller_still_starting_when_up_then_it_waits_until_jenkins_is_ready(self):
        # Starting: the CLI is refused (FAIL) or Jenkins has not reached COMPLETED (starting).
        self.use_controller("", "new\n", "FAIL", "starting\n", "ready\nregistered ballz\n")
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_calls().count("register-jobs"), 3)
        self.assertIn("platform: Jenkins loaded the new job ballz", result.stderr)

    def test_given_a_job_still_unloaded_after_the_reload_when_up_then_it_warns_and_the_platform_stays_up(self):
        self.use_controller("old\n", "new\n", "ready\nunregistered broken-job\n")
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("broken-job", result.stderr)
        self.assertIn("docker restart", result.stderr)

    def test_given_a_controller_that_does_not_start_in_time_when_up_then_it_warns_and_the_platform_stays_up(self):
        self.use_controller("old\n", "new\n", "starting\n")
        self.env["YGG_JENKINS_START_TIMEOUT"] = "0"
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_calls().count("register-jobs"), 1)
        self.assertIn("could not check that Jenkins loaded every job", result.stderr)

    def test_given_a_host_without_jenkins_when_up_then_no_job_is_looked_for(self):
        self.use_controller("old\n", "new\n", "ready\nregistered ballz\n")
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("register-jobs", self.docker_calls())
        self.assertNotIn("ps --quiet jenkins", self.docker_calls())

    def test_given_a_saved_stamp_when_up_then_the_controller_is_built_with_it(self):
        (self.secrets / "jenkins-plugins-refreshed").write_text("20261101T120000Z\n")
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("YGG_JENKINS_PLUGINS_REFRESHED=20261101T120000Z\n", self.docker_calls())

    def test_given_now_on_another_command_then_it_is_a_usage_error(self):
        self.assertEqual(self.run_platform("up", "--now").returncode, 1)

    def test_given_the_jenkins_profile_without_a_library_version_when_up_then_it_is_refused(self):
        result = self.up("COMPOSE_PROFILES=forge,jenkins\nJENKINS_URL=https://jenkins.example.com/\n"
                         "JENKINS_ADMIN_PASSWORD=x\n" + self.GITHUB_APP)
        self.assertEqual(result.returncode, 1)
        self.assertIn("YGGDRASIL_LIBRARY_VERSION must be set", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_a_github_catalog_when_up_then_jenkins_reads_the_github_configuration_and_the_app(self):
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("YGG_CASC_CONFIG=/etc/jenkins/casc.yaml,/etc/jenkins/casc.github.yaml,"
                      "/etc/jenkins/casc.github-app.yaml\n", self.docker_calls())

    def test_given_a_github_catalog_without_the_read_only_app_when_up_then_its_file_is_not_read(self):
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertNotIn("casc.github-read.yaml", calls)
        # No key to mount: /dev/null, not a path Docker would create as a directory.
        self.assertIn("GITHUB_READ_APP_KEY_FILE=/dev/null\n", calls)

    def test_given_a_github_catalog_with_the_read_only_app_when_up_then_jenkins_reads_its_credential(self):
        result = self.up(self.JENKINS + self.GITHUB_APP + self.GITHUB_READ)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertIn("YGG_CASC_CONFIG=/etc/jenkins/casc.yaml,/etc/jenkins/casc.github.yaml,"
                      "/etc/jenkins/casc.github-app.yaml,/etc/jenkins/casc.github-read.yaml\n", calls)
        self.assertNotIn("GITHUB_READ_APP_KEY_FILE=/dev/null", calls)

    def test_given_half_of_the_read_only_app_when_up_then_it_is_refused(self):
        for line in self.GITHUB_READ.splitlines():
            result = self.up(self.JENKINS + self.GITHUB_APP + line + "\n")
            self.assertEqual(result.returncode, 1, line)
            self.assertIn("set both GITHUB_READ_APP_ID and GITHUB_READ_APP_KEY_FILE", result.stderr)
            self.assertEqual(self.docker_calls(), "")

    def test_given_no_nuget_key_when_up_then_jenkins_does_not_read_its_configuration(self):
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertNotIn("casc.nuget.yaml", calls)
        # No key to mount: /dev/null, not a path Docker would create as a directory.
        self.assertIn("NUGET_API_KEY_FILE=/dev/null\n", calls)

    def test_given_a_nuget_key_file_when_up_then_jenkins_reads_the_nuget_credential(self):
        for forge_block, files in (("", self.GITHUB_APP),
                                   ("forge:\n  type: forgejo\n  url: https://git.example.com\n", self.FORGE_FILES)):
            if forge_block:
                self.use_catalog(forge_block)
            self.log.unlink(missing_ok=True)
            result = self.up(self.JENKINS + files + "NUGET_API_KEY_FILE=/etc/yggdrasil/nuget-api-key\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = self.docker_calls()
            self.assertRegex(calls, r"YGG_CASC_CONFIG=/etc/jenkins/casc\.yaml,[^\n]*,/etc/jenkins/casc\.nuget\.yaml\n")
            # Compose reads the setting from the env file itself; platform.sh leaves it alone.
            self.assertNotIn("NUGET_API_KEY_FILE=/dev/null", calls)

    def credentials_dir(self, **files):
        directory = self.temp / "jenkins-credentials"
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
        for name, value in files.items():
            path = directory / name.replace("_", "-")
            path.write_text(value)
            path.chmod(0o644)
        return directory

    def test_given_no_credentials_directory_when_up_then_jenkins_reads_no_repository_credentials(self):
        result = self.up(self.JENKINS + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertNotIn("casc.credentials.yaml", calls)
        # Nothing to mount: /dev/null, not a path Docker would create as a directory.
        self.assertIn("JENKINS_CREDENTIALS_DIR=/dev/null\n", calls)
        self.assertIn("YGG_JENKINS_CREDENTIALS_FILE=/dev/null\n", calls)

    def test_given_a_credentials_directory_when_up_then_jenkins_reads_them_from_a_file_kept_with_the_secrets(self):
        # The controller replaces its system credentials with the configured ones at every start, so a credential
        # made in the UI is gone after the next up: the repositories' are configured from their files instead.
        directory = self.credentials_dir(reportgenerator_license="the-key")
        result = self.up(self.JENKINS + self.GITHUB_APP + f"JENKINS_CREDENTIALS_DIR={directory}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertRegex(calls, r"YGG_CASC_CONFIG=/etc/jenkins/casc\.yaml,[^\n]*,/etc/jenkins/casc\.credentials\.yaml\n")
        live = self.secrets / "jenkins-credentials.yaml"
        self.assertIn(f"YGG_JENKINS_CREDENTIALS_FILE={live}\n", calls)
        self.assertNotIn("JENKINS_CREDENTIALS_DIR=/dev/null", calls)
        self.assertIn('id: "reportgenerator-license"', live.read_text())
        self.assertNotIn("the-key", live.read_text())
        # The controller reads it as "other", like the secrets themselves.
        self.assertEqual(live.stat().st_mode & 0o777, 0o644)
        self.assertRegex(calls, r"YGG_JENKINS_CREDENTIALS_HASH=[0-9a-f]{64}\n")

    def test_given_a_changed_credential_when_up_then_the_hash_changes_so_jenkins_is_recreated(self):
        # The rendered file only names the ids: a new secret under the same id changes nothing in it, so the hash
        # also covers each file's size and modification time (never its content).
        directory = self.credentials_dir(reportgenerator_license="old")
        settings = self.JENKINS + self.GITHUB_APP + f"JENKINS_CREDENTIALS_DIR={directory}\n"
        self.assertEqual(self.up(settings).returncode, 0)
        first = re.search(r"YGG_JENKINS_CREDENTIALS_HASH=(\w+)", self.docker_calls()).group(1)
        path = directory / "reportgenerator-license"
        path.write_text("a newer key")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        self.log.unlink()
        self.assertEqual(self.up(settings).returncode, 0)
        second = re.search(r"YGG_JENKINS_CREDENTIALS_HASH=(\w+)", self.docker_calls()).group(1)
        self.assertNotEqual(first, second)

    def jenkins_config_hash(self):
        return re.search(r"YGG_JENKINS_CONFIG_HASH=(\S*)", self.docker_calls()).group(1)

    def test_given_the_jenkins_profile_when_up_twice_then_the_controller_config_hash_is_the_same(self):
        settings = self.JENKINS + self.GITHUB_APP
        self.assertEqual(self.up(settings).returncode, 0)
        first = self.jenkins_config_hash()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.log.unlink()
        self.assertEqual(self.up(settings).returncode, 0)
        # Nothing changed in the checkout: the controller is left running.
        self.assertEqual(self.jenkins_config_hash(), first)

    def test_given_a_changed_casc_file_when_up_then_the_controller_config_hash_changes(self):
        # The controller reads its Configuration as Code only when it starts, from files mounted at fixed paths:
        # only the hash (a label on the service) tells Compose that a pulled casc.yaml needs a new controller.
        # A copy of the checkout, so the change does not touch this one.
        checkout = self.temp / "checkout"
        for part in ("scripts", "platform", "jenkins"):
            shutil.copytree(SCRIPT.parent.parent / part, checkout / part,
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(SCRIPT.parent.parent / "catalog.yaml", checkout / "catalog.yaml")
        script = checkout / "scripts" / "platform.sh"
        (self.secrets / "platform.env").write_text(TRAEFIK_ENV + self.JENKINS + self.GITHUB_APP)

        def up():
            result = subprocess.run(["bash", str(script), "up"], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return self.jenkins_config_hash()

        first = up()
        casc = checkout / "platform" / "jenkins" / "controller" / "casc.yaml"
        casc.write_text(casc.read_text() + "# changed\n")
        self.log.unlink()
        self.assertNotEqual(up(), first)

    def test_given_a_credential_the_catalog_does_not_name_when_up_then_it_stops_before_docker(self):
        directory = self.credentials_dir(reportgenerator_licence="the-key")
        result = self.up(self.JENKINS + self.GITHUB_APP + f"JENKINS_CREDENTIALS_DIR={directory}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("reportgenerator-licence", result.stderr)
        self.assertEqual(self.docker_calls(), "")
        self.assertFalse((self.secrets / "jenkins-credentials.yaml").exists())

    def test_given_a_forgejo_catalog_when_up_then_the_read_only_github_app_is_not_configured(self):
        # Forgejo's ci identity is forge-read; github-read is a GitHub installation's.
        self.use_catalog("forge:\n  type: forgejo\n  url: https://git.example.com\n")
        result = self.up(self.JENKINS + self.FORGE_FILES + self.GITHUB_READ)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("casc.github-read.yaml", self.docker_calls())

    def test_given_the_jenkins_profile_without_the_build_agents_work_directories_when_up_then_it_is_refused(self):
        for name in ("CI_AGENT_WORKDIR", "RELEASE_AGENT_WORKDIR"):
            settings = "".join(line + "\n" for line in self.JENKINS.splitlines() if not line.startswith(name + "="))
            result = self.up(settings + self.GITHUB_APP)
            self.assertEqual(result.returncode, 1, name)
            self.assertIn(f"{name} must be set", result.stderr)
            self.assertEqual(self.docker_calls(), "")

    def test_given_a_relative_build_agent_work_directory_when_up_then_it_is_refused(self):
        result = self.up(self.JENKINS + self.GITHUB_APP + "RELEASE_AGENT_WORKDIR=agent\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("RELEASE_AGENT_WORKDIR in", result.stderr)
        self.assertIn("must be an absolute path", result.stderr)
        self.assertEqual(self.docker_calls(), "")

    def test_given_a_github_catalog_without_the_app_when_up_then_it_is_refused(self):
        result = self.up(self.JENKINS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("GITHUB_APP_ID must be set", result.stderr)

    def test_given_a_forgejo_catalog_when_up_then_jenkins_reads_the_forgejo_configuration_without_the_app(self):
        self.use_catalog("forge:\n  type: forgejo\n  url: https://git.example.com/\n  jenkinsUser: ygg-jenkins\n")
        result = self.up(self.JENKINS + self.FORGE_FILES)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        self.assertIn("YGG_CASC_CONFIG=/etc/jenkins/casc.yaml,/etc/jenkins/casc.forgejo.yaml\n", calls)
        self.assertIn("FORGE_URL=https://git.example.com\n", calls)
        self.assertIn("FORGE_JENKINS_USER=ygg-jenkins\n", calls)

    def test_given_a_forgejo_catalog_with_a_mirror_when_up_then_the_github_app_is_configured_and_required(self):
        self.use_catalog("forge:\n  type: forgejo\n  url: https://git.example.com\nmirror:\n  github: someone\n")
        refused = self.up(self.JENKINS + self.FORGE_FILES)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("GITHUB_APP_ID must be set", refused.stderr)
        result = self.up(self.JENKINS + self.FORGE_FILES + self.GITHUB_APP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("YGG_CASC_CONFIG=/etc/jenkins/casc.yaml,/etc/jenkins/casc.forgejo.yaml,"
                      "/etc/jenkins/casc.github-app.yaml\n", self.docker_calls())

    def test_given_a_forgejo_catalog_without_token_files_when_up_then_it_is_refused(self):
        self.use_catalog("forge:\n  type: forgejo\n  url: https://git.example.com\n")
        for name in ("FORGE_WRITE_TOKEN_FILE", "FORGE_READ_TOKEN_FILE", "FORGE_WEBHOOK_SECRET_FILE"):
            lines = [line for line in self.FORGE_FILES.splitlines() if not line.startswith(name + "=")]
            result = self.up(self.JENKINS + "\n".join(lines) + "\n")
            self.assertEqual(result.returncode, 1, name)
            self.assertIn(f"{name} must be set", result.stderr)
            self.assertEqual(self.docker_calls(), "")

    def test_given_the_forge_profile_when_up_then_no_jenkins_or_agent_variable_is_required(self):
        result = self.up("COMPOSE_PROFILES=forge\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())

    def test_given_the_apps_profile_when_up_then_prometheus_discovers_its_targets(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROMETHEUS_CONFIG_FILE=prometheus.yml\n", self.docker_calls())

    def test_given_up_when_run_then_the_deploy_locks_are_private_to_the_user(self):
        self.assertEqual(self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps\n").returncode, 0)
        self.assertEqual((self.secrets / "locks").stat().st_mode & 0o7777, 0o700)


    def test_given_the_agent_profile_without_its_variables_when_up_then_it_is_refused(self):
        result = self.up("ENVIRONMENTS=production\nCOMPOSE_PROFILES=apps,agent\nJENKINS_URL=https://jenkins.example.com/\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("JENKINS_AGENT_NAME must be set", result.stderr)
        self.assertEqual(self.docker_calls(), "")


if __name__ == "__main__":
    unittest.main()
