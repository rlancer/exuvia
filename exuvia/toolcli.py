"""``exuvia tool``: the agent's tools from a terminal, one call per command.

    exuvia tool --url URL                         list the tools
    exuvia tool --url URL guide                   how exuvia works, and this project's notes
    exuvia tool --url URL run_node --schema       a tool's description and input schema
    exuvia tool --url URL run_node -a name=prices
    exuvia tool --url URL write_node '{"name": "x", "kind": "sql", "source": "select 1"}'
    echo '{...}' | exuvia tool --url URL write_node -

These are the tools ``exuvia mcp`` serves, for an agent that has a shell but
no working MCP connection: a client without MCP, an MCP setup that fails, a
corporate proxy that the client's own connection does not get through. Each
call is a plain HTTPS request to ``POST <url>/api/tools/{name}`` with
``EXUVIA_API_TOKEN`` (or ``EXUVIA_API_TOKEN_FILE``) as the bearer token, and
it uses the system's proxy settings and trust store (nethttp.py).

``URL`` is an ``exuvia serve`` address, a hub project (``<hub>/p/<project>``)
or a whole hub (each project tool then takes ``-a project=<id>``). It can
come from ``EXUVIA_URL`` instead. With neither, the project folder in
``--project`` (default: the current folder) is opened in this process.

The tool's answer goes to stdout. A tool that fails prints its answer to
stderr and exits 1; a server that cannot be reached exits 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "tool",
        help="call the agent's tools from a terminal (when MCP is not an option)",
        description="Call one of the agent's tools (the ones `exuvia mcp` serves) and print its answer. "
                    "With no tool name, list them.",
        epilog="examples:\n"
               "  exuvia tool --url https://hub.example.com/p/research guide\n"
               "  exuvia tool run_node --schema\n"
               "  exuvia tool run_node -a name=prices -a wait_seconds=120\n"
               "  echo '{\"name\": \"x\", \"kind\": \"sql\", \"source\": \"select 1\"}' | exuvia tool write_node -",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", nargs="?", help="the tool; `guide` for the instructions; leave out to list the tools")
    p.add_argument("input", nargs="?", help="the tool's input as a JSON object, or - to read it from stdin")
    p.add_argument("-a", "--arg", action="append", default=[], metavar="KEY=VALUE",
                   help="one input field, repeatable; VALUE is read as JSON when it is JSON (42, true, [..]), else as text")
    p.add_argument("--url", metavar="URL",
                   help="a running exuvia: its address, <hub>/p/<project>, or a hub (env EXUVIA_URL); sends EXUVIA_API_TOKEN")
    p.add_argument("--project", metavar="DIR", help="a project folder on this machine instead (default: .)")
    p.add_argument("--allow-http", action="store_true", help="send to a plain-http address on another machine anyway")
    p.add_argument("--schema", action="store_true", help="print the tool's description and input schema instead of calling it")
    p.add_argument("--json", action="store_true", help='print {"ok": ..., "text": ...} instead of the answer alone')
    p.set_defaults(fn=cmd_tool)


def parse_input(raw: str | None, pairs: list[str], stdin: Any = None) -> dict[str, Any]:
    """The tool input from a JSON argument (``-`` reads stdin) and ``-a
    KEY=VALUE`` fields, which win over the JSON's."""
    args: dict[str, Any] = {}
    if raw is not None:
        text = (stdin or sys.stdin).read() if raw == "-" else raw
        try:
            args = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise SystemExit(f"exuvia tool: the input is not JSON ({e.msg} at line {e.lineno} column {e.colno})") from None
        if not isinstance(args, dict):
            raise SystemExit("exuvia tool: the input must be a JSON object, such as {\"name\": \"prices\"}")
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"exuvia tool: -a takes KEY=VALUE, got {pair!r}")
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    return args


def cmd_tool(args: argparse.Namespace) -> None:
    url = args.url or os.environ.get("EXUVIA_URL") or None
    if url and args.project:
        raise SystemExit("exuvia tool: give --url (or EXUVIA_URL) or --project, not both")
    if args.input is not None and args.name is None:
        raise SystemExit("exuvia tool: name the tool before its input")
    tool_input = parse_input(args.input, args.arg) if args.name and not args.schema else {}
    try:
        code = asyncio.run(_run(url, args.project, args.name, tool_input, schema=args.schema, raw=args.json,
                                allow_http=args.allow_http))
    except BrokenPipeError:  # `exuvia tool ... | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 0
    sys.exit(code)


async def _backend(url: str | None, project: str | None, allow_http: bool = False) -> Any:
    from .mcp_server import HubBackend, LocalBackend, RemoteBackend, remote_backend

    if url:
        from . import remote

        try:
            url = remote.check_url(url, allow_http)  # plain http only to this machine, unless --allow-http
            backend: Any = await remote_backend(url, remote.token())
        except ValueError as e:
            raise SystemExit(f"exuvia tool: {e}") from None
        if isinstance(backend, HubBackend):
            await backend.start()  # says plainly when the token is missing or wrong
        assert isinstance(backend, (HubBackend, RemoteBackend))
        return backend
    from .cli import _load_dotenv

    root = Path(project or ".").resolve()
    if not (root / "nodes").is_dir():
        raise SystemExit(
            f"exuvia tool: {root} is not an exuvia project (no nodes/). Pass --url (or set EXUVIA_URL) "
            "to use a running exuvia, or --project <folder>."
        )
    _load_dotenv(root)
    os.environ.setdefault("EXUVIA_SCHEDULER", "external")  # schedules belong to `exuvia serve`
    backend = LocalBackend(root)
    with _quiet():
        await backend.start()
    return backend


class _quiet:
    """Send stray prints (the workspace's, a node's) to stderr, so stdout
    holds the tool's answer and nothing else."""

    def __enter__(self) -> None:
        self.out = sys.stdout
        sys.stdout = sys.stderr

    def __exit__(self, *_: Any) -> None:
        sys.stdout = self.out


async def _tool_list(backend: Any) -> list[dict[str, Any]]:
    from .mcp_server import GUIDE_TOOL, RemoteBackend
    from .remote import Unreachable

    if isinstance(backend, RemoteBackend):
        try:
            await backend._load_guide()  # not this install's list when the server is down
        except Unreachable as e:
            raise _Unreachable(backend.offline_message(e)) from None
    return [getattr(backend, "guide_tool", GUIDE_TOOL), *await backend.tools()]


class _Unreachable(Exception):
    pass


async def _run(url: str | None, project: str | None, name: str | None, tool_input: dict[str, Any],
               *, schema: bool, raw: bool, allow_http: bool = False) -> int:
    try:
        backend = await _backend(url, project, allow_http)
    except RuntimeError as e:  # a hub that is not answering or turned the token away, an older server
        print(f"exuvia tool: {e}", file=sys.stderr)
        return 2
    try:
        if name is None or schema:
            tools = await _tool_list(backend)
            if name is None:
                for t in tools:
                    print(f"{t['name']}\n    {_first_sentence(t['description'])}")
                return 0
            tool = next((t for t in tools if t["name"] == name), None)
            if tool is None:
                print(f"exuvia tool: no tool {name}; `exuvia tool` lists them", file=sys.stderr)
                return 1
            print(json.dumps({k: tool[k] for k in ("name", "description", "input_schema")}, indent=2))
            return 0
        with _quiet():
            if name == "guide":
                guide = getattr(backend, "guide", None)
                ok, text = await guide(tool_input) if guide else (True, await backend.instructions())
            else:
                ok, text = await backend.call(name, tool_input)
    except _Unreachable as e:
        print(f"exuvia tool: {e}", file=sys.stderr)
        return 2
    finally:
        await backend.stop()
    if raw:
        print(json.dumps({"ok": ok, "text": text}))
    else:
        print(text, file=sys.stdout if ok else sys.stderr)
    if not ok and text.startswith("could not reach"):
        return 2
    return 0 if ok else 1


def _first_sentence(text: str) -> str:
    line = " ".join(text.split())
    end = line.find(". ")
    line = line[: end + 1] if end != -1 else line
    return line if len(line) <= 160 else line[:157] + "..."
