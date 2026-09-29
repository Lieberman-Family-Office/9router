#!/bin/bash
# One-time: turn a Namespace macOS devbox into the 9router qualification baseline.
# Run ON the devbox:  bash 9router_devbox_baseline.sh <release.tgz>
# Mirrors production: Homebrew node, release adopted behind
# /opt/homebrew/lib/node_modules/9router, same start.sh, same launchd label.
# After it: log in providers + create one API key in the dashboard (steps printed).
set -euo pipefail
tgz=${1:?usage: 9router_devbox_baseline.sh <release.tgz>}
[ "$(sysctl -n kern.hv_vmm_present)" = 1 ] || { echo "refused: run on the devbox, not the host"; exit 1; }
link=/opt/homebrew/lib/node_modules/9router
[ -e "$link" ] && { echo "refused: $link exists (already provisioned)"; exit 1; }

ver=$(tar -xOzf "$tgz" package/package.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
d="$HOME/.9router"
rel="$d/releases/$ver"
label=com.lfenergy.9router
plist="$HOME/Library/LaunchAgents/$label.plist"

mkdir -p "$d/logs" "$HOME/Library/LaunchAgents"
npm install -g --prefix "$rel" "$tgz"
ln -s "$rel/lib/node_modules/9router" "$link"
ln -sf ../lib/node_modules/9router/cli.js /opt/homebrew/bin/9router
echo "$ver" > "$d/baseline" # 9router_vm_qualify.py guest resets to this version

(umask 077 && cat > "$d/env.sh" <<EOF
export DATA_DIR="\$HOME/.9router"
export JWT_SECRET="$(openssl rand -hex 32)"
export INITIAL_PASSWORD="$(openssl rand -hex 12)"
export ENABLE_REQUEST_LOGS="false"
export NODE_ENV="production"
export NINEROUTER_DBG_CHUNKS="0"
export NINEROUTER_NO_TRAY=1
export REQUEST_DETAILS_MODE="metadata"
EOF
)

cat > "$d/start.sh" <<'EOF'
#!/bin/zsh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin"
export NINEROUTER_ATTACHED_SERVER=1
if [[ -f "${HOME}/.9router/env.sh" ]]; then set -a; . "${HOME}/.9router/env.sh"; set +a; fi
exec 9router --host 127.0.0.1 --port 20128 --no-browser --skip-update --log
EOF
chmod 755 "$d/start.sh"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$d/start.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$d/logs/launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>$d/logs/launchd-stderr.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$plist"

cat <<EOF
baseline $ver installed and running on 127.0.0.1:20128.
Next (operator, once):
  1. from the host: ssh -N -L 20129:127.0.0.1:20128 <devbox ssh alias, e.g. 9router-test-vm.devbox.namespace>
  2. open http://127.0.0.1:20129 ; password: grep INITIAL_PASSWORD ~/.9router/env.sh
  3. log in the Codex and Claude providers; create one API key
EOF
