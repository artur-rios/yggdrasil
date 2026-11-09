#!/usr/bin/env bash
# Runs Heimdall's security signal job once for one environment, and tells a heartbeat monitor how it went. Run as ygg
# on the Application Server, on its own rootless engine, by the systemd user timer
# yggdrasil-security-signals@<environment>.timer (platform/systemd, installed by `ygg install`, step signals-timer).
#
#     scripts/security-signals.sh run <environment>
#
# The job reads the API's log files, finds the signals it has not reported and mails them through Hermes under the
# client `heimdall-signals`. Both breach notification clocks run from awareness, so a job that cannot deliver has to
# reach a person by a path that does not depend on what just failed: Hermes, this engine, this machine. That path is
# the heartbeat, a monitor outside this machine: every run ends by sending it its exit status, and the monitor mails
# its owner when a status is not 0 or when no ping arrives, which covers a dead machine, a stopped engine, a disabled
# timer and a run killed by its timeout. Only the exit status is sent: what the job prints holds user identifiers, and
# stays in ygg's journal.
#
# Reads <secrets>/security-signals-<environment>.env, secrets being $YGG_SECRETS_DIR (default /etc/yggdrasil).
# Template: env/security-signals.env.example. The container gets the key and the address through the -e NAME form, so
# neither is ever an argument of a command (arguments are readable by every user of the machine). Nor is the ping URL:
# curl reads it on stdin. The container never gets the ping URL at all.
#
# Exit 0 when the job delivered whatever it found. Otherwise the job's own status: 1 signals found and not delivered,
# 2 the collection could not run, 125 to 127 the container could not start. 3 for what is wrong before the container
# (a setting, the job's copy, the engine, the arguments), reported to the heartbeat whenever the URL could be read.
# 4 when only the ping failed: the monitor will notice the silence, and the journal says why.
set -euo pipefail

status_wrong_setup=3
status_ping_failed=4
heartbeat_url=""

# ping <status>: sends the status to the heartbeat. Fails when the monitor could not be told. Its error names the host
# at most: the URL itself is a secret, since whoever has it can report that all is well.
ping_heartbeat() {
  curl --fail --silent --show-error --max-time 10 --retry 5 --retry-connrefused --output /dev/null --config - \
    <<<"url = \"$heartbeat_url/$1\""
}

# Everything that goes wrong before the container comes here (docker-engine.sh calls it too), so that it reaches the
# heartbeat as soon as the URL is known. Before that, the missing ping does the same work.
die() {
  echo "security-signals: $*" >&2
  if [[ -n "$heartbeat_url" ]]; then
    ping_heartbeat "$status_wrong_setup" || echo "security-signals: the heartbeat could not be told either" >&2
  fi
  exit "$status_wrong_setup"
}

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# docker_socket, require_rootless and use_rootless_engine, shared with platform.sh, backup.sh and renovate.sh.
# shellcheck source=docker-engine.sh
source "$root/scripts/docker-engine.sh"

# The job's interpreter, an exact version and the digest of its manifest list: what reads the logs and holds the key
# changes with a commit here, never because a tag moved.
SIGNALS_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# The job itself: platform/heimdall-signals/security_signals.py is a copy of scripts/security_signals.py of
# heimdall-api at this commit, kept here so that the machine runs what was reviewed, needs no access to the forge at
# run time, and changes only with a commit of this repository. To update it, copy the file from the new commit and
# change both values in the same pull request (docs/hosts.md). A copy that does not match is refused.
SIGNALS_COMMIT=7496a4553dd276e0146c925ecee24c3d1aa3be16
SIGNALS_SHA256=5fdfd25e70fc3f0dd7127f10964350909d1d4a4de30ba7a3bb591c47d07c883a

[[ $# -eq 2 && "$1" == run ]] || die "usage: security-signals.sh run <environment>"
environment=$2
[[ "$environment" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die "not an environment: $environment"

env_file="${YGG_SECRETS_DIR:-/etc/yggdrasil}/security-signals-$environment.env"
job_dir="$root/platform/heimdall-signals"
# Compose's name for the API's container in this environment's project: the one that holds the logs volume.
api_container="heimdall-api-$environment-api-1"

# read_settings: reads the whole file before complaining about any of it, because the heartbeat URL may be on a later
# line than the mistake. What it says about a line never includes the value.
read_settings() {
  [[ -f "$env_file" ]] || die "missing $env_file (template: env/security-signals.env.example)"
  local line number=0 name value problems=()
  HEIMDALL_SIGNAL_HERMES_API_KEY="" HEIMDALL_SIGNAL_ALERT_TO=""
  local url=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    line=${line%$'\r'}
    [[ -n "$line" && "$line" != \#* ]] || continue
    if [[ ! "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
      problems+=("line $number is not NAME=value (no quotes, no export, no comment after a value)")
      continue
    fi
    name=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    case $name in
      HEIMDALL_SIGNAL_HERMES_API_KEY | HEIMDALL_SIGNAL_ALERT_TO) printf -v "$name" '%s' "$value" ;;
      HEIMDALL_SIGNAL_HEARTBEAT_URL) url=$value ;;
      *) problems+=("line $number sets $name, which this file does not take") ;;
    esac
  done <"$env_file"

  # Double quotes and backslashes would break out of the value curl reads.
  if [[ -z "$url" ]]; then
    problems+=("HEIMDALL_SIGNAL_HEARTBEAT_URL is empty: a failed run would reach nobody")
  elif [[ "$url" =~ ^https?://[^[:space:]\"\\]+$ ]]; then
    heartbeat_url=${url%/}
  else
    problems+=("HEIMDALL_SIGNAL_HEARTBEAT_URL is not an http or https URL")
  fi
  [[ -n "$HEIMDALL_SIGNAL_HERMES_API_KEY" ]] || problems+=("HEIMDALL_SIGNAL_HERMES_API_KEY is empty")
  [[ -n "$HEIMDALL_SIGNAL_ALERT_TO" ]] || problems+=("HEIMDALL_SIGNAL_ALERT_TO is empty")
  if ((${#problems[@]})); then
    local IFS=';'
    die "$env_file: ${problems[*]}"
  fi
}

read_settings

actual=$(sha256sum "$job_dir/security_signals.py" 2>/dev/null | cut -d' ' -f1) || actual=""
[[ "$actual" == "$SIGNALS_SHA256" ]] \
  || die "$job_dir/security_signals.py is not heimdall-api's at $SIGNALS_COMMIT (checksum differs): restore it, or update the pin"

use_rootless_engine

# What the docker CLI hands to the container by name, with -e and no value. Hermes has no published port, so the
# container is on the edge network and reaches it at its alias there. The logs are in the API's named volume, and the
# watermark is written next to them, which keeps it across runs.
export HEIMDALL_SIGNAL_HERMES_API_KEY HEIMDALL_SIGNAL_ALERT_TO
export HERMES_BASE_URL="http://hermes-api.$environment:8080"

status=0
docker run --rm \
  --network edge \
  --volumes-from "$api_container" \
  -e HEIMDALL_LOG_DIRECTORY=/app/logs \
  -e HEIMDALL_SIGNAL_ALERT_TO -e HERMES_BASE_URL -e HEIMDALL_SIGNAL_HERMES_API_KEY \
  -v "$job_dir:/signals:ro" \
  "$SIGNALS_IMAGE" \
  python3 /signals/security_signals.py --quiet || status=$?

if ! ping_heartbeat "$status"; then
  echo "security-signals: the job exited $status and the heartbeat could not be told" >&2
  ((status != 0)) || status=$status_ping_failed
fi
exit "$status"
