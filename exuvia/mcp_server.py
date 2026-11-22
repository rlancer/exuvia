"""exuvia as an MCP server (``exuvia mcp``).

Gives any MCP client (Claude Code, Claude Desktop, Cursor, ...) the same tools
the built-in agent has: list, read and write nodes, helpers and views, run a
node, query outputs, define parameters, browse and restore revisions. The
agent's instructions (how exuvia works, this project's parameters and notes)
are the server's ``instructions``, and the ``guide`` tool returns them too,
for clients that do not pass instructions to their model.

Three ways to reach projects:

* ``exuvia mcp DIR``: open the project in this process. Nothing else needs to
  be running. Schedules don't fire here (``exuvia serve`` runs them).
* ``exuvia mcp --url URL``: relay to a running ``exuvia serve`` (``URL`` is
  its address) or one hub project (``URL`` is ``<hub>/p/<project>``) through
  ``POST /api/tools/{name}``. The server stays the only process that owns the
  project, so its UI shows every change as it happens. If nothing answers at
  ``URL``, the relay starts anyway and each tool call says how to start the
  workbench.
* ``exuvia mcp --url HUB``: a hub's address (its ``/healthz`` says ``"mode":
  "hub"``) relays to every project on it: ``list_projects`` and
  ``create_project``, and each project tool takes a ``project`` argument.

``EXUVIA_API_TOKEN`` is sent as a bearer token when set: the server's shared
API token, or a person's agent token (``exv_...``, made in the hub's Connect
agent), which signs the session in as them.

Changes and runs are recorded with the origin ``mcp``.

The servers themselves also speak MCP over HTTP at ``/mcp`` (mcp_http.py), so
a client that supports remote servers needs none of this; the relay is for
clients that only run commands, and for reading the token from a file.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
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

# A hub's own tools (HubBackend), besides every project tool with a `project` argument.
PROJECT_ARG = {"type": "string", "description": "The project's id, as list_projects shows it."}
HUB_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": (
            "The projects on this exuvia hub (id, name, number of nodes) and the templates a new one can "
            "start from. Every other tool works in one project, named by its `project` argument."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_project",
        "description": (
            "Create a project on this hub, empty or from a template, and return its id and workbench URL. "
            "Ask the user before creating one they did not ask for."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "1-64 letters, digits, `-` or `_`, starting with a letter or digit; it is in the project's URL."},
                "name": {"type": "string", "description": "A display name (defaults to the id)."},
                "template": {"type": "string", "description": "A template from list_projects to start from."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]
HUB_GUIDE_TOOL = {
    "name": "guide",
    "description": (
        "With `project`: how exuvia works and how to write that project's nodes, helpers, views and parameters, "
        "with the project's own parameters and notes; read it before you write or change anything there. "
        "Without: how this hub works and its projects."
    ),
    "input_schema": {"type": "object", "properties": {"project": PROJECT_ARG}, "additionalProperties": False},
}


# Project tools that are about exuvia, not a project: a hub offers them
# without a `project` argument and answers them itself.
PROJECT_FREE = {"docs"}


def hub_tool_list(project_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A hub's tools: its own, then each project tool with a required
    ``project`` (but those in PROJECT_FREE as they are)."""
    out = list(HUB_TOOLS)
    for t in project_tools:
        if t["name"] in PROJECT_FREE:
            out.append({"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]})
            continue
        schema = dict(t["input_schema"])
        schema["properties"] = {"project": PROJECT_ARG, **schema.get("properties", {})}
        schema["required"] = ["project", *schema.get("required", [])]
        out.append({"name": t["name"], "description": t["description"], "input_schema": schema})
    return out


def hub_instructions(url: str, projects: list[dict[str, Any]] | str) -> str:
    """What an agent connected to a whole hub is told; ``projects`` is the
    hub's project list, or why it is not available."""
    if isinstance(projects, str):
        listing = f"(The project list is not available: {projects}.)"
    elif not projects:
        listing = "It has no projects yet: create_project makes one."
    else:
        rows = "\n".join(f"- {p['id']}: {p['name']} ({p['nodes']} nodes)" for p in projects)
        listing = f"Its projects:\n{rows}"
    return (
        f"An exuvia hub at {url}: many exuvia projects (DAG workbenches of Python and SQL nodes), each "
        f"with its own nodes, runs, outputs and views, and its workbench at {url}/p/<project>/. "
        "Every project tool takes the project's id as `project`; `docs` (exuvia's documentation) needs none. "
        "Before you write or change anything in a project, call `guide` with that `project`: it says how "
        "exuvia works and holds the project's own "
        "parameters and notes. Use the project the user names; when it is unclear which one, ask, or call "
        f"list_projects. {listing}"
    )


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


class HubBackend:
    """Every project on a running hub: the hub's own tools (list and create
    projects) and each project tool with a ``project`` argument, relayed to
    ``<hub>/p/<project>/api/tools/{name}``."""

    guide_tool = HUB_GUIDE_TOOL

    def __init__(self, url: str, token: str | None = None):
        import httpx

        headers = {"X-Exuvia-Origin": ORIGIN}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.url = url.rstrip("/")
        self.http = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(REMOTE_TIMEOUT, connect=10.0))
        self._project_tools: list[dict[str, Any]] | None = None

    async def start(self) -> None:
        r = await self.http.get(f"{self.url}/api/projects")
        if r.status_code == 401:
            await self.http.aclose()
            raise RuntimeError(
                f"{self.url} turned the token away: set EXUVIA_API_TOKEN (or EXUVIA_API_TOKEN_FILE, a file that holds it) "
                "to the hub's API token, or to your agent token (the hub's account menu: Connect an agent)"
            )
        if r.status_code != 200:
            await self.http.aclose()
            raise RuntimeError(f"GET {self.url}/api/projects: HTTP {r.status_code} {r.text[:300]}")
        self._project_tools = await self._load_project_tools(r.json().get("projects", []))

    async def stop(self) -> None:
        await self.http.aclose()

    async def _load_project_tools(self, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The project tools as the hub defines them (its version may differ
        from this install's): /api/tools, or an older hub's project guide."""
        r = await self.http.get(f"{self.url}/api/tools")
        if r.status_code == 200:
            return r.json()["tools"]
        if projects:
            r = await self.http.get(f"{self.url}/p/{projects[0]['id']}/api/guide")
            if r.status_code == 200:
                return r.json()["tools"]
        from .agent import TOOLS

        return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in TOOLS]

    async def tools(self) -> list[dict[str, Any]]:
        return hub_tool_list(self._project_tools or [])

    async def _projects(self) -> tuple[bool, dict[str, Any] | str]:
        import httpx

        try:
            r = await self.http.get(f"{self.url}/api/projects")
        except httpx.HTTPError as e:
            return False, f"could not reach the exuvia hub at {self.url} ({type(e).__name__}): ask the user whether it is running"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:2000]}"
        return True, r.json()

    async def instructions(self) -> str:
        ok, listing = await self._projects()
        return hub_instructions(self.url, listing["projects"] if ok else str(listing))  # type: ignore[index]

    async def guide(self, args: dict[str, Any]) -> tuple[bool, str]:
        import httpx

        project = args.get("project")
        if not project:
            return True, await self.instructions()
        try:
            r = await self.http.get(f"{self.url}/p/{project}/api/guide")
        except httpx.HTTPError as e:
            return False, f"could not reach the exuvia hub at {self.url} ({type(e).__name__})"
        if r.status_code == 404:
            return False, f"no project {project} on this hub: list_projects shows them"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:2000]}"
        return True, r.json()["instructions"]

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        import json

        import httpx

        from .hub import PROJECT_ID

        try:
            if name == "list_projects":
                ok, listing = await self._projects()
                return ok, json.dumps(listing, indent=1) if ok else str(listing)
            if name in PROJECT_FREE:  # the docs of this install, which may be a different version than the hub
                from . import docs

                return docs.tool(args)
            if name == "create_project":
                r = await self.http.post(f"{self.url}/api/projects", json={k: v for k, v in args.items() if k in ("id", "name", "template")})
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}: {r.json().get('detail', r.text[:2000]) if r.headers.get('content-type', '').startswith('application/json') else r.text[:2000]}"
                made = r.json()
                return True, json.dumps({**made, "workbench": f"{self.url}/p/{made['id']}/"})
            args = dict(args)
            project = args.pop("project", None)
            if not isinstance(project, str) or not PROJECT_ID.match(project):
                return False, "INVALID_INPUT: name the project as `project` (its id; list_projects shows them)"
            r = await self.http.post(f"{self.url}/p/{project}/api/tools/{name}", json=args)
        except httpx.HTTPError as e:
            return False, f"could not reach the exuvia hub at {self.url} ({type(e).__name__}): ask the user whether it is running"
        if r.status_code == 404 and "no project" in r.text:
            return False, f"no project {project} on this hub: list_projects shows them"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:2000]}"
        body = r.json()
        return bool(body.get("ok")), str(body.get("text", ""))


async def remote_backend(url: str, token: str | None) -> RemoteBackend | HubBackend:
    """A hub's own address relays to all its projects; any other URL (an
    ``exuvia serve``, a hub project, or nothing answering yet) to one."""
    from urllib.parse import urlsplit

    import httpx

    if not urlsplit(url).path.strip("/"):
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                r = await http.get(url.rstrip("/") + "/healthz")
            if r.status_code == 200 and r.json().get("mode") == "hub":
                return HubBackend(url, token)
        except (httpx.HTTPError, ValueError):
            pass
    return RemoteBackend(url, token)


def build_server(backend: Backend, instructions: str | Callable[[], str]) -> Any:
    """The MCP server for ``backend``: its tools plus ``guide``. Given a
    function, the instructions are worked out again at each ``initialize``
    (the HTTP endpoint, exuvia/mcp_http.py, serves many clients from one
    server, and a hub's projects come and go)."""
    from importlib.metadata import version

    import mcp.types as types
    from mcp.server.lowlevel import Server

    if callable(instructions):
        live = instructions

        class LiveServer(Server):  # type: ignore[misc]
            @property
            def instructions(self) -> str:  # type: ignore[override]
                return live()

            @instructions.setter
            def instructions(self, _: Any) -> None:  # Server.__init__ sets it
                pass

        server_cls: Any = LiveServer
        instructions = ""
    else:
        server_cls = Server

    guide_tool = getattr(backend, "guide_tool", GUIDE_TOOL)

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        defs = [guide_tool, *await backend.tools()]
        return types.ListToolsResult(
            tools=[types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"]) for t in defs]
        )

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name == guide_tool["name"]:
            guide = getattr(backend, "guide", None)
            ok, text = await guide(params.arguments or {}) if guide else (True, await backend.instructions())
        else:
            ok, text = await backend.call(params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(text=text)], is_error=not ok)

    return server_cls(
        "exuvia",
        version=version("exuvia"),
        instructions=instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(backend: LocalBackend | RemoteBackend | HubBackend) -> None:
    from mcp.server.stdio import stdio_server

    remote = isinstance(backend, (RemoteBackend, HubBackend))
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
    """``exuvia mcp``: serve one project, or a hub's projects, over stdio.
    Everything but the MCP wire goes to stderr."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        sys.exit("exuvia mcp needs the MCP SDK, which exuvia installs: reinstall exuvia (pip install -U exuvia)")

    if url:
        if directory:
            sys.exit("exuvia mcp: give a project directory or --url, not both")
        backend: LocalBackend | RemoteBackend | HubBackend = asyncio.run(remote_backend(url, os.environ.get("EXUVIA_API_TOKEN")))
        where = f"the hub at {url} (every project)" if isinstance(backend, HubBackend) else url
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
