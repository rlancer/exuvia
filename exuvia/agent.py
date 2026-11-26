"""The exuvia agent: Claude with tools over the live graph.

The agent edits the same node files a human edits, runs nodes through the same
executor, and inspects real outputs before answering. Every tool call is
streamed to the UI so the analyst sees exactly what changed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, AsyncIterator

from . import auth, docs, feedback
from .agent_mcp import McpServers, Toolbox
from .agent_mcp import enabled as mcp_enabled
from .llm import Provider, resolve_provider
from .envvars import EnvError
from .npm import NpmError
from .npm import declared as npm_declared
from .packages import PackagesError, agent_mode, missing_package
from .project import NODE_NAME, coerce_param
from .report import log_tail, node_report
from .workspace import Workspace, acting_as

MAX_TURNS = 30

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_nodes",
        "description": "List every node in the graph with its kind, upstream deps, description, schedule, freshness state, and output schema (columns + dtypes) and row count when it has run.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_node",
        "description": "Return the full source code of one node file.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_node",
        "description": (
            "Create or overwrite a node file. `source` is the complete file contents. "
            "Returns the parsed deps or a parse error. Writing does not run the node; call run_node after."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case node name; also the file name"},
                "kind": {"type": "string", "enum": ["python", "sql"]},
                "source": {"type": "string"},
            },
            "required": ["name", "kind", "source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_node",
        "description": "Delete a node file. Only do this when the user asked for it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_files",
        "description": (
            "List the project's shared helper modules (`lib/**/*.py`) with their size and the nodes that import each."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_file",
        "description": "Return the source of one helper module, e.g. `lib/stats.py`, and the nodes that import it.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "lib/<name>.py or lib/<package>/<name>.py"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a helper module under `lib/` (Python only; nodes import it as "
            "`from lib.stats import weighted_corr`). `source` is the complete file. Returns the nodes that "
            "import it: they are now stale, so re-run them. Use helpers for code several nodes share "
            "instead of copying it into each node."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "lib/<name>.py or lib/<package>/<name>.py"},
                "source": {"type": "string"},
            },
            "required": ["path", "source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a helper module under `lib/`. Only when the user asked for it or no node imports it any more.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_revisions",
        "description": (
            "List the saved versions of one node's code (`name`), or of every file of one view (`view`), newest "
            "first: path, hash, who saved it (agent, api, file, template, restore, ...), when, and whether it is "
            "the current code. Read an old version with read_revision; bring it back with restore_node or "
            "restore_view_file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "a node name"},
                "view": {"type": "string", "description": "a view name, instead of `name`"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_revision",
        "description": "Return the full source of a saved version, by the hash list_revisions gave.",
        "input_schema": {
            "type": "object",
            "properties": {"hash": {"type": "string"}},
            "required": ["hash"],
            "additionalProperties": False,
        },
    },
    {
        "name": "restore_node",
        "description": (
            "Make a saved version of a node its current code again (also brings back a deleted node). "
            "The restore is itself saved as a new version, so it can be undone the same way."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "hash": {"type": "string"}},
            "required": ["name", "hash"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_views",
        "description": (
            "List the project's views (custom UI under `views/<name>/`) with their title, the nodes each reads, "
            "its files, and its state for the default parameters: ready, missing (a read node has no output), "
            "stale (one is out of date) or error (with the reason)."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_view",
        "description": "Return one view's state and the full source of each of its files, view.json first.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_view",
        "description": (
            "Create a view or replace its manifest (`views/<name>/view.json`). `reads` lists the nodes whose "
            "outputs the view may read, and nothing else is readable from it. The entry file (default App.tsx) "
            "is written separately with write_view_file. Returns the view's state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case view name; also the folder name"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "reads": {"type": "array", "items": {"type": "string"}, "description": "node names"},
                "entry": {"type": "string", "description": "the component file, default App.tsx"},
                "runtime": {"type": "string", "enum": ["react"]},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_view_file",
        "description": (
            "Create or overwrite one file of a view, e.g. `App.tsx` or `components/Chart.tsx` (paths are "
            "relative to the view folder; .tsx .ts .jsx .js .css .json .html .md .svg, at most 512 KB). "
            "`source` is the complete file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "the view"},
                "path": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["name", "path", "source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_view_file",
        "description": "Delete one file of a view.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
            "required": ["name", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_view",
        "description": "Delete a whole view folder. Only when the user asked for it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "restore_view_file",
        "description": (
            "Make a saved version of one view file its current content again (also brings back a deleted file "
            "or view). `path` is relative to the view folder; the hash comes from list_revisions with `view`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "path": {"type": "string"}, "hash": {"type": "string"}},
            "required": ["name", "path", "hash"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_node",
        "description": (
            "Run a node (and any stale upstream nodes). Returns status, error + log tail on failure, "
            "and on success the output schema, row count, and the first rows (`parts`, by name, for a node with "
            "several outputs). `params` overrides run "
            "parameters for this run (e.g. {\"as_of\": \"2026-03-02\"} to replay a past date); omitted "
            "parameters use their defaults. `full_refresh` (partitioned nodes only) rebuilds the output from "
            "empty: what the run returns becomes the whole output, and exuvia.partitions() is [] for it. "
            "It waits up to `wait_seconds` (default 240, at most 900; 0 returns at once) for the run to end; a run "
            "still going then keeps running and comes back as status `running` with its run_id and log so far: "
            "follow it with read_run (which can wait too), stop it with cancel_run. Do not call run_node again "
            "to check on it, that queues another run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "params": {"type": "object"},
                "full_refresh": {"type": "boolean"},
                "wait_seconds": {
                    "type": "integer", "minimum": 0, "maximum": 900,
                    "description": "how long to wait for the run to end, default 240",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_data",
        "description": (
            "Run read-only DuckDB SQL over the current outputs of all table nodes; each node is a view "
            "named after the node, and each table of a node with several outputs is `<node>.<name>`. Use it to inspect data before writing or after running nodes. Max 50 rows returned. "
            "`params` picks which parameter set the views show (defaults otherwise). For nodes that depend on "
            "parameters, `<node>__runs` unions every parameter set that has run, with a `_<param>` column each — "
            "use it to compare runs across dates. It reads node outputs only: no other file and no network "
            "(read a file in a node instead)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}, "params": {"type": "object"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "define_param",
        "description": (
            "Declare or change a project run parameter (stored in exuvia.json). Nodes that list it in `params` "
            "get one output per value. Use a `date` parameter named `as_of` with default `today` when results "
            "should be reproducible as of a past date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case parameter name"},
                "type": {"type": "string", "enum": ["date", "string", "number", "boolean"]},
                "default": {"description": "default value; for a date, `today` means the run date"},
                "description": {"type": "string"},
            },
            "required": ["name", "type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_param",
        "description": (
            "Remove a project run parameter from exuvia.json. Only when the user asked for it: nodes that still "
            "read it fail until they stop declaring it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_runs",
        "description": (
            "Recent runs, newest first: of one node (`name`) or of every node. Each has its id, node, status "
            "(queued, running, success, cached, failed, skipped; a stopped run is failed with error `cancelled`), trigger (schedule, cascade, agent, mcp, api, "
            "...), who started it, when it was queued, started and finished (UTC), its parameter values, rows and "
            "error. Use it to see whether scheduled runs succeeded and to find a failed one; read_run gives its log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "a node name; omit for every node"},
                "limit": {"type": "integer", "description": "how many runs, default 20, at most 100"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_run",
        "description": (
            "One run, by the id list_runs or run_node gave: status, trigger, timing, parameter values, the "
            "output ids of the inputs it read, rows and schema, the error, and the end of its log (`log_lines`, "
            "default 200), stdout and stderr in the order printed. A run still queued or running shows its log so "
            "far; `wait_seconds` (at most 900) waits that long for it to end first. When a long traceback pushes "
            "what the node printed out of the tail, `printed` has the lines just before it. When the node's code "
            "has changed since, `source` is the code that run executed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "log_lines": {"type": "integer", "description": "how many lines from the end of the log, default 200"},
                "wait_seconds": {
                    "type": "integer", "minimum": 0, "maximum": 900,
                    "description": "wait up to this long for a queued or running run to end, default 0",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_run",
        "description": (
            "Stop a run that is queued or running, by the id run_node or list_runs gave: a running attempt is "
            "killed, a queued run (waiting for its upstream nodes or behind another run of the same node) ends "
            "at once, and it is not retried. It ends failed with error `cancelled`; runs downstream of it in the "
            "same batch are skipped. Use it for a run that hangs or that runs code you have since fixed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_packages",
        "description": (
            "The packages this project lists in its pyproject.toml, each with the version installed and whether "
            "it comes from the project or from exuvia itself, plus the install status (installing, ready, failed "
            "with uv's error) and any requests still waiting for approval. With `environment: true`, also "
            "`environment`: every package installed where nodes import from (exuvia's own environment and the "
            "project's install), name to version. Check it before asking for a package. `view_packages` lists "
            "the npm packages the project's views may import (its package.json)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "environment": {"type": "boolean", "description": "also list every installed package"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_env",
        "description": (
            "The project's environment variables, which every node gets in os.environ: each one's name, whether "
            "it is secret, and a plain variable's value. A secret's value is never shown, and it appears as *** "
            "in run logs. `readable: false` marks a secret this server cannot decrypt, which nodes do not get "
            "until a person enters it again. `source` says where each comes from: `project` (the project's own), "
            "`group` (inherited from the hub group the project is in, named in `group`) or `hub` (set by the "
            "hub's admins for every project on it); a project variable wins over its group's of the same name "
            "(`overrides_group`), and both over the hub's (`overrides_hub`). People set them in the workbench's "
            "Environment panel, on the group's page or (admins) on the hub's page; you cannot set them. Check here "
            "for the name of an API key or setting before writing a node that reads one."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "request_secret",
        "description": (
            "Ask the user to set an API key, token or other secret a node needs, without it ever passing "
            "through you: give only the variable's name and why it is needed. The user enters the value "
            "themselves, in the workbench's Environment panel or with `exuvia env set` in their own terminal, "
            "and it is stored encrypted. The result says whether it is already set, and otherwise how the user "
            "sets it (a link, a terminal command): pass that on. Never ask for the value in the chat, never "
            "write one into a file, a node or a command, and never run `exuvia env set` yourself. Call "
            "list_env to see when it is set, then run the node."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "the variable's name, e.g. FRED_API_KEY"},
                "reason": {"type": "string", "description": "one line for the user: what needs it"},
            },
            "required": ["name", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_packages",
        "description": (
            "Ask for packages to be added to the project's pyproject.toml and installed, for nodes that import "
            "something not available. Give PyPI requirement strings (`scikit-learn`, `scipy>=1.14`); one without "
            "a version is pinned to the version installed. Depending on the server, the request either waits for "
            "a person to approve it in the workbench or is installed right away; the result says which. Ask only "
            "for packages the task needs, and check the package name: never guess at names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "packages": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "reason": {"type": "string", "description": "one line for the person approving: what needs it"},
            },
            "required": ["packages", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_view_packages",
        "description": (
            "Add npm packages for views to import (charting, tables, dates, markdown...) to the project's "
            "package.json: `recharts`, `date-fns@^4`, `@tanstack/react-table`. One without a version is pinned to "
            "the newest release. Each one is checked on the CDN exuvia serves them from and added right away; a view "
            "then imports it by name (`import { LineChart } from \"recharts\"`), and a subpath "
            "(`lodash-es/debounce`, `leaflet/dist/leaflet.css`) works too. Views run with no network, so a package "
            "that fetches anything (map tiles, web fonts, an API) will not work there. Never add react, react-dom, "
            "apache-arrow or vega-embed: views have them already."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "packages": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["packages"],
            "additionalProperties": False,
        },
    },
    {
        "name": "docs",
        "description": (
            "exuvia's documentation, for what the guide does not cover: the `exuvia` command line (every command "
            "and its options) and updating exuvia, deploying a server or hub (Docker, "
            "Compose, Kubernetes), auth and agent tokens, private package indexes and Kerberos, executors, record "
            "databases, schedules on hosts that sleep, the HTTP API, embedding exuvia, MCP setup, views in depth. "
            "With no input: the table of contents. `topic`: one section by its heading. `query`: the passages "
            "that match some words, across every section. It is the README of the installed version, read "
            "locally: no network."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "a heading from the table of contents, e.g. `Kerberos` or `Deploying`"},
                "query": {"type": "string", "description": "words to search for, e.g. `postgres records`"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "version",
        "description": (
            "Which exuvia this is and how to update it: the running `version`, how it was `install`ed (`uvx`, "
            "`uv-tool`, `pipx`, `pip`, `source` for a clone, or `deployed` for a server run from the image), the "
            "`latest` release on PyPI, `update` (`available`, `current`, `off`, `dev` or `error`) and `upgrade`: the "
            "command that updates this install, or null on a deployed server, which whoever runs it updates by "
            "deploying the new `image`. Through `exuvia mcp --url` it answers for the server and for the `exuvia` on "
            "this machine, which can differ. An update takes effect when that exuvia restarts. Never update exuvia "
            "unless the user asks you to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "ask PyPI again rather than use the answer cached for a few hours"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "report_issue",
        "description": (
            "Tell the exuvia maintainers something is wrong or missing: a bug in exuvia itself, a tool that fails "
            "on valid input, instructions that are wrong or confusing, a feature the user needs that does not "
            "exist, or plain feedback. Reports are welcome, so use this when you hit a problem that is exuvia's, "
            "not one in the user's own code or data. Sends nothing: it returns a draft GitHub issue and a link "
            "that opens it prefilled, for the user to read and submit. Describe what you did, what happened and "
            "what you expected, with the exact error text; leave out secrets and the user's data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "one line, specific: the symptom, not 'it broke'"},
                "details": {
                    "type": "string",
                    "description": (
                        "Markdown: what you were doing, what happened (exact error), what you expected, and "
                        "the smallest steps that reproduce it"
                    ),
                },
                "kind": {"type": "string", "enum": ["bug", "idea", "feedback"], "description": "default bug"},
            },
            "required": ["title", "details"],
            "additionalProperties": False,
        },
    },
]


VIEW_TOOLS = {
    "list_views", "read_view", "write_view", "write_view_file", "delete_view_file", "delete_view", "restore_view_file",
}

# Tools that change nothing in the project: all a viewer of a shared hub
# project may call (sharing.py). Every other tool needs an editor.
READ_TOOLS = frozenset({
    "list_nodes", "read_node", "list_files", "read_file", "list_revisions", "read_revision",
    "list_views", "read_view", "query_data", "list_runs", "read_run", "list_packages", "list_env",
    "report_issue", "version",
})

RUNS_LIMIT = 100
RUN_LOG_LINES = 200
RUN_WAIT_DEFAULT = 240  # seconds run_node waits: under the ~300 s idle timeout of common MCP clients
RUN_WAIT_MAX = 900  # the stdio relay's own timeout (mcp_server.REMOTE_TIMEOUT)
RUNNING_LOG_LINES = 20
RUN_TEXT_MAX = 20000  # characters of a read_run answer
RUN_SOURCE_MAX = 8000


def _utc(ts: Any) -> str | None:
    """A stored epoch time as ISO 8601 UTC, for a model to read."""
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in run.items() if k not in ("queued_at", "started_at", "finished_at") and v not in (None, "", {}, [])}
    for k in ("queued_at", "started_at", "finished_at"):
        if run.get(k) is not None:
            out[k.removesuffix("_at")] = _utc(run[k])
    return out


async def _wait_for_run(ws: Workspace, run_id: str, seconds: float) -> dict[str, Any] | None:
    """The run once it has ended, or as it is after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        run = ws.store.run(run_id)
        if run is None or run["status"] not in ("queued", "running") or time.monotonic() >= deadline:
            return run
        await asyncio.sleep(min(1.0, max(deadline - time.monotonic(), 0.05)))


def _running_report(ws: Workspace, target: str, queued: dict[str, str]) -> dict[str, Any]:
    """What run_node says about a run still going when it stops waiting: each
    queued run's status, and the log so far of the one running."""
    runs = {n: ws.store.run(rid) or {} for n, rid in queued.items()}
    out: dict[str, Any] = {
        "status": "running",
        "run_id": queued.get(target),
        "runs": {n: r.get("status") for n, r in runs.items()},
        "next": (
            "The run goes on. read_run with this run_id shows its log so far (wait_seconds waits for it to end); "
            "cancel_run stops it. Calling run_node again would queue another run."
        ),
    }
    going = next((n for n, r in runs.items() if r.get("status") == "running"), None)
    if going:
        log = (runs[going].get("log") or "").splitlines()
        if going != target:
            out["running_now"] = {"node": going, "run_id": queued[going]}
        out["log_tail"] = "\n".join(log[-RUNNING_LOG_LINES:])
    return out


def _available_packages(root: Path | None = None) -> str:
    """The data packages exuvia's environment has, plus the ones the project
    declares in its pyproject.toml (installed before a node runs)."""
    from .packages import PackagesError, declared

    try:
        extra = declared(root) if root else []
    except PackagesError:
        extra = []
    wanted = [
        "pandas", "numpy", "duckdb", "pyarrow", "polars", "scipy", "statsmodels",
        "scikit-learn", "requests", "httpx", "yfinance", "matplotlib",
    ]
    have = []
    for pkg in wanted:
        try:
            have.append(f"{pkg} {metadata.version(pkg)}")
        except metadata.PackageNotFoundError:
            pass
    out = ", ".join(have)
    if extra:
        out += "; and from the project's pyproject.toml: " + ", ".join(extra)
    return out


def _view_packages(root: Path) -> str:
    """The npm packages the project's views may import, for the prompt."""
    try:
        deps = npm_declared(root)
    except NpmError as e:
        return f"which does not parse ({e})"
    return "now: " + ", ".join(f"`{n}` {v}" for n, v in sorted(deps.items())) if deps else "none yet"


def _packages_how() -> str:
    mode = agent_mode()
    if mode == "off":
        return "ask the user to add a package you need; you cannot install packages on this server."
    if mode == "install":
        return "request_packages adds and installs packages directly on this server; ask only for what the task needs."
    return (
        "ask for a package with request_packages. A person approves the request in the workbench, so tell the "
        "user it is waiting for them, and re-run the node once list_packages shows it installed."
    )


SYSTEM = """You are the agent inside exuvia, a DAG workbench for analysts and data scientists. You build and maintain data pipelines by editing node files and running them.

## How exuvia works
- A project is a folder of node files in `nodes/`. One file = one node. File name (snake_case) = node name.
- Python node: a module with exactly one function decorated `@exuvia.node(...)`. Its parameter names ARE its upstream dependencies; exuvia passes each upstream's output (table outputs arrive as pandas DataFrames). Return a DataFrame (preferred), a dict/list/scalar (stored as JSON; numpy values, dates, dataclasses and pydantic models are converted), or None. Any other object (a fitted model, a custom class) fails the run: return the numbers you need from it instead. Put the description in the function docstring.
- Several outputs from one node: when one step naturally yields several results of different shapes (a model fit's predictions, coefficients and metrics; an API call that returns two tables), return a dict, dataclass or pydantic model whose values include DataFrames, e.g. `return {"preds": preds_df, "coefs": coefs_df, "metrics": {"r2": r2}}`. Names are lowercase identifiers. Each DataFrame is a table and anything else is JSON. Downstream, SQL reads a table as `<node>.<name>` (`select * from fit.coefs`); a Python parameter `fit` gets a dict `{"preds": DataFrame, ...}`, or the dataclass / pydantic model itself when the parameter is annotated with it (`def report(fit: FitResult)`, the class in a `lib/` helper). A JSON output can be typed the same way. Prefer separate nodes when the results do not come from the same expensive step.
- Decorator keywords (must be literals): `schedule="<cron>"` (local time; re-runs this node on that cadence), `cascade=True|False` (auto re-run when upstream output changes; default True), `chart={...}` a Vega-Lite spec WITHOUT a `data` key — the UI injects the node's output rows, `tags=[...]`, `retries=N` (try a failed run again up to N more times, 0-10; for flaky sources such as APIs) with `retry_delay=<seconds>` (default 30, doubling per retry), `overlap="queue"|"skip"` (a scheduled fire while the node is still running: run it after, the default, or drop it), `timeout=<seconds>` (an attempt that takes longer is stopped and fails, then retried with `retries`; default the server's EXUVIA_RUN_TIMEOUT, 1800; set it on nodes that read from something that can hang, such as an external database; SQL: `-- timeout: 300`), `push=True` (a push node: its output is uploaded from another machine with `exuvia push`, for data only that machine can read, such as a Bloomberg Terminal's; it takes no upstream nodes and no schedule, its function never runs here, and run_node refuses it), `live=True` or `live=<seconds>` (a live node re-runs whenever a view that depends on it is opened, or only when its output is older than that many seconds; the view shows its last output at once and updates when the new one lands; for data worth fetching again whenever someone looks, such as quotes; not for push nodes), `partition_by="<column>"` (a partitioned node, for data that grows a little each run, such as daily prices: each run returns rows for one or many values of that column and they replace those partitions, every other partition is kept; `exuvia.partitions()` lists the values already stored, [] on the first run or a full refresh, so the node fetches only what is new; SQL: `-- partition_by: date` and `$last_partition`; downstream nodes read the whole history; its own params such as as_of don't split the output; rebuild it from scratch with run_node full_refresh), `key=["date", "ticker"]` (the columns that identify a row: a run whose output has a null in them or a key twice fails and the previous output stays; declare it on every table node whose rows have a natural key, so duplicates can't skew an analysis; for a partitioned node it holds across the whole output, so a row whose partition changes must be returned with its old partition too), `on_duplicate="last"` (with key: keep the last row for each repeated key instead of failing, only for sources known to resend rows), `merge=True` (partitioned node with a key, for sources that send part of a day at a time: a run's rows update or insert by key into the partitions they hold, and stored rows the run doesn't return stay; a merge never removes a row, so deletes and re-dated rows need run_node full_refresh, which replaces the whole output).
- SQL node (`.sql`): DuckDB SQL; reference upstream nodes by name as tables. Header comments: first `--` lines are the description; `-- schedule: <cron>`, `-- cascade: false`, `-- chart: {json}`, `-- retries: 2`, `-- retry_delay: 60`, `-- overlap: skip`, `-- live: true` (or seconds), `-- partition_by: date` (then `$last_partition` is the newest stored value, or NULL), `-- key: date, ticker`, `-- on_duplicate: last`, `-- merge: true`.
- Run parameters make results reproducible over time. A project declares them (see "Run parameters" below; add one with define_param). A node that reads one must declare it: `@exuvia.node(params=["as_of"])` then `exuvia.param("as_of")` (dates arrive as "YYYY-MM-DD" strings); in SQL, `-- params: as_of` then `$as_of`. A node's output is stored per value of the parameters it or its upstream nodes declare, so each date has its own output and history, and nodes without parameters are computed once and shared. A node that reads `as_of` must compute "as of that date": filter source data to rows known on or before it, and never call "now"/"today" yourself.
- Outputs are content-hashed. Unchanged output stops a cascade. Each node runs in its own subprocess with the project root as the working directory and on sys.path.
- Shared helpers: code several nodes need (a formula, an interpolation, a cleaning step) goes in a helper module under `lib/` instead of being copied into each node. Write it with write_file (e.g. `lib/stats.py`; packages like `lib/vol/surface.py` work too, no `__init__.py` needed) and import it in the node with a plain import statement: `from lib.stats import weighted_corr`. Helpers are Python only and are not nodes: no `@exuvia.node`, no outputs. A node's code hash covers every helper it imports (directly or through other helpers), so editing a helper makes those nodes stale; write_file returns them in `used_by` — re-run them. Imports built at runtime (importlib, `__import__`) are not tracked.
- Secrets and settings come from environment variables: read them with `os.environ["NAME"]` (or `.get`), never hardcode credentials or copy a key into a file. `list_env` shows which variables the project has. When a node needs one that is missing, write the node to read it by a clear name (e.g. `FRED_API_KEY`) and call `request_secret` with that name and why: the user enters the value themselves and it never passes through you. Never ask them to paste a secret into the chat.

## Example Python node
```python
import exuvia


@exuvia.node(schedule="0 18 * * 1-5", chart={"mark": "line", "encoding": {"x": {"field": "date", "type": "temporal"}, "y": {"field": "vol_20d", "type": "quantitative"}, "color": {"field": "ticker"}}})
def rolling_vol(returns):
    \"\"\"20-day annualized volatility per ticker.\"\"\"
    out = returns.sort_values("date").copy()
    out["vol_20d"] = out.groupby("ticker")["ret"].transform(lambda s: s.rolling(20).std() * 252 ** 0.5)
    return out.dropna(subset=["vol_20d"])[["date", "ticker", "vol_20d"]]
```

## Working style
- Look before you write: list_nodes, read the nodes you depend on, and query_data to learn real column names and types.
- Prefer small, single-purpose nodes with clear names over one big node. Reuse existing nodes as inputs instead of recomputing.
- After writing a node, run it. If it fails, read the error, fix it, and re-run. Do not hand back a node you have not seen succeed unless the user asked you not to run it.
- Runs you did not start (a schedule, a cascade, someone else): list_runs shows them, newest first, and read_run gives one run's error and log.
- Long runs: run_node waits up to wait_seconds (default 240), then returns status `running` with the run_id while the run goes on. Follow it with read_run (wait_seconds to wait for the end; its log so far otherwise) instead of calling run_node again, and print progress (`print(..., flush=True)`) in nodes that take minutes, so the log shows how far they got. A run that hangs or runs code you have since fixed: cancel_run, then run the node again.
- Add a `chart` when a node's output is naturally visual (time series, distributions, rankings).
- Imports go inside the function or at module top; available packages: {packages}; `list_packages` with `environment: true` lists every installed package and its version. Do not pip install. A package that is not listed goes in the project's `pyproject.toml` under `[project] dependencies`, which exuvia installs from: {packages_how}
- Only delete or rename nodes when asked. Keep edits minimal when changing an existing node.
- For anything about exuvia this guide does not cover (the `exuvia` command line and its options, updating exuvia, deploying it, hubs, auth and tokens, executors, package indexes, the HTTP API, MCP setup), read `docs` before you answer or act: it is the documentation of the installed version. Do not answer those from memory, and do not guess at commands or flags.
- `version` says which exuvia is running, how it was installed, whether a newer release is out and the exact command that updates it. Use it when the user asks about the version or updating, or when something they want is not in this version's `docs`. Update exuvia only when the user asks.
- Finish with a short summary of what you changed and what the data shows — a few sentences, not a report. Describe findings factually; you are a research tool and do not give investment advice.

## Views
- A view is custom UI over node outputs, kept apart from the pipeline: a folder `views/<name>/` holding a `view.json` manifest (`title`, `description`, `runtime` (only "react"), `entry` (default `App.tsx`), `reads`) and its source files (.tsx .ts .jsx .js .css .json .html .md .svg). Create one with write_view, then write its files with write_view_file.
- A view only reads. It sees the outputs of the nodes listed in `reads` and nothing else, and it never runs anything, except that opening it re-runs the live nodes it depends on (`live=True`). Computation belongs in nodes: when a view needs data no node produces, add or change a node and list it in `reads`; when it should be current each time the view is opened, make that node live.
- A view's state follows the nodes it reads, for a parameter set: ready, missing (a read node has no output yet), stale (one is out of date), or error (broken manifest, missing entry file, unknown node).
- The user sees views in the Views mode of the exuvia UI, rendered in a sandboxed iframe with no network access. The entry file's `export default` is a React component. Imports: the view's own files by relative path (`./Chart`, `./styles.css`, `./data.json`); the packages every view has: `react` (19), `react-dom`, `apache-arrow`, `vega-embed`, `exuvia`; and the npm packages in the project's `package.json`, {view_packages}. Add an npm package with add_view_packages rather than writing a chart or table library by hand. The frame fetches nothing at run time, so a package that loads tiles, fonts or data from the network will not work.
- Data comes from `exuvia`: `useTable(node)` / `useJson(node)` (a read node's current output; `useTable("fit.coefs")` for one output of a node with several), `useQuery(sql)` (DuckDB over the read nodes only, `<node>__runs` for history), each `{data, error, loading}` and reloading when an output changes; `rows(table)` turns an Arrow table into plain objects; `useParams()` gives `[values, setParams, declared]` (setting parameters changes what is shown, it never runs nodes); `useDark()`; `useRefreshing(node?)` (true while a read node is being re-run, e.g. a live node refreshing as the view opened: show a small "updating" note rather than blanking the view); `download(node | {sql}, format)` saves a read node's output or a query's whole result as csv, xlsx, parquet or json (for a download button). Style with the page's CSS variables (`var(--text)`, `--muted`, `--line`, `--panel`, `--accent`, `--mono`), which follow light and dark mode.
- Only write views when the user asks for one.

## When exuvia itself is the problem
- If a tool fails on valid input, exuvia contradicts this guide, this guide is wrong or unclear, or the user needs something exuvia cannot do, say so plainly and offer to report it with report_issue. The maintainers want these reports and the project is young, so do not stay quiet about a rough edge. Do not use it for errors in the user's own nodes or data: fix those.
- report_issue sends nothing. It returns a draft GitHub issue (github.com/rlancer/exuvia) and a link that opens it prefilled; the user reads it and submits. Put in the exact error and the smallest steps to reproduce, never secrets or the user's data.

## Run parameters
{params}{notes}"""


def _validate(name: str, args: Any) -> str | None:
    """Tool inputs are model output; validate before running (eager streaming and
    OpenAI-compatible endpoints do no server-side schema validation)."""
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return f"unknown tool {name}"
    if not isinstance(args, dict):
        return "tool input must be an object"
    schema = tool["input_schema"]
    for key in schema.get("required", []):
        if schema["properties"][key].get("type") == "array":
            if not isinstance(args.get(key), list) or not args[key]:
                return f"missing or invalid `{key}`: a non-empty list"
        elif not isinstance(args.get(key), str) or not args[key]:
            return f"missing or invalid `{key}`"
    for key, prop in schema["properties"].items():
        if "enum" in prop and key in args and args[key] not in prop["enum"]:
            return f"`{key}` must be one of {prop['enum']}"
        if prop.get("type") == "object" and args.get(key) is not None and not isinstance(args[key], dict):
            return f"`{key}` must be an object"
        if prop.get("type") == "string" and key in args and not isinstance(args[key], str):
            return f"`{key}` must be a string"
        if prop.get("type") == "boolean" and args.get(key) is not None and not isinstance(args[key], bool):
            return f"`{key}` must be true or false"
        if prop.get("type") == "integer" and args.get(key) is not None and (
            isinstance(args[key], bool) or not isinstance(args[key], int) or args[key] < prop.get("minimum", 1)
        ):
            least = prop.get("minimum", 1)
            return f"`{key}` must be a positive whole number" if least == 1 else f"`{key}` must be a whole number from {least}"
        if prop.get("type") == "array" and key in args and not (
            isinstance(args[key], list) and all(isinstance(v, str) for v in args[key])
        ):
            return f"`{key}` must be a list of strings"
    extra = set(args) - set(schema["properties"])
    if extra:
        return f"unexpected field(s): {', '.join(sorted(extra))}"
    return None


class Agent:
    def __init__(self, ws: Workspace, command: str = "serve"):
        self.ws = ws
        # The exuvia command hosting this agent (serve, hub or mcp): the
        # `version` tool's update command for uvx runs it again.
        self.command = command
        self.sessions: dict[str, list[Any]] = {}
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.active = 0  # chats in flight (a host must not stop the server mid-turn)
        self.running: dict[str, asyncio.Task[None]] = {}  # session -> background turn
        self.provider: Provider | None = resolve_provider()
        # The people's outside MCP servers (agent_mcp.py), set by the server
        # that hosts this agent; None: the agent has exuvia's tools only.
        self.mcp: McpServers | None = None

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    @property
    def label(self) -> str:
        return self.provider.label if self.provider else ""

    # -- tools ---------------------------------------------------------------
    async def call_tool(self, name: str, args: Any, origin: str = "agent") -> tuple[bool, str, dict[str, Any]]:
        """Run one tool for any caller: this agent's loop, an MCP client
        (``exuvia mcp``) or ``POST /api/tools/{name}``. Inputs are validated
        first, and a bad input or a crash comes back as a failed result, so
        the caller's model can read it and try again. Code changed and nodes
        run are recorded as ``origin``'s."""
        problem = _validate(name, args)
        if problem:
            return False, f"INVALID_INPUT: {problem}", {}
        try:
            return await self._call(name, args, origin)
        except Exception as e:  # surface tool crashes to the model, not the user
            return False, (
                f"{type(e).__name__}: {e}\n\n(An unexpected error inside exuvia. If your input was valid, "
                "this is a bug: tell the user and offer to report it with `report_issue`.)"
            ), {}

    async def _call(self, name: str, args: dict[str, Any], origin: str = "agent") -> tuple[bool, str, dict[str, Any]]:
        """Returns (ok, text-for-model, ui-extra). Code the tool changes is
        recorded as ``origin``'s."""
        with acting_as(origin):
            return await self._dispatch(name, args, origin)

    async def _dispatch(self, name: str, args: dict[str, Any], origin: str = "agent") -> tuple[bool, str, dict[str, Any]]:
        ws = self.ws
        if name == "list_nodes":
            snap = ws.snapshot()
            nodes = [
                {
                    k: n.get(k)
                    for k in (
                        "name", "kind", "deps", "helpers", "description", "schedule", "cascade", "params", "run_params",
                        "state", "reason", "rows", "schema",
                    )
                    if n.get(k) not in (None, [], "")
                }
                for n in snap["nodes"]
            ]
            return True, json.dumps(nodes, default=str), {}
        if name == "read_node":
            try:
                return True, ws.project.source(args["name"]), {"node": args["name"]}
            except KeyError:
                return False, f"no node named {args['name']}", {}
        if name == "write_node":
            with ws.editing():
                ws.project.write_node(args["name"], args["source"], args["kind"])
                ws.reload()
            node = ws.graph.nodes.get(args["name"])
            if node is None:
                return False, "file written but node not found after reload", {}
            if node.parse_error:
                return False, f"written, but it does not parse: {node.parse_error}", {"node": node.name}
            return True, json.dumps({"written": node.name, "deps": node.deps, "schedule": node.schedule}), {"node": node.name}
        if name == "delete_node":
            with ws.editing():
                try:
                    ws.project.delete_node(args["name"])
                except KeyError:
                    return False, f"no node named {args['name']}", {}
                ws.reload()
            return True, f"deleted {args['name']}", {}
        if name == "read_revision":
            source = ws.code_source(args["hash"])
            if source is None:
                return False, f"no saved version {args['hash']}", {}
            return True, source, {}
        if name == "list_revisions":
            view, node = args.get("view"), args.get("name")
            if bool(view) == bool(node):
                return False, "give either `name` (a node) or `view`", {}
            revs = ws.code_revisions(node=node, view=view, limit=50)
            if not revs:
                return False, f"no saved versions of {view or node}", {}
            return True, json.dumps(revs, default=str), {"view": view} if view else {"node": node}
        if name in VIEW_TOOLS:
            return self._view_tool(name, args)
        if name == "restore_node":
            try:
                ws.restore_node(args["name"], args["hash"])
            except KeyError as e:
                return False, str(e.args[0]), {}
            return True, f"restored {args['name']} to version {args['hash']}", {"node": args["name"]}

        if name == "list_files":
            return True, json.dumps(ws.helpers()), {}
        if name in ("read_file", "write_file", "delete_file"):
            path = args["path"]
            try:
                if name == "read_file":
                    info = ws.read_helper(path)
                    used = f"# used by: {', '.join(info['used_by']) or 'no node yet'}\n"
                    return True, used + info["source"], {"file": path}
                if name == "delete_file":
                    return True, json.dumps(ws.delete_helper(path)), {}
                result = ws.write_helper(path, args["source"])
            except ValueError as e:
                return False, str(e), {}
            except KeyError:
                return False, f"no file {path}", {}
            if result["parse_error"]:
                return False, f"written, but it does not parse: {result['parse_error']}", {"file": path}
            return True, json.dumps(result), {"file": path}
        if name == "run_node":
            target = args["name"]
            if target not in ws.graph.nodes:
                return False, f"no node named {target}", {}
            if ws.graph.nodes[target].push:
                return False, (
                    f"{target} is a push node: its output is uploaded from another machine with "
                    "`exuvia push`, never run here. Ask the user to push it."
                ), {"node": target}
            refresh = bool(args.get("full_refresh"))
            if refresh and not ws.graph.nodes[target].partition_by:
                return False, f"{target} is not partitioned: full_refresh is for nodes with partition_by", {}
            try:
                params = ws.resolve_params(args.get("params"))
            except ValueError as e:
                return False, str(e), {}
            wait = args.get("wait_seconds")
            wait = RUN_WAIT_DEFAULT if wait is None else min(max(wait, 0), RUN_WAIT_MAX)
            queued: dict[str, str] = {}
            ready = asyncio.Event()

            def on_queued(ids: dict[str, str]) -> None:
                queued.update(ids)
                ready.set()

            # A task of the workspace's own, so a run still going when this
            # call returns keeps running.
            task = ws._spawn(ws.run(
                [target], params=params, force={target}, trigger=origin,
                full_refresh={target} if refresh else None, on_queued=on_queued,
            ))
            queuing = asyncio.ensure_future(ready.wait())
            try:
                await asyncio.wait({task, queuing}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                queuing.cancel()
            done, _ = await asyncio.wait({task}, timeout=wait)
            if not done:
                return True, json.dumps(_running_report(ws, target, queued), default=str)[:12000], {"node": target}
            ok, report = await node_report(ws, target, params, task.result())
            return ok, json.dumps(report, default=str)[:12000], {"node": target}
        if name == "query_data":
            try:
                res = await asyncio.to_thread(ws.query, args["sql"], limit=50, params=args.get("params"))
            except Exception as e:  # duckdb errors go back to the model
                return False, f"{type(e).__name__}: {e}", {}
            cols = [c["name"] for c in res["columns"]]
            return True, json.dumps([dict(zip(cols, r)) for r in res["rows"]], default=str)[:12000], {}
        if name == "define_param":
            if not NODE_NAME.match(args["name"]):
                return False, "parameter names must be snake_case identifiers", {}
            spec = {"type": args["type"], "default": args.get("default"), "description": args.get("description") or ""}
            try:
                coerce_param(args["name"], spec["type"], spec["default"], ws.project.today)
            except ValueError as e:
                return False, str(e), {}
            ws.set_param(args["name"], spec)
            return True, json.dumps({"defined": args["name"], **spec, "value_now": ws.resolve_params()[args["name"]]}), {}
        if name == "delete_param":
            if args["name"] not in ws.project.params:
                return False, f"no parameter named {args['name']}", {}
            readers = sorted(n.name for n in ws.graph.nodes.values() if args["name"] in (n.params or []))
            ws.set_param(args["name"], None)
            out: dict[str, Any] = {"deleted": args["name"]}
            if readers:
                out["still_declared_by"] = readers
            return True, json.dumps(out), {}
        if name == "list_runs":
            node, limit = args.get("name"), min(args.get("limit") or 20, RUNS_LIMIT)
            if node and node not in ws.graph.nodes and ws.project.path_for(node) is None:
                return False, f"no node named {node}", {}
            rows = ws.store.runs_for(node, limit=limit) if node else ws.store.recent_runs(limit=limit)
            return True, json.dumps([_run_summary(r) for r in rows], default=str), {"node": node} if node else {}
        if name == "cancel_run":
            run = ws.store.run(args["run_id"])
            if run is None:
                return False, f"no run {args['run_id']}: list_runs shows them", {}
            if run["status"] not in ("queued", "running"):
                return False, f"run {run['id']} of {run['node']} has already ended: {run['status']}", {"node": run["node"]}
            if not ws.cancel(run["id"]):
                return False, (
                    f"run {run['id']} of {run['node']} is not going in this server, so it cannot be stopped from "
                    "here (it may have just ended: read_run shows it)"
                ), {"node": run["node"]}
            final = await _wait_for_run(ws, run["id"], 10)
            out = {"cancelled": run["id"], "node": run["node"], "status": final.get("status"), "error": final.get("error")}
            return True, json.dumps(out), {"node": run["node"]}
        if name == "read_run":
            wait = min(max(args.get("wait_seconds") or 0, 0), RUN_WAIT_MAX)
            run = await _wait_for_run(ws, args["run_id"], wait) if wait else ws.store.run(args["run_id"])
            if run is None:
                return False, f"no run {args['run_id']}: list_runs shows them", {}
            raw = run.pop("log", None) or ""
            log = raw.splitlines()
            lines = args.get("log_lines") or RUN_LOG_LINES
            out = _run_summary(run)
            out.update(log_tail(raw, lines))
            if len(log) > lines:
                out["log_lines_omitted"] = len(log) - lines
            src = ws.run_source(args["run_id"]) or {}
            out["code_current"] = src.get("code_current", False)
            if not out["code_current"] and src.get("source"):
                out["source"] = src["source"][:RUN_SOURCE_MAX]
            missing = missing_package(run.get("error"))
            if missing:
                out["missing_package"] = missing
            text = json.dumps(out, default=str)
            if len(text) > RUN_TEXT_MAX:  # a huge log: keep its end
                out["log_tail"] = out["log_tail"][-(RUN_TEXT_MAX // 2):]
                text = json.dumps(out, default=str)
            return True, text, {"node": run["node"]}
        if name == "list_packages":
            st = ws.packages.state()
            st.pop("manifest", None)
            st["log"] = "\n".join(st["log"].splitlines()[-20:])
            if args.get("environment"):
                st["environment"] = ws.packages.environment()
                if not ws.packages.local:
                    st["environment_note"] = (
                        "nodes run on a remote executor: this is the server's environment, which may differ "
                        "from the executor's"
                    )
            st["view_packages"] = ws.npm.state()["packages"]
            return True, json.dumps(st, default=str), {}
        if name == "add_view_packages":
            if agent_mode() == "off":
                return False, "changing packages from the agent is turned off on this server (EXUVIA_AGENT_PACKAGES=off)", {}
            try:
                st = await asyncio.to_thread(ws.npm.add, args.get("packages") or [])
            except NpmError as e:
                return False, str(e)[-3000:], {}
            listed = ", ".join(f"{p['name']}@{p['version']}" for p in st["packages"])
            return True, f"package.json now lists {listed}. Views import them by name.", {}
        if name == "list_env":
            def shown(r: dict[str, Any]) -> dict[str, Any]:
                out = {k: v for k, v in r.items() if k in ("name", "secret", "value", "source") or (k == "readable" and not v)}
                if r["source"] == "group":
                    out["group"] = r["group"]["name"]
                if r.get("overrides"):
                    out["overrides_group"] = r["overrides"]["name"]
                if r.get("overrides_hub"):
                    out["overrides_hub"] = True
                return out

            return True, json.dumps({"variables": [shown(r) for r in ws.effective_env()]}), {}
        if name == "request_secret":
            try:
                res = ws.request_env_var(args["name"], args.get("reason") or "", origin)
            except EnvError as e:
                return False, str(e), {}
            if res["status"] == "set":
                res["message"] = f"{res['name']} is already set: nodes read it with os.environ[{res['name']!r}]."
            else:
                res["message"] = (
                    f"Asked the user to set {res['name']}. It waits for them in the workbench's Environment panel, "
                    f"where they enter the value (it is stored encrypted and never shown to you). From a terminal "
                    f"they can run `exuvia env set {res['name']}` instead. Tell them, without asking for the value; "
                    "call list_env to see when it is set, then run the node."
                )
            return True, json.dumps(res), {}
        if name == "request_packages":
            try:
                res = await asyncio.to_thread(
                    ws.packages.request, args.get("packages") or [], args.get("reason") or "", origin
                )
            except PackagesError as e:
                return False, str(e)[-3000:], {}
            if res["status"] == "requested":
                text = (
                    f"Requested {', '.join(res['packages'])}. A person approves it in the workbench's Packages panel; "
                    "tell the user it is waiting for them, and re-run the node once list_packages shows it installed."
                )
            else:
                text = (
                    f"Added {', '.join(res['packages'])} to pyproject.toml. They install in the background; running "
                    "a node waits for the install to finish."
                )
            return True, text, {}
        if name == "docs":
            ok, text = docs.tool(args)
            return ok, text, {}
        if name == "version":
            from . import about

            return True, json.dumps(await about.about(args.get("refresh") is True, self.command)), {}
        if name == "report_issue":
            return True, json.dumps(feedback.draft(args["title"], args["details"], args.get("kind") or "bug", origin)), {}
        return False, f"unknown tool {name}", {}

    def _view_tool(self, name: str, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        ws = self.ws
        if name == "list_views":
            keys = ("name", "title", "description", "reads", "files", "state", "reason")
            return True, json.dumps([{k: v[k] for k in keys} for v in ws.views()]), {}
        view = args["name"]
        extra = {"view": view}
        try:
            if name == "read_view":
                info = ws.read_view(view)
                head = f"view {view}: {info['state']}" + (f" ({info['reason']})" if info["reason"] else "")
                order = sorted(info["sources"], key=lambda f: (f["path"] != "view.json", f["path"]))
                return True, "\n\n".join([head, *(f"--- {f['path']} ---\n{f['source']}" for f in order)]), extra
            if name == "delete_view":
                return True, json.dumps(ws.delete_view(view)), {}
            if name == "write_view":
                keys = ("title", "description", "reads", "entry", "runtime")
                info = ws.write_view_manifest(view, {k: args[k] for k in keys if k in args})
            elif name == "write_view_file":
                info = ws.write_view_file(view, args["path"], args["source"])
            elif name == "delete_view_file":
                info = ws.delete_view_file(view, args["path"])
            else:  # restore_view_file
                info = ws.restore_view_file(view, args["path"], args["hash"])
        except ValueError as e:
            return False, str(e), {}
        except KeyError as e:
            return False, f"not found: {e.args[0]}", {}
        # A view is often written in pieces (manifest, then entry), so a view
        # that is not complete yet is reported, not treated as a failure.
        summary = {k: info[k] for k in ("name", "reads", "files", "state", "reason")}
        return True, json.dumps(summary), extra

    # -- loop ----------------------------------------------------------------
    async def chat(self, session_id: str | None, message: str, selected: str | None) -> AsyncIterator[dict[str, Any]]:
        assert self.provider is not None
        provider = self.provider
        session_id = session_id or uuid.uuid4().hex[:12]
        history, transcript = self._load(session_id)
        # Who asked: the request's sign-in, which the background turn inherits.
        by, by_user = auth.current_id(), auth.current_uid()
        yield {"type": "session", "session_id": session_id}
        transcript.append({"type": "user", "text": message, "at": time.time()})
        self.active += 1
        toolbox: Toolbox | None = None
        try:
            toolbox = await self._toolbox()
            async for ev in self._loop(provider, history, message, selected, toolbox):
                _record(transcript, ev)
                yield ev
        finally:
            self.active -= 1
            if toolbox is not None:
                await toolbox.close()
            # Conversations are part of the project: they survive restarts.
            self.ws.store.save_agent_session(session_id, history, transcript, by, by_user)
            self.ws.bus.publish({"type": "agent", "session_id": session_id})
        yield {"type": "done"}

    def start(
        self, session_id: str | None, message: str, selected: str | None
    ) -> tuple[str, "asyncio.Queue[dict[str, Any] | None]"]:
        """Run a turn in the background and return a queue of its events
        (``None`` marks the end). The turn keeps going if the caller stops
        listening, and every event is also published on the workspace bus as
        ``agent_event`` so any client can follow or re-attach to it."""
        session_id = session_id or uuid.uuid4().hex[:12]
        if session_id in self.running:
            raise RuntimeError("the agent is already working in this conversation")
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def work() -> None:
            try:
                async for ev in self.chat(session_id, message, selected):
                    self.ws.bus.publish({"type": "agent_event", "session_id": session_id, "event": ev})
                    queue.put_nowait(ev)
            except asyncio.CancelledError:
                stopped = {"type": "error", "error": "Stopped."}
                self.ws.bus.publish({"type": "agent_event", "session_id": session_id, "event": stopped})
                queue.put_nowait(stopped)
            except Exception as e:  # never leave a listener hanging
                failed = {"type": "error", "error": f"{type(e).__name__}: {e}"}
                self.ws.bus.publish({"type": "agent_event", "session_id": session_id, "event": failed})
                queue.put_nowait(failed)
            finally:
                self.running.pop(session_id, None)
                done = {"type": "done"}
                self.ws.bus.publish({"type": "agent_event", "session_id": session_id, "event": done})
                queue.put_nowait(None)

        self.running[session_id] = asyncio.create_task(work())
        return session_id, queue

    async def _toolbox(self) -> "Toolbox | None":
        """This turn's outside MCP tools: the servers on the list of whoever
        asked (agent_mcp.py), connected now. None when they have none."""
        store = self.mcp
        if store is None or not mcp_enabled():
            return None
        owner = store.owner()
        if owner is None:
            return None
        servers = await asyncio.to_thread(store.active, owner)
        if not servers:
            return None
        box = Toolbox(servers, allow_private=store.allow_private)
        await box.start({t["name"] for t in TOOLS})
        return box

    def cancel(self, session_id: str) -> bool:
        task = self.running.get(session_id)
        if task is None:
            return False
        task.cancel()
        return True

    def _load(self, session_id: str) -> tuple[list[Any], list[dict[str, Any]]]:
        if session_id not in self.sessions:
            saved = self.ws.store.agent_session(session_id)
            self.sessions[session_id] = saved["history"] if saved else []
            self.transcripts[session_id] = saved["events"] if saved else []
        return self.sessions[session_id], self.transcripts[session_id]

    def conversations(self, *, user: str | None = None, name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """The saved conversations, the latest first (all, or one person's:
        by user id or by name), each with whether a turn is running in it now."""
        rows = self.ws.store.agent_sessions(started_by_user=user, started_by=name, limit=limit)
        return [{**r, "running": r["id"] in self.running} for r in rows]

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        """What the UI showed for this conversation, for replay after a reload."""
        return list(self._load(session_id)[1])

    def instructions(self) -> str:
        """The system prompt: how exuvia works, this project's parameters and its
        notes. Also served at `GET /guide` for agents that drive exuvia over HTTP."""
        ws = self.ws
        specs = ws.project.params
        if specs:
            now = ws.resolve_params()
            lines = [
                f"- `{n}` ({s['type']}, default {s['default']!r}, currently {now.get(n)!r})"
                + (f": {s['description']}" if s["description"] else "")
                for n, s in specs.items()
            ]
            params = "This project declares:\n" + "\n".join(lines)
        else:
            params = "This project declares none yet."
        notes = ws.project.instructions()
        return (
            SYSTEM.replace("{packages}", _available_packages(ws.project.root))
            .replace("{packages_how}", _packages_how())
            .replace("{view_packages}", _view_packages(ws.project.root))
            .replace("{params}", params)
            .replace("{notes}", f"\n\n## Project notes\n{notes}" if notes else "")
        )

    async def _loop(
        self, provider: Provider, history: list[Any], message: str, selected: str | None,
        toolbox: Toolbox | None = None,
    ) -> AsyncIterator[dict[str, Any]]:

        context = ""
        if selected and selected in self.ws.graph.nodes:
            st = self.ws.status().get(selected, {})
            context = f"\n\n<ui_context>The user has node `{selected}` selected (state: {st.get('state')}"
            if st.get("reason") and st.get("state") in ("failed", "error"):
                context += f"; error: {st['reason']}"
            context += ").</ui_context>"
        provider.add_user(history, message + context)
        system = self.instructions()
        tools = TOOLS
        if toolbox is not None:
            system += toolbox.instructions()
            tools = [*TOOLS, *toolbox.tools]

        for _ in range(MAX_TURNS):
            turn = None
            async for ev in provider.turn(system, tools, history):
                if ev["type"] == "turn_end":
                    turn = ev["turn"]
                else:
                    yield ev
            if turn is None or turn.status in ("end", "fatal"):
                break
            if turn.status == "retry":
                continue

            results = []
            for call in turn.calls:
                args = call.args or {}
                yield {"type": "tool", "id": call.id, "name": call.name, "input": _ui_input(call.name, args)}
                if call.parse_error:
                    ok, text, extra = False, f"INVALID_INPUT: {call.parse_error}", {}
                elif toolbox is not None and call.name in toolbox:
                    ok, text = await toolbox.call(call.name, call.args)
                    extra = {"mcp": toolbox.server_of(call.name)}
                else:
                    ok, text, extra = await self.call_tool(call.name, call.args)
                yield {"type": "tool_result", "id": call.id, "name": call.name, "ok": ok, "summary": text[:400], **extra}
                results.append((call.id, text, not ok))
            provider.add_tool_results(history, results)

    def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self.transcripts.pop(session_id, None)
        self.ws.store.delete_agent_session(session_id)


def _record(transcript: list[dict[str, Any]], ev: dict[str, Any]) -> None:
    """Append a UI event to the saved transcript, merging streamed text."""
    kind = ev.get("type")
    if kind == "text":
        if transcript and transcript[-1].get("type") == "text":
            transcript[-1]["text"] += ev.get("text", "")
        else:
            transcript.append({"type": "text", "text": ev.get("text", "")})
    elif kind in ("tool", "tool_result", "error"):
        transcript.append(dict(ev))


def _ui_input(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "write_node":
        src = args.get("source") or ""
        return {"name": args.get("name"), "kind": args.get("kind"), "lines": src.count("\n") + 1}
    if name == "write_file":
        src = args.get("source") or ""
        return {"path": args.get("path"), "lines": src.count("\n") + 1}
    if name == "write_view_file":
        src = args.get("source") or ""
        return {"name": args.get("name"), "path": args.get("path"), "lines": src.count("\n") + 1}
    return args
