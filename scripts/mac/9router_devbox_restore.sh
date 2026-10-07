#!/bin/bash
# Guest-only managed restoration. Keep provider DB and every referenced release.
# The scoped controller recreates short sockets and slot plists before the proxy.
set -euo pipefail
[ "$#" -eq 2 ] && [ "$1" = "--scope" ] || {
  echo "usage: 9router_devbox_restore.sh --scope <private-scope.json>" >&2
  exit 1
}
exec python3 "$(dirname "$0")/9router_vm_qualify.py" restore --scope "$2"
