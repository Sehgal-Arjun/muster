"""The lock and the scheduled job's refusals.

Both exist so the sync can be run on a timer without becoming a nuisance, and
both are the kind of thing that is only ever exercised by accident in
production. The refusals are the interesting part: declining to run has to be
reliable, or a fifteen-minute timer stacks runs until the machine gives up.
"""

from __future__ import annotations

import multiprocessing
import plistlib

import pytest

from muster import scheduling
from muster.locking import LockHeld, sync_lock


def test_a_second_holder_is_refused_immediately(tmp_path) -> None:
    lock = tmp_path / "sync.lock"
    with sync_lock(lock):
        with pytest.raises(LockHeld):
            with sync_lock(lock):
                pass


def test_the_lock_is_released_when_the_block_exits(tmp_path) -> None:
    lock = tmp_path / "sync.lock"
    with sync_lock(lock):
        pass
    with sync_lock(lock):
        pass  # would raise if the first hold leaked


def _hold(path, ready, release):
    from muster.locking import sync_lock

    with sync_lock(path):
        ready.set()
        release.wait(timeout=10)


def test_the_lock_excludes_other_processes(tmp_path) -> None:
    """flock, not a pid file: this has to hold across processes, not threads."""
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    lock = tmp_path / "sync.lock"
    holder = ctx.Process(target=_hold, args=(lock, ready, release))
    holder.start()
    try:
        assert ready.wait(timeout=15), "helper never acquired the lock"
        with pytest.raises(LockHeld):
            with sync_lock(lock):
                pass
    finally:
        release.set()
        holder.join(timeout=15)


def test_a_dead_holder_does_not_leave_the_lock_stuck(tmp_path) -> None:
    """The kernel releases flock on exit, which a pid file would not."""
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    lock = tmp_path / "sync.lock"
    holder = ctx.Process(target=_hold, args=(lock, ready, release))
    holder.start()
    assert ready.wait(timeout=15)
    holder.terminate()
    holder.join(timeout=15)

    with sync_lock(lock):
        pass


# --- the agent ------------------------------------------------------------


def test_the_agent_never_asks_to_wake_the_machine() -> None:
    """StartCalendarInterval or WakeSystem here would wake a sleeping laptop."""
    spec = scheduling.plist(__import__("pathlib").Path("/usr/local/bin/muster"))
    assert "StartCalendarInterval" not in spec
    assert "WakeSystem" not in spec
    assert spec["StartInterval"] == 900


def test_the_agent_runs_at_background_priority() -> None:
    spec = scheduling.plist(__import__("pathlib").Path("/usr/local/bin/muster"))
    assert spec["ProcessType"] == "Background"
    assert spec["LowPriorityIO"] is True


def test_the_agent_does_not_sync_the_moment_it_is_installed() -> None:
    """Installing a schedule should not start a backfill nobody asked for."""
    spec = scheduling.plist(__import__("pathlib").Path("/usr/local/bin/muster"))
    assert spec["RunAtLoad"] is False


def test_the_agent_is_valid_plist() -> None:
    spec = scheduling.plist(__import__("pathlib").Path("/usr/local/bin/muster"))
    assert plistlib.loads(plistlib.dumps(spec)) == spec


# --- battery --------------------------------------------------------------


@pytest.mark.parametrize(
    ("on_battery", "percent", "expected"),
    [
        (True, 5, True),
        (True, 19, True),
        (True, 20, False),      # the floor itself is allowed
        (True, 80, False),
        (False, 5, False),      # on mains, a flat battery is irrelevant
        (True, None, False),    # unreadable: do the work rather than guess
    ],
)
def test_when_the_scheduled_sync_stands_down(on_battery, percent, expected) -> None:
    assert scheduling.Power(on_battery=on_battery, percent=percent).too_low is expected


def test_power_parses_pmset(monkeypatch) -> None:
    import subprocess

    sample = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=12345)\t57%; discharging; 3:21 remaining present: true\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, sample, "")
    )
    power = scheduling.power()
    assert power.on_battery is True
    assert power.percent == 57


def test_power_treats_unreadable_output_as_mains(monkeypatch) -> None:
    import subprocess

    def boom(*a, **k):
        raise OSError("no pmset here")

    monkeypatch.setattr(subprocess, "run", boom)
    assert scheduling.power().on_battery is False


def test_the_scheduled_pass_holds_the_lock_for_its_whole_duration(tmp_path, monkeypatch) -> None:
    """Probing the lock and releasing it is the same as not locking at all.

    The first walk of a large Documents folder runs far longer than the
    fifteen-minute timer, so a released lock means the next run starts on top of
    the last — both walking the same tree and writing the same rows.
    """
    from muster import cli
    from muster.locking import LockHeld, sync_lock

    cfg = cli.load_config()
    held_during_work: list[bool] = []

    def check_lock() -> bool:
        """True if the lock is held by somebody — including us."""
        try:
            with sync_lock(cfg.lock_path):
                return False
        except LockHeld:
            return True

    monkeypatch.setattr(
        cli, "_run_incremental",
        lambda **kw: (held_during_work.append(check_lock()), 0)[1],
    )
    monkeypatch.setattr(
        cli, "_run_file_pass",
        lambda: (held_during_work.append(check_lock()), 0)[1],
    )
    import muster.scheduling as sched
    monkeypatch.setattr(sched, "power", lambda: sched.Power(on_battery=False, percent=100))

    cli.schedule_run()

    assert held_during_work == [True, True], "the lock was not held across the pass"


def test_the_scheduled_pass_stands_down_when_the_lock_is_held(monkeypatch, capsys) -> None:
    from muster import cli
    from muster.locking import sync_lock
    import muster.scheduling as sched

    cfg = cli.load_config()
    monkeypatch.setattr(sched, "power", lambda: sched.Power(on_battery=False, percent=100))
    ran = []
    monkeypatch.setattr(cli, "_run_incremental", lambda **kw: ran.append("gmail") or 0)
    monkeypatch.setattr(cli, "_run_file_pass", lambda: ran.append("files") or 0)

    with sync_lock(cfg.lock_path):
        cli.schedule_run()

    assert ran == [], "it ran anyway while another sync held the lock"
    assert "skipped" in capsys.readouterr().out
