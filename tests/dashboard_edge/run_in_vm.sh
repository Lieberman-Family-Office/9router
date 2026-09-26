#!/bin/sh
# Runs INSIDE the disposable container. Proves production-shaped Caddy:
#   old: /v1 requires bearer; / and /dashboard → empty 404
#   new: same /v1 gate; / and /dashboard → fake 9router HTML (cookies intact)
set -eu

python3 /repro/fake_upstream.py >/tmp/upstream.log 2>&1 &
sleep 1

run_case() {
	label=$1
	cfg=$2
	caddy stop >/dev/null 2>&1 || true
	caddy run --config "/repro/$cfg" --adapter caddyfile >/tmp/caddy-$label.log 2>&1 &
	sleep 1
	root=$(curl -sS -o /tmp/root-$label.body -w '%{http_code}' http://127.0.0.1:8080/ || true)
	dash=$(curl -sS -D /tmp/dash-$label.hdr -o /tmp/dash-$label.body -w '%{http_code}' http://127.0.0.1:8080/dashboard || true)
	asset=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/_next/static/proof.js || true)
	v1_noauth=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/v1/models || true)
	v1_auth=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer test-bearer' http://127.0.0.1:8080/v1/models || true)
	cookie=$(grep -i '^set-cookie:' /tmp/dash-$label.hdr | head -n 1 || true)
	has_html=0
	grep -q '9Router' /tmp/dash-$label.body && has_html=1 || true
	has_cookie=0
	[ -n "$cookie" ] && has_cookie=1
	printf '{"caddy":"%s","root":"%s","dashboard":"%s","asset":"%s","v1_noauth":"%s","v1_auth":"%s","has_html":%s,"has_cookie":%s}\n' \
		"$label" "$root" "$dash" "$asset" "$v1_noauth" "$v1_auth" "$has_html" "$has_cookie"
	eval "root_${label}=$root"
	eval "dash_${label}=$dash"
	eval "asset_${label}=$asset"
	eval "v1n_${label}=$v1_noauth"
	eval "v1a_${label}=$v1_auth"
	eval "html_${label}=$has_html"
	eval "cookie_${label}=$has_cookie"
}

run_case old Caddyfile.old
run_case new Caddyfile.new

failures=0
[ "$root_old" = 404 ] && [ "$dash_old" = 404 ] && [ "$asset_old" = 404 ] || {
	echo "FAIL old: expected UI paths 404" >&2
	failures=$((failures + 1))
}
[ "$v1n_old" = 401 ] && [ "$v1a_old" = 200 ] || {
	echo "FAIL old: expected /v1 401 without bearer, 200 with" >&2
	failures=$((failures + 1))
}
[ "$root_new" = 307 ] && [ "$dash_new" = 200 ] && [ "$asset_new" = 200 ] || {
	echo "FAIL new: expected root 307, dashboard/asset 200" >&2
	failures=$((failures + 1))
}
[ "$html_new" = 1 ] && [ "$cookie_new" = 1 ] || {
	echo "FAIL new: expected HTML body and Set-Cookie" >&2
	failures=$((failures + 1))
}
[ "$v1n_new" = 401 ] && [ "$v1a_new" = 200 ] || {
	echo "FAIL new: /v1 bearer gate must remain" >&2
	failures=$((failures + 1))
}

[ "$failures" = 0 ] && echo "PASS: old 404 UI; new serves dashboard; /v1 bearer intact" || exit 1
