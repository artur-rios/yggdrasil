# shellcheck shell=bash
# The machine roles of scripts/host.sh: what `check` and `install` do for `dev` and `apps` (Ubuntu
# 24.04 hosts that run every engine rootless), as opposed to `workstation`, which keeps today's
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
MACHINE_STEPS=(packages rootful-docker-off system-settings unattended-upgrades users ci-limits ssh checkout secrets role-file)

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

# machine_preflight <role>: install for dev and apps is for Ubuntu 24.04, run by the admin with sudo.
machine_preflight() {
  if ! is_ubuntu || [[ "$(os_field VERSION_ID)" != 24.04 ]]; then
    die "the $1 role installs on Ubuntu 24.04 LTS only; this is $(os_field PRETTY_NAME || echo unknown)"
  fi
  [[ "$(id -u)" != 0 ]] || die "run install as the admin user, not as root: it asks sudo for what needs root, and the admin is whoever runs it"
  has sudo || die "installing needs sudo: install it first"
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
  as_root install -d -m 0755 "$(dirname "$path")"
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
  esac
}

# Rootless engines publish ports below 1024 (80 and 443) without a privileged helper.
machine_sysctl_content() { echo "net.ipv4.ip_unprivileged_port_start=80"; }

# Ubuntu 24.04 confines unprivileged user namespaces; this profile lets rootlesskit use them.
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
  if ! is_ubuntu || [[ "$(os_field VERSION_ID)" != 24.04 ]]; then
    row warn "Ubuntu" "$(os_field PRETTY_NAME || echo unknown): install knows Ubuntu 24.04 only, check works anywhere"
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
  # The distribution's docker.io and friends clash with Docker's packages.
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
  if [[ "$owner" == ygg:ygg ]]; then
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
    packages) echo "Packages: ${machine_missing[*]:-the distribution's Docker packages are replaced by Docker's}, with Docker's from its apt repository (download.docker.com)." ;;
    rootful-docker-off) echo "Turn off the machine's own Docker (docker.service, docker.socket, containerd.service) and empty the docker group." ;;
    system-settings) echo "Unprivileged ports from 80, an AppArmor profile for rootlesskit and cgroup delegation to users' units." ;;
    unattended-upgrades) echo "Install security updates by themselves (${YGG_SYSROOT:-}/etc/apt/apt.conf.d/20auto-upgrades)." ;;
    users) echo "Create the service users of the $machine_current_role role ($(role_users "$machine_current_role" | paste -sd' ' -)) without sudo or docker access, and close their home directories (750)." ;;
    ci-limits) echo "Cap the ci user at six CPUs and 20G of memory." ;;
    ssh) echo "Harden ssh (00-yggdrasil.conf, ahead of the cloud image's own files): keys only, no root login, and only $(machine_admin) and the role's ssh users may log in. Your key is offered to the ones without one." ;;
    checkout) echo "Give the checkout, $root, to the ygg user." ;;
    secrets) echo "Create the users' secrets directories (750, each its user's) with the env templates copied in, never over a file that is there." ;;
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
  local members=() member
  IFS=, read -r -a members < <(getent group docker 2>/dev/null | cut -d: -f4 || true) || true
  for member in "${members[@]}"; do
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
  # ssh may be socket-activated (Ubuntu 24.04), where a plain reload can find nothing to reload.
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

step_role_file() {
  write_system_file "${YGG_SYSROOT:-}/etc/yggdrasil-role" 0644 "$machine_current_role"
}
