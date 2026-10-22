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
MACHINE_STEPS=(packages rootful-docker-off system-settings role-file)

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

machine_check_role_file() {
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
    role-file) echo "Write the role, $machine_current_role, to ${YGG_SYSROOT:-}/etc/yggdrasil-role." ;;
  esac
}

# install_machine <role>: runs the steps in machine_todo (check_machine fills it), in the order of
# MACHINE_STEPS, asking before each. A step can change what the later ones find (installing Docker
# starts the engine the next step turns off), so the machine is checked again after each one.
install_machine() {
  local step did=""
  machine_current_role=$1
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

step_role_file() {
  write_system_file "${YGG_SYSROOT:-}/etc/yggdrasil-role" 0644 "$machine_current_role"
}
