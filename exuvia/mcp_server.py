"""exuvia as an MCP server (``exuvia mcp``).

Gives any MCP client (Claude Code, Claude Desktop, Cursor, ...) the same tools
the built-in agent has: list, read and write nodes, helpers and views, run a
node, query outputs, define parameters, browse and restore revisions. The
agent's instructions (how exuvia works, this project's parameters and notes)
are the server's ``instructions``, and the ``guide`` tool returns them too,
for clients that do not pass instructions to their model.

Two ways to reach a project:

* ``exuvia mcp DIR``: open the project in this process. Nothing else needs to
  be running. Schedules don't fire here (``exuvia serve`` runs them).
* ``exuvia mcp --url URL``: relay to a running ``exuvia serve`` (``URL`` is
  its address) or a hub project (``URL`` is ``<hub>/p/<project>``) through
  ``POST /api/tools/{name}``. The server stays the only process that owns the
  project, so its UI shows every change as it happens. ``EXUVIA_API_TOKEN`` is
  sent as a bearer token when set. If nothing answers at ``URL``, the relay
  starts anyway and each tool call says how to start the workbench.

Changes and runs are recorded with the origin ``mcp``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Protocol

ORIGIN = "mcp"
GUIDE_TOOL = {
    "name": "guide",
    "description": (
        "How exuvia works and how to write this project's nodes, helpers, views and parameters, "
        "with the project's own parameters and notes. Read it before you write or change anything."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
REMOTE_TIMEOUT = 900.0  # a run_node call waits for the run to finish


class Backend(Protocol):
    async def instructions(self) -> str: ...
    async def tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]: ...


class LocalBackend:
    """A project opened in this process."""

    def __init__(self, root: Path):
        from .agent import Agent
        from .workspace import Workspace

        self.ws = Workspace(root)
        self.agent = Agent(self.ws)

    async def start(self) -> None:
        await self.ws.start()

    async def stop(self) -> None:
        await self.ws.stop()

    async def instructions(self) -> str:
        return self.agent.instructions()

    async def tools(self) -> list[dict[str, Any]]:
        from .agent import TOOLS

        return TOOLS

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        ok, text, _ = await self.agent.call_tool(name, args, ORIGIN)
        return ok, text


class RemoteBackend:
    """A project served by a running exuvia: ``<url>/api/guide`` and
    ``<url>/api/tools/{name}``."""

    def __init__(self, url: str, token: str | None = None):
        import httpx

        headers = {"X-Exuvia-Origin": ORIGIN}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.api = url.rstrip("/") + "/api"
        self.http = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(REMOTE_TIMEOUT, connect=10.0))
        self._guide: dict[str, Any] | None = None

    async def start(self) -> None:
        """Load the guide. A workbench that is not running is not fatal: the
        relay starts anyway, with this package's own tool list, and every
        call says how to start the workbench until one answers. A server
        that answers wrongly (an HTTP error, an old exuvia) is fatal."""
        import httpx

        try:
            await self._load_guide()
        except httpx.HTTPError as e:
            print(f"exuvia mcp: {self.offline_message(e)}", file=sys.stderr)
        except RuntimeError:
            await self.http.aclose()
            raise

    def offline_message(self, e: Exception) -> str:
        from urllib.parse import urlsplit

        base = self.api.removesuffix("/api")
        url = urlsplit(base)
        if url.path.strip("/"):  # a hub project: there is no one command to give
            how = "Ask the user to start that exuvia server"
        else:
            port = f" --port {url.port}" if url.port else ""
            how = (
                f"Start it in the background with `exuvia serve <project folder>{port}` "
                "(or `uvx exuvia serve ...`), or ask the user to"
            )
        return (
            f"could not reach exuvia at {base} ({type(e).__name__}): the workbench this MCP server "
            f"relays to is not running. {how}, then call the tool again."
        )

    async def stop(self) -> None:
        await self.http.aclose()

    async def _load_guide(self) -> dict[str, Any]:
        r = await self.http.get(f"{self.api}/guide")
        if r.status_code != 200:
            raise RuntimeError(f"GET {self.api}/guide: HTTP {r.status_code} {r.text[:300]}")
        guide = r.json()
        if any("input_schema" not in t for t in guide.get("tools", [])):
            raise RuntimeError(f"{self.api} is an older exuvia without /api/tools: upgrade the server")
        self._guide = guide
        return guide

    async def instructions(self) -> str:
        import httpx

        try:
            return (await self._load_guide())["instructions"]
        except httpx.HTTPError as e:
            return self.offline_message(e)

    async def tools(self) -> list[dict[str, Any]]:
        import httpx

        if self._guide:
            return self._guide["tools"]
        try:
            return (await self._load_guide())["tools"]
        except httpx.HTTPError:
            from .agent import TOOLS  # the same tools, from this install

            return TOOLS

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        import httpx

        try:
            r = await self.http.post(f"{self.api}/tools/{name}", json=args)
        except httpx.HTTPError as e:
            return False, self.offline_message(e)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:2000]}"
        body = r.json()
        return bool(body.get("ok")), str(body.get("text", ""))


def build_server(backend: Backend, instructions: str) -> Any:
    """The MCP server for ``backend``: its tools plus ``guide``."""
    from importlib.metadata import version

    import mcp.types as types
    from mcp.server.lowlevel import Server

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        defs = [GUIDE_TOOL, *await backend.tools()]
        return types.ListToolsResult(
            tools=[types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"]) for t in defs]
        )

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name == GUIDE_TOOL["name"]:
            ok, text = True, await backend.instructions()
        else:
            ok, text = await backend.call(params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(text=text)], is_error=not ok)

    return Server(
        "exuvia",
        version=version("exuvia"),
        instructions=instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(backend: LocalBackend | RemoteBackend) -> None:
    from mcp.server.stdio import stdio_server

    remote = isinstance(backend, RemoteBackend)
    if remote:
        await backend.start()  # a server that answers wrongly fails here, with a plain message
    # Inside stdio_server, stray prints (the workspace's, a node's) land on
    # stderr instead of corrupting the protocol on stdout.
    async with stdio_server() as (read, write):
        if not remote:
            await backend.start()
        try:
            server = build_server(backend, await backend.instructions())
            await server.run(read, write, server.create_initialization_options())
        finally:
            await backend.stop()


def main(directory: str | None, url: str | None) -> None:
    """``exuvia mcp``: serve one project over stdio. Everything but the MCP
    wire goes to stderr."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        sys.exit("exuvia mcp needs the MCP SDK, which exuvia installs: reinstall exuvia (pip install -U exuvia)")

    if url:
        if directory:
            sys.exit("exuvia mcp: give a project directory or --url, not both")
        backend: LocalBackend | RemoteBackend = RemoteBackend(url, os.environ.get("EXUVIA_API_TOKEN"))
        where = url
    else:
        root = Path(directory or ".").resolve()
        if not (root / "nodes").is_dir():
            sys.exit(f"exuvia mcp: {root} is not an exuvia project (no nodes/): run `exuvia init {directory or '.'}` first")
        backend = LocalBackend(root)
        where = str(root)
    print(f"exuvia mcp: serving {where} over stdio", file=sys.stderr)
    try:
        asyncio.run(serve_stdio(backend))
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        sys.exit(f"exuvia mcp: {e}")
