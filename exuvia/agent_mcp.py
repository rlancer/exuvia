"""Outside MCP servers for the built-in agent, one list per person.

Each person keeps their own list of remote MCP servers (Streamable HTTP): a
name, a URL and the headers that sign them in, such as ``Authorization:
Bearer ...`` or Portkey's ``x-portkey-api-key``. When they talk to the agent,
every enabled server on *their* list is connected for that turn and its tools
are offered to the model next to exuvia's own, named ``<server>__<tool>``.
Someone else's turn in the same project never sees them.

Whose list a turn uses: on a server where people sign in (users.py), the
signed-in person's (an agent token counts as its owner); a request that
signed nobody in has none. On a server nobody signs in to (``EXUVIA_AUTH=
token``, the default for ``exuvia serve``) there is one list, the server's,
under the owner ``""``. A share link alone never has one.

Header values are secrets: the API never sends them back, only their names
and last four characters. They are kept in the server's database as they are
(they must be sent as they are), beside the user list: the hub's
``.exuvia/hub.db``, the project's database for ``exuvia serve``, or the shared
``EXUVIA_DB``. A hub export leaves them out.

OAuth: a server that signs people in with OAuth (MCP's authorization spec)
gets a **Sign in** instead of a header. ``sign_in`` starts the flow with the
MCP SDK's OAuth client: it discovers the server's authorization server,
registers exuvia there (dynamic client registration) and hands back the page
to open in a popup. The authorization server sends the browser back to
``/api/me/mcp/oauth/callback`` on this exuvia server, and ``finish_sign_in``
gives the code to the waiting flow, which trades it for tokens and keeps them
on the server's row (``oauth``), as secret as the headers. Each turn then
sends the access token and refreshes it when it runs out; when that no longer
works the server is left out and the agent says to sign in again. A sign-in
in progress lives in this process (``_SIGN_INS``), so the callback must reach
the replica that started it.

Where a server may point: anywhere public. Loopback, private and link-local
addresses (the machine itself, the cluster's network, a cloud's metadata
service) are refused on a server that needs a sign-in or a token, unless
``EXUVIA_AGENT_MCP_PRIVATE=1``. A local server with no auth allows them, so
an MCP server on your own machine works. The same holds for every request a
connection makes, the OAuth discovery, registration and token requests too.
``EXUVIA_AGENT_MCP=off`` turns the feature off.

Each connection lives in its own task for the length of one agent turn (the
MCP client's task groups must open and close in the task that owns them, and
the agent's turn is an async generator), and the turn talks to it through a
queue.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import threading
import time
import weakref
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import auth
from .db import Database, database_url, scope_prefix, shared_database
from .store import SCHEMA_LOCK_KEY

NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")  # the tools' prefix: keep it short
HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
MAX_HEADER_VALUE = 8192
MAX_URL = 2048
MAX_SERVERS = 20
MAX_HEADERS = 20
# Headers the MCP transport sets itself: a value here would break the protocol.
RESERVED_HEADERS = frozenset({
    "host", "content-length", "content-type", "accept", "connection", "transfer-encoding", "upgrade",
    "mcp-session-id", "mcp-protocol-version", "last-event-id",
})
CONNECT_TIMEOUT = 20.0  # connect, handshake and list the tools
CALL_TIMEOUT = 300.0  # one tool call
MAX_TOOLS = 100  # per server, so one server can't crowd out exuvia's own tools
MAX_RESULT = 100_000  # characters of one tool result handed to the model
TOOL_NAME_MAX = 64  # OpenAI's limit, the tighter of the two providers'
SEP = "__"

TABLE = """create table if not exists agent_mcp_servers (
    scope text not null default '',
    owner text not null,         -- a user id (users.py); '' on a server nobody signs in to
    name text not null,          -- the tools' prefix: <name>__<tool>
    url text not null,
    headers text not null,       -- a JSON object; the values are secrets and never leave the server
    oauth text,                  -- an OAuth sign-in (JSON: the registered client and the tokens), secret too
    enabled integer not null default 1,
    created_at double precision not null,
    updated_at double precision not null,
    primary key (scope, owner, name)
)"""

_prepared: "weakref.WeakSet[Any]" = weakref.WeakSet()


def enabled() -> bool:
    """``EXUVIA_AGENT_MCP``: on unless ``off``."""
    return os.environ.get("EXUVIA_AGENT_MCP", "on").strip().lower() not in ("off", "0", "false", "no")


def _prepare(db: Database) -> None:
    """Create the table once per database, as users.py does."""
    if db in _prepared:
        return
    if not db.columns("agent_mcp_servers"):
        with db.transaction():
            if db.dialect == "postgres":
                db.query(f"select pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
            db.query(TABLE)
    elif "oauth" not in db.columns("agent_mcp_servers"):
        with db.transaction():
            if db.dialect == "postgres":
                db.query(f"select pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
            if "oauth" not in db.columns("agent_mcp_servers"):
                db.query("alter table agent_mcp_servers add column oauth text")
    _prepared.add(db)


def _hint(value: str) -> str:
    """The last four characters of a long value, to tell keys apart."""
    return value[-4:] if len(value) >= 12 else ""


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("a name is 1-24 characters: a lowercase letter, then lowercase letters, digits or _")
    return name


def check_url(url: str) -> str:
    url = (url or "").strip() if isinstance(url, str) else ""
    parts = urlsplit(url)
    if len(url) > MAX_URL or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the URL must be an http:// or https:// address, such as https://mcp.example.com/mcp")
    if parts.username or parts.password:
        raise ValueError("put credentials in a header, not in the URL")
    return url


def check_headers(headers: dict[str, Any]) -> dict[str, str]:
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object of name: value")
    out: dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not HEADER_NAME.match(k):
            raise ValueError(f"not a header name: {k!r}")
        if k.lower() in RESERVED_HEADERS:
            raise ValueError(f"the MCP connection sets {k} itself")
        if not isinstance(v, str) or not v or len(v) > MAX_HEADER_VALUE or any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError(f"the value of {k} must be non-empty text on one line")
        out[k] = v
    if len(out) > MAX_HEADERS:
        raise ValueError(f"at most {MAX_HEADERS} headers")
    return out


def _private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def check_reachable(url: str, allow_private: bool) -> None:
    """Refuse a URL whose host is (or resolves to) a loopback, private or
    link-local address, unless they are allowed. A name that does not resolve
    here passes (an HTTP proxy may resolve it; the connection says if not)."""
    if allow_private:
        return
    host = urlsplit(url).hostname or ""
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        raise ValueError(f"{host} is this machine: not allowed here (EXUVIA_AGENT_MCP_PRIVATE=1 allows it)")
    if _private(host):
        raise ValueError(f"{host} is a private address: not allowed here (EXUVIA_AGENT_MCP_PRIVATE=1 allows it)")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return
    for info in infos:
        if _private(str(info[4][0])):
            raise ValueError(f"{host} resolves to a private address: not allowed here (EXUVIA_AGENT_MCP_PRIVATE=1 allows it)")


@dataclass(frozen=True)
class Server:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    # An OAuth sign-in (see the module doc) and how to save its refreshed tokens.
    oauth: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    save_oauth: Callable[[dict[str, Any] | None], None] | None = field(default=None, compare=False, repr=False)


def _signed_in(oauth: dict[str, Any] | None) -> bool:
    return bool(oauth and oauth.get("tokens"))


class McpServers:
    """The people's MCP server lists. ``people``: whether this server signs
    people in (each has a list) or not (one list, the server's).
    ``allow_private``: whether servers may point at private addresses."""

    def __init__(self, db: Database, scope: str = "", *, people: bool, allow_private: bool, owns_db: bool = False):
        self.db = db
        self.scope = scope
        self.people = people
        self.allow_private = allow_private
        self._owns_db = owns_db
        self._lock = threading.Lock()
        _prepare(db)

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.db.query(sql, args)

    # -- whose list ----------------------------------------------------------
    def owner(self, ident: auth.Identity | None = None, *, current: bool = True) -> str | None:
        """Whose list this request uses (None: nobody's). See the module doc."""
        if current:
            ident = auth.current()
        if ident is not None and ident.method == "link":
            return None
        if self.people:
            return ident.uid if ident is not None and ident.uid else None
        return ""

    # -- the list ------------------------------------------------------------
    def list(self, owner: str) -> list[dict[str, Any]]:
        """One person's servers, by name: the header values replaced by hints."""
        rows = self._q(
            "select name, url, headers, oauth, enabled, created_at, updated_at from agent_mcp_servers "
            "where scope=? and owner=? order by name",
            (self.scope, owner),
        )
        out = []
        for r in rows:
            headers = json.loads(r["headers"])
            out.append({
                "name": r["name"], "url": r["url"], "enabled": bool(r["enabled"]),
                "headers": [{"name": k, "hint": _hint(v)} for k, v in headers.items()],
                "signed_in": _signed_in(json.loads(r["oauth"]) if r["oauth"] else None),
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            })
        return out

    def _server(self, owner: str, r: dict[str, Any]) -> Server:
        name, url = r["name"], r["url"]
        # Tokens a turn refreshes replace the sign-in it started from, and
        # only that one: not a sign-out or a new sign-in made meanwhile.
        was = [r["oauth"]]

        def save(data: dict[str, Any] | None) -> None:
            was[0] = self.set_oauth(owner, name, data, url=url, was=was[0])

        return Server(
            name, url, json.loads(r["headers"]), oauth=json.loads(r["oauth"]) if r["oauth"] else None,
            save_oauth=save,
        )

    def active(self, owner: str) -> list[Server]:
        """One person's enabled servers, with their headers, for a turn."""
        rows = self._q(
            "select name, url, headers, oauth from agent_mcp_servers where scope=? and owner=? and enabled=1 order by name",
            (self.scope, owner),
        )
        return [self._server(owner, r) for r in rows]

    def set_oauth(
        self, owner: str, name: str, data: dict[str, Any] | None, *, url: str | None = None, was: Any = ...,
    ) -> str | None:
        """Keep a server's OAuth sign-in (None: sign out). ``url``: only if
        the server still has the URL the tokens were made for. ``was``: only
        if its sign-in is still this stored value. Returns the value written."""
        value = json.dumps(data) if data else None
        sql = "update agent_mcp_servers set oauth=?, updated_at=? where scope=? and owner=? and name=?"
        args: list[Any] = [value, time.time(), self.scope, owner, name]
        if url is not None:
            sql += " and url=?"
            args.append(url)
        if was is None:
            sql += " and oauth is null"
        elif was is not ...:
            sql += " and oauth=?"
            args.append(was)
        self._q(sql, tuple(args))
        return value

    def put(
        self, owner: str, name: str, *, url: str | None = None, headers: dict[str, Any] | None = None,
        enabled: bool | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        """Add a server or change one. ``headers`` is merged into the ones it
        has: a value sets a header, ``None`` removes it, and headers left out
        stay as they are (so a key need not be typed again). ValueError: a bad
        name, URL or header, or too many servers."""
        now = time.time() if now is None else now
        check_name(name)
        changes = headers or {}
        if not isinstance(changes, dict):
            raise ValueError("headers must be an object of name: value")
        with self._lock, self.db.transaction():
            rows = self._q(
                "select url, headers, enabled from agent_mcp_servers where scope=? and owner=? and name=?",
                (self.scope, owner, name),
            )
            if not rows and url is None:
                raise ValueError("a new server needs a url")
            if not rows:
                count = self._q("select count(*) as n from agent_mcp_servers where scope=? and owner=?", (self.scope, owner))
                if count[0]["n"] >= MAX_SERVERS:
                    raise ValueError(f"at most {MAX_SERVERS} MCP servers each")
            merged: dict[str, Any] = json.loads(rows[0]["headers"]) if rows else {}
            for k, v in changes.items():
                # Header names are case-insensitive: a new spelling replaces the old one.
                for old in [o for o in merged if isinstance(k, str) and o.lower() == k.lower()]:
                    del merged[old]
                if v is not None:
                    merged[k] = v
            merged = check_headers(merged)
            new_url = check_url(url) if url is not None else rows[0]["url"]
            on = (1 if enabled else 0) if enabled is not None else (rows[0]["enabled"] if rows else 1)
            if rows:
                self._q(
                    "update agent_mcp_servers set url=?, headers=?, enabled=?, updated_at=? where scope=? and owner=? and name=?",
                    (new_url, json.dumps(merged), on, now, self.scope, owner, name),
                )
                if new_url != rows[0]["url"]:  # an OAuth sign-in is for the server it was made with
                    self._q(
                        "update agent_mcp_servers set oauth=null where scope=? and owner=? and name=?",
                        (self.scope, owner, name),
                    )
            else:
                self._q(
                    "insert into agent_mcp_servers (scope, owner, name, url, headers, enabled, created_at, updated_at) "
                    "values (?,?,?,?,?,?,?,?)",
                    (self.scope, owner, name, new_url, json.dumps(merged), on, now, now),
                )
        return next(s for s in self.list(owner) if s["name"] == name)

    def get(self, owner: str, name: str) -> Server | None:
        rows = self._q(
            "select name, url, headers, oauth from agent_mcp_servers where scope=? and owner=? and name=?",
            (self.scope, owner, name),
        )
        return self._server(owner, rows[0]) if rows else None

    def delete(self, owner: str, name: str) -> bool:
        with self.db.transaction():
            found = self._q(
                "select 1 from agent_mcp_servers where scope=? and owner=? and name=?", (self.scope, owner, name)
            )
            if found:
                self._q("delete from agent_mcp_servers where scope=? and owner=? and name=?", (self.scope, owner, name))
        return bool(found)


def open_mcp_servers(local: Any, *, people: bool, allow_private: bool) -> McpServers:
    """The MCP server lists, where the user list is kept (users.py): the
    shared ``EXUVIA_DB`` when one is set, else ``local`` (a SQLite file for a
    hub, the project's database for serve)."""
    from pathlib import Path

    url = database_url()
    if url:
        return McpServers(shared_database(url), scope_prefix(), people=people, allow_private=allow_private)
    if isinstance(local, Path):
        from .db import SQLiteDatabase

        local.parent.mkdir(parents=True, exist_ok=True)
        return McpServers(SQLiteDatabase(local), people=people, allow_private=allow_private, owns_db=True)
    return McpServers(local, people=people, allow_private=allow_private)


def private_allowed(auth_method: str) -> bool:
    """Whether servers may point at private addresses: on a local server with
    no auth, or with ``EXUVIA_AGENT_MCP_PRIVATE=1``."""
    flag = os.environ.get("EXUVIA_AGENT_MCP_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")
    return flag or auth_method == "none"


# -- one turn's connections ------------------------------------------------------
@dataclass
class _Http:
    """The last HTTP error a connection saw: the MCP client turns a failed
    response into a generic error and drops its status."""

    status: int | None = None
    oauth: bool = False  # a 401 that offers an OAuth sign-in (MCP's authorization spec)

    async def hook(self, response: Any) -> None:
        if response.status_code >= 400:
            self.status = response.status_code
            self.oauth = "resource_metadata" in response.headers.get("www-authenticate", "")


class SignInNeeded(Exception):
    """An OAuth server wants the person to sign in (again): a turn can't."""


def _innermost(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def _find(e: BaseException, match: Callable[[BaseException], bool]) -> BaseException | None:
    """The first exception in a failure that matches: in its groups and what
    it was raised from (the MCP client wraps what the OAuth flow raises)."""
    seen: set[int] = set()
    stack = [e]
    while stack:
        x = stack.pop()
        if id(x) in seen:
            continue
        seen.add(id(x))
        if match(x):
            return x
        if isinstance(x, BaseExceptionGroup):
            stack.extend(x.exceptions)
        stack.extend(c for c in (x.__cause__, x.__context__) if c is not None)
    return None


def _sign_in_needed(e: BaseException) -> bool:
    return _find(e, lambda x: isinstance(x, SignInNeeded)) is not None


def wants_sign_in(e: BaseException, http: _Http | None = None) -> bool:
    """Whether the server asked for an OAuth sign-in: the button to offer."""
    return _sign_in_needed(e) or (http is not None and http.status == 401 and http.oauth)


def describe_error(e: BaseException, http: _Http | None = None) -> str:
    """A connection failure in a sentence: the innermost useful exception of
    a task-group's exception group, and what an HTTP status means."""
    if _sign_in_needed(e):
        return "your OAuth sign-in to this server has run out: sign in again in the MCP servers menu"
    flow = _find(e, lambda x: "OAuth" in type(x).__name__ and bool(str(x).strip()))
    if flow is not None:  # the SDK's OAuth flow, or the callback's own sentence
        return f"the OAuth sign-in failed: {str(flow).strip()}"
    e = _innermost(e)
    status = getattr(getattr(e, "response", None), "status_code", None) or (http.status if http else None)
    if status == 401 and http is not None and http.oauth:
        return "the server wants you to sign in (HTTP 401, OAuth): use Sign in next to it in the MCP servers menu"
    if status in (401, 403):
        return f"the server refused the credentials (HTTP {status}): check the headers"
    if status == 404:
        return "nothing answers at that URL (HTTP 404): check the path, often /mcp"
    if status:
        return f"HTTP {status} from the server"
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return "the server did not answer in time"
    name = type(e).__name__
    text = str(e).strip()
    if isinstance(e, ValueError) and text:  # check_reachable's own sentence
        return text
    if "ConnectError" in name or "ConnectTimeout" in name:
        return f"could not connect{': ' + text if text else ''}"
    return f"{name}: {text}" if text else name


def _tool_name(server: str, tool: str, taken: set[str]) -> str:
    """``<server>__<tool>``, cleaned to the characters both providers allow,
    cut to their length limit, and made unique."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool) or "tool"
    base = f"{server}{SEP}{clean}"[:TOOL_NAME_MAX]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[: TOOL_NAME_MAX - len(suffix)] + suffix, n + 1
    taken.add(name)
    return name


def _schema(raw: Any) -> dict[str, Any]:
    """A tool's input schema as the providers take it: an object schema."""
    schema = dict(raw) if isinstance(raw, dict) else {}
    schema.pop("$schema", None)
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    schema.setdefault("properties", {})
    return schema


def result_text(result: Any) -> tuple[bool, str]:
    """A CallToolResult as (ok, text for the model)."""
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        kind = getattr(c, "type", None)
        if kind == "text":
            parts.append(c.text)
        elif kind == "resource":
            res = c.resource
            parts.append(getattr(res, "text", None) or f"[resource {res.uri} ({res.mime_type or 'binary'}) not shown]")
        elif kind == "resource_link":
            parts.append(f"[resource {c.uri}" + (f": {c.description}" if c.description else "") + "]")
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({c.mime_type}) not shown: the agent reads text only]")
    structured = getattr(result, "structured_content", None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured, default=str))
    text = "\n".join(parts) or "(the tool returned nothing)"
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + f"\n\n[cut at {MAX_RESULT} characters of {len(text)}]"
    return not bool(getattr(result, "is_error", False)), text


def _guard(allow_private: bool) -> Callable[[Any], Any]:
    """An httpx request hook that applies check_reachable to every request a
    connection makes: an OAuth server names its own endpoints, so they are
    checked as the MCP server's URL is. Each host once per connection."""
    checked: set[str] = set()

    async def hook(request: Any) -> None:
        host = request.url.host
        if host not in checked:
            await check_reachable(str(request.url), allow_private)
            checked.add(host)

    return hook


def _client(server: Server, seen: _Http, *, allow_private: bool = True, auth: Any = None) -> Any:
    """The MCP client for a remote server, sending its headers, and signing
    in with ``auth`` (an OAuth provider) or, when the server has an OAuth
    sign-in, with its tokens."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp_types import Implementation

    from . import __version__

    if auth is None and _signed_in(server.oauth):
        auth = _oauth(server, _SavedSignIn(server.oauth, server.save_oauth), _needs_sign_in, _needs_sign_in)
    http = create_mcp_http_client(headers=server.headers, timeout=httpx2.Timeout(30.0, read=CALL_TIMEOUT), auth=auth)
    http.event_hooks["request"].append(_guard(allow_private))
    http.event_hooks["response"].append(seen.hook)
    return http, Client(
        streamable_http_client(server.url, http_client=http),
        client_info=Implementation(name="exuvia-agent", version=__version__),
        read_timeout_seconds=CALL_TIMEOUT,
    )


Connector = Callable[[Server], Any]


@dataclass
class _Link:
    server: Server
    queue: "asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future[tuple[bool, str]]] | None]"
    task: "asyncio.Task[None] | None" = None
    error: str | None = None
    signin: bool = False  # the error is an OAuth sign-in the person has to make
    tools: list[Any] = field(default_factory=list)


class Toolbox:
    """One agent turn's outside tools. ``start`` connects every server and
    lists its tools; ``call`` runs one; ``close`` disconnects. A server that
    fails to connect is left out, with the reason in ``status``."""

    def __init__(self, servers: list[Server], *, allow_private: bool, connect: Connector | None = None):
        self.servers = servers
        self.allow_private = allow_private
        self._connect = connect
        self._links: dict[str, _Link] = {}
        self._tools: dict[str, tuple[str, str]] = {}  # exposed name -> (server, tool)
        self.tools: list[dict[str, Any]] = []  # in the providers' format

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def server_of(self, name: str) -> str:
        return self._tools[name][0]

    async def start(self, taken: set[str]) -> None:
        """Connect to every server at once and list their tools. ``taken``:
        names already in use (exuvia's own tools)."""
        loop = asyncio.get_running_loop()
        readies: list[tuple[_Link, asyncio.Future[list[Any]]]] = []
        for s in self.servers:
            link = _Link(s, asyncio.Queue())
            ready: asyncio.Future[list[Any]] = loop.create_future()
            link.task = asyncio.create_task(self._serve(link, ready))
            self._links[s.name] = link
            readies.append((link, ready))
        deadline = loop.time() + CONNECT_TIMEOUT
        for link, ready in readies:
            try:
                link.tools = await asyncio.wait_for(asyncio.shield(ready), max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                link.error = "the server did not answer in time"
                assert link.task is not None
                link.task.cancel()
            except Exception as e:  # noqa: BLE001 - any failure leaves this server out
                link.error = link.error or describe_error(e)
        names = set(taken)
        for link in self._links.values():
            for t in link.tools[:MAX_TOOLS]:
                exposed = _tool_name(link.server.name, t.name, names)
                self._tools[exposed] = (link.server.name, t.name)
                about = (t.description or t.title or "").strip()
                self.tools.append({
                    "name": exposed,
                    "description": f"[MCP server {link.server.name}] {about}"[:4000],
                    "input_schema": _schema(t.input_schema),
                })

    async def _serve(self, link: _Link, ready: "asyncio.Future[list[Any]]") -> None:
        """Hold one server's connection for the turn, running its calls in
        order. Runs in its own task (see the module doc)."""
        pending: asyncio.Future[tuple[bool, str]] | None = None
        seen = _Http()
        try:
            await check_reachable(link.server.url, self.allow_private)
            async with AsyncExitStack() as stack:
                if self._connect is not None:
                    client = self._connect(link.server)
                else:
                    http, client = _client(link.server, seen, allow_private=self.allow_private)
                    await stack.enter_async_context(http)
                await stack.enter_async_context(client)
                tools: list[Any] = []
                cursor = None
                while len(tools) < MAX_TOOLS:
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                if not ready.done():
                    ready.set_result(tools)
                while True:
                    item = await link.queue.get()
                    if item is None:
                        break
                    tool, args, pending = item
                    seen.status = None
                    try:
                        res = await client.call_tool(tool, args)
                        out = result_text(res)
                    except Exception as e:  # noqa: BLE001 - a failed call is the model's to read
                        out = (False, f"the MCP server {link.server.name} failed: {describe_error(e, seen)}")
                    if not pending.done():
                        pending.set_result(out)
                    pending = None
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - reported, never raised into the turn
            why = describe_error(e, seen)
            if link.error is None:
                link.error, link.signin = why, wants_sign_in(e, seen)
            if not ready.done():
                ready.set_exception(RuntimeError(why))
            if pending is not None and not pending.done():
                pending.set_result((False, f"the MCP server {link.server.name} disconnected: {why}"))
        finally:
            # Calls still queued get an answer, so no caller waits forever.
            while not link.queue.empty():
                item = link.queue.get_nowait()
                if item is not None and not item[2].done():
                    item[2].set_result((False, f"the MCP server {link.server.name} is not connected"))

    async def call(self, exposed: str, args: Any) -> tuple[bool, str]:
        server, tool = self._tools[exposed]
        link = self._links[server]
        if not isinstance(args, dict):
            return False, "INVALID_INPUT: tool input must be an object"
        if link.task is None or link.task.done():
            return False, f"the MCP server {server} is not connected" + (f": {link.error}" if link.error else "")
        fut: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        await link.queue.put((tool, args, fut))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), CALL_TIMEOUT + 10)
        except asyncio.TimeoutError:
            return False, f"the MCP server {server} did not answer in {int(CALL_TIMEOUT)}s"

    def status(self) -> list[dict[str, Any]]:
        """Each server: its name, how many tools it gave, or why it is missing."""
        out = []
        for name, link in self._links.items():
            entry: dict[str, Any] = {"name": name, "tools": sum(1 for s, _ in self._tools.values() if s == name)}
            if link.error and not entry["tools"]:
                entry["error"] = link.error
                if link.signin:
                    entry["signin"] = True
            out.append(entry)
        return out

    def instructions(self) -> str:
        """What the system prompt says about this turn's outside tools."""
        if not self._links:
            return ""
        lines = []
        for s in self.status():
            if "error" in s:
                lines.append(f"- `{s['name']}`: not available this turn ({s['error']})")
            else:
                lines.append(f"- `{s['name']}`: {s['tools']} tool(s), named `{s['name']}{SEP}<tool>`")
        return (
            "\n\n## The user's MCP servers\n"
            "The user connected these outside MCP servers. Their tools act outside exuvia, as the user, so use them "
            "when the task needs them and say what you did with them. If a server the task needs is not available, "
            "tell the user why (the reason is below) instead of working around it; they manage the list in the "
            "chat's MCP servers menu.\n" + "\n".join(lines)
        )

    async def close(self) -> None:
        for link in self._links.values():
            link.queue.put_nowait(None)
        tasks = [link.task for link in self._links.values() if link.task is not None]
        if not tasks:
            return
        _, still = await asyncio.wait(tasks, timeout=5)
        for t in still:
            t.cancel()
        if still:
            await asyncio.wait(still, timeout=5)


async def probe(server: Server, *, allow_private: bool, connect: Connector | None = None) -> dict[str, Any]:
    """Connect once and list the tools, for the "Test" button: ``{ok, tools}``
    or ``{ok: false, error}``, with ``signin: true`` when the server wants an
    OAuth sign-in."""
    box = Toolbox([server], allow_private=allow_private, connect=connect)
    try:
        await box.start(set())
        st = box.status()[0]
        if "error" in st:
            return {"ok": False, "error": st["error"], **({"signin": True} if st.get("signin") else {})}
        tools = box._links[server.name].tools[:MAX_TOOLS]
        return {"ok": True, "tools": [{"name": t.name, "description": (t.description or t.title or "").strip()} for t in tools]}
    finally:
        await box.close()


# -- OAuth sign-in ---------------------------------------------------------------
CALLBACK_PATH = "/api/me/mcp/oauth/callback"
SIGN_IN_TIMEOUT = 600.0  # from "Sign in" to the person coming back
TOKEN_MARGIN = 60.0  # refresh an access token this long before it runs out


class _SavedSignIn:
    """The SDK's TokenStorage over a server's ``oauth`` value: the client
    exuvia registered (``client``), the tokens and when the access token runs
    out (``expires_at``). Saved when new tokens arrive, never before, so a
    sign-in that is abandoned leaves the one before it."""

    def __init__(self, data: dict[str, Any] | None, save: Callable[[dict[str, Any] | None], None] | None):
        self.data: dict[str, Any] = dict(data or {})
        self._save = save
        self.context: Any = None  # the provider's OAuthContext, whose discovery is kept with the tokens

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        tokens = self.data.get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens: Any) -> None:
        self.data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self.data["expires_at"] = time.time() + tokens.expires_in - TOKEN_MARGIN if tokens.expires_in else None
        # Where the authorization server is (its token endpoint above all):
        # a later turn's provider refreshes there without discovering it again.
        ctx = self.context
        if ctx is not None and ctx.oauth_metadata is not None:
            self.data["discovery"] = {
                "oauth_metadata": ctx.oauth_metadata.model_dump(mode="json", exclude_none=True),
                "resource_metadata": ctx.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
                if ctx.protected_resource_metadata is not None else None,
                "auth_server_url": ctx.auth_server_url,
            }
        if self._save is not None:
            await asyncio.to_thread(self._save, dict(self.data))

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        client = self.data.get("client")
        return OAuthClientInformationFull.model_validate(client) if client else None

    async def set_client_info(self, client_info: Any) -> None:
        self.data["client"] = client_info.model_dump(mode="json", exclude_none=True)


async def _needs_sign_in(*_: Any) -> Any:
    """A turn's redirect and callback handlers: a turn can't open a browser."""
    raise SignInNeeded("the server wants a new sign-in")


def _oauth(server: Server, storage: _SavedSignIn, redirect: Callable[..., Any], callback: Callable[..., Any]) -> Any:
    """The MCP SDK's OAuth client for one server. It refreshes the access
    token before a request once it has run out, which needs the expiry the
    SDK keeps only in memory: it is read back from ``storage`` here."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata, ProtectedResourceMetadata

    class Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            self.context.token_expiry_time = storage.data.get("expires_at")
            # The SDK keeps discovery in memory only; without it a refresh
            # would go to `<server origin>/token`.
            found = storage.data.get("discovery") or {}
            try:
                if found.get("oauth_metadata") and self.context.oauth_metadata is None:
                    self.context.oauth_metadata = OAuthMetadata.model_validate(found["oauth_metadata"])
                if found.get("resource_metadata") and self.context.protected_resource_metadata is None:
                    self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(found["resource_metadata"])
                if found.get("auth_server_url") and self.context.auth_server_url is None:
                    self.context.auth_server_url = found["auth_server_url"]
            except ValueError:  # kept by another version: discover it again
                pass

    metadata = OAuthClientMetadata(
        client_name="exuvia",
        redirect_uris=[storage.data["redirect_uri"]],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        application_type="web",
    )
    provider = Provider(server.url, metadata, storage, redirect_handler=redirect, callback_handler=callback)
    storage.context = provider.context
    return provider


@dataclass
class _SignIn:
    owner: str
    name: str
    code: "asyncio.Future[Any]"
    task: "asyncio.Task[dict[str, Any]] | None" = None
    back: str | None = None  # the page to return to, when the sign-in ran in its tab rather than a popup


_SIGN_INS: dict[str, _SignIn] = {}  # sign-ins in progress, by their OAuth state


async def _list_tools(server: Server, *, allow_private: bool, auth: Any) -> dict[str, Any]:
    """Connect with ``auth``, list the tools and disconnect: probe's answer."""
    seen = _Http()
    try:
        await check_reachable(server.url, allow_private)
        http, client = _client(server, seen, allow_private=allow_private, auth=auth)
        async with http, client:
            tools = (await client.list_tools()).tools[:MAX_TOOLS]
    except asyncio.CancelledError:
        raise
    except BaseException as e:  # noqa: BLE001 - reported
        return {"ok": False, "error": describe_error(e, seen)}
    return {"ok": True, "tools": [{"name": t.name, "description": (t.description or t.title or "").strip()} for t in tools]}


async def sign_in(
    store: McpServers, owner: str, server: Server, redirect_uri: str, back: str | None = None,
) -> dict[str, Any]:
    """Start an OAuth sign-in to a saved server. ``redirect_uri``: this exuvia
    server's callback (``CALLBACK_PATH``) as the person's browser reaches it.
    ``{url}``: the authorization server's page to open; or, when the server
    let exuvia in without one, probe's answer."""
    loop = asyncio.get_running_loop()
    opened: asyncio.Future[str] = loop.create_future()
    pending = _SignIn(owner, server.name, loop.create_future(), back=back)
    states: list[str] = []

    async def redirect(url: str) -> None:
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("the authorization server's sign-in page is not an http(s) address")
        state = (parse_qs(urlsplit(url).query).get("state") or [""])[0]
        _SIGN_INS[state] = pending
        states.append(state)
        if not opened.done():
            opened.set_result(url)

    async def callback() -> Any:
        return await pending.code

    # A fresh sign-in, with the client registered before if it was for this callback.
    old = server.oauth or {}
    client = old.get("client") if redirect_uri in ((old.get("client") or {}).get("redirect_uris") or []) else None
    storage = _SavedSignIn(
        {"redirect_uri": redirect_uri, **({"client": client} if client else {})},
        lambda data: store.set_oauth(owner, server.name, data, url=server.url),
    )
    bare = Server(server.name, server.url, server.headers)

    async def run() -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                _list_tools(bare, allow_private=store.allow_private, auth=_oauth(bare, storage, redirect, callback)),
                SIGN_IN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "the sign-in was not finished in time: start it again"}
        finally:
            for s in states:
                _SIGN_INS.pop(s, None)

    pending.task = asyncio.create_task(run())
    await asyncio.wait({opened, pending.task}, timeout=CONNECT_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
    if opened.done():
        return {"url": opened.result()}
    if pending.task.done():
        return pending.task.result()
    pending.task.cancel()
    return {"ok": False, "error": "the server did not answer in time"}


async def finish_sign_in(
    owner: str | None, state: str, code: str | None, iss: str | None = None, error: str | None = None,
) -> dict[str, Any]:
    """The authorization server sent the person back: hand the code to the
    sign-in waiting for it and wait for it to connect. Probe's answer, with
    the server's ``name``."""
    from mcp.client.auth import OAuthFlowError
    from mcp.shared.auth import AuthorizationCodeResult

    pending = _SIGN_INS.get(state or "")
    if pending is None or pending.owner != owner or pending.task is None or pending.task.done():
        return {"ok": False, "error": "this sign-in is over or unknown here: start it again from the MCP servers menu"}
    _SIGN_INS.pop(state, None)
    if error or not code:
        pending.code.set_exception(OAuthFlowError(f"the authorization server said {error or 'nothing'}"))
    else:
        pending.code.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
    try:
        result = await asyncio.wait_for(asyncio.shield(pending.task), CONNECT_TIMEOUT * 2)
    except asyncio.TimeoutError:
        result = {"ok": False, "error": "signed in, but the server did not answer in time: press Test"}
    return {**result, "name": pending.name, **({"back": pending.back} if pending.back else {})}
