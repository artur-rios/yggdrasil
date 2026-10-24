"""Tests for scripts/prove-machine.sh: the spec's rootless-setup proofs, run by the admin on a real machine.

    python3 -m unittest discover -s scripts -p "test_prove_machine.py"

Nothing here needs a real machine: `ps`, `sudo`, `getent`, `id`, `test` and `docker` are fakes on PATH (the way
test_machine.py's are). They keep their state in plain files under $FAKE_STATE, and every call is logged to
$FAKE_STATE/calls. What is tested is what the script concludes from what those answer, and which commands it
gives docker (who runs them, what they mount and publish), since that is what makes a proof mean something.

The fake `sudo -u <user>` writes the user down in $FAKE_AS_USER for the fakes below it, which is how `docker`
knows whose engine it is talking to and how `test -r` answers for that user.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "prove-machine.sh"

FAKES = {
    # `sudo -u <user> <command>` runs the command as that user, as far as the fakes can tell; it fails for a
    # user in $FAKE_STATE/sudo-u-denied-<user>. `sudo <command>` is root.
    "sudo": r"""
        echo "sudo $*" >>"$FAKE_STATE/calls"
        if [[ "$1" == -n ]]; then shift; fi
        if [[ "$1" == -u ]]; then
          [[ ! -e "$FAKE_STATE/sudo-u-denied-$2" ]] || exit 1
          export FAKE_AS_USER=$2
          echo "as $2 in $PWD" >>"$FAKE_STATE/calls"
          shift 2
        fi
        exec "$@"
    """,
    # The users: "name:x:uid:gid::home:shell" lines in $FAKE_STATE/passwd.
    "getent": r"""
        [[ "$1" == passwd ]] || exit 2
        grep -- "^$2:" "$FAKE_STATE/passwd" || exit 2
    """,
    "id": r"""
        case "$*" in
          -u) echo "${FAKE_UID:-1000}" ;;
          -un) echo admin ;;
          *) echo "fake id: unexpected call: $*" >&2; exit 64 ;;
        esac
    """,
    # What `ps -eo pid=,uid=,comm=` prints: the lines of $FAKE_STATE/ps.
    "ps": r"""
        echo "ps $*" >>"$FAKE_STATE/calls"
        cat "$FAKE_STATE/ps"
    """,
    # `test -e <path>`: the path is in $FAKE_STATE/exists. `test -r <path>` as a user: the path is in
    # $FAKE_STATE/readable-by-<user>. Anything else is the real test.
    "test": r"""
        case "$1" in
          -e) grep -qxF -- "$2" "$FAKE_STATE/exists" 2>/dev/null; exit $? ;;
          -r) [[ -n "${FAKE_AS_USER:-}" ]] && grep -qxF -- "$2" "$FAKE_STATE/readable-by-$FAKE_AS_USER" 2>/dev/null
              exit $? ;;
          *) exec /usr/bin/test "$@" ;;
        esac
    """,
    # The engine of whoever it runs as, found by $DOCKER_HOST. The answers are chosen by the container's name
    # (ygg-prove-probe, -server, -client) and by switches in the environment:
    #   FAKE_ENGINE_DOWN, FAKE_ENGINE_ROOTFUL, FAKE_NO_IMAGE, FAKE_PULL_FAILS  the engine and the image
    #   FAKE_PROBE_REFUSED   the probe's `docker run` fails, as a rootless engine does for a path it can't read
    #   FAKE_PROBE_REACHES   what the probe reports it could reach ("run-socket home etc")
    #   FAKE_PROBE_SILENT    the probe prints nothing at all
    #   FAKE_SERVER_FAILS, FAKE_NO_PORT, FAKE_NO_ANSWER, FAKE_NO_GATEWAY   the Testcontainers proof
    "docker": r"""
        echo "docker as=${FAKE_AS_USER:-} host=${DOCKER_HOST:-} ${*//$'
'/ }" >>"$FAKE_STATE/calls"
        case "$1" in
          info)
            [[ -z "${FAKE_ENGINE_DOWN:-}" ]] || exit 1
            if [[ -n "${FAKE_ENGINE_ROOTFUL:-}" ]]; then echo '["name=seccomp,profile=builtin"]'
            else echo '["name=seccomp,profile=builtin","name=rootless"]'; fi
            ;;
          image) [[ -z "${FAKE_NO_IMAGE:-}" ]] || exit 1 ;;
          pull) [[ -z "${FAKE_PULL_FAILS:-}" ]] || { echo "pull access denied" >&2; exit 1; } ;;
          rm) ;;
          port) [[ -z "${FAKE_NO_PORT:-}" ]] || exit 1
                echo "0.0.0.0:49153"; echo "[::]:49153" ;;
          ps) echo "traefik-container-id" ;;
          logs) cat "$FAKE_STATE/traefik-log" 2>/dev/null ;;
          run)
            case "$*" in
              *ygg-prove-probe*)
                if [[ -n "${FAKE_PROBE_REFUSED:-}" ]]; then
                  echo "docker: Error response from daemon: error while creating mount source path: permission denied" >&2
                  exit 125
                fi
                [[ -z "${FAKE_PROBE_SILENT:-}" ]] || exit 0
                echo "reached:${FAKE_PROBE_REACHES:-}"
                echo "probe-done"
                ;;
              *ygg-prove-server*)
                [[ -z "${FAKE_SERVER_FAILS:-}" ]] || { echo "port is already allocated" >&2; exit 125; }
                echo "server-container-id"
                ;;
              *ygg-prove-client*)
                if [[ -n "${FAKE_NO_GATEWAY:-}" ]]; then echo "override:"
                else echo "override:172.17.0.1"; fi
                if [[ -n "${FAKE_NO_ANSWER:-}" ]]; then echo "body:"; else echo "body:ygg-prove"; fi
                ;;
            esac
            ;;
        esac
    """,
}

# Processes as `ps -eo pid=,uid=,comm=` shows them on a healthy machine: root has the kernel threads and sshd,
# the service users have the engines and what runs in them (a container's root is the service user's uid).
HEALTHY_PS = """\
    1     0 systemd
    2     0 kthreadd
  800     0 sshd
 1500  1001 dockerd
 1510  1001 containerd
 1520  1001 rootlesskit
 1600  1001 traefik
 1700  1002 dockerd
 1800    69 java
"""

PASSWD = """\
ygg:x:1001:1001::/home/ygg:/bin/bash
ci:x:1002:1002::/home/ci:/usr/sbin/nologin
"""

TRAEFIK_LINE = '{{"ClientAddr":"{host}:5000","ClientHost":"{host}","ClientPort":"5000","RequestPath":"/secret-path?token=abc","RequestMethod":"GET"}}'

TAR_IMAGE = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class ProveMachineTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="prove-machine-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + "\n".join(line[8:] if line.startswith("        ") else line
                                                                 for line in body.strip("\n").splitlines()) + "\n")
            path.chmod(0o755)
        (self.state / "calls").write_text("")
        (self.state / "ps").write_text(HEALTHY_PS)
        (self.state / "passwd").write_text(PASSWD)
        # What exists on the machine and only root can see (the sockets and directories of other users).
        (self.state / "exists").write_text(
            "/run/user/1001/docker.sock\n/run/user/1001\n/home/ygg\n/etc/yggdrasil\n")
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_STATE=str(self.state))
        for name in list(self.env):
            if name.startswith("FAKE_") and name != "FAKE_STATE":
                del self.env[name]

    def prove(self, *arguments, **env):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        return (self.state / "calls").read_text().splitlines()

    def docker_calls(self, containing=""):
        return [line for line in self.calls() if line.startswith("docker ") and containing in line]

    def docker_runs(self, name):
        """The `docker run` calls that start this container (a run is not the rm that cleans up after it)."""
        return [line for line in self.docker_calls(name) if " run " in line]

    def rows(self, result):
        """name -> (status, detail), for the report rows: '  <status>  <name padded to 20> <detail>'."""
        found = {}
        for line in result.stdout.splitlines():
            match = re.match(r"^  (ok|FAIL|look)\s+(.{20}) (.*)$", line)
            if match:
                found[match.group(2).strip()] = (match.group(1), match.group(3))
        return found

    def status_of(self, result, name):
        rows = self.rows(result)
        self.assertIn(name, rows, f"no row {name!r} in:\n{result.stdout}\n{result.stderr}")
        return rows[name][0]

    def readable_by_ci(self, *paths):
        (self.state / "readable-by-ci").write_text("".join(path + "\n" for path in paths))

    # ---- usage, root --------------------------------------------------------------------------------

    def test_given_no_role_or_a_wrong_one_when_proved_then_it_is_a_usage_error(self):
        for arguments in ([], ["workstation"], ["dev", "apps"], [""]):
            with self.subTest(arguments=arguments):
                result = self.prove(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage", result.stderr)

    def test_given_root_when_proved_then_it_refuses_before_running_anything(self):
        result = self.prove("dev", FAKE_UID="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not as root", result.stderr)
        self.assertEqual(self.calls(), [])

    # ---- no root services ---------------------------------------------------------------------------

    def test_given_a_healthy_machine_when_proved_then_no_root_services_is_ok_and_the_exit_is_zero(self):
        result = self.prove("apps")
        self.assertEqual(self.status_of(result, "no root services"), "ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # sshd and the kernel threads are root's, and fine.
        self.assertNotIn("sshd", self.rows(result)["no root services"][1])

    def test_given_a_root_owned_dockerd_when_proved_then_no_root_services_fails(self):
        (self.state / "ps").write_text(HEALTHY_PS + " 4242     0 dockerd\n")
        result = self.prove("apps")
        self.assertEqual(self.status_of(result, "no root services"), "FAIL")
        detail = self.rows(result)["no root services"][1]
        self.assertIn("4242 dockerd", detail)
        self.assertEqual(result.returncode, 1)

    def test_given_each_forbidden_command_owned_by_root_when_proved_then_each_is_named(self):
        forbidden = ["dockerd", "containerd", "rootlesskit", "traefik", "forgejo", "gitea", "java", "node",
                     "prometheus", "loki", "alloy", "grafana"]
        lines = "".join(f"{3000 + i} 0 {name}\n" for i, name in enumerate(forbidden))
        (self.state / "ps").write_text(HEALTHY_PS + lines)
        result = self.prove("apps")
        detail = self.rows(result)["no root services"][1]
        for i, name in enumerate(forbidden):
            self.assertIn(f"{3000 + i} {name}", detail)

    def test_given_a_similar_name_owned_by_root_when_proved_then_it_is_not_a_root_service(self):
        # Only the commands of the list: containerd-shim and a node_exporter-like name are other processes.
        (self.state / "ps").write_text(HEALTHY_PS + "  900     0 sshd\n  901     0 nodeping\n  902     0 kworker/0:1\n")
        result = self.prove("apps")
        self.assertEqual(self.status_of(result, "no root services"), "ok")

    # ---- (dev) a check can't reach ygg ---------------------------------------------------------------

    def test_given_the_ygg_socket_readable_from_a_check_when_proved_then_it_fails(self):
        self.readable_by_ci("/run/user/1001/docker.sock")
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "ci -> ygg (host)"), "FAIL")
        self.assertEqual(result.returncode, 1)

    def test_given_the_ygg_socket_not_readable_from_a_check_when_proved_then_the_host_side_is_ok(self):
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "ci -> ygg (host)"), "ok")
        # The proof was asked of ci through sudo, from /, in ci's runtime directory.
        self.assertIn("sudo -u ci env XDG_RUNTIME_DIR=/run/user/1002 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1002/bus test -r /run/user/1001/docker.sock",
                      self.calls())
        self.assertIn("as ci in /", self.calls())

    def test_given_no_socket_of_ygg_when_proved_then_nothing_is_proven_and_it_fails(self):
        # A socket that isn't there can't be read either: that is not a proof.
        (self.state / "exists").write_text("/run/user/1001\n/home/ygg\n/etc/yggdrasil\n")
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "ci -> ygg (host)"), "FAIL")
        self.assertIn("not there", self.rows(result)["ci -> ygg (host)"][1])

    def test_given_sudo_denied_for_ci_when_proved_then_nothing_is_proven_and_it_fails(self):
        (self.state / "sudo-u-denied-ci").write_text("")
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "ci -> ygg (host)"), "FAIL")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
        self.assertEqual(self.status_of(result, "testcontainers"), "FAIL")

    def test_given_the_run_refused_when_proved_then_the_container_side_is_ok(self):
        # A rootless engine can't mount what its user can't read: the refusal is the proof.
        result = self.prove("dev", FAKE_PROBE_REFUSED="1")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "ok")
        self.assertIn("refused", self.rows(result)["ci -> ygg (docker)"][1])

    def test_given_the_probe_runs_and_reaches_nothing_when_proved_then_the_container_side_is_ok(self):
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "ok")

    def test_given_the_probe_mounts_what_it_is_told_when_proved_then_it_runs_as_ci_with_read_only_mounts(self):
        result = self.prove("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = [line for line in self.docker_calls("ygg-prove-probe") if " run " in line]
        self.assertEqual(len(run), 1, run)
        run = run[0]
        # ci's own engine, found by the socket in ci's runtime directory.
        self.assertTrue(run.startswith("docker as=ci host=unix:///run/user/1002/docker.sock "), run)
        for mount in ("/run/user/1001:/probe-run:ro", "/home/ygg:/probe-home:ro", "/etc/yggdrasil:/probe-etc:ro"):
            self.assertIn(f"-v {mount}", run)
        self.assertIn(TAR_IMAGE, run)

    def test_given_the_probe_reaches_the_socket_when_proved_then_it_fails(self):
        for reached in ("run-socket", "home", "etc", "run-socket home etc"):
            with self.subTest(reached=reached):
                result = self.prove("dev", FAKE_PROBE_REACHES=reached)
                self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
                self.assertEqual(result.returncode, 1)
                for word in reached.split():
                    self.assertIn(word, self.rows(result)["ci -> ygg (docker)"][1])

    def test_given_a_probe_that_prints_nothing_when_proved_then_it_fails(self):
        # Exit 0 and no report is a container that never ran its checks, not one that reached nothing.
        result = self.prove("dev", FAKE_PROBE_SILENT="1")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")

    def test_given_a_path_that_is_not_there_when_proved_then_the_probe_does_not_run(self):
        # docker would make a missing source path, or refuse for that reason: neither is a proof.
        (self.state / "exists").write_text("/run/user/1001/docker.sock\n/run/user/1001\n/home/ygg\n")
        result = self.prove("dev", FAKE_PROBE_REFUSED="1")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
        self.assertIn("/etc/yggdrasil", self.rows(result)["ci -> ygg (docker)"][1])
        self.assertEqual(self.docker_runs("ygg-prove-probe"), [])

    def test_given_an_engine_that_does_not_answer_when_proved_then_the_container_proofs_fail(self):
        result = self.prove("dev", FAKE_ENGINE_DOWN="1")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
        self.assertEqual(self.status_of(result, "testcontainers"), "FAIL")
        self.assertIn("does not answer", self.rows(result)["testcontainers"][1])
        self.assertEqual(self.docker_runs("ygg-prove-"), [])

    def test_given_a_rootful_engine_for_ci_when_proved_then_the_container_proofs_fail(self):
        result = self.prove("dev", FAKE_ENGINE_ROOTFUL="1")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
        self.assertIn("rootless", self.rows(result)["testcontainers"][1])

    def test_given_an_image_that_cannot_be_pulled_when_proved_then_a_refused_run_is_no_proof(self):
        result = self.prove("dev", FAKE_NO_IMAGE="1", FAKE_PULL_FAILS="1", FAKE_PROBE_REFUSED="1")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
        self.assertIn("pull", self.rows(result)["ci -> ygg (docker)"][1])

    def test_given_a_missing_image_when_proved_then_it_is_pulled_once_by_ci(self):
        result = self.prove("dev", FAKE_NO_IMAGE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pulls = self.docker_calls(" pull ")
        self.assertEqual(len(pulls), 1, pulls)
        self.assertIn("as=ci", pulls[0])
        self.assertIn(TAR_IMAGE, pulls[0])

    def test_given_no_ygg_user_when_proved_then_the_dev_proofs_fail(self):
        (self.state / "passwd").write_text("ci:x:1002:1002::/home/ci:/usr/sbin/nologin\n")
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "ci -> ygg (host)"), "FAIL")
        self.assertEqual(self.status_of(result, "ci -> ygg (docker)"), "FAIL")
        self.assertEqual(result.returncode, 1)

    def test_given_no_ci_user_when_proved_then_the_dev_proofs_fail_and_the_script_still_finishes(self):
        (self.state / "passwd").write_text(PASSWD.splitlines(keepends=True)[0])
        result = self.prove("dev")
        for name in ("ci -> ygg (host)", "ci -> ygg (docker)", "testcontainers"):
            self.assertEqual(self.status_of(result, name), "FAIL")
        self.assertIn("no ci user", self.rows(result)["testcontainers"][1])
        self.assertEqual(result.returncode, 1)

    # ---- (dev) Testcontainers ------------------------------------------------------------------------

    def test_given_the_published_port_reachable_when_proved_then_testcontainers_is_ok(self):
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "testcontainers"), "ok")
        detail = self.rows(result)["testcontainers"][1]
        self.assertIn("172.17.0.1:49153", detail)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_given_the_server_when_started_then_it_publishes_a_random_port_on_all_interfaces_in_ci_engine(self):
        self.prove("dev")
        server = [line for line in self.docker_calls("ygg-prove-server") if " run " in line]
        self.assertEqual(len(server), 1, server)
        server = server[0]
        self.assertTrue(server.startswith("docker as=ci host=unix:///run/user/1002/docker.sock "), server)
        # Like Testcontainers: the host port chosen by the engine, on every interface (no 127.0.0.1).
        self.assertIn("-p 0:8080", server)
        self.assertNotIn("127.0.0.1", server)
        self.assertIn(TAR_IMAGE, server)
        self.assertIn("httpd -f -p 8080", server)
        # The port is read back from the engine.
        port = self.docker_calls(" port ")
        self.assertEqual(len(port), 1, port)
        self.assertIn("ygg-prove-server", port[0])

    def test_given_the_client_when_started_then_it_gets_the_override_from_run_check_inside_it(self):
        self.prove("dev")
        client = [line for line in self.docker_calls("ygg-prove-client") if " run " in line]
        self.assertEqual(len(client), 1, client)
        client = client[0]
        self.assertTrue(client.startswith("docker as=ci host=unix:///run/user/1002/docker.sock "), client)
        # The same engine (a second container next to the server), with the script that computes the override
        # mounted read-only, run by the container's own sh, and the published port asked of the address it finds.
        self.assertIn("run-check.sh:ro", client)
        self.assertIn("--print-host-override", client)
        self.assertIn("wget", client)
        self.assertTrue(client.rstrip().endswith("49153"), client)

    def test_given_no_answer_from_the_published_port_when_proved_then_testcontainers_fails(self):
        result = self.prove("dev", FAKE_NO_ANSWER="1")
        self.assertEqual(self.status_of(result, "testcontainers"), "FAIL")
        self.assertIn("172.17.0.1:49153", self.rows(result)["testcontainers"][1])
        self.assertEqual(result.returncode, 1)

    def test_given_no_gateway_in_the_container_when_proved_then_testcontainers_fails(self):
        result = self.prove("dev", FAKE_NO_GATEWAY="1", FAKE_NO_ANSWER="1")
        self.assertEqual(self.status_of(result, "testcontainers"), "FAIL")
        self.assertIn("gateway", self.rows(result)["testcontainers"][1])

    def test_given_a_server_that_does_not_start_when_proved_then_testcontainers_fails(self):
        result = self.prove("dev", FAKE_SERVER_FAILS="1")
        self.assertEqual(self.status_of(result, "testcontainers"), "FAIL")
        self.assertEqual(self.docker_runs("ygg-prove-client"), [])

    def test_given_no_published_port_when_proved_then_testcontainers_fails(self):
        result = self.prove("dev", FAKE_NO_PORT="1")
        self.assertEqual(self.status_of(result, "testcontainers"), "FAIL")
        self.assertEqual(self.docker_runs("ygg-prove-client"), [])

    def test_given_any_outcome_when_proved_then_every_prove_container_is_removed_by_ci(self):
        for env in ({}, {"FAKE_NO_ANSWER": "1"}, {"FAKE_SERVER_FAILS": "1"}, {"FAKE_PROBE_REFUSED": "1"}):
            with self.subTest(env=env):
                (self.state / "calls").write_text("")
                self.prove("dev", **env)
                removed = self.docker_calls(" rm ")
                for name in ("ygg-prove-server", "ygg-prove-client", "ygg-prove-probe"):
                    self.assertTrue(any(name in line and "as=ci" in line and " -f " in line for line in removed),
                                    f"{name} not removed: {removed}")
                # And the last thing done with docker is the removal, so nothing is left behind.
                self.assertIn(" rm ", self.docker_calls()[-1])

    def test_given_an_interrupted_run_when_proved_then_the_containers_are_still_removed(self):
        # The trap is on EXIT, which a signal reaches: here the script is killed while `docker run` of the
        # client hangs.
        hang = self.bin / "docker"
        original = hang.read_text()
        hang.write_text(original.replace("*ygg-prove-client*)", "*ygg-prove-client*)\n                sleep 30\n"))
        process = subprocess.Popen(["bash", str(SCRIPT), "dev"], env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = 40
            while deadline and not self.docker_runs("ygg-prove-client"):
                time.sleep(0.25)
                deadline -= 1
            self.assertTrue(self.docker_runs("ygg-prove-client"), "the client was never started")
            before = len(self.calls())
            os.killpg(process.pid, 15)
            process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
        # What was done after the signal: the server (and the others) removed by the trap.
        after = [line for line in self.calls()[before:] if " rm " in line]
        for name in ("ygg-prove-server", "ygg-prove-client", "ygg-prove-probe"):
            self.assertTrue(any(name in line for line in after), f"{name} not removed after the signal: {after}")

    def test_given_the_apps_role_when_proved_then_no_ci_proof_runs_and_no_container_starts(self):
        result = self.prove("apps")
        rows = self.rows(result)
        for name in ("ci -> ygg (host)", "ci -> ygg (docker)", "testcontainers"):
            self.assertNotIn(name, rows)
        self.assertFalse([line for line in self.docker_calls() if "as=ci" in line])
        self.assertEqual(self.docker_runs("ygg-prove-"), [])

    # ---- (apps) real client addresses ------------------------------------------------------------------

    def traefik_log(self, hosts):
        (self.state / "traefik-log").write_text(
            "".join(TRAEFIK_LINE.format(host=host) + "\n" for host in hosts)
            + "time=2026-10-24T10:00:00Z level=info msg=\"not an access log line\"\n")

    def test_given_role_apps_when_proved_then_client_addresses_are_shown_for_a_look(self):
        self.traefik_log(["203.0.113.5", "198.51.100.7", "203.0.113.5", "2001:db8::1"])
        result = self.prove("apps")
        self.assertEqual(self.status_of(result, "client addresses"), "look")
        detail = self.rows(result)["client addresses"][1]
        for address in ("203.0.113.5", "198.51.100.7", "2001:db8::1"):
            self.assertIn(address, detail)
        # Distinct: one mention of the address that came twice.
        self.assertEqual(detail.count("203.0.113.5"), 1)
        # A look is for the admin: it is not a failure.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_given_more_than_ten_addresses_when_proved_then_the_last_ten_distinct_are_shown_newest_first(self):
        self.traefik_log([f"192.0.2.{n}" for n in range(1, 16)])
        result = self.prove("apps")
        detail = self.rows(result)["client addresses"][1]
        shown = re.findall(r"192\.0\.2\.\d+", detail)
        self.assertEqual(shown, [f"192.0.2.{n}" for n in range(15, 5, -1)])

    def test_given_an_access_log_when_proved_then_no_path_or_header_is_printed(self):
        self.traefik_log(["203.0.113.5"])
        result = self.prove("apps")
        output = result.stdout + result.stderr
        for fragment in ("secret-path", "token=abc", "GET", "RequestPath"):
            self.assertNotIn(fragment, output)

    def test_given_the_log_read_when_proved_then_it_is_ygg_that_reads_traefiks_container(self):
        self.traefik_log(["203.0.113.5"])
        self.prove("apps")
        finds = self.docker_calls(" ps ")
        self.assertEqual(len(finds), 1, finds)
        self.assertIn("as=ygg", finds[0])
        self.assertIn("host=unix:///run/user/1001/docker.sock", finds[0])
        self.assertIn("com.docker.compose.service=traefik", finds[0])
        logs = self.docker_calls(" logs ")
        self.assertEqual(len(logs), 1, logs)
        self.assertIn("traefik-container-id", logs[0])

    def test_given_an_empty_log_when_proved_then_the_look_says_to_make_a_request(self):
        (self.state / "traefik-log").write_text("")
        result = self.prove("apps")
        self.assertEqual(self.status_of(result, "client addresses"), "look")
        self.assertIn("request", self.rows(result)["client addresses"][1])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_given_text_that_is_not_an_address_in_the_log_when_proved_then_it_is_not_printed(self):
        (self.state / "traefik-log").write_text(
            '{"ClientHost":"<script>alert(1)</script>","RequestPath":"/x"}\n' + TRAEFIK_LINE.format(host="203.0.113.5") + "\n")
        result = self.prove("apps")
        self.assertNotIn("script", result.stdout + result.stderr)
        self.assertIn("203.0.113.5", self.rows(result)["client addresses"][1])

    def test_given_no_user_ygg_when_proved_on_apps_then_the_look_fails(self):
        (self.state / "passwd").write_text("")
        result = self.prove("apps")
        self.assertEqual(self.status_of(result, "client addresses"), "FAIL")
        self.assertEqual(result.returncode, 1)

    def test_given_the_dev_role_when_proved_then_there_is_no_client_address_row(self):
        result = self.prove("dev")
        self.assertNotIn("client addresses", self.rows(result))
        self.assertEqual(self.docker_calls(" logs "), [])

    # ---- exit code, secrets ----------------------------------------------------------------------------

    def test_given_a_healthy_dev_machine_when_proved_then_every_row_is_ok_and_the_exit_is_zero(self):
        result = self.prove("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({status for status, _ in self.rows(result).values()}, {"ok"}, result.stdout)
        self.assertEqual(set(self.rows(result)),
                         {"no root services", "ci -> ygg (host)", "ci -> ygg (docker)", "testcontainers"})

    def test_given_one_failure_among_the_proofs_when_proved_then_the_others_still_run_and_the_exit_is_one(self):
        (self.state / "ps").write_text(HEALTHY_PS + " 4242     0 traefik\n")
        result = self.prove("dev")
        self.assertEqual(self.status_of(result, "no root services"), "FAIL")
        self.assertEqual(self.status_of(result, "testcontainers"), "ok")
        self.assertEqual(result.returncode, 1)

    def test_given_secrets_in_the_environment_when_proved_then_none_is_printed_or_given_to_a_command(self):
        secret = "s3cr3t-token-value"
        self.traefik_log(["203.0.113.5"])
        for role in ("apps", "dev"):
            result = self.prove(role, CF_DNS_API_TOKEN=secret, FORGE_TOKEN=secret, RENOVATE_TOKEN=secret)
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn(secret, "\n".join(self.calls()))


if __name__ == "__main__":
    unittest.main()
