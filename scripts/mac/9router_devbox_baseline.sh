#!/bin/bash
# Guest-only managed baseline. Use independent credentials and the exact package.
# Provisioning does not authorize provider login or reset persistent login state.
set -euo pipefail
[ "$#" -eq 3 ] && [ "$2" = "--input" ] || {
  echo "usage: 9router_devbox_baseline.sh <release.tgz> --input <private-input.json>" >&2
  exit 1
}
exec python3 "$(dirname "$0")/9router_vm_qualify.py" baseline "$1" --input "$3"
