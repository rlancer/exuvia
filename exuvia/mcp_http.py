"""MCP over Streamable HTTP, served by ``exuvia serve`` and ``exuvia hub``
themselves, so a client needs only the address (README "MCP"):

* ``exuvia hub``: ``<hub>/mcp`` serves every project, with the same tools as
  ``exuvia mcp --url <hub>`` (``list_projects``, ``create_project`` and each
  project tool with a ``project`` argument), and ``<hub>/p/<project>/mcp``
  serves that one project.
* ``exuvia serve``: ``<server>/mcp`` serves its project.

The tools run in this process, as ``POST /api/tools/{name}`` does: nothing is
relayed. The endpoint sits behind the same auth as the API (server.py
``require_auth``): the shared ``EXUVIA_API_TOKEN`` or a person's agent token
as ``Authorization: Bearer``, so the client works as that person. Changes and
runs are recorded with the origin ``mcp``.

The session manager is stateless: every POST stands alone, so any replica can
answer it and a restart drops nothing. The tools never ask the client
anything back, which is all a session would buy.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from .mcp_server import HUB_GUIDE_TOOL, ORIGIN, build_server, hub_instructions, hub_tool_list

# What the request being served is about. Set by McpEndpoint before the MCP
# server sees the request; the server's tasks start from the request's task
# and so see the request's values (and its signed-in person, auth.py).
_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("exuvia_mcp_ctx", default=None)
_base_url: contextvars.ContextVar[str] = contextvars.ContextVar("exuvia_mcp_base_url", default="")


def _tool_defs() -> list[dict[str, Any]]:
    from .agent import TOOLS

    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in TOOLS]


class ProjectBackend:
    """One loaded project: ``exuvia serve``'s own, or the hub project the
    request's path names."""

    def instructions_now(self) -> str:
        return _ctx.get().agent.instructions()

    async def instructions(self) -> str:
        return self.instructions_now()

    async def tools(self) -> list[dict[str, Any]]:
        return _tool_defs()

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        ok, text, _ = await _ctx.get().agent.call_tool(name, args, ORIGIN)
        return ok, text


class HubLocalBackend:
    """Every project on this hub, as HubBackend (mcp_server.py) offers them
    from outside, but called in this process."""

    guide_tool = HUB_GUIDE_TOOL

    def __init__(self, hub: Any):
        self.hub = hub

    def instructions_now(self) -> str:
        return hub_instructions(_base_url.get(), self.hub.list())

    async def instructions(self) -> str:
        return self.instructions_now()

    async def tools(self) -> list[dict[str, Any]]:
        return hub_tool_list(_tool_defs())

    async def _project(self, project: Any) -> tuple[Any, str | None]:
        from .hub import PROJECT_ID

        if not isinstance(project, str) or not project:
            return None, "INVALID_INPUT: name the project as `project` (its id; list_projects shows them)"
        if not PROJECT_ID.match(project) or not self.hub.exists(project):
            return None, f"no project {project} on this hub: list_projects shows them"
        return await self.hub.get(project), None

    async def guide(self, args: dict[str, Any]) -> tuple[bool, str]:
        if not args.get("project"):
            return True, self.instructions_now()
        ctx, problem = await self._project(args["project"])
        return (False, problem) if problem else (True, ctx.agent.instructions())

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if name == "list_projects":
            return True, json.dumps({"projects": self.hub.list(), "templates": self.hub.templates()}, indent=1)
        if name == "create_project":
            pid, display, template = args.get("id"), args.get("name"), args.get("template")
            if not isinstance(pid, str) or not all(v is None or isinstance(v, str) for v in (display, template)):
                return False, "INVALID_INPUT: `id` is required, and `id`, `name` and `template` are strings"
            try:
                await self.hub.create(pid, display, template)
            except FileExistsError:
                return False, f"project {pid} already exists"
            except ValueError as e:
                return False, str(e)
            made = await self.hub.get(pid)
            return True, json.dumps({"id": pid, "name": made.ws.project.name, "workbench": f"{_base_url.get()}/p/{pid}/"})
        args = dict(args)
        ctx, problem = await self._project(args.pop("project", None))
        if problem:
            return False, problem
        ok, text, _ = await ctx.agent.call_tool(name, args, ORIGIN)
        return ok, text


def _base(scope: dict[str, Any]) -> str:
    """The address the client used, for links in what the agent is told.
    Behind a TLS proxy uvicorn may see plain http: X-Forwarded-Proto says
    otherwise. Only text for the agent, so an untrusted header can't do harm."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    scheme = headers.get("x-forwarded-proto", "").split(",")[0].strip() or scope.get("scheme", "http")
    host = headers.get("x-forwarded-host", "").split(",")[0].strip() or headers.get("host", "")
    if not host and scope.get("server"):
        host = "%s:%s" % scope["server"]
    return f"{scheme}://{host}"


class McpEndpoint:
    """The ASGI app at an MCP path. ``resolve`` turns the request into the
    project it is about (a Response instead: the request ends there)."""

    def __init__(self, backend: Any, resolve: Callable[[dict[str, Any]], Awaitable[Any]] | None = None):
        self.server = build_server(backend, backend.instructions_now)
        self.resolve = resolve
        self.manager: Any = None

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """For the app's lifespan. A session manager runs only once, so each
        lifespan gets its own (an app can be started again, as tests do)."""
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        self.manager = StreamableHTTPSessionManager(self.server, stateless=True)
        async with self.manager.run():
            try:
                yield
            finally:
                self.manager = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        from starlette.responses import JSONResponse

        if self.manager is None:
            await JSONResponse({"detail": "the MCP endpoint is not running"}, status_code=503)(scope, receive, send)
            return
        ctx = None
        if self.resolve is not None:
            ctx = await self.resolve(scope)
            if isinstance(ctx, JSONResponse):
                await ctx(scope, receive, send)
                return
        ctx_token, url_token = _ctx.set(ctx), _base_url.set(_base(scope))
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            _ctx.reset(ctx_token)
            _base_url.reset(url_token)


MCP_METHODS = ["GET", "POST", "DELETE"]


def add_routes(app: Any, prefix: str, endpoint: McpEndpoint) -> None:
    """``<prefix>/mcp`` (and with a trailing slash, which some clients add)."""
    from starlette.routing import Route

    for path in (f"{prefix}/mcp", f"{prefix}/mcp/"):
        app.router.routes.append(Route(path, endpoint=endpoint, methods=MCP_METHODS, include_in_schema=False))


def project_endpoint(get_ctx: Callable[[dict[str, Any]], Awaitable[Any]]) -> McpEndpoint:
    return McpEndpoint(ProjectBackend(), get_ctx)


def hub_endpoint(hub: Any) -> McpEndpoint:
    return McpEndpoint(HubLocalBackend(hub))
