"""Failure webhook: which failed runs are sent, what is sent, and that a
receiver that is down never affects the run."""

import asyncio
import json
import textwrap
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from exuvia import notify
from exuvia.workspace import Workspace

HOOK = "https://hooks.test/T000/B000"
T0 = datetime(2026, 1, 5, 9, 1).timestamp()  # a Monday, 09:01 local


class Receiver:
    """Records every POST; can be told to fail."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.urls: list[str] = []
        self.fail: str | None = None  # "500" or "connect"

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        self.urls.append(str(request.url))
        self.bodies.append(json.loads(request.content))
        if self.fail == "500":
            return httpx.Response(500, text="receiver down")
        return httpx.Response(200, text="ok")


@pytest.fixture
def receiver(monkeypatch):
    rec = Receiver()
    monkeypatch.setattr(notify, "_transport", httpx.MockTransport(rec.handle))
    monkeypatch.delenv("EXUVIA_FAILURE_WEBHOOK", raising=False)
    monkeypatch.delenv("EXUVIA_FAILURE_TRIGGERS", raising=False)
    monkeypatch.delenv("EXUVIA_PUBLIC_URL", raising=False)
    yield rec


def node(root: Path, name: str, body: str) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / f"{name}.py").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def project(root: Path, **config) -> None:
    root.mkdir(parents=True, exist_ok=True)
    cfg = {"name": "desk", "params": {"as_of": {"type": "date", "default": "2026-03-02"}}, **config}
    (root / "exuvia.json").write_text(json.dumps(cfg), encoding="utf-8")
    node(
        root,
        "boom",
        '''
        import exuvia

        @exuvia.node(schedule="*/5 * * * *", params=["as_of"])
        def boom():
            raise ValueError("feed is empty for " + exuvia.param("as_of"))
        ''',
    )
    node(
        root,
        "ok",
        '''
        import exuvia

        @exuvia.node()
        def ok():
            return {"x": 1}
        ''',
    )
    node(
        root,
        "after",
        '''
        import exuvia

        @exuvia.node()
        def after(boom):
            return boom
        ''',
    )


def drive(ws, coro_fn):
    """Run on a loop, let spawned run tasks finish, then wait for deliveries."""

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        res = await coro_fn()
        for _ in range(100):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending)
        return res

    res = asyncio.run(go())
    notify.wait()
    return res


def run(ws, targets, **kw):
    return drive(ws, lambda: ws.run(targets, **kw))


def scheduled_fire(ws):
    async def ticks():
        assert await ws.tick(T0) == []  # first sight starts the clock
        return await ws.tick(T0 + 4 * 60)  # 09:05

    return drive(ws, ticks)


def test_scheduled_failure_posts_the_run(tmp_path, receiver, monkeypatch):
    monkeypatch.setenv("EXUVIA_PUBLIC_URL", "https://exuvia.example.com/")
    project(tmp_path, failure_webhook=HOOK)
    ws = Workspace(tmp_path)
    assert scheduled_fire(ws) == ["boom"]

    assert receiver.urls == [HOOK]
    (body,) = receiver.bodies
    run = ws.store.runs_for("boom")[0]
    assert run["status"] == "failed"
    assert body["event"] == "run.failed"
    assert body["project"] == "desk"
    assert body["node"] == "boom"
    assert body["run_id"] == run["id"]
    assert body["trigger"] == "schedule"
    assert body["status"] == "failed"
    assert "ValueError: feed is empty for 2026-03-02" in body["error"]
    assert body["params"] == {"as_of": "2026-03-02"}
    assert body["queued_at"].endswith("+00:00") and body["finished_at"] >= body["queued_at"]
    assert body["url"] == f"https://exuvia.example.com/api/runs/{run['id']}"
    # Slack-ready: one line naming what broke, the exception, then the link.
    assert body["text"].startswith("exuvia: desk / boom failed (schedule): ValueError: feed is empty")
    assert body["text"].endswith(body["url"])


def test_success_manual_and_skipped_runs_are_quiet(tmp_path, receiver):
    project(tmp_path, failure_webhook=HOOK)
    ws = Workspace(tmp_path)
    assert run(ws, ["ok"], trigger="schedule")["ok"]["status"] == "success"
    assert run(ws, ["boom"])["boom"]["status"] == "failed"  # manual: someone is watching
    assert receiver.bodies == []

    # A scheduled failure is reported once, for the node that broke, not for
    # the downstream node skipped because of it.
    finals = run(ws, ["after"], force={"boom"}, trigger="schedule")
    assert finals["after"]["status"] == "skipped"
    assert [b["node"] for b in receiver.bodies] == ["boom"]
    assert receiver.bodies[0]["url"] is None  # no EXUVIA_PUBLIC_URL, no link


def test_triggers_option_and_cascade(tmp_path, receiver):
    project(tmp_path, failure_webhook={"url": HOOK, "triggers": "all"})
    ws = Workspace(tmp_path)
    run(ws, ["boom"])
    assert [b["trigger"] for b in receiver.bodies] == ["manual"]

    project(tmp_path, failure_webhook={"url": HOOK, "triggers": ["backfill"]})
    run(ws, ["boom"], force={"boom"}, trigger="cascade")
    run(ws, ["boom"], force={"boom"}, trigger="backfill")
    assert [b["trigger"] for b in receiver.bodies] == ["manual", "backfill"]


def test_env_fallback_and_project_opt_out(tmp_path, receiver, monkeypatch):
    monkeypatch.setenv("EXUVIA_FAILURE_WEBHOOK", HOOK + "/hub")
    project(tmp_path)
    ws = Workspace(tmp_path)
    run(ws, ["boom"], trigger="cascade")
    assert receiver.urls == [HOOK + "/hub"]

    project(tmp_path, failure_webhook=False)  # this project opts out of the hub's hook
    run(ws, ["boom"], force={"boom"}, trigger="cascade")
    assert len(receiver.urls) == 1


@pytest.mark.parametrize("fail", ["500", "connect"])
def test_a_receiver_that_is_down_does_not_break_the_run(tmp_path, receiver, capsys, fail):
    receiver.fail = fail
    project(tmp_path, failure_webhook=HOOK)
    ws = Workspace(tmp_path)
    finals = run(ws, ["boom"], trigger="schedule")
    assert finals["boom"]["status"] == "failed"
    assert ws.store.run(finals["boom"]["id"])["status"] == "failed"
    err = capsys.readouterr().err
    assert "failure webhook for boom" in err
    # The workspace keeps working after a failed delivery.
    assert run(ws, ["ok"], trigger="schedule")["ok"]["status"] == "success"


def test_runs_interrupted_by_a_restart_are_not_sent(tmp_path, receiver):
    project(tmp_path, failure_webhook=HOOK)
    ws = Workspace(tmp_path)
    run_id = ws.store.create_run("boom", "b1", "schedule")
    ws.store.close()
    ws2 = Workspace(tmp_path)  # marks the orphaned run failed at startup
    notify.wait()
    assert ws2.store.run(run_id)["status"] == "failed"
    assert receiver.bodies == []


def test_long_errors_are_trimmed():
    body = notify.payload("p", {"node": "n", "id": "r", "trigger": "schedule", "error": "x" * 10_000})
    assert len(body["error"]) == notify.MAX_ERROR
    assert len(body["text"]) < 400


def test_hub_projects_link_to_their_own_api(tmp_path, receiver, monkeypatch):
    from exuvia.hub import Hub

    monkeypatch.setenv("EXUVIA_PUBLIC_URL", "https://hub.example.com")
    project(tmp_path / "desk", failure_webhook=HOOK)

    async def go():
        hub = Hub(tmp_path)
        try:
            ctx = await hub.get("desk")
            await ctx.ws.run(["boom"], trigger="schedule")
        finally:
            await hub.close()

    asyncio.run(go())
    notify.wait()
    (body,) = receiver.bodies
    assert body["url"] == f"https://hub.example.com/p/desk/api/runs/{body['run_id']}"
