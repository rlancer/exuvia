"""Persisted, catch-up scheduling (`Workspace.tick`), without running nodes."""

import asyncio
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

from exuvia.workspace import Workspace

T0 = datetime(2026, 1, 5, 9, 1).timestamp()  # a Monday, 09:01 local
MIN = 60


def make(tmp_path, cron="*/5 * * * *", extra="", calls=None):
    (tmp_path / "nodes").mkdir(exist_ok=True)
    (tmp_path / "nodes" / "src.py").write_text(
        textwrap.dedent(f'''
            import exuvia

            @exuvia.node(schedule="{cron}"{extra})
            def src():
                return {{"ok": True}}
        '''),
        encoding="utf-8",
    )
    ws = Workspace(tmp_path)
    fired: list[str] = []

    async def fake_run(targets, **kw):
        fired.extend(targets)
        if calls is not None:
            calls.append(kw)
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


def test_scheduled_run_gets_its_slot_date_not_the_date_it_ran(tmp_path):
    # Friday's 16:30 fire is missed while the host sleeps; the catch-up run on
    # Monday morning computes Friday, the day it was due for.
    calls: list[dict] = []
    ws, _ = make(tmp_path, cron="30 16 * * 1-5", calls=calls)
    ws.set_settings(timezone="America/New_York")
    ws.set_param("as_of", {"type": "date", "default": "today"})
    ws.set_param("region", {"type": "string", "default": "us"})
    tick(ws, _et(2026, 11, 13, 9, 0))
    assert tick(ws, _et(2026, 11, 16, 9, 0)) == ["src"]
    assert calls[-1]["params"] == {"as_of": "2026-11-13"}  # Friday, other params default
    assert calls[-1]["trigger"] == "schedule"
    assert tick(ws, _et(2026, 11, 16, 16, 30, 3)) == ["src"]  # on time (a tick a few seconds late)
    assert calls[-1]["params"] == {"as_of": "2026-11-16"}


def test_slot_date_uses_the_project_timezone(tmp_path):
    # 23:30 in New York is already the next day in UTC.
    calls: list[dict] = []
    ws, _ = make(tmp_path, cron="30 23 * * *", calls=calls)
    ws.set_settings(timezone="America/New_York")
    ws.set_param("as_of", {"type": "date", "default": "today"})
    tick(ws, _et(2026, 11, 13, 12, 0))
    assert tick(ws, _et(2026, 11, 14, 0, 10)) == ["src"]  # run 40 minutes late
    assert calls[-1]["params"] == {"as_of": "2026-11-13"}


def test_a_fixed_date_default_is_left_alone(tmp_path):
    calls: list[dict] = []
    ws, _ = make(tmp_path, calls=calls)
    ws.set_param("as_of", {"type": "date", "default": "2026-01-02"})
    tick(ws, T0)
    assert tick(ws, T0 + 4 * MIN) == ["src"]
    assert calls[-1]["params"] == {}


def test_a_fire_while_running_waits_for_the_run_by_default(tmp_path):
    ws, fired = make(tmp_path)
    tick(ws, T0)
    ws._inflight[("src", "")] = "r1"  # a run of src is in progress
    assert tick(ws, T0 + 4 * MIN) == []  # 09:05 is due but waits
    # An external scheduler is told to look again soon, not "now".
    assert ws.next_fire_at(T0 + 4 * MIN) == T0 + 4 * MIN + 30
    assert tick(ws, T0 + 6 * MIN) == []
    del ws._inflight[("src", "")]
    assert tick(ws, T0 + 7 * MIN) == ["src"]  # the run ended: the queued fire runs
    assert tick(ws, T0 + 8 * MIN) == []  # once, not once per tick it waited
    assert tick(ws, T0 + 9 * MIN) == ["src"]  # 09:10, back on the clock
    assert fired == ["src", "src"]


def test_overlap_skip_drops_a_fire_while_running(tmp_path):
    ws, fired = make(tmp_path, extra=', overlap="skip"')
    tick(ws, T0)
    ws._inflight[("src", "")] = "r1"
    assert tick(ws, T0 + 4 * MIN) == []  # 09:05 dropped
    assert ws.next_fire_at(T0 + 4 * MIN) == datetime(2026, 1, 5, 9, 10).timestamp()
    del ws._inflight[("src", "")]
    assert tick(ws, T0 + 7 * MIN) == []
    assert tick(ws, T0 + 9 * MIN) == ["src"]  # 09:10
    assert fired == ["src"]


def test_scheduled_runs_are_tracked_and_stopped(tmp_path):
    ws, _ = make(tmp_path)
    started = asyncio.Event()

    async def slow_run(targets, **kw):
        started.set()
        await asyncio.sleep(3600)

    ws.run = slow_run

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        await ws.tick(T0)
        await ws.tick(T0 + 4 * MIN)
        await started.wait()
        assert len(ws._tasks) == 1  # held until it ends
        task = ws._tasks[0]
        await ws.stop()
        assert task.cancelled()
        assert ws._tasks == []

    asyncio.run(go())


# -- the schedule at a glance (GET /api/schedule) -------------------------------------
def test_schedule_lists_scheduled_nodes_soonest_first_with_their_next_fires(tmp_path):
    ws, _ = make(tmp_path)  # src: every 5 minutes
    (tmp_path / "nodes" / "daily.py").write_text(
        'import exuvia\n\n@exuvia.node(schedule="0 18 * * *")\ndef daily(src):\n    return 1\n', encoding="utf-8"
    )
    (tmp_path / "nodes" / "later.py").write_text(
        'import exuvia\n\n@exuvia.node()\ndef later(daily):\n    return 1\n', encoding="utf-8"
    )
    ws.reload()
    tick(ws, T0)  # starts src's clock at 09:01
    out = ws.schedule(upcoming=3, now=T0)
    assert [n["name"] for n in out["nodes"]] == ["src", "daily"]
    src, daily = out["nodes"]
    at = lambda ts: datetime.fromtimestamp(ts).strftime("%a %H:%M")  # noqa: E731
    assert [at(t) for t in src["upcoming"]] == ["Mon 09:05", "Mon 09:10", "Mon 09:15"]
    assert src["next"] == src["upcoming"][0]
    assert at(daily["next"]) == "Mon 18:00"  # no clock yet: from now on
    assert src["cascades"] == ["daily", "later"] and daily["cascades"] == ["later"]
    assert (src["cron"], src["source"], src["paused"], src["last"]) == ("*/5 * * * *", "code", False, None)
    assert out["live"] == [] and out["scheduler"] in ("internal", "external")


def test_a_paused_schedule_has_no_next_fire(tmp_path):
    ws, _ = make(tmp_path)
    ws.set_paused("src", True)
    (row,) = ws.schedule(now=T0)["nodes"]
    assert (row["paused"], row["next"], row["upcoming"]) == (True, None, [])


def test_schedule_names_the_last_scheduled_run_and_the_live_nodes(tmp_path):
    ws, _ = make(tmp_path)
    run_id = ws.store.create_run("src", "b1", "schedule", {}, "", None, None)
    ws.store.create_run("src", "b2", "manual", {}, "", None, None)
    (tmp_path / "nodes" / "quotes.py").write_text(
        'import exuvia\n\n@exuvia.node(live=60)\ndef quotes():\n    return 1\n', encoding="utf-8"
    )
    (tmp_path / "views" / "board").mkdir(parents=True)
    (tmp_path / "views" / "board" / "view.json").write_text('{"reads": ["quotes"]}', encoding="utf-8")
    (tmp_path / "views" / "board" / "App.tsx").write_text("export default () => null;\n", encoding="utf-8")
    ws.reload()
    out = ws.schedule(now=T0)
    assert out["nodes"][0]["last"]["id"] == run_id
    assert [(n["name"], n["live"], n["views"]) for n in out["live"]] == [("quotes", 60.0, ["board"])]


def test_schedule_route(tmp_path):
    from fastapi.testclient import TestClient

    from exuvia.server import create_app

    make(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        body = client.get("/api/schedule").json()
    assert [n["name"] for n in body["nodes"]] == ["src"] and len(body["nodes"][0]["upcoming"]) == 5
