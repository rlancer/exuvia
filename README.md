# exuvia

**An AI-first DAG workbench for analysts and data scientists.**

Marimo showed that notebooks work better as a reactive graph than as a list of cells.
exuvia takes the next step: the graph *is* the interface. Each node is a plain
file (Python or SQL) that can be scheduled on its own, every output is cached
and content-addressed, and an AI agent (Claude, DeepSeek, or any OpenRouter
model) can read, write, run, and debug nodes alongside you on a live canvas.

*Exuvia* (n.): the shell a lobster leaves behind when it molts. Every run
leaves one too: an immutable, content-addressed output you can go back to.

```
universe ──► prices (cron 17:00 weekdays) ──► returns ──► rolling_vol ──► sector_summary.sql
    └──────────────────────────────────────────────────────────────────────────┘
```

> Everything here runs from the published package (PyPI) and image (GHCR),
> with no clone. The exceptions are the commands that open `examples/` and
> [Contributing](#contributing), which is about working on exuvia itself.

## Why

| | notebooks | orchestrators (Airflow, Dagster) | **exuvia** |
|---|---|---|---|
| Unit of work | cell | task / asset | **node file** |
| Dependencies | hidden state / reactive cells | declared in code | **function parameters & SQL table names** |
| Scheduling | none | whole DAG / asset jobs | **per node, cron, cascades downstream** |
| Iteration loop | fast | slow (deploy) | **fast — edit, run, see data** |
| AI | sidebar chat | none | **agent with tools over the graph** |
| Custom UI | widgets and HTML built from Python | none (a separate app) | **views: TypeScript, React and CSS in files of their own** |
| Storage | `.ipynb` JSON | Python package | **one plain file per node, git-friendly** |

exuvia is also a place to build analytics with AI, and the two usual ways of
doing that each give something up. A chat with data connectors (MCP servers
over your warehouse, say) can only assemble what fits in a conversation, and a
good answer cannot be run again. A coding agent can build a far stronger
analysis, but each one becomes an app of its own, with a backend that loads
the data and a front end that shows it, and nothing kept of what it said last
week.

| | AI chat with connectors | a coding agent on its own | **exuvia** |
|---|---|---|---|
| How the answer is made | tool calls, and whatever fits in one conversation | code the agent writes and runs | **code the agent writes and runs, as nodes in a graph** |
| Running it again | ask again and get a different answer | rerun whatever scripts it left, in the right order | **run any node, by hand or on a schedule; what it depends on runs first** |
| Last week's numbers | somewhere in a transcript | overwritten, unless you built storage for them | **kept: one output per run date, and a table of every run to query** |
| The interface | a reply in the chat | a dashboard app per analysis: backend and front end | **a view: front-end files that read stored outputs, with no backend to write** |
| What you are left with | a conversation | one more app to host and keep running | **plain files in one project, with every version kept** |

You keep the agent you already use: exuvia's tools are an MCP server (see
[MCP](#mcp-the-agents-tools-in-your-own-agent)), so Claude Code, Cursor or
Codex builds in the graph instead of in an empty repo.

## Quick start

With [uv](https://docs.astral.sh/uv/) there is nothing to install first:
`uvx` downloads exuvia into its cache and runs it.

```bash
uvx exuvia init my-research     # a project folder with a first node
uvx exuvia serve my-research
# → http://127.0.0.1:7420
```

To have an `exuvia` command on your PATH, install it once. Any of these
works; the first two give exuvia an environment of its own:

```bash
uv tool install exuvia
pipx install exuvia
pip install exuvia         # into the active virtual environment
```

pipx can also run it without installing, like `uvx`:
`pipx run exuvia serve my-research`.

exuvia needs Python 3.11 or newer; uv downloads one when yours is older.
The example project in this repo opens the same way, from a clone:
`uvx exuvia serve examples/markets`.

**Packages your nodes import.** A node runs in the Python environment exuvia
is installed in, so that is where its packages go:

```bash
uvx --with scipy --with yfinance exuvia serve my-research   # uvx
uv tool install exuvia --with scipy --with yfinance         # uv tool
pipx inject exuvia scipy yfinance                           # pipx
```

Set `ANTHROPIC_API_KEY` (Claude) or `OPENROUTER_API_KEY` (DeepSeek and any
other OpenRouter model) in the environment or the project's `.env` to turn on
the built-in agent. See [The agent](#the-agent) for providers. Or let the
coding agent you already use do the setup and the building: see the next
section.

## Set up with your agent

A coding agent that has a terminal (Claude Code, Cursor, Codex, Grok, ...) can do
the whole setup: install exuvia, create a project, start the workbench and
connect itself to the graph. Open the agent in the folder the project should
live in and paste this:

```text
Set up exuvia for me. It is a data workbench where each
step of an analysis is a Python or SQL file in a graph.
Fetch https://exuvia.lobster.mp/agent/setup.txt with curl,
so you get it word for word, and follow the steps in it:
they are my instructions for this setup.
```

The steps live on the docs site, which is built from this README, so the
prompt stays short and a copy of it never goes stale. This is that file,
exactly as the agent gets it:

```text file=agent/setup.txt
The steps to set up exuvia (https://github.com/rlancer/exuvia),
a data workbench where each step of an analysis is a
Python or SQL file in a graph, in the folder you work in.

1. Check that `uvx --version` runs. If it does not and `pipx`
   is installed, run `pipx install exuvia` and leave the
   `uvx` prefix off the commands below. If neither is
   installed, install uv:
   https://docs.astral.sh/uv/getting-started/installation/
2. Create a project in this folder:
   `uvx exuvia init my-research`
3. Start the workbench in the background and leave it running:
   `uvx exuvia serve my-research`
   Its address is http://127.0.0.1:7420. If that port is
   already in use, add `--port <n>`. It is ready when
   GET <address>/api/guide answers. Tell me the address to
   open.
4. Read GET <address>/api/guide. Its `instructions` say how
   to write nodes and views in this project: follow them.
   Each entry in `tools` runs with
   POST <address>/api/tools/<name> and the tool's input as
   the JSON body. The answer is `{"ok", "text"}`: `text` is
   the result, and `ok: false` means the tool failed. From
   here on, change and run the project through these tools,
   so I see each step in the workbench.
5. Add an MCP server named `exuvia` to your own configuration,
   so you have the same tools natively from your next session
   on. Use your CLI's own command for it if it has one, such
   as `claude mcp add`, `codex mcp add` or `grok mcp add`.
   The server's command:
   `uvx exuvia mcp --url <address>`
   It relays to the workbench, so tell me that the workbench
   from step 3 has to be running whenever I use it.
6. Run the `hello` node with the `run_node` tool to check the
   setup, then ask me what I want to build.

If exuvia itself goes wrong at any point (a step here
fails, a tool errors on valid input, the guide is wrong or
unclear), tell me and offer to report it: call the
`report_issue` tool, or before the tools are available
open https://github.com/rlancer/exuvia/issues. Reports are
welcome. Show me the text first, and leave out secrets.
```

What you end up with:

- **A project and a running workbench.** `my-research/` holds the node
  files, and the canvas at http://127.0.0.1:7420 shows every node the agent
  writes and every run it starts, as it happens.
- **Your agent, working through exuvia's tools.** It reads the same
  instructions the built-in agent gets and calls the same tools, over HTTP
  for now (`GET /api/guide`, `POST /api/tools/{name}`) and over MCP from its
  next session. exuvia needs no model key for this: the model is the one
  your agent already runs on.
- **An MCP server that relays to the workbench.** In later sessions start
  `exuvia serve my-research` before the agent, or the `exuvia` MCP server
  has nothing to connect to: its tools then answer with how to start the
  workbench, so the agent can do it or ask you to.
  [MCP](#mcp-the-agents-tools-in-your-own-agent) has the other mode, which
  opens the project without a running server.

To put exuvia on a server instead of your machine, the prompt under
[Hand it to an agent](#hand-it-to-an-agent) deploys a hub with Docker
Compose, Swarm or Kubernetes.

## Nodes

One file in `nodes/` = one node. The file name is the node name.

**Python** — parameters are upstream nodes; return a DataFrame (or JSON-able value):

```python
"""20-day annualized realized volatility per ticker."""
import exuvia

@exuvia.node(
    schedule="0 18 * * 1-5",          # optional: re-run on this cron (local time)
    cascade=True,                      # re-run when upstream output changes (default)
    chart={"mark": "line", "encoding": {...}},   # optional Vega-Lite spec, no `data`
)
def rolling_vol(returns):
    out = returns.sort_values("date").copy()
    out["vol_20d"] = out.groupby("ticker")["ret"].transform(lambda s: s.rolling(20).std() * 252**0.5)
    return out.dropna()
```

**SQL** (DuckDB) — upstream nodes are tables:

```sql
-- Latest vol by sector.
-- schedule: */30 9-16 * * 1-5
-- chart: {"mark": "bar", "encoding": {...}}
select u.sector, avg(v.vol_20d) as avg_vol
from universe u join rolling_vol v using (ticker)
group by 1
```

The graph is read **statically** (`ast` for Python, identifier matching for
SQL), so a broken node still shows up in the graph with its error, and nothing
runs until you ask.

**Shared helpers** — code several nodes need lives in Python modules under
`lib/` (`lib/stats.py`, `lib/vol/surface.py`; no `__init__.py` needed), and
nodes import it with plain import statements:

```python
@exuvia.node()
def basket_corr(returns):
    from lib.stats import weighted_corr
    return weighted_corr(returns)
```

A node's code hash covers every `lib/` file it imports, directly or through
other helpers, so editing a helper marks exactly those nodes stale. Only
static `import` / `from … import` statements are tracked. Besides `nodes/`,
`lib/**/*.py` and view folders (see "Views") are the only places the API and
the agent can write.

## Run parameters and replays

A project can declare **run parameters** in `exuvia.json`. A node that reads
one says so, and its output is then stored once per value:

```json
{
  "name": "put-selling",
  "timezone": "America/New_York",
  "params": { "as_of": { "type": "date", "default": "today", "description": "Run date" } },
  "schedules": { "screen": "30 16 * * 1-5" }
}
```

```python
@exuvia.node(params=["as_of"])
def screen():
    day = exuvia.param("as_of")        # "2026-11-06": dates arrive as ISO strings
    ...
```

```sql
-- params: as_of
select * from screen where as_of_date <= $as_of
```

- Types are `date`, `string`, `number` and `boolean`. A `date` default of
  `today` resolves to the run date in the project `timezone`.
- **One output per parameter set.** Running for `as_of=2026-11-06` does not
  overwrite today's output; running it again is a cache hit. A node that
  declares no parameter and reads none upstream is computed once and shared.
- **Replays.** `POST /api/backfill` runs the graph for each value in a list or
  date range (weekdays only by default), skipping values that are already
  fresh. Progress is on `GET /api/backfill` and the event stream.
- **Time series for free.** In `query_data` and `POST /api/query`,
  `<node>__runs` unions a node's output across every parameter set that has
  run, with a `_<param>` column each (`select _as_of, count(*) from screen__runs group by 1`).
- `timezone` also sets the clock for cron schedules. `schedules` overrides or
  adds a per-node cron without editing the node file (this is what the
  schedule API writes), so an embedding app can let people schedule nodes.
  `failure_webhook` tells Slack or any URL when a scheduled run fails (see
  [Failure notifications](#schedules-auth-and-settings)).

```bash
exuvia run my-project --param as_of=2026-11-06
exuvia status my-project --param as_of=2026-11-06
```

## Views

A **view** is custom UI over node outputs, kept apart from the graph. In a
notebook the UI lives in each cell and is built from Python: widget calls, or
HTML and JavaScript held in strings. Here the nodes compute outputs in Python
and SQL, and a view is front-end code that only reads them: TypeScript, React
and CSS in files of their own, with components, imports and types. The
canvas, inspector and SQL box stay as they are.

How a view works, in short ("Rendering a view" below has the detail):

1. Nodes run and store their outputs. A view never runs anything.
2. The view's `view.json` names the nodes it reads. Those are the only
   outputs it can reach, and they are its edges on the graph.
3. The workbench loads the view's files into a sandboxed frame, compiles them
   in the browser (there is no build step and no `npm install`) and renders
   the entry file's default export.
4. The code asks for data through the `exuvia` module: `useTable("node")` for
   a whole output, `useQuery("select …")` for DuckDB SQL over the nodes it
   reads. Tables arrive as typed Arrow data, not as HTML or JSON text.
5. When a run replaces an output the view reads, its hooks reload. When one
   of its files is saved, the frame is rebuilt.

The agent writes views with the same kind of tools it has for nodes. Whoever
wrote it, a view runs in the sandbox and can only read.

Views are React, rendered inside the workbench. An app written in anything
else reads the same outputs over HTTP, as Arrow streams or Parquet files (see
[Binary data](#binary-data-arrow-and-parquet)).

A view is a folder `views/<name>/` next to `nodes/` and `lib/`, holding a
`view.json` manifest and its source files:

```
views/vol_board/
  view.json
  App.tsx
  components/Chart.tsx
  styles.css
```

```json
{
  "title": "Vol board",
  "description": "Realized vol per ticker, as of the selected date",
  "runtime": "react",
  "entry": "App.tsx",
  "reads": ["rolling_vol", "universe"]
}
```

- **`reads` is the whole of what a view can see**: the outputs of those nodes,
  nothing else. Like a node's upstreams it gives the graph edges, a state, and
  the list of outputs a view depends on.
- **Rendering never runs compute.** A view reads outputs that exist; it does
  not trigger runs. Work a view needs belongs in a node it reads.
- `runtime` is `react` (the default and, for now, the only one) and `entry`
  is the component file (`App.tsx` by default; `.tsx`, `.ts`, `.jsx` or
  `.js`). `title` defaults to the folder name.
- View names follow the node-name rule (snake_case). Files are text with one
  of `.tsx .ts .jsx .js .css .json .html .md .svg`, at most 512 KB each, in
  plain relative paths (up to 8 levels); other files in the folder are not
  part of the view and `node_modules/` is ignored.
- Views are read **statically**, like nodes: a missing or broken `view.json`,
  an unknown key, a missing entry file or a `reads` entry naming a node that
  does not exist still lists the view, with a `parse_error`.
- Every file of a view is **versioned** like node files (see "Execution
  model"), so any version can be restored and a deleted view brought back.
  The file watcher picks up edits made on disk.
- Views are not shipped to a remote executor: no node can read them, so
  editing a view never ships a new project bundle.

`GET /api/project` (and `GET /api/views`) list each view with its `state` for
the requested parameter set, derived from the nodes it reads:

| `state` | |
|---|---|
| `ready` | every read node has a fresh output for this parameter set |
| `missing` | a read node has no output for it yet (`reason` names them) |
| `stale` | every read node has an output, but one is out of date or being refreshed (`reason` names them and their state) |
| `error` | the view itself is broken; `reason` is its `parse_error` |

Each view also carries `outputs`: per read node, its state, `kind` and the
`artifact_id` of the output the view would show now. Artifact ids name their
content, so a client can fetch `/api/artifacts/{node}/{artifact_id}.parquet`
once and cache it for good. `hash` covers every file of the view, so a
client can tell when its code changed.

### Rendering a view

The UI's **Views** mode (next to the graph) lists the views with their state
and reason, shows the nodes each one reads (click one to open it on the
canvas), lets you pick the run parameters, and renders the selected view.
Compile and runtime errors from the view are listed under it with the file
they came from. `examples/markets` ships one (`views/vol_board`).

A view runs in an `<iframe sandbox="allow-scripts">` built from a `srcdoc`:
an opaque origin, so no cookies, no API token and no access to the page. The
frame fetches nothing at all: its Content-Security-Policy names no URL
(`default-src 'none'`, `connect-src 'none'`, images and fonts only from
`data:`/`blob:`) and lets only its own bootstrap script run (by nonce), plus
code that script evaluates. The page fetches the runtime once and posts its
source to the bootstrap. (Chromium does not let an opaque-origin frame load
scripts from a local server anyway, so `<script src>` from the frame would
fail for `exuvia serve` on localhost.) The view's only way to data is asking
the page, and the page answers only for the nodes in `reads`.

The runtime (`ui/src/views/runtime.tsx`, built to
`exuvia/static/view-runtime/runtime.js` and served at
`GET /api/view-runtime/runtime.js`) is bundled with exuvia; nothing is
fetched from a CDN, so views work where exuvia has no internet. It compiles
each file in the browser with [Sucrase](https://github.com/alangpierce/sucrase)
(TypeScript + JSX to CommonJS) the first time it is imported, then renders the
entry file's `export default` with React 18. A view can import:

- its own files by relative path, with or without the extension (`./Chart`,
  `../lib/format.ts`; `./dir` finds `dir/index.tsx`), never outside its folder;
- `.css` files (added to the page), `.json` (the parsed value), `.svg` (a
  `data:` URL);
- by bare name, only what the runtime ships: `react` (+ `react/jsx-runtime`),
  `react-dom` (+ `/client`), `apache-arrow`, `vega-embed` (Vega-Lite charts)
  and `exuvia`. Anything else is a compile error naming the file: there is no
  package install and no network.

`exuvia` is the view's API:

```tsx
import { rows, useParams, useQuery, useTable } from "exuvia";

export default function App() {
  const vol = useTable("rolling_vol");              // {data, error, loading}; data is an Arrow Table
  const top = useQuery("select * from sector_summary order by avg_vol desc limit 3");
  const [params, setParams, declared] = useParams();
  if (vol.error) return <p>{vol.error}</p>;
  return <pre>{JSON.stringify(rows(top.data), null, 2)}</pre>;
}
```

| | |
|---|---|
| `useTable(node)`, `table(node)` | a read node's current table output, as an Arrow `Table` |
| `useJson(node)`, `json(node)` | a read node's current JSON output |
| `useQuery(sql)`, `query(sql)` | DuckDB SQL that sees only the read nodes (`<node>` and `<node>__runs`), as an Arrow `Table` |
| `useParams()`, `getParams()`, `setParams(values)` | the run parameters the view shows; setting them changes which outputs it reads, never runs anything |
| `onChange(fn)` | called with the nodes whose output was replaced |
| `rows(table)` | Arrow rows as plain objects (64-bit integers become numbers when they fit) |
| `useView()`, `useDark()` | the view's name, title and reads; whether the page is in dark mode |

The hooks reload by themselves when an output they read is replaced (a run
finished, or the parameters changed) and keep the previous data while
reloading. The page's CSS variables (`--bg`, `--panel`, `--line`, `--text`,
`--muted`, `--accent`, `--fresh`, `--failed`, `--mono`, `--sans`) are set in
the frame and follow light and dark mode.

**The protocol** between the frame and the page is `postMessage`, defined in
one typed module, `ui/src/views/protocol.ts`. Every message carries
`channel: "exuvia-view/1"`, and each side also checks the sender (the frame's
origin is `"null"`, so the origin says nothing):

| direction | message | |
|---|---|---|
| frame → page | `boot` | the bootstrap is listening |
| page → frame | `runtime {source}` | the runtime script, evaluated by the bootstrap |
| frame → page | `ready` | the runtime loaded |
| page → frame | `load {view, params, dark}` | the view's files (`{path: source}`), entry and reads, and the parameter set |
| frame → page | `request {id, method, args}` | `table {node}`, `json {node}`, `query {sql}`, `params {}`, `setParams {params}` |
| page → frame | `response {id, ok, result \| error}` | Arrow IPC bytes for `table`/`query` (transferred, not copied), the value for `json`, `{params, specs}` for the parameter calls |
| page → frame | `changed {nodes, params}` | outputs the view reads were replaced; driven by `/api/events` |
| page → frame | `theme {dark}` | |
| frame → page | `status {phase}` | `compiled`, `rendered`, or `compile`/`runtime` with `error`, `file`, `stack` |

The page refuses `table`/`json` for a node not in `reads` and sends every
request to the view-scoped routes below, which refuse it again on the server.
It caches table answers by `artifact_id` (an id names its content). When the
view's files change (its `hash`), the frame is rebuilt.

**View-scoped data routes.** `GET /api/views/{name}/data/{node}` is
`/api/nodes/{node}/data` (JSON or Arrow, `?params=`) that answers 403 unless
the view reads `node`. `POST /api/views/{name}/query` is `/api/query` over a
DuckDB connection that holds only the views of the nodes in `reads` (and
their `__runs`) and is then locked: external access is off except for those
exact output files, extensions cannot load, and the configuration cannot be
changed back, so the SQL cannot `read_parquet` another node's file, `COPY`
out, `ATTACH` a file or `INSTALL` anything.

## Execution model

- **Content-addressed outputs.** Each run writes its result to
  `.exuvia/artifacts/<node>/<sha>.parquet` (or `.json`), keyed by the
  node's parameter set (see "Run parameters and replays"). Paths are stored
  relative to the project, so a project directory can be moved or restored
  elsewhere with its outputs intact.
- **Outputs in object storage (optional).** Set `EXUVIA_ARTIFACT_STORE` to an
  S3 URL and the bucket becomes the durable copy of every output (AWS S3,
  Cloudflare R2, MinIO, anything S3-compatible). Each output is uploaded when
  its run finishes; the local `.exuvia/artifacts/` folder becomes a cache that
  a fresh process refills on first read, and hub exports leave uploaded
  outputs out. Outputs recorded before the store was set (or whose upload
  failed) are uploaded in the background when the project loads, and stay in
  exports until they are. Deleting a project deletes its objects.
- **Freshness is derived, not flagged.** A node is *fresh* when its code hash
  matches its last successful run and each upstream's current artifact is the
  one that run consumed. Otherwise it is *stale*, and the reason says why.
- **Run = target + stale ancestors**, in dependency order, in parallel where
  the graph allows (`EXUVIA_MAX_PARALLEL`, default 4).
- **Subprocess isolation.** Every node runs in its own Python process: crash
  isolation, clean per-run logs streamed live to the UI, killable timeouts
  (`EXUVIA_RUN_TIMEOUT`, default 1800 s).
- **Per-node schedules + cascade.** A scheduled node is forced to re-run on its
  cron. If it produces *different* output, downstream nodes with
  `cascade=True` refresh automatically; identical output stops the cascade.
  Schedules can be paused from the UI without editing code.
- **Run history** lives in a database: status, trigger (manual / schedule /
  cascade / agent / cli), timings, logs, schema. Schedule state (last fire
  per node) is persisted too, so restarts neither drop nor double-fire
  schedules. By default that is `.exuvia/exuvia.db` (SQLite) inside the
  project; see "Records: SQLite, Postgres, or your own adapter".
- **Every version of the code is kept.** Each time a node file, a `lib/` helper,
  a view file, `exuvia.json` or `AGENTS.md` changes, by any route (UI, API,
  agent, an editor, a template), the new text is saved as a revision with who
  saved it (`agent`, `api` or the caller's `X-Exuvia-Origin`, `file`,
  `template`, `import`, `restore`, `load`). A deletion is a revision too.
  Revisions are keyed by the same hash each run records, so every output can
  show the exact code that produced it, and any version can be restored (a
  restore is itself a revision). The agent has `list_revisions`,
  `read_revision`, `restore_node` and `restore_view_file`.

## Records: SQLite, Postgres, or your own adapter

Run history, schedules, agent sessions and code revisions are small rows; node
outputs are files (above). The rows go wherever `EXUVIA_DB` points:

| `EXUVIA_DB` | |
|---|---|
| unset | SQLite at `.exuvia/exuvia.db` in each project. Nothing to set up; a hub export carries it. |
| `sqlite:///<path>` | one SQLite file for every project the process serves |
| `postgresql://…` | Postgres (`pip install 'exuvia[postgres]'`) |

In a shared database every row carries its project's scope,
`EXUVIA_DB_SCOPE` + the project id, so several hubs (one per user, say) can
share one database by each setting their own `EXUVIA_DB_SCOPE`. A hub export
then leaves the records out, since they already live in the database, and
deleting a project deletes its rows. A project's identity there is its
directory name, so moving a standalone project to a differently named folder
starts a fresh history. One process at a time serves a given project.

The SQL is one dialect SQLite and Postgres both accept (`?` placeholders,
`on conflict` upserts, window functions). Another backend, such as an HTTP
SQL service like Cloudflare D1, is a class implementing `exuvia.db.Database`:
`query`, `transaction`, `columns`, `close`. A project database written by an
older exuvia is upgraded in place when it opens.

## Architecture: control plane + executors

```
            ┌──────────── control plane (exuvia serve) ────────────┐
 UI / API ──►  graph · freshness · scheduler · run history · agent │
            │  artifacts (.exuvia/)                                 │
            └───────────────┬──────────────────────────────────────┘
                            │ Executor interface
              ┌─────────────┴──────────────┐
     LocalExecutor (default)        RemoteExecutor ──HTTP──► exuvia executor
     warm subprocess pool                                   (stateless container)
```

The **control plane** is the only stateful part. **Executors** run one node
at a time and return a content-addressed artifact; they hold no project state.

- **Local (default):** a pool of pre-warmed, single-use subprocesses on the
  same machine.
- **Remote:** `exuvia executor` is a plain HTTP service — the same image runs
  on Cloudflare Containers, Fly, Cloud Run, Kubernetes, or a box under a desk.
  Point a control plane at it with `--executor URL` / `EXUVIA_EXECUTOR_URL`.
  Every request flows control plane → executor (project zip, upstream
  artifacts in; logs and artifact out), so a laptop can drive a cloud
  executor without being reachable from it. Project bundles are hash-verified
  and cached on the executor, so unchanged code is not re-sent.

Executor protocol (bearer `EXUVIA_EXECUTOR_TOKEN`):

| Route | |
|---|---|
| `HEAD/PUT /v1/bundles/{hash}` | is this project version cached / upload it (zip, verified against the hash) |
| `PUT /v1/runs/{id}/inputs/{dep}` | upload an upstream artifact |
| `POST /v1/runs/{id}` | run; NDJSON response: `{"type":"log"}` lines then one `{"type":"result"}` |
| `GET /v1/runs/{id}/output` | download the artifact (control plane re-verifies its hash) |
| `POST /v1/runs/{id}/cancel`, `DELETE /v1/runs/{id}` | cancel / clean up |
| `GET /healthz` | liveness |

## Deploying

Every release publishes the image to `ghcr.io/rlancer/exuvia` for
linux/amd64 and linux/arm64, tagged with its version (`0.3.0`), its minor
line (`0.3`) and `latest`. It is public: pull it, with no login and nothing
to build. The one image runs every role: `exuvia hub <dir>` (many projects,
each with its API and the web UI, see "Hub"), `exuvia serve <dir>` (one
project with the web UI) and an executor (its default command, see
"Architecture"). The UI is built into the image and served by the same
process, so there is no separate frontend to deploy.

### Which URL is which

`<url>` is the address the server answers at, e.g. `http://127.0.0.1:7420`.
Both commands include the workbench; neither needs a separate frontend.

| | `exuvia serve <dir>` (one project) | `exuvia hub <dir>` (many projects) |
|---|---|---|
| Open in a browser | `<url>/` | `<url>/`, the hub's home: create, open and delete projects; each one's workbench is at `<url>/p/<project>/` |
| First visit with `EXUVIA_API_TOKEN` set | `<url>/?token=<token>` | `<url>/?token=<token>`, or open `<url>/` and enter the token |
| Project API prefix | `<url>/api` | `<url>/p/<project>/api` |
| Server-wide API | none | `<url>/api` (`/api/projects`, `/api/schedule/tick`, ...) |
| `exuvia mcp --url` | `<url>` | `<url>` (every project), or `<url>/p/<project>` (just that one) |
| Read-only check | `GET <url>/api/project` | `GET <url>/p/<project>/api/project` |
| `GET <url>/healthz` answers | `"mode": "serve"` | `"mode": "hub"` |

`GET …/api/project` is the graph: every node with its state, and every
view. There is no `GET …/api/nodes` list; `/api/nodes/{name}` reads one
node's file.

### Compose, Swarm and Kubernetes

`deploy/` holds a ready hub for each: one service with a volume at `/data`,
a `/healthz` check and `EXUVIA_API_TOKEN` auth. CI brings each one up and
runs the end-to-end test against it. You need the one file, not the repo:

```bash
# Docker Compose or Podman Compose, on one host
curl -O https://raw.githubusercontent.com/rlancer/exuvia/main/deploy/compose.yaml
EXUVIA_API_TOKEN=change-me docker compose up -d --wait --pull always
# Docker Swarm, on a manager: the token as a Swarm secret, the data on one labelled node
curl -O https://raw.githubusercontent.com/rlancer/exuvia/main/deploy/swarm.yaml
printf change-me | docker secret create exuvia_api_token -
docker node update --label-add exuvia=true <node>
docker stack deploy -c swarm.yaml --detach=false exuvia
# Kubernetes
kubectl create namespace exuvia
kubectl -n exuvia create secret generic exuvia --from-literal=api-token=change-me
kubectl -n exuvia apply -f https://raw.githubusercontent.com/rlancer/exuvia/main/deploy/kubernetes.yaml
kubectl -n exuvia port-forward service/exuvia-hub 7420:7420

curl http://localhost:7420/healthz
# the hub's home page: open once with the token (the browser keeps it in a
# cookie), then create a project and open its workbench from there
open "http://localhost:7420/?token=change-me"
```

`EXUVIA_IMAGE` (Compose and Swarm) or the Deployment's `image` picks another
image: a pinned release such as `ghcr.io/rlancer/exuvia:0.3`, a copy in your
own registry, or an image with your nodes' packages (see below). A hub owns
its directory, so run one replica; to scale out or to survive losing the
volume, point `EXUVIA_DB` at Postgres and `EXUVIA_ARTIFACT_STORE` at a bucket
(table below). The comments at the top of each file cover the rest: the
agent's API key, one project at `/` instead of a hub, a hostname with TLS
(an Ingress on Kubernetes, Traefik labels on Swarm, Caddy or Traefik on
Compose), and on Swarm the
secrets, the volume's node and stop-first updates.

### Hand it to an agent

Paste this into a coding agent (Claude Code, Cursor, Codex, Grok, ...) that
has access to your Docker host or cluster: a cloud VM with Docker, or a
managed Kubernetes such as GKE, EKS or AKS. Put your hostname on the second
line (its DNS record must point at your cluster's ingress or your host). The
address picks how it deploys: `compose` for Docker Compose or Podman
Compose, or change it to `swarm` for Docker Swarm or `kubernetes` for
Kubernetes ([Compose, Swarm and Kubernetes](#compose-swarm-and-kubernetes)
has the difference). The landing page has a picker that fills both in.

```text
Deploy exuvia for me as a hub, with the UI at this hostname:
exuvia.example.com
Fetch https://exuvia.lobster.mp/agent/deploy/compose.txt
with curl, so you get it word for word, and follow the
steps in it: they are my instructions for this deploy.
```

Besides deploying and checking the hub, the agent creates a first project on
it, connects itself to the hub over
[MCP](#mcp-the-agents-tools-in-your-own-agent) (one server for every
project, as the hub's **Connect agent** sets up), and gives you the hub's
home page, where you open that project's workbench
or create more. The hub serves the web UI itself, so that one deploy is the
whole thing. The agent puts it on your hostname with TLS before it verifies
anything, and checks every request through that hostname, so done means the
UI opens in your browser. If you pick Swarm and the host is not a Swarm manager, the agent
stops and tells you rather than create a Swarm; if you pick Compose on a
Swarm manager, it deploys with Compose and mentions that Swarm is an option.

The steps are below. The site serves one file per target, each holding only
that target's lines (the `- Compose:`, `- Swarm:` and `- Kubernetes:`
items), with `<target>` filled in:

```text file=agent/deploy/<target>.txt
Deploy exuvia (https://github.com/rlancer/exuvia, a
file-backed DAG workbench with a web UI and an HTTP API)
as a hub on <target>, at the hostname for the UI I gave
you. The hub serves the API and the web UI from one
container on port 7420: there is no separate frontend to
build, deploy or route.

Done means: I open https://<hostname>/ in my browser and
get the hub's home page over TLS, and you are connected to
the hub over MCP. A healthy container is not enough: the
hostname has to route to it (step 5), and step 6 checks
everything through that hostname.

1. If I gave you no hostname, or the placeholder
   exuvia.example.com, ask me for mine before going on.
   Then check the host and download the deploy file
   (curl -O):
   - Compose: check that `docker compose version` (or
     `podman compose version`) runs, then download
     https://raw.githubusercontent.com/rlancer/exuvia/main/deploy/compose.yaml
     If `docker info --format '{{.Swarm.ControlAvailable}}'`
     prints `true`, the host is a Swarm manager: carry on
     with Compose, which works there, and tell me at the
     end that the Swarm deploy is an option.
   - Swarm: run `docker info --format
     '{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}'`.
     Anything but `active true` means the host is not a
     Swarm manager: stop and tell me, and don't create a
     Swarm for this (the Compose deploy fits a plain Docker
     host). Otherwise download
     https://raw.githubusercontent.com/rlancer/exuvia/main/deploy/swarm.yaml
   - Kubernetes: check that `kubectl` reaches the cluster
     (`kubectl version`), then download
     https://raw.githubusercontent.com/rlancer/exuvia/main/deploy/kubernetes.yaml
   Read the comments at the top of the file; they are the
   instructions.
2. Generate a random API token (e.g. `openssl rand -hex 32`)
   and store it without printing it or committing it:
   - Compose: EXUVIA_API_TOKEN in a .env next to
     compose.yaml that only its owner can read.
   - Swarm: pipe it into
     `docker secret create exuvia_api_token -`.
   - Kubernetes: `kubectl create namespace exuvia`, then
     `api-token` in a secret named `exuvia` in it.
   Also keep it on this machine in
   ~/.config/exuvia/<hostname>.token (the file the hub's
   Connect agent uses), readable only by me: `mkdir -p
   ~/.config/exuvia`, then write it with `umask 077`. The
   MCP server (step 7) and my browser need it.
3. Image: ghcr.io/rlancer/exuvia:latest, public, no login
   needed. If the hosts can't reach ghcr.io, copy it to a
   registry they can reach (docker pull, tag, push) and
   point the deploy at the copy:
   - Compose and Swarm: set EXUVIA_IMAGE.
   - Kubernetes: set the Deployment's image.
4. Bring it up and wait until it is healthy:
   - Compose: `docker compose -f compose.yaml up -d --wait
     --pull always` (so an older `latest` already on the
     host is not reused)
   - Swarm: label the node that keeps the data
     (`docker node update --label-add exuvia=true <node>`),
     then `docker stack deploy -c swarm.yaml --detach=false
     exuvia`
   - Kubernetes: `kubectl -n exuvia apply -f kubernetes.yaml`,
     then `kubectl -n exuvia rollout status
     deployment/exuvia-hub`
   Keep one replica (the hub owns its volume). If outputs
   go to an existing bucket (EXUVIA_ARTIFACT_STORE), use a
   prefix only this hub writes to, e.g.
   s3://<bucket>/exuvia-prod.
5. Route the hostname to the hub, with TLS. This is how I
   reach the UI, so don't skip it or leave it for later:
   - Compose: set EXUVIA_HOST=<hostname> in the .env. If
     nothing listens on ports 80 and 443, uncomment the
     `caddy` service and the `caddy-data` volume (Caddy
     gets the certificate itself). If a Traefik runs on the
     host, uncomment the hub's Traefik labels and the
     `proxy` network instead, with the entrypoint, cert
     resolver and network names that Traefik uses (`docker
     inspect` on its container shows them). Then bind the
     hub's port to 127.0.0.1 and run step 4's command again.
   - Swarm: set EXUVIA_HOST=<hostname>, uncomment the
     Traefik labels and the Traefik network in swarm.yaml,
     with the network, entrypoint and cert resolver names
     of the swarm's Traefik (`docker service inspect` on
     it), delete the `ports` block and deploy again. No
     Traefik on the swarm: stop and tell me.
   - Kubernetes: uncomment the Ingress at the bottom of
     kubernetes.yaml, put the hostname in `tls` and
     `rules`, set `ingressClassName` and the annotations
     for the cluster's ingress controller (`kubectl get
     ingressclass`; cert-manager's issuer if it has one)
     and apply again. No ingress controller: stop and tell
     me.
   The comments at the top of the file say more. The UI is
   not a separate service or path: route the whole host
   (every path: /, /p/..., /api/..., /assets/...) to the
   hub. Point the hostname's DNS at the proxy or the
   ingress controller's external address (tell me the
   record to add if you can't), then wait until
   https://<hostname>/healthz answers with a valid
   certificate. From then on <hub url> is
   https://<hostname>. If I tell you I have no hostname,
   keep the hub private instead (port-forward, or an SSH
   tunnel to localhost:7420), use that address as <hub url>
   and say so in the report: never expose it without TLS.
6. Verify through <hub url>, not localhost or a
   port-forward (unless I told you I have no hostname):
   GET /healthz returns {"ok": true, "mode": "hub"}.
   GET /api/projects without the token returns 401; if it
   doesn't, the hub has no auth: take it down and stop.
   With `Authorization: Bearer <token>`:
   POST /api/projects {"id": "smoke"},
   PUT /p/smoke/api/nodes/hello
     {"source": "select 42 as answer", "kind": "sql"},
   POST /p/smoke/api/run {"wait": 60} returns "ok": true,
   GET /p/smoke/api/nodes/hello/data returns the column
   `answer` and rows [[42]]; GET /p/smoke/ returns the
   workbench page (HTML) and GET / the hub's home page;
   then DELETE /api/projects/smoke. A request that works
   directly but fails through the hostname means the route
   from step 5 is wrong: fix it before going on.
   If /p/smoke/ is a 404, the image predates the built-in
   UI (/healthz "version" below 0.3.5): pull
   ghcr.io/rlancer/exuvia:latest again and redeploy. Don't
   build or deploy a separate frontend.
7. Create a project for me:
   POST /api/projects {"id": "my-research"}.
   Then add an MCP server named `exuvia` to your own
   configuration, so you have exuvia's tools natively from
   your next session on, for every project on the hub. Use
   your CLI's own command for it if it has one, such as
   `claude mcp add` (with --scope user), `codex mcp add`
   or `grok mcp add`. The server's command:
   `uvx exuvia@latest mcp --url <hub url>`
   (`pipx run exuvia mcp ...` without uv; @latest because
   an older exuvia installed here can't serve a whole hub),
   with the environment variable EXUVIA_API_TOKEN_FILE set
   to the file from step 2, so the token itself stays
   out of the MCP configuration. <hub url> is the address
   this machine reaches the hub at. Check that its
   list_projects tool shows my-research.
8. Report the hub URL (https://<hostname>), where the token is stored, the
   verification output, the MCP server you added, and the
   hub's home page to open in a
   browser: <hub url>/ (it lists my-research, creates and
   deletes projects, opens each one's workbench, and under
   Connect agent sets up MCP for any other agent). It
   asks for the token once and the browser keeps it in a
   cookie; tell me to paste it from the file from step 2
   rather than printing it. Also tell me that if the hub
   is only reachable on a private network, setting
   EXUVIA_BROWSER_AUTH=open on it lets browsers in without
   the token (agents and the API still need it).
```

### One container with docker run

```bash
# one project with the web UI, from a folder on this machine
docker run -p 7420:7420 --user "$(id -u):$(id -g)" -v "$PWD/my-research:/project" \
  ghcr.io/rlancer/exuvia exuvia serve /project --host 0.0.0.0
# an executor: the control plane stays on your machine, nodes run in the container
docker run -p 8080:8080 -e EXUVIA_EXECUTOR_TOKEN=change-me ghcr.io/rlancer/exuvia
EXUVIA_EXECUTOR_TOKEN=change-me exuvia serve my-research --executor http://localhost:8080
```

`--user` runs the container as you, so the files it writes into
`my-research` are yours. Without it the image runs as its own `exuvia` user
(uid 10001).

### Packages your nodes import

Nodes run in the image's Python environment, so their packages go into the
image. Put this in a `Dockerfile` of your own:

```dockerfile
FROM ghcr.io/rlancer/exuvia:0.3
USER root
RUN uv pip install --python /opt/venv/bin/python scipy statsmodels
USER exuvia
```

`docker build -t my-exuvia .` builds it. Use it in place of the published
image: in `docker run`, or as `EXUVIA_IMAGE` or the Deployment's `image`
(Swarm and Kubernetes pull it from a registry, so push it to one first).
With a remote executor, the executor is the one that needs the packages.

### Schedules, auth and settings

**Hosts that sleep or scale to zero.** An in-process scheduler can't fire while
its process is asleep. Run the control plane with `--scheduler external` and
have something call `POST /api/schedule/tick` every minute or so: a Cloudflare
Durable Object alarm or Cron Trigger, a Kubernetes CronJob, or system cron.
Ticks are idempotent; fires missed while asleep are coalesced into one
catch-up run, and paused schedules skip their backlog.

**Failure notifications.** Give a project a webhook and exuvia POSTs JSON to
it when a scheduled or cascade run fails (manual runs have someone watching
them; skipped downstream nodes are not reported, only the node that broke):

```json
{ "failure_webhook": "https://hooks.slack.com/services/T000/B000/XXXX" }
```

```json
{ "failure_webhook": { "url": "https://example.com/hooks/exuvia", "triggers": "all" } }
```

`triggers` is `"all"` or a list of `schedule`, `cascade`, `manual`,
`backfill` (default `["schedule", "cascade"]`). `EXUVIA_FAILURE_WEBHOOK` (and
`EXUVIA_FAILURE_TRIGGERS`, comma-separated) set one for every project on a
host or hub; a project's own `failure_webhook` wins, and `false` turns it off
for that project. The webhook URL is often a secret, and `exuvia.json` is kept
in code revisions and usually committed, so prefer the env var for those.

```json
{
  "text": "exuvia: put-selling / screen failed (schedule): ValueError: feed is empty\nhttps://exuvia.example.com/api/runs/3f9c2a1b7d4e",
  "event": "run.failed",
  "project": "put-selling",
  "node": "screen",
  "run_id": "3f9c2a1b7d4e",
  "trigger": "schedule",
  "status": "failed",
  "error": "Traceback (most recent call last): … ValueError: feed is empty",
  "params": { "as_of": "2026-11-06" },
  "queued_at": "2026-11-06T21:30:00.412+00:00",
  "finished_at": "2026-11-06T21:30:04.981+00:00",
  "url": "https://exuvia.example.com/api/runs/3f9c2a1b7d4e"
}
```

`text` makes it a Slack incoming-webhook message as it is (for Discord, add
`/slack` to the webhook URL); the other fields are for your own receiver.
`error` is trimmed to 2000 characters. `url` is the run's record (error and
log) in the API, `/p/<project>/api/runs/<id>` in a hub, and is `null` unless
`EXUVIA_PUBLIC_URL` says where the server is reachable. Delivery is one POST
off the run path with a 5 s timeout; a failed delivery is printed to stderr
and not retried. Runs marked failed because the server restarted mid-run are
not sent.

**Auth.** Set `EXUVIA_API_TOKEN` on a hosted control plane: API clients send
`Authorization: Bearer <token>`; browsers open `/?token=<token>` once and get
an HttpOnly cookie. On a hub that only a private network can reach, set
`EXUVIA_BROWSER_AUTH=open` so people never handle the token: every page load
hands the browser that cookie (`SameSite=Lax`, so other websites can't send
requests with it), while API calls without the cookie or the token still get
a 401, so agents and MCP clients keep using the token. It lets in anyone who
can load the page, so use it only behind a VPN or an internal-only address. Always set `EXUVIA_EXECUTOR_TOKEN` on a networked executor
— it runs arbitrary code by design.

**Secrets from files.** Each setting that holds a secret can come from a file
instead: set `<NAME>_FILE` to its path, such as
`EXUVIA_API_TOKEN_FILE=/run/secrets/exuvia_api_token` for a Docker or Swarm
secret, or a file in a Kubernetes secret volume. Surrounding whitespace is
dropped. This works for `EXUVIA_API_TOKEN`, `EXUVIA_EXECUTOR_TOKEN`,
`EXUVIA_DB`, `EXUVIA_FAILURE_WEBHOOK`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY` and the
`AWS_*` keys. Setting both forms to different values, or naming a missing or
empty file, stops exuvia with an error.

| Env | Default | |
|---|---|---|
| `EXUVIA_EXECUTOR_URL` / `EXUVIA_EXECUTOR_TOKEN` | local pool | remote executor + its token |
| `EXUVIA_SCHEDULER` | `internal` | `external` = only `POST /api/schedule/tick` advances schedules |
| `EXUVIA_API_TOKEN` | off | shared-secret auth for the control plane API |
| `EXUVIA_BROWSER_AUTH` | `token` | `open` = every page load gives the browser the auth cookie, so people never enter the token (private networks only; see "Auth" above) |
| `<NAME>_FILE` | — | read a secret setting from a file, e.g. `EXUVIA_API_TOKEN_FILE=/run/secrets/exuvia_api_token` (see "Secrets from files" above) |
| `EXUVIA_FAILURE_WEBHOOK` | off | URL that failed runs are POSTed to, for projects without their own `failure_webhook` |
| `EXUVIA_FAILURE_TRIGGERS` | `schedule,cascade` | which failed runs the env webhook reports (`all`, or any of `schedule`, `cascade`, `manual`, `backfill`) |
| `EXUVIA_PUBLIC_URL` | — | where this server is reachable (e.g. `https://exuvia.example.com`), for run links in failure webhooks |
| `EXUVIA_MAX_PARALLEL` | 4 | concurrent node runs (control plane and executor) |
| `EXUVIA_RUN_TIMEOUT` | 1800 | seconds before a node run is killed |
| `EXUVIA_BUNDLE_MAX_MB` | 50 | max project size shipped to a remote executor |
| `EXUVIA_EXECUTOR_DIR` | temp | executor scratch space |
| `EXUVIA_PROVIDER` | auto | `anthropic`, `openrouter`, or `openai` (any OpenAI-compatible server) |
| `EXUVIA_MODEL` | per provider | agent model (see below) |
| `EXUVIA_EFFORT` | `medium` | Claude effort level |
| `EXUVIA_BASE_URL` | OpenRouter | base URL for the `openai` provider (Ollama, vLLM, LiteLLM, …) |
| `EXUVIA_DB` | per-project SQLite | where run history, schedules, sessions and code revisions live: `sqlite:///<path>` or `postgresql://…` |
| `EXUVIA_DB_SCOPE` | — | prefix for project scopes in a shared `EXUVIA_DB` (e.g. a user id) |
| `EXUVIA_ARTIFACT_STORE` | local disk | `s3://bucket/prefix`: keep node outputs in an S3-compatible bucket, keyed `<prefix>/<project>/<node>/<sha>.<ext>` |
| `EXUVIA_S3_ENDPOINT` | AWS | e.g. `https://<account>.r2.cloudflarestorage.com` or a MinIO URL |
| `EXUVIA_S3_REGION` | `AWS_REGION`, then `us-east-1` | signing region (`auto` for R2) |
| `EXUVIA_S3_ADDRESSING` | `path` | `virtual` puts the bucket in the host name |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | — | bucket credentials (requests are SigV4-signed) |

### Outputs in an existing bucket

`EXUVIA_ARTIFACT_STORE` does not need a bucket of its own. exuvia only reads
and writes objects under the prefix in the URL, with GetObject, HeadObject,
PutObject, DeleteObject and ListObjectsV2. It never reads or changes bucket
settings (lifecycle rules, policies, CORS, versioning). Its only bulk delete
runs when a project is deleted, and it removes `<prefix>/<project>/`. Two rules
keep it away from everything else in the bucket:

- **Always give a prefix**, e.g. `s3://shared-bucket/exuvia`. With a bare
  `s3://shared-bucket`, project folders sit at the bucket root, and deleting
  a project named `logs` deletes whatever else is under `logs/`.
- **One prefix per deployment**, e.g. `s3://shared-bucket/exuvia-prod` and
  `s3://shared-bucket/exuvia-staging`. Object keys use the project id and not
  `EXUVIA_DB_SCOPE`, so two hubs on the same prefix that both have a project
  `demo` overwrite each other's outputs, and deleting `demo` on one deletes
  the other's outputs too.

Credentials can then be limited to the prefix. On AWS:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::shared-bucket/exuvia/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::shared-bucket",
      "Condition": {"StringLike": {"s3:prefix": "exuvia/*"}}
    }
  ]
}
```

## The agent

The agent runs a tool loop over the live workspace: `list_nodes`, `read_node`,
`write_node`, `delete_node`, `list_files` / `read_file` / `write_file` /
`delete_file` (shared helpers under `lib/`), `list_views` / `read_view` /
`write_view` / `write_view_file` / `delete_view_file` / `delete_view` (see
"Views"), `run_node`, `define_param`, and
`query_data` (DuckDB over every node's current output), and `report_issue`
(see "MCP"). It looks at real column names before writing, runs
what it writes, reads the traceback when something fails, and fixes it. Every
tool call shows up in the chat, and nodes it touches flash on the canvas.

Pick the model with environment variables; the provider is auto-detected from
whichever key is set (Claude wins if both are):

| Provider | Key | Default model |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` (adaptive thinking, server-side fallback) |
| `openrouter` | `OPENROUTER_API_KEY` (or `OPEN_ROUTER_KEY`) | `deepseek/deepseek-v4-pro` |
| `openai` | `OPENAI_API_KEY` + `EXUVIA_BASE_URL` + `EXUVIA_MODEL` | none (must be set) |

```bash
# DeepSeek through OpenRouter
OPENROUTER_API_KEY=sk-or-... exuvia serve my-project
# any other OpenRouter model
EXUVIA_PROVIDER=openrouter EXUVIA_MODEL=qwen/qwen3-coder exuvia serve my-project
# a local model
EXUVIA_PROVIDER=openai EXUVIA_BASE_URL=http://localhost:11434/v1 EXUVIA_MODEL=qwen3:32b OPENAI_API_KEY=x exuvia serve .
```

The model has to support function calling. The UI's agent header shows which
model is active.

**The same model from a node.** A Python node can call the configured model
once, for text, with `exuvia.llm.complete`. It uses the provider the agent
uses and raises `LLMError` when none is set, or when the reply is empty or
cut off, so a node never stores half an answer.

```python
from exuvia import llm

@exuvia.node
def note(figures):
    return {"summary": llm.complete(figures.to_csv(index=False), system="Summarise these figures in two sentences.")}
```

**Project notes.** Put an `AGENTS.md` next to `exuvia.json` (or an `instructions`
string in it) to tell the agent about this project's data sources, helpers
and conventions. It is appended to the system prompt on every turn, which is
how a host app teaches the agent its own data without changing exuvia.

**Sessions.** A conversation is stored with the project
(`GET /api/agent/{session_id}` returns its transcript and whether a turn is
running). A turn keeps running if the client disconnects, and its events are
also published on `/api/events`, so a UI can reload and re-attach.

Things to ask it:

- "Pull in daily prices and add a 60-day rolling correlation to SPY"
- "Why did `returns` fail?" (with the node selected)
- "Schedule `prices` every 30 minutes during market hours"
- "Chart the drawdown for each ticker"

## MCP: the agent's tools in your own agent

`exuvia mcp` serves a project's agent tools over the Model Context Protocol,
so Claude Code, Claude Desktop, Cursor or any other MCP client can build and
run pipelines with the model you already use; exuvia needs no API key of its
own. The client gets the same tools as the built-in agent (see "The agent",
plus `list_revisions`, `read_revision`, `restore_node` and
`restore_view_file`) and a `guide` tool. The agent's instructions (how exuvia
works, this project's parameters and `AGENTS.md`) are the server's
instructions, and `guide` returns them for clients that don't pass those on.

```bash
exuvia mcp my-project                                # open the project in this process
exuvia mcp --url http://127.0.0.1:7420               # relay to a running `exuvia serve`, or every project of a hub
exuvia mcp --url http://127.0.0.1:7420/p/my-project  # ...or to one project of a hub
```

Add it to Claude Code (or any client's MCP config) with `uvx`, so nothing
has to be installed first:

```bash
claude mcp add exuvia -- uvx exuvia mcp /path/to/my-project
```

```json
{"mcpServers": {"exuvia": {"command": "uvx", "args": ["exuvia", "mcp", "/path/to/my-project"]}}}
```

With pipx the command is `pipx run exuvia mcp ...`, or plain `exuvia mcp ...`
after `pipx install exuvia`. (Older commands with `exuvia[mcp]` still work:
the MCP SDK used to be an extra and is now always installed.) To have your agent
install exuvia and add the server itself, see
[Set up with your agent](#set-up-with-your-agent).

**Which mode.** With a directory, `exuvia mcp` opens the project itself and
needs nothing else running; its schedules don't fire (that is `exuvia serve`'s
job; set `EXUVIA_SCHEDULER=internal` to have them fire anyway), and
`--executor` runs nodes remotely as with `serve`. The directory must already
be a project (`exuvia init` makes one). While `exuvia serve` has the project
open, use `--url` instead: the server stays the only process that owns the
project, its UI shows every change and run as it happens, and the tools reach
it through `POST /api/tools/{name}`. `EXUVIA_API_TOKEN` is sent as a bearer
token when set; `EXUVIA_API_TOKEN_FILE` (a path; `~` is your home directory)
keeps the token itself out of the MCP client's configuration.

**A whole hub.** Given a hub's own address (its `/healthz` says `"mode":
"hub"`), `exuvia mcp --url` serves every project on it: `list_projects`,
`create_project`, and each project tool with a required `project` argument
(the project's id); `guide` with `project` returns that project's
instructions, and without it describes the hub and lists its projects. One
MCP server then covers all of them, including ones created later. A hub's
home page writes this setup out (**Connect agent**). For an agent that should
see only one project, give `<hub>/p/<project>` instead.

This needs exuvia 0.3.11 or newer on the agent's machine, so the hub's
commands say `uvx exuvia@latest`: a plain `uvx exuvia` runs the `exuvia`
installed with `uv tool install` when there is one, however old. An older
exuvia given a hub's address doesn't start, and says to upgrade
(`uv tool upgrade exuvia` or `pipx upgrade exuvia`).

Code the client changes is recorded with the origin `mcp`, and nodes it runs
with the trigger `mcp`, so the history shows which changes came from your
agent. A tool that fails returns its error (a traceback's last lines, an
invalid input) as the tool result, for the model to read and fix.

**Reporting problems.** When the problem is exuvia's rather than your
project's (a tool fails on valid input, the instructions are wrong or
unclear, something you need is missing), your agent can say so with the
`report_issue` tool. It takes a `title`, the `details` (what it did, the exact
error, what it expected) and a `kind` (`bug`, `idea` or `feedback`), and it
**sends nothing**: it returns a draft issue, with the exuvia, Python and OS
versions added, and a link that opens it prefilled at
[github.com/rlancer/exuvia/issues](https://github.com/rlancer/exuvia/issues).
You read it and press Submit, or tell the agent to file it with `gh`. The
agent's instructions ask it to offer this whenever exuvia is at fault, and to
leave secrets and your data out. Issues from anyone are welcome, with or
without an agent.

## Headless

```bash
exuvia run examples/markets                 # run everything stale
exuvia run examples/markets returns --force # force one node (+ stale upstream)
exuvia status examples/markets
```

`exuvia run` exits non-zero when any node fails, so it drops straight into CI
or a system cron.

## Hub: many projects from one process

`exuvia hub <dir>` serves every project under one directory, each with the
full API under its own prefix and the web UI at `/p/<project>/`. `/` is the
hub's home page: it lists the projects, creates one (from a template, when
the hub has any) and opens its workbench, and deletes one. Its **Connect
agent** button gives the [MCP](#mcp-the-agents-tools-in-your-own-agent) setup
for the whole hub (one server for every project): a prompt that has your
coding agent add the server itself, the `claude mcp add` and `codex mcp add`
commands, and `mcpServers` JSON for other clients. Each reads the hub's token
from a file (`~/.config/exuvia/<host>.token`, which the page shows how to
create), never from its own config. It is the shape a
multi-tenant host needs: one process (or one container) per tenant, one
project per workflow. With `EXUVIA_API_TOKEN` set, the home page asks for the
token, or a browser opens `/?token=<token>` or `/p/<project>/?token=<token>`
once; either way it gets the cookie. With `EXUVIA_BROWSER_AUTH=open` as well,
it gets the cookie without asking.

```bash
exuvia hub ./projects --templates ./templates --scheduler external
```

| Route | |
|---|---|
| `GET /` | the hub's home page (the web UI): list, create, open and delete projects |
| `GET /p/{project}/` | that project's workbench (the web UI, talking to `/p/{project}/api`) |
| `GET /api/projects` | list projects (`{projects, templates, auth}`; `auth`: whether clients need the API token) |
| `GET /api/tools` | the project tools, as each project's `/guide` lists them (for `exuvia mcp --url <hub>`) |
| `POST /api/projects` | create one (`{id, name, template}`; templates come from `--templates` / `EXUVIA_TEMPLATES_DIR`) |
| `PUT /api/projects/{project}/template` | replace its code with a template's (`{template}`): node files and views not in the template are removed (each view folder is replaced whole), the name, run history and stored outputs are kept, and removed files stay restorable from their revisions |
| `DELETE /api/projects/{project}` | remove one |
| `ANY /p/{project}/api/...` | that project's API (everything in the table below) |
| `POST /api/schedule/tick` | evaluate every project's schedules; returns what started and the next due time. A project that fails to load or tick is skipped and named under `errors` (id → reason); the others still tick |
| `GET /api/hub/state` | whether anything is running, and a counter that changes when state does |
| `GET /api/hub/export`, `POST /api/hub/import` | the whole directory as one archive, for hosts with no persistent disk (without outputs already in `EXUVIA_ARTIFACT_STORE`, or records already in a shared `EXUVIA_DB`) |

With `--scheduler external` nothing fires on its own: the host calls
`/api/schedule/tick` when it wants schedules evaluated and can sleep until the
`next` time it returns.

## Embedding / using it inside another app

Everything the UI does is a plain HTTP call, so a host app can drive exuvia
headlessly or iframe the UI:

| Route | |
|---|---|
| `GET /api/project` | graph + per-node status, and every view with its state (`?params={...}` for one parameter set) |
| `PATCH /api/project` | rename, set the timezone |
| `GET /api/params`, `PUT/DELETE /api/params/{name}` | run parameters and the values that have run |
| `GET/PUT/DELETE /api/nodes/{name}` | read / write / delete a node file |
| `GET /api/files`, `GET/PUT/DELETE /api/files/lib/{path}.py` | list / read / write (`{"source"}`) / delete shared helper modules; each answer names the nodes that import the file (`used_by`). Paths outside `lib/`, other extensions and `..` are rejected (400) |
| `GET /api/views` | every view with its state (`?params=`), as in `/api/project` |
| `GET/PUT/DELETE /api/views/{name}` | one view with its parsed manifest and every file's source / create it or replace its `view.json` (body: the manifest) / delete the folder |
| `GET/PUT/DELETE /api/views/{name}/files/{path}` | read / write (`{"source"}`) / delete one file of a view (`App.tsx`, `components/Chart.tsx`). Paths outside the view folder, `..`, absolute paths and other extensions are rejected (400); writes return the view |
| `POST /api/views/{name}/restore` | make a saved version (`{path, hash}`) of one view file current again; brings back a deleted file or view |
| `GET /api/views/{name}/data/{node}` | `/api/nodes/{node}/data` for a view: 403 unless the view's `reads` lists `node` |
| `POST /api/views/{name}/query` | `/api/query` for a view: only the nodes in its `reads` exist, and the connection cannot open other files (see "Rendering a view") |
| `POST /api/nodes` | create from a template |
| `POST /api/nodes/{name}/run` | run a node; `{"wait": seconds}` waits and returns the same report the agent's `run_node` gets (status, error + log tail, or schema, rows and first rows; `running: true` if still going) |
| `POST /api/run` | bring the graph (or named nodes) up to date for a parameter set (`wait` as above: per-node status and each failure's error + log tail) |
| `GET /api/guide` | the agent's instructions for this project (conventions, parameters, project notes) and its tools with their input schemas, for an outside agent writing nodes over HTTP |
| `POST /api/tools/{name}` | run one of those tools with the body as its input, as the agent would: `{"ok", "text", ...}`. A failed tool or a rejected input is still a 200 with `ok: false` and `text` saying why; an unknown tool is a 404. `exuvia mcp --url` relays MCP tool calls here (see "MCP") |
| `POST /api/run-stale` | refresh everything stale |
| `POST/GET/DELETE /api/backfill` | replay a list or date range of parameter values |
| `GET /api/nodes/{name}/data` | a node's output (`?params=` or `?run_id=`, `?limit=`, `?offset=`): JSON rows, or an Arrow stream (see "Binary data") |
| `GET/HEAD /api/artifacts/{node}/{artifact_id}.parquet` (or `.json`) | the stored file of one output, byte for byte, with `Range` support (see "Binary data") |
| `GET /api/nodes/{name}/history` | the node's stored output per parameter set (with the `code_hash` each was computed with) |
| `GET /api/code/revisions` | saved versions of the code, newest first (`?node=` for one node, `?view=` for one view's files, deleted ones included); each says who saved it and whether it is current |
| `GET /api/code/blobs/{hash}` | the text of one saved version |
| `POST /api/nodes/{name}/restore` | make a saved version (`{hash}`) current again; brings back a deleted node |
| `GET /api/runs/{run_id}/source` | the exact code a run executed |
| `POST /api/query` | DuckDB SQL over all outputs (`{sql, limit, params}`): JSON rows, or an Arrow stream (see "Binary data") |
| `POST /api/nodes/{name}/schedule` | set, clear, pause or resume a schedule |
| `POST /api/schedule/tick` | evaluate schedules now (for external schedulers) |
| `GET /api/events` | SSE: graph changes, run status, live logs |
| `POST /api/agent` | SSE: agent turn |
| `GET/DELETE /api/agent/{session_id}`, `POST .../cancel` | saved transcript, clear it, stop a turn |

Requests that change code may send `X-Exuvia-Origin` (letters, digits, `-`,
`_`; e.g. `ui` or `mcp`) to label the revisions they create; otherwise they
are recorded as `api`.

### Binary data: Arrow and Parquet

The JSON answers of `/api/query` and `/api/nodes/{name}/data` are for
previews: values become text or floats, and at most 5,000 rows come back
(500 and 200 unless `limit` says otherwise). A client that wants the data
itself has two other ways to read it.

**Arrow stream.** Send `Accept: application/vnd.apache.arrow.stream` to either
route and the answer is an [Arrow IPC stream](https://arrow.apache.org/docs/format/Columnar.html#ipc-streaming-format):
typed columns, every row unless `limit` is given, written batch by batch.

```js
import { tableFromIPC } from "apache-arrow";

const res = await fetch("/api/query", {
  method: "POST",
  headers: { "Content-Type": "application/json", Accept: "application/vnd.apache.arrow.stream" },
  body: JSON.stringify({ sql: "select * from rolling_vol where ticker = 'AAPL'" }),
});
const table = tableFromIPC(new Uint8Array(await res.arrayBuffer()));
JSON.parse(table.schema.metadata.get("exuvia")); // { columns: [{ name, type }] }
```

- The schema metadata key `exuvia` holds, as JSON, what the JSON answer has
  besides the rows: `columns` (names and DuckDB types) and, for a node's
  output, `kind`, `total`, `run_id`, `run_params` and `finished_at`.
- A statement that fails is answered with the usual `400` JSON error. One
  that fails after its first batch has been sent ends the stream early, which
  an Arrow reader reports as a truncated stream.
- On `/api/nodes/{name}/data`, an output that is not a table (a JSON value,
  or no output yet) is answered as JSON whatever `Accept` says, so send
  `Accept: application/vnd.apache.arrow.stream, application/json` and look at
  the response's `Content-Type`.
- Buffers are not compressed, so any Arrow library can read the stream.

**The stored file.** Every output is a file named after its content:
`artifact_id` in `/api/project` and `/api/nodes/{name}/history`, with
`output_kind` / `kind` saying whether it is a table (`.parquet`) or a JSON
value (`.json`). `GET /api/artifacts/{node}/{artifact_id}.parquet` returns
that file as it is stored, downloading it from `EXUVIA_ARTIFACT_STORE` first
when this process does not have it.

- The answer for a given URL never changes, so it is sent with
  `Cache-Control: private, max-age=31536000, immutable` and the artifact id
  as its `ETag`.
- `Range` requests are honoured (`206`, `Accept-Ranges: bytes`) and `HEAD`
  gives the size, which is all a Parquet reader that fetches only the footer
  and the columns it needs (DuckDB, DuckDB-Wasm, pyarrow) asks for.
- Outputs of earlier runs and other parameter sets stay readable for as long
  as the project exists; an id no successful run produced is a `404`.

Set `EXUVIA_CORS_ORIGINS` for cross-origin hosts. `exuvia.connectors.lobster`
queries the Lobster lake from any node (`LOBSTER_API` for the base URL;
`LOBSTER_TOKEN` is sent as a bearer token when set and omitted otherwise, for
hosts that attach credentials at the network edge).

## Contributing

This section is for working on exuvia itself: everything in it runs from a
clone. [mise](https://mise.jdx.dev/) supplies the tools (uv, Python, bun,
actionlint) from `mise.toml`.

```bash
git clone https://github.com/rlancer/exuvia
cd exuvia
mise run sync      # uv sync + bun install
mise run ui-dev    # Vite on :5180 proxying /api → :7420
mise run demo      # ui-build if needed, then exuvia serve examples/markets
mise run ui-build  # bundle the UI and the views runtime into exuvia/static
mise run ui-typecheck # tsc over the UI (CI's `bun run build` runs it too)
mise run test
mise run ui-test   # views runtime unit tests (Vitest)
mise run e2e       # build the container image and drive a hub in it over HTTP
mise run agent-e2e # hand the setup prompt to a coding agent and check its work
mise run lint-actions # actionlint + shellcheck over .github/workflows
mise run site-dev   # the website on :4380
mise run site-build # typecheck + static build into site/dist
```

The UI and the website use [bun](https://bun.sh/) and no Node: `bun install`
installs from `bun.lock`, and `bun run <script>` runs Vite, Vitest, Astro and
wrangler on bun itself (each folder's `bunfig.toml`). Run a package binary
as `bun run <bin>`, not `bunx`, which would pick up a Node on PATH, and the
UI tests as `bun run test`: plain `bun test` is bun's own test runner. The
one step on Node is `wrangler dev` (`mise run site-cf-dev`), whose local
Workers runtime does not start under bun; mise installs Node for that task
alone.

`sync`, `ui-build` and `demo` run `scripts/dev.py`, which makes a fresh git
worktree ready in seconds. It keeps uv's and bun's package caches in the
clone's git dir (`.git/exuvia-cache/`), on the same drive as every worktree,
so installs hardlink packages instead of copying them. It also keeps the
built UI keyed on the content of `ui/` (uncommitted edits included), so a
worktree that hasn't changed the UI copies the build in instead of running
Vite. A build skips `tsc` and runs both Vite builds at once; `cd ui && bun
run build` is the full build with the typecheck, as CI runs it.
`.claude/launch.json` starts `demo` and `ui-dev` from an agent's browser pane.

The views runtime is a second build (`ui/vite.runtime.config.ts`): one
self-contained script, because the page hands it to the frame as text and
the frame loads nothing itself. `exuvia serve` (and every hub project) serves
it at `/api/view-runtime/runtime.js`, so a page that embeds a headless exuvia
gets the runtime matching the server it talks to. Build once before using
views from the dev server.

### Testing the setup prompt with an agent

`tests/e2e/agent_setup_e2e.py` checks that a coding agent can get from
nothing to a working analysis with only the prompt under
[Set up with your agent](#set-up-with-your-agent), read from this README.
The prompt has the agent fetch its steps from the docs site; the harness
serves this README's copy of them on localhost instead, so a run tests the
steps in the checkout (`--live-steps` lets the agent fetch the site's). It runs two headless sessions in an empty folder: the setup prompt, then a
new session asked for a small analysis, so the MCP server the agent added
is the one it uses. It checks that the project, the workbench, the `exuvia`
MCP entry and the `hello` run exist, then that the analysis nodes were added,
ran fresh, and ran through MCP. The default agent is the Grok Build CLI
(`agent` or `grok`) with `grok-4.5`:

```bash
mise run agent-e2e                                        # exuvia from PyPI
mise exec -- python tests/e2e/agent_setup_e2e.py --source local   # this checkout
mise exec -- python tests/e2e/agent_setup_e2e.py --model grok-4.7 --phase setup
```

`--cold-analysis` stops the workbench before the second session, so the agent
has to start it, prompted only by what the MCP tools answer.

`--source local` builds a wheel of the checkout, versioned above the latest
release, and points uv at it, so the agent's plain `uvx exuvia` commands run
the code under test. Each run starts uv with an empty cache, as a new user
has (`--warm-cache` uses yours). The agent's user-level MCP config
(`~/.grok/config.toml`) is saved first and restored afterwards. Transcripts and `report.json` are
kept in a `-logs` folder next to the work folder. When a run fails, fix the
prompt or exuvia rather than the agent's instructions.

### Building the image

Releases publish the image (see Releasing), so you only build one to try a
change to it:

```bash
docker build -t exuvia:local .
EXUVIA_IMAGE=exuvia:local EXUVIA_API_TOKEN=change-me docker compose -f deploy/compose.yaml up -d
```

Swarm and Kubernetes nodes pull their image from a registry, so push the
build to one they can reach, then set `EXUVIA_IMAGE` or the Deployment's
`image` to it.

`mise run e2e` (`tests/e2e/container_e2e.py`, standard library only) builds
the `Dockerfile` image, starts it as `exuvia hub /data`, and works through it
over HTTP: a project from the `examples/markets` template, every node run,
an output and its Parquet artifact read, the views runtime fetched, and a
second project written node by node and deleted. It uses podman when it is
on PATH and docker otherwise (`CONTAINER_ENGINE` picks one); on Windows and
macOS start the podman VM first (`podman machine init` once, then
`podman machine start`). The container is always removed, and its logs are
printed when a step fails. With `E2E_URL` (and `E2E_TOKEN`) it tests a hub
that is already running instead, uploading the example over HTTP and deleting
what it created. CI runs it with docker in the `container` job, against the
image itself and against the `deploy/` recipes brought up with Compose, as a
Swarm stack and on a kind cluster. `pytest` does not collect it. To check
a deployment of your own:
`E2E_URL=http://localhost:7420 E2E_TOKEN=change-me python tests/e2e/container_e2e.py`
creates two projects, runs the example graph, checks the outputs and deletes
both again.

### The website

The website (`site/`, Astro) is a landing page plus a docs portal. The docs
pages are built from the `## ` sections of this README, so there is one copy
of the reference: `site/src/lib/docs.ts` maps each section to a page, and the
site build fails when a section is added, renamed or removed without
updating that map. The getting-started tutorial is the one hand-written page
(`site/src/docs/getting-started.md`). `/brand/` is the brand page: the name,
the mark, the colours, the type, the animals and every social card, all read
from the code that draws them (`site/src/lib/brand.ts`). Every page names a
social card, a PNG in `site/public/og/` listed in `site/src/lib/cards.ts`: a
picture with no words on it, one for the home page, one for the docs' front
page, one for each group of docs pages and one for the brand page. The build
fails when a page's card is missing.
`site/tools/capture.mjs` retakes the product screenshots from a running
`exuvia serve`, and photographs the social cards from the site's own
`/card/` pages, which exist only under `astro dev`. The animals on the landing page are low-poly models built in code
(`site/src/lib/poly.ts`), with no image or model files. The site is static (`site/dist`). It is built for the
root of its host unless `SITE_BASE` (for example `/exuvia`) names a sub-path
to serve it from; every link, asset and the search index follow that base.
`SITE_URL` (the origin only) makes the canonical and social-card URLs
absolute.
The public site is https://exuvia.lobster.mp/: the `exuvia-site`
Cloudflare Worker (static assets from `site/dist`, `site/wrangler.jsonc`)
on that custom domain. `.github/workflows/deploy-site.yml` builds it on
every push that touches `site/` or this README: `main` deploys to
production, other branches upload a preview version whose workers.dev URL is
in the job summary. By hand, with `wrangler login` done: `mise run
site-deploy`, or `mise run site-cf-dev` to serve the build locally with
wrangler (http://localhost:8787/).

### Layout

```
exuvia/
  project.py    static graph discovery (ast / SQL identifiers)
  workspace.py  control plane: freshness, run orchestration, cascade, scheduler, watcher
  executor.py   Executor interface: LocalExecutor (warm pool) + RemoteExecutor (HTTP client)
  executor_service.py  `exuvia executor` HTTP service
  pool.py       pre-warmed single-use worker processes
  runner.py     per-node subprocess entry point
  store.py      SQLite run history
  arrowstream.py  query results as an Arrow IPC stream
  agent.py      agent tools + provider-neutral tool loop
  llm.py        model providers: Anthropic, OpenAI-compatible (OpenRouter / DeepSeek)
  server.py     FastAPI routes + SSE + static UI
ui/             React + React Flow + CodeMirror + Vega-Lite
ui/src/views/   views runtime (sandboxed iframe), its postMessage protocol and the page's side of it
examples/       demo projects
site/           the website: landing page + docs portal (Astro, static)
```

### Releasing

Nobody bumps a version. The package's version is its latest `v*` git tag
(hatch-vcs), and every push to `main` that changes the package (`exuvia/`,
`ui/`, `pyproject.toml`, `uv.lock`, the Dockerfile) is released as the next
patch version by `.github/workflows/release.yml`. It tags the merged commit
and starts two workflows on that tag:

- `publish.yml` builds exactly the tag's version, runs the tests, bundles the
  UI into the wheel and publishes to PyPI. It uses trusted publishing, so no
  token is stored anywhere.
- `image.yml` builds the container image for linux/amd64 and linux/arm64,
  with the tag's version passed in as `EXUVIA_VERSION`, and pushes it to
  `ghcr.io/rlancer/exuvia` as `<version>`, `<major>.<minor>` and `latest`.

A push that changes only docs, tests, the site or CI releases nothing. A
label on the pull request changes the bump: `release:minor` (0.3.1 → 0.4.0),
`release:major` (0.3.1 → 1.0.0) or `release:skip` (nothing now; the change
goes out with the next release). Actions → Release → Run workflow releases
`main` as it is, with the bump you pick.

Between releases, a checkout reports a development version such as
`0.3.2.dev4+g1a2b3c4`, and a build without git history (a plain
`docker build`) reports `0.0.0`. Pushing a `vX.Y.Z` tag by hand still
starts both workflows, and each can be started on an existing tag from the
Actions tab, which is how to retry one that failed. A version cannot be
released twice: `v*` tags cannot be moved or deleted, and PyPI refuses a
version it already has, so a bad release is fixed by the next version.

GHCR creates a new package as private. After the first image is pushed, set
the package to public once (package settings → Change visibility).

## Status

Prototype. Not yet: multi-user auth (only a shared token; the hub leaves tenancy to its
host), executor autoscaling, incremental nodes (run parameters cover date partitions), non-Python runtimes, secrets management beyond env vars.

Found a bug, or something missing? Open an issue at
[github.com/rlancer/exuvia/issues](https://github.com/rlancer/exuvia/issues). Your agent can draft one
for you with `report_issue` (see "MCP").

## License

Apache-2.0
