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
import tempfile
import time as _time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _STARTED, __version__, about as about_mod, auth, dataapi, mcp_http
from .activity import ActivityStore
from .datakeys import DataKeyStore
from .agent import READ_TOOLS, TOOLS, Agent
from .arrowstream import MEDIA_TYPE as ARROW_STREAM, ArrowStream, wants_arrow
from .envvars import EnvError, public as envvars_public
from .exports import ExportError, check_format, code_archive, file_response, file_stem, json_response, slug, table_response, zip_response
from .npm import NpmError
from .packages import PackagesError
from .project import NODE_NAME, PARAM_TYPES, coerce_param
from .pushing import format_of, max_bytes
from .report import graph_report, node_report
from .groups import open_groups
from .sharing import GENERAL, GRANT_ROLES, LINK_COOKIE, MAX_LINK_DAYS, at_least, looks_like_link, open_sharing
from .store import clean_origin
from .agent_mcp import CALLBACK_PATH, McpServers, finish_sign_in, open_mcp_servers, private_allowed, probe, sign_in
from .agent_mcp import enabled as agent_mcp_enabled
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
    # A partitioned node: rebuild it from an empty output, so what the run
    # returns becomes the whole output.
    full_refresh: bool = False
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


class McpServerReq(BaseModel):
    url: str | None = None  # required for a new server
    # Merged into the server's headers: a value sets one, null removes it,
    # and headers left out keep their values (secrets need not be retyped).
    headers: dict[str, str | None] | None = None
    enabled: bool | None = None


class McpPresetReq(BaseModel):
    url: str
    description: str = ""
    # The header names each person fills in when they connect: no values.
    headers: list[str] = []


class McpConnectReq(BaseModel):
    # A value for each header the preset asks for.
    headers: dict[str, str] | None = None


class McpSignInReq(BaseModel):
    # The page's origin (https://exuvia.example.com): the OAuth callback is
    # sent there, so it is where the person's browser reaches this server.
    origin: str | None = None
    # The page to come back to after the sign-in, when it runs in that page's
    # tab (a browser that blocks the popup). Only a page on ``origin``.
    back: str | None = None


class ScheduleReq(BaseModel):
    paused: bool | None = None
    cron: str | None = None  # "" turns the schedule off; null drops the override


class ParamReq(BaseModel):
    type: str = "string"
    default: Any = None
    description: str = ""


class EnvVarReq(BaseModel):
    # None keeps the saved value (making a plain variable secret).
    value: str | None = None
    secret: bool = True


class SettingsReq(BaseModel):
    name: str | None = None
    timezone: str | None = None


class QueryReq(BaseModel):
    sql: str
    limit: int | None = None
    params: dict[str, Any] | None = None


class ViewRefresh(BaseModel):
    params: dict[str, Any] | None = None


class QueryExport(BaseModel):
    sql: str
    params: dict[str, Any] | None = None
    format: str = "csv"  # csv | xlsx | parquet | json
    filename: str | None = None  # without the extension; default "query"


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
    # With sharing on: what everyone else signed in gets (private | view |
    # edit; default: view for a project someone owns, edit for one nobody
    # does, or EXUVIA_DEFAULT_ACCESS), and who owns it (a user id; default:
    # whoever creates it).
    access: str | None = None
    owner: str | None = None
    # A group to put it in (groups.py): the creator needs editor on the group.
    group: str | None = None


class SharingPatch(BaseModel):
    general: str | None = None  # private | view | edit
    owner: str | None = None  # a user id: hand the project over


class ShareReq(BaseModel):
    user: str | None = None  # a user id, or
    email: str | None = None  # an email, for someone who may not have signed in yet
    role: str = "viewer"  # viewer | editor


class GroupCreate(BaseModel):
    name: str


class GroupPatch(BaseModel):
    name: str | None = None
    owner: str | None = None  # a user id: hand the group over


class ProjectGroupReq(BaseModel):
    group: str | None = None  # a group id; null: take the project out of its group


class LinkReq(BaseModel):
    name: str = ""
    expires_in_days: float | None = None  # unset: until revoked


class AccessReq(BaseModel):
    role: str = "viewer"
    message: str = ""


class ApproveReq(BaseModel):
    role: str | None = None  # default: what was asked for


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


def _export_format(fmt: str, allow_zip: bool = False) -> str:
    if allow_zip and (fmt or "").lower() == "zip":
        return "zip"
    try:
        return check_format(fmt)
    except ExportError as e:
        raise HTTPException(e.status, str(e)) from e


def _table_export(stream: ArrowStream, fmt: str, stem: str) -> Any:
    try:
        return table_response(stream, fmt, stem)
    except ExportError as e:
        raise HTTPException(e.status, str(e)) from e


def _stream_meta(stream: ArrowStream) -> dict[str, Any]:
    """What ``preview_arrow`` put in the stream's metadata (run id, parameters, ...)."""
    raw = (stream.schema.metadata or {}).get(b"exuvia")
    return json.loads(raw) if raw else {}


def _partition_range(raw: str | None) -> tuple[str | None, str | None] | None:
    """``?partitions=`` on a partitioned output's data and export routes:
    ``lo..hi`` (either end may be left out) or a single value."""
    if raw is None or raw == "":
        return None
    lo, sep, hi = raw.partition("..")
    return (lo or None, hi or None) if sep else (raw, raw)


def _need_partitioned(ws: Workspace, name: str, partitions: Any, run_id: str | None) -> None:
    """``?partitions=`` only means something for a partitioned node: say so
    rather than answer with every row."""
    node = ws.graph.nodes.get(name)
    if partitions is not None and run_id is None and node is not None and not node.partition_by:
        raise HTTPException(400, f"{name} is not partitioned: leave out partitions")


def _export_output(
    ws: Workspace,
    name: str,
    fmt: str,
    params: dict[str, Any],
    run_id: str | None,
    part: str | None,
    filename: str | None = None,
    partitions: tuple[str | None, str | None] | None = None,
) -> Any:
    """A node's output as a download: a table in ``fmt``; a JSON output as
    .json; a node with several outputs as the stored zip of all of them
    (``fmt`` zip) or, with ``part``, one of them like an output of its own.
    ``partitions`` keeps a range of a partitioned output's partitions."""
    fmt = _export_format(fmt, allow_zip=True)
    _need_partitioned(ws, name, partitions, run_id)
    stream = (
        None if fmt == "zip"
        else ws.preview_arrow(name, params=params, run_id=run_id, part=part, partitions=partitions)
    )
    if stream is not None:
        return _table_export(stream, fmt, filename or file_stem(name, part, _stream_meta(stream).get("run_params")))
    try:
        out = ws.preview(name, limit=0, params=params, run_id=run_id, part=part, partitions=partitions)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0])) from e
    kind = out.get("kind")
    if kind == "table" and out.get("partition_by") and fmt != "zip":
        raise HTTPException(404, f"{name}'s output has no rows{' in those partitions' if partitions else ''} yet")
    stem = filename or file_stem(name, part, out.get("run_params"))
    if kind == "json":
        if fmt != "json":
            raise HTTPException(400, f"{name}'s output is JSON, not a table: download it as json")
        return json_response(out.get("value"), stem)
    if kind == "multi":
        run = ws.store.run(out["run_id"]) if out.get("run_id") else None
        found = ws.artifact_file(name, run["artifact_id"]) if run and run.get("artifact_id") else None
        if fmt == "zip" and found is not None:
            return file_response(found[0], ARTIFACT_MEDIA_TYPES["multi"], f"{slug(stem)}.zip")
        names = ", ".join(p["name"] for p in out.get("parts") or [])
        raise HTTPException(400, f"{name} has several outputs ({names}): pick one with part, or download them all as zip")
    if kind == "table" and fmt == "zip":
        raise HTTPException(400, "zip is for a node with several outputs: pick csv, xlsx, parquet or json")
    raise HTTPException(404, f"{name} has no output yet for these parameters")


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

    @r.get("/access")
    def access(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """What this request may do here. A single project (``exuvia serve``)
        has no sharing: everyone who gets in can do everything. A hub answers
        this route itself, with the project's sharing (sharing.py)."""
        ident = auth.current()
        return {"role": "owner", "sharing": False, "people": False, "user": ident.as_dict() if ident else None}

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

    # -- environment variables (envvars.py) -------------------------------------
    @r.get("/env")
    def env_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The project's variables (names, and plain values: a secret's value
        is never sent back), and the requests to set one that wait."""
        return ctx.ws.env_state()

    @r.put("/env/{name}")
    def env_put(name: str, body: EnvVarReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        try:
            ctx.ws.set_env_var(name, body.value, body.secret)
        except EnvError as e:
            raise HTTPException(400, str(e)) from e
        return ctx.ws.env_state()

    @r.delete("/env/{name}")
    def env_delete(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        ctx.ws.delete_env_var(name)
        return ctx.ws.env_state()

    @r.delete("/env/requests/{name}")
    def env_dismiss(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Turn down a request to set ``name`` (request_secret)."""
        ctx.ws.dismiss_env_request(name)
        return ctx.ws.env_state()

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

    # -- npm packages for views (the project's package.json; see npm.py) -------------
    @r.get("/npm")
    def npm_state(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The npm packages the project's views may import, from its package.json."""
        return ctx.ws.npm.state()

    @r.post("/npm")
    async def npm_edit(body: PackagesEdit, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Add packages (``recharts``, ``recharts@^2``; one without a version
        is pinned to the exact version the CDN resolves it to) and/or remove
        packages by name. Each one added is checked on the CDN first: a
        package it cannot serve is a 400 and leaves package.json as it was."""
        npm = ctx.ws.npm
        try:
            if body.remove:
                await asyncio.to_thread(npm.remove, body.remove)
            if body.add:
                await asyncio.to_thread(npm.add, body.add)
        except NpmError as e:
            raise HTTPException(400, str(e)) from e
        return npm.state()

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

    @r.get("/views/{name}/packages")
    async def view_packages(name: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The npm modules a view imports (from the project's package.json),
        for the page to post into the view's frame with its files:
        ``entries`` (import -> module id), ``modules`` (module id -> source),
        ``errors`` (import -> why it could not be fetched) and ``declared``."""
        try:
            return await asyncio.to_thread(_view_call, ctx.ws.view_packages, name)
        except NpmError as e:
            raise HTTPException(400, str(e)) from e

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
        partitions: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """``GET /nodes/{node}/data`` for a view: 403 unless the view reads ``node``."""
        try:
            ctx.ws.view_node(name, node)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return data(
            request, node, limit=limit, offset=offset, params=params, run_id=None, part=part,
            partitions=partitions, ctx=ctx,
        )

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

    @r.post("/views/{name}/refresh")
    async def view_refresh(name: str, body: ViewRefresh, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Re-run the live nodes a view depends on (``live=True`` or
        ``-- live: true``), for one parameter set; the page calls this when
        it opens the view. Returns at once with ``live`` (the view's live
        nodes), ``started`` and ``skipped`` (node -> why: already running,
        or refreshed within its ``live`` seconds). The view keeps showing
        the stored outputs; the runs' events replace them as they land.
        Anyone who can open the view may call it: a live node's author
        asked for it to run when the view is looked at."""
        params = _resolve(ctx.ws, body.params)
        try:
            res = ctx.ws.refresh_view(name, params)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        # One turn of the loop starts the runs, which registers them, so a
        # project read after this answer already shows them queued.
        await asyncio.sleep(0)
        return res

    @r.get("/views/{name}/data/{node}/export", response_model=None)
    def view_data_export(
        name: str,
        node: str,
        format: str = "csv",
        params: str | None = None,
        part: str | None = None,
        filename: str | None = None,
        partitions: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """``GET /nodes/{node}/export`` for a view: 403 unless the view reads ``node``."""
        try:
            ctx.ws.view_node(name, node)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        resolved = _resolve(ctx.ws, _query_params(params))
        return _export_output(ctx.ws, node, format, resolved, None, part, filename, _partition_range(partitions))

    @r.post("/views/{name}/query/export", response_model=None)
    def view_query_export(name: str, body: QueryExport, ctx: Ctx = Depends(get_ctx)) -> Any:
        """``POST /query/export`` for a view: the SQL sees only the view's ``reads``."""
        fmt = _export_format(body.format)
        try:
            stream = ctx.ws.view_query_arrow(name, body.sql, params=body.params)
        except KeyError as e:
            raise HTTPException(404, f"no view {name}") from e
        except Exception as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}") from e
        return _table_export(stream, fmt, body.filename or f"{name}-query")

    @r.post("/nodes/{name}/run")
    async def run_node(name: str, body: RunReq, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        _need(ctx.ws, name)
        node = ctx.ws.graph.nodes.get(name)
        if node is not None and node.push:
            raise HTTPException(400, f"{name} is a push node: its output is uploaded with `exuvia push`, not run here")
        if body.full_refresh and node is not None and not node.partition_by:
            raise HTTPException(400, f"{name} is not partitioned: a full refresh is for nodes with partition_by")
        params = _resolve(ctx.ws, body.params)
        task = asyncio.create_task(
            ctx.ws.run(
                [name], params=params, force={name} if body.force else set(),
                full_refresh={name} if body.full_refresh else None,
            )
        )
        if body.wait is None:
            return {"ok": True, "params": params}
        finals = await _await_run(task, body.wait)
        if finals is None:
            return {"ok": True, "params": params, "running": True}
        ok, report = await node_report(ctx.ws, name, params, finals)
        return {"ok": ok, "params": params, "running": False, **report}

    @r.post("/nodes/{name}/push")
    async def push_node(
        name: str,
        request: Request,
        format: str | None = None,  # noqa: A002 — the query parameter's name
        params: str | None = None,
        note: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> dict[str, Any]:
        """Upload a push node's output (pushing.py): the body is a parquet
        file or a JSON value, ``params`` a JSON object of run parameters,
        ``note`` a line for the run's log. Recorded as a run triggered
        ``push``; new output cascades downstream."""
        ws = ctx.ws
        _need(ws, name)
        # A page on another site can POST a form or a no-cors fetch here
        # without asking first; only those can't send another content type.
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct in ("", "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            raise HTTPException(
                415, "send the data with Content-Type: application/vnd.apache.parquet or application/json "
                "(or application/octet-stream and ?format=parquet|json)",
            )
        try:
            fmt = format_of(format, request.headers.get("content-type"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        try:
            values = json.loads(params) if params else None
        except ValueError as e:
            raise HTTPException(400, "params must be a JSON object, e.g. {\"as_of\": \"2026-11-20\"}") from e
        if values is not None and not isinstance(values, dict):
            raise HTTPException(400, "params must be a JSON object, e.g. {\"as_of\": \"2026-11-20\"}")
        limit = max_bytes()
        fd, tmp = tempfile.mkstemp(dir=ws.project.state_dir, prefix="push-", suffix=f".{fmt}")
        try:
            size = 0
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"the upload is over {limit // (1024 * 1024)} MB (EXUVIA_PUSH_MAX_MB)")
                    f.write(chunk)
            if not size:
                raise HTTPException(400, "nothing was sent: the body is the data to push")
            try:
                run = await ws.push(name, Path(tmp), fmt, params=values, note=(note or "")[:500] or None)
            except KeyError as e:
                raise HTTPException(404, str(e.args[0])) from e
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        finally:
            with suppress(OSError):
                os.unlink(tmp)
        if run.get("status") != "success":
            raise HTTPException(422, f"the pushed data could not be read: {run.get('error')} (run {run.get('id')})")
        run.pop("log", None)
        return {"ok": True, "run": run}

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

    @r.get("/schedule")
    def schedule_overview(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """Every scheduled node, soonest first, with its next fire times, its
        last scheduled run and what it cascades to; and the live nodes (see
        ``Workspace.schedule``)."""
        return ctx.ws.schedule()

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
        partitions: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """A node's output. JSON: the first ``limit`` rows (200 unless given,
        5000 at most). With ``Accept: application/vnd.apache.arrow.stream`` a
        table output is an Arrow stream of every row, or of ``limit`` rows
        when given; an output that is not a table is still answered as JSON.
        A node with several outputs answers ``kind: multi`` and its
        ``parts``; ``part`` picks one, which is then answered like an output
        of its own. For a partitioned output, ``partitions`` (``lo..hi``,
        either end optional, or one value) keeps those partitions and reads
        only the files that hold them."""
        _need(ctx.ws, name)
        resolved = _resolve(ctx.ws, _query_params(params))
        rng = _partition_range(partitions)
        _need_partitioned(ctx.ws, name, rng, run_id)
        if wants_arrow(request.headers.get("accept")):
            stream = ctx.ws.preview_arrow(
                name, limit=limit, offset=offset, params=resolved, run_id=run_id, part=part, partitions=rng
            )
            if stream is not None:
                return _arrow(stream)
        rows = 200 if limit is None else min(limit, 5000)
        try:
            return ctx.ws.preview(
                name, limit=rows, offset=offset, params=resolved, run_id=run_id, part=part, partitions=rng
            )
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e

    @r.get("/nodes/{name}/partitions")
    def partitions_of(
        name: str, params: str | None = None, run_id: str | None = None, ctx: Ctx = Depends(get_ctx)
    ) -> dict[str, Any]:
        """A partitioned output's manifest: ``partitions`` (each value with
        the digest of its rows and its row count) and ``files``, the Parquet
        chunks that hold them, each readable at
        ``/artifacts/{name}/{artifact_id}.parquet``. ``artifact_id`` is the
        output's id, which changes only when its data does. 404 for an
        output that isn't partitioned."""
        _need(ctx.ws, name)
        out = ctx.ws.partitions_of(name, _resolve(ctx.ws, _query_params(params)), run_id)
        if out is None:
            raise HTTPException(404, f"{name} has no partitioned output for these parameters")
        return out

    @r.get("/nodes/{name}/export", response_model=None)
    def export_node(
        name: str,
        format: str = "csv",
        params: str | None = None,
        run_id: str | None = None,
        part: str | None = None,
        filename: str | None = None,
        partitions: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> Any:
        """A node's whole output as a file to save: ``format`` csv, xlsx,
        parquet or json for a table (json only for a JSON output), or zip for
        every output of a node with several. ``params``, ``run_id`` and
        ``part`` pick the output as for ``GET /nodes/{name}/data``.
        ``filename`` (no extension) names the download; the default is the
        node's name and the run's parameter values. Excel takes at most one
        sheet's 1,048,575 rows: a bigger table is a 413."""
        _need(ctx.ws, name)
        return _export_output(
            ctx.ws, name, format, _resolve(ctx.ws, _query_params(params)), run_id, part, filename,
            _partition_range(partitions),
        )

    @r.api_route("/artifacts/{node}/{filename}", methods=["GET", "HEAD"], response_model=None)
    def artifact(node: str, filename: str, ctx: Ctx = Depends(get_ctx)) -> FileResponse:
        """The stored file of one output, ``<artifact_id>.parquet``,
        ``<artifact_id>.json`` or ``<artifact_id>.zip`` (several outputs),
        or one chunk of a partitioned output (``GET /nodes/{node}/partitions``
        lists them), byte for byte. An artifact id names its
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
        node: str | None = None,
        view: str | None = None,
        limit: int = 200,
        after: str | None = None,
        ctx: Ctx = Depends(get_ctx),
    ) -> list[dict[str, Any]]:
        """Saved versions of the project's code, newest first; ``node``
        narrows to one node, ``view`` to one view's files (deleted ones included).
        ``after`` (a revision id, or empty for the first) instead pages
        through every revision oldest first, from the one after it."""
        limit = min(max(limit, 1), 1000)
        if after is not None:
            if node or view:
                raise HTTPException(400, "after pages through every revision: give it without node or view")
            try:
                return ctx.ws.store.code_revisions_after(after or None, limit)
            except KeyError:
                raise HTTPException(404, f"no revision {after}") from None
        return ctx.ws.code_revisions(node=node, view=view, limit=limit)

    @r.get("/code/blobs/{digest}")
    def code_blob(digest: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        source = ctx.ws.code_source(digest)
        if source is None:
            raise HTTPException(404, "no saved version with that hash")
        return {"hash": digest, "source": source}

    @r.get("/code/archive", response_model=None)
    def code_zip(node: str | None = None, view: str | None = None, ctx: Ctx = Depends(get_ctx)) -> Any:
        """The project's code as a .zip: every node, ``lib/`` helper and view,
        with ``exuvia.json``, ``pyproject.toml``, ``uv.lock`` and the like.
        ``node`` narrows it to that node and the helpers it imports, ``view``
        to that view's files. Never ``.env``, run history or outputs."""
        if node and view:
            raise HTTPException(400, "give node or view, not both")
        try:
            body, filename = code_archive(ctx.ws.project, ctx.ws.graph, node=node, view=view)
        except KeyError as e:
            raise HTTPException(404, f"not found: {e.args[0]}") from e
        return zip_response(body, filename)

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

    @r.post("/query/export", response_model=None)
    def query_export(body: QueryExport, ctx: Ctx = Depends(get_ctx)) -> Any:
        """``POST /query``'s whole result as a file to save, in ``format``
        csv, xlsx, parquet or json; ``filename`` (no extension) names it."""
        fmt = _export_format(body.format)
        try:
            stream = ctx.ws.query_arrow(body.sql, params=body.params)
        except Exception as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}") from e
        return _table_export(stream, fmt, body.filename or "query")

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

    @r.get("/agent")
    def agent_sessions(limit: int = 50, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
        """The saved conversations, the latest first, for the chat's list of
        past conversations. A signed-in person sees the ones they started;
        with nobody signed in (a local server, the API token) every one is
        listed, and a share link alone lists none."""
        ident = auth.current()
        limit = max(1, min(limit, 200))
        if ident is None:
            return {"sessions": ctx.agent.conversations(limit=limit)}
        if ident.uid:
            return {"sessions": ctx.agent.conversations(user=ident.uid, limit=limit)}
        if ident.method != "link" and ident.id:
            return {"sessions": ctx.agent.conversations(name=ident.id, limit=limit)}
        return {"sessions": []}

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
HUB_ADMIN_META = '<meta name="exuvia-hub" content="admin">'
# Kept under 300 characters with its JSON: an old client shows only that much.
HUB_GUIDE_HINT = (
    "This is an exuvia hub. MCP for a whole hub needs exuvia 0.3.11 or newer: "
    "use `uvx exuvia@latest mcp --url <hub>`, or run `uv tool upgrade exuvia` or `pipx upgrade exuvia`. "
    "For one project, use <hub>/p/<project>."
)

# A hub project's API; its workbench page (/p/<id>/) is not API.
_HUB_API = re.compile(r"^/p/[^/]+/api(/|$)")
_MCP_PATH = re.compile(r"^(/p/[^/]+)?/mcp/?$")  # mcp_http.py: auth as the API
# Anything under a hub project: its workbench page and its API.
_HUB_PROJECT_PATH = re.compile(r"^/p/([^/]+)(/|$)")


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


def _auth_method() -> str:
    """How this server lets people in: ``headers`` (a sign-in proxy),
    ``token`` (the shared API token) or ``none``."""
    return "headers" if auth.header_auth() else ("token" if os.environ.get("EXUVIA_API_TOKEN") else "none")


def _open_agent_mcp(local: Any, users: UserStore | None) -> McpServers:
    """The people's MCP server lists (agent_mcp.py), beside the user list."""
    return open_mcp_servers(local, people=users is not None, allow_private=private_allowed(_auth_method()))


def _operator_or_admin() -> bool:
    """An admin, or the operator (the API token, or a server nobody signs in to)."""
    ident = auth.current()
    return ident is None or ident.role == "admin"


def _agent_mcp_routes(app: FastAPI, store: McpServers, is_admin: Callable[[], bool] = _operator_or_admin) -> None:
    """``/api/me/mcp``: the MCP servers the built-in agent connects to when
    this person talks to it (agent_mcp.py). Header values go in and never
    come back out: a listing shows each header's name and last 4 characters.
    ``/api/mcp/presets``: the servers an admin offers everyone, which each
    person connects to from their own list (``is_admin``: who may change them)."""

    def owner(write: bool = False) -> str:
        if not agent_mcp_enabled():
            raise HTTPException(404, "MCP servers for the agent are off on this server (EXUVIA_AGENT_MCP=off)")
        ident = auth.current()
        who = store.owner()
        if who is None:
            raise HTTPException(403, "MCP servers belong to a signed-in person: sign in through the proxy")
        if write and ident is not None and ident.method == "token":
            raise HTTPException(403, "an agent token can't change MCP servers: sign in through the proxy")
        return who

    @app.get("/api/me/mcp")
    def mcp_list() -> dict[str, Any]:
        """This person's MCP servers. ``shared``: true on a server nobody
        signs in to, where everyone who can open it shares one list.
        ``private``: whether servers may point at private addresses."""
        who = owner()
        return {
            "servers": store.list(who), "shared": not store.people, "private": store.allow_private,
            "presets": store.presets(), "can_edit_presets": can_edit_presets(),
        }

    def can_edit_presets() -> bool:
        """An admin (or the operator), but not through an agent token."""
        ident = auth.current()
        return is_admin() and not (ident is not None and ident.method == "token")

    def require_preset_admin() -> None:
        if not agent_mcp_enabled():
            raise HTTPException(404, "MCP servers for the agent are off on this server (EXUVIA_AGENT_MCP=off)")
        if not can_edit_presets():
            raise HTTPException(403, "only an admin can change the MCP servers on offer: everyone sees them")

    @app.get("/api/mcp/presets")
    def mcp_presets() -> dict[str, Any]:
        """The MCP servers on offer to everyone, and whether the caller may change them."""
        if not agent_mcp_enabled():
            raise HTTPException(404, "MCP servers for the agent are off on this server (EXUVIA_AGENT_MCP=off)")
        return {"presets": store.presets(), "can_edit": can_edit_presets()}

    @app.put("/api/mcp/presets/{name}")
    def mcp_preset_put(name: str, body: McpPresetReq) -> dict[str, Any]:
        """Offer an MCP server to everyone, or change one on offer (admins)."""
        require_preset_admin()
        try:
            return store.put_preset(name, url=body.url, description=body.description, headers=body.headers)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/mcp/presets/{name}")
    def mcp_preset_delete(name: str) -> dict[str, Any]:
        """Stop offering a server (admins). Whoever connected to it keeps their copy."""
        require_preset_admin()
        if not store.delete_preset(name):
            raise HTTPException(404, f"no MCP server {name} on offer")
        return {"ok": True}

    @app.post("/api/me/mcp/presets/{name}/connect")
    def mcp_connect(name: str, body: McpConnectReq | None = None) -> dict[str, Any]:
        """Put a server on offer on this person's list, with the header
        values it asks for: the agent uses it from their next message on."""
        who = owner(write=True)
        try:
            return store.connect(who, name, body.headers if body else None)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/api/me/mcp/{name}")
    def mcp_put(name: str, body: McpServerReq) -> dict[str, Any]:
        """Add an MCP server (``url`` required) or change one."""
        who = owner(write=True)
        try:
            return store.put(who, name, url=body.url, headers=body.headers, enabled=body.enabled)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/me/mcp/{name}")
    def mcp_delete(name: str) -> dict[str, Any]:
        who = owner(write=True)
        if not store.delete(who, name):
            raise HTTPException(404, f"no MCP server {name}")
        return {"ok": True}

    @app.post("/api/me/mcp/{name}/test")
    async def mcp_test(name: str) -> dict[str, Any]:
        """Connect to a saved server and list its tools: ``{ok, tools}`` or
        ``{ok: false, error}``."""
        who = owner()
        server = await asyncio.to_thread(store.get, who, name)
        if server is None:
            raise HTTPException(404, f"no MCP server {name}")
        return await probe(server, allow_private=store.allow_private)

    @app.post("/api/me/mcp/{name}/oauth")
    async def mcp_sign_in(name: str, request: Request, body: McpSignInReq | None = None) -> dict[str, Any]:
        """Start an OAuth sign-in to a saved server (agent_mcp.py): ``{url}``,
        the page to open in a popup, which comes back to
        ``/api/me/mcp/oauth/callback``; or ``{ok, ...}`` when none was needed."""
        who = owner(write=True)
        server = await asyncio.to_thread(store.get, who, name)
        if server is None:
            raise HTTPException(404, f"no MCP server {name}")
        origin = (body.origin if body and body.origin else str(request.base_url)).rstrip("/")
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query:
            raise HTTPException(400, "origin must be the page's origin, such as https://exuvia.example.com")
        back = body.back if body and body.back and body.back.startswith(origin + "/") else None
        return await sign_in(store, who, server, origin + CALLBACK_PATH, back)

    @app.delete("/api/me/mcp/{name}/oauth")
    def mcp_sign_out(name: str) -> dict[str, Any]:
        """Forget a server's OAuth sign-in (its tokens stay valid at the server until they run out)."""
        who = owner(write=True)
        if store.get(who, name) is None:
            raise HTTPException(404, f"no MCP server {name}")
        store.set_oauth(who, name, None)
        return {"ok": True}

    @app.get(CALLBACK_PATH)
    async def mcp_oauth_callback(
        state: str = "", code: str | None = None, iss: str | None = None, error: str | None = None,
        error_description: str | None = None,
    ) -> HTMLResponse:
        """Where the authorization server sends the browser back: finishes the
        sign-in, says how it went and tells the page that opened it."""
        who = owner()
        why = f"{error}: {error_description}" if error and error_description else error
        result = await finish_sign_in(who, state, code, iss, why)
        name = html.escape(str(result.get("name", "the server")))
        if result.get("ok"):
            n = len(result.get("tools", []))
            text = f"Signed in to {name}: {n} tool{'' if n == 1 else 's'}."
        else:
            text = f"Not signed in to {name}: {html.escape(str(result.get('error')))}"
        if result.get("back"):
            text += f' <a href="{html.escape(result["back"])}">Back to exuvia</a>'
        message = json.dumps({"type": "exuvia-mcp-oauth", **result}).replace("<", "\\u003c")
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>exuvia: MCP sign-in</title>"
            '<meta name=viewport content="width=device-width">'
            f'<body style="font:15px system-ui,sans-serif;margin:2rem;max-width:36rem"><p>{text}</p>'
            f"<script>const m = {message};"
            "try { if (window.opener) window.opener.postMessage(m, location.origin); } catch (e) {}"
            "if (window.opener) { if (m.ok) setTimeout(() => window.close(), 1200); }"
            "else if (m.back) setTimeout(() => location.replace(m.back), m.ok ? 1200 : 6000);</script>"
        )


def _install_common(app: FastAPI, users: UserStore | None, command: str = "serve", sharing: Any = None) -> None:
    """CORS, who is signed in (EXUVIA_AUTH) and the optional shared-secret
    auth, for both app shapes. ``users`` is the user list each sign-in is
    recorded in (None when nobody signs in: EXUVIA_AUTH=token). ``command``
    is the CLI command this app is (``serve`` or ``hub``). ``sharing`` is a
    hub's sharing rules (sharing.py), whose share links let a request into
    one project without signing in."""
    origins = [o for o in os.environ.get("EXUVIA_CORS_ORIGINS", "").split(",") if o]
    if origins:  # e.g. https://lobster.mp when embedded in another app
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    headers = auth.header_auth()
    api_token = os.environ.get("EXUVIA_API_TOKEN")
    method = _auth_method()

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

    @app.get("/api/people")
    def people(q: str = "", limit: int = 20) -> dict[str, Any]:
        """Enabled users whose name or email contains ``q``: id, name and
        email, for choosing who to share a project with. For anyone signed in
        (and the operator); not for a request let in by a share link alone."""
        if users is None:
            raise HTTPException(404, "no user list: nobody signs in on this server (see EXUVIA_AUTH)")
        ident = auth.current()
        if ident is not None and not ident.uid:
            raise HTTPException(403, "sign in to look people up")
        return {"people": users.search(q, max(1, min(limit, 100)))}

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

    async def link_for(request: Request, path: str) -> str | None:
        """The hub project a share link on this request opens, when the
        request is for that project's page or API (sharing.py). Browsers
        carry it in a cookie scoped to the project's path (set by /s/<token>);
        scripts can send it as ``X-Exuvia-Link``. Never for MCP."""
        if sharing is None or _MCP_PATH.match(path):
            return None
        m = _HUB_PROJECT_PATH.match(path)
        token = request.cookies.get(LINK_COOKIE) or request.headers.get("x-exuvia-link")
        if not m or not looks_like_link(token):
            return None
        pid = await asyncio.to_thread(sharing.link_project, token)
        return pid if pid == m.group(1) else None

    @app.middleware("http")
    async def require_auth(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        path = request.url.path
        if path == "/healthz":  # liveness: no auth
            return await call_next(request)
        if path.startswith(dataapi.PREFIX):  # a data key, which the data API checks itself (dataapi.py)
            return await call_next(request)
        if auth.IN_PROCESS in request.scope:  # this process calling its own API, signed in already
            with auth.signed_in(request.scope[auth.IN_PROCESS]):
                return await call_next(request)
        # A hub with share links: the UI's static bundle (the same files the
        # package ships) and /s/<token>, which checks the link itself, are
        # open, so someone with a link and no sign-in can load the page.
        if sharing is not None and (path.startswith("/assets/") or path.startswith("/s/")):
            return await call_next(request)
        link = await link_for(request, path)
        ident = headers.identify(request.headers, request.client.host if request.client else None) if headers else None
        if ident is not None:
            if users is not None:  # recorded in the user list (see users.py)
                user = users.recent(ident) or await asyncio.to_thread(users.sign_in, ident)
                if user.disabled:
                    return disabled(path)
                ident = dataclasses.replace(ident, uid=user.id, role=user.role)
            with auth.signed_in(dataclasses.replace(ident, link=link)):
                return await call_next(request)
        # An agent token (users.py): signed in as the person who made it,
        # from anywhere, since MCP clients and scripts can't pass the proxy.
        bearer = request.headers.get("authorization", "")
        if users is not None and bearer[:7].lower() == "bearer " and looks_like_token(bearer[7:].strip()):
            owner = await asyncio.to_thread(users.by_token, bearer[7:].strip())
            if owner is not None:
                if owner.disabled:
                    return disabled(path)
                ident = auth.Identity(user=owner.name, email=owner.email, method="token", uid=owner.id, role=owner.role,
                                      link=link)
                with auth.signed_in(ident):
                    return await call_next(request)
        # A share link alone: read-only access to its one project, as nobody
        # in particular. The hub's API token, when the request has it too, wins.
        if link is not None and not (api_token and _token_ok(request, api_token)):
            with auth.signed_in(auth.Identity(user=None, email=None, method="link", link=link)):
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
    ctx.agent.mcp = _open_agent_mcp(ws.store.db, users)
    _agent_mcp_routes(app, ctx.agent.mcp)
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


# A hub project's API path, after /p/<id>/api.
_PROJECT_API_REST = re.compile(r"^/p/[^/]+/api(/.*)?$")
# POSTs that read and change nothing: SQL over outputs (DuckDB, read-only),
# and the same result as a download.
_READ_POSTS = re.compile(r"^/(query|views/[^/]+/query)(/export)?$")
# Opening a view re-runs its live nodes (see Workspace.refresh_view): their
# author made them live so whoever looks at the view sees current data.
_VIEW_REFRESH = re.compile(r"^/views/[^/]+/refresh$")
_TOOL_POST = re.compile(r"^/tools/([^/]+)$")


def _needed_role(request: Request) -> str:
    """The role a project API request needs (sharing.py): reading is for
    viewers (GETs, SQL queries, the read-only tools), everything else, from
    saving code to running a node or talking to the agent, for editors."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    m = _PROJECT_API_REST.match(request.url.path)
    rest = (m.group(1) or "/") if m else request.url.path
    if _READ_POSTS.match(rest) or _VIEW_REFRESH.match(rest):
        return "viewer"
    tool = _TOOL_POST.match(rest)
    if tool and tool.group(1) in READ_TOOLS:
        return "viewer"
    return "editor"


def _sharing_routes(app: FastAPI, hub: Any, require: Callable[[str, str], str], store: Callable[[], Any]) -> None:
    """The routes that share a hub project (sharing.py): who it is shared
    with, general access, ownership, share links and access requests. Only
    the owner (or an admin, or the hub's API token) changes them; anyone who
    can open the project sees who else can."""

    def by() -> str | None:
        return auth.current_uid()

    def people_on(sh: Any) -> None:
        if not sh.people:
            raise HTTPException(400, "nobody signs in on this hub (see EXUVIA_AUTH), so it is shared by link only")

    def answer(project: str) -> dict[str, Any]:
        sh = store()
        role = require(project, "viewer")
        manage = role == "owner"
        from .sharing import default_general

        out: dict[str, Any] = {
            "project": project, "you": role, "can_manage": manage, "people_enabled": sh.people,
            "default_access": default_general(), **sh.describe(project),
            "people": sh.members(project) if sh.people else [],
        }
        if manage:
            out["links"] = sh.links(project)
            out["requests"] = sh.requests(project) if sh.people else []
        return out

    @app.get("/api/projects/{project}/sharing")
    def sharing_get(project: str) -> dict[str, Any]:
        """Who the project is shared with and how. The owner also gets its
        share links and the access requests waiting for an answer."""
        return answer(project)

    @app.patch("/api/projects/{project}/sharing")
    def sharing_patch(project: str, body: SharingPatch) -> dict[str, Any]:
        """Change general access (``private``, ``view``, ``edit``) or hand
        the project to another owner (the old owner stays on as an editor)."""
        sh = store()
        require(project, "owner")
        try:
            if body.general is not None:
                people_on(sh)
                sh.set_general(project, body.general)
            if body.owner is not None:
                people_on(sh)
                sh.set_owner(project, body.owner, by=by())
        except KeyError:
            raise HTTPException(404, f"no user {body.owner}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return answer(project)

    @app.put("/api/projects/{project}/sharing/people")
    def sharing_grant(project: str, body: ShareReq) -> dict[str, Any]:
        """Share the project with someone (or change their role): ``user`` is a
        user id, or ``email`` names someone who may not have signed in yet."""
        sh = store()
        require(project, "owner")
        people_on(sh)
        try:
            sh.grant(project, body.role, user_id=body.user, email=body.email, by=by())
        except KeyError:
            raise HTTPException(404, f"no user {body.user}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return answer(project)

    @app.delete("/api/projects/{project}/sharing/people/{principal}")
    def sharing_revoke(project: str, principal: str) -> dict[str, Any]:
        """Stop sharing with someone (``user:<id>`` or ``email:<address>``).
        Anyone can take themselves off a project they were shared on."""
        from .sharing import email_principal, user_principal

        sh = store()
        ident = auth.current()
        mine = bool(ident and ident.uid) and principal in (
            user_principal(ident.uid or ""), email_principal(ident.email or "") if ident and ident.email else "",
        )
        require(project, "viewer" if mine else "owner")
        if not sh.revoke(project, principal):
            raise HTTPException(404, f"project {project} is not shared with {principal}")
        return answer(project) if at_least(sh.current_role(project), "viewer") else {"ok": True, "left": True}

    @app.post("/api/projects/{project}/sharing/links", status_code=201)
    def sharing_link_create(project: str, body: LinkReq, request: Request) -> dict[str, Any]:
        """A read-only share link. ``token`` and ``url`` are in this answer only."""
        sh = store()
        require(project, "owner")
        expires = None
        if body.expires_in_days is not None:
            if not 0 < body.expires_in_days <= MAX_LINK_DAYS:
                raise HTTPException(400, f"expires_in_days must be more than 0 and at most {MAX_LINK_DAYS}")
            expires = _time.time() + body.expires_in_days * 86400
        row, token = sh.create_link(project, body.name, expires, by=by())
        return {**row, "token": token, "url": f"/s/{token}"}

    @app.delete("/api/projects/{project}/sharing/links/{lid}")
    def sharing_link_revoke(project: str, lid: str) -> dict[str, Any]:
        sh = store()
        require(project, "owner")
        if not sh.revoke_link(project, lid):
            raise HTTPException(404, f"no link {lid}")
        return answer(project)

    @app.post("/api/projects/{project}/sharing/requests", status_code=201)
    def sharing_request(project: str, body: AccessReq) -> dict[str, Any]:
        """Ask the owner for ``role`` on a project you can't open (or only view)."""
        from .hub import PROJECT_ID
        from .sharing import RANK

        sh = store()
        people_on(sh)
        ident = auth.current()
        if ident is None or not ident.uid:
            raise HTTPException(403, "sign in to request access")
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        if body.role not in GRANT_ROLES:
            raise HTTPException(400, f"role must be one of {', '.join(GRANT_ROLES)}")
        role = sh.current_role(project)
        if RANK[role] >= RANK[body.role]:
            raise HTTPException(400, f"you already have {role} access to {project}")
        sh.request_access(project, ident.uid, body.role, body.message)
        return {"ok": True, "requested": sh.request_of(project, ident.uid)}

    @app.post("/api/projects/{project}/sharing/requests/{uid}/approve")
    def sharing_approve(project: str, uid: str, body: ApproveReq) -> dict[str, Any]:
        sh = store()
        require(project, "owner")
        try:
            sh.approve(project, uid, body.role, by=by())
        except KeyError:
            raise HTTPException(404, f"no request from {uid}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return answer(project)

    @app.delete("/api/projects/{project}/sharing/requests/{uid}")
    def sharing_dismiss(project: str, uid: str) -> dict[str, Any]:
        """Dismiss a request (the owner), or withdraw your own."""
        sh = store()
        ident = auth.current()
        mine = bool(ident and ident.uid == uid)
        if not mine:
            require(project, "owner")
        if not sh.drop_request(project, uid):
            raise HTTPException(404, f"no request from {uid}")
        return answer(project) if not mine else {"ok": True}

    @app.get("/s/{token}", include_in_schema=False)
    def share_link(token: str, request: Request, app: str | None = None) -> Any:
        """Open a share link: the browser keeps it in a cookie for that one
        project's pages and API, and lands on its workbench, or with
        ``?app=<view>`` on that one view as an app (full screen, no
        workbench around it)."""
        sh = hub.sharing
        project = sh.link_project(token) if sh is not None else None
        if project is None or not hub.exists(project):
            msg = "This share link doesn't work: it was revoked, it expired, or its project is gone. Ask whoever sent it for a new one."
            return HTMLResponse(f"<!doctype html><title>exuvia: link not found</title><p>{html.escape(msg)}</p>", status_code=404)
        to = f"/p/{project}/"
        if app and NODE_NAME.match(app):  # a view name, so the address stays on this project
            to += f"?app={app}"
        resp = RedirectResponse(to, status_code=303)
        expires = sh.link_expiry(token)
        max_age = int(expires - _time.time()) if expires else 400 * 86400
        resp.set_cookie(LINK_COOKIE, token, max_age=max(max_age, 1), path=f"/p/{project}/", httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
        return resp


def _group_routes(app: FastAPI, hub: Any, require: Callable[[str, str], str], role_on: Callable[[str], str | None]) -> None:
    """The routes of a hub's groups (groups.py): anyone signed in creates
    one; its owner manages its people; its editors set its variables and,
    with the project's owner, put projects in it."""
    gs = hub.groups

    def by() -> str | None:
        return auth.current_uid()

    def group_role(gid: str, need: str) -> str:
        """The request's role on a group, at least ``need`` (404: no such
        group, or one the request has no role on; 403: not enough)."""
        role = gs.role(gid, auth.current())
        if role is None:
            raise HTTPException(404, f"no group {gid}")
        if not at_least(role, need):
            what = "change its variables or projects" if need == "editor" else "do that"
            raise HTTPException(403, f"only an {'editor' if need == 'editor' else 'owner'} of group "
                                     f"{gs.describe(gid)['name']} can {what}")
        return role

    def people_on() -> None:
        if not gs.people:
            raise HTTPException(400, "nobody signs in on this hub (see EXUVIA_AUTH), so a group has no people to add")

    def env_answer(gid: str) -> dict[str, Any]:
        return {"variables": envvars_public(hub.group_env(gid).rows()), "requests": []}

    def answer(gid: str) -> dict[str, Any]:
        role = group_role(gid, "viewer")
        projects = []
        for pid in gs.projects(gid):
            if hub.exists(pid):
                projects.append({"id": pid, "name": hub.project_name(pid), "access": role_on(pid)})
        return {
            **gs.describe(gid), "you": role, "can_edit": at_least(role, "editor"), "can_manage": role == "owner",
            "people_enabled": gs.people, "people": gs.members(gid) if gs.people else [],
            "projects": projects, **env_answer(gid),
        }

    def env_error(e: Exception) -> HTTPException:
        return HTTPException(400, str(e))

    @app.get("/api/groups")
    def groups_list() -> dict[str, Any]:
        """The groups this request has a role on."""
        ident = auth.current()
        return {"groups": gs.visible(ident), "can_create": ident is None or bool(ident.uid), "people": gs.people}

    @app.post("/api/groups", status_code=201)
    def group_create(body: GroupCreate) -> dict[str, Any]:
        """A new group, owned by whoever creates it."""
        ident = auth.current()
        if ident is not None and not ident.uid:
            raise HTTPException(403, "sign in to create a group")
        try:
            gid = gs.create(body.name, by())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return answer(gid)

    @app.get("/api/groups/{gid}")
    def group_get(gid: str) -> dict[str, Any]:
        """The group: its owner and people, its projects and its variables
        (names, and plain values: a secret's value is never sent back)."""
        return answer(gid)

    @app.patch("/api/groups/{gid}")
    def group_patch(gid: str, body: GroupPatch) -> dict[str, Any]:
        """Rename the group, or hand it to another owner (the old owner stays
        on as an editor)."""
        group_role(gid, "owner")
        try:
            if body.name is not None:
                gs.rename(gid, body.name)
            if body.owner is not None:
                people_on()
                gs.set_owner(gid, body.owner, by=by())
        except KeyError:
            raise HTTPException(404, f"no user {body.owner}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        hub.env_changed(gs.projects(gid))
        return answer(gid)

    @app.delete("/api/groups/{gid}")
    def group_delete(gid: str) -> dict[str, Any]:
        """Delete the group and its variables. Its projects stay, outside any group."""
        group_role(gid, "owner")
        projects = gs.delete(gid)
        hub.env_changed(projects)
        return {"ok": True, "projects": projects}

    @app.put("/api/groups/{gid}/people")
    def group_grant(gid: str, body: ShareReq) -> dict[str, Any]:
        """Add someone to the group (or change their role): a ``viewer`` can
        open its projects, an ``editor`` can change and run them and set the
        group's variables."""
        group_role(gid, "owner")
        people_on()
        try:
            gs.grant(gid, body.role, user_id=body.user, email=body.email, by=by())
        except KeyError:
            raise HTTPException(404, f"no user {body.user}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return answer(gid)

    @app.delete("/api/groups/{gid}/people/{principal}")
    def group_revoke(gid: str, principal: str) -> dict[str, Any]:
        """Take someone off the group. Anyone can leave a group they are in."""
        from .sharing import email_principal, user_principal

        ident = auth.current()
        mine = bool(ident and ident.uid) and principal in (
            user_principal(ident.uid or ""), email_principal(ident.email or "") if ident and ident.email else "",
        )
        group_role(gid, "viewer" if mine else "owner")
        if not gs.revoke(gid, principal):
            raise HTTPException(404, f"group {gid} has no {principal}")
        return answer(gid) if gs.role(gid, auth.current()) else {"ok": True, "left": True}

    @app.get("/api/groups/{gid}/env")
    def group_env_list(gid: str) -> dict[str, Any]:
        group_role(gid, "viewer")
        return env_answer(gid)

    @app.put("/api/groups/{gid}/env/{name}")
    def group_env_put(gid: str, name: str, body: EnvVarReq) -> dict[str, Any]:
        """Set one of the group's variables: every project in it gets it,
        unless the project sets the same name itself."""
        group_role(gid, "editor")
        try:
            hub.group_env(gid).set(name, body.value, body.secret, auth.current_id())
        except EnvError as e:
            raise env_error(e) from e
        hub.env_changed(gs.projects(gid))
        return env_answer(gid)

    @app.delete("/api/groups/{gid}/env/{name}")
    def group_env_delete(gid: str, name: str) -> dict[str, Any]:
        group_role(gid, "editor")
        if hub.group_env(gid).delete(name):
            hub.env_changed(gs.projects(gid))
        return env_answer(gid)

    @app.put("/api/projects/{project}/group")
    def project_group(project: str, body: ProjectGroupReq) -> dict[str, Any]:
        """Put a project in a group, move it to another, or (``group``: null)
        take it out. Adding needs editor on the group and ownership of the
        project: the group's people get their role on the project, and
        everyone who can edit the project can read the group's secrets.
        Taking it out needs either."""
        require(project, "viewer")
        ident = auth.current()
        mine = role_on(project) == "owner"
        current = gs.group_of(project, fresh=True)
        target = body.group or None
        if current is not None and current["id"] == target:
            return {"project": project, "group": current}
        if current is not None and not mine and not at_least(gs.role(current["id"], ident), "editor"):
            raise HTTPException(
                403, f"only the owner of project {project} or an editor of group {current['name']} can take it out of the group")
        if target is not None:
            role = gs.role(target, ident)
            if role is None:
                raise HTTPException(404, f"no group {target}")
            if not at_least(role, "editor"):
                raise HTTPException(403, f"only an editor of group {gs.describe(target)['name']} can add projects to it")
            if not mine:
                raise HTTPException(
                    403, f"only the owner of project {project} can put it in a group: the group's people get access "
                         "to it, and its editors can read the group's secrets")
        gs.set_project_group(project, target, by=by())
        hub.env_changed([project])
        return {"project": project, "group": gs.group_of(project, fresh=True)}


def create_hub_app(root: str | Path, templates: str | Path | None = None) -> FastAPI:
    from .hub import HUB_USERS_DB, PROJECT_ID, Hub, StillInUse

    hub = Hub(root, templates)
    # One user list for every project on the hub (see users.py), and the
    # projects' sharing beside it (sharing.py) whenever anyone has to get in:
    # people sign in, or there is an API token for share links to stand in for.
    if auth.identifies_people():
        hub.users = open_users(hub.root / HUB_USERS_DB)
    if hub.users is not None or os.environ.get("EXUVIA_API_TOKEN"):
        hub.sharing = open_sharing(hub.root / HUB_USERS_DB, hub.users)
    # Groups of projects (groups.py), on every hub: their variables are worth
    # sharing even where nobody signs in.
    hub.groups = open_groups(hub.root / HUB_USERS_DB, hub.users, hub.sharing)
    if hub.sharing is not None:
        hub.sharing.groups = hub.groups
    hub.seal_group_env()
    hub.activity = ActivityStore(hub.groups.db, scope=hub.groups.scope)
    hub.data_keys = DataKeyStore(hub.groups.db, scope=hub.groups.scope)

    def note_use(project: str, need: str) -> None:
        """Who opened (and, needing edit access, changed) the project: the
        home page's Recently used and Recently changed."""
        hub.activity.touch(project, auth.current_uid(), edited=need != "viewer")

    def role_on(project: str) -> str | None:
        """The request's role on a project: owner for everyone on a hub
        without sharing (nobody has to get in)."""
        return hub.sharing.current_role(project) if hub.sharing is not None else "owner"

    def denial(project: str, role: str | None, need: str) -> str:
        if role is None:
            ident = auth.current()
            ask = " or request access from its page" if ident is not None and ident.uid else ""
            return f"you don't have access to project {project}: ask its owner to share it with you{ask}"
        if need == "editor":
            return f"you can view project {project} but not change it: ask its owner for edit access"
        return f"only the owner of project {project} (or a hub admin) can do that"

    def require(project: str, need: str) -> str:
        """The request's role on an existing project, at least ``need``
        (404: no such project; 403: not allowed)."""
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        role = role_on(project)
        if not at_least(role, need):
            raise HTTPException(403, denial(project, role, need))
        return role  # type: ignore[return-value]

    def is_admin() -> bool:
        """An admin, or the operator (the API token, or a hub nobody signs in to)."""
        ident = auth.current()
        return ident is None or ident.role == "admin"

    def require_admin() -> None:
        """Hub-wide operations (export, import): an admin, or the operator."""
        if not is_admin():
            raise HTTPException(403, "admins only: this covers every project on the hub")

    def sharing_store() -> Any:
        if hub.sharing is None:
            raise HTTPException(404, "no sharing on this hub: nobody has to sign in or send a token to get in")
        return hub.sharing

    async def path_project(scope: dict[str, Any]) -> Any:
        project = scope.get("path_params", {}).get("project", "")
        if not PROJECT_ID.match(project) or not hub.exists(project):
            return JSONResponse({"detail": f"no project {project}"}, status_code=404)
        role = await asyncio.to_thread(role_on, project)
        if role is None:
            return JSONResponse({"detail": denial(project, role, "viewer")}, status_code=403)
        await asyncio.to_thread(note_use, project, "viewer")
        return await hub.get(project)

    def path_role(scope: dict[str, Any]) -> str | None:
        return role_on(scope.get("path_params", {}).get("project", ""))

    # MCP over HTTP (mcp_http.py): /mcp for every project, /p/<id>/mcp for one.
    hub_mcp = mcp_http.hub_endpoint(hub, role_on)
    project_mcp = mcp_http.project_endpoint(path_project, path_role)

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
    _install_common(app, hub.users, "hub", hub.sharing)
    app.state.hub = hub
    hub.app = app  # the hub's MCP calls its API in process (mcp_http.HubLocalBackend)
    hub.mcp =_open_agent_mcp(hub.root / HUB_USERS_DB, hub.users)
    _agent_mcp_routes(app, hub.mcp, is_admin)
    dataapi.install(app, hub, role_on, is_admin)

    async def get_ctx(project: str, request: Request) -> Ctx:
        # The rules may need a database read: off the event loop (the thread
        # gets this request's context, so it sees who is signed in).
        need = _needed_role(request)
        await asyncio.to_thread(require, project, need)
        await asyncio.to_thread(note_use, project, need)
        return await hub.get(project)

    @app.get("/p/{project}/api/access")
    def project_access(project: str) -> dict[str, Any]:
        """What this request may do in the project, for its workbench: the
        role (null: none, and the page offers to request access), its owner
        and general access, and whether a share link let it in."""
        if not PROJECT_ID.match(project) or not hub.exists(project):
            raise HTTPException(404, f"no project {project}")
        ident = auth.current()
        role = role_on(project)
        out: dict[str, Any] = {
            "project": project, "role": role, "sharing": hub.sharing is not None,
            "people": bool(hub.sharing is not None and hub.sharing.people),
            "user": ident.as_dict() if ident and ident.method != "link" else None,
            "link": bool(ident and ident.link == project),
        }
        if hub.sharing is not None:
            out.update(hub.sharing.describe(project) if role else {})
            signed_in = bool(ident and ident.uid)
            out["can_request"] = signed_in and hub.sharing.people and role != "owner"
            out["requested"] = hub.sharing.request_of(project, ident.uid) if signed_in else None  # type: ignore[union-attr]
        group = hub.groups.group_of(project) if role else None
        out["group"] = {**group, "access": hub.groups.role(group["id"], ident)} if group else None
        return out

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
        """The projects this request can open (all of them without sharing),
        each with the caller's role, owner and general access when the hub
        has sharing. `auth`: whether clients need the API token (the home
        page's MCP setup says so)."""
        out: dict[str, Any] = {
            "projects": with_activity(visible_projects()), "templates": hub.templates(),
            "auth": bool(os.environ.get("EXUVIA_API_TOKEN")), "sharing": None,
            "groups": hub.groups.visible(auth.current()),
            "hub_env": {"variables": len(hub.groups.hub_env_rows()), "can_edit": is_admin()},
        }
        if agent_mcp_enabled():
            # The MCP servers an admin offers everyone (agent_mcp.py): how many, and whether the caller changes them.
            ident = auth.current()
            out["agent_mcp"] = {
                "presets": len(hub.mcp.presets()),
                "can_edit": is_admin() and not (ident is not None and ident.method == "token"),
            }
        if hub.sharing is not None:
            from .sharing import new_project_general

            # What the New project dialog starts at: whoever creates it owns it.
            owned = auth.current_uid() is not None
            out["sharing"] = {"people": hub.sharing.people, "default_access": new_project_general(owned)}
        return out

    def visible_projects() -> list[dict[str, Any]]:
        listed = [{**p, "group": hub.groups.group_of(p["id"])} for p in hub.list()]
        if hub.sharing is None:
            return listed
        seen = hub.sharing.overview([p["id"] for p in listed], auth.current())
        return [{**p, **seen[p["id"]]} for p in listed if p["id"] in seen]

    def with_activity(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each project with when the caller last opened it (``opened_at``)
        and changed it (``edited_at``; creating it counts), and when anyone
        last changed it (``changed_at``): null when not known."""
        mine = hub.activity.of(auth.current_uid())
        changed = hub.activity.changed()
        out = []
        for p in projects:
            used = mine.get(p["id"], {})
            times = [t for t in (changed.get(p["id"]), hub.files_changed(p["id"])) if t is not None]
            out.append({**p, "opened_at": used.get("opened_at"), "edited_at": used.get("edited_at"),
                        "changed_at": max(times, default=None)})
        return out

    hub.visible = visible_projects  # the hub's MCP lists the same projects (mcp_http.py)

    @app.post("/api/projects")
    async def project_create(body: ProjectCreate) -> dict[str, Any]:
        ident = auth.current()
        if ident is not None and not ident.uid:
            raise HTTPException(403, "sign in to create a project")
        if body.owner is not None and ident is not None and ident.role != "admin":
            raise HTTPException(403, "only an admin (or the hub's API token) can create a project for someone else")
        group = body.group or None
        if group is not None:
            # Checked before the project exists, as putting one in the group
            # later would be: the creator owns it, and must edit the group.
            role = hub.groups.role(group, ident)
            if role is None:
                raise HTTPException(404, f"no group {group}")
            if not at_least(role, "editor"):
                raise HTTPException(
                    403, f"only an editor of group {hub.groups.describe(group)['name']} can add projects to it")
        try:
            await hub.create(body.id, body.name, body.template, access=body.access, owner=body.owner)
        except FileExistsError as e:
            raise HTTPException(409, f"project {body.id} already exists") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        out = {"id": body.id, "name": (await hub.get(body.id)).ws.project.name}
        # Whoever creates it worked on it: it heads their Recently worked on.
        await asyncio.to_thread(note_use, body.id, "owner")
        if group is not None:
            hub.groups.set_project_group(body.id, group, by=auth.current_uid())
            hub.env_changed([body.id])
            out["group"] = hub.groups.group_of(body.id, fresh=True)
        return out

    @app.put("/api/projects/{project}/template")
    async def project_apply_template(project: str, body: TemplateApply) -> dict[str, Any]:
        """Replace the project's code with a template's; history and outputs stay."""
        require(project, "owner")
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
        require(project, "owner")
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

    @app.get("/api/admin/settings")
    def admin_settings() -> dict[str, Any]:
        """How the server is set up, for the admin page (admin.py): read-only,
        set by environment variables, with no secret in it."""
        require_admin()
        from .admin import settings

        return {"groups": settings(_auth_method())}

    @app.get("/api/hub/export")
    async def hub_export() -> FileResponse:
        """All projects with their run history and artifacts, as one .tar.gz."""
        require_admin()
        path = await asyncio.to_thread(hub.export)
        return FileResponse(path, media_type="application/gzip", filename="exuvia-hub.tar.gz")

    @app.post("/api/hub/import")
    async def hub_import(request: Request) -> dict[str, Any]:
        """Restore projects from an export. Projects in the archive replace
        same-named ones; others are left alone."""
        require_admin()
        blob = await request.body()
        try:
            restored = await hub.restore(blob)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except StillInUse as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True, "projects": restored}

    # The hub's own variables (groups.py, "Hub variables"): every project
    # gets them, under its group's and its own. Anyone on the hub sees their
    # names and plain values; only an admin (or the operator) changes them.
    def hub_env_answer() -> dict[str, Any]:
        return {"variables": envvars_public(hub.hub_env().rows()), "requests": [], "can_edit": is_admin()}

    def require_hub_env_admin() -> None:
        if not is_admin():
            raise HTTPException(403, "only a hub admin can change the hub's variables: every project on the hub gets them")

    @app.get("/api/hub/env")
    def hub_env_list() -> dict[str, Any]:
        return hub_env_answer()

    @app.put("/api/hub/env/{name}")
    def hub_env_put(name: str, body: EnvVarReq) -> dict[str, Any]:
        """Set one of the hub's variables: every project on the hub gets it,
        unless the project or its group sets the same name."""
        require_hub_env_admin()
        try:
            hub.hub_env().set(name, body.value, body.secret, auth.current_id())
        except EnvError as e:
            raise HTTPException(400, str(e)) from e
        hub.env_changed()
        return hub_env_answer()

    @app.delete("/api/hub/env/{name}")
    def hub_env_delete(name: str) -> dict[str, Any]:
        require_hub_env_admin()
        if hub.hub_env().delete(name):
            hub.env_changed()
        return hub_env_answer()

    _sharing_routes(app, hub, require, sharing_store)
    _group_routes(app, hub, require, role_on)

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

        def marked(meta: str) -> HTMLResponse:
            """The UI's page with a meta tag saying which of the hub's own pages it is."""
            page = (STATIC / "index.html").read_text(encoding="utf-8")
            # Into <head>, or right after the doctype (before it would mean quirks mode).
            out, n = re.subn(r"<head\b[^>]*>", lambda m: m.group(0) + meta, page, count=1, flags=re.I)
            if not n:
                out = re.sub(r"^(\s*<!doctype[^>]*>)?", lambda m: (m.group(0) or "") + meta, page, count=1, flags=re.I)
            return HTMLResponse(out)

        @app.get("/", response_class=HTMLResponse)
        def hub_home() -> HTMLResponse:
            return marked(HUB_HOME_META)

        @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
        def hub_admin_page() -> HTMLResponse:
            """The admin page: the page itself says who may use it (its API is admins only)."""
            return marked(HUB_ADMIN_META)

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
