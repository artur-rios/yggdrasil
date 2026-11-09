"""Tests for scripts/security-signals.sh (Heimdall's security signal job and its heartbeat) and its systemd units.

    python3 -m unittest discover -s scripts -p "test_security_signals_job.py"

`docker` and `curl` are fakes on PATH. `docker` logs its arguments and, for `run`, the values the container would get
by name; $FAKE_RUN_STATUS is the run's exit status, and it prints what the job prints when it cannot deliver. `curl`
logs its arguments and the configuration it reads on stdin; $FAKE_CURL_STATUS is its exit status. The machine step
that installs the units is tested in test_machine.py.
"""

import hashlib
import os
import pathlib
import re
import shutil
import socket as sockets
import subprocess
import tempfile
import textwrap
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
SCRIPT = SCRIPTS / "security-signals.sh"
UNITS = ROOT / "platform" / "systemd"
TEMPLATE = ROOT / "env" / "security-signals.env.example"
JOB = ROOT / "platform" / "heimdall-signals" / "security_signals.py"

KEY = "k3y.Abc-123_xyz"
ALERT_TO = "owner@example.com"
PING = "https://hc-ping.example.com/1234-uuid"
HELD_SIGNAL = "SECURITY_SIGNAL REPEATED_REFUSALS actor 3f2a91c4"

FAKES = {
    "docker": r"""
        echo "docker $*" >>"$FAKE_LOG"
        case "$1" in
          info) echo "${FAKE_SECURITY:-[\"name=seccomp,profile=builtin\",\"name=rootless\"]}" ;;
          run)
            for name in HEIMDALL_SIGNAL_HERMES_API_KEY HEIMDALL_SIGNAL_ALERT_TO HERMES_BASE_URL HEIMDALL_SIGNAL_HEARTBEAT_URL; do
              echo "$name=${!name:-}" >>"$FAKE_LOG.container"
            done
            status=${FAKE_RUN_STATUS:-0}
            ((status == 0)) || echo "SECURITY_SIGNAL REPEATED_REFUSALS actor 3f2a91c4" >&2
            exit "$status"
            ;;
        esac
    """,
    "curl": r"""
        echo "curl $*" >>"$FAKE_LOG"
        cat >>"$FAKE_LOG.curl"
        exit "${FAKE_CURL_STATUS:-0}"
    """,
}

SETTINGS = f"""\
# the owner's file
HEIMDALL_SIGNAL_HERMES_API_KEY={KEY}
HEIMDALL_SIGNAL_ALERT_TO={ALERT_TO}
HEIMDALL_SIGNAL_HEARTBEAT_URL={PING}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.log = self.temp / "log"
        self.secrets = self.temp / "secrets"
        self.secrets.mkdir()
        self.runtime = self.temp / "run"
        self.runtime.mkdir()
        self.socket = self.make_socket()
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
            path.chmod(0o755)
        self.settings(SETTINGS)

    def make_socket(self):
        path = self.runtime / "docker.sock"
        server = sockets.socket(sockets.AF_UNIX)
        server.bind(str(path))
        self.addCleanup(server.close)
        return path

    def settings(self, text, environment="production"):
        (self.secrets / f"security-signals-{environment}.env").write_text(text)

    def run_job(self, *args, script=SCRIPT, **env):
        return subprocess.run(
            ["bash", str(script), *(args or ("run", "production"))],
            capture_output=True, text=True,
            env={
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "HOME": str(self.temp),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "YGG_SECRETS_DIR": str(self.secrets),
                "FAKE_LOG": str(self.log),
                **env,
            },
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def runs(self):
        return [c for c in self.calls() if c.startswith("docker run")]

    def pings(self):
        path = pathlib.Path(f"{self.log}.curl")
        return path.read_text().splitlines() if path.exists() else []

    def assert_pinged(self, status):
        self.assertEqual(self.pings(), [f'url = "{PING}/{status}"'])


class TheRun(Base):
    def test_given_good_settings_when_run_then_the_container_is_the_documented_one(self):
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        [run] = self.runs()
        self.assertIn("--rm", run)
        self.assertIn("--network edge", run)
        self.assertIn("--volumes-from heimdall-api-production-api-1", run)
        self.assertIn("-e HEIMDALL_LOG_DIRECTORY=/app/logs", run)
        self.assertIn(f"-v {ROOT}/platform/heimdall-signals:/signals:ro", run)
        self.assertRegex(run, r" python:\d+\.\d+\.\d+-slim-bookworm@sha256:[a-f0-9]{64} ")
        self.assertTrue(run.endswith("python3 /signals/security_signals.py --quiet"), run)

    def test_given_an_environment_when_run_then_its_names_follow_it(self):
        self.settings(SETTINGS, "homologation")
        self.assertEqual(self.run_job("run", "homologation").returncode, 0)
        self.assertIn("--volumes-from heimdall-api-homologation-api-1", self.runs()[0])
        self.assertIn("HERMES_BASE_URL=http://hermes-api.homologation:8080",
                      pathlib.Path(f"{self.log}.container").read_text())

    def test_given_good_settings_when_run_then_secrets_reach_the_container_by_name_only(self):
        self.run_job()
        [run] = self.runs()
        for name in ("HEIMDALL_SIGNAL_ALERT_TO", "HERMES_BASE_URL", "HEIMDALL_SIGNAL_HERMES_API_KEY"):
            self.assertRegex(run, rf"-e {name}( |$)")
        self.assertNotIn(KEY, run)
        self.assertNotIn(ALERT_TO, run)
        given = pathlib.Path(f"{self.log}.container").read_text().splitlines()
        self.assertIn(f"HEIMDALL_SIGNAL_HERMES_API_KEY={KEY}", given)
        self.assertIn(f"HEIMDALL_SIGNAL_ALERT_TO={ALERT_TO}", given)
        self.assertIn("HERMES_BASE_URL=http://hermes-api.production:8080", given)

    def test_given_good_settings_when_run_then_the_container_never_gets_the_ping_url(self):
        self.run_job()
        self.assertIn("HEIMDALL_SIGNAL_HEARTBEAT_URL=", pathlib.Path(f"{self.log}.container").read_text().splitlines())
        self.assertNotIn(PING, self.runs()[0])

    def test_given_no_rootless_engine_when_run_then_nothing_runs_and_the_heartbeat_hears(self):
        result = self.run_job(FAKE_SECURITY='["name=seccomp,profile=builtin"]')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.runs(), [])
        self.assert_pinged(3)


class TheHeartbeat(Base):
    def test_given_a_delivered_run_when_it_ends_then_the_heartbeat_gets_zero(self):
        self.assertEqual(self.run_job().returncode, 0)
        self.assert_pinged(0)

    def test_given_any_failing_job_when_it_ends_then_the_heartbeat_gets_its_status_and_it_is_the_exit(self):
        for status in (1, 2, 125, 126, 127):
            with self.subTest(status=status):
                pathlib.Path(f"{self.log}.curl").unlink(missing_ok=True)
                result = self.run_job(FAKE_RUN_STATUS=str(status))
                self.assertEqual(result.returncode, status)
                self.assert_pinged(status)

    def test_given_a_failed_run_when_it_ends_then_the_held_signals_stay_in_the_journal(self):
        result = self.run_job(FAKE_RUN_STATUS="1")
        self.assertIn(HELD_SIGNAL, result.stderr)
        sent = "\n".join(self.pings()) + "\n".join(c for c in self.calls() if c.startswith("curl"))
        self.assertNotIn(HELD_SIGNAL, sent)
        self.assertNotIn("3f2a91c4", sent)

    def test_given_a_ping_when_sent_then_its_url_is_not_an_argument(self):
        self.run_job()
        [curl] = [c for c in self.calls() if c.startswith("curl")]
        self.assertNotIn(PING, curl)
        self.assertIn("--config -", curl)
        self.assertIn("--max-time", curl)

    def test_given_a_url_with_a_trailing_slash_when_pinged_then_there_is_no_double_slash(self):
        self.settings(SETTINGS.replace(PING, PING + "/"))
        self.run_job()
        self.assert_pinged(0)

    def test_given_a_delivered_run_and_no_monitor_when_it_ends_then_the_run_fails(self):
        result = self.run_job(FAKE_CURL_STATUS="7")
        self.assertEqual(result.returncode, 4)
        self.assertIn("heartbeat could not be told", result.stderr)

    def test_given_a_failed_run_and_no_monitor_when_it_ends_then_the_status_is_the_jobs(self):
        result = self.run_job(FAKE_RUN_STATUS="1", FAKE_CURL_STATUS="7")
        self.assertEqual(result.returncode, 1)
        self.assertIn("heartbeat could not be told", result.stderr)


class TheSettings(Base):
    def test_given_no_file_when_run_then_it_fails_naming_the_template_and_cannot_ping(self):
        (self.secrets / "security-signals-production.env").unlink()
        result = self.run_job()
        self.assertEqual(result.returncode, 3)
        self.assertIn("env/security-signals.env.example", result.stderr)
        self.assertEqual(self.pings(), [])
        self.assertEqual(self.runs(), [])

    def test_given_no_key_when_run_then_the_heartbeat_hears_and_nothing_runs(self):
        self.settings(SETTINGS.replace(KEY, ""))
        result = self.run_job()
        self.assertEqual(result.returncode, 3)
        self.assertIn("HEIMDALL_SIGNAL_HERMES_API_KEY is empty", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assert_pinged(3)

    def test_given_no_address_when_run_then_the_heartbeat_hears(self):
        self.settings(SETTINGS.replace(ALERT_TO, ""))
        self.assertEqual(self.run_job().returncode, 3)
        self.assert_pinged(3)

    def test_given_no_heartbeat_when_run_then_it_refuses_to_run_unobserved(self):
        self.settings(SETTINGS.replace(PING, ""))
        result = self.run_job()
        self.assertEqual(result.returncode, 3)
        self.assertIn("HEIMDALL_SIGNAL_HEARTBEAT_URL is empty", result.stderr)
        self.assertEqual(self.runs(), [])

    def test_given_a_url_that_could_break_the_curl_config_when_run_then_it_is_refused(self):
        for url in ('https://x/"injected', "https://x/a\\b", "https://x y", "ftp://x"):
            with self.subTest(url=url):
                self.settings(SETTINGS.replace(PING, url))
                result = self.run_job()
                self.assertEqual(result.returncode, 3)
                self.assertIn("not an http or https URL", result.stderr)
                self.assertEqual(self.pings(), [])

    def test_given_a_mistake_before_the_url_when_run_then_the_heartbeat_still_hears(self):
        self.settings("export OOPS\n" + SETTINGS)
        self.assertEqual(self.run_job().returncode, 3)
        self.assert_pinged(3)

    def test_given_an_unknown_setting_when_run_then_it_is_named_and_its_value_is_not(self):
        self.settings(SETTINGS + "MAILGUN_API_KEY=secret-value\n")
        result = self.run_job()
        self.assertEqual(result.returncode, 3)
        self.assertIn("MAILGUN_API_KEY", result.stderr)
        self.assertNotIn("secret-value", result.stderr)

    def test_given_a_wrong_setting_when_reported_then_no_value_is_printed(self):
        self.settings(SETTINGS.replace(ALERT_TO, ""))
        result = self.run_job()
        for secret in (KEY, PING):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_given_crlf_line_endings_when_read_then_they_do_not_become_part_of_the_values(self):
        self.settings(SETTINGS.replace("\n", "\r\n"))
        self.assertEqual(self.run_job().returncode, 0)
        self.assert_pinged(0)

    def test_given_bad_arguments_when_run_then_nothing_happens(self):
        for args in (("run",), ("list", "production"), ("run", "production", "x"), ("run", "../etc")):
            with self.subTest(args=args):
                self.assertEqual(self.run_job(*args).returncode, 3)
                self.assertEqual(self.calls(), [])


class ThePin(Base):
    def copy_of_the_repository(self):
        root = self.temp / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "platform").mkdir()
        shutil.copy(SCRIPT, root / "scripts")
        shutil.copy(SCRIPTS / "docker-engine.sh", root / "scripts")
        shutil.copytree(JOB.parent, root / "platform" / "heimdall-signals")
        return root

    def pinned(self, name):
        return re.search(rf"^{name}=(\S+)$", SCRIPT.read_text(), re.M).group(1)

    def test_given_the_copy_in_the_repository_when_hashed_then_it_is_the_pinned_one(self):
        self.assertEqual(hashlib.sha256(JOB.read_bytes()).hexdigest(), self.pinned("SIGNALS_SHA256"))

    def test_given_the_pin_when_read_then_it_is_a_full_commit(self):
        self.assertRegex(self.pinned("SIGNALS_COMMIT"), r"^[a-f0-9]{40}$")

    def test_given_a_changed_copy_when_run_then_nothing_runs_and_the_heartbeat_hears(self):
        root = self.copy_of_the_repository()
        with (root / "platform" / "heimdall-signals" / "security_signals.py").open("a") as job:
            job.write("# changed on the machine\n")
        result = self.run_job(script=root / "scripts" / "security-signals.sh")
        self.assertEqual(result.returncode, 3)
        self.assertIn("checksum differs", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assert_pinged(3)

    def test_given_a_missing_copy_when_run_then_the_heartbeat_hears(self):
        root = self.copy_of_the_repository()
        (root / "platform" / "heimdall-signals" / "security_signals.py").unlink()
        self.assertEqual(self.run_job(script=root / "scripts" / "security-signals.sh").returncode, 3)
        self.assert_pinged(3)


class TheUnits(unittest.TestCase):
    def service(self):
        return (UNITS / "yggdrasil-security-signals@.service").read_text()

    def timer(self):
        return (UNITS / "yggdrasil-security-signals@.timer").read_text()

    def test_the_service_runs_the_script_for_its_instance_on_the_users_engine(self):
        service = self.service()
        self.assertIn("ExecStart=/opt/yggdrasil/scripts/security-signals.sh run %i\n", service)
        self.assertIn("Environment=DOCKER_HOST=unix://%t/docker.sock\n", service)
        self.assertIn("After=docker.service\n", service)
        self.assertIn("Wants=docker.service\n", service)
        self.assertIn("Type=oneshot\n", service)
        self.assertRegex(service, r"TimeoutStartSec=\d+")

    def test_the_timer_runs_every_fifteen_minutes_and_catches_up(self):
        timer = self.timer()
        self.assertIn("OnCalendar=*:0/15\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)

    def test_the_script_is_executable_as_the_unit_runs_it_directly(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_the_template_has_the_settings_the_script_reads_and_no_values(self):
        lines = [l for l in TEMPLATE.read_text().splitlines() if l and not l.startswith("#")]
        self.assertEqual(sorted(lines), sorted([
            "HEIMDALL_SIGNAL_HERMES_API_KEY=", "HEIMDALL_SIGNAL_ALERT_TO=", "HEIMDALL_SIGNAL_HEARTBEAT_URL=",
        ]))


if __name__ == "__main__":
    unittest.main()
