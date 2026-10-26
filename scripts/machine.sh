# shellcheck shell=bash
# root, the checkout, is host.sh's: it sets it before it sources this file (SC2154 on its own, which
# CI runs without host.sh). The step_* and machine_check_* functions are called by name, built from
# MACHINE_STEPS (check_machine, install_machine), never directly.
# shellcheck disable=SC2154
#
# The machine roles of scripts/host.sh: what `check` and `install` do for `dev` and `apps` (Ubuntu
# 24.04 or 26.04 hosts that run every engine rootless), as opposed to `workstation`, which keeps today's
# check_host / install_host. Sourced by host.sh: functions and tables only, nothing runs here, and it
# relies on host.sh's say, warn, die, row, confirm, as_root, apt_get, installed and os_field.
#
# A role is a list of STEPS, each one piece of the machine:
#
#   machine_check_<step>   prints its report rows, and adds the step to machine_todo when the machine
#                          isn't as the step wants it (check_machine runs them all);
#   step_<step>            makes it so (install_machine runs the ones in machine_todo, in the order of
#                          MACHINE_STEPS, asking first). It must be safe to run again.
#
# <step> has its dashes written as underscores in the function names (rootful-docker-off ->
# rootful_docker_off). A new step is a name in MACHINE_STEPS, those two functions and a line in
# machine_step_text; the order of MACHINE_STEPS is the order of install (and of the report).
#
# Every file these steps read or write outside a home is under $YGG_SYSROOT (empty on a real host, a
# temporary directory in the tests), reads included.

MACHINE_ROLES=(dev apps workstation)
# The Ubuntu releases install knows, by VERSION_ID: the LTS releases. Docker's apt repository is picked
# by the release's codename (host.sh), so a release is added here once that repository has it.
MACHINE_UBUNTU_VERSIONS=(24.04 26.04)
MACHINE_STEPS=(packages rootful-docker-off system-settings unattended-upgrades users ci-limits low-ports ssh checkout secrets firewall engines prune-timer backup-timer renovate-timer role-file)

# The packages of a dev or apps machine: today's tools, rootless Docker's needs, Docker's own (from
# its apt repository), and what the later steps use.
MACHINE_TOOL_PACKAGES=(git curl openssl python3 apache2-utils ca-certificates python3-yaml python3-cryptography)
MACHINE_DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin)
MACHINE_PACKAGES=("${MACHINE_TOOL_PACKAGES[@]}" uidmap dbus-user-session slirp4netns "${MACHINE_DOCKER_PACKAGES[@]}" ufw rsync unattended-upgrades)

# The system's own engine: the rootless ones of the service users replace it.
ROOTFUL_DOCKER_UNITS=(docker.service docker.socket containerd.service)

# Filled in by check_machine: the steps to run, in MACHINE_STEPS order, and the packages missing.
machine_todo=() machine_missing=()
# The role being checked or installed, for the steps that need it.
machine_current_role=""

# ---- Roles --------------------------------------------------------------------------------------

# machine_role [<role>]: dev, apps or workstation: the argument, else the first word of the role file
# the installer writes, else workstation.
machine_role() {
  local role=${1:-} file="${YGG_SYSROOT:-}/etc/yggdrasil-role" known
  if [[ -z "$role" && -r "$file" ]]; then
    read -r role _ <"$file" || true
  fi
  role=${role:-workstation}
  for known in "${MACHINE_ROLES[@]}"; do
    if [[ "$role" == "$known" ]]; then
      echo "$role"
      return 0
    fi
  done
  die "unknown role '$role': it is one of ${MACHINE_ROLES[*]}"
}

# role_users <role>: the role's service users, one per line, in the order they are set up.
role_users() {
  case $1 in
    dev) printf '%s\n' ygg ci release renovate dev ;;
    apps) printf '%s\n' ygg ;;
  esac
}

# role_ssh_users <role>: the service users that may log in over ssh (the admin may too), in order.
# The others (ci, release, renovate) have no shell: they only ever run as a service.
role_ssh_users() {
  case $1 in
    dev) printf '%s\n' ygg dev ;;
    apps) printf '%s\n' ygg ;;
  esac
}

# machine_user_shell <user>: the shell a service user of the role being checked or installed gets.
machine_user_shell() {
  local ssh_user
  for ssh_user in $(role_ssh_users "$machine_current_role"); do
    if [[ "$ssh_user" == "$1" ]]; then
      echo /bin/bash
      return 0
    fi
  done
  echo /usr/sbin/nologin
}

# passwd_field <user> <n>: field n of the user's passwd entry (3 uid, 6 home, 7 shell); nothing when
# there is no such user. Uids and homes are asked of the system, never assumed.
passwd_field() {
  local entry
  entry=$(getent passwd "$1" 2>/dev/null) || return 0
  cut -d: -f"$2" <<<"$entry"
}

# user_exists <user>
user_exists() { getent passwd "$1" >/dev/null 2>&1; }

# secrets_dir_of <user>: the user's secrets directory under $YGG_SYSROOT: ygg keeps /etc/yggdrasil,
# every other service user has /etc/yggdrasil-<user>.
secrets_dir_of() {
  if [[ "$1" == ygg ]]; then
    echo "${YGG_SYSROOT:-}/etc/yggdrasil"
  else
    echo "${YGG_SYSROOT:-}/etc/yggdrasil-$1"
  fi
}

# machine_ubuntu_known: this is one of the Ubuntu releases of MACHINE_UBUNTU_VERSIONS.
machine_ubuntu_known() {
  is_ubuntu || return 1
  local version
  version=$(os_field VERSION_ID)
  [[ " ${MACHINE_UBUNTU_VERSIONS[*]} " == *" $version "* ]]
}

# The releases as a message says them: "Ubuntu 24.04 or 26.04 LTS".
machine_ubuntu_text() {
  local versions=("${MACHINE_UBUNTU_VERSIONS[@]}")
  local last=${versions[-1]}
  unset 'versions[-1]'
  if ((${#versions[@]})); then
    local IFS=,
    echo "Ubuntu ${versions[*]} or $last LTS"
  else
    echo "Ubuntu $last LTS"
  fi
}

# machine_preflight <role>: install for dev and apps is for the Ubuntu releases above, run by the admin
# with sudo.
machine_preflight() {
  machine_ubuntu_known || die "the $1 role installs on $(machine_ubuntu_text) only; this is $(os_field PRETTY_NAME || echo unknown)"
  [[ "$(id -u)" != 0 ]] || die "run install as the admin user, not as root: it asks sudo for what needs root, and the admin is whoever runs it"
  has sudo || die "installing needs sudo: install it first"
}

# The checkout install sets up: the backup unit runs /opt/yggdrasil/scripts/backup.sh, and the checkout
# step gives the checkout to ygg. Under $YGG_SYSROOT in the tests (a link to the checkout under test).
machine_checkout_path() { echo "${YGG_SYSROOT:-}/opt/yggdrasil"; }

# machine_checkout_at_home: this checkout ($root) is /opt/yggdrasil, both resolved (readlink -f).
machine_checkout_at_home() {
  local here there
  here=$(readlink -f "$root") || return 1
  there=$(readlink -f "$(machine_checkout_path)" 2>/dev/null) || return 1
  [[ -n "$there" && -d "$there" && "$here" == "$there" ]]
}

# machine_checkout_refusal: dies, with nothing changed, when install runs from another checkout: its
# checkout step would hand that one (a clone in the admin's home, say) to ygg, and the backup timer
# would run a copy that isn't there.
machine_checkout_refusal() {
  machine_checkout_at_home \
    || die "this checkout is $root, not $(machine_checkout_path): clone yggdrasil there (setup.md step 8) and run install from it"
}

# ---- System files -------------------------------------------------------------------------------

# write_system_file <path> <mode> <content>: the content plus a newline, as root; nothing is written
# when the file already holds it.
write_system_file() {
  local path=$1 mode=$2 content=$3
  if [[ -f "$path" && "$(<"$path")" == "$content" ]]; then
    [[ "$(stat -c %a "$path")" == "${mode#0}" ]] || as_root chmod "$mode" "$path"
    return 0
  fi
  # Only a directory that isn't there is made: install -d -m would reset the mode of one that is (a
  # secrets directory, 750, that holds a file of its own).
  [[ -d "$(dirname "$path")" ]] || as_root install -d -m 0755 "$(dirname "$path")"
  printf '%s\n' "$content" | as_root tee "$path" >/dev/null
  as_root chmod "$mode" "$path"
}

# file_holds <path> <content>: the file's content is exactly this (trailing newlines aside).
file_holds() { [[ -f "$1" && "$(<"$1")" == "$2" ]]; }

machine_sysctl_path() { echo "${YGG_SYSROOT:-}/etc/sysctl.d/90-yggdrasil.conf"; }
machine_apparmor_path() { echo "${YGG_SYSROOT:-}/etc/apparmor.d/usr.bin.rootlesskit"; }
machine_delegate_path() { echo "${YGG_SYSROOT:-}/etc/systemd/system/user@.service.d/delegate.conf"; }
machine_upgrades_path() { echo "${YGG_SYSROOT:-}/etc/apt/apt.conf.d/20auto-upgrades"; }
# sshd takes the FIRST value it finds across sshd_config.d in lexical order, and cloud images ship
# 50-cloud-init.conf (PasswordAuthentication yes) and 60-cloudimg-settings.conf: the drop-in must sort
# before them, or the hardening does nothing.
machine_ssh_path() { echo "${YGG_SYSROOT:-}/etc/ssh/sshd_config.d/00-yggdrasil.conf"; }
# What an earlier install called it: removed once the new one is in force.
machine_ssh_legacy_path() { echo "${YGG_SYSROOT:-}/etc/ssh/sshd_config.d/90-yggdrasil.conf"; }

# Security updates install themselves: nobody logs in to a CI or apps machine to run them.
machine_upgrades_content() {
  printf '%s\n' 'APT::Periodic::Update-Package-Lists "1";' 'APT::Periodic::Unattended-Upgrade "1";'
}

# The ci user's share of the machine, so a build can't starve Forgejo or the release agent: six cores
# and 20G. The limits go on the user's slice, which is named by uid.
machine_ci_limits_path() {
  local uid
  uid=$(passwd_field ci 3)
  [[ -z "$uid" ]] || echo "${YGG_SYSROOT:-}/etc/systemd/system/user-$uid.slice.d/limits.conf"
}
machine_ci_limits_content() { printf '%s\n' "[Slice]" "CPUQuota=600%" "MemoryMax=20G"; }

# The unprivileged-ports sysctl (machine_sysctl_content) lets every user bind 80 and 443, not only ygg:
# a check in ci's engine could publish -p 80:80 -p 443:443 and win the ports while ygg's Traefik
# restarts, then answer Let's Encrypt for git. and jenkins. So the users that run code the owner didn't
# write or hasn't reviewed (ci, release, renovate, dev) may not bind a port below 1024 at all, on their
# slice, which holds their engine and every container in it. That includes a container binding 80 in
# its own network namespace (an nginx in a check): such a check must listen on 1024 or above.
#
# NEEDS VERIFYING on the real host (Ubuntu 24.04, systemd 255, and 26.04; cgroup v2): that SocketBindDeny= takes
# effect on a user-<uid>.slice drop-in after `systemctl daemon-reload` (or only once the user's
# manager restarts), and that rootlesskit's port forwarder (slirp4netns port driver) is refused the
# bind. Syntax: systemd.resource-control(5), "[address-family:][transport-protocol:][ip-ports]".
MACHINE_LOW_PORT_USERS=(ci release renovate dev)
machine_low_ports_path() {
  local uid
  uid=$(passwd_field "$1" 3)
  [[ -z "$uid" ]] || echo "${YGG_SYSROOT:-}/etc/systemd/system/user-$uid.slice.d/50-yggdrasil-ports.conf"
}
machine_low_ports_content() { printf '%s\n' "[Slice]" "SocketBindDeny=tcp:1-1023" "SocketBindDeny=udp:1-1023"; }

# The admin is whoever runs install (it refuses root). Only the admin and the role's ssh users may log
# in, and only with a key.
machine_admin() { id -un; }
machine_admin_keys() {
  local home
  home=$(passwd_field "$(machine_admin)" 6)
  echo "${home:-$HOME}/.ssh/authorized_keys"
}
# machine_ssh_allowed: the users who may log in, one per line: the admin, then the role's ssh users.
machine_ssh_allowed() {
  local admin ssh_user
  admin=$(machine_admin)
  echo "$admin"
  for ssh_user in $(role_ssh_users "$machine_current_role"); do
    [[ "$ssh_user" == "$admin" ]] || echo "$ssh_user"
  done
}
machine_ssh_content() {
  printf '%s\n' "PermitRootLogin no" "PasswordAuthentication no" "KbdInteractiveAuthentication no" \
    "AllowUsers $(machine_ssh_allowed | paste -sd' ' -)"
}

# machine_ssh_effective: what sshd would run with (`sshd -T`: every file, first value wins), as root;
# fails when it can't be read (no sudo without a password, sshd missing).
machine_ssh_effective() {
  if ((EUID == 0)); then
    sshd -T 2>/dev/null
  elif has sudo; then
    sudo -n sshd -T 2>/dev/null
  else
    return 1
  fi
}

# machine_ssh_conflict <sshd -T output>: the first setting that isn't what the hardening wants, said
# for a person; nothing when they all are. A different value here means another file (or sshd_config
# itself) sets it first.
machine_ssh_conflict() {
  local pair key want value users expected
  for pair in "permitrootlogin no" "passwordauthentication no" "kbdinteractiveauthentication no"; do
    key=${pair% *}
    want=${pair#* }
    value=$(awk -v key="$key" '$1 == key { print $2; exit }' <<<"$1")
    if [[ "$value" != "$want" ]]; then
      echo "$key is ${value:-unset}, not $want"
      return 0
    fi
  done
  users=$(awk '$1 == "allowusers" { for (i = 2; i <= NF; i++) print $i }' <<<"$1" | sort | paste -sd' ' -)
  expected=$(machine_ssh_allowed | sort | paste -sd' ' -)
  if [[ "$users" != "$expected" ]]; then
    echo "allowusers is '${users:-unset}', not '$expected'"
  fi
}

# machine_ssh_keys_refusal: dies, with nothing changed, when the admin has no key: hardening switches
# passwords off, so it would lock the admin out.
machine_ssh_keys_refusal() {
  local keys
  keys=$(machine_admin_keys)
  [[ -s "$keys" ]] || die "$keys is missing or empty: switching passwords off would lock you out. Put your public key in it (ssh-copy-id from your own machine) and run install again."
}

# machine_secret_files <user>: the templates copied into the user's secrets directory, as
# "<template in env/>:<file>".
machine_secret_files() {
  case $1 in
    ygg) printf '%s\n' platform.env.example:platform.env acme.env.example:acme.env ;;
    ci | release) printf '%s\n' agent.env.example:agent.env ;;
    renovate) printf '%s\n' renovate.env.example:renovate.env ;;
  esac
}

# Rootless engines publish ports below 1024 (80 and 443) without a privileged helper.
machine_sysctl_content() { echo "net.ipv4.ip_unprivileged_port_start=80"; }

# Ubuntu 24.04 and later confine unprivileged user namespaces; this profile lets rootlesskit use them.
machine_apparmor_content() {
  cat <<'EOF'
abi <abi/4.0>,
include <tunables/global>

"/usr/bin/rootlesskit" flags=(unconfined) {
  userns,

  include if exists <local/usr.bin.rootlesskit>
}
EOF
}

# Users' units get the controllers a rootless engine's resource limits (CPUQuota, MemoryMax) need.
machine_delegate_content() {
  printf '%s\n' "[Service]" "Delegate=cpu cpuset io memory pids"
}

# ---- Rootless engines ---------------------------------------------------------------------------

# The marks around what install adds to a user's .profile, so a later install replaces it instead of
# adding a second one.
PROFILE_BEGIN="# >>> yggdrasil >>>"
PROFILE_END="# <<< yggdrasil <<<"

# Where the systemd user manager of a service user keeps the drop-in of its docker.service, in the home.
machine_override_path() { echo "$(passwd_field "$1" 6)/.config/systemd/user/docker.service.d/override.conf"; }
# The engine publishes and reaches through slirp4netns, so a container sees the addresses of its clients
# (the default driver would show them all as the engine's own).
machine_override_content() {
  printf '%s\n' "[Service]" \
    'Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns"' \
    'Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns"'
}
machine_engine_socket() { echo "/run/user/$(passwd_field "$1" 3)/docker.sock"; }
# What systemd writes down for a user whose manager starts at boot, without anyone logging in.
machine_linger_path() { echo "${YGG_SYSROOT:-}/var/lib/systemd/linger/$1"; }
# The block of a user's .profile: the CLI talks to the user's own engine, in login shells too.
machine_profile_block() {
  printf '%s\n' "$PROFILE_BEGIN" "export DOCKER_HOST=unix://$(machine_engine_socket "$1")" "$PROFILE_END"
}

# as_user <sudo option or ""> <user> <command...>: the command as the user, in the user's systemd and
# bus (so `systemctl --user` and a rootless docker find them), from / (the admin's directory is closed to
# the service users, and a nologin user has no shell to start in). sudo, not a shell: ci, release and
# renovate can't log in.
as_user() {
  local sudo_option=$1 user=$2 uid
  shift 2
  uid=$(passwd_field "$user" 3)
  [[ -n "$uid" ]] || die "there is no $user user"
  (cd / && sudo ${sudo_option:+"$sudo_option"} -u "$user" env \
    "XDG_RUNTIME_DIR=/run/user/$uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus" "$@")
}

# run_as_user <user> <command...>: as_user, asking for a password if sudo wants one.
run_as_user() {
  local user=$1
  shift
  as_user "" "$user" "$@"
}

# read_as_user <user> <command...>: as_user for a check, which never waits for a password: it fails
# when sudo would ask.
read_as_user() {
  local user=$1
  shift
  as_user -n "$user" "$@"
}

# machine_can_read_as_users: sudo would run something for another user without asking for a password.
machine_can_read_as_users() {
  ((EUID == 0)) || { has sudo && sudo -n true 2>/dev/null; }
}

# user_file_read <user> <path>: the file's content, read as the user (homes are closed to the admin);
# nothing when it isn't there, and dies when it is there and can't be read: that must not look like an
# empty file, which would be written over.
user_file_read() {
  if run_as_user "$1" test -e "$2"; then
    run_as_user "$1" cat "$2" || die "$2 is there but can't be read as $1: it is left as it is"
  fi
}

# user_file_write <user> <path> <content>: the content plus a newline in the user's file, made by the user
# so that it is theirs.
user_file_write() {
  run_as_user "$1" mkdir -p "$(dirname "$2")"
  printf '%s\n' "$3" | run_as_user "$1" tee "$2" >/dev/null
}

# profile_with_block <user> <profile content>: the profile without any block of ours, then the block
# with the user's socket. Only a complete block (a begin mark, then its end mark with no other begin
# mark between) is taken out: a begin mark without its end is not a block, and it and everything after
# it stay as they are.
profile_with_block() {
  local stripped
  stripped=$(awk -v begin="$PROFILE_BEGIN" -v end="$PROFILE_END" '
    $0 == begin { if (inside) print held; inside = 1; held = $0; next }
    inside { held = held ORS $0; if ($0 == end) { inside = 0; held = "" }; next }
    { print }
    END { if (inside) print held }' <<<"$2")
  printf '%s%s\n' "${stripped:+$stripped$'\n\n'}" "$(machine_profile_block "$1")"
}

# machine_override_is <content>: the content is the drop-in, exactly: install writes exactly that, so a
# check that accepted more would leave a file that install rewrites looking right.
machine_override_is() { [[ "$1" == "$(machine_override_content)" ]]; }

# machine_wait_for_user_manager <user>: enabling linger starts the user's systemd, which takes a moment,
# and the engine's setup needs it (and its bus) to be there.
machine_wait_for_user_manager() {
  local tries
  for ((tries = 0; tries < 30; tries++)); do
    if run_as_user "$1" systemctl --user show-environment >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  die "the systemd user manager of $1 did not start (loginctl enable-linger $1 and journalctl -u user@$(passwd_field "$1" 3))"
}

# ---- The prune timers ---------------------------------------------------------------------------

# The user units of the weekly prune (platform/systemd), dev machine only: the same pair for ci and release,
# the users whose rootless engines keep the images and build cache of every build. Each user gets its own
# copy in its own systemd directory and its own timer, and prunes its own engine.
MACHINE_PRUNE_UNITS=(yggdrasil-prune.service yggdrasil-prune.timer)
MACHINE_PRUNE_USERS=(ci release)
machine_prune_unit_dir() { echo "$(passwd_field "$1" 6)/.config/systemd/user"; }

# ---- The backup timer ---------------------------------------------------------------------------

# The user units of the nightly backup (scripts/backup.sh), kept in the checkout and copied into ygg's
# own systemd directory, which is where its manager looks.
MACHINE_BACKUP_UNITS=(yggdrasil-backup.service yggdrasil-backup.timer)
machine_backup_unit_dir() { echo "$(passwd_field ygg 6)/.config/systemd/user"; }

# ---- The renovate timer -------------------------------------------------------------------------

# The user units of Renovate's daily run (scripts/renovate.sh), dev machine only: the same way, into the
# systemd directory of the renovate user, which runs it in its own engine.
MACHINE_RENOVATE_UNITS=(yggdrasil-renovate.service yggdrasil-renovate.timer)
machine_renovate_unit_dir() { echo "$(passwd_field renovate 6)/.config/systemd/user"; }

# ---- Firewall -----------------------------------------------------------------------------------

# Cloudflare publishes the addresses its proxies connect from, one CIDR per line: apps answers on 80 and
# 443 to those only. The rules carry a comment, so a later install knows which ones are its own.
CLOUDFLARE_URL=https://www.cloudflare.com
CLOUDFLARE_COMMENT=yggdrasil-cloudflare

# What Traefik reads: it lives in ygg's secrets directory, where ygg's containers get their files from.
machine_cloudflare_ips_path() { echo "$(secrets_dir_of ygg)/cloudflare-ips"; }

# cidr_v4 <text>: a.b.c.d/n, every octet up to 255 and n up to 32.
cidr_v4() {
  local octet
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$ ]] || return 1
  for octet in "${BASH_REMATCH[@]:1:4}"; do
    ((10#$octet <= 255)) || return 1
  done
  ((10#${BASH_REMATCH[5]} <= 32))
}

# cidr_v6 <text>: hex groups and colons (at most one "::"), then /n with n up to 128.
cidr_v6() {
  local address prefix head tail groups=() group
  [[ "$1" =~ ^([0-9a-fA-F:]+)/([0-9]{1,3})$ ]] || return 1
  address=${BASH_REMATCH[1]} prefix=${BASH_REMATCH[2]}
  ((10#$prefix <= 128)) || return 1
  [[ "$address" != *:::* && "$address" != *::*::* ]] || return 1
  # A single colon can't start or end an address: only half of a "::" can.
  [[ "$address" != :[!:]* && "$address" != *[!:]: ]] || return 1
  if [[ "$address" == *::* ]]; then
    head=${address%%::*} tail=${address#*::}
    IFS=: read -r -a groups <<<"$head${head:+${tail:+:}}$tail"
    # The "::" stands for at least one group of zeros.
    ((${#groups[@]} <= 7)) || return 1
  else
    IFS=: read -r -a groups <<<"$address"
    ((${#groups[@]} == 8)) || return 1
  fi
  for group in "${groups[@]}"; do
    [[ "$group" =~ ^[0-9a-fA-F]{1,4}$ ]] || return 1
  done
}

# cloudflare_ranges: Cloudflare's current ranges, one per line, IPv4 then IPv6. Fails, printing
# nothing, when either list can't be downloaded, holds anything but CIDRs (an error page, say) or comes
# back empty: a firewall built from half an answer would lock out real visitors or let in everyone.
cloudflare_ranges() {
  local family list line count found=()
  for family in v4 v6; do
    list=$(curl -fsSL --max-time 20 "$CLOUDFLARE_URL/ips-$family" 2>/dev/null) || return 1
    count=0
    while IFS= read -r line; do
      line=${line%$'\r'}
      [[ -n "$line" ]] || continue
      "cidr_$family" "$line" || return 1
      found+=("$line")
      count=$((count + 1))
    done <<<"$list"
    ((count > 0)) || return 1
  done
  printf '%s\n' "${found[@]}"
}

# machine_firewall_allows <role>: the "ufw allow" arguments the role always has, one per line. (The
# apps machine's web ports are the Cloudflare rules, which come from the download.)
machine_firewall_allows() {
  case $1 in
    dev) printf '%s\n' OpenSSH 80,443/tcp 2222/tcp ;;
    apps) printf '%s\n' OpenSSH ;;
  esac
}

# machine_root_read <command...>: runs it as root without ever asking for a password (a check must not
# wait for one); fails when it can't.
machine_root_read() {
  if ((EUID == 0)); then "$@"; else sudo -n "$@"; fi
}

# machine_ufw_rules <plain ufw status output, not verbose>: the allow rules, one per line, as "<to><TAB><from><TAB><comment>",
# the IPv6 twin of a rule folded into it. ufw's columns have spaces in them ("Anywhere (v6)",
# "ALLOW IN") and a trailing "# comment", so the fields are found around ALLOW.
machine_ufw_rules() {
  awk '
    {
      comment = ""
      if (match($0, / +# /)) {
        comment = substr($0, RSTART + RLENGTH)
        $0 = substr($0, 1, RSTART - 1)
      }
      for (i = 1; i <= NF; i++) if ($i == "ALLOW") break
      if (i > NF || i == 1) next
      to = $1
      for (j = 2; j < i; j++) to = to " " $j
      k = i + 1
      if ($k == "IN") k++
      from = $k
      for (k++; k <= NF; k++) from = from " " $k
      sub(/ \(v6\)$/, "", to)
      sub(/ \(v6\)$/, "", from)
      printf "%s\t%s\t%s\n", to, from, comment
    }' <<<"$1" | sort -u
}

# machine_ufw_default_incoming <ufw status verbose output>: what ufw does with incoming connections no
# rule allows (deny, reject or allow); nothing when the output doesn't say.
machine_ufw_default_incoming() {
  sed -n 's/^Default: \([a-z]*\) (incoming).*/\1/p' <<<"$1" | head -n1
}

# machine_ufw_unexpected <rules> <role>: the "to" of each rule that lets everyone in and is not one of the
# role's own (machine_firewall_allows), one per line: a port, a range or an application profile ("Nginx
# Full" is 80 and 443 under another name) that someone else opened.
machine_ufw_unexpected() {
  awk -F'\t' -v expected="$(machine_firewall_allows "$2" | paste -sd'|' -)" '
    BEGIN { n = split(expected, list, "|"); for (i = 1; i <= n; i++) own[list[i]] = 1 }
    $2 == "Anywhere" && !($1 in own) { print $1 }' <<<"$1" | sort -u
}

# machine_ufw_has <rules> <to> <from> <comment>
machine_ufw_has() { grep -qxF -- "$2"$'\t'"$3"$'\t'"$4" <<<"$1"; }

# machine_ufw_cloudflare <rules>: the ranges that the rules carrying our comment let in, one per line.
machine_ufw_cloudflare() {
  awk -F'\t' -v comment="$CLOUDFLARE_COMMENT" '$3 == comment { print $2 }' <<<"$1" | sort -u
}

# machine_ufw_open_web <rules>: the "to" of each rule that lets everyone in on port 80 or 443 (as one of
# its ports or inside its range), one per line.
machine_ufw_open_web() {
  awk -F'\t' '
    $2 == "Anywhere" && $1 !~ /\/udp$/ {
      spec = $1
      sub(/\/tcp$/, "", spec)
      n = split(spec, parts, ",")
      for (i = 1; i <= n; i++) {
        if (parts[i] ~ /^[0-9]+$/) { low = high = parts[i] + 0 }
        else if (parts[i] ~ /^[0-9]+:[0-9]+$/) { split(parts[i], bounds, ":"); low = bounds[1] + 0; high = bounds[2] + 0 }
        else continue
        if ((low <= 80 && 80 <= high) || (low <= 443 && 443 <= high)) { print $1; break }
      }
    }' <<<"$1" | sort -u
}

# machine_plain_web <to>: the rule is just 80 and/or 443 (what the workstation's firewall opens), which
# install can delete without taking anything else with it.
machine_plain_web() { [[ "$1" =~ ^(80|443)(,(80|443))?(/tcp)?$ ]]; }

# machine_only_in_second <lines a> <lines b>: the lines of b that a doesn't have.
machine_only_in_second() {
  comm -13 <(sort -u <<<"$1" | sed '/^$/d') <(sort -u <<<"$2" | sed '/^$/d')
}

# machine_some: the lines on stdin as "a, b, c and N more", for a report row.
machine_some() {
  local items
  mapfile -t items
  printf '%s' "$(printf '%s, ' "${items[@]:0:3}" | sed 's/, $//')"
  if ((${#items[@]} > 3)); then printf ' and %d more' $((${#items[@]} - 3)); fi
}

# ---- Check --------------------------------------------------------------------------------------

# machine_need <step>: adds the step to machine_todo once.
machine_need() {
  local step
  for step in "${machine_todo[@]}"; do [[ "$step" == "$1" ]] && return 0; done
  machine_todo+=("$1")
}

# machine_needs <step>: true when the step is in machine_todo.
machine_needs() {
  local step
  for step in "${machine_todo[@]}"; do [[ "$step" == "$1" ]] && return 0; done
  return 1
}

# check_machine <role> [quiet]: fills in machine_todo; prints the report unless quiet.
check_machine() {
  machine_todo=() machine_missing=()
  machine_current_role=$1
  if [[ -n "${2:-}" ]]; then
    machine_report >/dev/null
  else
    machine_report
  fi
}

machine_report() {
  title "System"
  if ! machine_ubuntu_known; then
    row warn "Ubuntu" "$(os_field PRETTY_NAME || echo unknown): install knows $(machine_ubuntu_text) only, check works anywhere"
  fi
  local step
  for step in "${MACHINE_STEPS[@]}"; do
    "machine_check_${step//-/_}"
  done
}

machine_check_packages() {
  local package conflicting=() name
  for package in "${MACHINE_PACKAGES[@]}"; do
    installed "$package" || machine_missing+=("$package")
  done
  # The distribution's docker.io and friends clash with Docker's packages (CONFLICTING is host.sh's).
  # shellcheck disable=SC2153
  for name in "${CONFLICTING[@]}"; do installed "$name" && conflicting+=("$name"); done
  if ((${#conflicting[@]})); then
    row warn "packages" "the distribution's (${conflicting[*]}): install replaces them with Docker's${machine_missing[*]:+; missing: ${machine_missing[*]}}"
    machine_need packages
  elif ((${#machine_missing[@]})); then
    row missing "packages" "${machine_missing[*]}"
    machine_need packages
  else
    row ok "packages" "${#MACHINE_PACKAGES[@]} installed"
  fi
}

machine_check_rootful_docker_off() {
  local unit state running=() enabled=() sock="${YGG_SYSROOT:-}/var/run/docker.sock" members=""
  for unit in "${ROOTFUL_DOCKER_UNITS[@]}"; do
    # Off is inactive now (not failed, reloading or deactivating either) and not coming back at boot.
    state=$(systemctl is-active "$unit" 2>/dev/null || true)
    [[ "$state" == inactive ]] || running+=("$unit${state:+ ($state)}")
    state=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    case $state in
      "" | disabled | masked | static | not-found) ;;
      *) enabled+=("$unit ($state)") ;;
    esac
  done
  [[ -e "$sock" ]] && running+=("$sock")
  if ((${#running[@]} || ${#enabled[@]})); then
    row warn "rootful docker" "${running[*]:+running: ${running[*]}; }${enabled[*]:+enabled: ${enabled[*]}; }install turns it off (the service users' engines are rootless)"
    machine_need rootful-docker-off
  else
    row ok "rootful docker" "off, and off after a reboot"
  fi

  members=$(getent group docker 2>/dev/null | cut -d: -f4 | tr ',' ' ') || members=""
  members=${members% }
  if [[ -n "${members// /}" ]]; then
    row missing "docker group" "members: $members (docker group access is root access): install removes them"
    machine_need rootful-docker-off
  else
    row ok "docker group" "no members"
  fi
}

# machine_check_file <row name> <path> <content> <step>: one row for a system file.
machine_check_file() {
  if file_holds "$2" "$3"; then
    row ok "$1" "$2"
  elif [[ -f "$2" ]]; then
    row warn "$1" "$2 differs: install rewrites it"
    machine_need "$4"
  else
    row missing "$1" "$2"
    machine_need "$4"
  fi
}

machine_check_system_settings() {
  machine_check_file "unprivileged ports" "$(machine_sysctl_path)" "$(machine_sysctl_content)" system-settings
  machine_check_file "apparmor rootlesskit" "$(machine_apparmor_path)" "$(machine_apparmor_content)" system-settings
  machine_check_file "cgroup delegation" "$(machine_delegate_path)" "$(machine_delegate_content)" system-settings
}

machine_check_unattended_upgrades() {
  machine_check_file "unattended upgrades" "$(machine_upgrades_path)" "$(machine_upgrades_content)" unattended-upgrades
}

# user_privilege_problem <user>: how the user has root's powers (the sudo or docker group, sudo rules),
# nothing when it has none. A service user must not: ci runs other people's code.
user_privilege_problem() {
  local user=$1 group out reasons=""
  for group in $(id -nG "$user" 2>/dev/null || true); do
    case $group in
      sudo | docker) reasons+="${reasons:+, }in the $group group" ;;
    esac
  done
  if has sudo; then
    # -n: a check never waits for a password, and what it can't read it doesn't count.
    out=$(sudo -n -l -U "$user" 2>&1) || true
    case $out in
      *"may run the following"*) reasons+="${reasons:+, }has sudo rules" ;;
    esac
  fi
  echo "$reasons"
}

machine_check_users() {
  title "Users"
  local users user want shell home mode problems=() fixable problem
  mapfile -t users < <(role_users "$machine_current_role")
  for user in "${users[@]}"; do
    # The admin is never managed by install, whatever it is called.
    if [[ "$user" == "$(machine_admin)" ]]; then
      row ok "user $user" "is the admin (you): install leaves it alone"
      continue
    fi
    want=$(machine_user_shell "$user")
    if ! user_exists "$user"; then
      row missing "user $user" "not there: install creates it (shell $want)"
      machine_need users
      continue
    fi
    shell=$(passwd_field "$user" 7)
    home=$(passwd_field "$user" 6)
    mode=$(stat -c %a "$home" 2>/dev/null) || mode=missing
    problems=()
    [[ "$shell" == "$want" ]] || problems+=("shell is $shell, not $want")
    [[ "$mode" == 750 ]] || problems+=("home $home is $mode, not 750")
    # Install puts the shell and the home right; sudo and docker are the admin's to take away.
    fixable=${#problems[@]}
    problem=$(user_privilege_problem "$user")
    [[ -z "$problem" ]] || problems+=("$problem: install leaves that to you")
    ((fixable == 0)) || machine_need users
    if ((${#problems[@]})); then
      row warn "user $user" "$(printf '%s; ' "${problems[@]}" | sed 's/; $//')"
    else
      row ok "user $user" "shell $shell, home $mode, no sudo"
    fi
  done
}

machine_check_ci_limits() {
  [[ "$machine_current_role" == dev ]] || return 0
  local path
  path=$(machine_ci_limits_path)
  if [[ -z "$path" ]]; then
    row missing "ci limits" "no ci user yet: install caps it at six CPUs and 20G once it exists"
    machine_need ci-limits
  else
    machine_check_file "ci limits" "$path" "$(machine_ci_limits_content)" ci-limits
  fi
}

machine_check_low_ports() {
  [[ "$machine_current_role" == dev ]] || return 0
  local user path
  for user in "${MACHINE_LOW_PORT_USERS[@]}"; do
    path=$(machine_low_ports_path "$user")
    if [[ -z "$path" ]]; then
      row missing "low ports $user" "no $user user yet: install keeps it off ports below 1024 once it exists"
      machine_need low-ports
    else
      machine_check_file "low ports $user" "$path" "$(machine_low_ports_content)" low-ports
    fi
  done
}

machine_check_ssh() {
  local path legacy keys effective conflict
  path=$(machine_ssh_path)
  legacy=$(machine_ssh_legacy_path)
  keys=$(machine_admin_keys)
  if [[ -e "$legacy" ]]; then
    row warn "ssh" "$legacy is from an earlier install: install replaces it with $path"
    machine_need ssh
  elif file_holds "$path" "$(machine_ssh_content)"; then
    # The file being right isn't enough: another file may set a value before it.
    if effective=$(machine_ssh_effective); then
      conflict=$(machine_ssh_conflict "$effective")
      if [[ -z "$conflict" ]]; then
        row ok "ssh" "keys only, no root login, only the role's users ($path)"
      else
        row warn "ssh" "$path is there but sshd runs with $conflict: another setting comes first"
        machine_need ssh
      fi
    else
      row ok "ssh" "$path (what sshd runs with needs sudo to read: not checked)"
    fi
  elif [[ ! -s "$keys" ]]; then
    # Install refuses rather than lock the admin out: passwords would be switched off.
    row warn "ssh" "$keys is missing or empty: install refuses to harden ssh until your key is in it"
    machine_need ssh
  elif [[ -f "$path" ]]; then
    row warn "ssh" "$path differs: install rewrites it"
    machine_need ssh
  else
    row missing "ssh" "$path: install makes it keys only, no root login"
    machine_need ssh
  fi
}

machine_check_checkout() {
  local owner
  owner=$(stat -c %U:%G "$root" 2>/dev/null) || owner=unknown
  if ! machine_checkout_at_home; then
    row warn "checkout owner" "$root is not $(machine_checkout_path): install refuses to run from it (clone yggdrasil there)"
  elif [[ "$owner" == ygg:ygg ]]; then
    row ok "checkout owner" "$owner ($root)"
  else
    row warn "checkout owner" "$owner ($root): install gives it to ygg"
    machine_need checkout
  fi
}

machine_check_secrets() {
  local users user dir owner mode
  mapfile -t users < <(role_users "$machine_current_role")
  for user in "${users[@]}"; do
    dir=$(secrets_dir_of "$user")
    if [[ ! -d "$dir" ]]; then
      row missing "secrets $user" "$dir: install creates it ($user:$user 750)"
      machine_need secrets
      continue
    fi
    # The directory is closed to the admin, so what is in it is the step's business, not a check's.
    owner=$(stat -c %U:%G "$dir")
    mode=$(stat -c %a "$dir")
    if [[ "$owner" == "$user:$user" && "$mode" == 750 ]]; then
      row ok "secrets $user" "$dir ($owner $mode)"
    else
      row warn "secrets $user" "$dir is $owner $mode, not $user:$user 750: install puts it right"
      machine_need secrets
    fi
  done
}

machine_check_firewall() {
  title "Firewall"
  local status listing rules spec incoming open_plain=() others=() missing=()
  if ! has ufw; then
    row missing "ufw" "not installed: install adds it, then sets the firewall"
    machine_need firewall
    return 0
  fi
  # Two reads. The default policy is only in `status verbose`; the rules come from plain `status`,
  # because verbose shows an application profile's rule by its ports ("22/tcp (OpenSSH)", and
  # "22/tcp (OpenSSH (v6))" for its twin) rather than the name `ufw allow OpenSSH` gave it, the name
  # step_firewall reads and writes.
  if ! status=$(machine_root_read ufw status verbose 2>/dev/null) \
    || ! listing=$(machine_root_read ufw status 2>/dev/null); then
    row warn "ufw" "its status needs root to read: not checked (run check with sudo)"
    return 0
  fi
  if [[ "$status" != *"Status: active"* ]]; then
    row missing "ufw" "not enabled: install allows ssh, denies the rest and turns it on"
    machine_need firewall
    return 0
  fi
  incoming=$(machine_ufw_default_incoming "$status")
  # The rules below only mean something if what they don't allow is refused.
  case $incoming in
    deny | reject) row ok "ufw" "enabled; incoming that no rule allows is refused (default: $incoming)" ;;
    *)
      row warn "ufw" "enabled, but incoming is ${incoming:-unknown} by default: every port is open; install sets deny"
      machine_need firewall
      ;;
  esac
  rules=$(machine_ufw_rules "$listing")

  for spec in $(machine_firewall_allows "$machine_current_role"); do
    machine_ufw_has "$rules" "$spec" Anywhere "" || missing+=("$spec")
  done
  # A rule letting everyone in that install didn't make is reported. On the apps machine the plain 80
  # and 443 ones (what the workstation's firewall opens) are install's to delete: those ports are for
  # Cloudflare alone. The rest (a profile, a range, another port) are the admin's to look at.
  while IFS= read -r spec; do
    [[ -n "$spec" ]] || continue
    if [[ "$machine_current_role" == apps ]] && machine_plain_web "$spec"; then
      open_plain+=("$spec")
    else
      others+=("$spec")
    fi
  done < <(machine_ufw_unexpected "$rules" "$machine_current_role")
  if ((${#missing[@]})); then
    row missing "ports" "not allowed: ${missing[*]}"
    machine_need firewall
  elif ((${#open_plain[@]})); then
    row warn "ports" "80/443 are open to everyone ($(machine_list "${open_plain[@]}" ${others[@]+"${others[@]}"})): install closes $(machine_list "${open_plain[@]}"), Cloudflare gets in"
    machine_need firewall
  elif ((${#others[@]})); then
    row warn "ports" "open to everyone, and not by install: $(machine_list "${others[@]}"). Install leaves them: if they shouldn't be, sudo ufw delete allow '<rule>'"
  elif [[ "$machine_current_role" == apps ]]; then
    row ok "ports" "ssh; 80 and 443 only from Cloudflare"
  else
    row ok "ports" "22, 80, 443 and 2222"
  fi

  [[ "$machine_current_role" != apps ]] || machine_check_cloudflare_ranges "$rules"
}

# machine_list <item...>: the items as "a, b, c", for a report row.
machine_list() {
  local joined
  joined=$(printf '%s, ' "$@")
  echo "${joined%, }"
}

# machine_check_cloudflare_ranges <rules>: the apps machine's rules and its cloudflare-ips file against
# what Cloudflare publishes today.
machine_check_cloudflare_ranges() {
  local current path held in_ufw added stale differences=()
  path=$(machine_cloudflare_ips_path)
  if ! current=$(cloudflare_ranges); then
    row warn "cloudflare ranges" "could not check: Cloudflare's list can't be fetched"
    return 0
  fi
  in_ufw=$(machine_ufw_cloudflare "$1")
  added=$(machine_only_in_second "$in_ufw" "$current")
  stale=$(machine_only_in_second "$current" "$in_ufw")
  [[ -z "$added" ]] || differences+=("ufw lacks $(machine_some <<<"$added")")
  [[ -z "$stale" ]] || differences+=("ufw still has $(machine_some <<<"$stale")")
  # The secrets directory is closed to the admin, so the file is read as root.
  if ! held=$(machine_root_read cat "$path" 2>/dev/null); then
    differences+=("$path can't be read or isn't there")
  elif [[ "$held" != "$current" ]]; then
    differences+=("$path differs")
  fi
  if ((${#differences[@]})); then
    row warn "cloudflare ranges" "$(printf '%s; ' "${differences[@]}" | sed 's/; $//'): install updates them"
    machine_need firewall
  else
    row ok "cloudflare ranges" "$(wc -l <<<"$current") ranges, in ufw and in $path"
  fi
}

machine_check_engines() {
  title "Rootless Docker"
  local users user
  mapfile -t users < <(role_users "$machine_current_role")
  for user in "${users[@]}"; do
    machine_check_engine "$user"
  done
  [[ "$machine_current_role" != dev ]] || machine_check_isolation
}

# machine_check_engine <user>: the three rows of one service user's engine. The engine and its drop-in
# are asked of the user itself (the home is closed to the admin), through sudo without a password.
machine_check_engine() {
  local user=$1 uid security override held
  uid=$(passwd_field "$user" 3)
  if [[ -z "$uid" ]]; then
    row missing "engine $user" "no $user user yet: install creates it, then its engine"
    machine_need engines
    return 0
  fi
  override=$(machine_override_path "$user")

  if ! machine_can_read_as_users; then
    row warn "engine $user" "asking it needs sudo without a password: not checked (run check with sudo)"
  # DOCKER_HOST: the user's own engine is asked, whichever engine the docker CLI would pick.
  elif security=$(read_as_user "$user" env "DOCKER_HOST=unix://$(machine_engine_socket "$user")" docker info --format '{{json .SecurityOptions}}' 2>/dev/null); then
    if [[ "$security" == *name=rootless* ]]; then
      row ok "engine $user" "rootless, on $(machine_engine_socket "$user")"
    else
      row warn "engine $user" "answers, but not rootless ($security): the platform runs on rootless Docker only"
      machine_need engines
    fi
  else
    row missing "engine $user" "does not answer on $(machine_engine_socket "$user"): install sets it up"
    machine_need engines
  fi

  if [[ -e "$(machine_linger_path "$user")" ]]; then
    row ok "linger $user" "on: the engine starts at boot, with nobody logged in"
  else
    row missing "linger $user" "off: the engine would stop with the user's last session"
    machine_need engines
  fi

  if ! machine_can_read_as_users; then
    row warn "client addresses $user" "the home is closed to you and sudo would ask for a password: not checked"
  elif ! held=$(read_as_user "$user" cat "$override" 2>/dev/null); then
    row missing "client addresses $user" "$override: install writes it (slirp4netns, so containers see their clients' addresses)"
    machine_need engines
  elif machine_override_is "$held"; then
    row ok "client addresses $user" "slirp4netns network and port driver ($override)"
  else
    row warn "client addresses $user" "$override is not the slirp4netns settings alone: install rewrites it"
    machine_need engines
  fi
}

# machine_check_isolation: no other service user may reach what ygg has: its engine (that is root over
# the platform) and its secrets. And ci, which runs other people's code, may not reach the release
# agent's engine or secrets either (the signing keys come with later plans). What a check finds here,
# install can't repair: it is for the admin to look at.
machine_check_isolation() {
  user_exists ygg || return 0
  local user
  for user in ci release dev renovate; do
    machine_check_cannot_read "$user → ygg engine" "$user" "$(machine_engine_socket ygg)"
  done
  machine_check_cannot_read "ci → release engine" ci "$(machine_engine_socket release)"
  for user in ci release dev renovate; do
    machine_check_cannot_read "$user → ygg secrets" "$user" "$(secrets_dir_of ygg)/platform.env"
  done
  machine_check_cannot_read "ci → release secrets" ci "$(secrets_dir_of release)/agent.env"
}

# machine_check_cannot_read <row name> <user> <path>: ok only when the user's failing to read the path
# means something. A refusal of sudo, or a path that isn't there, would fail the same way, so the row
# first proves that commands do run as the user (`true`) and that the path exists (asked of root), and
# otherwise says it did not check.
machine_check_cannot_read() {
  local name=$1 user=$2 path=$3
  if ! user_exists "$user"; then
    row missing "$name" "no $user user yet"
  elif ! machine_can_read_as_users; then
    row warn "$name" "asking needs sudo without a password: not checked (run check with sudo)"
  elif ! read_as_user "$user" true 2>/dev/null; then
    row warn "$name" "sudo does not run commands as $user: not checked"
  elif ! machine_root_read test -e "$path" 2>/dev/null; then
    row warn "$name" "$path is not there (its engine is not running, or the file is missing): not checked"
  elif read_as_user "$user" test -r "$path"; then
    row warn "$name" "$user can read $path: that breaks the isolation of the users (look at the permissions on it and on the run directory)"
  else
    row ok "$name" "$user can't read $path"
  fi
}

# machine_check_prune_timer: dev only. For ci and for release: its two prune units are the checkout's
# copies, and its timer is enabled; asked of the user itself (its home is closed to the admin), through sudo
# without a password.
machine_check_prune_timer() {
  [[ "$machine_current_role" == dev ]] || return 0
  title "CI engine prunes"
  local user dir unit held stale unit_state
  for user in "${MACHINE_PRUNE_USERS[@]}"; do
    if ! user_exists "$user"; then
      row missing "prune timer $user" "no $user user yet: install creates it, then the weekly prune of its engine"
      machine_need prune-timer
      continue
    fi
    if ! machine_can_read_as_users; then
      row warn "prune timer $user" "the home of $user is closed to you and sudo would ask for a password: not checked (run check with sudo)"
      continue
    fi
    dir=$(machine_prune_unit_dir "$user")
    stale=()
    for unit in "${MACHINE_PRUNE_UNITS[@]}"; do
      if ! held=$(read_as_user "$user" cat "$dir/$unit" 2>/dev/null) || [[ "$held" != "$(<"$root/platform/systemd/$unit")" ]]; then
        stale+=("$unit")
      fi
    done
    if ((${#stale[@]})); then
      row missing "prune timer $user" "$dir: ${stale[*]} not there or not the checkout's copy: install writes them"
      machine_need prune-timer
    elif ! unit_state=$(read_as_user "$user" systemctl --user is-enabled yggdrasil-prune.timer 2>/dev/null); then
      row missing "prune timer $user" "yggdrasil-prune.timer is ${unit_state:-not enabled}: install enables it (Sundays at 03:00)"
      machine_need prune-timer
    else
      row ok "prune timer $user" "yggdrasil-prune.timer enabled for $user, Sundays at 03:00 ($dir)"
    fi
  done
}

# machine_check_backup_timer: ygg's two units are the checkout's copies, and its timer is enabled. Both
# are asked of ygg itself (its home is closed to the admin), through sudo without a password.
machine_check_backup_timer() {
  title "Backups"
  local dir unit held stale=() unit_state
  if ! user_exists ygg; then
    row missing "backup timer" "no ygg user yet: install creates it, then the timer of its nightly backup"
    machine_need backup-timer
    return 0
  fi
  if ! machine_can_read_as_users; then
    row warn "backup timer" "the home of ygg is closed to you and sudo would ask for a password: not checked (run check with sudo)"
    for unit in backup.conf "last backup" "newest backup"; do
      row warn "$unit" "asking ygg needs sudo without a password: not checked"
    done
    return 0
  fi
  dir=$(machine_backup_unit_dir)
  for unit in "${MACHINE_BACKUP_UNITS[@]}"; do
    if ! held=$(read_as_user ygg cat "$dir/$unit" 2>/dev/null) || [[ "$held" != "$(<"$root/platform/systemd/$unit")" ]]; then
      stale+=("$unit")
    fi
  done
  if ((${#stale[@]})); then
    row missing "backup timer" "$dir: ${stale[*]} not there or not the checkout's copy: install writes them"
    machine_need backup-timer
  elif ! unit_state=$(read_as_user ygg systemctl --user is-enabled yggdrasil-backup.timer 2>/dev/null); then
    row missing "backup timer" "yggdrasil-backup.timer is ${unit_state:-not enabled}: install enables it (every night at 03:30)"
    machine_need backup-timer
  else
    row ok "backup timer" "yggdrasil-backup.timer enabled for ygg, every night at 03:30 ($dir)"
  fi
  machine_check_backup_health
}

# machine_check_renovate_timer: dev only. renovate's two units are the checkout's copies, and its timer is
# enabled; asked of renovate itself (its home is closed to the admin), through sudo without a password.
machine_check_renovate_timer() {
  [[ "$machine_current_role" == dev ]] || return 0
  title "Renovate"
  local dir unit held stale=() unit_state
  if ! user_exists renovate; then
    row missing "renovate timer" "no renovate user yet: install creates it, then the timer of its daily run"
    machine_need renovate-timer
    return 0
  fi
  if ! machine_can_read_as_users; then
    row warn "renovate timer" "the home of renovate is closed to you and sudo would ask for a password: not checked (run check with sudo)"
    for unit in renovate.env "renovate github token" "last renovate run"; do
      row warn "$unit" "asking renovate needs sudo without a password: not checked"
    done
    return 0
  fi
  dir=$(machine_renovate_unit_dir)
  for unit in "${MACHINE_RENOVATE_UNITS[@]}"; do
    if ! held=$(read_as_user renovate cat "$dir/$unit" 2>/dev/null) || [[ "$held" != "$(<"$root/platform/systemd/$unit")" ]]; then
      stale+=("$unit")
    fi
  done
  if ((${#stale[@]})); then
    row missing "renovate timer" "$dir: ${stale[*]} not there or not the checkout's copy: install writes them"
    machine_need renovate-timer
  elif ! unit_state=$(read_as_user renovate systemctl --user is-enabled yggdrasil-renovate.timer 2>/dev/null); then
    row missing "renovate timer" "yggdrasil-renovate.timer is ${unit_state:-not enabled}: install enables it (every day at 05:00)"
    machine_need renovate-timer
  else
    row ok "renovate timer" "yggdrasil-renovate.timer enabled for renovate, every day at 05:00 ($dir)"
  fi
  machine_check_renovate_health
}

# machine_check_renovate_health: whether Renovate can run and did, which the units being there doesn't say:
# renovate.env has a token, and the last run succeeded. Warnings for the owner; none is install's to repair.
machine_check_renovate_health() {
  local env_file shown result="" started="" key value
  env_file="$(secrets_dir_of renovate)/renovate.env"
  if ! read_as_user renovate test -f "$env_file" 2>/dev/null; then
    row warn "renovate.env" "$env_file is not there: copy env/renovate.env.example and fill it in, as renovate (docs/hosts.md): every daily run fails until then"
  elif read_as_user renovate grep -q '^RENOVATE_TOKEN=.' "$env_file" 2>/dev/null; then
    row ok "renovate.env" "$env_file, with a token"
  else
    row warn "renovate.env" "RENOVATE_TOKEN is empty in $env_file: put the renovate user's Forgejo token there (docs/hosts.md): every daily run fails until then"
  fi

  # Optional: Renovate works without it, but fetching the release notes and changelogs of the dependencies
  # hosted on GitHub from one shared address runs into GitHub's anonymous rate limit. Only whether it is set is
  # asked of the file (grep -q): the value is never read here, let alone printed.
  # (A file that is not there has its row above.)
  if read_as_user renovate test -f "$env_file" 2>/dev/null; then
    if read_as_user renovate grep -q '^RENOVATE_GITHUB_COM_TOKEN=.' "$env_file" 2>/dev/null; then
      row ok "renovate github token" "RENOVATE_GITHUB_COM_TOKEN is set in $env_file"
    else
      row warn "renovate github token" "RENOVATE_GITHUB_COM_TOKEN is empty in $env_file: release notes and changelogs from GitHub will be rate-limited (optional; a fine-grained, read-only, public-repositories token: docs/hosts.md)"
    fi
  fi

  if shown=$(read_as_user renovate systemctl --user show yggdrasil-renovate.service -p Result -p ExecMainStartTimestamp 2>/dev/null); then
    while IFS='=' read -r key value; do
      case $key in
        Result) result=$value ;;
        ExecMainStartTimestamp) started=$value ;;
      esac
    done <<<"$shown"
    if [[ -z "$started" ]]; then
      row warn "last renovate run" "never run yet: the timer runs it at 05:00, or 'scripts/renovate.sh run' as renovate now"
    elif [[ "$result" == success ]]; then
      row ok "last renovate run" "succeeded, started $started"
    else
      row warn "last renovate run" "failed (Result=${result:-unknown}), started $started: journalctl --user -u yggdrasil-renovate, as renovate"
    fi
  else
    row warn "last renovate run" "systemctl --user show yggdrasil-renovate.service failed as renovate: not checked"
  fi
}

# Backups older than this are a warning: the timer runs every night, so a day and a half without one is
# a night missed.
MACHINE_BACKUP_MAX_AGE_HOURS=36

# machine_check_backup_health: whether the backups actually happen, which the units being there doesn't
# say: backup.conf is there, the last run succeeded, and the newest day in ~ygg/backups is recent. Asked
# of ygg (its secrets directory and home are closed to the admin), through sudo without a password.
# None of it is install's to repair: they are warnings for the owner.
machine_check_backup_health() {
  local conf shown result="" started="" key value home newest modified age_hours
  conf="$(secrets_dir_of ygg)/backup.conf"
  if read_as_user ygg test -f "$conf" 2>/dev/null; then
    row ok "backup.conf" "$conf"
  else
    row warn "backup.conf" "$conf is not there: copy env/backup.conf.example and fill it in, as ygg (docs/hosts.md): every nightly run fails until then"
  fi

  if shown=$(read_as_user ygg systemctl --user show yggdrasil-backup.service -p Result -p ExecMainStartTimestamp 2>/dev/null); then
    while IFS='=' read -r key value; do
      case $key in
        Result) result=$value ;;
        ExecMainStartTimestamp) started=$value ;;
      esac
    done <<<"$shown"
    if [[ -z "$started" || "$started" == n/a ]]; then
      row warn "last backup" "never run yet: the timer runs it at 03:30, or 'ygg backup run' as ygg now"
    elif [[ "$result" == success ]]; then
      row ok "last backup" "succeeded, started $started"
    else
      row warn "last backup" "failed (Result=${result:-unknown}), started $started: journalctl --user -u yggdrasil-backup, as ygg"
    fi
  else
    row warn "last backup" "systemctl --user show yggdrasil-backup.service failed as ygg: not checked"
  fi

  home=$(passwd_field ygg 6)
  # The days are named by date, so the last one by name is the newest; its age is its directory's
  # modification time (the dumps are written into it).
  newest=$(read_as_user ygg find "$home/backups" -mindepth 1 -maxdepth 1 -type d \
    -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' -printf '%f %T@\n' 2>/dev/null | sort | tail -n1) || newest=""
  if [[ -z "$newest" ]]; then
    row warn "newest backup" "none in $home/backups yet"
  else
    # find's %T@ is seconds with a fraction: the whole seconds are enough.
    modified=${newest#* }
    age_hours=$((($(date +%s) - ${modified%%.*}) / 3600))
    if ((age_hours > MACHINE_BACKUP_MAX_AGE_HOURS)); then
      row warn "newest backup" "${newest%% *}, $age_hours hours old: nothing newer in $MACHINE_BACKUP_MAX_AGE_HOURS hours (see the last backup row)"
    else
      row ok "newest backup" "${newest%% *}, $age_hours hours old ($home/backups)"
    fi
  fi
}

machine_check_role_file() {
  title "Role"
  local file="${YGG_SYSROOT:-}/etc/yggdrasil-role" written=""
  if [[ -r "$file" ]]; then
    read -r written _ <"$file" || true
  fi
  if [[ "$written" == "$machine_current_role" ]]; then
    row ok "role" "$written ($file)"
  elif [[ -n "$written" ]]; then
    row warn "role" "$file says $written: install writes $machine_current_role"
    machine_need role-file
  else
    row missing "role" "$file: install writes $machine_current_role"
    machine_need role-file
  fi
}

# ---- Install ------------------------------------------------------------------------------------

# machine_step_text <step>: what the step is about to do, said before asking.
machine_step_text() {
  case $1 in
    packages)
      local packages_text=${machine_missing[*]:-}
      [[ -n "$packages_text" ]] || packages_text="the distribution's Docker packages are replaced by Docker's"
      echo "Packages: $packages_text, with Docker's from its apt repository (download.docker.com)."
      ;;
    rootful-docker-off) echo "Turn off the machine's own Docker (docker.service, docker.socket, containerd.service) and empty the docker group." ;;
    system-settings) echo "Unprivileged ports from 80, an AppArmor profile for rootlesskit and cgroup delegation to users' units." ;;
    unattended-upgrades) echo "Install security updates by themselves (${YGG_SYSROOT:-}/etc/apt/apt.conf.d/20auto-upgrades)." ;;
    users) echo "Create the service users of the $machine_current_role role ($(role_users "$machine_current_role" | paste -sd' ' -)) without sudo or docker access, and close their home directories (750)." ;;
    ci-limits) echo "Cap the ci user at six CPUs and 20G of memory." ;;
    low-ports) echo "Keep ${MACHINE_LOW_PORT_USERS[*]} (and their containers) off every port below 1024, so none of them can take 80 or 443 from ygg's Traefik." ;;
    ssh) echo "Harden ssh (00-yggdrasil.conf, ahead of the cloud image's own files): keys only, no root login, and only $(machine_admin) and the role's ssh users may log in. Your key is offered to the ones without one." ;;
    checkout) echo "Give the checkout, $root, to the ygg user." ;;
    secrets) echo "Create the users' secrets directories (750, each its user's) with the env templates copied in, never over a file that is there." ;;
    firewall)
      if [[ "$machine_current_role" == apps ]]; then
        echo "Firewall: ssh in, and 80 and 443 only from Cloudflare's ranges (downloaded from www.cloudflare.com, kept in $(machine_cloudflare_ips_path) for Traefik); a rule opening them to everyone is deleted. Everything else incoming is denied, then ufw is turned on."
      else
        echo "Firewall: ssh, HTTP, HTTPS and Forgejo's ssh port (22, 80, 443, 2222) in, everything else incoming denied, then ufw is turned on."
      fi
      ;;
    engines) echo "Give each service user ($(role_users "$machine_current_role" | paste -sd' ' -)) its own rootless Docker engine: linger on, slirp4netns network and port driver, started now and at boot, and DOCKER_HOST in its .profile." ;;
    prune-timer) echo "Install the weekly Docker prune for ${MACHINE_PRUNE_USERS[*]} (dev machine): the units yggdrasil-prune.service and .timer into each one's ~/.config/systemd/user (as that user), and enable the timer (Sundays at 03:00). Each prunes its own engine of what has not been used for a week." ;;
    backup-timer) echo "Install the nightly backup for ygg: the units yggdrasil-backup.service and .timer into $(machine_backup_unit_dir) (as ygg), and enable the timer (03:30). What is backed up, and where to, is in $(secrets_dir_of ygg)/backup.conf (env/backup.conf.example)." ;;
    renovate-timer) echo "Install the daily Renovate run for renovate (dev machine): the units yggdrasil-renovate.service and .timer into $(machine_renovate_unit_dir) (as renovate), and enable the timer (05:00). Its Forgejo token goes in $(secrets_dir_of renovate)/renovate.env (env/renovate.env.example)." ;;
    role-file) echo "Write the role, $machine_current_role, to ${YGG_SYSROOT:-}/etc/yggdrasil-role." ;;
  esac
}

# install_machine <role>: runs the steps in machine_todo (check_machine fills it), in the order of
# MACHINE_STEPS, asking before each. A step can change what the later ones find (installing Docker
# starts the engine the next step turns off), so the machine is checked again after each one.
install_machine() {
  local step did=""
  machine_current_role=$1
  # Refused before anything is changed: dying at the ssh step would leave the machine half set up.
  ! machine_needs ssh || machine_ssh_keys_refusal
  for step in "${MACHINE_STEPS[@]}"; do
    machine_needs "$step" || continue
    say "$(machine_step_text "$step")"
    confirm "Do this?" y || continue
    # The role goes down before the first change, not last: an install that dies halfway would otherwise
    # leave a machine whose bare `ygg install` falls back to workstation (rootful Docker, the docker
    # group, 80 and 443 open to everyone). The role-file step stays for check and repair.
    [[ -n "$did" ]] || step_role_file
    "step_${step//-/_}"
    did=1
    check_machine "$1" quiet
  done
  [[ -n "$did" ]] || say "Nothing installed."
}

step_packages() {
  # Declining leaves things as they are; a failing apt-get dies in remove_conflicting.
  remove_conflicting || {
    warn "Docker's packages can't be installed next to the distribution's: left as they are."
    return 0
  }
  # Removing the clashing packages can take Docker's with them, so what is missing is counted now.
  local package needs_docker=""
  machine_missing=()
  for package in "${MACHINE_PACKAGES[@]}"; do
    installed "$package" && continue
    machine_missing+=("$package")
    [[ " ${MACHINE_DOCKER_PACKAGES[*]} " == *" $package "* ]] && needs_docker=1
  done
  if [[ -n "$needs_docker" ]]; then
    add_docker_repository
  else
    apt_get update
  fi
  if ((${#machine_missing[@]})); then
    apt_get install -y "${machine_missing[@]}"
  fi
}

# The service users' engines are rootless; the rootful one that the packages start would answer on
# /var/run/docker.sock to anyone in the docker group, which is root.
step_rootful_docker_off() {
  as_root systemctl disable --now "${ROOTFUL_DOCKER_UNITS[@]}"
  # Masked, a docker-ce upgrade can't enable or start them again.
  as_root systemctl mask "${ROOTFUL_DOCKER_UNITS[@]}"
  local group_members=() member
  IFS=, read -r -a group_members < <(getent group docker 2>/dev/null | cut -d: -f4 || true) || true
  for member in "${group_members[@]}"; do
    as_root gpasswd -d "$member" docker
  done
}

step_system_settings() {
  write_system_file "$(machine_sysctl_path)" 0644 "$(machine_sysctl_content)"
  write_system_file "$(machine_apparmor_path)" 0644 "$(machine_apparmor_content)"
  write_system_file "$(machine_delegate_path)" 0644 "$(machine_delegate_content)"
  as_root sysctl --system >/dev/null
  as_root systemctl restart apparmor.service
  as_root systemctl daemon-reload
}

step_unattended_upgrades() {
  write_system_file "$(machine_upgrades_path)" 0644 "$(machine_upgrades_content)"
}

# The service users get no sudo and no docker group: nothing here adds anyone to any group. Their homes
# are closed to everyone else.
step_users() {
  local users user shell home
  mapfile -t users < <(role_users "$machine_current_role")
  for user in "${users[@]}"; do
    # The admin is never managed by install, whatever it is called.
    [[ "$user" != "$(machine_admin)" ]] || continue
    shell=$(machine_user_shell "$user")
    if ! user_exists "$user"; then
      as_root useradd --create-home --user-group --shell "$shell" "$user"
    elif [[ "$(passwd_field "$user" 7)" != "$shell" ]]; then
      as_root usermod --shell "$shell" "$user"
    fi
    home=$(passwd_field "$user" 6)
    as_root chmod 750 "$home"
  done
}

step_ci_limits() {
  local path
  path=$(machine_ci_limits_path)
  if [[ -z "$path" ]]; then
    warn "there is no ci user yet: its limits are left for the next install."
    return 0
  fi
  write_system_file "$path" 0644 "$(machine_ci_limits_content)"
  as_root systemctl daemon-reload
}

step_low_ports() {
  local user path wrote=""
  for user in "${MACHINE_LOW_PORT_USERS[@]}"; do
    path=$(machine_low_ports_path "$user")
    if [[ -z "$path" ]]; then
      warn "there is no $user user yet: its port limit is left for the next install."
      continue
    fi
    write_system_file "$path" 0644 "$(machine_low_ports_content)"
    wrote=1
  done
  [[ -z "$wrote" ]] || as_root systemctl daemon-reload
}

# Passwords off is only safe when a key gets the admin in: without one this refuses, before anything is
# written. Then the drop-in is written and tried: sshd -t must pass and sshd -T (what sshd would really
# run with, first value wins across the files) must show the hardening in force. If either fails the
# drop-in is taken back out and ssh is not reloaded: a bad file would lock everyone out at the next
# restart, and one that another file overrides would leave passwords on.
step_ssh() {
  local path keys admin ssh_user home previous="" had="" effective conflict
  path=$(machine_ssh_path)
  keys=$(machine_admin_keys)
  machine_ssh_keys_refusal
  admin=$(machine_admin)
  for ssh_user in $(role_ssh_users "$machine_current_role"); do
    [[ "$ssh_user" != "$admin" ]] || continue
    home=$(passwd_field "$ssh_user" 6)
    if [[ -z "$home" ]]; then
      warn "there is no $ssh_user user yet: its keys are left for the next install."
      continue
    fi
    # The home is closed to the admin, so only root can say whether the user has keys; keys that are
    # there are never overwritten.
    as_root test -s "$home/.ssh/authorized_keys" && continue
    confirm "$ssh_user has no authorized_keys: copy yours ($keys) to it?" y || continue
    as_root install -d -m 700 -o "$ssh_user" -g "$ssh_user" "$home/.ssh"
    as_root install -m 600 -o "$ssh_user" -g "$ssh_user" "$keys" "$home/.ssh/authorized_keys"
  done
  if [[ -f "$path" ]]; then
    had=1
    previous=$(<"$path")
  fi
  write_system_file "$path" 0644 "$(machine_ssh_content)"
  # On a fresh host sshd's privilege separation directory doesn't exist until sshd first starts.
  as_root install -d -m 755 "${YGG_SYSROOT:-}/run/sshd"
  if ! as_root sshd -t; then
    machine_ssh_rollback "$path" "$had" "$previous"
    die "sshd -t rejects the configuration: $path is back as it was and ssh was not reloaded"
  fi
  if ! effective=$(as_root sshd -T); then
    machine_ssh_rollback "$path" "$had" "$previous"
    die "sshd -T could not show the effective configuration: $path is back as it was and ssh was not reloaded"
  fi
  conflict=$(machine_ssh_conflict "$effective")
  if [[ -n "$conflict" ]]; then
    machine_ssh_rollback "$path" "$had" "$previous"
    die "sshd would run with $conflict: another setting comes first (sshd_config or a file ahead of $path). $path is back as it was and ssh was not reloaded"
  fi
  as_root rm -f "$(machine_ssh_legacy_path)"
  # ssh may be socket-activated (Ubuntu 24.04 and later), where a plain reload can find nothing to reload.
  as_root systemctl try-reload-or-restart ssh
}

# machine_ssh_rollback <path> <had> <previous content>: the drop-in as it was before step_ssh.
machine_ssh_rollback() {
  if [[ -n "$2" ]]; then
    write_system_file "$1" 0644 "$3"
  else
    as_root rm -f "$1"
  fi
}

# ygg deploys from this checkout, so it owns it.
step_checkout() {
  if ! user_exists ygg; then
    warn "there is no ygg user yet: the checkout stays as it is until the next install."
    return 0
  fi
  as_root chown -R ygg:ygg "$root"
  as_root chmod 755 "$root"
}

# Each user's secrets directory, closed to everyone else, with the env templates copied in as they are
# for the user to fill; a file that is already there is never touched.
step_secrets() {
  local users user dir pair template file
  mapfile -t users < <(role_users "$machine_current_role")
  for user in "${users[@]}"; do
    if ! user_exists "$user"; then
      warn "there is no $user user yet: its secrets directory is left for the next install."
      continue
    fi
    dir=$(secrets_dir_of "$user")
    as_root install -d -m 750 -o "$user" -g "$user" "$dir"
    for pair in $(machine_secret_files "$user"); do
      template="$root/env/${pair%%:*}"
      file="$dir/${pair##*:}"
      [[ -f "$template" ]] || die "$template is missing from the checkout"
      as_root test -e "$file" || as_root install -m 600 -o "$user" -g "$user" "$template" "$file"
    done
  done
}

# The firewall fails closed. SSH is allowed and the default for incoming set to deny before ufw is
# turned on (the session running this isn't cut, and nothing else gets in), whatever happens to the
# apps machine's download of Cloudflare's ranges. When the download fails only the Cloudflare part
# waits: the rules and cloudflare-ips of the last download that worked stay, and 80 and 443 are never
# opened to everyone to make up for it. Rules that are there already are not added again, and the
# ranges Cloudflare no longer lists are deleted once the new ones are in.
step_firewall() {
  local role=$machine_current_role ranges="" fetched="" status rules spec cidr file have previous
  if [[ "$role" == apps ]]; then
    if ranges=$(cloudflare_ranges); then
      fetched=1
    else
      warn "could not fetch Cloudflare's ranges: ufw is on with ssh allowed and incoming denied, but the Cloudflare rules and cloudflare-ips are left as they are (none, on a new machine: 80 and 443 stay closed). Run install again once www.cloudflare.com answers."
    fi
  fi
  status=$(as_root ufw status) || die "ufw status failed: the firewall was not changed"
  rules=$(machine_ufw_rules "$status")
  for spec in $(machine_firewall_allows "$role"); do
    machine_ufw_has "$rules" "$spec" Anywhere "" || as_root ufw allow "$spec"
  done
  as_root ufw default deny incoming
  as_root ufw default allow outgoing
  # ufw lists no rules while it is off, so it is turned on (ssh is allowed) before the rest is compared.
  if [[ "$status" != *"Status: active"* ]]; then
    as_root ufw --force enable
    status=$(as_root ufw status) || die "ufw status failed after turning it on"
    rules=$(machine_ufw_rules "$status")
  fi
  [[ "$role" == apps ]] || return 0

  if [[ -n "$fetched" ]]; then
    have=$(machine_ufw_cloudflare "$rules")
    while IFS= read -r cidr; do
      [[ -z "$cidr" ]] || as_root ufw allow proto tcp from "$cidr" to any port 80,443 comment "$CLOUDFLARE_COMMENT"
    done < <(machine_only_in_second "$have" "$ranges")
    while IFS= read -r cidr; do
      [[ -z "$cidr" ]] || as_root ufw delete allow proto tcp from "$cidr" to any port 80,443
    done < <(machine_only_in_second "$ranges" "$have")

    file=$(machine_cloudflare_ips_path)
    if [[ -d "$(dirname "$file")" ]]; then
      # The secrets directory is closed to the admin: what the file held is read as root.
      previous=$(as_root cat "$file" 2>/dev/null) || previous=""
      write_system_file "$file" 0644 "$ranges"
      as_root chown ygg:ygg "$file"
      # Traefik's trusted ranges come from its traefik.yml, which platform.sh renders from this file.
      [[ "$previous" == "$ranges" ]] \
        || say "cloudflare-ips has new ranges: Traefik trusts them after the next 'scripts/platform.sh up' as ygg."
    else
      warn "$(dirname "$file") isn't there yet: cloudflare-ips is left for the next install."
    fi
  fi

  # What the workstation's firewall opens (80 and 443 to everyone) doesn't belong on this machine.
  for spec in $(machine_ufw_open_web "$rules"); do
    ! machine_plain_web "$spec" || as_root ufw delete allow "$spec"
  done
}

# Each service user runs its own rootless engine under its own systemd (linger keeps that running with
# nobody logged in): dockerd-rootless-setuptool.sh makes the user unit, the drop-in makes it use
# slirp4netns, and DOCKER_HOST in the .profile points the CLI of a login shell at the user's socket. All of
# it is done as the user: the homes are closed to the admin, and the files are the user's own.
step_engines() {
  local users user home override held active restart profile current wanted
  mapfile -t users < <(role_users "$machine_current_role")
  for user in "${users[@]}"; do
    if ! user_exists "$user"; then
      warn "there is no $user user yet: its engine is left for the next install."
      continue
    fi
    home=$(passwd_field "$user" 6)
    as_root loginctl enable-linger "$user"
    machine_wait_for_user_manager "$user"

    override=$(machine_override_path "$user")
    restart=""
    # An engine that is running only takes the new addresses when it restarts.
    active=""
    if run_as_user "$user" systemctl --user is-active --quiet docker; then active=1; fi
    held=$(user_file_read "$user" "$override")
    if ! machine_override_is "$held"; then
      user_file_write "$user" "$override" "$(machine_override_content)"
      restart=$active
    fi
    run_as_user "$user" dockerd-rootless-setuptool.sh install
    run_as_user "$user" systemctl --user daemon-reload
    run_as_user "$user" systemctl --user enable --now docker
    [[ -z "$restart" ]] || run_as_user "$user" systemctl --user restart docker

    profile="$home/.profile"
    current=$(user_file_read "$user" "$profile")
    wanted=$(profile_with_block "$user" "$current")
    [[ "$current" == "$wanted" ]] || user_file_write "$user" "$profile" "$wanted"
  done
}

# The prune units are copied by ci and by release themselves, each into its own home, and each timer is
# enabled now and at boot after its user's engine (the prune happens in it). Nothing to configure: unlike the
# backup, it needs no file from the owner.
step_prune_timer() {
  local user dir unit
  for user in "${MACHINE_PRUNE_USERS[@]}"; do
    if ! user_exists "$user"; then
      warn "there is no $user user yet: its prune timer is left for the next install."
      continue
    fi
    dir=$(machine_prune_unit_dir "$user")
    machine_wait_for_user_manager "$user"
    run_as_user "$user" mkdir -p "$dir"
    for unit in "${MACHINE_PRUNE_UNITS[@]}"; do
      [[ -f "$root/platform/systemd/$unit" ]] || die "$root/platform/systemd/$unit is missing from the checkout"
      run_as_user "$user" tee "$dir/$unit" <"$root/platform/systemd/$unit" >/dev/null
    done
    run_as_user "$user" systemctl --user daemon-reload
    run_as_user "$user" systemctl --user enable --now yggdrasil-prune.timer
  done
}

# The units are copied by ygg itself, into its own home, from the checkout: the files are its own and the
# copy is what the check compares with. The timer is enabled now and at boot (linger keeps ygg's manager
# up), and runs the backup whether or not backup.conf is filled in yet: a night without a target fails
# loudly in the journal instead of being silently missing.
step_backup_timer() {
  if ! user_exists ygg; then
    warn "there is no ygg user yet: its backup timer is left for the next install."
    return 0
  fi
  local dir unit
  dir=$(machine_backup_unit_dir)
  machine_wait_for_user_manager ygg
  run_as_user ygg mkdir -p "$dir"
  for unit in "${MACHINE_BACKUP_UNITS[@]}"; do
    [[ -f "$root/platform/systemd/$unit" ]] || die "$root/platform/systemd/$unit is missing from the checkout"
    run_as_user ygg tee "$dir/$unit" <"$root/platform/systemd/$unit" >/dev/null
  done
  run_as_user ygg systemctl --user daemon-reload
  run_as_user ygg systemctl --user enable --now yggdrasil-backup.timer
}

# The same for renovate, on the dev machine only: its units are copied by renovate itself into its own home,
# and the timer is enabled now and at boot after its engine (the run happens in it). It runs whether or not
# renovate.env has its token yet: a day without one fails loudly in the journal instead of being silently
# missing.
step_renovate_timer() {
  if ! user_exists renovate; then
    warn "there is no renovate user yet: its timer is left for the next install."
    return 0
  fi
  local dir unit
  dir=$(machine_renovate_unit_dir)
  machine_wait_for_user_manager renovate
  run_as_user renovate mkdir -p "$dir"
  for unit in "${MACHINE_RENOVATE_UNITS[@]}"; do
    [[ -f "$root/platform/systemd/$unit" ]] || die "$root/platform/systemd/$unit is missing from the checkout"
    run_as_user renovate tee "$dir/$unit" <"$root/platform/systemd/$unit" >/dev/null
  done
  run_as_user renovate systemctl --user daemon-reload
  run_as_user renovate systemctl --user enable --now yggdrasil-renovate.timer
}

step_role_file() {
  write_system_file "${YGG_SYSROOT:-}/etc/yggdrasil-role" 0644 "$machine_current_role"
}
