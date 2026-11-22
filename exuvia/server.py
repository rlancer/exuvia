"""HTTP API + static UI. Everything the UI does goes through these routes, so
another app (e.g. Lobster) can drive exuvia headlessly or embed the UI.

Two shapes, one set of project routes:

* ``create_app(root)`` — one project, routes under ``/api`` (``exuvia serve``).
* ``create_hub_app(dir)`` — many projects, each under ``/p/{project}/api``,
  plus hub routes to create/delete projects, tick every schedule at once, and
  export/import all state (``exuvia hub``). This is what a host app runs when
  each of its documents is a exuvia project.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import html
import json
import os
import re
import time as _time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _STARTED, __version__, about as about_mod, auth, mcp_http
from .agent import TOOLS, Agent
from .arrowstream import MEDIA_TYPE as ARROW_STREAM, ArrowStream, wants_arrow
from .packages import PackagesError
from .project import NODE_NAME, PARAM_TYPES, coerce_param
from .report import graph_report, node_report
from .store import clean_origin
from .users import UserStore, looks_like_token, open_users
from .workspace import SCHEDULER, Workspace, acting_as

STATIC = Path(__file__).parent / "static"
BACKFILL_MAX = 400
RUN_WAIT_MAX = 300.0
# Stored output files by extension (see runner.save_artifact), and how each is served.
ARTIFACT_KINDS = {"parquet": "table", "json": "json", "zip": "multi"}
ARTIFACT_MEDIA_TYPES = {"table": "application/vnd.apache.parquet", "json": "application/json", "multi": "application/zip"}


async def _await_run(task: "asyncio.Task[dict[str, Any]]", wait: float) -> dict[str, Any] | None:
    """The run's finals, or None when it is still going after ``wait`` seconds
    (it keeps running: the wait is shielded)."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=min(max(wait, 0.0), RUN_WAIT_MAX))
    except asyncio.TimeoutError:
        return None


class NodeWrite(BaseModel):
    source: str
    kind: str | None = None


class NodeRestore(BaseModel):
    hash: str


class ViewRestore(BaseModel):
    path: str  # relative to the view folder, e.g. App.tsx
    hash: str


async def _origin(request: Request) -> Any:
    """Code changed by this request is recorded as ``X-Exuvia-Origin``'s
    (a host can say ``ui`` or ``mcp``), or as ``api``."""
    with acting_as(request.headers.get("x-exuvia-origin") or "api"):
        yield


class FileWrite(BaseModel):
    source: str


class NodeCreate(BaseModel):
    name: str
    kind: str = "python"
    deps: list[str] = []
    description: str = ""
    source: str | None = None


class RunReq(BaseModel):
    force: bool = True
    params: dict[str, Any] | None = None
    # Seconds to wait for the run to finish and report on it (see report.py);
    # unset returns as soon as the run starts. The run continues past the wait.
    wait: float | None = None


class RunAllReq(BaseModel):
    targets: list[str] | None = None
    force: bool = False
    params: dict[str, Any] | None = None
    wait: float | None = None


class BackfillReq(BaseModel):
    param: str
    values: list[Any] | None = None
    # Date parameters: an inclusive range instead of explicit values.
    start: str | None = None
    end: str | None = None
    weekdays: bool = True
    targets: list[str] | None = None
    params: dict[str, Any] | None = None
    force: bool = False


class UserPatch(BaseModel):
    role: str | None = None  # admin | member
    disabled: bool | None = None


class TokenReq(BaseModel):
    name: str = ""  # what the token is for: "laptop", "Claude Code", ...


class ScheduleReq(BaseModel):
    paused: bool | None = None
    cron: str | None = None  # "" turns the schedule off; null drops the override


class ParamReq(BaseModel):
    type: str = "string"
    default: Any = None
    description: str = ""


class SettingsReq(BaseModel):
    name: str | None = None
    timezone: str | None = None


class QueryReq(BaseModel):
    sql: str
    limit: int | None = None
    params: dict[str, Any] | None = None


class AgentReq(BaseModel):
    message: str
    session_id: str | None = None
    selected: str | None = None


class PackagesEdit(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class ManifestWrite(BaseModel):
    text: str


class ProjectCreate(BaseModel):
    id: str
    name: str | None = None
    template: str | None = None


class TemplateApply(BaseModel):
    template: str


@dataclass
class Ctx:
    """One loaded project: its workspace and its agent."""

    ws: Workspace
    agent: Agent


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _arrow(stream: ArrowStream) -> StreamingResponse:
    return StreamingResponse(stream, media_type=ARROW_STREAM, headers={"Cache-Control": "no-store"})


def _query_params(raw: str | None) -> dict[str, Any] | None:
    """GET routes take run parameters as a JSON object in ``?params=``."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, "`params` must be a JSON object") from e
    if not isinstance(value, dict):
        raise HTTPException(400, "`params` must be a JSON object")
    return value


def _resolve(ws: Workspace, params: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return ws.resolve_params(params)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _date_range(start: str, end: str, weekdays: bool) -> list[str]:
    try:
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as e:
        raise HTTPException(400, "start and end must be YYYY-MM-DD dates") from e
    if d1 < d0:
        raise HTTPException(400, "end is before start")
    out = []
    cur = d0
    while cur <= d1:
        if not weekdays or cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def project_router(get_ctx: Callable[..., Any]) -> APIRouter:
    """Every route that acts on one project. Mounted at ``/api`` for a single
    project and at ``/p/{project}/api`` in a hub."""
    r = APIRouter(dependencies=[Depends(_origin)])

    def _need(ws: Workspace, name: str) -> None:
        if name not in ws.graph.nodes and ws.project.path_for(name) is None:
            raise HTTPException(404, f"no node named {name}")

    @r.get("/config")
    def config(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return {
            "ai_enabled": ctx.agent.enabled,
            "model": ctx.agent.label,
            "project": ctx.ws.project.name,
            "executor": type(ctx.ws.executor).__name__,
            "scheduler": SCHEDULER,
            "version": __version__,
        }

    @r.post("/schedule/tick")
    async def schedule_tick(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Evaluate schedules now. Safe to call at any cadence; see Workspace.tick.
        ``next`` is when the following tick has work (null: nothing scheduled)."""
        started = await ctx.ws.tick()
        return {"started": started, "next": ctx.ws.next_fire_at()}

    @r.get("/guide")
    def guide(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """How to write this project's nodes: the built-in agent's own
        instructions (exuvia conventions, parameters, project notes) and its
        tool list, for an outside agent that edits the project over HTTP
        (each tool runs at ``POST /tools/{name}``)."""
        tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in TOOLS]
        return {"instructions": ctx.agent.instructions(), "tools": tools}

    @r.post("/tools/{name}")
    async def tool_call(name: str, request: Request, args: Any = Body(default=None), ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Run one of the agent's tools (``/guide`` lists them) with ``args``
        as its input; ``exuvia mcp --url`` relays MCP tool calls here. A tool
        that fails, or an input it rejects, is still a 200 with ``ok: false``
        and ``text`` saying why, for the calling model to read. Changes and
        runs are recorded as ``X-Exuvia-Origin``'s (default ``api``)."""
        if not any(t["name"] == name for t in TOOLS):
            raise HTTPException(404, f"no tool {name}")
        origin = clean_origin(request.headers.get("x-exuvia-origin"), "api")
        ok, text, extra = await ctx.agent.call_tool(name, {} if args is None else args, origin)
        return {"ok": ok, "text": text, **extra}

    @r.get("/project")
    def project(params: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return ctx.ws.snapshot(_resolve(ctx.ws, _query_params(params)))

    @r.patch("/project")
    def settings(body: SettingsReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        try:
            ctx.ws.set_settings(name=body.name, timezone=body.timezone)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return ctx.ws.snapshot()

    # -- parameters ----------------------------------------------------------
    @r.get("/params")
    def params_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Declared parameters, their current values, and every value run so far."""
        ws = ctx.ws
        now, seen = _resolve(ws, None), ws.param_values()
        return {
            "params": [
                {"name": n, **spec, "value": now.get(n), "values": seen.get(n, [])}
                for n, spec in ws.project.params.items()
            ]
        }

    @r.put("/params/{name}")
    def param_put(name: str, body: ParamReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        if not NODE_NAME.match(name):
            raise HTTPException(400, "Parameter names must be snake_case identifiers.")
        if body.type not in PARAM_TYPES:
            raise HTTPException(400, f"type must be one of {', '.join(PARAM_TYPES)}")
        try:
            coerce_param(name, body.type, body.default, ctx.ws.project.today)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        ctx.ws.set_param(name, body.model_dump())
        return params_list(ctx)

    @r.delete("/params/{name}")
    def param_delete(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        ctx.ws.set_param(name, None)
        return params_list(ctx)

    # -- packages (the project's pyproject.toml; see packages.py) -------------------
    def _packages_error(e: Exception) -> HTTPException:
        return HTTPException(400, str(e).strip()[-4000:])

    @r.get("/packages")
    def packages_state(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The project's packages, their install status and log, and the
        agent's requests waiting for approval. Starts an install when the
        list changed on disk since the last one (an edit outside exuvia)."""
        ctx.ws.packages.prebuild()
        return ctx.ws.packages.state()

    @r.post("/packages")
    async def packages_edit(body: PackagesEdit, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Add requirements (``scipy``, ``acme-models>=2``; one without a
        version is pinned to the version it resolves to) and/or remove
        packages by name. The edit is checked against exuvia's own versions
        first: one that does not resolve is a 400 with uv's explanation and
        leaves pyproject.toml as it was. The install then runs in the
        background (``packages`` events)."""
        pk = ctx.ws.packages
        try:
            if body.remove:
                await asyncio.to_thread(pk.remove, body.remove)
            if body.add:
                await asyncio.to_thread(pk.add, body.add)
        except PackagesError as e:
            raise _packages_error(e) from e
        return pk.state()

    @r.put("/packages/manifest")
    async def packages_manifest(body: ManifestWrite, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Replace pyproject.toml as a whole, then install from it."""
        try:
            return await asyncio.to_thread(ctx.ws.packages.write_manifest, body.text)
        except PackagesError as e:
            raise _packages_error(e) from e

    @r.post("/packages/retry")
    def packages_retry(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Install the current list again after a failure (a registry that
        was down, credentials that were fixed)."""
        return ctx.ws.packages.retry()

    @r.post("/packages/requests/{request_id}/approve")
    async def packages_approve(request_id: int, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Add the packages an agent asked for."""
        try:
            return await asyncio.to_thread(ctx.ws.packages.approve, request_id)
        except KeyError:
            raise HTTPException(404, f"no package request {request_id}") from None
        except PackagesError as e:
            raise _packages_error(e) from e

    @r.delete("/packages/requests/{request_id}")
    def packages_dismiss(request_id: int, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        try:
            return ctx.ws.packages.dismiss(request_id)
        except KeyError:
            raise HTTPException(404, f"no package request {request_id}") from None

    # -- nodes -----------------------------------------------------------------
    @r.get("/nodes/{name}")
    def get_node(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        ws = ctx.ws
        _need(ws, name)
        node = ws.graph.nodes.get(name)
        return {"name": name, "source": ws.project.source(name), "node": node.to_dict() if node else None}

    @r.put("/nodes/{name}")
    def put_node(name: str, body: NodeWrite, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        with ctx.ws.editing():
            try:
                ctx.ws.project.write_node(name, body.source, body.kind)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            ctx.ws.reload()
        return ctx.ws.snapshot()

    @r.post("/nodes")
    def create_node(body: NodeCreate, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        ws = ctx.ws
        if not NODE_NAME.match(body.name):
            raise HTTPException(400, "Node names must be snake_case identifiers.")
        if ws.project.path_for(body.name):
            raise HTTPException(409, f"{body.name} already exists")
        src = body.source or ws.project.template(body.name, body.kind, body.deps, body.description)
        with ws.editing():
            ws.project.write_node(body.name, src, body.kind)
            ws.reload()
        return ws.snapshot()

    @r.delete("/nodes/{name}")
    def delete_node(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        _need(ctx.ws, name)
        with ctx.ws.editing():
            ctx.ws.project.delete_node(name)
            ctx.ws.reload()
        return ctx.ws.snapshot()

    # -- helper files (lib/**/*.py) ---------------------------------------------
    @r.get("/files")
    def files_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Shared helper modules under ``lib/`` and the nodes that import each."""
        return {"files": ctx.ws.helpers()}

    @r.get("/files/{path:path}")
    def file_get(path: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        try:
            return ctx.ws.read_helper(path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, f"no file {path}") from e

    @r.put("/files/{path:path}")
    def file_put(path: str, body: FileWrite, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        try:
            return ctx.ws.write_helper(path, body.source)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @r.delete("/files/{path:path}")
    def file_delete(path: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        try:
            return ctx.ws.delete_helper(path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, f"no file {path}") from e

    # -- views (views/<name>/: a manifest + UI source files) -----------------------
    def _view_call(fn: Callable[..., Any], *args: Any) -> Any:
        """Bad names and paths are 400, a view or file that is not there 404."""
        try:
            return fn(*args)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, f"not found: {e.args[0]}") from e

    @r.get("/views")
    def views_list(params: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Every view with its state for one parameter set (the same entries
        as ``views`` in ``GET /project``)."""
        return {"views": ctx.ws.views(_resolve(ctx.ws, _query_params(params)))}

    @r.get("/views/{name}")
    def view_get(name: str, params: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """One view: its entry, the parsed manifest and every file's source."""
        return _view_call(ctx.ws.read_view, name, _resolve(ctx.ws, _query_params(params)))

    @r.put("/views/{name}")
    def view_put(name: str, body: dict[str, Any] = Body(...), ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Create a view or replace its manifest (``view.json``)."""
        return _view_call(ctx.ws.write_view_manifest, name, body)

    @r.delete("/views/{name}")
    def view_delete(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return _view_call(ctx.ws.delete_view, name)

    @r.get("/views/{name}/files/{path:path}")
    def view_file_get(name: str, path: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return {"view": name, "path": path, "source": _view_call(ctx.ws.project.read_view_file, name, path)}

    @r.put("/views/{name}/files/{path:path}")
    def view_file_put(name: str, path: str, body: FileWrite, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return _view_call(ctx.ws.write_view_file, name, path, body.source)

    @r.delete("/views/{name}/files/{path:path}")
    def view_file_delete(name: str, path: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return _view_call(ctx.ws.delete_view_file, name, path)

    @r.post("/views/{name}/restore")
    def view_restore(name: str, body: ViewRestore, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Make a saved version (``{path, hash}``) of one of the view's files
        current again; brings back a deleted file or view."""
        return _view_call(ctx.ws.restore_view_file, name, body.path, body.hash)

    @r.get("/view-runtime/runtime.js", response_model=None)
    def view_runtime(ctx: Ctx = Depends(get_ctx)) -> FileResponse:
        """The views runtime: the script a host page posts into a view's
        sandboxed frame (``ui/vite.runtime.config.ts``). Served under the API
        so a page that embeds a headless exuvia (a hub behind a proxy) gets
        the runtime that matches the server it talks to."""
        f = STATIC / "view-runtime" / "runtime.js"
        if not f.is_file():
            raise HTTPException(404, "the views runtime is not built: run `bun run build` in exuvia/ui")
        return FileResponse(f, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    # What a view's iframe may read: only the nodes in its manifest's `reads`.
    @r.get("/views/{name}/data/{node}", response_model=None)
    def view_data(
        request: Request,
        name: str,
        node: str,
        limit: int | None = None,
        offset: int = 0,
        params: str | None = None,
        part: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """``GET /nodes/{node}/data`` for a view: 403 unless the view reads ``node``."""
        try:
            ctx.ws.view_node(name, node)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return data(request, node, limit=limit, offset=offset, params=params, run_id=None, part=part, ctx=ctx)

    @r.post("/views/{name}/query", response_model=None)
    def view_query(name: str, body: QueryReq, request: Request, ctx: Ctx = Depends(get_ctx)) -> Any:
        """``POST /query`` for a view: the SQL sees only the nodes in the
        view's ``reads`` (plus their ``__runs`` history), and the connection
        cannot open any other file, the network or an extension."""
        try:
            if wants_arrow(request.headers.get("accept")):
                return _arrow(ctx.ws.view_query_arrow(name, body.sql, limit=body.limit, params=body.params))
            rows = 500 if body.limit is None else min(body.limit, 5000)
            return ctx.ws.view_query(name, body.sql, limit=rows, params=body.params)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        except Exception as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}") from e

    @r.post("/nodes/{name}/run")
    async def run_node(name: str, body: RunReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        _need(ctx.ws, name)
        params = _resolve(ctx.ws, body.params)
        task = asyncio.create_task(ctx.ws.run([name], params=params, force={name} if body.force else set()))
        if body.wait is None:
            return {"ok": True, "params": params}
        finals = await _await_run(task, body.wait)
        if finals is None:
            return {"ok": True, "params": params, "running": True}
        ok, report = await node_report(ctx.ws, name, params, finals)
        return {"ok": ok, "params": params, "running": False, **report}

    @r.post("/run")
    async def run_all(body: RunAllReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Bring the graph (or ``targets``) up to date for one parameter set.
        Only stale nodes run unless ``force``."""
        ws = ctx.ws
        params = _resolve(ws, body.params)
        if body.targets is None:
            st = ws.status(params)
            targets = list(st) if body.force else [n for n, s in st.items() if s["state"] in ("stale", "new", "failed")]
        else:
            targets = body.targets
            for t in targets:
                _need(ws, t)
        if not targets:
            return {"ok": True, "targets": targets, "params": params}
        task = asyncio.create_task(ws.run(targets, params=params, force=set(targets) if body.force else set()))
        if body.wait is None:
            return {"ok": True, "targets": targets, "params": params}
        finals = await _await_run(task, body.wait)
        if finals is None:
            return {"ok": True, "targets": targets, "params": params, "running": True}
        ok, report = graph_report(ws, finals)
        return {"ok": ok, "targets": targets, "params": params, "running": False, **report}

    @r.post("/run-stale")
    async def run_stale(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return await run_all(RunAllReq(), ctx)

    @r.post("/backfill")
    async def backfill_start(body: BackfillReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Replay the graph for a list (or date range) of one parameter's values."""
        values = body.values
        if values is None:
            if not body.start or not body.end:
                raise HTTPException(400, "give `values`, or `start` and `end`")
            values = _date_range(body.start, body.end, body.weekdays)
        if not values:
            raise HTTPException(400, "nothing to backfill in that range")
        if len(values) > BACKFILL_MAX:
            raise HTTPException(400, f"at most {BACKFILL_MAX} values per backfill")
        try:
            return ctx.ws.start_backfill(
                body.param, values, targets=body.targets, params=body.params, force=body.force
            )
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @r.get("/backfill")
    def backfill_get(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return {"backfill": ctx.ws.backfill_status()}

    @r.delete("/backfill")
    def backfill_cancel(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return {"ok": ctx.ws.cancel_backfill()}

    @r.post("/nodes/{name}/schedule")
    def schedule(name: str, body: ScheduleReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Pause/resume a schedule, and/or set its cron from outside the code."""
        ws = ctx.ws
        _need(ws, name)
        if "cron" in body.model_fields_set:
            try:
                ws.set_schedule(name, body.cron)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if body.paused is not None:
            ws.set_paused(name, body.paused)
        return {"ok": True, "schedule": ws.status().get(name, {}).get("schedule")}

    @r.get("/nodes/{name}/data", response_model=None)
    def data(
        request: Request,
        name: str,
        limit: int | None = None,
        offset: int = 0,
        params: str | None = None,
        run_id: str | None = None,
        part: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """A node's output. JSON: the first ``limit`` rows (200 unless given,
        5000 at most). With ``Accept: application/vnd.apache.arrow.stream`` a
        table output is an Arrow stream of every row, or of ``limit`` rows
        when given; an output that is not a table is still answered as JSON.
        A node with several outputs answers ``kind: multi`` and its
        ``parts``; ``part`` picks one, which is then answered like an output
        of its own."""
        _need(ctx.ws, name)
        resolved = _resolve(ctx.ws, _query_params(params))
        if wants_arrow(request.headers.get("accept")):
            stream = ctx.ws.preview_arrow(name, limit=limit, offset=offset, params=resolved, run_id=run_id, part=part)
            if stream is not None:
                return _arrow(stream)
        rows = 200 if limit is None else min(limit, 5000)
        try:
            return ctx.ws.preview(name, limit=rows, offset=offset, params=resolved, run_id=run_id, part=part)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e

    @r.api_route("/artifacts/{node}/{filename}", methods=["GET", "HEAD"], response_model=None)
    def artifact(node: str, filename: str, ctx: Ctx = Depends(get_ctx)) -> FileResponse:
        """The stored file of one output, ``<artifact_id>.parquet``,
        ``<artifact_id>.json`` or ``<artifact_id>.zip`` (several outputs),
        byte for byte. An artifact id names its
        content, so the answer never changes and can be cached for good;
        ``Range`` requests are honoured, which lets a client read part of a
        Parquet file."""
        artifact_id, _, ext = filename.rpartition(".")
        kind = ARTIFACT_KINDS.get(ext)
        found = ctx.ws.artifact_file(node, artifact_id) if kind and artifact_id else None
        if found is None or found[1] != kind:
            raise HTTPException(404, f"no output {filename} of {node}")
        return FileResponse(
            found[0],
            media_type=ARTIFACT_MEDIA_TYPES[kind],
            headers={"Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{artifact_id}"'},
        )

    @r.api_route("/artifacts/{node}/{artifact_id}/{filename}", methods=["GET", "HEAD"], response_model=None)
    def artifact_part(node: str, artifact_id: str, filename: str, ctx: Ctx = Depends(get_ctx)) -> FileResponse:
        """One part of an output with several (``<artifact_id>.zip``), as a
        file of its own: ``<part>.parquet`` or ``<part>.json``. Cached like
        the whole artifact, since the id names the content of every part."""
        part, _, ext = filename.rpartition(".")
        kind = ARTIFACT_KINDS.get(ext)
        found = ctx.ws.artifact_file(node, artifact_id, part=part) if kind in ("table", "json") and part else None
        if found is None or found[1] != kind:
            raise HTTPException(404, f"no output {artifact_id}/{filename} of {node}")
        return FileResponse(
            found[0],
            media_type=ARTIFACT_MEDIA_TYPES[kind],
            headers={"Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{artifact_id}/{part}"'},
        )

    @r.get("/nodes/{name}/runs")
    def runs(name: str, ctx: Ctx = Depends(get_ctx)) -> list[dict[str, Any]]:
        return ctx.ws.store.runs_for(name)

    @r.get("/nodes/{name}/history")
    def history(name: str, ctx: Ctx = Depends(get_ctx)) -> list[dict[str, Any]]:
        """The node's output for every parameter set it has run with."""
        _need(ctx.ws, name)
        return ctx.ws.history(name)

    @r.get("/runs/{run_id}/source")
    def run_source(run_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The code this run executed (``source`` is null when that version
        was never recorded: runs from before code revisions existed)."""
        out = ctx.ws.run_source(run_id)
        if out is None:
            raise HTTPException(404, "no such run")
        return out

    # -- code revisions --------------------------------------------------------
    @r.get("/code/revisions")
    def code_revisions(
        node: str | None = None, view: str | None = None, limit: int = 200, ctx: Ctx = Depends(get_ctx)
    ) -> list[dict[str, Any]]:
        """Saved versions of the project's code, newest first; ``node``
        narrows to one node, ``view`` to one view's files (deleted ones included)."""
        return ctx.ws.code_revisions(node=node, view=view, limit=min(max(limit, 1), 1000))

    @r.get("/code/blobs/{digest}")
    def code_blob(digest: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        source = ctx.ws.code_source(digest)
        if source is None:
            raise HTTPException(404, "no saved version with that hash")
        return {"hash": digest, "source": source}

    @r.post("/nodes/{name}/restore")
    def restore_node(name: str, body: NodeRestore, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Make a saved version of ``name`` its current code (a deleted node
        comes back). Recorded as a new revision."""
        try:
            ctx.ws.restore_node(name, body.hash)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        return ctx.ws.snapshot()

    @r.get("/runs")
    def recent_runs(ctx: Ctx = Depends(get_ctx)) -> list[dict[str, Any]]:
        return ctx.ws.store.recent_runs()

    @r.get("/runs/{run_id}")
    def run_detail(run_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        row = ctx.ws.store.run(run_id)
        if row is None:
            raise HTTPException(404, "no such run")
        return row

    @r.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return {"ok": ctx.ws.cancel(run_id)}

    @r.post("/query", response_model=None)
    def query(body: QueryReq, request: Request, ctx: Ctx = Depends(get_ctx)) -> Any:
        """DuckDB SQL over every node's output. JSON: the first ``limit`` rows
        (500 unless given, 5000 at most). With
        ``Accept: application/vnd.apache.arrow.stream``: an Arrow stream of
        every row, or of ``limit`` rows when given."""
        try:
            if wants_arrow(request.headers.get("accept")):
                return _arrow(ctx.ws.query_arrow(body.sql, limit=body.limit, params=body.params))
            rows = 500 if body.limit is None else min(body.limit, 5000)
            return ctx.ws.query(body.sql, limit=rows, params=body.params)
        except Exception as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}") from e

    @r.get("/events")
    async def events(request: Request, ctx: Ctx = Depends(get_ctx)) -> StreamingResponse:
        ws = ctx.ws
        q = ws.bus.subscribe()

        async def gen():
            try:
                yield _sse({"type": "hello"})
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield _sse(ev)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                ws.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # -- agent -----------------------------------------------------------------
    @r.post("/agent")
    async def agent_chat(body: AgentReq, ctx: Ctx = Depends(get_ctx)) -> StreamingResponse:
        agent = ctx.agent
        if not agent.enabled:
            raise HTTPException(400, "Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY (or PORTKEY_API_KEY with EXUVIA_MODEL) to enable the agent.")
        try:
            # The turn runs in the background: closing this stream does not
            # stop it, and /events carries the same events for re-attaching.
            _, queue = agent.start(body.session_id, body.message, body.selected)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

        async def gen():
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield _sse(ev)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @r.get("/agent/{session_id}")
    def agent_transcript(session_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The saved conversation, to redraw the chat after a reload."""
        return {
            "session_id": session_id,
            "running": session_id in ctx.agent.running,
            "events": ctx.agent.transcript(session_id),
        }

    @r.post("/agent/{session_id}/cancel")
    def agent_cancel(session_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        return {"ok": ctx.agent.cancel(session_id)}

    @r.delete("/agent/{session_id}")
    def agent_reset(session_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        ctx.agent.cancel(session_id)
        ctx.agent.reset(session_id)
        return {"ok": True}

    return r


# Marks the page a hub serves at / as the hub's home (ui/src/main.tsx).
HUB_HOME_META = '<meta name="exuvia-hub" content="home">'
# Kept under 300 characters with its JSON: an old client shows only that much.
HUB_GUIDE_HINT = (
    "This is an exuvia hub. MCP for a whole hub needs exuvia 0.3.11 or newer: "
    "use `uvx exuvia@latest mcp --url <hub>`, or run `uv tool upgrade exuvia` or `pipx upgrade exuvia`. "
    "For one project, use <hub>/p/<project>."
)

# A hub project's API; its workbench page (/p/<id>/) is not API.
_HUB_API = re.compile(r"^/p/[^/]+/api(/|$)")
_MCP_PATH = re.compile(r"^(/p/[^/]+)?/mcp/?$")  # mcp_http.py: auth as the API


def _token_ok(request: Request, api_token: str) -> bool:
    """Whether the request carries the API token (header, or the browser cookie)."""
    header = request.headers.get("authorization", "")
    cookie = request.cookies.get("exuvia_token", "")
    return hmac.compare_digest(header.encode(), f"Bearer {api_token}".encode()) or \
        hmac.compare_digest(cookie.encode(), api_token.encode())


def _browser_open() -> bool:
    """EXUVIA_BROWSER_AUTH: `token` (default: a browser enters the token once) or `open`."""
    mode = os.environ.get("EXUVIA_BROWSER_AUTH", "token").strip().lower() or "token"
    if mode not in ("token", "open"):
        raise SystemExit(f"EXUVIA_BROWSER_AUTH must be token or open, not {mode!r}")
    return mode == "open"


def _install_common(app: FastAPI, users: UserStore | None, command: str = "serve") -> None:
    """CORS, who is signed in (EXUVIA_AUTH) and the optional shared-secret
    auth, for both app shapes. ``users`` is the user list each sign-in is
    recorded in (None when nobody signs in: EXUVIA_AUTH=token). ``command``
    is the CLI command this app is (``serve`` or ``hub``)."""
    origins = [o for o in os.environ.get("EXUVIA_CORS_ORIGINS", "").split(",") if o]
    if origins:  # e.g. https://lobster.mp when embedded in another app
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    headers = auth.header_auth()
    api_token = os.environ.get("EXUVIA_API_TOKEN")
    method = "headers" if headers else ("token" if api_token else "none")

    @app.get("/api/me")
    def me() -> dict[str, Any]:
        """Who this request is from: ``user`` is null for a caller with only
        the API token (or on a server with no auth). ``auth`` is how this
        server identifies people: ``headers``, ``token`` or ``none``.
        ``tokens``: whether people can make agent tokens here (/api/me/tokens)."""
        ident = auth.current()
        return {"auth": method, "user": ident.as_dict() if ident else None, "tokens": users is not None}

    def token_owner() -> tuple[UserStore, str]:
        """The user list and the signed-in person's id, for managing their own
        agent tokens. A request made with an agent token can't: a leaked token
        must not be able to mint more."""
        if users is None:
            raise HTTPException(404, "no agent tokens: nobody signs in on this server (see EXUVIA_AUTH)")
        ident = auth.current()
        if ident is None or not ident.uid:
            raise HTTPException(403, "agent tokens belong to a signed-in person: sign in through the proxy")
        if ident.method == "token":
            raise HTTPException(403, "an agent token can't manage tokens: sign in through the proxy")
        return users, ident.uid

    @app.get("/api/me/tokens")
    def tokens_list() -> dict[str, Any]:
        """The signed-in person's agent tokens (names and hints, never the tokens)."""
        store, uid = token_owner()
        return {"tokens": store.tokens(uid)}

    @app.post("/api/me/tokens", status_code=201)
    def tokens_create(body: TokenReq) -> dict[str, Any]:
        """A new agent token for the signed-in person. ``token`` is in this
        answer only: it is not kept, so it can't be shown again."""
        store, uid = token_owner()
        row, token = store.create_token(uid, body.name)
        return {**row, "token": token}

    @app.delete("/api/me/tokens/{tid}")
    def tokens_revoke(tid: str) -> dict[str, Any]:
        """Revoke one of the signed-in person's agent tokens."""
        store, uid = token_owner()
        if not store.revoke_token(uid, tid):
            raise HTTPException(404, f"no token {tid}")
        return {"ok": True}

    @app.get("/api/about")
    async def about(refresh: bool = False) -> dict[str, Any]:
        """The running version, how it was installed (``deployed`` or a local
        install) and whether a newer release is out, for the help menu. The
        check against PyPI is cached; ``refresh`` asks again (at most once a
        minute). ``EXUVIA_UPDATE_CHECK=off`` turns it off."""
        return await about_mod.about(refresh, command)

    def admin_users() -> UserStore:
        """The user list, for an admin: a signed-in admin, or a caller with
        the API token (the operator)."""
        if users is None:
            raise HTTPException(404, "no user list: nobody signs in on this server (see EXUVIA_AUTH)")
        ident = auth.current()
        if ident is not None and ident.role != "admin":
            raise HTTPException(403, "admins only")
        return users

    @app.get("/api/users")
    def users_list() -> dict[str, Any]:
        """Everyone who has signed in, most recently seen first."""
        return {"users": admin_users().list()}

    @app.patch("/api/users/{uid}")
    def users_update(uid: str, body: UserPatch) -> dict[str, Any]:
        """Make someone an admin or a member, or disable / re-enable them."""
        store = admin_users()
        ident = auth.current()
        if ident is not None and ident.uid == uid and (body.role == "member" or body.disabled):
            raise HTTPException(400, "you cannot demote or disable yourself; ask another admin")
        try:
            return store.update(uid, role=body.role, disabled=body.disabled).as_dict()
        except KeyError:
            raise HTTPException(404, f"no user {uid}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    if not headers and not api_token:
        return
    browser_open = _browser_open() if api_token and not headers else False

    # API clients send `Authorization: Bearer <token>`; browsers open
    # `/?token=<token>` once and get an HttpOnly cookie (EventSource can't
    # send headers). With EXUVIA_BROWSER_AUTH=open (a hub on a private
    # network), every page load hands out the cookie, so people never type
    # the token; the API still turns away callers that have neither cookie
    # nor token, and SameSite=Lax keeps other sites from riding the cookie.
    #
    # With EXUVIA_AUTH=headers a proxy in front has signed the person in and
    # says who they are on every request, the page and the API alike. The API
    # token, when set, still lets agents and MCP clients in without them.
    def give_cookie(request: Request, resp: Any) -> Any:
        assert api_token
        resp.set_cookie("exuvia_token", api_token, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp

    def is_api(path: str) -> bool:
        return path.startswith("/api/") or bool(_HUB_API.match(path)) or bool(_MCP_PATH.match(path))

    def unidentified(path: str) -> Any:
        assert headers is not None
        detail = (
            f"not signed in: exuvia expects the {headers.user_header} or {headers.email_header} "
            "header from the sign-in proxy in front of it"
            + ("; agents and scripts send an agent token (Authorization: Bearer exv_...) from the hub's account menu (Connect an agent)"
               if users is not None and is_api(path) else "")
        )
        if is_api(path):
            return JSONResponse({"detail": detail}, status_code=401)
        return HTMLResponse(f"<!doctype html><title>exuvia: not signed in</title><p>{html.escape(detail)}.</p>",
                            status_code=401)

    def disabled(path: str) -> Any:
        detail = "your exuvia account is disabled: ask an admin to enable it"
        if is_api(path):
            return JSONResponse({"detail": detail}, status_code=403)
        return HTMLResponse(f"<!doctype html><title>exuvia: disabled</title><p>{html.escape(detail)}.</p>",
                            status_code=403)

    @app.middleware("http")
    async def require_auth(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        path = request.url.path
        if path == "/healthz":  # liveness: no auth
            return await call_next(request)
        ident = headers.identify(request.headers, request.client.host if request.client else None) if headers else None
        if ident is not None:
            if users is not None:  # recorded in the user list (see users.py)
                user = users.recent(ident) or await asyncio.to_thread(users.sign_in, ident)
                if user.disabled:
                    return disabled(path)
                ident = dataclasses.replace(ident, uid=user.id, role=user.role)
            with auth.signed_in(ident):
                return await call_next(request)
        # An agent token (users.py): signed in as the person who made it,
        # from anywhere, since MCP clients and scripts can't pass the proxy.
        bearer = request.headers.get("authorization", "")
        if users is not None and bearer[:7].lower() == "bearer " and looks_like_token(bearer[7:].strip()):
            owner = await asyncio.to_thread(users.by_token, bearer[7:].strip())
            if owner is not None:
                if owner.disabled:
                    return disabled(path)
                ident = auth.Identity(user=owner.name, email=owner.email, method="token", uid=owner.id, role=owner.role)
                with auth.signed_in(ident):
                    return await call_next(request)
        if not api_token:  # header auth alone: nobody gets in without the headers
            return unidentified(path)
        qs_token = request.query_params.get("token")
        if qs_token is not None and not is_api(path):
            if not hmac.compare_digest(qs_token.encode(), api_token.encode()):
                return JSONResponse({"detail": "bad token"}, status_code=401)
            return give_cookie(request, RedirectResponse(path or "/"))
        if is_api(path):
            if not _token_ok(request, api_token):
                if headers:
                    return unidentified(path)
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)
        resp = await call_next(request)
        if browser_open and request.cookies.get("exuvia_token") != api_token:
            give_cookie(request, resp)
        return resp


def create_app(root: str | Path) -> FastAPI:
    ws = Workspace(root)
    ctx = Ctx(ws, Agent(ws))

    async def this_project(_scope: dict[str, Any]) -> Ctx:
        return ctx

    mcp = mcp_http.project_endpoint(this_project)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await ws.start()
        async with mcp.running():
            yield
        await ws.stop()

    app = FastAPI(title="exuvia", lifespan=lifespan)
    # One project: the user list lives in its database (or the shared one).
    users = open_users(ws.store.db) if auth.identifies_people() else None
    _install_common(app, users)
    app.state.users = users
    app.state.ws = ws
    app.include_router(project_router(lambda: ctx), prefix="/api")
    mcp_http.add_routes(app, "", mcp)  # MCP over HTTP (mcp_http.py), before the UI's catch-all

    # Liveness for container platforms (no auth, like the hub's and the executor's).
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "mode": "serve", "uptime_s": round(_time.monotonic() - _STARTED, 3)}

    # A headless install may ship only the views runtime (static/view-runtime/).
    if (STATIC / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


        @app.get("/{path:path}")
        def spa(path: str) -> FileResponse:
            f = STATIC / path
            return FileResponse(f if path and f.is_file() else STATIC / "index.html")

    return app


def create_hub_app(root: str | Path, templates: str | Path | None = None) -> FastAPI:
    from .hub import HUB_USERS_DB, PROJECT_ID, Hub, StillInUse

    hub = Hub(root, templates)

    async def path_project(scope: dict[str, Any]) -> Any:
        project = scope.get("path_params", {}).get("project", "")
        if not PROJECT_ID.match(project) or not hub.exists(project):
            return JSONResponse({"detail": f"no project {project}"}, status_code=404)
        return await hub.get(project)

    # MCP over HTTP (mcp_http.py): /mcp for every project, /p/<id>/mcp for one.
    hub_mcp, project_mcp = mcp_http.hub_endpoint(hub), mcp_http.project_endpoint(path_project)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if SCHEDULER == "internal":
            # Each workspace runs its own schedule loop, so every project has
            # to be loaded. With an external scheduler they load on demand.
            for pid in hub.ids():
                await hub.get(pid)
        async with hub_mcp.running(), project_mcp.running():
            yield
        await hub.close()

    app = FastAPI(title="exuvia hub", lifespan=lifespan)
    # One user list for every project on the hub (see users.py).
    if auth.identifies_people():
        hub.users = open_users(hub.root / HUB_USERS_DB)
    _install_common(app, hub.users, "hub")
    app.state.hub = hub

    async def get_ctx(project: str) -> Ctx:
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        return await hub.get(project)

    app.include_router(project_router(get_ctx), prefix="/p/{project}/api")
    mcp_http.add_routes(app, "", hub_mcp)
    mcp_http.add_routes(app, "/p/{project}", project_mcp)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "mode": "hub", "uptime_s": round(_time.monotonic() - _STARTED, 3)}

    @app.get("/api/tools")
    def hub_tools() -> dict[str, Any]:
        """The project tools (each runs at ``POST /p/{project}/api/tools/{name}``),
        for ``exuvia mcp --url <hub>`` to offer before a project exists."""
        from .agent import TOOLS

        return {"tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in TOOLS]}

    @app.get("/api/guide", include_in_schema=False)
    def hub_guide() -> None:
        """A project's guide is at /p/<id>/api/guide. Asked here, the caller is
        an `exuvia mcp --url <hub>` from before hub-wide MCP (0.3.10 and older):
        it takes the hub for one project and stops on anything but a 200,
        showing the start of the body, so the 404 says what to do."""
        raise HTTPException(404, HUB_GUIDE_HINT)

    @app.get("/api/projects")
    def projects() -> dict[str, Any]:
        # `auth`: whether clients need the API token (the home page's MCP setup says so).
        return {"projects": hub.list(), "templates": hub.templates(), "auth": bool(os.environ.get("EXUVIA_API_TOKEN"))}

    @app.post("/api/projects")
    async def project_create(body: ProjectCreate) -> dict[str, Any]:
        try:
            await hub.create(body.id, body.name, body.template)
        except FileExistsError as e:
            raise HTTPException(409, f"project {body.id} already exists") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"id": body.id, "name": (await hub.get(body.id)).ws.project.name}

    @app.put("/api/projects/{project}/template")
    async def project_apply_template(project: str, body: TemplateApply) -> dict[str, Any]:
        """Replace the project's code with a template's; history and outputs stay."""
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        try:
            await hub.apply_template(project, body.template)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except StillInUse as e:
            raise HTTPException(409, str(e)) from e
        ctx = await hub.get(project)
        return {"id": project, "name": ctx.ws.project.name, "nodes": sorted(p.stem for p in ctx.ws.project.node_files())}

    @app.delete("/api/projects/{project}")
    async def project_delete(project: str) -> dict[str, Any]:
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        try:
            await hub.delete(project)
        except StillInUse as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.post("/api/schedule/tick")
    async def tick_all() -> dict[str, Any]:
        """Evaluate every project's schedules. ``next`` is the earliest time
        any project has work again (null: nothing scheduled anywhere)."""
        return await hub.tick()

    @app.get("/api/hub/state")
    def hub_state() -> dict[str, Any]:
        """``version`` moves whenever anything worth persisting changed;
        ``busy`` is true while runs, backfills or agent turns are in flight."""
        return hub.state()

    @app.get("/api/hub/export")
    async def hub_export() -> FileResponse:
        """All projects with their run history and artifacts, as one .tar.gz."""
        path = await asyncio.to_thread(hub.export)
        return FileResponse(path, media_type="application/gzip", filename="exuvia-hub.tar.gz")

    @app.post("/api/hub/import")
    async def hub_import(request: Request) -> dict[str, Any]:
        """Restore projects from an export. Projects in the archive replace
        same-named ones; others are left alone."""
        blob = await request.body()
        try:
            restored = await hub.restore(blob)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except StillInUse as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True, "projects": restored}

    # The UI: the hub's home page at / (list, create, delete and open
    # projects, through /api/projects) and the workbench, one per project, at
    # /p/<id>/, talking to /p/<id>/api. Both are the one page `exuvia serve`
    # serves; a meta tag tells it it is the hub's home. Registered last, so
    # the API routes win.
    if (STATIC / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

        def authorized(request: Request) -> bool:
            if auth.current() is not None:  # signed in through the proxy
                return True
            token = os.environ.get("EXUVIA_API_TOKEN")
            if auth.header_auth() is not None:
                return bool(token) and _token_ok(request, token)
            return not token or _browser_open() or _token_ok(request, token)

        @app.get("/", response_class=HTMLResponse)
        def hub_home() -> HTMLResponse:
            page = (STATIC / "index.html").read_text(encoding="utf-8")
            # Into <head>, or right after the doctype (before it would mean quirks mode).
            marked, n = re.subn(r"<head\b[^>]*>", lambda m: m.group(0) + HUB_HOME_META, page, count=1, flags=re.I)
            if not n:
                marked = re.sub(r"^(\s*<!doctype[^>]*>)?", lambda m: (m.group(0) or "") + HUB_HOME_META, page, count=1, flags=re.I)
            return HTMLResponse(marked)

        @app.get("/p/{project}", include_in_schema=False)
        def project_page_slash(project: str) -> RedirectResponse:
            return RedirectResponse(f"/p/{project}/")

        @app.get("/p/{project}/{path:path}", include_in_schema=False)
        def project_page(project: str, path: str, request: Request) -> FileResponse:
            # Unknown API routes stay 404s; a missing project is only
            # reported to a caller that may see the project list.
            if path == "api" or path.startswith("api/") or not PROJECT_ID.match(project):
                raise HTTPException(404, "not found")
            if authorized(request) and not hub.exists(project):
                raise HTTPException(404, f"no project {project}")
            return FileResponse(STATIC / "index.html")

    return app
