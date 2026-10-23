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
                         "JENKINS_ADMIN_PASSWORD=x\nGITHUB_APP_ID=1\nGITHUB_APP_KEY_FILE=/etc/yggdrasil/github-app.pem\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up --detach", self.docker_calls())
        self.assertIn("PROMETHEUS_CONFIG_FILE=prometheus-static.yml\n", self.docker_calls())

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
