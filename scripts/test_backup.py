"""Tests for scripts/backup.sh (the nightly backup of a machine to the other one) and the systemd units
that run it.

    python3 -m unittest discover -s scripts -p "test_backup.py"

`docker`, `rsync` and `ssh` are fakes on PATH that log every call to $FAKE_LOG; a call whose arguments
contain $FAKE_FAIL fails (docker) and $FAKE_RSYNC_STATUS is rsync's exit status. HOME is a temporary
directory, so ~/backups is too.
"""

import datetime
import gzip
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "backup.sh"
VARS = SCRIPTS / "vars.py"
UNITS = SCRIPTS.parent / "platform" / "systemd"
TEMPLATE = SCRIPTS.parent / "env" / "backup.conf.example"

FAKES = {
    # `info` answers as a rootless engine (backup.sh checks that, like platform.sh); `ps` finds the
    # forgejo container; `exec` and `run` print what the dump would; `cp` writes the file copied out.
    "docker": r"""
        echo "docker $*" >>"$FAKE_LOG"
        # The lock on ~/backups is fd 9 of backup.sh: a child that kept it open (ssh's ControlPersist,
        # say) would hold the lock after backup.sh is gone.
        [[ ! -e /proc/self/fd/9 ]] || echo "fd 9 open in docker $1" >>"$FAKE_LOG"
        if [[ -n "${FAKE_FAIL:-}" && "$*" == *"$FAKE_FAIL"* ]]; then
          echo "fake docker: failing on purpose" >&2
          exit 1
        fi
        case "$1" in
          info) echo '["name=seccomp,profile=builtin","name=rootless"]' ;;
          ps)
            # The database's project: the database itself, since it runs, and FAKE_PROJECT_OTHERS.
            if [[ "$*" == *"com.docker.compose.project=app"* ]]; then
              echo "dbid00000000"
              [[ -z "${FAKE_PROJECT_OTHERS:-}" ]] || echo "$FAKE_PROJECT_OTHERS"
            else
              [[ -z "${FAKE_NO_FORGEJO:-}" ]] && echo "${FAKE_FORGEJO:-forgejo-id}"
            fi
            ;;
          inspect)
            case "$*" in
              *State.Status*) echo "${FAKE_STATE:-running}" ;;
              *com.docker.compose.project*) echo "app" ;;
              *.Id*) echo "dbid0000000000000000" ;;
            esac
            ;;
          exec)
            container=$2
            case "$3" in
              pg_dumpall) echo "-- postgres dump from $container" ;;
              pg_isready) [[ -z "${FAKE_NOT_READY:-}" ]] || exit 1 ;;
            esac
            ;;
          cp) echo "zip of ${2%%:*}" >"$3" ;;
          run) echo "tar of the volume" ;;
        esac
        exit 0
    """,
    "rsync": r"""
        echo "rsync $*" >>"$FAKE_LOG"
        [[ ! -e /proc/self/fd/9 ]] || echo "fd 9 open in rsync" >>"$FAKE_LOG"
        exit "${FAKE_RSYNC_STATUS:-0}"
    """,
    "ssh": r"""
        echo "ssh $*" >>"$FAKE_LOG"
    """,
}

THREE_KINDS = "forgejo\nvolume yggdrasil_jenkins_home\npostgres app-db-1 postgres\n"
BUSYBOX = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-backup-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        self.home = self.temp / "home"
        self.secrets = self.temp / "secrets"
        for directory in (bin_dir, self.home, self.secrets):
            directory.mkdir()
        for name, body in FAKES.items():
            fake = bin_dir / name
            fake.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body).lstrip())
            fake.chmod(0o755)
        self.log = self.temp / "calls"
        self.log.write_text("")
        self.backups = self.home / "backups"
        self.today = datetime.date.today().isoformat()
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "YGG_SECRETS_DIR": str(self.secrets),
            "DOCKER_HOST": f"unix://{self.temp}/docker.sock",
            "FAKE_LOG": str(self.log),
        }
        self.env.pop("XDG_RUNTIME_DIR", None)

    def conf(self, items=THREE_KINDS, target="backup@other:from-here", extra=""):
        header = f"BACKUP_TARGET={target}\n" if target is not None else ""
        (self.secrets / "backup.conf").write_text(header + extra + items)

    def backup(self, *arguments, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments], env={**self.env, **env}, capture_output=True, text=True
        )

    def calls(self):
        return self.log.read_text().splitlines()

    def today_files(self):
        directory = self.backups / self.today
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def test_given_the_three_kinds_when_run_then_one_file_each_in_todays_directory(self):
        self.conf()
        result = self.backup("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.today_files(),
            ["forgejo.zip", "postgres-app-db-1.sql.gz", "volume-yggdrasil_jenkins_home.tar.gz"],
        )
        day = self.backups / self.today
        calls = self.calls()
        # The container is found by the labels of its Compose project and service, not by its name.
        ps = next(call for call in calls if call.startswith("docker ps"))
        self.assertIn("label=com.docker.compose.project=yggdrasil", ps)
        self.assertIn("label=com.docker.compose.service=forgejo", ps)
        self.assertIn("docker exec forgejo-id forgejo dump --config /var/lib/gitea/custom/conf/app.ini "
                      "--type zip --file /tmp/forgejo-dump.zip", calls)
        self.assertIn("docker cp forgejo-id:/tmp/forgejo-dump.zip " + str(day / "forgejo.zip.partial"), calls)
        self.assertEqual((day / "forgejo.zip").read_text(), "zip of forgejo-id\n")
        # The volume is mounted read-only in a throwaway busybox.
        run = next(call for call in calls if call.startswith("docker run"))
        self.assertIn("--rm", run)
        self.assertIn("yggdrasil_jenkins_home:/volume:ro", run)
        # Pinned by version and digest, like the platform's images.
        self.assertIn(f"{BUSYBOX} tar", run)
        self.assertEqual((day / "volume-yggdrasil_jenkins_home.tar.gz").read_text(), "tar of the volume\n")
        self.assertIn("docker exec app-db-1 pg_dumpall -U postgres", calls)
        with gzip.open(day / "postgres-app-db-1.sql.gz", "rt") as dump:
            self.assertEqual(dump.read(), "-- postgres dump from app-db-1\n")

    def test_given_a_stopped_database_when_run_then_it_is_started_dumped_and_stopped_again(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="exited")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("app-db-1 is stopped", result.stdout)
        calls = [call for call in self.calls() if "app-db-1" in call]
        self.assertEqual(calls[:4], [
            "docker inspect --format {{.State.Status}} app-db-1",
            "docker start app-db-1",
            "docker exec app-db-1 pg_isready --quiet --username postgres --host 127.0.0.1",
            "docker exec app-db-1 pg_dumpall -U postgres",
        ])
        self.assertEqual(calls[-1], "docker stop app-db-1")
        self.assertEqual(self.calls().count("docker stop app-db-1"), 1)
        self.assertIn("postgres-app-db-1.sql.gz", self.today_files())

    def test_given_a_running_database_when_run_then_it_is_neither_started_nor_stopped(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([call for call in self.calls() if call.startswith(("docker start", "docker stop"))])

    def test_given_a_stopped_database_that_never_gets_ready_when_run_then_it_fails_and_is_stopped_again(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="exited", FAKE_NOT_READY="1", BACKUP_POSTGRES_START_WAIT="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not accept connections within 2s", result.stderr)
        calls = self.calls()
        self.assertNotIn("docker exec app-db-1 pg_dumpall -U postgres", calls)
        self.assertIn("docker stop app-db-1", calls)
        self.assertNotIn("postgres-app-db-1.sql.gz", self.today_files())

    def test_given_a_created_database_when_run_then_it_is_started_for_the_dump_too(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="created")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker start app-db-1", self.calls())
        self.assertIn("docker stop app-db-1", self.calls())

    def test_given_a_restarting_database_when_run_then_it_fails_and_is_neither_started_nor_stopped(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="restarting")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app-db-1 is restarting", result.stderr)
        self.assertFalse([call for call in self.calls() if call.startswith(("docker start", "docker stop"))])

    def test_given_its_environment_started_during_the_dump_when_run_then_the_database_stays_up(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="exited", FAKE_PROJECT_OTHERS="apiid0000000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("app is running now: app-db-1 stays up", result.stdout)
        self.assertNotIn("docker stop app-db-1", self.calls())

    def test_given_a_failing_dump_of_a_stopped_database_when_run_then_it_is_stopped_again(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="exited", FAKE_FAIL="pg_dumpall")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls().count("docker stop app-db-1"), 1)
        self.assertNotIn("postgres-app-db-1.sql.gz", self.today_files())

    def test_given_a_failing_start_when_run_then_the_item_fails_and_nothing_is_stopped(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="exited", FAKE_FAIL="start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED: postgres app-db-1 postgres", result.stderr)
        self.assertNotIn("docker stop app-db-1", self.calls())

    def test_given_a_failing_stop_when_run_then_the_item_fails(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_STATE="exited", FAKE_FAIL="stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not stop app-db-1 again", result.stderr)
        self.assertIn("FAILED: postgres app-db-1 postgres", result.stderr)

    def test_given_a_missing_database_container_when_run_then_that_item_fails(self):
        self.conf("postgres app-db-1 postgres\n")
        result = self.backup("run", FAKE_FAIL="inspect")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED: postgres app-db-1 postgres", result.stderr)
        self.assertFalse([call for call in self.calls() if call.startswith("docker start")])

    def test_given_a_failing_item_when_run_then_the_others_still_run_and_the_exit_names_it(self):
        self.conf()
        result = self.backup("run", FAKE_FAIL="pg_dumpall")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("postgres app-db-1 postgres", result.stderr)
        # The others are there, and the failing one left nothing behind, not even half of a file.
        self.assertEqual(self.today_files(), ["forgejo.zip", "volume-yggdrasil_jenkins_home.tar.gz"])
        # What there is still goes to the other machine.
        self.assertTrue([call for call in self.calls() if call.startswith("rsync ")])

    def test_given_forgejo_failing_when_run_then_nothing_half_written_is_left_and_the_container_is_cleaned(self):
        self.conf()
        result = self.backup("run", FAKE_FAIL="forgejo dump")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forgejo", result.stderr)
        self.assertEqual(self.today_files(), ["postgres-app-db-1.sql.gz", "volume-yggdrasil_jenkins_home.tar.gz"])
        # The dump file is removed from the container after, as before.
        self.assertEqual(self.calls().count("docker exec forgejo-id rm -f /tmp/forgejo-dump.zip"), 2)

    def test_given_no_forgejo_container_when_run_then_that_item_fails_and_the_others_run(self):
        self.conf()
        result = self.backup("run", FAKE_NO_FORGEJO="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forgejo", result.stderr)
        self.assertEqual(self.today_files(), ["postgres-app-db-1.sql.gz", "volume-yggdrasil_jenkins_home.tar.gz"])

    def test_given_no_target_when_run_then_it_is_refused_before_dumping(self):
        self.conf(target=None)
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BACKUP_TARGET", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.backups.exists())

    def test_given_an_empty_target_as_in_the_template_when_run_then_it_is_refused_too(self):
        self.conf(target="")
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BACKUP_TARGET", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_given_more_days_than_kept_when_run_then_the_oldest_local_days_are_removed(self):
        self.conf(extra="BACKUP_KEEP=3\n")
        for day in ("2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"):
            (self.backups / day).mkdir(parents=True)
            (self.backups / day / "forgejo.zip").write_text("old")
        (self.backups / "notes").mkdir()
        result = self.backup("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kept = sorted(path.name for path in self.backups.iterdir())
        # Today and the two newest before it; what isn't a day is left alone.
        self.assertEqual(kept, sorted(["2020-01-04", "2020-01-05", "notes", self.today]))
        # The removal is before the send, so the other machine's copy loses them too (--delete).
        self.assertTrue([call for call in self.calls() if call.startswith("rsync ")])

    def day(self, name, *files):
        directory = self.backups / name
        directory.mkdir(parents=True, exist_ok=True)
        for file in files:
            (directory / file).write_text(f"{file} of {name}")
        return directory

    def files_by_day(self):
        return {day.name: sorted(path.name for path in day.iterdir())
                for day in sorted(self.backups.iterdir()) if day.is_dir()}

    def test_given_an_item_failing_for_longer_than_kept_when_run_then_its_newest_good_file_stays(self):
        self.conf(items="forgejo\npostgres app-db-1 postgres\n", extra="BACKUP_KEEP=2\n")
        self.day("2020-01-01", "forgejo.zip", "postgres-app-db-1.sql.gz", "volume-gone.tar.gz")
        self.day("2020-01-02", "postgres-app-db-1.sql.gz")
        self.day("2020-01-03", "postgres-app-db-1.sql.gz")
        # Forgejo has failed every night since the 1st, and fails again.
        result = self.backup("run", FAKE_FAIL="forgejo dump")
        self.assertNotEqual(result.returncode, 0)
        # The two newest days stay whole. Beyond them only forgejo.zip of the 1st is kept: it is the
        # last good copy of Forgejo anywhere, and --delete would take it off the other machine too.
        # The rest of that day goes, the file of an item no longer in backup.conf included.
        self.assertEqual(self.files_by_day(), {
            "2020-01-01": ["forgejo.zip"],
            "2020-01-03": ["postgres-app-db-1.sql.gz"],
            self.today: ["postgres-app-db-1.sql.gz"],
        })
        self.assertEqual((self.backups / "2020-01-01" / "forgejo.zip").read_text(), "forgejo.zip of 2020-01-01")
        self.assertIn("keeping 2020-01-01/forgejo.zip", result.stdout)

    def test_given_a_newer_good_file_when_run_then_the_old_days_go_whole(self):
        self.conf(items="forgejo\n", extra="BACKUP_KEEP=1\n")
        self.day("2020-01-01", "forgejo.zip")
        self.day("2020-01-02", "forgejo.zip.partial")
        self.assertEqual(self.backup("run").returncode, 0)
        # Today's forgejo.zip is the newest good one; a .partial left by a crash is no backup.
        self.assertEqual(self.files_by_day(), {self.today: ["forgejo.zip"]})

    def test_given_a_run_when_done_then_no_child_holds_the_lock(self):
        self.conf()
        self.assertEqual(self.backup("run").returncode, 0)
        self.assertTrue([call for call in self.calls() if call.startswith("rsync ")])
        self.assertEqual([call for call in self.calls() if "fd 9 open" in call], [])

    def test_given_the_default_keep_when_run_then_seven_days_stay(self):
        self.conf()
        for number in range(1, 10):
            (self.backups / f"2020-01-0{number}").mkdir(parents=True)
        self.assertEqual(self.backup("run").returncode, 0)
        kept = sorted(path.name for path in self.backups.iterdir())
        self.assertEqual(len(kept), 7, kept)
        self.assertEqual(kept[-1], self.today)

    def test_given_a_day_with_nothing_dumped_when_run_then_it_is_not_counted_as_a_day(self):
        self.conf(items="postgres app-db-1 postgres\n", extra="BACKUP_KEEP=2\n")
        for day in ("2020-01-01", "2020-01-02"):
            (self.backups / day).mkdir(parents=True)
            (self.backups / day / "forgejo.zip").write_text("old")
        result = self.backup("run", FAKE_FAIL="pg_dumpall")
        self.assertNotEqual(result.returncode, 0)
        # Every dump of the night failed: the days that do hold backups stay.
        self.assertEqual(sorted(path.name for path in self.backups.iterdir()), ["2020-01-01", "2020-01-02"])

    def test_given_a_run_when_done_then_rsync_sends_with_the_backup_key(self):
        self.conf()
        self.assertEqual(self.backup("run").returncode, 0)
        sends = [call for call in self.calls() if call.startswith("rsync ")]
        self.assertEqual(
            sends,
            [f"rsync -a --delete -e ssh -i {self.home}/.ssh/yggdrasil-backup -o BatchMode=yes "
             f"{self.backups}/ backup@other:from-here"],
        )

    def test_given_a_configured_key_when_run_then_rsync_uses_it(self):
        self.conf(extra="BACKUP_KEY=~/.ssh/other-key\n")
        self.assertEqual(self.backup("run").returncode, 0)
        sends = [call for call in self.calls() if call.startswith("rsync ")]
        self.assertEqual(len(sends), 1)
        self.assertIn(f"-e ssh -i {self.home}/.ssh/other-key -o BatchMode=yes ", sends[0])

    def test_given_a_key_path_with_a_blank_when_run_then_rsync_gets_it_quoted(self):
        self.conf(extra="BACKUP_KEY=/keys/my key\n")
        self.assertEqual(self.backup("run").returncode, 0)
        send = next(call for call in self.calls() if call.startswith("rsync "))
        self.assertIn("-e ssh -i '/keys/my key' -o BatchMode=yes ", send)
        self.conf(extra="BACKUP_KEY=/keys/it's\n")
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BACKUP_KEY", result.stderr)

    def test_given_rsync_failing_when_run_then_the_exit_says_so_after_the_dumps_were_made(self):
        self.conf()
        result = self.backup("run", FAKE_RSYNC_STATUS="23")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rsync", result.stderr)
        self.assertEqual(len(self.today_files()), 3)

    def vars_py(self, *arguments, secrets=None, stdin=None):
        """scripts/vars.py on a store in `secrets` (default: this test's secrets directory)."""
        env = {**self.env, "YGG_SECRETS_DIR": str(secrets or self.secrets)}
        result = subprocess.run([sys.executable, str(VARS), *arguments], env=env, input=stdin,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_given_a_store_when_backed_up_then_its_archive_restores_it(self):
        self.vars_py("init", stdin="saved\n")
        self.vars_py("set", "platform", "DOMAIN=example.com", "--no-secret")
        self.vars_py("set", "platform", "CF_DNS_API_TOKEN=s3cret", "--secret")
        self.conf("vars\n")
        result = self.backup("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.today_files(), ["vars.tar.gz"])
        archive = self.backups / self.today / "vars.tar.gz"
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        with tarfile.open(archive) as tar:
            members = {member.name: member for member in tar.getmembers()}
            self.assertEqual(sorted(members), ["vars.db", "vars.key"])
            for member in members.values():
                self.assertEqual(stat.S_IMODE(member.mode), 0o600, member.name)
            restored = self.temp / "restored"
            restored.mkdir()
            tar.extractall(restored, filter="data")
        # Unpacked into a secrets directory, it is the store: the key opens it, every value is there.
        self.assertEqual((restored / "vars.key").read_bytes(), (self.secrets / "vars.key").read_bytes())
        self.assertEqual(self.vars_py("get", "platform", "DOMAIN", secrets=restored).strip(), "example.com")
        self.assertEqual(self.vars_py("get", "platform", "CF_DNS_API_TOKEN", "--reveal", secrets=restored).strip(),
                         "s3cret")
        # Nothing is left of the copy outside the archive.
        self.assertEqual(sorted(path.name for path in (self.backups / self.today).iterdir()), ["vars.tar.gz"])

    def test_given_no_store_when_backed_up_then_the_item_fails_and_nothing_is_left(self):
        self.conf("vars\n")
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED: vars", result.stderr)
        self.assertEqual(self.today_files(), [])

    def test_given_vars_with_an_argument_when_run_then_it_is_refused(self):
        self.conf("vars /etc/yggdrasil\n")
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vars takes no arguments", result.stderr)
        self.assertFalse(self.backups.exists())

    def test_given_an_unknown_item_kind_when_run_then_it_is_refused_naming_the_line(self):
        self.conf(items="forgejo\nvolumes oops\n")
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("volumes oops", result.stderr)
        # target on line 1, forgejo on line 2, the unknown kind on line 3
        self.assertIn("line 3", result.stderr)
        # Nothing was dumped for the lines before it.
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.backups.exists())

    def test_given_an_item_missing_its_arguments_when_run_then_it_is_refused(self):
        for line in ("volume", "postgres app-db-1", "forgejo extra", "volume ../etc"):
            with self.subTest(line=line):
                self.conf(items=f"{line}\n")
                result = self.backup("run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(line, result.stderr)
                self.assertEqual(self.calls(), [])

    def test_given_comments_and_blank_lines_when_run_then_they_are_skipped(self):
        self.conf(items="# the data\n\n   volume data   \n")
        result = self.backup("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.today_files(), ["volume-data.tar.gz"])

    def test_given_a_rootful_engine_when_run_then_nothing_is_dumped(self):
        self.conf()
        shim = self.temp / "bin" / "docker"
        shim.write_text(shim.read_text().replace('"name=rootless"', '"name=cgroupns"'))
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rootless", result.stderr)
        self.assertEqual([call for call in self.calls() if not call.startswith("docker info")], [])

    def test_given_no_config_when_run_then_it_says_where_the_template_is(self):
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup.conf", result.stderr)
        self.assertIn("env/backup.conf.example", result.stderr)

    def test_given_an_unknown_command_when_run_then_the_usage_is_shown(self):
        result = self.backup("frobnicate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr)

    def test_given_backups_when_listed_then_the_days_and_their_files_are_shown(self):
        (self.backups / "2020-01-01").mkdir(parents=True)
        (self.backups / "2020-01-01" / "forgejo.zip").write_text("x" * 10)
        (self.backups / "2020-01-02").mkdir()
        result = self.backup("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "2020-01-01")
        self.assertIn("forgejo.zip", lines[1])
        self.assertEqual(lines[2], "2020-01-02")
        # list is a read: it needs no configuration and runs nothing.
        self.assertEqual(self.calls(), [])

    def test_given_a_complete_newest_day_when_listed_then_it_says_so(self):
        self.conf(items="forgejo\nvolume data\n")
        self.day("2020-01-01", "forgejo.zip")
        self.day("2020-01-02", "forgejo.zip", "volume-data.tar.gz")
        result = self.backup("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "Newest: 2020-01-02, complete (every item of backup.conf)")
        self.assertEqual(self.calls(), [])

    def test_given_an_incomplete_newest_day_when_listed_then_it_names_what_is_missing(self):
        self.conf(items="forgejo\nvolume data\npostgres db postgres\n")
        self.day("2020-01-02", "volume-data.tar.gz", "forgejo.zip.partial")
        result = self.backup("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1],
                         "Newest: 2020-01-02, INCOMPLETE: no forgejo.zip, postgres-db.sql.gz")

    def test_given_no_config_when_listed_then_the_newest_day_is_shown_without_a_verdict(self):
        self.day("2020-01-02", "forgejo.zip")
        result = self.backup("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Newest: 2020-01-02", result.stdout.splitlines()[-1])
        self.assertIn("backup.conf", result.stdout.splitlines()[-1])

    def test_given_no_backups_when_listed_then_it_says_so(self):
        result = self.backup("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No backups yet", result.stdout)

    def test_given_a_target_and_no_items_when_run_then_it_is_refused_before_dumping_or_sending(self):
        # The template with its target filled in: every item is still a comment.
        text = TEMPLATE.read_text().replace("BACKUP_TARGET=\n", "BACKUP_TARGET=backup@other:here\n")
        (self.secrets / "backup.conf").write_text(text)
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no items", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.backups.exists())

    def test_given_a_run_when_done_then_the_backups_are_private(self):
        self.conf()
        self.assertEqual(self.backup("run").returncode, 0)
        day = self.backups / self.today
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(day.stat().st_mode), 0o700)
        for name in self.today_files():
            self.assertEqual(stat.S_IMODE((day / name).stat().st_mode), 0o600, name)

    @staticmethod
    def kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_given_a_run_in_progress_when_another_starts_then_it_exits_without_dumping(self):
        self.conf()
        self.backups.mkdir()
        holder = subprocess.Popen(["flock", str(self.backups), "sleep", "30"], start_new_session=True)
        # The whole group: whatever flock started holds the lock too.
        self.addCleanup(self.kill_group, holder)
        # Wait for the holder to have the lock.
        for _ in range(100):
            if subprocess.run(["flock", "-n", str(self.backups), "true"]).returncode != 0:
                break
            time.sleep(0.05)
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another backup is running", result.stderr)
        self.assertEqual(self.calls(), [])
        self.kill_group(holder)
        holder.wait()
        # The lock goes with the process that held it.
        self.assertEqual(self.backup("run").returncode, 0)

    def test_given_the_template_when_run_then_it_is_refused_for_want_of_a_target_only(self):
        # The copy nobody edited: BACKUP_TARGET is empty on purpose, and every item is a comment.
        shutil.copy(TEMPLATE, self.secrets / "backup.conf")
        result = self.backup("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BACKUP_TARGET is not set", result.stderr)


class UnitTests(unittest.TestCase):
    def read(self, name):
        return (UNITS / name).read_text()

    def test_given_the_service_when_read_then_it_runs_the_backup_on_the_users_engine(self):
        text = self.read("yggdrasil-backup.service")
        self.assertIn("ExecStart=/opt/yggdrasil/scripts/backup.sh run", text)
        # A user unit doesn't read .profile: %t is the user's runtime directory.
        self.assertIn("Environment=DOCKER_HOST=unix://%t/docker.sock", text)
        self.assertIn("Type=oneshot", text)
        # The engine is the user unit docker.service: a catch-up run at boot waits for it.
        self.assertIn("After=docker.service", text)
        self.assertIn("Wants=docker.service", text)

    def test_given_the_timer_when_read_then_it_fires_nightly_and_catches_up(self):
        text = self.read("yggdrasil-backup.timer")
        self.assertIn("OnCalendar=*-*-* 03:30:00", text)
        self.assertIn("Persistent=true", text)
        self.assertIn("RandomizedDelaySec=15m", text)
        self.assertIn("WantedBy=timers.target", text)


if __name__ == "__main__":
    unittest.main()
