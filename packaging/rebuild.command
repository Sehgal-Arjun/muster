#!/bin/bash
#
# Double-click this to quit Muster, rebuild it, and start the new one.
#
# A .command file is what macOS will run from a double-click, and it opens a
# Terminal window — which is wanted here, because a release build takes minutes
# and silent progress is indistinguishable from a hang.
#
# The environment is the catch. A double-clicked script gets a login shell with
# none of the interactive rc files, so anything installed by a version manager
# is simply absent: cargo is on PATH because its installer writes to
# ~/.zprofile, but node under nvm is not, and the script would die on
# "npm: command not found" before doing anything. Hence the setup below.

set -uo pipefail

# Resolve symlinks before deriving the repo: there is a convenience alias at
# the repo root pointing here, and dirname/.. from *that* lands outside the
# checkout entirely.
SELF="${BASH_SOURCE[0]}"
while [ -L "$SELF" ]; do
    target="$(readlink "$SELF")"
    case "$target" in
        /*) SELF="$target" ;;
        *)  SELF="$(dirname "$SELF")/$target" ;;
    esac
done
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$REPO" || exit 1

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
fail() {
    printf "\n\033[31m%s\033[0m\n\n" "$*"
    echo "Nothing was changed — the previously installed Muster is untouched."
    echo
    read -n 1 -s -r -p "Press any key to close this window."
    exit 1
}

bold "Muster — rebuild and reinstall"
echo "  $REPO"
echo

# --- environment -----------------------------------------------------------
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v npm >/dev/null 2>&1; then
    # nvm's own script is the reliable way in; globbing the versions directory
    # is the fallback for when it is installed but not initialised.
    if [ -s "$HOME/.nvm/nvm.sh" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1
    fi
    if ! command -v npm >/dev/null 2>&1; then
        newest="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
        [ -n "$newest" ] && export PATH="$newest:$PATH"
    fi
fi

for tool in npm cargo; do
    command -v "$tool" >/dev/null 2>&1 || fail "Cannot find $tool. Open a normal Terminal, run 'command -v $tool', and tell me what it says."
done
echo "  node  $(command -v node)"
echo "  cargo $(command -v cargo)"
echo

# --- stop the running copy -------------------------------------------------
# Before building, not after: a half-replaced bundle under a live process is
# worse than a few extra seconds without the panel.
bold "Quitting Muster"
osascript -e 'quit app "Muster"' >/dev/null 2>&1
pkill -f "Muster.app/Contents/MacOS/muster-ui" 2>/dev/null
pkill -f "target/release/bundle/macos/Muster.app" 2>/dev/null
sleep 1
echo "  stopped"
echo

# --- build -----------------------------------------------------------------
cd "$REPO/ui" || fail "No ui/ directory in $REPO"

if [ ! -d node_modules ]; then
    bold "Installing frontend dependencies (first run only)"
    npm install --no-audit --no-fund || fail "npm install failed."
    echo
fi

bold "Building (a few minutes — release builds use LTO)"
npm run tauri build || fail "The build failed. The error is above."
echo

# --- install ---------------------------------------------------------------
bold "Installing"
"$REPO/packaging/install.sh" || fail "Install failed. The error is above."

printf "\n\033[32m%s\033[0m\n" "Muster is running the new build. Press ⌘⇧Space."
echo
echo "This window can be closed."
