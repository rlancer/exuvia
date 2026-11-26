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
runs are recorded with the origin ``mcp``. On a hub with sharing
(sharing.py) a client sees only the projects its person can open, and in a
project they can only view it gets the read-only tools.

The session manager is stateless: every POST stands alone, so any replica can
answer it and a restart drops nothing. The tools never ask the client
anything back, which is all a session would buy: ``request_secret`` answers
with the workbench link instead of a URL elicitation (mcp_server.py).
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from .mcp_server import (
    HUB_API_TOOLS,
    HUB_GUIDE_TOOL,
    NO_PROJECT,
    ORIGIN,
    PROJECT_FREE,
    build_server,
    hub_api_call,
    hub_instructions,
    hub_tool_list,
    project_free,
    secret_request_links,
)

# What the request being served is about. Set by McpEndpoint before the MCP
# server sees the request; the server's tasks start from the request's task
# and so see the request's values (and its signed-in person, auth.py).
_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("exuvia_mcp_ctx", default=None)
_base_url: contextvars.ContextVar[str] = contextvars.ContextVar("exuvia_mcp_base_url", default="")
# The workbench of the project an endpoint serves: the address the client
# used, without the /mcp at its end (``<server>`` or ``<hub>/p/<project>``).
_workbench: contextvars.ContextVar[str] = contextvars.ContextVar("exuvia_mcp_workbench", default="")
# The request's role in the project a /p/<id>/mcp request is about (sharing.py).
_role: contextvars.ContextVar[str | None] = contextvars.ContextVar("exuvia_mcp_role", default="owner")


def _may(role: str | None, tool: str) -> bool:
    """Whether ``role`` may call ``tool``: viewers only the read-only ones."""
    from .agent import READ_TOOLS
    from .sharing import at_least

    return tool in READ_TOOLS if not at_least(role, "editor") else True


def _view_only(project: str | None) -> str:
    where = f"project {project}" if project else "this project"
    return f"PERMISSION: you can view {where} but not change it; ask its owner for edit access"


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
        role = _role.get()
        return [t for t in _tool_defs() if _may(role, t["name"])]

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if not _may(_role.get(), name):
            return False, _view_only(None)
        ok, text, _ = await _ctx.get().agent.call_tool(name, args, ORIGIN)
        if ok and name == "request_secret":
            base = _workbench.get()
            text = secret_request_links(text, base, f"--url {base}")
        return ok, text


class HubLocalBackend:
    """Every project on this hub, as HubBackend (mcp_server.py) offers them
    from outside, but called in this process."""

    guide_tool = HUB_GUIDE_TOOL

    def __init__(self, hub: Any, role_of: Callable[[str], str | None] | None = None):
        self.hub = hub
        self.role_of = role_of or (lambda _project: "owner")

    def instructions_now(self) -> str:
        return hub_instructions(_base_url.get(), self.hub.visible())

    async def instructions(self) -> str:
        return self.instructions_now()

    async def tools(self) -> list[dict[str, Any]]:
        return hub_tool_list(_tool_defs())

    async def _api(self, method: str, path: str, body: Any, query: dict[str, Any] | None) -> tuple[int, Any]:
        """A request to this hub's own HTTP API, served in this process as
        the person this MCP request signed in as (auth.IN_PROCESS), so the
        hub tools get the API's checks and answers (mcp_server.hub_api_call)."""
        import httpx

        from . import auth

        app, ident = self.hub.app, auth.current()

        async def as_caller(scope: dict[str, Any], receive: Any, send: Any) -> None:
            scope[auth.IN_PROCESS] = ident
            await app(scope, receive, send)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=as_caller), base_url="http://hub", headers={"X-Exuvia-Origin": ORIGIN},
        ) as http:
            r = await http.request(method, path, json=body, params=query)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    async def _project(self, project: Any) -> tuple[Any, str | None]:
        from .hub import PROJECT_ID

        if not isinstance(project, str) or not project:
            return None, NO_PROJECT
        if not PROJECT_ID.match(project) or not self.hub.exists(project):
            return None, f"no project {project} on this hub: list_projects shows them"
        if self.role_of(project) is None:
            return None, f"PERMISSION: you don't have access to project {project}; ask its owner to share it with you"
        return await self.hub.get(project), None

    async def guide(self, args: dict[str, Any]) -> tuple[bool, str]:
        if not args.get("project"):
            return True, self.instructions_now()
        ctx, problem = await self._project(args["project"])
        return (False, problem) if problem else (True, ctx.agent.instructions())

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if name == "list_projects":
            return True, json.dumps({"projects": self.hub.visible(), "templates": self.hub.templates()}, indent=1)
        if name in PROJECT_FREE:
            return await project_free(name, args, "hub")
        if name in HUB_API_TOOLS:
            return await hub_api_call(name, args, self._api, _base_url.get())
        args = dict(args)
        project = args.pop("project", None)
        ctx, problem = await self._project(project)
        if problem:
            return False, problem
        if not _may(self.role_of(project), name):
            return False, _view_only(project)
        ok, text, _ = await ctx.agent.call_tool(name, args, ORIGIN)
        if ok and name == "request_secret":
            base = f"{_base_url.get()}/p/{project}"
            text = secret_request_links(text, base, f"--url {base}")
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

    def __init__(
        self, backend: Any, resolve: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        role: Callable[[dict[str, Any]], str | None] | None = None,
    ):
        # Stateless: no connection to send the client a request on (elicitation).
        self.server = build_server(backend, backend.instructions_now, elicit=False)
        self.resolve = resolve
        self.role = role
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
        path = (scope.get("root_path", "") + scope.get("path", "")).rstrip("/")
        bench_token = _workbench.set(_base(scope) + path.removesuffix("/mcp"))
        role_token = _role.set(self.role(scope) if self.role is not None else "owner")
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            _ctx.reset(ctx_token)
            _base_url.reset(url_token)
            _workbench.reset(bench_token)
            _role.reset(role_token)


MCP_METHODS = ["GET", "POST", "DELETE"]


def add_routes(app: Any, prefix: str, endpoint: McpEndpoint) -> None:
    """``<prefix>/mcp`` (and with a trailing slash, which some clients add)."""
    from starlette.routing import Route

    for path in (f"{prefix}/mcp", f"{prefix}/mcp/"):
        app.router.routes.append(Route(path, endpoint=endpoint, methods=MCP_METHODS, include_in_schema=False))


def project_endpoint(
    get_ctx: Callable[[dict[str, Any]], Awaitable[Any]], role: Callable[[dict[str, Any]], str | None] | None = None,
) -> McpEndpoint:
    """``role`` says what the request may do in the project (sharing.py);
    unset: everything (``exuvia serve``)."""
    return McpEndpoint(ProjectBackend(), get_ctx, role)


def hub_endpoint(hub: Any, role_of: Callable[[str], str | None] | None = None) -> McpEndpoint:
    return McpEndpoint(HubLocalBackend(hub, role_of))
