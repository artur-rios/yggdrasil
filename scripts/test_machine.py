"""Tests for the machine roles of scripts/host.sh (scripts/machine.sh): packages, rootful Docker off,
the system settings, the service users, ssh, the checkout, the secrets, the firewall, the rootless
engines and the role file.

    python3 -m unittest discover -s scripts -p "test_machine.py"

Everything that needs root or changes the system is a fake on PATH: `sudo` (logs, then runs the
command, which only ever touches the temporary $YGG_SYSROOT), `apt-get`, `dpkg-query`, `dpkg`, `curl`,
`systemctl`, `sysctl`, `apparmor_parser`, `gpasswd`, `getent`, `id`, `docker`, `useradd`, `usermod`,
`chown`, `chmod`, `install`, `stat`, `sshd`, `ufw`, `loginctl`, `dockerd-rootless-setuptool.sh` and
`test`. The fakes keep their state in $FAKE_STATE
(packages installed, active units, the docker group, who owns what, ufw's rules) and in a fake $YGG_SYSROOT/etc/passwd, so a
second run sees what the first one did, and log every call to $FAKE_STATE/calls. None of them changes
an owner: `chown` only records it (the checkout step chowns the real checkout, which must stay as it
is), and `chmod` only touches what is under the test's temporary directory.
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
    # `sudo -l -U <user>` lists $FAKE_STATE/sudo-<user> when there is one, as sudo would a rule, and
    # otherwise says the user may not run sudo. `sudo -n` fails while $FAKE_STATE/sudo-denied exists
    # (a password would be asked for). `sudo -u <user> <command>` writes down who it runs as and from
    # where, and tells the fakes below (FAKE_AS_USER) so they can answer for that user.
    "sudo": r"""
        echo "sudo $*" >>"$FAKE_STATE/calls"
        if [[ "$1" == -n ]]; then
          shift
          [[ ! -e "$FAKE_STATE/sudo-denied" ]] || exit 1
        fi
        if [[ "$1" == -l ]]; then
          if [[ -s "$FAKE_STATE/sudo-$3" ]]; then cat "$FAKE_STATE/sudo-$3"
          else echo "User $3 is not allowed to run sudo on this host."; exit 1; fi
          exit 0
        fi
        if [[ "$1" == -u ]]; then
          export FAKE_AS_USER=$2
          echo "as $2 in $PWD" >>"$FAKE_STATE/calls"
          shift 2
        fi
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
    # `curl ... -o <file>` creates the file, as downloading the repository key would. Cloudflare's lists
    # come from $FAKE_CURL_DIR/<the url's basename> (ips-v4, ips-v6); curl -f fails (22) when the file
    # isn't there, as it does for an unreachable host or an HTTP error.
    "curl": r"""
        echo "curl $*" >>"$FAKE_STATE/calls"
        for argument in "$@"; do
          if [[ "$argument" == https://www.cloudflare.com/ips-* ]]; then
            [[ -f "$FAKE_CURL_DIR/${argument##*/}" ]] || exit 22
            cat "$FAKE_CURL_DIR/${argument##*/}"
            exit 0
          fi
        done
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
        if [[ "$1" == passwd ]]; then
          grep -- "^$2:" "$YGG_SYSROOT/etc/passwd" || exit 2
        elif [[ "$1" == group && "$2" == docker && -s "$FAKE_STATE/group" ]]; then
          IFS=: read -r gid members <"$FAKE_STATE/group"
          echo "docker:x:$gid:$members"
        else
          exit 2
        fi
    """,
    # The admin is "admin" (uid 1000, or $FAKE_UID); it is in no group but its own. Another user is in
    # its own group and those listed in $FAKE_STATE/groups-<user>.
    "id": r"""
        case "$*" in
          -un) echo "${FAKE_ADMIN:-admin}" ;;
          -u) echo "${FAKE_UID:-1000}" ;;
          -nG | "-nG admin") echo admin ;;
          "-nG "*) echo "$2 $(cat "$FAKE_STATE/groups-$2" 2>/dev/null)" ;;
          *) echo "fake id: unexpected call: $*" >&2; exit 64 ;;
        esac
    """,
    # A new user gets the next uid, a home under $FAKE_HOMES (as open as mkdir leaves it) and a line in
    # the fake passwd.
    "useradd": r"""
        echo "useradd $*" >>"$FAKE_STATE/calls"
        shell=""
        while (($#)); do
          case "$1" in
            --shell) shell=$2; shift ;;
            --*) ;;
            *) name=$1 ;;
          esac
          shift
        done
        uid=$((1000 + $(wc -l <"$YGG_SYSROOT/etc/passwd")))
        mkdir -p "$FAKE_HOMES/$name"
        echo "$name:x:$uid:$uid::$FAKE_HOMES/$name:$shell" >>"$YGG_SYSROOT/etc/passwd"
    """,
    "usermod": r"""
        echo "usermod $*" >>"$FAKE_STATE/calls"
        if [[ "$1" == --shell ]]; then
          sed -i "s|^\($3:.*:\)[^:]*\$|\1$2|" "$YGG_SYSROOT/etc/passwd"
        fi
    """,
    # Nothing is chowned: the owner is written down for `stat` below to give back.
    "chown": r"""
        echo "chown $*" >>"$FAKE_STATE/calls"
        [[ "$1" == -R ]] && shift
        printf '%s\t%s\n' "${@: -1}" "$1" >>"$FAKE_STATE/owners"
    """,
    "chmod": r"""
        echo "chmod $*" >>"$FAKE_STATE/calls"
        case "${@: -1}" in "$FAKE_SANDBOX"/*) /usr/bin/chmod "$@" ;; esac
    """,
    # `install -o <user> -g <group>` records the owner instead of setting it.
    "install": r"""
        arguments=() owner="" group=""
        while (($#)); do
          case "$1" in
            -o) owner=$2; shift 2 ;;
            -g) group=$2; shift 2 ;;
            *) arguments+=("$1"); shift ;;
          esac
        done
        /usr/bin/install "${arguments[@]}" || exit $?
        if [[ -n "$owner" ]]; then
          printf '%s\t%s:%s\n' "${arguments[-1]}" "$owner" "${group:-$owner}" >>"$FAKE_STATE/owners"
        fi
    """,
    "stat": r"""
        if [[ "$1 $2" == "-c %U:%G" ]]; then
          owner=$(awk -F'\t' -v path="$3" '$1 == path { found = $2 } END { print found }' "$FAKE_STATE/owners")
          if [[ -n "$owner" ]]; then echo "$owner"; exit 0; fi
        fi
        exec /usr/bin/stat "$@"
    """,
    # Like a fresh host's sshd, it wants /run/sshd. -t: FAKE_SSHD_STATUS=1 is a configuration it
    # rejects. -T: the effective configuration, as the real one reads it: every *.conf of sshd_config.d
    # in lexical order, the FIRST value of a key wins (the defaults are the permissive ones).
    "sshd": r"""
        echo "sshd $*" >>"$FAKE_STATE/calls"
        if [[ ! -d "$YGG_SYSROOT/run/sshd" ]]; then
          echo "Missing privilege separation directory: /run/sshd" >&2
          exit 255
        fi
        if [[ "$1" == -t && "${FAKE_SSHD_STATUS:-0}" != 0 ]]; then
          echo "fake sshd: bad configuration" >&2
          exit "$FAKE_SSHD_STATUS"
        fi
        if [[ "$1" == -T ]]; then
          declare -A seen
          for file in $(ls "$YGG_SYSROOT"/etc/ssh/sshd_config.d/*.conf 2>/dev/null | sort); do
            while read -r key value; do
              [[ -z "$key" || "$key" == \#* ]] && continue
              key=${key,,}
              [[ -n "${seen[$key]:-}" ]] || seen[$key]=$value
            done <"$file"
          done
          echo "permitrootlogin ${seen[permitrootlogin]:-without-password}"
          echo "passwordauthentication ${seen[passwordauthentication]:-yes}"
          echo "kbdinteractiveauthentication ${seen[kbdinteractiveauthentication]:-yes}"
          for user in ${seen[allowusers]:-}; do echo "allowusers $user"; done
        fi
    """,
    # The rules are lines of "<to><TAB><from><TAB><comment>" in $FAKE_STATE/ufw-rules, and
    # $FAKE_STATE/ufw-on exists while the firewall is enabled. Like ufw, it skips a rule it already has,
    # gives a rule for "Anywhere" an IPv6 twin, lists nothing while it is off, and deletes by what the
    # rule says (not by its comment).
    "ufw": r"""
        echo "ufw $*" >>"$FAKE_STATE/calls"
        rules="$FAKE_STATE/ufw-rules"
        touch "$rules"
        tab=$'\t'
        add() {
          if grep -qxF -- "$1$tab$2$tab$3" "$rules"; then echo "Skipping adding existing rule"
          else printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$rules"; fi
        }
        remove() {
          awk -F'\t' -v to="$1" -v from="$2" '
            $1 == to && $2 == from { next }
            from == "Anywhere" && $1 == to " (v6)" && $2 == "Anywhere (v6)" { next }
            { print }' "$rules" >"$rules.new"
          mv "$rules.new" "$rules"
        }
        case "$1" in
          status)
            if [[ ! -e "$FAKE_STATE/ufw-on" ]]; then echo "Status: inactive"; exit 0; fi
            printf 'Status: active\n\nTo                         Action      From\n--                         ------      ----\n'
            while IFS=$'\t' read -r to from comment; do
              printf '%-26s %-11s %-26s' "$to" ALLOW "$from"
              [[ -z "$comment" ]] || printf ' # %s' "$comment"
              echo
            done <"$rules"
            ;;
          --force)
            [[ "$2" == enable ]] && touch "$FAKE_STATE/ufw-on"
            ;;
          allow)
            shift
            if [[ "$1" == proto ]]; then
              # proto tcp from <cidr> to any port <ports> [comment <text>]
              add "$8/$2" "$4" "${10:-}"
            else
              add "$1" Anywhere ""
              add "$1 (v6)" "Anywhere (v6)" ""
            fi
            ;;
          delete)
            shift 2
            if [[ "$1" == proto ]]; then remove "$8/$2" "$4"
            else remove "$1" Anywhere; fi
            ;;
          *) echo "fake ufw: unexpected call: $*" >&2; exit 64 ;;
        esac
    """,
    # enable-linger records the user in the directory systemd keeps them in.
    "loginctl": r"""
        echo "loginctl $*" >>"$FAKE_STATE/calls"
        if [[ "$1" == enable-linger ]]; then
          mkdir -p "$YGG_SYSROOT/var/lib/systemd/linger"
          touch "$YGG_SYSROOT/var/lib/systemd/linger/$2"
        fi
    """,
    "dockerd-rootless-setuptool.sh": r"""
        echo "dockerd-rootless-setuptool.sh $* [as ${FAKE_AS_USER:-nobody}]" >>"$FAKE_STATE/calls"
    """,
    # Run as a service user, `test -r <path>` is true for the paths listed in
    # $FAKE_STATE/readable-<user> and false for all others; for anyone else it is the real one.
    "test": r"""
        if [[ -n "${FAKE_AS_USER:-}" && "$1" == -r ]]; then
          grep -qxF -- "$2" "$FAKE_STATE/readable-$FAKE_AS_USER" 2>/dev/null
          exit $?
        fi
        exec /usr/bin/test "$@"
    """,
    # An engine that is installed but whose daemon the admin can't reach: what a workstation
    # without docker-group membership looks like. Run as a service user (sudo -u), `info --format`
    # answers with $FAKE_STATE/security-<user> (a rootless engine's options unless it says otherwise;
    # "down" is an engine that doesn't answer).
    "docker": r"""
        case "$1" in
          --version) echo "Docker version 27.0.0, build abc" ;;
          info)
            [[ -n "${FAKE_AS_USER:-}" && "$2" == --format ]] || exit 1
            echo "docker $* [as $FAKE_AS_USER, DOCKER_HOST=${DOCKER_HOST:-}]" >>"$FAKE_STATE/calls"
            answer=$(cat "$FAKE_STATE/security-$FAKE_AS_USER" 2>/dev/null || echo '["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]')
            [[ "$answer" != down ]] || exit 1
            echo "$answer"
            ;;
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

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA admin@laptop\n"
UPGRADES = 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
LIMITS = "[Slice]\nCPUQuota=600%\nMemoryMax=20G\n"
SSH_DEV = "PermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nAllowUsers admin ygg dev\n"
DEV_USERS = ["ygg", "ci", "release", "renovate", "dev"]
CLOUDFLARE_V4 = ["173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22"]
CLOUDFLARE_V6 = ["2400:cb00::/32", "2606:4700::/32"]
CHECKOUT = str(HOST.parent.parent)
OVERRIDE = (
    '[Service]\n'
    'Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns"\n'
    'Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns"\n'
)
ROOTFUL = '["name=seccomp,profile=builtin","name=cgroupns"]'

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
        (self.state / "owners").write_text("")
        # The admin has a home with a key in it, as the owner of a machine does: the ssh step refuses
        # to switch passwords off without one.
        self.admin_home = self.temp / "home"
        self.admin_keys = self.admin_home / ".ssh" / "authorized_keys"
        self.admin_keys.parent.mkdir()
        self.admin_keys.write_text(KEY)
        self.homes = self.temp / "homes"
        self.homes.mkdir()
        # Cloudflare answers with its lists.
        self.cloudflare = self.temp / "cloudflare"
        self.cloudflare.mkdir()
        self.serve_cloudflare(CLOUDFLARE_V4, CLOUDFLARE_V6)
        self.passwd = self.sysroot / "etc" / "passwd"
        self.passwd.write_text(f"admin:x:1000:1000::{self.admin_home}:/bin/bash\n")
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.temp / "home"),
            "FAKE_STATE": str(self.state),
            "FAKE_HOMES": str(self.homes),
            "FAKE_SANDBOX": str(self.temp),
            "FAKE_CURL_DIR": str(self.cloudflare),
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

    def users(self):
        """name -> passwd fields, for every user but the admin."""
        entries = [line.split(":") for line in self.passwd.read_text().splitlines()]
        return {fields[0]: fields for fields in entries if fields[0] != "admin"}

    def owners(self):
        """path -> the owner last recorded by the fake chown / install -o."""
        recorded = {}
        for line in (self.state / "owners").read_text().splitlines():
            path, owner = line.split("	")
            recorded[path] = owner
        return recorded

    def rows(self, *arguments, **env):
        result = self.host("check", *arguments, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, [line for line in result.stdout.splitlines() if line.startswith("  ")]

    def row(self, rows, name):
        return next((line for line in rows if line[6:].startswith(name + " ")), None)

    def serve_cloudflare(self, v4, v6):
        (self.cloudflare / "ips-v4").write_text("".join(line + "\n" for line in v4))
        (self.cloudflare / "ips-v6").write_text("".join(line + "\n" for line in v6))

    def ufw_rules(self):
        """(to, from, comment) for every rule the fake ufw holds, the IPv6 twins left out."""
        path = self.state / "ufw-rules"
        lines = path.read_text().splitlines() if path.exists() else []
        rules = [tuple(line.split("\t")) for line in lines]
        return [rule for rule in rules if not rule[0].endswith(" (v6)")]

    def set_ufw(self, rules):
        """The fake ufw holds exactly these rules (and their IPv6 twins), enabled."""
        twins = [(f"{to} (v6)", "Anywhere (v6)", comment) for to, source, comment in rules if source == "Anywhere"]
        (self.state / "ufw-rules").write_text("".join("\t".join(rule) + "\n" for rule in rules + twins))
        (self.state / "ufw-on").write_text("")

    def ufw_enabled(self):
        return (self.state / "ufw-on").exists()

    def cloudflare_rules(self):
        return sorted(source for to, source, comment in self.ufw_rules() if comment == "yggdrasil-cloudflare")

    def ufw_calls(self, verb):
        return [call for call in self.calls() if call.startswith(f"ufw {verb}")]

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
        for name in ("sysctl.d/90-yggdrasil.conf", "usr.bin.rootlesskit", "delegate.conf", "20auto-upgrades",
                     "sshd_config.d/00-yggdrasil.conf", "yggdrasil-role"):
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
            ["bash", str(HOST), "install", "dev"], env=env, capture_output=True, text=True, input="y\nn\n" + "y\n" * 30
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
            ["bash", str(HOST), "install", "apps"], env=env, capture_output=True, text=True, input="n\n" * 30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.etc("yggdrasil-role").exists())
        self.assertEqual([c for c in self.calls() if c.startswith("sudo tee")], [])

    # ---- Service users, ssh, checkout and secrets ----------------------------------------------

    def test_given_role_dev_when_installed_then_the_five_users_exist_without_sudo(self):
        self.install("dev")
        users = self.users()
        self.assertEqual(list(users), DEV_USERS)
        calls = self.calls()
        self.assertIn("useradd --create-home --user-group --shell /bin/bash ygg", calls)
        self.assertIn("useradd --create-home --user-group --shell /usr/sbin/nologin ci", calls)
        for user in DEV_USERS:
            self.assertEqual(users[user][5] and (pathlib.Path(users[user][5]).stat().st_mode & 0o777), 0o750, user)
        # Nobody is added to a group: that is how sudo and docker would be given.
        self.assertFalse([call for call in calls if call.startswith(("usermod", "gpasswd -a", "adduser"))], calls)
        stdout, rows = self.rows("dev")
        for user in DEV_USERS:
            line = self.row(rows, f"user {user}")
            self.assertTrue(line.startswith("  ok "), line)
            self.assertIn("no sudo", line)

    def test_given_role_apps_when_installed_then_only_ygg_is_created(self):
        self.install("apps")
        self.assertEqual(list(self.users()), ["ygg"])
        self.assertEqual(self.users()["ygg"][6], "/bin/bash")
        self.assertEqual(len([call for call in self.calls() if call.startswith("useradd")]), 1)
        self.assertFalse(self.etc("yggdrasil-ci").exists())
        self.assertEqual(list(self.etc("systemd/system").glob("user-*.slice.d")), [])

    def test_given_role_dev_when_installed_then_ci_release_and_renovate_cannot_log_in(self):
        self.install("dev")
        users = self.users()
        for user in ("ci", "release", "renovate"):
            self.assertEqual(users[user][6], "/usr/sbin/nologin", user)
        for user in ("ygg", "dev"):
            self.assertEqual(users[user][6], "/bin/bash", user)

    def test_given_a_user_with_the_wrong_shell_when_installed_then_it_is_put_right_without_a_new_user(self):
        self.install("dev")
        self.passwd.write_text(self.passwd.read_text().replace(
            f"{self.homes / 'ci'}:/usr/sbin/nologin", f"{self.homes / 'ci'}:/bin/bash"))
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "user ci").startswith("  ok "))
        self.install("dev")
        self.assertEqual(self.users()["ci"][6], "/usr/sbin/nologin")
        self.assertEqual(len([call for call in self.calls() if call.startswith("useradd")]), 5)

    def test_given_role_dev_when_installed_then_ci_is_capped_at_six_cpus_and_20g(self):
        self.install("dev")
        uid = self.users()["ci"][2]
        path = self.etc(f"systemd/system/user-{uid}.slice.d/limits.conf")
        self.assertEqual(path.read_text(), LIMITS)
        calls = self.calls()
        self.assertGreater(len(calls) - 1 - calls[::-1].index("systemctl daemon-reload"), calls.index(f"sudo tee {path}"))

    def test_given_an_admin_without_authorized_keys_when_ssh_is_hardened_then_it_is_refused_and_nothing_written(self):
        for keys in ("missing", "empty"):
            with self.subTest(keys=keys):
                if keys == "missing":
                    self.admin_keys.unlink(missing_ok=True)
                else:
                    self.admin_keys.write_text("")
                result = self.host("install", "dev")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("authorized_keys", result.stderr)
                self.assertFalse(self.etc("ssh/sshd_config.d/00-yggdrasil.conf").exists())
                calls = self.calls()
                self.assertFalse([call for call in calls if "sshd" in call or "try-reload-or-restart ssh" in call], calls)
                # Refused up front: no step ran, so nothing was done as root (the checks only read, with
                # `sudo -n`).
                self.assertEqual([call for call in calls if call.startswith("sudo") and not call.startswith("sudo -n")], [])
                self.assertEqual(self.users(), {})
                self.assertFalse(list(self.homes.glob("*/.ssh")))
                stdout, rows = self.rows("dev")
                self.assertIn("refuses", self.row(rows, "ssh"))

    def test_given_an_invalid_sshd_config_when_hardened_then_ssh_is_not_reloaded(self):
        result = self.host("install", "dev", FAKE_SSHD_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sshd -t", result.stderr)
        calls = self.calls()
        self.assertIn("sshd -t", calls)
        self.assertFalse([call for call in calls if "try-reload-or-restart ssh" in call], calls)
        # A configuration sshd rejects is not left to lock everyone out at the next restart.
        self.assertFalse(self.etc("ssh/sshd_config.d/00-yggdrasil.conf").exists())

    def test_given_a_previous_ssh_dropin_when_the_new_one_is_rejected_then_the_previous_one_is_back(self):
        path = self.etc("ssh/sshd_config.d/00-yggdrasil.conf")
        path.parent.mkdir(parents=True)
        path.write_text("PermitRootLogin yes\n")
        result = self.host("install", "dev", FAKE_SSHD_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), "PermitRootLogin yes\n")

    def test_given_role_dev_when_hardened_then_only_the_admin_ygg_and_dev_may_log_in(self):
        self.install("dev")
        path = self.etc("ssh/sshd_config.d/00-yggdrasil.conf")
        self.assertEqual(path.read_text(), SSH_DEV)
        calls = self.calls()
        self.assertLess(calls.index("sshd -t"), calls.index("systemctl try-reload-or-restart ssh"))
        self.assertLess(calls.index(f"sudo tee {path}"), calls.index("sshd -t"))

    def test_given_role_apps_when_hardened_then_only_the_admin_and_ygg_may_log_in(self):
        self.install("apps")
        self.assertIn("AllowUsers admin ygg\n", self.etc("ssh/sshd_config.d/00-yggdrasil.conf").read_text())

    def test_given_role_dev_when_hardened_then_the_admins_key_goes_to_ygg_and_dev_only(self):
        self.install("dev")
        for user in ("ygg", "dev"):
            keys = self.homes / user / ".ssh" / "authorized_keys"
            self.assertEqual(keys.read_text(), KEY, user)
            self.assertEqual(keys.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.owners()[str(keys)], f"{user}:{user}")
        for user in ("ci", "release", "renovate"):
            self.assertFalse((self.homes / user / ".ssh").exists(), user)

    def test_given_a_user_with_its_own_keys_when_hardened_then_they_are_kept(self):
        mine = self.homes / "ygg" / ".ssh" / "authorized_keys"
        mine.parent.mkdir(parents=True)
        mine.write_text("ssh-ed25519 BBBB ygg@elsewhere\n")
        self.passwd.write_text(self.passwd.read_text() + f"ygg:x:1001:1001::{self.homes / 'ygg'}:/bin/bash\n")
        self.install("dev")
        self.assertEqual(mine.read_text(), "ssh-ed25519 BBBB ygg@elsewhere\n")
        # ygg was there already: only the other four are created.
        self.assertEqual(len([call for call in self.calls() if call.startswith("useradd")]), 4)

    def test_given_declined_key_copy_when_hardened_then_the_users_get_no_keys(self):
        env = {k: v for k, v in self.env.items() if k != "YGG_ASSUME_YES"}
        # Yes to the steps, no to every copy of the admin's key (and to the picker's other questions).
        answers = "y\n" * 5 + "n\n" * 2 + "y\n" * 20
        result = subprocess.run(["bash", str(HOST), "install", "dev"], env=env, capture_output=True, text=True, input=answers)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.homes.glob("*/.ssh/authorized_keys")), [])

    def test_given_existing_users_when_installed_twice_then_useradd_is_not_called_again(self):
        self.install("dev")
        self.install("dev")
        self.assertEqual(len([call for call in self.calls() if call.startswith("useradd")]), 5)
        self.assertEqual(len(self.users()), 5)

    def test_given_role_dev_when_installed_then_the_checkout_belongs_to_ygg(self):
        self.install("dev")
        calls = self.calls()
        self.assertIn(f"chown -R ygg:ygg {CHECKOUT}", calls)
        self.assertIn(f"chmod 755 {CHECKOUT}", calls)
        self.assertEqual(self.owners()[CHECKOUT], "ygg:ygg")
        stdout, rows = self.rows("dev")
        self.assertIn("ygg:ygg", self.row(rows, "checkout owner"))

    def test_given_role_dev_when_installed_then_every_secrets_directory_is_closed_to_its_user(self):
        self.install("dev")
        owners = self.owners()
        env = pathlib.Path(__file__).resolve().parent.parent / "env"
        expected = {
            "ygg": {"platform.env": "platform.env.example", "acme.env": "acme.env.example"},
            "ci": {"agent.env": "agent.env.example"},
            "release": {"agent.env": "agent.env.example"},
            "renovate": {},
            "dev": {},
        }
        for user, files in expected.items():
            directory = self.etc("yggdrasil" if user == "ygg" else f"yggdrasil-{user}")
            self.assertEqual(directory.stat().st_mode & 0o777, 0o750, directory)
            self.assertEqual(owners[str(directory)], f"{user}:{user}")
            self.assertEqual(sorted(p.name for p in directory.iterdir()), sorted(files), user)
            for name, template in files.items():
                self.assertEqual((directory / name).read_text(), (env / template).read_text())
                self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
                self.assertEqual(owners[str(directory / name)], f"{user}:{user}")

    def test_given_a_secrets_file_already_there_when_installed_then_it_is_kept(self):
        mine = self.etc("yggdrasil/platform.env")
        mine.parent.mkdir(parents=True)
        mine.write_text("DOMAIN=mine.example.com\n")
        self.install("dev")
        self.assertEqual(mine.read_text(), "DOMAIN=mine.example.com\n")
        self.assertTrue(self.etc("yggdrasil/acme.env").exists())
        self.assertNotIn(str(mine), self.owners())

    def test_given_role_apps_when_installed_then_only_yggs_secrets_are_made(self):
        self.install("apps")
        self.assertEqual(
            sorted(p.name for p in self.etc("yggdrasil").iterdir()), ["acme.env", "cloudflare-ips", "platform.env"])
        self.assertEqual(sorted(p.name for p in self.sysroot.joinpath("etc").glob("yggdrasil-*")), ["yggdrasil-role"])

    def test_given_role_apps_when_installed_then_security_updates_are_automatic(self):
        self.install("apps")
        self.assertEqual(self.etc("apt/apt.conf.d/20auto-upgrades").read_text(), UPGRADES)

    def test_given_role_dev_when_checked_then_a_service_user_in_the_sudo_group_is_reported(self):
        self.install("dev")
        (self.state / "groups-ci").write_text("sudo\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "user ci")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("sudo", line)
        self.assertTrue(self.row(rows, "user release").startswith("  ok "))

    def test_given_role_dev_when_checked_then_a_service_user_in_the_docker_group_is_reported(self):
        self.install("dev")
        (self.state / "groups-renovate").write_text("docker\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "user renovate")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("docker", line)

    def test_given_a_sudo_rule_for_a_service_user_when_checked_then_it_is_reported(self):
        self.install("dev")
        (self.state / "sudo-release").write_text(
            "User release may run the following commands on host:\n    (ALL) NOPASSWD: ALL\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "user release")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("sudo", line)

    def test_given_a_machine_set_up_when_checked_then_every_row_is_ok(self):
        for role in ("dev", "apps"):
            with self.subTest(role=role):
                self.install(role)
                stdout, rows = self.rows(role)
                bad = [line for line in rows if not line.startswith("  ok ")]
                self.assertEqual(bad, [], stdout)
                for name in ("ssh", "checkout owner", "unattended upgrades", "secrets ygg", "user ygg"):
                    self.assertIsNotNone(self.row(rows, name), f"no row {name!r} in:\n{stdout}")
                self.assertEqual(self.row(rows, "ci limits") is not None, role == "dev")
                self.assertIn("== Users ==", stdout)

    def test_given_an_unset_up_machine_when_checked_then_nothing_is_written_or_run_as_root(self):
        result = self.host("check", "dev")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call for call in self.calls() if call.startswith(("sudo tee", "useradd", "chown", "chmod"))], [])
        self.assertEqual(list(self.users()), [])

    # ---- The firewall ----------------------------------------------------------------------------

    def test_given_role_dev_when_the_firewall_is_set_then_ssh_web_and_2222_are_open(self):
        self.install("dev")
        self.assertTrue(self.ufw_enabled())
        self.assertEqual(
            sorted(self.ufw_rules()),
            [("2222/tcp", "Anywhere", ""), ("80,443/tcp", "Anywhere", ""), ("OpenSSH", "Anywhere", "")])
        # SSH is allowed before the firewall is on, so the session running this isn't cut.
        calls = self.calls()
        self.assertLess(calls.index("ufw allow OpenSSH"), calls.index("ufw --force enable"))
        stdout, rows = self.rows("dev")
        self.assertTrue(self.row(rows, "ufw").startswith("  ok "), stdout)
        self.assertTrue(self.row(rows, "ports").startswith("  ok "), stdout)
        self.assertIsNone(self.row(rows, "cloudflare ranges"))

    def test_given_role_apps_when_the_firewall_is_set_then_web_is_open_only_to_cloudflare(self):
        self.install("apps")
        self.assertTrue(self.ufw_enabled())
        self.assertEqual(self.cloudflare_rules(), sorted(CLOUDFLARE_V4 + CLOUDFLARE_V6))
        rules = self.ufw_rules()
        self.assertIn(("OpenSSH", "Anywhere", ""), rules)
        # Every other rule is a Cloudflare range's, for 80 and 443 only; nothing is open to everyone.
        self.assertEqual(len(rules), 1 + len(CLOUDFLARE_V4 + CLOUDFLARE_V6))
        for to, source, comment in rules:
            if comment:
                self.assertEqual(to, "80,443/tcp")
        calls = self.calls()
        self.assertIn(
            "ufw allow proto tcp from 173.245.48.0/20 to any port 80,443 comment yggdrasil-cloudflare", calls)
        self.assertLess(calls.index("ufw allow OpenSSH"), calls.index("ufw --force enable"))
        stdout, rows = self.rows("apps")
        self.assertIn("only from Cloudflare", self.row(rows, "ports"))
        self.assertTrue(self.row(rows, "cloudflare ranges").startswith("  ok "), stdout)

    def test_given_role_apps_when_the_firewall_is_set_then_cloudflare_ips_is_written_for_traefik(self):
        self.install("apps")
        path = self.etc("yggdrasil/cloudflare-ips")
        self.assertEqual(path.read_text(), "".join(line + "\n" for line in CLOUDFLARE_V4 + CLOUDFLARE_V6))
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.owners()[str(path)], "ygg:ygg")
        # The file's directory is ygg's secrets directory: writing the file doesn't open it up.
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o750)

    def test_given_a_firewall_set_when_installed_again_then_no_rule_is_added_twice(self):
        self.install("apps")
        before = (self.state / "ufw-rules").read_text()
        # Made to run again: its file is gone, but every rule is there already.
        self.etc("yggdrasil/cloudflare-ips").unlink()
        self.install("apps")
        self.assertEqual((self.state / "ufw-rules").read_text(), before)
        self.assertEqual(len(self.ufw_calls("allow proto")), len(CLOUDFLARE_V4 + CLOUDFLARE_V6))
        self.assertEqual(len(self.ufw_calls("allow OpenSSH")), 1)
        self.assertEqual(self.ufw_calls("delete"), [])
        self.assertTrue(self.etc("yggdrasil/cloudflare-ips").exists())

    def test_given_a_changed_cloudflare_list_when_set_again_then_old_ranges_are_removed(self):
        self.install("apps")
        self.serve_cloudflare(["173.245.48.0/20", "198.41.128.0/17"], ["2606:4700::/32", "2803:f800::/32"])
        stdout, rows = self.rows("apps")
        self.assertFalse(self.row(rows, "cloudflare ranges").startswith("  ok "), stdout)
        self.install("apps")
        self.assertEqual(
            self.cloudflare_rules(), ["173.245.48.0/20", "198.41.128.0/17", "2606:4700::/32", "2803:f800::/32"])
        self.assertEqual(
            sorted(call.split()[6] for call in self.ufw_calls("delete")),
            ["103.21.244.0/22", "103.22.200.0/22", "2400:cb00::/32"])
        self.assertEqual(
            self.etc("yggdrasil/cloudflare-ips").read_text(),
            "173.245.48.0/20\n198.41.128.0/17\n2606:4700::/32\n2803:f800::/32\n")
        # The ranges that stayed were not added again, and ssh was left alone.
        self.assertEqual(len(self.ufw_calls("allow proto")), len(CLOUDFLARE_V4 + CLOUDFLARE_V6) + 2)
        self.assertIn(("OpenSSH", "Anywhere", ""), self.ufw_rules())
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "cloudflare ranges").startswith("  ok "), stdout)

    UNCHANGED = "could not fetch Cloudflare's ranges: the firewall and cloudflare-ips are unchanged"

    def firewall_calls(self, since=0):
        return [call for call in self.calls()[since:] if call.startswith(("ufw allow", "ufw delete", "ufw --force"))]

    def test_given_cloudflare_unreachable_when_the_firewall_is_set_then_nothing_changes(self):
        (self.cloudflare / "ips-v6").unlink()
        result = self.install("apps")
        self.assertIn(self.UNCHANGED, result.stderr)
        self.assertEqual(self.firewall_calls(), [])
        self.assertFalse(self.ufw_enabled())
        self.assertFalse(self.etc("yggdrasil/cloudflare-ips").exists())
        # The rest of the machine was set up all the same.
        self.assertTrue(self.etc("yggdrasil-role").exists())

    def test_given_cloudflare_unreachable_after_a_firewall_was_set_then_rules_and_file_are_kept(self):
        self.install("apps")
        rules = (self.state / "ufw-rules").read_text()
        ips = self.etc("yggdrasil/cloudflare-ips").read_text()
        # The firewall is off, so install tries to put it right: and the download fails.
        (self.state / "ufw-on").unlink()
        (self.cloudflare / "ips-v4").unlink()
        (self.cloudflare / "ips-v6").unlink()
        before = len(self.calls())
        result = self.install("apps")
        self.assertIn(self.UNCHANGED, result.stderr)
        self.assertEqual((self.state / "ufw-rules").read_text(), rules)
        self.assertEqual(self.etc("yggdrasil/cloudflare-ips").read_text(), ips)
        self.assertFalse(self.ufw_enabled())
        self.assertEqual(self.firewall_calls(before), [])

    def test_given_a_range_that_is_not_a_cidr_when_fetched_then_nothing_changes(self):
        (self.cloudflare / "ips-v4").write_text("<html><body>502 Bad Gateway</body></html>\n")
        result = self.install("apps")
        self.assertIn(self.UNCHANGED, result.stderr)
        self.assertEqual(self.firewall_calls(), [])
        self.assertFalse(self.ufw_enabled())
        self.assertFalse(self.etc("yggdrasil/cloudflare-ips").exists())

    def cloudflare_ranges(self):
        return subprocess.run(
            ["bash", "-c", f'source "{HOST.parent}/machine.sh"; cloudflare_ranges'],
            env=self.env, capture_output=True, text=True,
        )

    def test_given_both_lists_when_ranges_are_read_then_ipv4_comes_first_then_ipv6(self):
        result = self.cloudflare_ranges()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), CLOUDFLARE_V4 + CLOUDFLARE_V6)

    def test_given_junk_in_a_list_when_ranges_are_read_then_they_fail_and_print_nothing(self):
        junk = {
            "an empty ipv4 file": ("", None),
            "only blank lines": ("\n\n", None),
            "an html page": ("<!DOCTYPE html>\n<html></html>\n", None),
            "an ipv4 prefix over 32": ("10.0.0.0/33\n", None),
            "an ipv4 octet over 255": ("256.1.1.1/24\n", None),
            "an ipv4 address without a prefix": ("10.0.0.1\n", None),
            "an ipv4 address with three octets": ("10.0.0/8\n", None),
            "one bad line among good ones": ("173.245.48.0/20\nnot-a-range\n103.21.244.0/22\n", None),
            "an ipv6 range in the ipv4 list": ("2400:cb00::/32\n", None),
            "an ipv4 range in the ipv6 list": (None, "173.245.48.0/20\n"),
            "an ipv6 prefix over 128": (None, "2400:cb00::/129\n"),
            "an ipv6 group over four digits": (None, "2400:cb000::/32\n"),
            "an ipv6 address with two ::": (None, "2400::cb00::/32\n"),
            "an ipv6 address with too few groups": (None, "2400:cb00:1:2/32\n"),
            "an ipv6 error page": (None, "<html>\n"),
            "an empty ipv6 file": (None, ""),
        }
        for name, (v4, v6) in junk.items():
            with self.subTest(name):
                self.serve_cloudflare(CLOUDFLARE_V4, CLOUDFLARE_V6)
                if v4 is not None:
                    (self.cloudflare / "ips-v4").write_text(v4)
                if v6 is not None:
                    (self.cloudflare / "ips-v6").write_text(v6)
                result = self.cloudflare_ranges()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")

    def test_given_valid_but_unusual_ranges_when_read_then_they_are_accepted(self):
        v4 = ["173.245.48.0/20", "0.0.0.0/0", "10.0.0.1/32"]
        v6 = ["2400:cb00::/32", "2c0f:f248::/32", "::/0", "2001:db8:0:0:0:0:0:0/64", "fe80::1/128"]
        self.serve_cloudflare(v4, v6)
        result = self.cloudflare_ranges()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), v4 + v6)

    def test_given_drift_between_rules_and_cloudflare_when_checked_then_it_is_reported(self):
        self.install("apps")
        # Cloudflare adds a range and drops another; the file is stale as well.
        self.serve_cloudflare(CLOUDFLARE_V4[:2] + ["198.41.128.0/17"], CLOUDFLARE_V6)
        stdout, rows = self.rows("apps")
        line = self.row(rows, "cloudflare ranges")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("198.41.128.0/17", line)
        self.assertIn("103.22.200.0/22", line)
        self.assertIn("cloudflare-ips differs", line)
        # A range deleted from ufw by hand is reported too.
        self.serve_cloudflare(CLOUDFLARE_V4, CLOUDFLARE_V6)
        self.set_ufw([rule for rule in self.ufw_rules() if rule[1] != "2606:4700::/32"])
        stdout, rows = self.rows("apps")
        line = self.row(rows, "cloudflare ranges")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("2606:4700::/32", line)

    def test_given_a_missing_cloudflare_ips_file_when_checked_then_it_is_reported(self):
        self.install("apps")
        self.etc("yggdrasil/cloudflare-ips").unlink()
        stdout, rows = self.rows("apps")
        self.assertFalse(self.row(rows, "cloudflare ranges").startswith("  ok "), stdout)

    def test_given_cloudflare_unreachable_when_checked_then_it_says_it_could_not_check(self):
        self.install("apps")
        (self.cloudflare / "ips-v4").unlink()
        stdout, rows = self.rows("apps")
        line = self.row(rows, "cloudflare ranges")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("could not check", line)

    def test_given_an_open_80_rule_on_apps_when_checked_then_it_is_reported(self):
        self.install("apps")
        rules = self.ufw_rules()
        for rule in ("80/tcp", "443", "80,443/tcp", "1:1000/tcp"):
            with self.subTest(rule):
                self.set_ufw(rules + [(rule, "Anywhere", "")])
                stdout, rows = self.rows("apps")
                line = self.row(rows, "ports")
                self.assertFalse(line.startswith("  ok "), line)
                self.assertIn("open to everyone", line)
                self.assertIn(rule, line)
        # 80 for one address, or other ports for everyone, are fine.
        self.set_ufw(rules + [("80/tcp", "10.0.0.5", ""), ("22/udp", "Anywhere", ""), ("2222/tcp", "Anywhere", "")])
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "ports").startswith("  ok "), stdout)

    def test_given_an_open_web_rule_on_apps_when_installed_then_it_is_closed_and_cloudflare_kept(self):
        self.install("apps")
        self.set_ufw(self.ufw_rules() + [("80,443/tcp", "Anywhere", "")])
        self.install("apps")
        self.assertNotIn(("80,443/tcp", "Anywhere", ""), self.ufw_rules())
        self.assertEqual(self.cloudflare_rules(), sorted(CLOUDFLARE_V4 + CLOUDFLARE_V6))
        self.assertIn("ufw delete allow 80,443/tcp", self.calls())
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "ports").startswith("  ok "), stdout)

    def test_given_a_wide_range_with_web_in_it_on_apps_when_installed_then_it_is_left_alone(self):
        self.install("apps")
        self.set_ufw(self.ufw_rules() + [("1:1000/tcp", "Anywhere", "")])
        # Something else makes the firewall step run again.
        self.etc("yggdrasil/cloudflare-ips").unlink()
        self.install("apps")
        self.assertIn(("1:1000/tcp", "Anywhere", ""), self.ufw_rules())
        self.assertEqual(self.ufw_calls("delete"), [])

    def test_given_role_dev_without_2222_when_checked_then_the_ports_row_names_it(self):
        self.install("dev")
        self.set_ufw([rule for rule in self.ufw_rules() if rule[0] != "2222/tcp"])
        stdout, rows = self.rows("dev")
        line = self.row(rows, "ports")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("2222/tcp", line)
        self.install("dev")
        self.assertIn(("2222/tcp", "Anywhere", ""), self.ufw_rules())

    def test_given_a_disabled_ufw_when_checked_then_it_is_reported_and_install_turns_it_on(self):
        self.install("dev")
        (self.state / "ufw-on").unlink()
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "ufw").startswith("  ok "), stdout)
        self.install("dev")
        self.assertTrue(self.ufw_enabled())

    # ---- The ssh hardening has to take effect ----------------------------------------------------

    def cloud_init(self):
        path = self.etc("ssh/sshd_config.d/50-cloud-init.conf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PasswordAuthentication yes\n")
        return path

    def test_given_a_cloud_init_file_setting_passwords_on_when_hardened_then_the_dropin_still_wins(self):
        cloud = self.cloud_init()
        self.install("dev")
        # 00- sorts before 50-: sshd takes the first value it finds.
        self.assertEqual(self.etc("ssh/sshd_config.d/00-yggdrasil.conf").read_text(), SSH_DEV)
        self.assertEqual(cloud.read_text(), "PasswordAuthentication yes\n")
        calls = self.calls()
        self.assertIn("sshd -T", calls)
        self.assertLess(calls.index("sshd -T"), calls.index("systemctl try-reload-or-restart ssh"))
        stdout, rows = self.rows("dev")
        self.assertTrue(self.row(rows, "ssh").startswith("  ok "), stdout)
        self.assertIn("sshd -T", self.calls())

    def test_given_another_file_setting_a_value_first_when_hardened_then_it_is_rolled_back_and_refused(self):
        first = self.etc("ssh/sshd_config.d/00-aaa-first.conf")
        first.parent.mkdir(parents=True)
        first.write_text("PasswordAuthentication yes\n")
        result = self.host("install", "dev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("passwordauthentication is yes", result.stderr)
        self.assertFalse(self.etc("ssh/sshd_config.d/00-yggdrasil.conf").exists())
        self.assertFalse([call for call in self.calls() if "try-reload-or-restart ssh" in call])

    def test_given_another_file_setting_allowusers_first_when_hardened_then_it_is_refused(self):
        first = self.etc("ssh/sshd_config.d/00-aaa-first.conf")
        first.parent.mkdir(parents=True)
        first.write_text("AllowUsers admin ygg dev mallory\n")
        result = self.host("install", "dev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allowusers", result.stderr)
        self.assertFalse(self.etc("ssh/sshd_config.d/00-yggdrasil.conf").exists())

    def test_given_a_dropin_that_is_overridden_when_checked_then_the_ssh_row_names_the_setting(self):
        self.install("dev")
        override = self.etc("ssh/sshd_config.d/00-aaa-first.conf")
        override.write_text("PasswordAuthentication yes\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "ssh")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("passwordauthentication is yes", line)

    def test_given_the_dropin_of_an_earlier_install_when_hardened_then_it_is_removed(self):
        legacy = self.etc("ssh/sshd_config.d/90-yggdrasil.conf")
        legacy.parent.mkdir(parents=True)
        legacy.write_text(SSH_DEV)
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "ssh").startswith("  ok "))
        self.install("dev")
        self.assertFalse(legacy.exists())
        self.assertTrue(self.etc("ssh/sshd_config.d/00-yggdrasil.conf").exists())

    def test_given_a_fresh_host_when_hardened_then_sshds_runtime_directory_is_made_and_ssh_restarted_if_needed(self):
        self.assertFalse((self.sysroot / "run" / "sshd").exists())
        self.install("apps")
        self.assertTrue((self.sysroot / "run" / "sshd").is_dir())
        self.assertIn(f"sudo install -d -m 755 {self.sysroot}/run/sshd", self.calls())
        self.assertIn("systemctl try-reload-or-restart ssh", self.calls())

    def test_given_no_admin_key_but_ssh_already_hardened_when_installed_then_the_rest_still_runs(self):
        self.install("apps")
        self.admin_keys.unlink()
        self.etc("yggdrasil-role").unlink()
        self.install("apps")
        self.assertTrue(self.etc("yggdrasil-role").exists())

    def test_given_an_admin_named_like_a_role_user_when_installed_then_it_is_left_alone(self):
        self.passwd.write_text(f"dev:x:1000:1000::{self.admin_home}:/bin/bash\n")
        self.install("dev", FAKE_ADMIN="dev")
        calls = self.calls()
        self.assertEqual(
            [call.split()[-1] for call in calls if call.startswith("useradd")], ["ygg", "ci", "release", "renovate"])
        self.assertFalse([call for call in calls if call.startswith(("usermod", "sudo chmod 750")) and "dev" in call.split()[-1:]], calls)
        self.assertNotIn(f"chmod 750 {self.admin_home}", calls)
        self.assertEqual(
            self.etc("ssh/sshd_config.d/00-yggdrasil.conf").read_text().splitlines()[-1], "AllowUsers dev ygg")
        stdout, rows = self.rows("dev", FAKE_ADMIN="dev")
        self.assertIn("admin", self.row(rows, "user dev"))
        self.assertEqual([line for line in rows if not line.startswith("  ok ")], [], stdout)

    # ---- Rootless engines ------------------------------------------------------------------------

    def uid_of(self, user):
        return self.users()[user][2]

    def home_of(self, user):
        return pathlib.Path(self.users()[user][5])

    def linger_file(self, user):
        return self.sysroot / "var" / "lib" / "systemd" / "linger" / user

    def as_user(self, user, *command):
        """The sudo call that runs the command as the user, the way run_as_user words it."""
        uid = self.uid_of(user)
        return (f"sudo -u {user} env XDG_RUNTIME_DIR=/run/user/{uid} "
                f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus {' '.join(command)}")

    def override_of(self, user):
        return self.home_of(user) / ".config" / "systemd" / "user" / "docker.service.d" / "override.conf"

    def profile_block(self, user):
        return (f"# >>> yggdrasil >>>\nexport DOCKER_HOST=unix:///run/user/{self.uid_of(user)}/docker.sock\n"
                "# <<< yggdrasil <<<\n")

    def engine_calls(self):
        return [c for c in self.calls() if "dockerd-rootless-setuptool.sh" in c or "enable-linger" in c]

    def test_given_role_dev_when_engines_are_installed_then_each_of_the_five_users_gets_one(self):
        self.install("dev")
        calls = self.calls()
        for user in DEV_USERS:
            with self.subTest(user=user):
                self.assertIn(f"loginctl enable-linger {user}", calls)
                self.assertEqual(self.override_of(user).read_text(), OVERRIDE)
                self.assertEqual(calls.count(f"dockerd-rootless-setuptool.sh install [as {user}]"), 1)
                self.assertIn(self.as_user(user, "systemctl --user daemon-reload"), calls)
                self.assertIn(self.as_user(user, "systemctl --user enable --now docker"), calls)
                self.assertTrue((self.home_of(user) / ".profile").read_text().endswith(self.profile_block(user)))
        # The override is in place, and the daemon reloaded, before the engine is started.
        reload_call = calls.index(self.as_user("ygg", "systemctl --user daemon-reload"))
        self.assertLess(calls.index("dockerd-rootless-setuptool.sh install [as ygg]"), reload_call)
        self.assertLess(reload_call, calls.index(self.as_user("ygg", "systemctl --user enable --now docker")))
        stdout, rows = self.rows("dev")
        self.assertIn("== Rootless Docker ==", stdout)
        for user in DEV_USERS:
            for name in ("engine", "linger", "client addresses"):
                line = self.row(rows, f"{name} {user}")
                self.assertIsNotNone(line, f"no row {name} {user} in:\n{stdout}")
                self.assertTrue(line.startswith("  ok "), line)
        for name in ("ci → ygg engine", "release → ygg engine", "ci → ygg secrets"):
            self.assertTrue(self.row(rows, name).startswith("  ok "), stdout)

    def test_given_role_apps_when_engines_are_installed_then_only_ygg_gets_one(self):
        self.install("apps")
        self.assertEqual([c for c in self.calls() if c.startswith("dockerd-rootless-setuptool.sh")],
                         ["dockerd-rootless-setuptool.sh install [as ygg]"])
        stdout, rows = self.rows("apps")
        self.assertIsNone(self.row(rows, "engine ci"))
        self.assertFalse([line for line in rows if "→" in line], stdout)

    def test_given_a_nologin_user_when_its_engine_is_installed_then_the_commands_run_as_that_user(self):
        self.install("dev")
        self.assertEqual(self.users()["ci"][6], "/usr/sbin/nologin")
        calls = self.calls()
        uid = self.uid_of("ci")
        env = f"XDG_RUNTIME_DIR=/run/user/{uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"
        setup = f"sudo -u ci env {env} dockerd-rootless-setuptool.sh install"
        self.assertIn(setup, calls)
        # From /, not from the admin's closed home: ci can't enter that, and it has no shell to ask for.
        self.assertEqual(calls[calls.index(setup) + 1], "as ci in /")
        self.assertFalse([c for c in calls if c.startswith(("sudo -s", "sudo -i", "su "))], calls)
        # Every call made as ci has its runtime directory and bus.
        as_ci = [c for c in calls if c.startswith(("sudo -u ci ", "sudo -n -u ci "))]
        self.assertTrue(as_ci)
        for call in as_ci:
            self.assertIn(env, call)

    def test_given_an_engine_when_installed_then_it_keeps_client_addresses(self):
        self.install("dev")
        for user in DEV_USERS:
            self.assertEqual(self.override_of(user).read_text(), OVERRIDE, user)
            # Written by the user itself: the file is its own, in a home closed to everyone else.
            self.assertIn(self.as_user(user, "tee", str(self.override_of(user))), self.calls())
        self.override_of("ci").write_text(
            '[Service]\nEnvironment="DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns"\n')
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "client addresses ci").startswith("  ok "), stdout)
        self.assertTrue(self.row(rows, "client addresses ygg").startswith("  ok "), stdout)
        self.install("dev")
        self.assertEqual(self.override_of("ci").read_text(), OVERRIDE)
        # A running engine only takes the new addresses when it restarts.
        self.assertIn(self.as_user("ci", "systemctl --user restart docker"), self.calls())

    def test_given_an_engine_installed_twice_then_the_profile_has_one_docker_host_line(self):
        self.install("dev")
        profile = self.home_of("ygg") / ".profile"
        profile.write_text("alias ll='ls -l'\n" + profile.read_text())
        # Without linger the engine step runs again.
        self.linger_file("ygg").unlink()
        self.install("dev")
        text = profile.read_text()
        self.assertEqual(text.count("export DOCKER_HOST="), 1, text)
        self.assertEqual(text.count("# >>> yggdrasil >>>"), 1, text)
        self.assertEqual(text.count("# <<< yggdrasil <<<"), 1, text)
        self.assertIn("alias ll='ls -l'", text)
        self.assertTrue(text.endswith(self.profile_block("ygg")), text)

    def test_given_a_stale_block_in_the_profile_when_installed_then_it_is_replaced(self):
        self.install("apps")
        profile = self.home_of("ygg") / ".profile"
        profile.write_text("export PATH=$PATH:/x\n\n# >>> yggdrasil >>>\n"
                           "export DOCKER_HOST=unix:///run/user/7/docker.sock\n# <<< yggdrasil <<<\n")
        self.linger_file("ygg").unlink()
        self.install("apps")
        text = profile.read_text()
        self.assertEqual(text.count("DOCKER_HOST"), 1, text)
        self.assertIn(f"unix:///run/user/{self.uid_of('ygg')}/docker.sock", text)
        self.assertIn("export PATH=$PATH:/x", text)

    def test_given_engines_up_when_installed_again_then_no_engine_command_runs(self):
        self.install("dev")
        before = self.engine_calls()
        self.assertTrue(before)
        self.install("dev")
        self.assertEqual(self.engine_calls(), before)

    def test_given_ci_able_to_read_the_ygg_socket_when_checked_then_isolation_is_reported_broken(self):
        self.install("dev")
        socket = f"/run/user/{self.uid_of('ygg')}/docker.sock"
        (self.state / "readable-ci").write_text(socket + "\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "ci → ygg engine")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn(socket, line)
        self.assertTrue(self.row(rows, "release → ygg engine").startswith("  ok "), stdout)
        # Asked as ci, without ever waiting for a password.
        self.assertIn(f"sudo -n -u ci env XDG_RUNTIME_DIR=/run/user/{self.uid_of('ci')}", " ".join(self.calls()))

    def test_given_release_able_to_read_the_ygg_socket_when_checked_then_isolation_is_reported_broken(self):
        self.install("dev")
        (self.state / "readable-release").write_text(f"/run/user/{self.uid_of('ygg')}/docker.sock\n")
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "release → ygg engine").startswith("  ok "), stdout)
        self.assertTrue(self.row(rows, "ci → ygg engine").startswith("  ok "), stdout)

    def test_given_ci_able_to_read_the_ygg_secrets_when_checked_then_isolation_is_reported_broken(self):
        self.install("dev")
        (self.state / "readable-ci").write_text(f"{self.etc('yggdrasil')}/platform.env\n")
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "ci → ygg secrets").startswith("  ok "), stdout)
        self.assertTrue(self.row(rows, "ci → ygg engine").startswith("  ok "), stdout)

    def test_given_a_rootful_engine_answering_for_a_user_when_checked_then_its_engine_row_is_not_ok(self):
        self.install("dev")
        (self.state / "security-ci").write_text(ROOTFUL + "\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "engine ci")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("rootless", line)
        self.assertTrue(self.row(rows, "engine ygg").startswith("  ok "), stdout)
        # The check asked that user's own engine, not whichever one the CLI would pick.
        uid = self.uid_of("ci")
        self.assertIn(
            f"docker info --format {{{{json .SecurityOptions}}}} [as ci, DOCKER_HOST=unix:///run/user/{uid}/docker.sock]",
            self.calls())

    def test_given_an_engine_that_does_not_answer_when_checked_then_install_sets_it_up_again(self):
        self.install("apps")
        (self.state / "security-ygg").write_text("down\n")
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "engine ygg").startswith("  -- "), stdout)
        before = self.calls().count("dockerd-rootless-setuptool.sh install [as ygg]")
        self.install("apps")
        self.assertEqual(self.calls().count("dockerd-rootless-setuptool.sh install [as ygg]"), before + 1)

    def test_given_no_linger_when_checked_then_the_row_is_missing_and_install_enables_it(self):
        self.install("apps")
        self.linger_file("ygg").unlink()
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "linger ygg").startswith("  -- "), stdout)
        self.install("apps")
        self.assertTrue(self.linger_file("ygg").exists())

    def test_given_no_users_yet_when_checked_then_each_engine_is_missing_and_nothing_runs_as_a_user(self):
        stdout, rows = self.rows("dev")
        for user in DEV_USERS:
            self.assertTrue(self.row(rows, f"engine {user}").startswith("  -- "), stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("sudo -u", "sudo -n -u"))], [])

    def test_given_sudo_asking_for_a_password_when_checked_then_the_user_rows_are_not_checked(self):
        self.install("dev")
        (self.state / "sudo-denied").write_text("")
        calls_before = len(self.calls())
        stdout, rows = self.rows("dev")
        for name in ("engine ci", "client addresses ci", "ci → ygg engine", "ci → ygg secrets"):
            line = self.row(rows, name)
            self.assertIn("not checked", line)
            self.assertFalse(line.startswith("  -- "), line)
        # Linger is a file the admin can read: still checked.
        self.assertTrue(self.row(rows, "linger ci").startswith("  ok "), stdout)
        # A check never asks sudo for a password: every call as a user has -n.
        self.assertEqual([c for c in self.calls()[calls_before:] if c.startswith("sudo -u")], [])


if __name__ == "__main__":
    unittest.main()
