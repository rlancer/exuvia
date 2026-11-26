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
import json
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from dataclasses import dataclass
from typing import Any

ORIGIN = "mcp"
GUIDE_TOOL = {
    "name": "guide",
    "description": (
        "How exuvia works and how to write this project's nodes, helpers, views and parameters, "
        "with the project's own parameters and notes. Read it before you write or change anything."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
REMOTE_TIMEOUT = 900.0  # a run_node call waits for the run to finish (wait_seconds, at most 900)
# While a tool call is still going, a progress notification this often (when
# the client asked for progress), so a client that times out idle calls
# does not give up on a long run_node.
HEARTBEAT_S = 15.0

# A hub's own tools (HubBackend), besides every project tool with a `project` argument.
PROJECT_ARG = {
    "type": "string",
    "description": (
        "The project's id, as list_projects shows it: the project the user named, or the one this "
        "conversation already works in. If the user has not said which and the hub has more than one, "
        "ask them first; never pick one yourself."
    ),
}
# A project tool called without `project`, on a hub.
NO_PROJECT = (
    "INVALID_INPUT: name the project as `project` (its id; list_projects shows them). "
    "If the user has not said which project, ask them; don't pick one yourself."
)
HUB_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": (
            "The projects on this exuvia hub (id, name, number of nodes) and the templates a new one can "
            "start from. Every other tool works in one project, named by its `project` argument. The list "
            "is not a choice: its order means nothing, so when the user has not said which project to "
            "work in, show them the projects and ask."
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
                "group": {"type": "string", "description": "A group id (list_groups) to start it in; needs editor on the group."},
                "access": {
                    "type": "string", "enum": ["private", "view", "edit"],
                    "description": "On a hub with sharing: what everyone else signed in gets (default: the hub's).",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]

# Organizing a hub: groups and who can open what. Both hub backends answer
# these through the hub's own HTTP API (HubBackend over the network,
# mcp_http.HubLocalBackend in process), so the API's permission checks and
# answers are the tools' too.
_GROUP_ARG = {"type": "string", "description": "The group's id, as list_groups shows it."}
_WHO = {
    "user": {"type": "string", "description": "A user id (list_people finds it)."},
    "email": {"type": "string", "description": "Or an email, for someone who may not have signed in yet."},
}
_SHARE_ROLE = {
    "type": "string", "enum": ["viewer", "editor", "none"],
    "description": "viewer: can open; editor: can change and run; none: take them off.",
}
ORGANIZE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_people",
        "description": "People who sign in to this hub whose name or email contains `query`: their user ids, for sharing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Part of a name or email (empty: anyone)."},
                "limit": {"type": "integer", "description": "At most this many (default 20, at most 100)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_groups",
        "description": (
            "The groups you have a role on. A group is a folder of projects: its people get their group role "
            "(viewer or editor) on every project in it, and its projects share its environment variables."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_group",
        "description": "A group: its owner, its people and their roles, its projects, and its variables' names.",
        "input_schema": {"type": "object", "properties": {"group": _GROUP_ARG}, "required": ["group"], "additionalProperties": False},
    },
    {
        "name": "create_group",
        "description": "Create a group, owned by you, and return it. Ask the user before creating one they did not ask for.",
        "input_schema": {
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "update_group",
        "description": "Rename a group, or hand it to another owner (who must be a user; you stay on as an editor). Owner only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group": _GROUP_ARG,
                "name": {"type": "string", "description": "The new name."},
                "owner": {"type": "string", "description": "The new owner's user id. Confirm with the user first."},
            },
            "required": ["group"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_group",
        "description": (
            "Delete a group and its variables (secrets included); its projects stay, outside any group, and its "
            "people lose the access the group gave them. It can't be undone: confirm with the user first. Owner only."
        ),
        "input_schema": {"type": "object", "properties": {"group": _GROUP_ARG}, "required": ["group"], "additionalProperties": False},
    },
    {
        "name": "set_group_member",
        "description": (
            "Add someone to a group, change their role, or (role `none`) take them off; their group role applies "
            "to every project in the group. Give `user` or `email`. Owner only (anyone may take themselves off)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"group": _GROUP_ARG, **_WHO, "role": _SHARE_ROLE},
            "required": ["group", "role"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_project_group",
        "description": (
            "Put a project in a group, move it to another, or (`group`: null) take it out. Adding needs ownership "
            "of the project and editor on the group. The group's people get their role on the project, and "
            "everyone who can edit the project can read the group's secrets: say so to the user first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": PROJECT_ARG,
                "group": {"type": ["string", "null"], "description": "A group id, or null to take the project out of its group."},
            },
            "required": ["project", "group"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project_sharing",
        "description": (
            "Who can open a project: its owner, general access (what everyone else signed in gets), the people it "
            "is shared with, and its group. On a hub with sharing."
        ),
        "input_schema": {"type": "object", "properties": {"project": PROJECT_ARG}, "required": ["project"], "additionalProperties": False},
    },
    {
        "name": "set_project_member",
        "description": "Share a project with someone, change their role, or (role `none`) stop sharing with them. Give `user` or `email`. Owner only.",
        "input_schema": {
            "type": "object",
            "properties": {"project": PROJECT_ARG, **_WHO, "role": _SHARE_ROLE},
            "required": ["project", "role"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_project_access",
        "description": (
            "Change a project's general access (`private`: only people it is shared with; `view` or `edit`: "
            "everyone signed in), or hand it to another owner (you stay on as an editor; confirm with the user "
            "first). Owner only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": PROJECT_ARG,
                "access": {"type": "string", "enum": ["private", "view", "edit"]},
                "owner": {"type": "string", "description": "The new owner's user id."},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
]
HUB_TOOLS += ORGANIZE_TOOLS
# The hub tools answered through the hub's HTTP API (hub_api_call).
HUB_API_TOOLS = {"create_project", *(t["name"] for t in ORGANIZE_TOOLS)}
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
PROJECT_FREE = {"docs", "version"}


def with_local_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A server's tool list plus the PROJECT_FREE tools this install answers
    itself, for a relay to an older server that does not list them."""
    from .agent import TOOLS

    have = {t["name"] for t in tools}
    return [*tools, *(
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in TOOLS if t["name"] in PROJECT_FREE and t["name"] not in have
    )]


async def project_free(name: str, args: dict[str, Any], command: str, target: str = "<folder>") -> tuple[bool, str]:
    """A PROJECT_FREE tool, answered by this process: the docs of this
    install, or its version (``command`` and ``target``: what it runs, for
    the uvx update command)."""
    import json

    from .agent import _validate

    problem = _validate(name, args)
    if problem:
        return False, f"INVALID_INPUT: {problem}"
    if name == "docs":
        from . import docs

        return docs.tool(args)
    from . import about

    return True, json.dumps(await about.about(args.get("refresh") is True, command, target))


async def relay_version(server: tuple[bool, str], url: str, args: dict[str, Any]) -> tuple[bool, str]:
    """``version`` through a relay (``exuvia mcp --url``): the server's answer
    (``server``: its ``version`` tool's result) and this machine's exuvia,
    which runs the relay and can be another version."""
    import json

    ok, local = await project_free("version", args, "mcp", f"--url {url}")
    if not ok:
        return ok, local
    s_ok, s_text = server
    try:
        srv = json.loads(s_text) if s_ok else {"error": s_text}
    except ValueError:
        srv = {"error": s_text}
    return True, json.dumps({
        "server": {"url": url, **srv},
        "this_machine": json.loads(local),
        "note": (
            "`server` is the exuvia at that URL: its `upgrade` runs where that server runs (null: whoever runs it "
            "deploys the new image). `this_machine` is the `exuvia mcp` this client starts, the `exuvia` command "
            "on the user's machine; after updating it, the MCP client has to restart it."
        ),
    })


# A request to the hub's HTTP API: (method, path, JSON body, query) ->
# (status, the answer's JSON, or its text when it is not JSON).
HubRequest = Callable[[str, str, Any, "dict[str, Any] | None"], Awaitable[tuple[int, Any]]]


def _api_error(status: int, body: Any) -> str:
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if not isinstance(detail, str):
        detail = json.dumps(detail)
    if status == 404 and detail.startswith("Not Found"):
        detail += " (this hub's exuvia may be too old for this tool: ask whoever runs it to upgrade)"
    return f"HTTP {status}: {detail[:2000]}"


async def hub_api_call(name: str, args: dict[str, Any], request: HubRequest, base_url: str) -> tuple[bool, str]:
    """A HUB_API_TOOLS tool, as the request to the hub's HTTP API that the
    hub's own UI makes (``base_url``: the hub's address, for links)."""
    from urllib.parse import quote

    from .hub import PROJECT_ID
    from .sharing import email_principal, user_principal

    def arg(key: str, required: bool = False) -> str | None:
        v = args.get(key)
        if v is None and not required:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"`{key}` is {'required and ' if required else ''}a non-empty string")
        return v.strip()

    # Ids go in the request's path, which the server routes after decoding
    # it, so quoting alone would let a `/` through: check them instead.
    def seg(key: str) -> str:
        v = arg(key, True) or ""
        if not PROJECT_ID.match(v):
            raise ValueError(f"no {key} {v}: list_groups shows them")
        return v

    def project() -> str:
        p = arg("project", True) or ""
        if not PROJECT_ID.match(p):
            raise ValueError(f"no project {p} on this hub: list_projects shows them")
        return p

    def member(base: str) -> tuple[str, str, Any]:
        user, email, role = arg("user"), arg("email"), arg("role", True)
        if (user is None) == (email is None):
            raise ValueError("give one of `user` (a user id: list_people) or `email`")
        if "/" in (user or email or ""):
            raise ValueError(f"no user {user or email}")
        if role == "none":
            principal = user_principal(user) if user else email_principal(email or "")
            return "DELETE", f"{base}/people/{quote(principal, safe='')}", None
        return "PUT", f"{base}/people", {"user": user, "email": email, "role": role}

    try:
        query: dict[str, Any] | None = None
        body: Any = None
        if name == "create_project":
            method, path = "POST", "/api/projects"
            body = {k: arg(k, k == "id") for k in ("id", "name", "template", "group", "access") if args.get(k) is not None or k == "id"}
        elif name == "list_people":
            limit = args.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ValueError("`limit` is a whole number")
            method, path, query = "GET", "/api/people", {"q": args.get("query") or "", "limit": limit}
        elif name == "list_groups":
            method, path = "GET", "/api/groups"
        elif name == "get_group":
            method, path = "GET", f"/api/groups/{seg('group')}"
        elif name == "create_group":
            method, path, body = "POST", "/api/groups", {"name": arg("name", True)}
        elif name == "update_group":
            body = {k: arg(k) for k in ("name", "owner") if args.get(k) is not None}
            if not body:
                raise ValueError("give `name`, `owner` or both")
            method, path = "PATCH", f"/api/groups/{seg('group')}"
        elif name == "delete_group":
            method, path = "DELETE", f"/api/groups/{seg('group')}"
        elif name == "set_group_member":
            method, path, body = member(f"/api/groups/{seg('group')}")
        elif name == "set_project_group":
            if "group" not in args:
                raise ValueError("`group` is required: a group id, or null to take the project out of its group")
            method, path, body = "PUT", f"/api/projects/{project()}/group", {"group": arg("group")}
        elif name == "get_project_sharing":
            method, path = "GET", f"/api/projects/{project()}/sharing"
        elif name == "set_project_member":
            method, path, body = member(f"/api/projects/{project()}/sharing")
        elif name == "set_project_access":
            pid = project()
            body = {"general": arg("access"), "owner": arg("owner")}
            body = {k: v for k, v in body.items() if v is not None}
            if not body:
                raise ValueError("give `access`, `owner` or both")
            method, path = "PATCH", f"/api/projects/{pid}/sharing"
        else:
            raise ValueError(f"unknown tool {name}")
    except ValueError as e:
        return False, f"INVALID_INPUT: {e}"
    status, answer = await request(method, path, body, query)
    email = args.get("email")
    if status == 404 and method == "DELETE" and name in ("set_group_member", "set_project_member") and email:
        # Shared by the email of someone in the user list: stored as that user.
        s, people = await request("GET", "/api/people", None, {"q": email.strip(), "limit": 100})
        uid = next((p["id"] for p in (people.get("people", []) if s == 200 and isinstance(people, dict) else [])
                    if (p.get("email") or "").lower() == email.strip().lower()), None)
        if uid is not None:
            status, answer = await request(method, path.rsplit("/", 1)[0] + "/" + quote(user_principal(uid), safe=""), None, None)
    if not 200 <= status < 300:
        return False, _api_error(status, answer)
    if name == "create_project" and isinstance(answer, dict) and "id" in answer:
        answer = {**answer, "workbench": f"{base_url}/p/{answer['id']}/"}
    return True, json.dumps(answer, indent=1)


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
        "Every project tool takes the project's id as `project`; `docs` (exuvia's documentation, the `exuvia` "
        "command line included) and `version` (which exuvia this is and how to update it) need none. "
        "Before you write or change anything in a project, call `guide` with that `project`: it says how "
        "exuvia works and holds the project's own "
        "parameters and notes. Work in the project the user names, or the one this conversation already "
        "works in. When neither says which project and the hub has more than one, ask the user which one "
        "(list its projects for them) before you call any project tool: never pick one yourself, and "
        "never the first one listed just because it is first. With exactly one project, use it. "
        f"{listing}"
    )


def secret_request_links(text: str, workbench: str | None, terminal: str) -> str:
    """A ``request_secret`` answer with where the user sets the value: the
    workbench's Environment panel opened on it (``workbench``: the project's
    address, when known) and the command for their own terminal
    (``terminal``: ``exuvia env set``'s arguments after the name). Only the
    name travels; the value never passes through the client or the model."""
    import json

    try:
        res = json.loads(text)
    except ValueError:
        return text
    if not isinstance(res, dict) or res.get("status") != "requested":
        return text
    name = res["name"]
    res["terminal"] = f"exuvia env set {name}{(' ' + terminal) if terminal else ''}"
    where = f"in their own terminal: `{res['terminal']}` (it asks for the value without showing it)"
    if workbench:
        res["workbench"] = f"{workbench.rstrip('/')}/#env={name}"
        where = f"at {res['workbench']} (the workbench's Environment panel), or {where}"
    res["message"] = (
        f"Asked the user to set {name}. Tell them they enter the value {where}. Never ask for the value, never "
        "run that command yourself, and call list_env to see when it is set."
    )
    return json.dumps(res)


def url_elicitation(text: str) -> tuple[str, str] | None:
    """(message, url) for a ``request_secret`` answer that has a workbench
    link, to ask the user through the client (MCP URL-mode elicitation)."""
    import json

    try:
        res = json.loads(text)
    except ValueError:
        return None
    if not isinstance(res, dict) or res.get("status") != "requested" or not res.get("workbench"):
        return None
    why = f": {res['reason']}" if res.get("reason") else ""
    return f"Set the secret {res['name']} in exuvia{why}. You enter it in exuvia, not here.", res["workbench"]


@dataclass(frozen=True)
class ToolResult:
    """What a tool call answered. ``unreachable``: the exuvia it relays to
    did not answer, so ``text`` says that, not what the tool would say.
    Unpacks as ``ok, text``."""

    ok: bool
    text: str
    unreachable: bool = False

    def __iter__(self) -> Iterator[Any]:
        return iter((self.ok, self.text))


class Backend:
    """The tool-call layer the MCP server (build_server) and ``exuvia tool``
    (toolcli.py) share. A backend says what it offers (``instructions``,
    ``tools``) and answers ``call_tool``; ``guide`` is the guide tool's
    answer (by default the instructions)."""

    guide_tool: dict[str, Any] = GUIDE_TOOL

    async def instructions(self) -> str:
        raise NotImplementedError

    async def tools(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[bool, str] | ToolResult:
        raise NotImplementedError

    async def guide(self, args: dict[str, Any]) -> tuple[bool, str] | ToolResult:
        return True, await self.instructions()

    async def list_tools(self, include_guide: bool = True, *, live: bool = False) -> list[dict[str, Any]]:
        """Every tool, the guide first unless ``include_guide`` is false.
        ``live``: a relay's list as its server has it now, or
        remote.Unreachable (not this install's list when it is down)."""
        tools = await self.tools()
        return [self.guide_tool, *tools] if include_guide else tools

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Call tool ``name``, the guide included."""
        res = await (self.guide(args) if name == self.guide_tool["name"] else self.call_tool(name, args))
        return res if isinstance(res, ToolResult) else ToolResult(*res)

    def unreachable(self, e: Exception) -> str:
        """What to say when the exuvia a relay calls did not answer
        (remote.Unreachable ``e``); each relay words its own advice."""
        from .remote import unreachable

        return unreachable(getattr(e, "url", ""), e)


class LocalBackend(Backend):
    """A project opened in this process."""

    def __init__(self, root: Path):
        from .agent import Agent
        from .workspace import Workspace

        self.ws = Workspace(root)
        self.agent = Agent(self.ws, command="mcp")

    async def start(self) -> None:
        await self.ws.start()

    async def stop(self) -> None:
        await self.ws.stop()

    async def instructions(self) -> str:
        return self.agent.instructions()

    async def tools(self) -> list[dict[str, Any]]:
        from .agent import TOOLS

        return TOOLS

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        ok, text, _ = await self.agent.call_tool(name, args, ORIGIN)
        if ok and name == "request_secret":
            import shlex

            text = secret_request_links(text, None, shlex.quote(str(self.ws.project.root)))
        return ok, text


class _Relay(Backend):
    """What RemoteBackend and HubBackend share: one client for the running
    exuvia (remote.connect) and one way to ask it something."""

    def __init__(self, url: str, token: str | None):
        import httpx

        from .remote import connect

        # Plain http to another machine is the caller's choice here: `exuvia mcp --url` has always allowed it.
        self.http = connect(url, allow_http=True, origin=ORIGIN, token=token, asynchronous=True,
                            timeout=httpx.Timeout(REMOTE_TIMEOUT, connect=10.0))
        self.url = self.http.where

    async def stop(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, url: str, *, json: Any = None, also: tuple[int, ...] = ()) -> Any:
        """The response when the server answered 200 (or a status in
        ``also``); a failed ToolResult saying why when it did not answer or
        answered another status."""
        from .remote import Unreachable

        try:
            r = await self.http.request(method, url, json=json)
        except Unreachable as e:
            return ToolResult(False, self.unreachable(e), unreachable=True)
        if r.status_code != 200 and r.status_code not in also:
            return ToolResult(False, f"HTTP {r.status_code}: {r.text[:2000]}")
        return r


class RemoteBackend(_Relay):
    """A project served by a running exuvia: ``<url>/api/guide`` and
    ``<url>/api/tools/{name}``."""

    def __init__(self, url: str, token: str | None = None):
        super().__init__(url, token)
        self.api = self.url + "/api"
        self._guide: dict[str, Any] | None = None

    async def start(self) -> None:
        """Load the guide. A workbench that is not running is not fatal: the
        relay starts anyway, with this package's own tool list, and every
        call says how to start the workbench until one answers. A server
        that answers wrongly (an HTTP error, an old exuvia) is fatal."""
        from .remote import Unreachable

        try:
            await self._load_guide()
        except Unreachable as e:
            print(f"exuvia mcp: {self.offline_message(e)}", file=sys.stderr)
        except RuntimeError:
            await self.http.aclose()
            raise

    def offline_message(self, e: Exception) -> str:
        from urllib.parse import urlsplit

        from .nethttp import loopback
        from .remote import unreachable

        url = urlsplit(self.url)
        if loopback(url.hostname) and not url.path.strip("/"):  # a workbench on this machine: say how to start it
            port = f" --port {url.port}" if url.port else ""
            advice = (
                "the workbench these tools relay to is not running. Start it in the background with "
                f"`exuvia serve <project folder>{port}` (or `uvx exuvia serve ...`), or ask the user to, "
                "then call the tool again."
            )
        else:  # another machine, or a hub project: it cannot be started from here
            advice = (
                "the server at that address is unreachable. Ask the user to check that the exuvia server at that "
                "address is up and reachable, then call the tool again."
            )
        return unreachable(self.url, e, advice=advice)

    unreachable = offline_message

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
        from .remote import Unreachable

        try:
            return (await self._load_guide())["instructions"]
        except Unreachable as e:
            return self.offline_message(e)

    async def tools(self) -> list[dict[str, Any]]:
        from .remote import Unreachable

        if self._guide:
            return with_local_tools(self._guide["tools"])
        try:
            return with_local_tools((await self._load_guide())["tools"])
        except Unreachable:
            from .agent import TOOLS  # the same tools, from this install

            return TOOLS

    async def list_tools(self, include_guide: bool = True, *, live: bool = False) -> list[dict[str, Any]]:
        """As Backend.list_tools. Without ``live``, this install's list
        stands in while the server is down, so the relay can start."""
        if live:
            await self._load_guide()
        return await super().list_tools(include_guide)

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[bool, str] | ToolResult:
        if name == "version":
            return await relay_version(await self._post(name, args), self.api.removesuffix("/api"), args)
        res = await self._post(name, args)
        if not res.ok:
            return res
        ok, text = res
        if ok and name == "request_secret":
            base = self.api.removesuffix("/api")
            text = secret_request_links(text, base, f"--url {base}")
        return ok, text

    async def _post(self, name: str, args: dict[str, Any]) -> ToolResult:
        r = await self._request("POST", f"{self.api}/tools/{name}", json=args)
        if isinstance(r, ToolResult):
            return r
        body = r.json()
        return ToolResult(bool(body.get("ok")), str(body.get("text", "")))


class HubBackend(_Relay):
    """Every project on a running hub: the hub's own tools (list and create
    projects) and each project tool with a ``project`` argument, relayed to
    ``<hub>/p/<project>/api/tools/{name}``."""

    guide_tool = HUB_GUIDE_TOOL

    def __init__(self, url: str, token: str | None = None):
        super().__init__(url, token)
        self._project_tools: list[dict[str, Any]] | None = None

    async def start(self) -> None:
        from .remote import Unreachable

        try:
            r = await self.http.get(f"{self.url}/api/projects")
        except Unreachable as e:
            await self.http.aclose()
            raise RuntimeError(self.unreachable(e)) from None
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

    def unreachable(self, e: Exception) -> str:
        from .remote import unreachable

        return unreachable(self.url, e, what="the exuvia hub", advice="ask the user whether it is running.")

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
        return hub_tool_list(with_local_tools(self._project_tools or []))

    async def _projects(self) -> dict[str, Any] | ToolResult:
        """The hub's project listing, or a failed ToolResult saying why not."""
        r = await self._request("GET", f"{self.url}/api/projects")
        return r if isinstance(r, ToolResult) else r.json()

    async def _hub_about(self) -> tuple[bool, str]:
        """The hub's own version, as its help menu shows it (``GET /api/about``)."""
        r = await self._request("GET", f"{self.url}/api/about")
        return (False, r.text) if isinstance(r, ToolResult) else (True, r.text)

    async def _api(self, method: str, path: str, body: Any, query: dict[str, Any] | None) -> tuple[int, Any]:
        r = await self.http.request(method, self.url + path, json=body, params=query)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    async def instructions(self) -> str:
        listing = await self._projects()
        return hub_instructions(self.url, listing.text if isinstance(listing, ToolResult) else listing["projects"])

    async def guide(self, args: dict[str, Any]) -> tuple[bool, str] | ToolResult:
        project = args.get("project")
        if not project:
            return True, await self.instructions()
        r = await self._request("GET", f"{self.url}/p/{project}/api/guide", also=(404,))
        if isinstance(r, ToolResult):
            return r
        if r.status_code == 404:
            return False, f"no project {project} on this hub: list_projects shows them"
        return True, r.json()["instructions"]

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[bool, str] | ToolResult:
        import json

        from .hub import PROJECT_ID
        from .remote import Unreachable

        try:
            if name == "list_projects":
                listing = await self._projects()
                return listing if isinstance(listing, ToolResult) else (True, json.dumps(listing, indent=1))
            if name == "version":  # the hub's and this machine's
                return await relay_version(await self._hub_about(), self.url, args)
            if name in PROJECT_FREE:  # the docs of this install, which may be a different version than the hub
                return await project_free(name, args, "mcp", f"--url {self.url}")
            if name in HUB_API_TOOLS:
                return await hub_api_call(name, args, self._api, self.url)
            args = dict(args)
            project = args.pop("project", None)
            if not isinstance(project, str) or not PROJECT_ID.match(project):
                return False, NO_PROJECT
            r = await self._request("POST", f"{self.url}/p/{project}/api/tools/{name}", json=args, also=(404,))
        except Unreachable as e:  # from hub_api_call
            return ToolResult(False, self.unreachable(e), unreachable=True)
        if isinstance(r, ToolResult):
            return r
        if r.status_code == 404:
            if "no project" in r.text:
                return False, f"no project {project} on this hub: list_projects shows them"
            return False, f"HTTP {r.status_code}: {r.text[:2000]}"
        body = r.json()
        ok, text = bool(body.get("ok")), str(body.get("text", ""))
        if ok and name == "request_secret":
            base = f"{self.url}/p/{project}"
            text = secret_request_links(text, base, f"--url {base}")
        return ok, text


async def remote_backend(url: str, token: str | None) -> RemoteBackend | HubBackend:
    """A hub's own address relays to all its projects; any other URL (an
    ``exuvia serve``, a hub project, or nothing answering yet) to one."""
    from urllib.parse import urlsplit

    from .remote import Unreachable, connect

    if not urlsplit(url).path.strip("/"):
        try:
            async with connect(url, allow_http=True, origin=ORIGIN, token=None, timeout=10.0, asynchronous=True) as http:
                r = await http.get("/healthz")
            if r.status_code == 200 and r.json().get("mode") == "hub":
                return HubBackend(url, token)
        except (Unreachable, ValueError):
            pass
    return RemoteBackend(url, token)


ELICIT_KEY = "open_workbench"
USER_ANSWER = {"accept": "opened the link to set it", "decline": "declined to open the link"}


def _with_answer(text: str, action: str) -> str:
    import json

    res = json.loads(text)
    res["user"] = USER_ANSWER.get(action, "dismissed the prompt")
    return json.dumps(res)


async def _elicit_secret(ctx: Any, params: Any, text: str) -> Any:
    """Ask the user, through a client that supports it, to open the
    workbench where they set a requested secret (MCP URL-mode elicitation:
    the value is typed there, never in the client). Returns the tool's
    text, with the user's answer when there is one, or (2026-07-28
    protocol, which has no server-to-client requests) an
    ``InputRequiredResult`` the client answers by calling the tool again.
    A client without URL elicitation gets the text as it was."""
    import uuid

    import mcp_types as types

    answered = (params.input_responses or {}).get(ELICIT_KEY) if params.input_responses else None
    if answered is not None:  # the retry, with the user's answer
        return _with_answer(text, getattr(answered, "action", ""))
    ask = url_elicitation(text)
    session = getattr(ctx, "session", None)
    caps = getattr(session, "client_capabilities", None)
    if ask is None or caps is None or caps.elicitation is None or caps.elicitation.url is None:
        return text
    message, url = ask
    if not session.can_send_request:
        return types.InputRequiredResult(
            input_requests={ELICIT_KEY: types.ElicitRequest(params=types.ElicitRequestURLParams(message=message, url=url))},
            request_state="request_secret",
        )
    try:
        answer = await session.elicit_url(
            message=message, url=url, elicitation_id=str(uuid.uuid4()), related_request_id=ctx.request_id
        )
    except Exception as e:  # noqa: BLE001 — a client that fails to ask still gets the link
        print(f"exuvia mcp: could not ask the client to open {url}: {e}", file=sys.stderr)
        return text
    return _with_answer(text, answer.action)


async def _with_heartbeat(ctx: Any, call: Any) -> Any:
    """Await ``call``, reporting progress every HEARTBEAT_S seconds while it
    is still going. Reporting is a no-op when the client sent no progress
    token, and a failure to report never fails the call."""
    task = asyncio.ensure_future(call)
    beats = 0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_S)
            if done:
                return task.result()
            beats += 1
            try:
                await ctx.session.report_progress(beats, None, f"still working ({beats * HEARTBEAT_S:g}s)")
            except Exception:  # noqa: BLE001 — a heartbeat is best effort
                pass
    finally:
        if not task.done():
            task.cancel()


def build_server(backend: Backend, instructions: str | Callable[[], str], *, elicit: bool = True) -> Any:
    """The MCP server for ``backend``: its tools plus ``guide``. Given a
    function, the instructions are worked out again at each ``initialize``
    (the HTTP endpoint, exuvia/mcp_http.py, serves many clients from one
    server, and a hub's projects come and go). ``elicit``: ask the client
    to open the workbench for a requested secret (``request_secret``), which
    needs a connection that can send the client a request (stdio)."""
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

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        defs = await backend.list_tools()
        return types.ListToolsResult(
            tools=[types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"]) for t in defs]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> Any:
        if params.name == backend.guide_tool["name"]:
            ok, text = await backend.call(params.name, params.arguments or {})
        else:
            ok, text = await _with_heartbeat(ctx, backend.call(params.name, params.arguments or {}))
            if ok and params.name == "request_secret" and elicit:
                asked = await _elicit_secret(ctx, params, text)
                if not isinstance(asked, str):
                    return asked  # the client asks the user, then calls again
                text = asked
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
        from . import remote

        try:
            backend: LocalBackend | RemoteBackend | HubBackend = asyncio.run(remote_backend(url, remote.token()))
        except ValueError as e:  # not an http(s) address, or EXUVIA_API_TOKEN_FILE unreadable
            sys.exit(f"exuvia mcp: {e}")
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
