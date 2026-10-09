#!/usr/bin/env bash
# Kept so that every `scripts/ygg.sh ...` keeps working: the same program as the `ygg` command
# (scripts/ygg.py; `ygg help`, docs/cli.md). Without python3, check and install still run, from
# scripts/host.sh, so that a bare host can install it.
here=$(dirname "${BASH_SOURCE[0]}")
if ! command -v python3 >/dev/null 2>&1; then
  case ${1:-} in
    check | install) exec bash "$here/host.sh" "$1" ;;
    *) echo "ygg: python3 is needed: run scripts/ygg.sh install" >&2; exit 1 ;;
  esac
fi
exec python3 "$here/ygg.py" "$@"
