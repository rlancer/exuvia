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
import hmac
import json
import os
import time as _time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _STARTED, __version__
from .agent import TOOLS, Agent
from .arrowstream import MEDIA_TYPE as ARROW_STREAM, ArrowStream, wants_arrow
from .project import NODE_NAME, PARAM_TYPES, coerce_param
from .report import graph_report, node_report
from .store import clean_origin
from .workspace import SCHEDULER, Workspace, acting_as

STATIC = Path(__file__).parent / "static"
BACKFILL_MAX = 400
RUN_WAIT_MAX = 300.0
# Stored output files by extension (see runner.save_artifact), and how each is served.
ARTIFACT_KINDS = {"parquet": "table", "json": "json"}
ARTIFACT_MEDIA_TYPES = {"table": "application/vnd.apache.parquet", "json": "application/json"}


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
            raise HTTPException(404, "the views runtime is not built: run `npm run build` in exuvia/ui")
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
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """``GET /nodes/{node}/data`` for a view: 403 unless the view reads ``node``."""
        try:
            ctx.ws.view_node(name, node)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return data(request, node, limit=limit, offset=offset, params=params, run_id=None, ctx=ctx)

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
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """A node's output. JSON: the first ``limit`` rows (200 unless given,
        5000 at most). With ``Accept: application/vnd.apache.arrow.stream`` a
        table output is an Arrow stream of every row, or of ``limit`` rows
        when given; an output that is not a table is still answered as JSON."""
        _need(ctx.ws, name)
        resolved = _resolve(ctx.ws, _query_params(params))
        if wants_arrow(request.headers.get("accept")):
            stream = ctx.ws.preview_arrow(name, limit=limit, offset=offset, params=resolved, run_id=run_id)
            if stream is not None:
                return _arrow(stream)
        rows = 200 if limit is None else min(limit, 5000)
        return ctx.ws.preview(name, limit=rows, offset=offset, params=resolved, run_id=run_id)

    @r.api_route("/artifacts/{node}/{filename}", methods=["GET", "HEAD"], response_model=None)
    def artifact(node: str, filename: str, ctx: Ctx = Depends(get_ctx)) -> FileResponse:
        """The stored file of one output, ``<artifact_id>.parquet`` or
        ``<artifact_id>.json``, byte for byte. An artifact id names its
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
            raise HTTPException(400, "Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY to enable the agent.")
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


def _install_common(app: FastAPI) -> None:
    """CORS and the optional shared-secret auth, for both app shapes."""
    origins = [o for o in os.environ.get("EXUVIA_CORS_ORIGINS", "").split(",") if o]
    if origins:  # e.g. https://lobster.mp when embedded in another app
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    # API clients send `Authorization: Bearer <token>`; browsers open
    # `/?token=<token>` once and get an HttpOnly cookie (EventSource can't
    # send headers).
    api_token = os.environ.get("EXUVIA_API_TOKEN")
    if not api_token:
        return

    def is_api(path: str) -> bool:
        return path.startswith("/api/") or path.startswith("/p/")

    @app.middleware("http")
    async def require_token(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        path = request.url.path
        qs_token = request.query_params.get("token")
        if qs_token is not None and not is_api(path):
            if not hmac.compare_digest(qs_token.encode(), api_token.encode()):
                return JSONResponse({"detail": "bad token"}, status_code=401)
            resp = RedirectResponse(path or "/")
            resp.set_cookie("exuvia_token", api_token, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https")
            return resp
        if is_api(path):
            header = request.headers.get("authorization", "")
            cookie = request.cookies.get("exuvia_token", "")
            ok = hmac.compare_digest(header.encode(), f"Bearer {api_token}".encode()) or \
                hmac.compare_digest(cookie.encode(), api_token.encode())
            if not ok:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(root: str | Path) -> FastAPI:
    ws = Workspace(root)
    ctx = Ctx(ws, Agent(ws))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await ws.start()
        yield
        await ws.stop()

    app = FastAPI(title="exuvia", lifespan=lifespan)
    _install_common(app)
    app.state.ws = ws
    app.include_router(project_router(lambda: ctx), prefix="/api")

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
    from .hub import PROJECT_ID, Hub, StillInUse

    hub = Hub(root, templates)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if SCHEDULER == "internal":
            # Each workspace runs its own schedule loop, so every project has
            # to be loaded. With an external scheduler they load on demand.
            for pid in hub.ids():
                await hub.get(pid)
        yield
        await hub.close()

    app = FastAPI(title="exuvia hub", lifespan=lifespan)
    _install_common(app)
    app.state.hub = hub

    async def get_ctx(project: str) -> Ctx:
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        return await hub.get(project)

    app.include_router(project_router(get_ctx), prefix="/p/{project}/api")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "mode": "hub", "uptime_s": round(_time.monotonic() - _STARTED, 3)}

    @app.get("/api/projects")
    def projects() -> dict[str, Any]:
        return {"projects": hub.list(), "templates": hub.templates()}

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

    return app
