#!/bin/bash
#
# Double-click to stop or restart Muster's background work.
#
# One file that toggles, rather than two that you have to pick between: the
# question is only ever "is it using my machine or not", and the answer is
# visible in the window this opens.
#
# Pausing is always safe. Embedding writes vectors batch by batch, so stopping
# loses at most one batch, and whatever is left is simply picked up next time.

set -uo pipefail

SELF="${BASH_SOURCE[0]}"
while [ -L "$SELF" ]; do
    target="$(readlink "$SELF")"
    case "$target" in
        /*) SELF="$target" ;;
        *)  SELF="$(dirname "$SELF")/$target" ;;
    esac
done
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
MUSTER="$REPO/.venv/bin/muster"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }

[ -x "$MUSTER" ] || {
    printf "\033[31mCannot find muster at %s\033[0m\n\n" "$MUSTER"
    read -n 1 -s -r -p "Press any key to close."; exit 1
}

HOME_DIR="${MUSTER_HOME:-$HOME/.muster}"

if [ -f "$HOME_DIR/paused" ]; then
    bold "Muster is paused — resuming"
    echo
    "$MUSTER" resume
    echo
    echo "Background syncing and embedding will carry on from where they stopped."
else
    bold "Muster is running — pausing"
    echo
    "$MUSTER" pause
    echo
    echo "Your machine is your own again. Double-click this file to resume."
fi

echo
read -n 1 -s -r -p "Press any key to close this window."
