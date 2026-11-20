"""Persisted, catch-up scheduling (`Workspace.tick`), without running nodes."""

import asyncio
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

from exuvia.workspace import Workspace

T0 = datetime(2026, 1, 5, 9, 1).timestamp()  # a Monday, 09:01 local
MIN = 60


def make(tmp_path, cron="*/5 * * * *"):
    (tmp_path / "nodes").mkdir(exist_ok=True)
    (tmp_path / "nodes" / "src.py").write_text(
        textwrap.dedent(f'''
            import exuvia

            @exuvia.node(schedule="{cron}")
            def src():
                return {{"ok": True}}
        '''),
        encoding="utf-8",
    )
    ws = Workspace(tmp_path)
    fired: list[str] = []

    async def fake_run(targets, **kw):
        fired.extend(targets)
        return {}

    ws.run = fake_run  # scheduling only; execution is covered elsewhere
    return ws, fired


def tick(ws, now):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        due = await ws.tick(now)
        await asyncio.sleep(0)  # let created run tasks start
        return due

    return asyncio.run(go())


def test_first_sight_does_not_fire_then_fires_on_cron(tmp_path):
    ws, fired = make(tmp_path)
    assert tick(ws, T0) == []  # first seen at 09:01 — wait for 09:05
    assert tick(ws, T0 + 2 * MIN) == []
    assert tick(ws, T0 + 4 * MIN) == ["src"]  # 09:05
    assert tick(ws, T0 + 5 * MIN) == []
    assert fired == ["src"]


def test_missed_fires_coalesce_after_sleep(tmp_path):
    ws, fired = make(tmp_path)
    tick(ws, T0)
    assert tick(ws, T0 + 60 * MIN) == ["src"]  # 12 slots missed -> one catch-up run
    assert tick(ws, T0 + 61 * MIN) == []


def test_state_survives_restart(tmp_path):
    ws, _ = make(tmp_path)
    tick(ws, T0)
    ws2, fired = make(tmp_path)  # new process, same .exuvia/
    assert tick(ws2, T0 + 4 * MIN) == ["src"]
    assert fired == ["src"]


def test_pause_skips_backlog(tmp_path):
    ws, fired = make(tmp_path)
    tick(ws, T0)
    ws.set_paused("src", True)
    assert tick(ws, T0 + 30 * MIN) == []
    ws.set_paused("src", False)
    assert tick(ws, T0 + 31 * MIN) == []  # no burst of paused-time fires
    assert tick(ws, T0 + 34 * MIN) == ["src"]  # next real slot (09:35)
    assert fired == ["src"]


def test_cron_change_restarts_the_clock(tmp_path):
    ws, _ = make(tmp_path)
    tick(ws, T0)
    make(tmp_path, cron="0 * * * *")  # rewrite to hourly
    ws.reload()
    assert tick(ws, T0 + 30 * MIN) == []  # re-seen, not fired
    assert tick(ws, T0 + 59 * MIN) == ["src"]  # 10:00


def test_clock_changes_move_the_change_counter(tmp_path):
    # A host that saves on change must see a schedule's clock start and each
    # fire (even a skipped one), or a restart would forget them.
    ws, _ = make(tmp_path)
    tick(ws, T0)
    started = ws.bus.version
    assert started > 0  # the clock started
    tick(ws, T0 + 2 * MIN)
    assert ws.bus.version == started  # nothing due: nothing to save
    ws.set_paused("src", True)
    paused = ws.bus.version
    assert tick(ws, T0 + 4 * MIN) == []  # 09:05 passes while paused
    assert ws.bus.version > paused


def _et(*args):
    return datetime(*args, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_weekday_schedule_sleeps_over_the_weekend(tmp_path):
    # A host that sleeps between fires (the ChatV2 workflows host) asks for
    # the next fire time, sleeps until then, then ticks a fresh process.
    ws, _ = make(tmp_path, cron="30 16 * * 1-5")
    ws.set_settings(timezone="America/New_York")
    tick(ws, _et(2026, 11, 13, 9, 0))  # Friday morning: clock starts
    assert tick(ws, _et(2026, 11, 13, 16, 30)) == ["src"]
    monday = _et(2026, 11, 16, 16, 30)
    assert ws.next_fire_at(_et(2026, 11, 13, 16, 31)) == monday  # not Saturday or Sunday
    ws2, fired = make(tmp_path, cron="30 16 * * 1-5")  # woken Monday from saved state
    assert tick(ws2, monday - 1) == []
    assert tick(ws2, monday) == ["src"]
    assert fired == ["src"]


def test_weekday_schedule_keeps_wall_time_across_dst(tmp_path):
    # US clocks spring forward on Sunday 2026-03-08: Monday's 16:30 ET is an
    # hour earlier in UTC than Friday's.
    ws, _ = make(tmp_path, cron="30 16 * * 1-5")
    ws.set_settings(timezone="America/New_York")
    tick(ws, _et(2026, 3, 6, 9, 0))
    assert tick(ws, _et(2026, 3, 6, 16, 30)) == ["src"]
    nxt = ws.next_fire_at(_et(2026, 3, 6, 16, 31))
    assert nxt == _et(2026, 3, 9, 16, 30)
    assert nxt - _et(2026, 3, 6, 16, 30) == 3 * 24 * 3600 - 3600


def test_status_reports_next_fire(tmp_path):
    ws, _ = make(tmp_path)
    tick(ws, T0)
    nxt = ws.status()["src"]["schedule"]["next"]
    assert nxt == datetime(2026, 1, 5, 9, 5).timestamp()
