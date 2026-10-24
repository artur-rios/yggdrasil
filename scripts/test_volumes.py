"""Tests for scripts/volumes.sh (carries Docker volumes, databases included, from the old VPS to the apps
machine) and for the tar image it shares with scripts/backup.sh.

    python3 -m unittest discover -s scripts -p "test_volumes.py"

`docker` is a fake on PATH that keeps the volumes in a state directory ($FAKE_STATE/volumes/<name>/ holds
the labels, in `project` and `volume`, and the files, in `data/`) and logs every call to $FAKE_LOG. `tar`
inside its `docker run` is the host's own, run on the volume's directory, so the tar files are real and
their checksums are too. `id` is a fake that answers $FAKE_UID, to run as "root" without being root.

    FAKE_ENGINE=rootful      `docker info` answers as a rootful engine (rootless otherwise)
    FAKE_IN_USE="a b"        running containers mount those volumes
    FAKE_FAIL=<text>         a docker call whose arguments contain <text> fails
"""

import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "volumes.sh"
ENGINE = SCRIPTS / "docker-engine.sh"
BACKUP = SCRIPTS / "backup.sh"

TAR_IMAGE = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"

FAKES = {
    "docker": r"""
        echo "docker $*" >>"$FAKE_LOG"
        if [[ -n "${FAKE_FAIL:-}" && "$*" == *"$FAKE_FAIL"* ]]; then
          echo "fake docker: failing on purpose" >&2
          exit 1
        fi
        state=$FAKE_STATE/volumes
        mkdir -p "$state"
        case "$1" in
          info)
            if [[ "${FAKE_ENGINE:-rootless}" == rootful ]]; then
              echo '["name=seccomp,profile=builtin","name=cgroupns"]'
            else
              echo '["name=seccomp,profile=builtin","name=rootless"]'
            fi
            ;;
          volume)
            case "$2" in
              ls)
                # With a --filter: the volumes that have a Compose project label; without: all of them.
                for dir in "$state"/*/; do
                  [[ -d "$dir" ]] || continue
                  if [[ "$*" == *--filter* && ! -s "$dir/project" ]]; then continue; fi
                  basename "$dir"
                done
                ;;
              inspect)
                name=${*: -1}
                if [[ ! -d "$state/$name" ]]; then
                  echo "Error: No such volume: $name" >&2
                  exit 1
                fi
                printf '%s|%s\n' "$(cat "$state/$name/project")" "$(cat "$state/$name/volume")"
                ;;
              create)
                shift 2
                project="" compose_volume="" name=""
                while (($#)); do
                  case "$1" in
                    --label)
                      case "$2" in
                        com.docker.compose.project=*) project=${2#*=} ;;
                        com.docker.compose.volume=*) compose_volume=${2#*=} ;;
                      esac
                      shift 2
                      ;;
                    *) name=$1; shift ;;
                  esac
                done
                mkdir -p "$state/$name/data"
                printf '%s' "$project" >"$state/$name/project"
                printf '%s' "$compose_volume" >"$state/$name/volume"
                echo "$name"
                ;;
              rm) rm -rf "$state/$3" ;;
            esac
            ;;
          ps)
            for arg in "$@"; do
              if [[ "$arg" == volume=* && " ${FAKE_IN_USE:-} " == *" ${arg#volume=} "* ]]; then
                echo "container-of-${arg#volume=}"
              fi
            done
            ;;
          run)
            shift
            data="" image="" args=()
            while (($#)); do
              if [[ -n "$image" ]]; then
                args+=("${1//\/volume/$data}")
                shift
                continue
              fi
              case "$1" in
                --volume)
                  name=${2%%:*}
                  data=$state/$name/data
                  if [[ ! -d "$data" ]]; then
                    echo "fake docker: no volume $name (real docker would create it)" >&2
                    exit 125
                  fi
                  shift 2
                  ;;
                --rm | -i) shift ;;
                *) image=$1; shift ;;
              esac
            done
            exec "${args[@]}"
            ;;
        esac
        exit 0
    """,
    "id": r"""
        if [[ -n "${FAKE_UID:-}" && "$1" == -u ]]; then
          echo "$FAKE_UID"
          exit 0
        fi
        exec /usr/bin/id "$@"
    """,
}

APP_DATA = "app_data"
APP_DB = "app_db"


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class VolumesTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-volumes-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        for name, body in FAKES.items():
            fake = bin_dir / name
            fake.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body).lstrip())
            fake.chmod(0o755)
        self.log = self.temp / "calls"
        self.log.write_text("")
        # The old VPS' volumes and the new machine's, each its own engine.
        self.old = self.temp / "old"
        self.new = self.temp / "new"
        self.out = self.temp / "export"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.temp / "home"),
            "DOCKER_HOST": f"unix://{self.temp}/docker.sock",
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.old),
        }
        self.env.pop("XDG_RUNTIME_DIR", None)
        self.env.pop("FAKE_UID", None)

    def volumes(self, *arguments, script=SCRIPT, **env):
        merged = {**self.env, **env}
        merged = {key: value for key, value in merged.items() if value is not None}
        return subprocess.run(["bash", str(script), *arguments], env=merged, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def make_volume(self, state, name, project=None, compose_volume=None, files=None):
        directory = state / "volumes" / name
        (directory / "data").mkdir(parents=True)
        (directory / "project").write_text(project or "")
        (directory / "volume").write_text(compose_volume or "")
        for path, content in (files or {}).items():
            target = directory / "data" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def old_vps(self):
        """Two stopped Compose volumes and one that isn't Compose's."""
        self.make_volume(self.old, APP_DATA, "app", "data", {"a.txt": "alpha", "dir/b.txt": "bravo"})
        self.make_volume(self.old, APP_DB, "app", "db", {"PG_VERSION": "16"})
        self.make_volume(self.old, "loose", files={"x": "not compose's"})

    def manifest(self, directory=None):
        text = ((directory or self.out) / "manifest.tsv").read_text()
        return [line.split("\t") for line in text.splitlines()]

    def exported(self):
        result = self.volumes("export", str(self.out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def new_volumes(self):
        directory = self.new / "volumes"
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def import_new(self, **env):
        return self.volumes("import", str(self.out), FAKE_STATE=str(self.new), **env)

    # ---- export ------------------------------------------------------------------------------------

    def test_given_stopped_volumes_when_exported_then_each_has_a_tar_and_a_manifest_line(self):
        self.old_vps()
        result = self.volumes("export", str(self.out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Every Compose volume, and only those: the one without a project label is not carried.
        self.assertEqual(sorted(path.name for path in self.out.iterdir()),
                         [f"{APP_DATA}.tar", f"{APP_DB}.tar", "manifest.tsv"])
        rows = {row[0]: row for row in self.manifest()}
        self.assertEqual(sorted(rows), [APP_DATA, APP_DB])
        for name, compose_volume in ((APP_DATA, "data"), (APP_DB, "db")):
            volume, project, volume_label, size, checksum = rows[name]
            tar = self.out / f"{name}.tar"
            self.assertEqual((volume, project, volume_label), (name, "app", compose_volume))
            self.assertEqual(int(size), tar.stat().st_size)
            self.assertEqual(checksum, subprocess.run(["sha256sum", str(tar)], capture_output=True, text=True)
                             .stdout.split()[0])
        # The default is found by the Compose label, and the volume is mounted read-only in the pinned image.
        calls = self.calls()
        self.assertTrue([c for c in calls if c.startswith("docker volume ls") and
                         "label=com.docker.compose.project" in c])
        run = next(c for c in calls if c.startswith("docker run") and f"{APP_DATA}:/volume" in c)
        self.assertIn(f"{APP_DATA}:/volume:ro", run)
        self.assertIn(f"{TAR_IMAGE} tar", run)
        self.assertIn("--numeric-owner", run)
        # What the tar holds is the volume's files.
        listing = subprocess.run(["tar", "-tf", str(self.out / f"{APP_DATA}.tar")], capture_output=True, text=True)
        self.assertIn("a.txt", listing.stdout)
        self.assertIn("dir/b.txt", listing.stdout)

    def test_given_explicit_names_when_exported_then_only_those_are(self):
        self.old_vps()
        result = self.volumes("export", str(self.out), APP_DB)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [f"{APP_DB}.tar", "manifest.tsv"])
        self.assertEqual([row[0] for row in self.manifest()], [APP_DB])

    def test_given_an_unknown_name_when_exported_then_it_fails_naming_it_and_writes_nothing(self):
        self.old_vps()
        result = self.volumes("export", str(self.out), APP_DB, "nope", "gone")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nope", result.stderr)
        self.assertIn("gone", result.stderr)
        self.assertIn("no such volume", result.stderr.lower())
        self.assertFalse(self.out.exists())

    def test_given_a_volume_without_compose_labels_when_named_then_it_is_refused(self):
        self.old_vps()
        result = self.volumes("export", str(self.out), "loose")
        self.assertEqual(result.returncode, 1)
        self.assertIn("loose", result.stderr)
        self.assertIn("com.docker.compose", result.stderr)
        self.assertFalse(self.out.exists())

    def test_given_a_volume_in_use_when_exported_then_it_is_refused(self):
        self.old_vps()
        self.make_volume(self.old, "app_cache", "app", "cache")
        result = self.volumes("export", str(self.out), FAKE_IN_USE=f"{APP_DATA} app_cache")
        self.assertEqual(result.returncode, 1)
        # One message names every volume that is in use, and not the one that is free.
        self.assertIn(APP_DATA, result.stderr)
        self.assertIn("app_cache", result.stderr)
        self.assertNotIn(APP_DB, result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        # Nothing was written, not even the directory, and no tar was started.
        self.assertFalse(self.out.exists())
        self.assertEqual([c for c in self.calls() if c.startswith("docker run")], [])
        ps = [c for c in self.calls() if c.startswith("docker ps")]
        self.assertTrue(ps and all("--filter volume=" in c for c in ps))

    def test_given_a_tar_failing_when_exported_then_no_partial_file_and_no_manifest_are_left(self):
        self.old_vps()
        result = self.volumes("export", str(self.out), FAKE_FAIL=f"{APP_DB}:/volume")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(APP_DB, result.stderr)
        # The first volume was whole and stays; the failing one left nothing; the manifest comes last.
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [f"{APP_DATA}.tar"])

    def test_given_a_directory_with_a_manifest_when_exported_then_it_is_refused(self):
        self.old_vps()
        self.out.mkdir()
        (self.out / "manifest.tsv").write_text("older export\n")
        result = self.volumes("export", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("manifest.tsv", result.stderr)
        self.assertEqual((self.out / "manifest.tsv").read_text(), "older export\n")

    def test_given_no_compose_volumes_when_exported_then_it_says_so(self):
        self.make_volume(self.old, "loose")
        result = self.volumes("export", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("com.docker.compose.project", result.stderr)
        self.assertFalse(self.out.exists())

    def test_given_the_files_when_exported_then_they_are_private(self):
        self.old_vps()
        self.exported()
        for path in [self.out, *self.out.iterdir()]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, path)

    # ---- import ------------------------------------------------------------------------------------

    def test_given_a_manifest_when_imported_then_volumes_come_back_with_their_compose_labels(self):
        self.old_vps()
        self.exported()
        result = self.import_new()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.new_volumes(), [APP_DATA, APP_DB])
        for name, compose_volume in ((APP_DATA, "data"), (APP_DB, "db")):
            directory = self.new / "volumes" / name
            self.assertEqual((directory / "project").read_text(), "app")
            self.assertEqual((directory / "volume").read_text(), compose_volume)
        self.assertEqual((self.new / "volumes" / APP_DATA / "data" / "dir" / "b.txt").read_text(), "bravo")
        self.assertEqual((self.new / "volumes" / APP_DB / "data" / "PG_VERSION").read_text(), "16")
        calls = self.calls()
        # Created with exactly the labels of the manifest...
        self.assertIn(
            f"docker volume create --label com.docker.compose.project=app "
            f"--label com.docker.compose.volume=data {APP_DATA}", calls)
        # ... and untarred in the pinned image with permissions and numeric owners kept. Inside a container
        # of the engine it runs on, so a rootless engine maps uid 999 into its own namespace.
        untar = next(c for c in calls if c.startswith("docker run") and f"{APP_DATA}:/volume" in c and "-xpf" in c)
        self.assertIn(f"{TAR_IMAGE} tar", untar)
        self.assertIn("-xpf -", untar)
        self.assertIn("--numeric-owner", untar)
        self.assertNotIn(":ro", untar)

    def test_given_a_tar_with_another_checksum_when_imported_then_it_is_refused(self):
        self.old_vps()
        self.exported()
        # The second volume's tar changed on the way; the first is fine and must not be imported either.
        with open(self.out / f"{APP_DB}.tar", "ab") as tar:
            tar.write(b"changed")
        result = self.import_new()
        self.assertEqual(result.returncode, 1)
        self.assertIn(APP_DB, result.stderr)
        self.assertNotIn(APP_DATA, result.stderr)
        self.assertEqual(self.new_volumes(), [])
        self.assertEqual([c for c in self.calls() if c.startswith("docker volume create")], [])

    def test_given_a_missing_tar_when_imported_then_it_is_refused(self):
        self.old_vps()
        self.exported()
        (self.out / f"{APP_DATA}.tar").unlink()
        result = self.import_new()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{APP_DATA}.tar", result.stderr)
        self.assertEqual(self.new_volumes(), [])

    def test_given_a_volume_already_holding_data_when_imported_then_it_is_refused(self):
        self.old_vps()
        self.exported()
        self.make_volume(self.new, APP_DB, "app", "db", {"already": "mine"})
        result = self.import_new()
        self.assertEqual(result.returncode, 1)
        self.assertIn(APP_DB, result.stderr)
        # Nothing was created or touched: the other volume is not imported either.
        self.assertEqual(self.new_volumes(), [APP_DB])
        self.assertEqual((self.new / "volumes" / APP_DB / "data" / "already").read_text(), "mine")
        self.assertEqual([c for c in self.calls() if c.startswith("docker volume create")], [])

    def test_given_an_empty_volume_with_the_same_labels_when_imported_then_it_is_reused(self):
        self.old_vps()
        self.exported()
        self.make_volume(self.new, APP_DB, "app", "db")
        result = self.import_new()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.new / "volumes" / APP_DB / "data" / "PG_VERSION").read_text(), "16")
        created = [c for c in self.calls() if c.startswith("docker volume create")]
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].endswith(APP_DATA))

    def test_given_an_empty_volume_with_other_labels_when_imported_then_it_is_refused(self):
        self.old_vps()
        self.exported()
        self.make_volume(self.new, APP_DB, "someone-else", "db")
        result = self.import_new()
        self.assertEqual(result.returncode, 1)
        self.assertIn(APP_DB, result.stderr)
        self.assertIn("label", result.stderr)
        self.assertEqual(self.new_volumes(), [APP_DB])

    def test_given_an_untar_failing_when_imported_then_what_this_run_created_is_removed(self):
        self.old_vps()
        self.exported()
        self.make_volume(self.new, "kept", "other", "kept", {"mine": "yes"})
        result = self.import_new(FAKE_FAIL=f"--volume {APP_DB}:/volume")
        self.assertEqual(result.returncode, 1)
        self.assertIn(APP_DB, result.stderr)
        # A rerun finds a clean slate instead of a half-filled volume it would have to refuse.
        self.assertEqual(self.new_volumes(), ["kept"])
        self.assertIn(f"docker volume rm {APP_DATA}", self.calls())
        self.assertIn(f"docker volume rm {APP_DB}", self.calls())

    def test_given_a_malformed_manifest_when_imported_then_it_is_refused_before_anything(self):
        self.out.mkdir()
        for line in (
            "just-a-name",
            "../escape\tapp\tdata\t3\t" + "a" * 64,
            "name\tapp\tdata\tmany\t" + "a" * 64,
            "name\tapp\tdata\t3\tnot-a-checksum",
            "name\t\tdata\t3\t" + "a" * 64,
        ):
            with self.subTest(line=line):
                (self.out / "manifest.tsv").write_text(line + "\n")
                result = self.import_new()
                self.assertEqual(result.returncode, 1)
                self.assertIn("manifest.tsv", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_given_no_manifest_when_imported_then_it_is_refused(self):
        self.out.mkdir()
        result = self.import_new()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manifest.tsv", result.stderr)

    # ---- the engines -------------------------------------------------------------------------------

    def old_vps_as_root(self, *arguments, **env):
        """What the admin does on the old VPS: sudo, no DOCKER_HOST, the rootful engine."""
        return self.volumes(*arguments, **{"FAKE_UID": "0", "DOCKER_HOST": None, "FAKE_ENGINE": "rootful", **env})

    def test_given_root_on_a_rootful_engine_when_run_then_it_works(self):
        self.old_vps()
        result = self.old_vps_as_root("export", str(self.out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row[0] for row in self.manifest()], [APP_DATA, APP_DB])

    def test_given_root_with_docker_host_set_when_run_then_it_is_refused(self):
        self.old_vps()
        result = self.old_vps_as_root("export", str(self.out), DOCKER_HOST="unix:///run/user/1001/docker.sock")
        self.assertEqual(result.returncode, 1)
        self.assertIn("DOCKER_HOST", result.stderr)
        self.assertEqual([c for c in self.calls() if not c.startswith("docker info")], [])

    def test_given_root_on_a_rootless_engine_when_run_then_it_is_refused(self):
        self.old_vps()
        result = self.old_vps_as_root("export", str(self.out), FAKE_ENGINE="rootless")
        self.assertEqual(result.returncode, 1)
        self.assertIn("rootless", result.stderr)
        self.assertFalse(self.out.exists())

    def test_given_a_user_on_a_rootful_engine_when_run_then_it_is_refused(self):
        self.old_vps()
        result = self.volumes("export", str(self.out), FAKE_ENGINE="rootful")
        self.assertEqual(result.returncode, 1)
        self.assertIn("runs as root", result.stderr)
        self.assertFalse(self.out.exists())

    def test_given_a_user_without_a_rootless_engine_when_run_then_it_says_how_to_get_one(self):
        result = self.volumes("export", str(self.out), DOCKER_HOST=None, XDG_RUNTIME_DIR=str(self.temp / "run"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no rootless Docker engine", result.stderr)

    def test_given_only_the_script_and_the_engine_file_when_run_then_it_works(self):
        # The old VPS has a copy of the two files and nothing else of the repository.
        alone = self.temp / "alone"
        alone.mkdir()
        shutil.copy(SCRIPT, alone / "volumes.sh")
        shutil.copy(ENGINE, alone / "docker-engine.sh")
        self.old_vps()
        result = self.volumes("export", str(self.out), script=alone / "volumes.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row[0] for row in self.manifest()], [APP_DATA, APP_DB])

    # ---- the command line --------------------------------------------------------------------------

    def test_given_a_bad_command_line_when_run_then_the_usage_is_shown(self):
        for arguments in ((), ("export",), ("import",), ("import", "a", "b"), ("restore", "a")):
            with self.subTest(arguments=arguments):
                result = self.volumes(*arguments)
                self.assertEqual(result.returncode, 1)
                self.assertIn("usage: volumes.sh", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_given_a_name_that_is_not_a_volume_name_when_exported_then_it_is_refused(self):
        result = self.volumes("export", str(self.out), "../etc")
        self.assertEqual(result.returncode, 1)
        self.assertIn("../etc", result.stderr)
        self.assertEqual(self.calls(), [])

    # ---- the tar image -----------------------------------------------------------------------------

    def test_given_the_scripts_when_read_then_the_tar_image_is_defined_once_in_docker_engine(self):
        # backup.sh and volumes.sh tar with the same pinned image: one constant, in the file both source.
        self.assertRegex(ENGINE.read_text(), rf"(?m)^TAR_IMAGE={re.escape(TAR_IMAGE)}$")
        for script in (BACKUP, SCRIPT):
            text = script.read_text()
            self.assertNotIn("busybox:", text, script.name)
            self.assertIn('"$TAR_IMAGE"', text, script.name)


if __name__ == "__main__":
    unittest.main()
