#!/usr/bin/env bash
#
# Install Muster so it never needs a terminal again.
#
#   1. Muster.app -> /Applications
#   2. `muster serve` -> a LaunchAgent that starts at login and restarts on crash
#   3. Muster.app -> a login item, so the panel is always there on the hotkey
#
# The index is deliberately NOT inside the .app. Bundling Python would mean
# shipping a second copy of torch and the embedding model, and would put a
# multi-hour backfill behind a double-click. The panel talks to the server over
# loopback and says so plainly when it is not running.
#
# Undo everything with packaging/uninstall.sh.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SRC="$REPO/ui/src-tauri/target/release/bundle/macos/Muster.app"
APP_DST="/Applications/Muster.app"
LABEL="dev.muster.desktop.serve"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
MUSTER_BIN="$REPO/.venv/bin/muster"

say() { printf "  %s\n" "$*"; }

[ -x "$MUSTER_BIN" ] || { echo "No muster at $MUSTER_BIN — run 'make install' first." >&2; exit 1; }
[ -d "$APP_SRC" ] || { echo "No app at $APP_SRC — run 'cd ui && npm run tauri build' first." >&2; exit 1; }

echo "Installing Muster"

# --- 1. the app ------------------------------------------------------------
# Quit a running copy first: replacing a bundle under a live process leaves it
# in a state where the panel is up but its resources are gone.
pkill -f "/Applications/Muster.app" 2>/dev/null || true
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"
say "app        $APP_DST"

# --- 2. the server ---------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.muster"
sed -e "s|__MUSTER_BIN__|$MUSTER_BIN|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__REPO__|$REPO|g" \
    "$REPO/packaging/$LABEL.plist" > "$AGENT"

# bootout before bootstrap: launchctl refuses to load a label already present,
# and this script has to be safe to re-run after every rebuild.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$AGENT"
say "server     launchd $LABEL (log: ~/.muster/serve.log)"

# --- 3. launch at login ----------------------------------------------------
osascript -e 'tell application "System Events" to delete every login item whose name is "Muster"' >/dev/null 2>&1 || true
osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP_DST\", hidden:true}" >/dev/null
say "login item Muster (hidden)"

# --- wait for the server, so the first hotkey press is not a broken panel ---
printf "  waiting for the index"
for _ in $(seq 1 30); do
    if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:8765/status; then
        printf " up\n"; break
    fi
    printf "."; sleep 1
done

open -a "$APP_DST"

cat <<EOF

Done. Press ⌘⇧Space.

  The panel has no Dock icon and no window until you summon it — that is
  correct, not a failure to launch.

  server log   ~/.muster/serve.log
  uninstall    $REPO/packaging/uninstall.sh
EOF
