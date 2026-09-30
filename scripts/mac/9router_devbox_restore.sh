#!/bin/bash
# Restore the runtime links and launchd job from persistent devbox storage.
set -euo pipefail
[ "$(sysctl -n kern.hv_vmm_present)" = 1 ] || { echo "refused: run on the devbox"; exit 1; }
d=/Volumes/devbox/9router
[ -f "$d/baseline" ] || { echo "refused: persistent baseline is missing"; exit 1; }
ver=$(<"$d/baseline")
[[ "$ver" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "refused: invalid baseline version"; exit 1; }
rel="$d/releases/$ver/lib/node_modules/9router"
[ -d "$rel" ] || { echo "refused: baseline release is missing"; exit 1; }
link=/opt/homebrew/lib/node_modules/9router
mkdir -p /opt/homebrew/lib/node_modules "$HOME/Library/LaunchAgents"
if [ -L "$HOME/.9router" ]; then
  [ "$(readlink "$HOME/.9router")" = "$d" ] || { echo "refused: unexpected state link"; exit 1; }
elif [ -d "$HOME/.9router" ]; then
  # npm may create an empty runtime directory before the first launch.
  [ ! -e "$HOME/.9router/db" ] && [ ! -e "$d/bootstrap-home" ] || { echo "refused: existing home state needs explicit migration"; exit 1; }
  mv -n "$HOME/.9router" "$d/bootstrap-home"
  [ ! -e "$HOME/.9router" ] || { echo "refused: home state was not preserved"; exit 1; }
  ln -s "$d" "$HOME/.9router"
elif [ -e "$HOME/.9router" ]; then
  echo "refused: unexpected home state file"; exit 1
else
  ln -s "$d" "$HOME/.9router"
fi
if [ -L "$link" ]; then
  case "$(readlink "$link")" in "$d/releases/"*) ;; *) echo "refused: unexpected release link"; exit 1 ;; esac
elif [ -e "$link" ]; then
  echo "refused: existing router installation"; exit 1
else
  ln -s "$rel" "$link"
fi
if [ -L /opt/homebrew/bin/9router ]; then
  [ "$(readlink /opt/homebrew/bin/9router)" = ../lib/node_modules/9router/cli.js ] || { echo "refused: unexpected CLI link"; exit 1; }
elif [ -e /opt/homebrew/bin/9router ]; then
  echo "refused: existing CLI executable"; exit 1
else
  ln -s ../lib/node_modules/9router/cli.js /opt/homebrew/bin/9router
fi
label=com.lfenergy.9router
if ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
  cp "$d/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
fi
printf 'devbox runtime restored from %s\n' "$d"
