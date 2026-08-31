#!/usr/bin/env bash
#
# Remove everything install.sh added. Leaves ~/.muster alone: the index and the
# blob store are the expensive part and are not this script's to delete.

set -euo pipefail
LABEL="dev.muster.desktop.serve"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -f "/Applications/Muster.app" 2>/dev/null || true
osascript -e 'tell application "System Events" to delete every login item whose name is "Muster"' >/dev/null 2>&1 || true
rm -rf /Applications/Muster.app

cat <<EOF
Removed the app, the login item and the launch agent.

Your index is untouched at ~/.muster — delete it yourself if you mean to.
EOF
