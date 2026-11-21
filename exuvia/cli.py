"""exuvia command line.

    exuvia init <dir>            scaffold a new project
    exuvia serve [dir]           run the UI + API + scheduler
    exuvia hub <dir>             serve every project under <dir> (API + UI at /p/<id>/)
    exuvia run [dir] [node...]   run nodes headlessly (CI / cron friendly);
                                 --param as_of=2026-03-02 replays a parameter set
    exuvia status [dir]          print node freshness
    exuvia mcp [dir | --url URL] the agent's tools for MCP clients (Claude Code, Cursor, ...)
    exuvia executor              run nodes for a remote control plane (HTTP service)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .project import Project


def _load_dotenv(root: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so secrets stay out of node code."""
    import os

    for candidate in (root / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _cli_params(pairs: list[str] | None) -> dict[str, str]:
    """``--param as_of=2026-03-02`` (repeatable) -> run parameters."""
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--param expects name=value, got {pair!r}")
        out[key.strip()] = value.strip()
    return out


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.dir)
    p = Project(root)
    if not p.node_files():
        p.write_node(
            "hello",
            '"""A first node: returns a tiny table."""\nimport exuvia\n\n\n@exuvia.node()\ndef hello():\n'
            '    import pandas as pd\n\n    return pd.DataFrame({"x": [1, 2, 3], "y": [1, 4, 9]})\n',
        )
    (root / ".gitignore").write_text(".exuvia/\n.env\n", encoding="utf-8")
    print(f"Initialized exuvia project at {p.root}\n  exuvia serve {args.dir}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    root = Path(args.dir).resolve()
    _load_dotenv(root)
    import os

    if args.executor:
        os.environ["EXUVIA_EXECUTOR_URL"] = args.executor
    if args.scheduler:
        os.environ["EXUVIA_SCHEDULER"] = args.scheduler
    from .server import create_app

    app = create_app(root)
    print(f"exuvia · {root.name} · http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_hub(args: argparse.Namespace) -> None:
    import os

    import uvicorn

    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _load_dotenv(root)
    if args.executor:
        os.environ["EXUVIA_EXECUTOR_URL"] = args.executor
    if args.scheduler:
        os.environ["EXUVIA_SCHEDULER"] = args.scheduler
    from .server import create_hub_app

    app = create_hub_app(root, args.templates or os.environ.get("EXUVIA_TEMPLATES_DIR"))
    print(f"exuvia hub · {root} · http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_run(args: argparse.Namespace) -> None:
    from .workspace import Workspace

    root = Path(args.dir).resolve()
    _load_dotenv(root)

    async def go() -> int:
        ws = Workspace(root)
        ws.bus.bind(asyncio.get_running_loop())
        ws.executor.start()
        targets = args.nodes or list(ws.graph.nodes)
        try:
            finals = await ws.run(
                targets,
                params=_cli_params(args.param),
                force=set(args.nodes) if args.force else set(),
                trigger="cli",
                cascade=False,
            )
        finally:
            ws.executor.close()
        failed = 0
        for name, run in finals.items():
            status = (run or {}).get("status")
            failed += status in ("failed", "skipped")
            line = f"{status:>8}  {name}"
            if run and run.get("error"):
                line += f"  — {run['error']}"
            print(line)
        if not finals:
            print("everything is fresh")
        return 1 if failed else 0

    sys.exit(asyncio.run(go()))


def cmd_status(args: argparse.Namespace) -> None:
    from .workspace import Workspace

    ws = Workspace(Path(args.dir).resolve())
    st = ws.status(_cli_params(args.param))
    if args.json:
        print(json.dumps(st, default=str, indent=2))
        return
    for name in ws.graph.topo_order() if not ws.graph.cycle_members() else sorted(ws.graph.nodes):
        s = st[name]
        print(f"{s['state']:>8}  {name}" + (f"  ({s['reason']})" if s.get("reason") else ""))


def cmd_mcp(args: argparse.Namespace) -> None:
    import os

    if not args.url:
        root = Path(args.dir or ".").resolve()
        _load_dotenv(root)
        if args.executor:
            os.environ["EXUVIA_EXECUTOR_URL"] = args.executor
        # Schedules belong to `exuvia serve`; an MCP session only edits and runs.
        os.environ.setdefault("EXUVIA_SCHEDULER", "external")
    from .mcp_server import main as mcp_main

    mcp_main(args.dir, args.url)


def cmd_executor(args: argparse.Namespace) -> None:
    import os

    import uvicorn

    from .executor_service import create_executor_app

    if not os.environ.get("EXUVIA_EXECUTOR_TOKEN") and args.host not in ("127.0.0.1", "localhost"):
        print("warning: EXUVIA_EXECUTOR_TOKEN is not set and the executor is listening beyond localhost", file=sys.stderr)
    print(f"exuvia executor · http://{args.host}:{args.port}")
    uvicorn.run(create_executor_app(), host=args.host, port=args.port, log_level="warning")


def main() -> None:
    ap = argparse.ArgumentParser(prog="exuvia", description="AI-first DAG workbench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="scaffold a project")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("serve", help="run UI + API + scheduler")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7420)
    p.add_argument("--executor", metavar="URL", help="run nodes on a remote `exuvia executor` (env EXUVIA_EXECUTOR_URL)")
    p.add_argument("--scheduler", choices=["internal", "external"],
                   help="external: schedules only advance via POST /api/schedule/tick (env EXUVIA_SCHEDULER)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("hub", help="serve many projects from one directory")
    p.add_argument("dir")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7420)
    p.add_argument("--templates", metavar="DIR", help="project templates (env EXUVIA_TEMPLATES_DIR)")
    p.add_argument("--executor", metavar="URL", help="run nodes on a remote `exuvia executor`")
    p.add_argument("--scheduler", choices=["internal", "external"],
                   help="external: schedules only advance via POST /api/schedule/tick")
    p.set_defaults(fn=cmd_hub)

    p = sub.add_parser("run", help="run nodes headlessly")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("nodes", nargs="*")
    p.add_argument("--force", action="store_true", help="re-run named nodes even if fresh")
    p.add_argument("--param", action="append", metavar="NAME=VALUE",
                   help="run parameter, repeatable (e.g. --param as_of=2026-03-02)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="print node freshness")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.add_argument("--param", action="append", metavar="NAME=VALUE", help="status for this parameter set")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("mcp", help="serve a project's agent tools to MCP clients over stdio")
    p.add_argument("dir", nargs="?", help="project to open in this process (default: .)")
    p.add_argument("--url", metavar="URL",
                   help="relay to a running `exuvia serve` (its address) or hub project (<hub>/p/<project>) instead; "
                        "sends EXUVIA_API_TOKEN when set")
    p.add_argument("--executor", metavar="URL", help="run nodes on a remote `exuvia executor` (env EXUVIA_EXECUTOR_URL)")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("executor", help="serve node execution over HTTP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_executor)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
