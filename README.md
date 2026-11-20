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

> Run every command below from the repository root.

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

## Quick start

```bash
pip install exuvia          # in this repo: uv sync
exuvia serve examples/markets
# → http://127.0.0.1:7420
```

Set `ANTHROPIC_API_KEY` (Claude) or `OPENROUTER_API_KEY` (DeepSeek and any
other OpenRouter model) in the environment or the project's `.env` to turn on
the agent. See [The agent](#the-agent) for providers.

Start a fresh project:

```bash
exuvia init my-research
exuvia serve my-research
```

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

```bash
docker build -t exuvia .
# executor (default command)
docker run -p 8080:8080 -e EXUVIA_EXECUTOR_TOKEN=change-me exuvia
# control plane on your machine, nodes running in the container
EXUVIA_EXECUTOR_TOKEN=change-me exuvia serve my-project --executor http://localhost:8080
```

Nodes need their packages in the executor image — extend it:

```dockerfile
FROM exuvia
RUN uv pip install --python /opt/venv/bin/python scipy statsmodels
```

**Hosts that sleep or scale to zero.** An in-process scheduler can't fire while
its process is asleep. Run the control plane with `--scheduler external` and
have something call `POST /api/schedule/tick` every minute or so: a Cloudflare
Durable Object alarm or Cron Trigger, a Kubernetes CronJob, or system cron.
Ticks are idempotent; fires missed while asleep are coalesced into one
catch-up run, and paused schedules skip their backlog.

**Auth.** Set `EXUVIA_API_TOKEN` on a hosted control plane: API clients send
`Authorization: Bearer <token>`; browsers open `/?token=<token>` once and get
an HttpOnly cookie. Always set `EXUVIA_EXECUTOR_TOKEN` on a networked executor
— it runs arbitrary code by design.

| Env | Default | |
|---|---|---|
| `EXUVIA_EXECUTOR_URL` / `EXUVIA_EXECUTOR_TOKEN` | local pool | remote executor + its token |
| `EXUVIA_SCHEDULER` | `internal` | `external` = only `POST /api/schedule/tick` advances schedules |
| `EXUVIA_API_TOKEN` | off | shared-secret auth for the control plane API |
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

## The agent

The agent runs a tool loop over the live workspace: `list_nodes`, `read_node`,
`write_node`, `delete_node`, `list_files` / `read_file` / `write_file` /
`delete_file` (shared helpers under `lib/`), `list_views` / `read_view` /
`write_view` / `write_view_file` / `delete_view_file` / `delete_view` (see
"Views"), `run_node`, `define_param`, and
`query_data` (DuckDB over every node's current output). It looks at real column names before writing, runs
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
full API under its own prefix. It is the shape a multi-tenant host needs: one
process (or one container) per tenant, one project per workflow.

```bash
exuvia hub ./projects --templates ./templates --scheduler external
```

| Route | |
|---|---|
| `GET /api/projects` | list projects |
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
| `GET /api/guide` | the agent's instructions for this project (conventions, parameters, project notes) and its tool list, for an outside agent writing nodes over HTTP |
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

## Development

```bash
mise run sync      # uv sync + npm install
mise run ui-dev    # Vite on :5180 proxying /api → :7420
mise run demo      # exuvia serve examples/markets
mise run ui-build  # bundle the UI and the views runtime into exuvia/static
mise run test
cd ui && npm test  # views runtime unit tests (Vitest)
mise run site-dev   # the website on :4380
mise run site-build # typecheck + static build into site/dist
```

The views runtime is a second build (`ui/vite.runtime.config.ts`): one
self-contained script, because the page hands it to the frame as text and
the frame loads nothing itself. `exuvia serve` (and every hub project) serves
it at `/api/view-runtime/runtime.js`, so a page that embeds a headless exuvia
gets the runtime matching the server it talks to. Build once before using
views from the dev server.

The website (`site/`, Astro) is a landing page plus a docs portal. The docs
pages are built from the `## ` sections of this README, so there is one copy
of the reference: `site/src/lib/docs.ts` maps each section to a page, and the
site build fails when a section is added, renamed or removed without
updating that map. The getting-started tutorial is the one hand-written page
(`site/src/docs/getting-started.md`). `site/tools/capture.mjs` retakes the
product screenshots from a running `exuvia serve` and renders the social
card; `site/tools/ink.mjs` turns a generated woodcut into the transparent
prints the pages use. The site is static (`site/dist`). It is built for the
root of its host unless `SITE_BASE` (for example `/exuvia`) names a sub-path
to serve it from; every link, asset and the search index follow that base.
`SITE_URL` (the origin only) makes the canonical and social-card URLs
absolute.
`.github/workflows/deploy-site.yml` builds it on every push that
touches `site/` or this README and deploys it to the `exuvia` Cloudflare
Pages project: `main` goes to production (https://exuvia-c7l.pages.dev/),
other branches get a preview deployment whose URL is in the job summary.

Layout:

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

## Releasing

PyPI releases go through `.github/workflows/publish.yml` with trusted
publishing, so no token is stored anywhere. Bump `version` in `pyproject.toml`,
merge, then push a matching tag:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

The workflow checks the tag matches the version, runs the tests, bundles the
UI into the wheel, and publishes.

## Status

Prototype. Not yet: multi-user auth (only a shared token; the hub leaves tenancy to its
host), executor autoscaling, incremental nodes (run parameters cover date partitions), non-Python runtimes, secrets management beyond env vars.

## License

Apache-2.0
