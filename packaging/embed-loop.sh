#!/bin/bash
# Embed the backlog in fresh processes.
#
# A single long-lived embed run wedges: it drops to 0% CPU part-way through and
# stops making progress, reproducibly, on this machine. Whatever accumulates —
# MPS allocator, model state — a new process does not inherit, so the backlog is
# worked through in slices instead, each one a clean start.
#
# Every slice honours the pause switch, and the loop itself stops when paused,
# so `muster pause` still stops everything.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MUSTER="$REPO/.venv/bin/muster"
HOME_DIR="${MUSTER_HOME:-$HOME/.muster}"
SLICE="${1:-3000}"

pending() {
  "$REPO/.venv/bin/python" -c "
from muster.db import connect
from muster.config import load_config
from muster.embed import count_pending
cfg = load_config()
print(count_pending(connect(cfg.db_path), cfg.embed.model_id))"
}

while :; do
    if [ -f "$HOME_DIR/paused" ]; then
        echo "$(date '+%H:%M:%S') paused; stopping the loop"
        exit 0
    fi
    left="$(pending)"
    if [ "$left" -le 0 ] 2>/dev/null; then
        echo "$(date '+%H:%M:%S') done — nothing left to embed"
        exit 0
    fi
    echo "$(date '+%H:%M:%S') $left remaining; embedding up to $SLICE"
    "$MUSTER" embed --limit "$SLICE" 2>&1 | tail -2
done
