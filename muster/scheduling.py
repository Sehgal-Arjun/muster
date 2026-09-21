"""The scheduled sync: a launchd agent that runs every fifteen minutes.

Three things keep it from being a nuisance. It never wakes the machine —
``StartInterval`` fires on the next wake rather than scheduling one, and nothing
here sets ``WakeSystem``. It runs at background priority, so it competes with
nothing the user is doing. And it declines to run at all when the battery is low
or another sync is already going, because a search index that is fifteen minutes
stale costs nothing and a laptop that will not last the afternoon costs a lot.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "dev.muster.desktop.sync"
INTERVAL_SECONDS = 900
# Below this, on battery, the sync waits for mains. Chosen to sit above the
# level where macOS starts throttling and warning, not tuned.
BATTERY_FLOOR_PERCENT = 20


def agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_dir() -> Path:
    return Path.home() / ".muster" / "logs"


def muster_binary() -> Path:
    """The interpreter's own muster, so the agent cannot pick up a different one."""
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "muster" and candidate.exists():
        return candidate
    return Path(sys.executable).parent / "muster"


def plist(binary: Path, roots: tuple[str, ...] = ()) -> dict:
    logs = log_dir()
    spec = {
        "Label": LABEL,
        "ProgramArguments": [str(binary), "schedule", "run"],
        # Fires this often while awake. Deliberately NOT StartCalendarInterval:
        # that can wake a sleeping machine, and no index is worth that.
        "StartInterval": INTERVAL_SECONDS,
        # Not at load: installing the agent should not kick off a sync the user
        # did not ask for, possibly on battery, possibly a first full backfill.
        "RunAtLoad": False,
        # Background priority for both CPU and disk. A sync must never be the
        # reason something else feels slow.
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(logs / "sync.log"),
        "StandardErrorPath": str(logs / "sync.log"),
        # Long enough that a slow network does not produce a crash-loop.
        "ThrottleInterval": 60,
    }

    # Deleting a file should be noticed when it happens, not up to fifteen
    # minutes later. launchd watches the directories itself, so this costs
    # nothing while nothing changes and needs no watcher process of our own.
    #
    # Not recursive — launchd watches the named directories only — so a change
    # deep in a tree still waits for the timer. It catches the common case:
    # things are added to and removed from the top of Documents and Downloads.
    watched = [str(Path(root).expanduser()) for root in roots]
    watched = [path for path in watched if Path(path).is_dir()]
    if watched:
        spec["WatchPaths"] = watched
    return spec


def install(binary: Path | None = None, roots: tuple[str, ...] = ()) -> Path:
    """Write and load the agent. Safe to re-run."""
    binary = binary or muster_binary()
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist(binary, roots), handle)

    domain = f"gui/{os.getuid()}"
    # bootout first: launchd refuses a label already loaded, and this has to be
    # safe to run again after an upgrade.
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    return path


def uninstall() -> bool:
    """Unload and remove the agent. True if there was one."""
    path = agent_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True
    )
    if path.exists():
        path.unlink()
        return True
    return False


def installed() -> bool:
    return agent_path().exists()


@dataclass(frozen=True)
class Power:
    on_battery: bool
    percent: int | None

    @property
    def too_low(self) -> bool:
        return self.on_battery and self.percent is not None and self.percent < BATTERY_FLOOR_PERCENT


def power() -> Power:
    """Battery state via pmset.

    Unreadable output is treated as mains power: refusing to sync because we
    could not parse a status line would be worse than the occasional sync on a
    low battery.
    """
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return Power(on_battery=False, percent=None)

    on_battery = "Battery Power" in out
    percent = None
    for token in out.replace(";", " ").split():
        if token.endswith("%") and token[:-1].isdigit():
            percent = int(token[:-1])
            break
    return Power(on_battery=on_battery, percent=percent)
