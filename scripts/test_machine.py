"""Tests for the machine roles of scripts/host.sh (scripts/machine.sh): packages, rootful Docker off,
the system settings, the service users, ssh, the checkout, the secrets, the firewall, the rootless
engines, the backup timer and the role file.

    python3 -m unittest discover -s scripts -p "test_machine.py"

Everything that needs root or changes the system is a fake on PATH: `sudo` (logs, then runs the
command, which only ever touches the temporary $YGG_SYSROOT), `apt-get`, `dpkg-query`, `dpkg`, `curl`,
`systemctl`, `sysctl`, `apparmor_parser`, `gpasswd`, `getent`, `id`, `docker`, `useradd`, `usermod`,
`chown`, `chmod`, `install`, `stat`, `sshd`, `ufw`, `loginctl`, `dockerd-rootless-setuptool.sh`, `test` and `cat`. The fakes keep their state in $FAKE_STATE
(packages installed, active units, the docker group, who owns what, ufw's rules) and in a fake $YGG_SYSROOT/etc/passwd, so a
second run sees what the first one did, and log every call to $FAKE_STATE/calls. None of them changes
an owner: `chown` only records it (the checkout step chowns the real checkout, which must stay as it
is), and `chmod` only touches what is under the test's temporary directory.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

HOST = pathlib.Path(__file__).resolve().parent / "host.sh"

# Each fake is a bash script. State lives in plain files under $FAKE_STATE.
FAKES = {
    # `sudo -l -U <user>` lists $FAKE_STATE/sudo-<user> when there is one, as sudo would a rule, and
    # otherwise says the user may not run sudo. `sudo -n` fails while $FAKE_STATE/sudo-denied exists
    # (a password would be asked for). `sudo -u <user> <command>` writes down who it runs as and from
    # where, and tells the fakes below (FAKE_AS_USER) so they can answer for that user; it fails for
    # a user with $FAKE_STATE/sudo-u-denied-<user> (sudo not allowed to run as them). `sudo -v` (asking for
    # the password up front) succeeds unless $FAKE_STATE/sudo-v-fails exists (a wrong password, no tty).
    "sudo": r"""
        echo "sudo $*" >>"$FAKE_STATE/calls"
        if [[ "$1" == -v ]]; then
          [[ ! -e "$FAKE_STATE/sudo-v-fails" ]]
          exit $?
        fi
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
          [[ ! -e "$FAKE_STATE/sudo-u-denied-$2" ]] || exit 1
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
          # A user's engine is running from `enable`/`restart` of docker on, until
          # $FAKE_STATE/user-active-<user> is removed. Any unit `enable`d is listed in
          # $FAKE_STATE/user-enabled-<user>, which is what `is-enabled` reads.
          --user)
            case "$2" in
              is-active) [[ -e "$FAKE_STATE/user-active-${FAKE_AS_USER:-}" ]]; exit $? ;;
              is-enabled) grep -qxF -- "$3" "$FAKE_STATE/user-enabled-${FAKE_AS_USER:-}" 2>/dev/null; exit $? ;;
              # The properties of the user's units, as `show -p` prints them: what
              # $FAKE_STATE/user-show-<user> says, else those of a unit that never ran.
              show) cat "$FAKE_STATE/user-show-${FAKE_AS_USER:-}" 2>/dev/null \
                      || printf '%s\n' Result=success ExecMainStartTimestamp= ;;
              enable | restart | start)
                [[ "${@: -1}" != docker ]] || touch "$FAKE_STATE/user-active-${FAKE_AS_USER:-}"
                [[ "$2" != enable ]] || echo "${@: -1}" >>"$FAKE_STATE/user-enabled-${FAKE_AS_USER:-}"
                ;;
            esac
            ;;
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
    # rule says (not by its comment). The default for incoming is $FAKE_STATE/ufw-default-incoming (deny,
    # as on a fresh Ubuntu, when there is none), shown by `status verbose` and set by `default`. Like real
    # ufw, `status verbose` shows an application profile's rule by its ports with the profile's name after
    # them ("22/tcp (OpenSSH)", its twin "22/tcp (OpenSSH (v6))"), where plain `status` shows the name.
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
            echo "Status: active"
            action=ALLOW
            if [[ "${2:-}" == verbose ]]; then
              action="ALLOW IN"
              printf 'Logging: on (low)\nDefault: %s (incoming), %s (outgoing), disabled (routed)\nNew profiles: skip\n' \
                "$(cat "$FAKE_STATE/ufw-default-incoming" 2>/dev/null || echo deny)" \
                "$(cat "$FAKE_STATE/ufw-default-outgoing" 2>/dev/null || echo allow)"
            fi
            printf '\nTo                         Action      From\n--                         ------      ----\n'
            while IFS=$'\t' read -r to from comment; do
              if [[ "${2:-}" == verbose ]]; then
                app=${to% (v6)} v6=""
                [[ "$app" == "$to" ]] || v6=" (v6)"
                case $app in
                  OpenSSH) to="22/tcp ($app$v6)" ;;
                  "Nginx Full" | "WWW Full") to="80,443/tcp ($app$v6)" ;;
                esac
              fi
              printf '%-26s %-11s %-26s' "$to" "$action" "$from"
              [[ -z "$comment" ]] || printf ' # %s' "$comment"
              echo
            done <"$rules"
            ;;
          --force)
            [[ "$2" == enable ]] && touch "$FAKE_STATE/ufw-on"
            ;;
          default)
            echo "$2" >"$FAKE_STATE/ufw-default-$3"
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
    # A file listed in $FAKE_STATE/unreadable can't be read.
    "cat": r"""
        for argument in "$@"; do
          if grep -qxF -- "$argument" "$FAKE_STATE/unreadable" 2>/dev/null; then
            echo "cat: $argument: Permission denied" >&2
            exit 1
          fi
        done
        exec /usr/bin/cat "$@"
    """,
    "dockerd-rootless-setuptool.sh": r"""
        echo "dockerd-rootless-setuptool.sh $* [as ${FAKE_AS_USER:-nobody}]" >>"$FAKE_STATE/calls"
        echo "$XDG_RUNTIME_DIR/docker.sock" >>"$FAKE_STATE/exists"
    """,
    # Run as a service user, `test -r <path>` is true for the paths listed in
    # $FAKE_STATE/readable-<user> and false for all others; for anyone else it is the real one, except
    # that a path of /run/user (a real directory of the host) exists when $FAKE_STATE/exists says so.
    "test": r"""
        if [[ "$1" == -e && "$2" == /run/user/* ]]; then
          grep -qxF -- "$2" "$FAKE_STATE/exists" 2>/dev/null
          exit $?
        fi
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
LOW_PORTS = "[Slice]\nSocketBindDeny=tcp:1-1023\nSocketBindDeny=udp:1-1023\n"
# The users that run code the owner didn't write, or didn't review yet: none may take 80 or 443.
LOW_PORT_USERS = ["ci", "release", "renovate", "dev"]
# Every (row, user, path) of the dev machine's isolation, for a ygg uid, a release uid and the sysroot.
ISOLATION = [
    ("ci \u2192 ygg engine", "ci", "ygg-socket"), ("release \u2192 ygg engine", "release", "ygg-socket"),
    ("dev \u2192 ygg engine", "dev", "ygg-socket"), ("renovate \u2192 ygg engine", "renovate", "ygg-socket"),
    ("ci \u2192 release engine", "ci", "release-socket"),
    ("ci \u2192 ygg secrets", "ci", "ygg-secrets"), ("release \u2192 ygg secrets", "release", "ygg-secrets"),
    ("dev \u2192 ygg secrets", "dev", "ygg-secrets"), ("renovate \u2192 ygg secrets", "renovate", "ygg-secrets"),
    ("ci \u2192 release secrets", "ci", "release-secrets"),
]
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
UBUNTU_26 = ('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\nUBUNTU_CODENAME=resolute\n'
             'PRETTY_NAME="Ubuntu 26.04.1 LTS"\n')
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
        # Install refuses a checkout that isn't /opt/yggdrasil: in the tests that is $YGG_SYSROOT/opt/yggdrasil,
        # which is this checkout.
        self.opt_checkout = self.sysroot / "opt" / "yggdrasil"
        self.opt_checkout.parent.mkdir()
        self.opt_checkout.symlink_to(CHECKOUT)
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

    # ---- Before any step: sudo, the checkout and the role file ----------------------------------

    def test_given_role_dev_when_installed_then_sudo_is_asked_for_before_anything_else(self):
        self.install("dev")
        sudo_calls = [call for call in self.calls() if call.startswith("sudo ")]
        # Asked once, up front: without a ticket the check rows that need root say "not checked" and
        # queue nothing, and install would trust them.
        self.assertEqual(sudo_calls[0], "sudo -v")
        self.assertEqual(sudo_calls.count("sudo -v"), 1)

    def test_given_sudo_refusing_the_admin_when_installing_then_it_stops_with_nothing_changed(self):
        (self.state / "sudo-v-fails").write_text("")
        result = self.host("install", "apps")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo", result.stderr)
        self.assertEqual(self.calls(), ["sudo -v"])
        self.assertFalse(self.etc("yggdrasil-role").exists())

    def test_given_role_dev_when_checked_then_sudo_is_asked_for_once_up_front(self):
        self.rows("dev")
        self.assertEqual(self.calls()[0], "sudo -v")
        self.assertEqual(self.calls().count("sudo -v"), 1)

    def test_given_sudo_refusing_the_admin_when_checked_then_it_says_so_once_and_reports(self):
        self.install("dev")
        (self.state / "sudo-v-fails").write_text("")
        (self.state / "sudo-denied").write_text("")
        result = self.host("check", "dev")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("sudo -v failed"), 1, result.stderr)
        self.assertIn("not checked", result.stdout)
        self.assertIn("== Rootless Docker ==", result.stdout)

    def test_given_role_workstation_when_checked_then_sudo_is_not_asked_up_front(self):
        self.host("check")
        self.host("check", "workstation")
        self.assertNotIn("sudo -v", self.calls())

    def test_given_a_checkout_that_is_not_opt_yggdrasil_when_installing_then_it_is_refused(self):
        elsewhere = self.temp / "elsewhere"
        elsewhere.mkdir()
        for target in ("missing", "another directory"):
            with self.subTest(target):
                self.opt_checkout.unlink(missing_ok=True)
                if target == "another directory":
                    self.opt_checkout.symlink_to(elsewhere)
                (self.state / "calls").write_text("")
                result = self.host("install", "dev")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("/opt/yggdrasil", result.stderr)
                self.assertIn("clone", result.stderr)
                # Nothing is asked or changed: the checkout step would chown whatever checkout this is.
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.etc("yggdrasil-role").exists())

    def test_given_a_checkout_that_is_not_opt_yggdrasil_when_checked_then_its_row_says_so(self):
        self.opt_checkout.unlink()
        stdout, rows = self.rows("dev")
        line = self.row(rows, "checkout owner")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("/opt/yggdrasil", line)

    def test_given_a_step_that_fails_when_installing_then_the_role_file_is_already_written(self):
        result = self.host("install", "apps", FAKE_SSHD_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sshd -t", result.stderr)
        # A bare `ygg install` afterwards must not fall back to workstation (rootful Docker, 80/443 open).
        self.assertEqual(self.etc("yggdrasil-role").read_text().split(), ["apps"])
        calls = self.calls()
        self.assertLess(calls.index(f"sudo tee {self.etc('yggdrasil-role')}"), calls.index("sshd -t"))

    def test_given_debian_when_installing_role_dev_then_it_is_refused(self):
        self.etc("os-release").write_text(DEBIAN)
        result = self.host("install", "dev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ubuntu 24.04 or 26.04 LTS only", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_given_ubuntu_22_when_installing_role_apps_then_it_is_refused(self):
        self.etc("os-release").write_text(UBUNTU.replace("24.04", "22.04"))
        result = self.host("install", "apps")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ubuntu 24.04 or 26.04 LTS only", result.stderr)

    def test_given_ubuntu_26_when_installing_role_apps_then_the_release_is_accepted(self):
        self.etc("os-release").write_text(UBUNTU_26)
        # sshd -t failing stops the install after the role file is written, well past the preflight.
        result = self.host("install", "apps", FAKE_SSHD_STATUS="1")
        self.assertNotIn("installs on", result.stderr)
        self.assertEqual(self.etc("yggdrasil-role").read_text().split(), ["apps"])

    def test_given_ubuntu_26_when_checked_then_the_release_is_not_warned_about(self):
        self.etc("os-release").write_text(UBUNTU_26)
        result = self.host("check", "apps")
        self.assertNotIn("install knows", result.stdout)

    def test_given_ubuntu_22_when_checked_then_the_release_is_warned_about(self):
        self.etc("os-release").write_text(UBUNTU.replace("24.04", "22.04"))
        result = self.host("check", "apps")
        self.assertIn("install knows Ubuntu 24.04 or 26.04 LTS only", result.stdout)

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

    def low_ports_file(self, user):
        return self.etc(f"systemd/system/user-{self.users()[user][2]}.slice.d/50-yggdrasil-ports.conf")

    def test_given_role_dev_when_installed_then_no_user_running_others_code_may_bind_a_low_port(self):
        self.install("dev")
        for user in LOW_PORT_USERS:
            with self.subTest(user=user):
                path = self.low_ports_file(user)
                self.assertEqual(path.read_text(), LOW_PORTS)
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        # ygg is the one that publishes 80 and 443 (Traefik): its slice is left alone.
        self.assertFalse(self.low_ports_file("ygg").exists())
        calls = self.calls()
        last = self.low_ports_file(LOW_PORT_USERS[-1])
        self.assertGreater(len(calls) - 1 - calls[::-1].index("systemctl daemon-reload"), calls.index(f"sudo tee {last}"))
        stdout, rows = self.rows("dev")
        for user in LOW_PORT_USERS:
            self.assertTrue(self.row(rows, f"low ports {user}").startswith("  ok "), stdout)
        self.assertIsNone(self.row(rows, "low ports ygg"))

    def test_given_a_changed_low_ports_file_when_checked_then_it_is_not_ok_and_install_rewrites_it(self):
        self.install("dev")
        self.low_ports_file("ci").write_text("[Slice]\n")
        stdout, rows = self.rows("dev")
        self.assertFalse(self.row(rows, "low ports ci").startswith("  ok "), stdout)
        self.assertTrue(self.row(rows, "low ports dev").startswith("  ok "), stdout)
        self.install("dev")
        self.assertEqual(self.low_ports_file("ci").read_text(), LOW_PORTS)

    def test_given_no_users_yet_when_checked_then_the_low_ports_rows_are_missing(self):
        stdout, rows = self.rows("dev")
        for user in LOW_PORT_USERS:
            self.assertTrue(self.row(rows, f"low ports {user}").startswith("  -- "), stdout)

    def test_given_role_apps_when_installed_then_no_low_ports_file_is_written(self):
        self.install("apps")
        self.assertEqual(list(self.sysroot.glob("etc/systemd/system/user-*.slice.d/50-yggdrasil-ports.conf")), [])
        stdout, rows = self.rows("apps")
        self.assertFalse([line for line in rows if "low ports" in line], stdout)

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
                # Refused up front: no step ran, so nothing was done as root (sudo -v only asks for the
                # password, and the checks only read, with `sudo -n`).
                self.assertEqual(
                    [call for call in calls if call.startswith("sudo") and not call.startswith(("sudo -n", "sudo -v"))], [])
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
            "renovate": {"renovate.env": "renovate.env.example"},
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
                # A fresh firewall: the dev machine's 2222 is, rightly, not the apps machine's.
                (self.state / "ufw-rules").unlink(missing_ok=True)
                (self.state / "ufw-on").unlink(missing_ok=True)
                self.install(role)
                self.backups_working()
                if role == "dev":
                    self.renovate_working()
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
        # SSH is allowed before the firewall is on, so the session running this isn't cut, and what
        # nothing allows is refused.
        calls = self.calls()
        self.assertLess(calls.index("ufw allow OpenSSH"), calls.index("ufw --force enable"))
        self.assertLess(calls.index("ufw default deny incoming"), calls.index("ufw --force enable"))
        self.assertIn("ufw default allow outgoing", calls)
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

    def assert_openssh_seen_in(self, role):
        # Real ufw: `status verbose` says "22/tcp (OpenSSH)" and "22/tcp (OpenSSH (v6))" for the rule
        # `ufw allow OpenSSH` made; the rules are read from plain `status`, which says "OpenSSH".
        self.install(role)
        stdout, rows = self.rows(role)
        self.assertTrue(self.row(rows, "ports").startswith("  ok "), stdout)
        self.assertNotIn("OpenSSH", self.row(rows, "ports"))
        self.assertIn("ufw status", self.calls())
        self.assertIn("ufw status verbose", self.calls())

    def test_given_ufw_showing_openssh_by_its_ports_in_verbose_when_dev_is_checked_then_ssh_is_allowed(self):
        self.assert_openssh_seen_in("dev")

    def test_given_ufw_showing_openssh_by_its_ports_in_verbose_when_apps_is_checked_then_ssh_is_allowed(self):
        self.assert_openssh_seen_in("apps")

    def test_given_role_apps_when_the_firewall_is_set_then_cloudflare_ips_is_written_for_traefik(self):
        result = self.install("apps")
        # Traefik reads the file when platform.sh renders its configuration, not before.
        self.assertIn("after the next 'scripts/platform.sh up' as ygg", result.stdout)
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

    UNFETCHED = "could not fetch Cloudflare's ranges: ufw is on with ssh allowed and incoming denied"

    def firewall_calls(self, since=0):
        return [call for call in self.calls()[since:] if call.startswith(("ufw allow", "ufw delete", "ufw --force"))]

    def assert_closed_without_cloudflare(self, result):
        """The firewall fails closed: ssh in, everything else refused, no Cloudflare rule or file made."""
        self.assertIn(self.UNFETCHED, result.stderr)
        self.assertTrue(self.ufw_enabled())
        self.assertIn(("OpenSSH", "Anywhere", ""), self.ufw_rules())
        self.assertEqual((self.state / "ufw-default-incoming").read_text().strip(), "deny")
        calls = self.calls()
        self.assertLess(calls.index("ufw allow OpenSSH"), calls.index("ufw --force enable"))
        self.assertLess(calls.index("ufw default deny incoming"), calls.index("ufw --force enable"))

    def test_given_cloudflare_unreachable_when_the_firewall_is_set_then_it_is_on_and_web_is_closed(self):
        (self.cloudflare / "ips-v6").unlink()
        result = self.install("apps")
        self.assert_closed_without_cloudflare(result)
        self.assertEqual(self.cloudflare_rules(), [])
        self.assertEqual(self.ufw_rules(), [("OpenSSH", "Anywhere", "")])
        self.assertFalse(self.etc("yggdrasil/cloudflare-ips").exists())
        # The rest of the machine was set up all the same.
        self.assertTrue(self.etc("yggdrasil-role").exists())

    def test_given_cloudflare_unreachable_after_a_firewall_was_set_then_its_rules_and_file_are_kept(self):
        self.install("apps")
        ips = self.etc("yggdrasil/cloudflare-ips").read_text()
        # The firewall is off (and lets everything in by default), so install puts it right: and the
        # download fails.
        (self.state / "ufw-on").unlink()
        (self.state / "ufw-default-incoming").write_text("allow\n")
        (self.cloudflare / "ips-v4").unlink()
        (self.cloudflare / "ips-v6").unlink()
        before = len(self.calls())
        result = self.install("apps")
        self.assert_closed_without_cloudflare(result)
        # The Cloudflare rules that were there stay: the ones of the last download that worked.
        self.assertEqual(self.cloudflare_rules(), sorted(CLOUDFLARE_V4 + CLOUDFLARE_V6))
        self.assertEqual(self.etc("yggdrasil/cloudflare-ips").read_text(), ips)
        self.assertEqual([call for call in self.firewall_calls(before) if "proto" in call], [])

    def test_given_a_range_that_is_not_a_cidr_when_fetched_then_no_cloudflare_rule_is_made(self):
        (self.cloudflare / "ips-v4").write_text("<html><body>502 Bad Gateway</body></html>\n")
        result = self.install("apps")
        self.assert_closed_without_cloudflare(result)
        self.assertEqual(self.cloudflare_rules(), [])
        self.assertFalse(self.etc("yggdrasil/cloudflare-ips").exists())

    def test_given_cloudflare_unreachable_with_80_open_to_everyone_when_installed_then_it_is_closed(self):
        self.install("apps")
        self.set_ufw(self.ufw_rules() + [("80,443/tcp", "Anywhere", "")])
        (self.cloudflare / "ips-v4").unlink()
        result = self.install("apps")
        self.assertIn(self.UNFETCHED, result.stderr)
        self.assertNotIn(("80,443/tcp", "Anywhere", ""), self.ufw_rules())
        self.assertEqual(self.cloudflare_rules(), sorted(CLOUDFLARE_V4 + CLOUDFLARE_V6))

    def test_given_incoming_allowed_by_default_when_checked_then_it_is_reported_and_install_denies_it(self):
        for role in ("dev", "apps"):
            with self.subTest(role=role):
                self.install(role)
                (self.state / "ufw-default-incoming").write_text("allow\n")
                stdout, rows = self.rows(role)
                line = self.row(rows, "ufw")
                self.assertFalse(line.startswith("  ok "), line)
                self.assertIn("allow", line)
                # Read from what ufw says verbosely, which is the only place it says it.
                self.assertIn("sudo -n ufw status verbose", self.calls())
                self.install(role)
                self.assertEqual((self.state / "ufw-default-incoming").read_text().strip(), "deny")
                stdout, rows = self.rows(role)
                self.assertTrue(self.row(rows, "ufw").startswith("  ok "), stdout)

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
        # 80 for one address is fine.
        self.set_ufw(rules + [("80/tcp", "10.0.0.5", "")])
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "ports").startswith("  ok "), stdout)

    def test_given_a_rule_from_anywhere_that_install_did_not_make_when_checked_then_it_is_reported(self):
        cases = {
            # An application profile: "Nginx Full" is 80 and 443, to everyone, under another name.
            "apps": [("Nginx Full", "Anywhere", ""), ("WWW Full", "Anywhere", ""), ("2222/tcp", "Anywhere", ""),
                     ("22/udp", "Anywhere", "")],
            "dev": [("8080/tcp", "Anywhere", ""), ("Nginx Full", "Anywhere", "")],
        }
        for role, extra in cases.items():
            with self.subTest(role=role):
                self.install(role)
                rules = self.ufw_rules()
                for rule in extra:
                    self.set_ufw(rules + [rule])
                    stdout, rows = self.rows(role)
                    line = self.row(rows, "ports")
                    self.assertTrue(line.startswith("  !! "), line)
                    self.assertIn(rule[0], line)
                    self.assertIn("open to everyone", line)
                # Install reports them but takes out only the plain 80/443 rules it knows (the default
                # policy is what makes the firewall step run again here).
                self.set_ufw(rules + extra)
                (self.state / "ufw-default-incoming").write_text("allow\n")
                self.install(role)
                self.assertIn("ufw default deny incoming", self.calls())
                for rule in extra:
                    self.assertIn(rule, self.ufw_rules())

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
        self.backups_working()
        self.renovate_working()
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
        for name, user, path in ISOLATION:
            line = self.row(rows, name)
            self.assertIsNotNone(line, f"no row {name} in:\n{stdout}")
            self.assertTrue(line.startswith("  ok "), line)

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

    def isolation_path(self, kind):
        return {
            "ygg-socket": f"/run/user/{self.uid_of('ygg')}/docker.sock",
            "release-socket": f"/run/user/{self.uid_of('release')}/docker.sock",
            "ygg-secrets": f"{self.etc('yggdrasil')}/platform.env",
            "release-secrets": f"{self.etc('yggdrasil-release')}/agent.env",
        }[kind]

    def test_given_any_user_able_to_read_what_it_must_not_when_checked_then_only_that_row_is_broken(self):
        self.install("dev")
        for name, user, kind in ISOLATION:
            with self.subTest(name):
                for other in DEV_USERS:
                    (self.state / f"readable-{other}").unlink(missing_ok=True)
                (self.state / f"readable-{user}").write_text(self.isolation_path(kind) + "\n")
                stdout, rows = self.rows("dev")
                line = self.row(rows, name)
                self.assertFalse(line.startswith("  ok "), line)
                self.assertIn(self.isolation_path(kind), line)
                # Each row asks about its own user and path, so the others stay green.
                broken = [row for row in rows if "\u2192" in row and not row.startswith("  ok ")]
                self.assertEqual(broken, [line], stdout)

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

    # ---- Fixes: isolation proofs, the profile and the drop-in ------------------------------------

    def test_given_sudo_refusing_to_run_as_ci_when_checked_then_its_isolation_rows_are_not_ok(self):
        self.install("dev")
        (self.state / "sudo-u-denied-ci").write_text("")
        stdout, rows = self.rows("dev")
        for name in ("ci \u2192 ygg engine", "ci \u2192 ygg secrets"):
            line = self.row(rows, name)
            self.assertFalse(line.startswith("  ok "), line)
            self.assertIn("not checked", line)
        self.assertTrue(self.row(rows, "release \u2192 ygg engine").startswith("  ok "), stdout)

    def test_given_no_ygg_socket_when_checked_then_the_engine_isolation_rows_are_not_ok(self):
        self.install("dev")
        (self.state / "exists").write_text("")
        stdout, rows = self.rows("dev")
        for name in ("ci \u2192 ygg engine", "release \u2192 ygg engine"):
            line = self.row(rows, name)
            self.assertFalse(line.startswith("  ok "), line)
            self.assertIn("not checked", line)
            self.assertIn(f"/run/user/{self.uid_of('ygg')}/docker.sock", line)
        self.assertTrue(self.row(rows, "ci \u2192 ygg secrets").startswith("  ok "), stdout)

    def test_given_no_ygg_platform_env_when_checked_then_the_secrets_isolation_row_is_not_ok(self):
        self.install("dev")
        self.etc("yggdrasil/platform.env").unlink()
        stdout, rows = self.rows("dev")
        line = self.row(rows, "ci \u2192 ygg secrets")
        self.assertFalse(line.startswith("  ok "), line)
        self.assertIn("not checked", line)

    def test_given_a_proven_isolation_when_checked_then_the_row_asked_for_the_path_as_root_and_ci(self):
        self.install("dev")
        self.rows("dev")
        calls = self.calls()
        self.assertIn(f"sudo -n test -e /run/user/{self.uid_of('ygg')}/docker.sock", calls)
        self.assertIn(f"sudo -n test -e {self.etc('yggdrasil')}/platform.env", calls)
        self.assertTrue([c for c in calls if c.startswith("sudo -n -u ci ") and c.endswith(" true")])

    def test_given_a_profile_that_cannot_be_read_when_installed_then_install_stops_and_it_is_kept(self):
        self.install("apps")
        profile = self.home_of("ygg") / ".profile"
        profile.write_text("export MINE=1\n")
        (self.state / "unreadable").write_text(f"{profile}\n")
        self.linger_file("ygg").unlink()
        result = self.host("install", "apps")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(profile), result.stderr)
        self.assertEqual(profile.read_text(), "export MINE=1\n")

    def test_given_a_stray_begin_mark_when_installed_twice_then_nothing_of_the_profile_is_lost(self):
        self.install("apps")
        profile = self.home_of("ygg") / ".profile"
        profile.write_text("export A=1\n# >>> yggdrasil >>>\nexport B=2\n")
        self.linger_file("ygg").unlink()
        self.install("apps")
        first = profile.read_text()
        self.linger_file("ygg").unlink()
        self.install("apps")
        self.assertEqual(profile.read_text(), first)
        for kept in ("export A=1", "export B=2"):
            self.assertIn(kept, first)
        self.assertEqual(first.count("export DOCKER_HOST="), 1, first)
        self.assertTrue(first.endswith(self.profile_block("ygg")), first)

    def test_given_a_running_engine_and_no_dropin_when_installed_then_the_engine_restarts(self):
        self.install("apps")
        self.override_of("ygg").unlink()
        self.install("apps")
        self.assertEqual(self.override_of("ygg").read_text(), OVERRIDE)
        self.assertIn(self.as_user("ygg", "systemctl --user restart docker"), self.calls())

    def test_given_a_fresh_engine_when_installed_then_it_is_not_restarted(self):
        self.install("dev")
        self.assertFalse([c for c in self.calls() if "systemctl --user restart" in c], self.calls())

    def test_given_a_stopped_engine_and_no_dropin_when_installed_then_it_is_not_restarted(self):
        self.install("apps")
        self.override_of("ygg").unlink()
        (self.state / "user-active-ygg").unlink()
        self.install("apps")
        self.assertFalse([c for c in self.calls() if "systemctl --user restart" in c], self.calls())

    def test_given_a_dropin_with_more_than_the_two_lines_when_checked_then_it_is_not_ok_and_install_rewrites_it(self):
        self.install("apps")
        self.override_of("ygg").write_text(OVERRIDE + 'Environment="OTHER=1"\n')
        stdout, rows = self.rows("apps")
        self.assertFalse(self.row(rows, "client addresses ygg").startswith("  ok "), stdout)
        self.install("apps")
        self.assertEqual(self.override_of("ygg").read_text(), OVERRIDE)

    # ---- The backup timer ------------------------------------------------------------------------

    def unit_dir(self, user="ygg"):
        return self.home_of(user) / ".config" / "systemd" / "user"

    def repo_unit(self, name):
        return (HOST.parent.parent / "platform" / "systemd" / name).read_text()

    def timer_row(self, rows):
        return self.row(rows, "backup timer")

    LAST_RUN = "Wed 2026-10-21 03:31:02 UTC"

    def backups_working(self, age_hours=2):
        """What the owner adds after install: backup.conf, a run that succeeded, a fresh day of backups."""
        self.etc("yggdrasil/backup.conf").write_text("BACKUP_TARGET=ygg@other:.\nforgejo\n")
        self.last_run("success")
        self.backup_day("2026-10-21", age_hours)

    def last_run(self, result, started=LAST_RUN):
        (self.state / "user-show-ygg").write_text(f"Result={result}\nExecMainStartTimestamp={started}\n")

    def backup_day(self, name, age_hours):
        day = self.home_of("ygg") / "backups" / name
        day.mkdir(parents=True, exist_ok=True)
        moment = time.time() - age_hours * 3600
        os.utime(day, (moment, moment))
        return day

    def test_given_working_backups_when_checked_then_the_three_backup_rows_are_ok(self):
        self.install("apps")
        self.backups_working()
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "backup.conf").startswith("  ok "), stdout)
        self.assertIn(self.LAST_RUN, self.row(rows, "last backup"))
        self.assertTrue(self.row(rows, "last backup").startswith("  ok "), stdout)
        self.assertTrue(self.row(rows, "newest backup").startswith("  ok "), stdout)
        self.assertIn("2026-10-21", self.row(rows, "newest backup"))
        # Asked of ygg (the directory and the home are closed to the admin), without a password.
        self.assertIn(self.as_user("ygg", "systemctl --user show yggdrasil-backup.service -p Result -p ExecMainStartTimestamp")
                      .replace("sudo -u", "sudo -n -u"), self.calls())

    def test_given_no_backup_conf_when_checked_then_it_is_reported(self):
        self.install("dev")
        self.backups_working()
        self.etc("yggdrasil/backup.conf").unlink()
        stdout, rows = self.rows("dev")
        line = self.row(rows, "backup.conf")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("env/backup.conf.example", line)

    def test_given_a_failed_last_run_when_checked_then_it_is_reported(self):
        self.install("apps")
        self.backups_working()
        self.last_run("exit-code")
        stdout, rows = self.rows("apps")
        line = self.row(rows, "last backup")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("exit-code", line)
        self.assertIn("journalctl", line)

    def test_given_a_backup_that_never_ran_when_checked_then_it_is_a_warning(self):
        self.install("apps")
        stdout, rows = self.rows("apps")
        line = self.row(rows, "last backup")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("never run", line)
        line = self.row(rows, "newest backup")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("none", line)

    def test_given_a_newest_backup_older_than_36_hours_when_checked_then_it_is_a_warning(self):
        self.install("apps")
        self.backups_working(age_hours=40)
        self.backup_day("2026-10-19", 90)
        stdout, rows = self.rows("apps")
        line = self.row(rows, "newest backup")
        self.assertTrue(line.startswith("  !! "), line)
        # The newest day is the one named last, and its age is said.
        self.assertIn("2026-10-21", line)
        self.assertIn("40 hours", line)
        self.backups_working(age_hours=35)
        stdout, rows = self.rows("apps")
        self.assertTrue(self.row(rows, "newest backup").startswith("  ok "), stdout)

    def test_given_role_dev_when_installed_then_ygg_has_the_backup_timer_enabled(self):
        self.install("dev")
        for name in ("yggdrasil-backup.service", "yggdrasil-backup.timer"):
            self.assertEqual((self.unit_dir() / name).read_text(), self.repo_unit(name))
        calls = self.calls()
        # Written by ygg itself (its home is closed to the admin), as a user unit of its own manager.
        self.assertIn(self.as_user("ygg", "tee", str(self.unit_dir() / "yggdrasil-backup.timer")), calls)
        reload_call = calls.index(self.as_user("ygg", "systemctl --user daemon-reload"),
                                  calls.index(self.as_user("ygg", "tee", str(self.unit_dir() / "yggdrasil-backup.timer"))))
        enable = self.as_user("ygg", "systemctl --user enable --now yggdrasil-backup.timer")
        self.assertIn(enable, calls)
        self.assertLess(reload_call, calls.index(enable))
        # After the engine it backs up, and only for ygg.
        self.assertLess(calls.index(self.as_user("ygg", "systemctl --user enable --now docker")), calls.index(enable))
        for user in DEV_USERS[1:]:
            self.assertFalse((self.unit_dir(user) / "yggdrasil-backup.timer").exists(), user)
        self.assertEqual([c for c in calls if c.startswith("sudo") and c.endswith("yggdrasil-backup.timer") and " enable " in c], [enable])
        stdout, rows = self.rows("dev")
        self.assertTrue(self.timer_row(rows).startswith("  ok "), stdout)

    def test_given_role_apps_when_installed_then_ygg_has_the_backup_timer_enabled(self):
        self.install("apps")
        self.assertEqual((self.unit_dir() / "yggdrasil-backup.timer").read_text(),
                         self.repo_unit("yggdrasil-backup.timer"))
        self.assertIn(self.as_user("ygg", "systemctl --user enable --now yggdrasil-backup.timer"), self.calls())
        stdout, rows = self.rows("apps")
        self.assertTrue(self.timer_row(rows).startswith("  ok "), stdout)

    def test_given_the_timer_installed_when_installed_again_then_nothing_is_written_or_enabled(self):
        self.install("dev")
        # The checks read the units (sudo -n); only a step writes or enables them.
        def changes():
            return [c for c in self.calls() if "yggdrasil-backup" in c and c.startswith("sudo -u")]
        before = changes()
        self.assertTrue(before)
        self.install("dev")
        self.assertEqual(changes(), before)

    def test_given_a_changed_unit_when_checked_then_it_is_not_ok_and_install_writes_the_checkouts_copy(self):
        self.install("apps")
        service = self.unit_dir() / "yggdrasil-backup.service"
        service.write_text("[Service]\nExecStart=/bin/true\n")
        stdout, rows = self.rows("apps")
        self.assertFalse(self.timer_row(rows).startswith("  ok "), stdout)
        self.assertIn("yggdrasil-backup.service", self.timer_row(rows))
        self.install("apps")
        self.assertEqual(service.read_text(), self.repo_unit("yggdrasil-backup.service"))

    def test_given_a_timer_that_is_not_enabled_when_checked_then_it_is_missing_and_install_enables_it(self):
        self.install("apps")
        (self.state / "user-enabled-ygg").write_text("")
        stdout, rows = self.rows("apps")
        self.assertTrue(self.timer_row(rows).startswith("  -- "), stdout)
        self.install("apps")
        self.assertTrue(self.timer_row(self.rows("apps")[1]).startswith("  ok "))

    def test_given_no_ygg_user_yet_when_checked_then_the_timer_is_missing_and_nothing_runs_as_a_user(self):
        stdout, rows = self.rows("apps")
        self.assertTrue(self.timer_row(rows).startswith("  -- "), stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("sudo -u", "sudo -n -u"))], [])

    def test_given_sudo_asking_for_a_password_when_checked_then_the_timer_is_not_checked(self):
        self.install("apps")
        (self.state / "sudo-denied").write_text("")
        calls_before = len(self.calls())
        stdout, rows = self.rows("apps")
        line = self.timer_row(rows)
        self.assertTrue(line.startswith("  !! "), stdout)
        self.assertIn("not checked", line)
        for name in ("backup.conf", "last backup", "newest backup"):
            self.assertIn("not checked", self.row(rows, name), stdout)
        self.assertEqual([c for c in self.calls()[calls_before:] if c.startswith("sudo -u")], [])

    # ---- The prune timers ------------------------------------------------------------------------

    PRUNE_UNITS = ("yggdrasil-prune.service", "yggdrasil-prune.timer")

    def prune_row(self, rows, user):
        return self.row(rows, f"prune timer {user}")

    def prune_changes(self):
        return [c for c in self.calls() if "yggdrasil-prune" in c and c.startswith("sudo -u")]

    def test_given_role_dev_when_installed_then_ci_and_release_get_a_weekly_prune_timer(self):
        self.install("dev")
        calls = self.calls()
        stdout, rows = self.rows("dev")
        for user in ("ci", "release"):
            with self.subTest(user=user):
                for name in self.PRUNE_UNITS:
                    self.assertEqual((self.unit_dir(user) / name).read_text(), self.repo_unit(name))
                # Written by the user itself (the homes are closed to the admin), into its own manager.
                written = self.as_user(user, "tee", str(self.unit_dir(user) / "yggdrasil-prune.timer"))
                self.assertIn(written, calls)
                reload_call = calls.index(self.as_user(user, "systemctl --user daemon-reload"), calls.index(written))
                enable = self.as_user(user, "systemctl --user enable --now yggdrasil-prune.timer")
                self.assertIn(enable, calls)
                self.assertLess(reload_call, calls.index(enable))
                # After the engine it prunes.
                self.assertLess(calls.index(self.as_user(user, "systemctl --user enable --now docker")),
                                calls.index(enable))
                line = self.prune_row(rows, user)
                self.assertIsNotNone(line, stdout)
                self.assertTrue(line.startswith("  ok "), line)
                self.assertIn("Sundays at 03:00", line)
        # Only those two users: the others have engines too, and no one asked for theirs to be pruned.
        for user in ("ygg", "renovate", "dev"):
            for name in self.PRUNE_UNITS:
                self.assertFalse((self.unit_dir(user) / name).exists(), (user, name))
            self.assertIsNone(self.prune_row(rows, user), stdout)
        # The step comes right after the engines, before the backup and renovate timers.
        self.assertLess(calls.index(self.as_user("release", "systemctl --user enable --now yggdrasil-prune.timer")),
                        calls.index(self.as_user("ygg", "systemctl --user enable --now yggdrasil-backup.timer")))

    def test_given_role_apps_when_installed_then_no_prune_timer_is_made(self):
        self.install("apps")
        # ci and release do not exist on the apps machine; ygg's engine is the deploys', not CI's.
        for name in self.PRUNE_UNITS:
            self.assertFalse((self.unit_dir("ygg") / name).exists(), name)
        self.assertEqual([c for c in self.calls() if "prune" in c], [])
        stdout, rows = self.rows("apps")
        self.assertEqual([line for line in rows if "prune" in line], [], stdout)

    def test_given_the_prune_timers_installed_when_installed_again_then_nothing_is_written_or_enabled(self):
        self.install("dev")
        before = self.prune_changes()
        self.assertTrue(before)
        self.install("dev")
        self.assertEqual(self.prune_changes(), before)

    def test_given_a_changed_prune_unit_when_checked_then_it_is_not_ok_and_install_writes_the_checkouts_copy(self):
        self.install("dev")
        service = self.unit_dir("release") / "yggdrasil-prune.service"
        service.write_text("[Service]\nExecStart=/bin/true\n")
        stdout, rows = self.rows("dev")
        self.assertFalse(self.prune_row(rows, "release").startswith("  ok "), stdout)
        self.assertIn("yggdrasil-prune.service", self.prune_row(rows, "release"))
        self.assertTrue(self.prune_row(rows, "ci").startswith("  ok "), stdout)
        self.install("dev")
        self.assertEqual(service.read_text(), self.repo_unit("yggdrasil-prune.service"))

    def test_given_a_prune_timer_that_is_not_enabled_when_checked_then_it_is_missing_and_install_enables_it(self):
        self.install("dev")
        (self.state / "user-enabled-ci").write_text("")
        stdout, rows = self.rows("dev")
        self.assertTrue(self.prune_row(rows, "ci").startswith("  -- "), stdout)
        self.install("dev")
        self.assertTrue(self.prune_row(self.rows("dev")[1], "ci").startswith("  ok "))

    def test_given_no_ci_user_yet_when_checked_then_the_prune_timer_is_missing_and_nothing_runs_as_it(self):
        stdout, rows = self.rows("dev")
        self.assertTrue(self.prune_row(rows, "ci").startswith("  -- "), stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("sudo -u ci", "sudo -n -u ci"))], [])

    def test_given_sudo_asking_for_a_password_when_checked_then_the_prune_timers_are_not_checked(self):
        self.install("dev")
        (self.state / "sudo-denied").write_text("")
        calls_before = len(self.calls())
        stdout, rows = self.rows("dev")
        for user in ("ci", "release"):
            line = self.prune_row(rows, user)
            self.assertTrue(line.startswith("  !! "), stdout)
            self.assertIn("not checked", line)
        self.assertEqual([c for c in self.calls()[calls_before:] if c.startswith("sudo -u")], [])

    # ---- The renovate timer ----------------------------------------------------------------------

    RENOVATE_UNITS = ("yggdrasil-renovate.service", "yggdrasil-renovate.timer")

    def renovate_row(self, rows):
        return self.row(rows, "renovate timer")

    def renovate_working(self):
        """What the owner adds after install on the dev machine: the token in renovate.env, a run that succeeded."""
        env = self.etc("yggdrasil-renovate/renovate.env")
        env.write_text(env.read_text().replace("RENOVATE_TOKEN=\n", "RENOVATE_TOKEN=t0ken\n")
                       .replace("RENOVATE_GITHUB_COM_TOKEN=\n", "RENOVATE_GITHUB_COM_TOKEN=gh0ken\n"))
        (self.state / "user-show-renovate").write_text(f"Result=success\nExecMainStartTimestamp={self.LAST_RUN}\n")

    def renovate_changes(self):
        return [c for c in self.calls() if "yggdrasil-renovate" in c and c.startswith("sudo -u")]

    def test_given_role_dev_when_installed_then_renovate_has_its_timer_enabled(self):
        self.install("dev")
        for name in self.RENOVATE_UNITS:
            self.assertEqual((self.unit_dir("renovate") / name).read_text(), self.repo_unit(name))
        calls = self.calls()
        # Written by renovate itself (its home is closed to the admin), as a user unit of its own manager.
        written = self.as_user("renovate", "tee", str(self.unit_dir("renovate") / "yggdrasil-renovate.timer"))
        self.assertIn(written, calls)
        reload_call = calls.index(self.as_user("renovate", "systemctl --user daemon-reload"), calls.index(written))
        enable = self.as_user("renovate", "systemctl --user enable --now yggdrasil-renovate.timer")
        self.assertIn(enable, calls)
        self.assertLess(reload_call, calls.index(enable))
        # After the engine it runs Renovate in, and for renovate only: the other users get no such unit.
        self.assertLess(calls.index(self.as_user("renovate", "systemctl --user enable --now docker")), calls.index(enable))
        for user in ("ygg", "ci", "release", "dev"):
            for name in self.RENOVATE_UNITS:
                self.assertFalse((self.unit_dir(user) / name).exists(), (user, name))
        self.assertEqual([c for c in calls if c.startswith("sudo") and c.endswith("yggdrasil-renovate.timer") and " enable " in c], [enable])
        stdout, rows = self.rows("dev")
        self.assertTrue(self.renovate_row(rows).startswith("  ok "), stdout)
        self.assertIn("05:00", self.renovate_row(rows))

    def test_given_role_apps_when_installed_then_there_is_no_renovate_timer(self):
        self.install("apps")
        for user in ("ygg",):
            for name in self.RENOVATE_UNITS:
                self.assertFalse((self.unit_dir(user) / name).exists(), (user, name))
        self.assertEqual([c for c in self.calls() if "renovate" in c], [])
        stdout, rows = self.rows("apps")
        self.assertIsNone(self.renovate_row(rows), stdout)

    def test_given_the_renovate_timer_installed_when_installed_again_then_nothing_is_written_or_enabled(self):
        self.install("dev")
        before = self.renovate_changes()
        self.assertTrue(before)
        self.install("dev")
        self.assertEqual(self.renovate_changes(), before)

    def test_given_a_changed_renovate_unit_when_checked_then_it_is_not_ok_and_install_writes_the_checkouts_copy(self):
        self.install("dev")
        service = self.unit_dir("renovate") / "yggdrasil-renovate.service"
        service.write_text("[Service]\nExecStart=/bin/true\n")
        stdout, rows = self.rows("dev")
        self.assertFalse(self.renovate_row(rows).startswith("  ok "), stdout)
        self.assertIn("yggdrasil-renovate.service", self.renovate_row(rows))
        self.install("dev")
        self.assertEqual(service.read_text(), self.repo_unit("yggdrasil-renovate.service"))

    def test_given_a_renovate_timer_that_is_not_enabled_when_checked_then_it_is_missing_and_install_enables_it(self):
        self.install("dev")
        (self.state / "user-enabled-renovate").write_text("")
        stdout, rows = self.rows("dev")
        self.assertTrue(self.renovate_row(rows).startswith("  -- "), stdout)
        self.install("dev")
        self.assertTrue(self.renovate_row(self.rows("dev")[1]).startswith("  ok "))

    def test_given_no_renovate_user_yet_when_checked_then_the_timer_is_missing_and_nothing_runs_as_it(self):
        stdout, rows = self.rows("dev")
        self.assertTrue(self.renovate_row(rows).startswith("  -- "), stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("sudo -u renovate", "sudo -n -u renovate"))], [])

    def test_given_sudo_asking_for_a_password_when_checked_then_the_renovate_timer_is_not_checked(self):
        self.install("dev")
        (self.state / "sudo-denied").write_text("")
        calls_before = len(self.calls())
        stdout, rows = self.rows("dev")
        line = self.renovate_row(rows)
        self.assertTrue(line.startswith("  !! "), stdout)
        self.assertIn("not checked", line)
        for name in ("renovate.env", "renovate github token", "last renovate run"):
            self.assertIn("not checked", self.row(rows, name), stdout)
        self.assertEqual([c for c in self.calls()[calls_before:] if c.startswith("sudo -u")], [])

    def test_given_the_template_copy_when_checked_then_the_empty_token_is_reported_until_it_is_filled(self):
        self.install("dev")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "renovate.env")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("RENOVATE_TOKEN is empty", line)
        self.assertNotIn("t0ken", stdout)
        env = self.etc("yggdrasil-renovate/renovate.env")
        env.write_text(env.read_text().replace("RENOVATE_TOKEN=\n", "RENOVATE_TOKEN=t0ken\n"))
        stdout, rows = self.rows("dev")
        self.assertTrue(self.row(rows, "renovate.env").startswith("  ok "), stdout)
        # The token itself is never printed by the check.
        self.assertNotIn("t0ken", stdout)
        env.unlink()
        stdout, rows = self.rows("dev")
        self.assertIn("env/renovate.env.example", self.row(rows, "renovate.env"))

    def test_given_the_template_copy_when_checked_then_the_optional_github_token_is_a_warning_until_it_is_filled(self):
        self.install("dev")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "renovate github token")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("release notes and changelogs from GitHub will be rate-limited", line)
        env = self.etc("yggdrasil-renovate/renovate.env")
        env.write_text(env.read_text().replace("RENOVATE_GITHUB_COM_TOKEN=\n", "RENOVATE_GITHUB_COM_TOKEN=gh0ken\n"))
        stdout, rows = self.rows("dev")
        self.assertTrue(self.row(rows, "renovate github token").startswith("  ok "), stdout)
        # The token itself is never printed by the check.
        self.assertNotIn("gh0ken", stdout)

    def test_given_renovate_runs_when_checked_then_the_last_run_is_reported(self):
        self.install("dev")
        stdout, rows = self.rows("dev")
        self.assertIn("never run", self.row(rows, "last renovate run"))
        (self.state / "user-show-renovate").write_text(f"Result=success\nExecMainStartTimestamp={self.LAST_RUN}\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "last renovate run")
        self.assertTrue(line.startswith("  ok "), line)
        self.assertIn(self.LAST_RUN, line)
        (self.state / "user-show-renovate").write_text(f"Result=exit-code\nExecMainStartTimestamp={self.LAST_RUN}\n")
        stdout, rows = self.rows("dev")
        line = self.row(rows, "last renovate run")
        self.assertTrue(line.startswith("  !! "), line)
        self.assertIn("exit-code", line)
        self.assertIn("journalctl --user -u yggdrasil-renovate", line)


class PruneUnitTests(unittest.TestCase):
    """The two units of the prune timers, read as text: what they run and when."""

    UNITS = HOST.parent.parent / "platform" / "systemd"

    def test_given_the_timer_when_read_then_it_fires_on_sundays_at_three_with_catch_up_and_a_delay(self):
        text = (self.UNITS / "yggdrasil-prune.timer").read_text()
        self.assertRegex(text, r"(?m)^OnCalendar=Sun 03:00$")
        self.assertRegex(text, r"(?m)^Persistent=true$")
        self.assertRegex(text, r"(?m)^RandomizedDelaySec=1h$")
        self.assertRegex(text, r"(?m)^WantedBy=timers\.target$")

    def test_given_the_service_when_read_then_it_prunes_the_users_own_engine_of_what_is_a_week_old(self):
        text = (self.UNITS / "yggdrasil-prune.service").read_text()
        self.assertRegex(text, r"(?m)^After=docker\.service$")
        self.assertRegex(text, r"(?m)^Type=oneshot$")
        # A user unit doesn't read .profile: it names the user's own engine itself, never the system's.
        self.assertRegex(text, r"(?m)^Environment=DOCKER_HOST=unix://%t/docker\.sock$")
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertEqual(
            re.findall(r"(?m)^ExecStart=(.*)$", text),
            ["/usr/bin/docker system prune --all --force --filter until=168h",
             "/usr/bin/docker builder prune --all --force --filter until=168h"])
        # No volume is pruned (a build's cache volumes are state), and nothing here runs as another user.
        self.assertNotRegex(text, r"(?m)--volumes|volume prune|^User=")


if __name__ == "__main__":
    unittest.main()
