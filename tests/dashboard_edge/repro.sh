#!/bin/sh
# Disposable Linux container proof for widening Caddy so
# https://9r.liebermanfamilyoffice.com/ serves the 9router dashboard.
# Does not touch production. Mirrors EC2 Caddy path filtering.
#
# Usage: _scratch/9router_dashboard_edge/repro.sh
set -eu

here=$(cd "$(dirname "$0")" && pwd)
chmod +x "$here/run_in_vm.sh"

docker run --rm --platform linux/arm64 -v "$here:/repro:ro" caddy:2-alpine sh -c "
	set -e
	apk add --no-cache curl python3 >/dev/null
	sh /repro/run_in_vm.sh
"
