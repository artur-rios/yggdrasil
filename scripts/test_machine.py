"""Tests for the machine roles of scripts/host.sh (scripts/machine.sh): packages, rootful Docker off,
the system settings and the role file.

    python3 -m unittest discover -s scripts -p "test_machine.py"

Everything that needs root or changes the system is a fake on PATH: `sudo` (logs, then runs the
command, which only ever touches the temporary $YGG_SYSROOT), `apt-get`, `dpkg-query`, `dpkg`, `curl`,
`systemctl`, `sysctl`, `apparmor_parser`, `gpasswd`, `getent`, `id` and `docker`. The fakes keep their
state in $FAKE_STATE (packages installed, active units, the docker group), so a second run sees what
the first one did, and log every call to $FAKE_STATE/calls.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

HOST = pathlib.Path(__file__).resolve().parent / "host.sh"

# Each fake is a bash script. State lives in plain files under $FAKE_STATE.
FAKES = {
    "sudo": r"""
        echo "sudo $*" >>"$FAKE_STATE/calls"
        exec "$@"
    """,
    # `apt-get install` records the packages, so dpkg-query then reports them installed.
    "apt-get": r"""
        echo "apt-get $*" >>"$FAKE_STATE/calls"
        action=""
        for argument in "$@"; do
          if [[ -z "$action" ]]; then
            [[ "$argument" == -* ]] || action=$argument
          elif [[ "$argument" != -* ]]; then
            case $action in
              install) echo "$argument" >>"$FAKE_STATE/installed" ;;
              remove) sed -i "/^$argument\$/d" "$FAKE_STATE/installed" ;;
            esac
          fi
        done
    """,
    "dpkg-query": r"""
        package=${*: -1}
        grep -qx -- "$package" "$FAKE_STATE/installed" || exit 1
        printf 'install ok installed'
    """,
    "dpkg": r"""
        [[ "$1" == --print-architecture ]] && echo amd64
        exit 0
    """,
    # `curl ... -o <file>` creates the file, as downloading the repository key would.
    "curl": r"""
        echo "curl $*" >>"$FAKE_STATE/calls"
        while (($#)); do
          [[ "$1" == -o ]] && echo "key" >"$2"
          shift
        done
    """,
    # $FAKE_STATE/active lists the running units, one per line ("<unit>=<state>" for a state other than
    # active); /enabled the enabled ones and /masked the masked ones. disable --now stops and disables,
    # mask masks.
    "systemctl": r"""
        echo "systemctl $*" >>"$FAKE_STATE/calls"
        case "$1" in
          is-active)
            shift
            status=0
            for unit in "$@"; do
              line=$(grep -E -- "^${unit//./\.}(=|\$)" "$FAKE_STATE/active" | head -n1 || true)
              if [[ -z "$line" ]]; then echo inactive; status=3
              elif [[ "$line" == *=* ]]; then echo "${line#*=}"; status=3
              else echo active; fi
            done
            exit "$status"
            ;;
          is-enabled)
            if grep -qx -- "$2" "$FAKE_STATE/masked"; then echo masked; exit 1
            elif grep -qx -- "$2" "$FAKE_STATE/enabled"; then echo enabled
            else echo disabled; exit 1; fi
            ;;
          disable)
            for unit in "${@:3}"; do
              sed -i "/^$unit\(=.*\)\?\$/d" "$FAKE_STATE/active"
              sed -i "/^$unit\$/d" "$FAKE_STATE/enabled"
            done
            ;;
          mask)
            for unit in "${@:2}"; do echo "$unit" >>"$FAKE_STATE/masked"; done
            ;;
        esac
    """,
    "sysctl": r"""
        echo "sysctl $*" >>"$FAKE_STATE/calls"
    """,
    "apparmor_parser": r"""
        echo "apparmor_parser $*" >>"$FAKE_STATE/calls"
    """,
    # The docker group is one line in $FAKE_STATE/group: "<gid>:<comma separated members>".
    "gpasswd": r"""
        echo "gpasswd $*" >>"$FAKE_STATE/calls"
        if [[ "$1" == -d ]]; then
          members=$(cut -d: -f2 "$FAKE_STATE/group" | tr ',' '\n' | grep -vx -- "$2" | paste -sd, -)
          echo "$(cut -d: -f1 "$FAKE_STATE/group"):$members" >"$FAKE_STATE/group"
        fi
    """,
    "getent": r"""
        if [[ "$1" == group && "$2" == docker && -s "$FAKE_STATE/group" ]]; then
          IFS=: read -r gid members <"$FAKE_STATE/group"
          echo "docker:x:$gid:$members"
        else
          exit 2
        fi
    """,
    # The admin is "admin" (uid 1000, or $FAKE_UID); it is in no group but its own.
    "id": r"""
        case "$*" in
          -un) echo admin ;;
          -u) echo "${FAKE_UID:-1000}" ;;
          -nG | "-nG admin") echo admin ;;
          *) echo "fake id: unexpected call: $*" >&2; exit 64 ;;
        esac
    """,
    # An engine that is installed but whose daemon the admin can't reach: what a workstation
    # without docker-group membership looks like.
    "docker": r"""
        case "$1" in
          --version) echo "Docker version 27.0.0, build abc" ;;
          info) exit 1 ;;
          compose) echo "2.30.0" ;;
          buildx) echo "github.com/docker/buildx v0.17.0 abc" ;;
        esac
    """,
}

DOCKER_PACKAGES = [
    "docker-ce", "docker-ce-cli", "containerd.io", "docker-ce-rootless-extras",
    "docker-buildx-plugin", "docker-compose-plugin",
]
EVERY_PACKAGE = [
    "git", "curl", "openssl", "python3", "apache2-utils", "ca-certificates", "python3-yaml",
    "python3-cryptography", "uidmap", "dbus-user-session", "slirp4netns", *DOCKER_PACKAGES, "ufw",
    "rsync", "unattended-upgrades",
]
DOCKER_UNITS = ["docker.service", "docker.socket", "containerd.service"]

SYSCTL = "net.ipv4.ip_unprivileged_port_start=80\n"
APPARMOR = textwrap.dedent("""\
    abi <abi/4.0>,
    include <tunables/global>

    "/usr/bin/rootlesskit" flags=(unconfined) {
      userns,

      include if exists <local/usr.bin.rootlesskit>
    }
""")
DELEGATE = "[Service]\nDelegate=cpu cpuset io memory pids\n"

UBUNTU = 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\nPRETTY_NAME="Ubuntu 24.04 LTS"\n'
DEBIAN = 'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\nPRETTY_NAME="Debian GNU/Linux 12"\n'


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class MachineRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-machine-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.state = self.temp / "state"
        self.sysroot = self.temp / "root"
        bin_dir = self.temp / "bin"
        for directory in (self.state, self.sysroot / "etc" / "apt" / "sources.list.d", bin_dir, self.temp / "home"):
            directory.mkdir(parents=True)
        for name, body in FAKES.items():
            fake = bin_dir / name
            fake.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body).lstrip())
            fake.chmod(0o755)
        (self.sysroot / "etc" / "os-release").write_text(UBUNTU)
        (self.state / "installed").write_text("\n".join(EVERY_PACKAGE) + "\n")
        (self.state / "active").write_text("")
        (self.state / "enabled").write_text("")
        (self.state / "masked").write_text("")
        (self.state / "group").write_text("")
        (self.state / "calls").write_text("")
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.temp / "home"),
            "FAKE_STATE": str(self.state),
            "YGG_SYSROOT": str(self.sysroot),
            "YGG_ASSUME_YES": "1",
            "YGG_SECRETS_DIR": str(self.temp / "secrets"),
            "YGG_APPS_DIR": str(self.temp / "apps"),
            "YGG_PLAIN": "1",
            "NO_COLOR": "1",
        }

    def host(self, *arguments, **env):
        return subprocess.run(
            ["bash", str(HOST), *arguments], env={**self.env, **env}, capture_output=True, text=True
        )

    def calls(self):
        return (self.state / "calls").read_text().splitlines()

    def etc(self, path):
        return self.sysroot / "etc" / path

    def set_packages(self, packages):
        (self.state / "installed").write_text("\n".join(packages) + "\n")

    def set_active(self, units):
        (self.state / "active").write_text("".join(unit + "\n" for unit in units))

    def set_enabled(self, units):
        (self.state / "enabled").write_text("".join(unit + "\n" for unit in units))

    def set_docker_group(self, members):
        (self.state / "group").write_text("999:" + ",".join(members) + "\n")

    def install(self, role, **env):
        result = self.host("install", role, **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_given_no_role_file_when_role_is_read_then_it_is_workstation(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{HOST.parent}/machine.sh"; machine_role'],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), "workstation", result.stderr)

    def test_given_a_role_file_when_role_is_read_then_its_first_word_is_the_role(self):
        self.etc("yggdrasil-role").write_text("apps\n")
        result = subprocess.run(
            ["bash", "-c", f'source "{HOST.parent}/machine.sh"; machine_role; machine_role dev'],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.split(), ["apps", "dev"], result.stderr)

    def test_given_each_role_when_its_users_are_listed_then_they_are_in_order(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{HOST.parent}/machine.sh"; role_users dev; echo --; role_users apps;'
             f" echo --; role_users workstation; echo --; secrets_dir_of ygg; secrets_dir_of ci"],
            env=self.env, capture_output=True, text=True,
        )
        root = str(self.sysroot)
        self.assertEqual(
            result.stdout.split(),
            ["ygg", "ci", "release", "renovate", "dev", "--", "ygg", "--", "--",
             f"{root}/etc/yggdrasil", f"{root}/etc/yggdrasil-ci"],
            result.stderr,
        )

    def test_given_role_dev_when_installed_then_the_role_file_says_dev(self):
        self.install("dev")
        self.assertEqual(self.etc("yggdrasil-role").read_text().split(), ["dev"])
        self.assertEqual(self.etc("yggdrasil-role").stat().st_mode & 0o777, 0o644)

    def test_given_the_role_flag_when_installed_then_it_is_the_same_as_the_bare_role(self):
        result = self.host("install", "--role", "apps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.etc("yggdrasil-role").read_text().split(), ["apps"])

    def test_given_an_unknown_role_when_checked_then_it_is_refused(self):
        result = self.host("check", "laptop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown role", result.stderr)

    def test_given_a_running_rootful_engine_when_checked_then_it_is_listed_to_turn_off(self):
        self.set_active(["docker.service", "docker.socket"])
        result = self.host("check", "apps")
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines() if "rootful docker" in line)
        self.assertNotIn("ok", line.split("rootful docker")[0])
        self.assertIn("docker.service", line)
        self.assertIn("docker.socket", line)
        self.assertNotIn("containerd.service", line)

    def test_given_stopped_but_enabled_docker_units_when_checked_then_they_are_listed_to_turn_off(self):
        self.set_enabled(["docker.socket"])
        result = self.host("check", "apps")
        line = next(line for line in result.stdout.splitlines() if "rootful docker" in line)
        self.assertNotIn("ok", line.split("rootful docker")[0])
        self.assertIn("enabled: docker.socket", line)
        self.assertNotIn("running", line)

    def test_given_a_failed_docker_unit_when_checked_then_it_is_not_off(self):
        self.set_active(["docker.service=failed"])
        result = self.host("check", "apps")
        line = next(line for line in result.stdout.splitlines() if "rootful docker" in line)
        self.assertNotIn("ok", line.split("rootful docker")[0])
        self.assertIn("docker.service (failed)", line)

    def test_given_masked_and_stopped_docker_units_when_checked_then_the_row_is_ok(self):
        (self.state / "masked").write_text("".join(unit + "\n" for unit in DOCKER_UNITS))
        result = self.host("check", "apps")
        line = next(line for line in result.stdout.splitlines() if "rootful docker" in line)
        self.assertIn("ok", line.split("rootful docker")[0])

    def test_given_a_docker_socket_when_checked_then_the_rootful_engine_is_not_off(self):
        sock = self.sysroot / "var" / "run" / "docker.sock"
        sock.parent.mkdir(parents=True)
        sock.touch()
        result = self.host("check", "dev")
        line = next(line for line in result.stdout.splitlines() if "rootful docker" in line)
        self.assertIn("docker.sock", line)
        self.assertNotIn(" ok ", line)

    def test_given_a_machine_set_up_when_checked_then_every_system_row_is_ok(self):
        self.install("apps")
        result = self.host("check", "apps")
        rows = [line for line in result.stdout.splitlines() if line.startswith("  ")]
        names = ["role", "packages", "rootful docker", "docker group", "unprivileged ports",
                 "apparmor rootlesskit", "cgroup delegation"]
        for name in names:
            line = next((line for line in rows if line.split()[1:2] == name.split()[:1] and name in line), None)
            self.assertIsNotNone(line, f"no row {name!r} in:\n{result.stdout}")
            self.assertIn("ok", line.split(name)[0], line)
        self.assertIn("System", result.stdout)

    def test_given_role_apps_when_installed_then_the_three_docker_units_are_disabled_and_stopped(self):
        self.set_active(DOCKER_UNITS)
        self.install("apps")
        self.assertIn("systemctl disable --now docker.service docker.socket containerd.service", self.calls())

    def test_given_role_apps_when_installed_then_the_three_docker_units_are_masked_after_disabling(self):
        self.set_active(DOCKER_UNITS)
        self.set_enabled(DOCKER_UNITS)
        self.install("apps")
        calls = self.calls()
        disable = "systemctl disable --now docker.service docker.socket containerd.service"
        mask = "systemctl mask docker.service docker.socket containerd.service"
        self.assertLess(calls.index(disable), calls.index(mask))
        self.assertEqual((self.state / "masked").read_text().split(), DOCKER_UNITS)
        result = self.host("check", "apps")
        line = next(line for line in result.stdout.splitlines() if "rootful docker" in line)
        self.assertIn("ok", line.split("rootful docker")[0])

    def test_given_role_apps_when_installed_then_unprivileged_ports_start_at_80(self):
        self.install("apps")
        path = self.etc("sysctl.d/90-yggdrasil.conf")
        self.assertEqual(path.read_text(), SYSCTL)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        calls = self.calls()
        self.assertIn("sysctl --system", calls)
        self.assertLess(calls.index(f"sudo tee {path}"), calls.index("sysctl --system"))

    def test_given_role_apps_when_installed_then_rootlesskit_may_use_user_namespaces(self):
        self.install("apps")
        self.assertEqual(self.etc("apparmor.d/usr.bin.rootlesskit").read_text(), APPARMOR)
        self.assertIn("systemctl restart apparmor.service", self.calls())

    def test_given_role_apps_when_installed_then_the_user_slice_delegates_controllers(self):
        self.install("apps")
        self.assertEqual(self.etc("systemd/system/user@.service.d/delegate.conf").read_text(), DELEGATE)
        self.assertIn("systemctl daemon-reload", self.calls())

    def test_given_role_apps_when_installed_twice_then_each_system_file_is_written_once_with_the_same_content(self):
        self.install("apps")
        first = {p: p.read_text() for p in self.sysroot.rglob("*") if p.is_file() and "os-release" not in p.name}
        self.install("apps")
        second = {p: p.read_text() for p in self.sysroot.rglob("*") if p.is_file() and "os-release" not in p.name}
        self.assertEqual(first, second)
        writes = [call for call in self.calls() if call.startswith("sudo tee ")]
        self.assertEqual(len(writes), len(set(writes)), writes)
        for name in ("90-yggdrasil.conf", "usr.bin.rootlesskit", "delegate.conf", "yggdrasil-role"):
            self.assertEqual(sum(call.endswith(name) for call in writes), 1, (name, writes))

    def test_given_a_changed_system_file_when_installed_then_it_is_written_again(self):
        self.install("apps")
        self.etc("sysctl.d/90-yggdrasil.conf").write_text("net.ipv4.ip_unprivileged_port_start=1024\n")
        self.install("apps")
        self.assertEqual(self.etc("sysctl.d/90-yggdrasil.conf").read_text(), SYSCTL)

    def test_given_members_in_the_docker_group_when_installed_then_each_is_removed(self):
        self.set_docker_group(["alice", "admin"])
        self.install("dev")
        calls = self.calls()
        self.assertIn("gpasswd -d alice docker", calls)
        self.assertIn("gpasswd -d admin docker", calls)
        self.assertEqual((self.state / "group").read_text().strip(), "999:")

    def test_given_missing_packages_when_installed_then_apt_installs_them_from_dockers_repository(self):
        self.set_packages(["git", "curl", "openssl", "python3", "ca-certificates"])
        self.install("dev")
        calls = self.calls()
        install = next(call for call in calls if call.startswith("apt-get -q install -y") and "uidmap" in call)
        for package in EVERY_PACKAGE:
            if package not in ("git", "curl", "openssl", "python3", "ca-certificates"):
                self.assertIn(package, install.split())
        source = self.etc("apt/sources.list.d/docker.list").read_text()
        self.assertIn("https://download.docker.com/linux/ubuntu noble stable", source)
        self.assertTrue(self.etc("apt/keyrings/docker.asc").exists())
        result = self.host("check", "dev")
        line = next(line for line in result.stdout.splitlines() if "packages" in line)
        self.assertIn("ok", line.split("packages")[0], result.stdout)

    def test_given_distribution_docker_packages_when_installed_then_they_are_removed_first(self):
        self.set_packages([*EVERY_PACKAGE, "docker.io"])
        self.install("dev")
        self.assertIn("apt-get -q remove -y docker.io", self.calls())

    def test_given_a_failing_removal_of_distribution_packages_when_installed_then_it_stops_with_its_message(self):
        self.set_packages([*EVERY_PACKAGE, "docker.io"])
        fake = pathlib.Path(self.env["PATH"].split(os.pathsep)[0]) / "apt-get"
        fake.write_text("#!/usr/bin/env bash\n[[ \"$*\" == *remove* ]] && exit 100\nexit 0\n")
        result = self.host("install", "dev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not remove docker.io", result.stderr)

    def test_given_declined_removal_of_distribution_packages_when_installed_then_they_are_left_alone(self):
        self.set_packages([*EVERY_PACKAGE, "docker.io"])
        env = {k: v for k, v in self.env.items() if k != "YGG_ASSUME_YES"}
        result = subprocess.run(
            ["bash", str(HOST), "install", "dev"], env=env, capture_output=True, text=True, input="y\nn\ny\ny\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("left as they are", result.stderr)
        self.assertFalse(any("remove" in call for call in self.calls()))

    def test_given_role_workstation_when_checked_then_the_report_is_todays(self):
        bare = self.host("check")
        flagged = self.host("check", "--role", "workstation")
        named = self.host("check", "workstation")
        for result in (bare, flagged, named):
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bare.stdout, named.stdout)
        self.assertEqual(bare.stdout, flagged.stdout)
        self.assertIn("docker group", bare.stdout)
        self.assertIn("secrets directory", bare.stdout)
        self.assertNotIn("rootful docker", bare.stdout)
        self.assertNotIn("== System ==", bare.stdout)

    def test_given_debian_when_installing_role_dev_then_it_is_refused(self):
        self.etc("os-release").write_text(DEBIAN)
        result = self.host("install", "dev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ubuntu 24.04", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_given_ubuntu_22_when_installing_role_apps_then_it_is_refused(self):
        self.etc("os-release").write_text(UBUNTU.replace("24.04", "22.04"))
        result = self.host("install", "apps")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("24.04", result.stderr)

    def test_given_root_when_installing_role_dev_then_it_is_refused(self):
        result = self.host("install", "dev", FAKE_UID="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not as root", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_given_assume_yes_unset_when_installing_then_nothing_is_changed_without_an_answer(self):
        env = {k: v for k, v in self.env.items() if k != "YGG_ASSUME_YES"}
        result = subprocess.run(
            ["bash", str(HOST), "install", "apps"], env=env, capture_output=True, text=True, input="n\nn\nn\nn\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.etc("yggdrasil-role").exists())
        self.assertEqual([c for c in self.calls() if c.startswith("sudo tee")], [])


if __name__ == "__main__":
    unittest.main()
