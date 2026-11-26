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

**Packages your nodes import** go in the project's own `pyproject.toml`,
which `exuvia init` writes. Add one with uv in the project folder, and exuvia
installs it before the next run:

```bash
cd my-research && uv add scipy yfinance
```

[Packages](#packages) has the details, private package indexes among them.

Set `ANTHROPIC_API_KEY` (Claude) or `OPENROUTER_API_KEY` (DeepSeek and any
other OpenRouter model) in the environment or the project's `.env` to turn on
the built-in agent; a Portkey gateway or any OpenAI-compatible server works
too. See [The agent](#the-agent) for providers. Or let the
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
    retries=2,                         # optional: try a failed run again, up to 2 more times
    live=True,                         # optional: re-run when a view that reads it is opened
    key=["date", "ticker"],            # optional: fail a run whose rows repeat a key (see "Keys")
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
-- retries: 2
-- live: 300
-- chart: {"mark": "bar", "encoding": {...}}
select u.sector, avg(v.vol_20d) as avg_vol
from universe u join rolling_vol v using (ticker)
group by 1
```

The graph is read **statically** (`ast` for Python, identifier matching for
SQL), so a broken node still shows up in the graph with its error, and nothing
runs until you ask.

**Several outputs from one node.** When one step yields several results of
different shapes (a model fit's predictions, coefficients and metrics, or one
API call that returns two tables), return them together instead of redoing
the work in separate nodes. A dict, dataclass or pydantic model whose values
include a DataFrame is stored as named outputs: each DataFrame is a table,
anything else is JSON. Names are lowercase identifiers.

```python
@exuvia.node()
def fit(returns):
    ...
    return {"preds": preds, "coefs": coefs, "metrics": {"r2": r2}}
```

```sql
-- a table of a node with several outputs is <node>.<name>
select term, value from fit.coefs order by abs(value) desc
```

A Python node that takes `fit` gets a dict, `{"preds": DataFrame, "coefs":
DataFrame, "metrics": {...}}`. The node is still one node: one run, one
schedule, one freshness state, and the outputs downstream nodes read are
stale or fresh together. A Data tab with a picker shows each one.

**Typed outputs.** Dataclasses and pydantic models are JSON outputs too (or
several outputs, above, when a field holds a DataFrame). Annotate a
parameter with the class and it arrives as that class, built from the
upstream's output, rather than as a dict. Put the class in a `lib/` helper so
both nodes import it:

```python
from lib.models import Fit, Settings   # @dataclass Fit: preds, coefs; pydantic Settings

@exuvia.node()
def report(fit: Fit, settings: Settings):
    return fit.preds.tail(settings.window)
```

A pydantic model is validated (`model_validate`, nested models included); a
dataclass is called with the fields as keywords, one level deep. When the
output does not fit the class, the run fails and says which parameter it
could not build.

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
static `import` / `from … import` statements are tracked. Open a helper from
the workbench's **Files** tab (below) to edit it and see which nodes use it.
Besides `nodes/`,
`lib/**/*.py` and view folders (see "Views") are the only places the API and
the agent can write.

The workbench's side panel has three tabs: **Agent**, **Files** and
**History**. **Files** shows those files as one tree: `nodes/`, `lib/` and
each view's folder under `views/`, with each node's and view's state. It only
finds files; picking one opens it where it is edited. A node file selects the
node on the graph and opens the inspector's Code tab, a view file opens that
view's Code tab, and a helper opens in an editor in place of the graph, with
the nodes that import it and its saved versions. A helper's address is
`#files/lib/stats.py`, and `#files/<path>` opens any of these files from a
link. The chat keeps running while another tab is showing; a dot on the
Agent tab means it has something new.

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
  For a node with several outputs it is `<node>.<name>__runs`. It is for
  comparing runs in SQL and views; a node that should read a growing history
  is better [partitioned](#partitioned-nodes-a-history-that-grows-each-run).
- `timezone` also sets the clock for cron schedules. `schedules` overrides or
  adds a per-node cron without editing the node file (this is what the
  schedule API writes), so an embedding app can let people schedule nodes.
  `failure_webhook` tells Slack or any URL when a scheduled run fails (see
  [Failure notifications](#schedules-auth-and-settings)).

```bash
exuvia run my-project --param as_of=2026-11-06
exuvia status my-project --param as_of=2026-11-06
```

## Partitioned nodes: a history that grows each run

Some data arrives a little at a time: a closing price a day, a reading an
hour. Returning the whole history from every run refetches it and stores a
new full copy each time; one output per `as_of` stores each day once but
gives downstream nodes only that day. A **partitioned** node keeps one
output that each run adds to:

```python
@exuvia.node(partition_by="date", schedule="0 17 * * 1-5")
def prices(universe):
    have = exuvia.partitions()             # dates already stored, oldest first; [] the first time
    start = next_day(have[-1]) if have else "2006-01-01"
    return fetch_closes(universe, start)   # rows for any number of dates
```

```sql
-- partition_by: date
select * from source where $last_partition is null or date > $last_partition
```

- **A run replaces the partitions it returns.** Each value of the
  `partition_by` column in the rows a run returns (a date) is a partition:
  those rows replace whatever that partition held, and every partition the
  run leaves out is kept. A daily run returns today's rows, a backfill ten
  years of them in one call, a correction the day it fixes. `None` or no rows
  changes nothing (a holiday). Every row needs a value in the column.
- **What it already has.** `exuvia.partitions()` lists the stored values as
  text, in order (dates and numbers sort as such); a SQL node gets the newest
  as `$last_partition` (null when there is none).
- **Downstream nodes read the whole history** as their input table, and
  `<node>` in `query_data`, views and `/api/query` is all of it.
- **Identical rows change nothing.** A partition is identified by a hash of
  its rows, so a run that returns the same rows for a day writes nothing, the
  output keeps its `artifact_id`, and downstream nodes don't run again. When
  a day does change, they do.
- **Parameters.** A partitioned node may read `as_of` (`params=["as_of"]`)
  to know which day to fetch, but its own parameters don't split its output:
  each `as_of` run, and a backfill over `as_of` (`POST /api/backfill`), fills
  the same history, and downstream nodes don't inherit `as_of` from it.
- **Full refresh.** A code change marks the node stale, but its next run still
  adds to what is there: a source that only reports "now" never loses its
  history by accident. To rebuild from nothing, run it with a full refresh
  (`POST /api/nodes/{name}/run` with `"full_refresh": true`, `exuvia run
  my-project prices --full-refresh`, or `run_node` with `full_refresh`):
  `exuvia.partitions()` is empty for that run, and what it returns becomes
  the whole output. Changing `partition_by` itself needs one.
- **Sources that send part of a day: `merge=True`.** With a `key` (see
  [Keys](#keys-no-row-twice)), `merge=True` (SQL: `-- merge: true`) makes a
  run update or insert rows by key into the partitions it returns instead of
  replacing them: a run's row replaces the stored row with its key, a new key
  is added, and a stored row whose key the run didn't return stays. A feed
  that sends the morning's trades, then the afternoon's, then one corrected
  trade, keeps all of them. `on_duplicate="last"` applies to the run's own
  rows before they merge. A merged partition is digested like any other, so a
  merge that changes nothing writes nothing and runs nothing downstream; only
  the files holding the partitions a run returns are read. **A merge never
  removes a row**: to delete rows, or to move a row to another partition, run
  a full refresh, which doesn't merge: what that run returns becomes the
  whole output.
- One partition column per node, and one table: a node with several outputs
  can't be partitioned. A push node can (`push=True, partition_by="date"`):
  each push replaces the partitions it holds (or, with `merge=True`, merges
  into them).

**How it is stored.** The output is a manifest of Parquet files: one per
partition as it is written, in `.exuvia/artifacts/<node>/` like every output
and uploaded to `EXUVIA_ARTIFACT_STORE` under the same keys. Every file costs a
reader a little, so exuvia compacts: once a node has more than 30 files
beyond one per year, each year's files are merged into one (dates group by
year; other values are merged together). A correction to a day inside a
merged year rewrites that year's file without it. Readers get exactly the
manifest's files, the query sandbox can open no other, and compaction never
changes the output's id, so it never makes anything stale. Each run's version
of the manifest is kept in the records database, so an earlier run's output
stays readable (`?run_id=`). Nothing deletes files no manifest uses anymore:
exuvia never deletes outputs.

**Reading it from outside.**

- `GET /api/nodes/{name}/data` and `/export` take `partitions=lo..hi`
  (either end may be left out) or one value, and read only the files that
  hold those partitions: `?partitions=2026-11-01..` is this month so far.
- `GET /api/nodes/{name}/partitions` lists the manifest: every partition with
  its row count and digest, and the Parquet files with the partitions each
  holds. Each file is `GET /api/artifacts/{name}/{artifact_id}.parquet`.

## Keys: no row twice

A duplicate row doesn't fail anything by itself: it doubles a sum, skews an
average, and the analysis is quietly wrong. Declare the columns that
identify a row and exuvia checks every output against them before storing
it:

```python
@exuvia.node(partition_by="date", key=["date", "ticker"])
def prices(universe): ...
```

```sql
-- key: sector
select sector, avg(vol_20d) as avg_vol from rolling_vol join universe using (ticker) group by 1
```

- **A run that breaks the key fails.** No row may have a null in a key
  column, and no two rows may share a key. Otherwise the run fails with how
  many rows repeat a key and the worst examples (`3 rows repeat a key (date,
  ticker), e.g. (2026-11-03, AAPL) ×2, ...`), and the node's previous output
  stays: nothing downstream sees the duplicates. A failed key isn't retried.
- **`on_duplicate="last"`** (SQL: `-- on_duplicate: last`) keeps, for each
  key, the last row in the order the node returned them, for a source known
  to send a row twice (overlapping pages, a re-sent file). The log says how
  many rows were dropped. Nulls in a key column still fail.
- **A partitioned node's key holds across its whole output**, not only the
  rows one run returns. Take a trade re-dated from the 3rd to the 4th: a run
  that returns only the 4th would leave the old copy in the 3rd, so it fails
  and names both. Return the 3rd as well, without the trade, and the run
  succeeds. `on_duplicate="last"` doesn't apply here: exuvia can't tell which
  copy is right. A node with `merge=True` can't move a row that way, since a
  merge never removes one: that takes a full refresh. A run checks its own rows against the partitions it keeps
  (only for a key without the partition column; with it, a key can't repeat
  across partitions). When the key is first declared or changes, the next run
  checks the whole stored output instead, even if it changes nothing, so an
  output that already has duplicates says so (fix it with a
  [full refresh](#partitioned-nodes-a-history-that-grows-each-run)). That is
  a scan of the key columns, under a second for five million rows.
- **A partitioned node with a key can merge** (`merge=True`): its runs update
  or insert rows by key instead of replacing whole partitions, for sources
  that send part of a day at a time. See
  [Partitioned nodes](#partitioned-nodes-a-history-that-grows-each-run).
- It works on any node that returns one table, pushed outputs included. The
  check runs in DuckDB on the control plane, over what the run wrote, so it
  is the same for Python and SQL nodes and for local and remote executors.

## Push nodes: data from someone's desktop

Some data can only be read on one person's machine. A Bloomberg
Terminal's Desktop API answers on that machine's `localhost`, signed in as
that person, so neither the hub nor an executor can pull from it. A **push
node** stands for that data in the graph, and a script on the machine that
has it uploads each new version:

```python
"""Daily closes from the Bloomberg Terminal on the trading desk."""
import exuvia


@exuvia.node(push=True)
def bbg_closes():
    ...  # never run: the output is pushed
```

On the desktop, from Python:

```python
import exuvia

df = ...  # read it with blpapi, xbbg, Excel, whatever that machine has
exuvia.push("https://exuvia.example.com/p/research", "bbg_closes", df, note="desk 4")
```

or with the CLI, from a `.parquet`, `.csv` or `.json` file:

```bash
exuvia push bbg_closes closes.csv --url https://exuvia.example.com/p/research --note "desk 4"
```

- **Each push is a run** of the node, triggered `push`, with who pushed it
  and the note in its log. Freshness, history, views and downloads treat
  it like a computed output, and a new output cascades to the nodes that
  read it. Pushing the same data again makes the same output, which stops
  the cascade.
- **Nothing on the hub runs it.** Runs leave push nodes out, and running one
  by name (from the workbench, the API or the agent) says it is pushed. Until
  the first push it shows `new`, "waiting for a push", and a node reading it
  fails with "has not been pushed yet". A push node takes no upstream nodes
  and has no schedule: schedule the script on the desktop (Task Scheduler,
  cron) instead. Editing the node file doesn't make the pushed output stale.
- **Parameters.** A push node may declare run parameters
  (`push=True, params=["as_of"]`); each push is for one set of values
  (`params={"as_of": "2026-11-20"}`, `--param as_of=2026-11-20`), so a
  day's snapshot can be pushed late without replacing today's.
- **What it sends.** A DataFrame (pandas, polars or pyarrow) or a `.csv` goes
  as Parquet, stored exactly as a node's returned DataFrame is; a JSON-able
  value or a `.json` file as JSON. One push is one output: several tables are
  several push nodes. The hub takes uploads up to 1 GB
  (`EXUVIA_PUSH_MAX_MB`).
- **Who may push.** Pushing changes the project, so it needs edit access.
  The token is `EXUVIA_API_TOKEN` (or the file `EXUVIA_API_TOKEN_FILE`
  names), or `token=`: your own agent token from the workbench's account
  menu, or the server's. As with `exuvia env`, plain http is refused unless
  the server is on the same machine (`--allow-http`, `allow_http=True`).
  Over HTTP it is `POST /api/nodes/{name}/push` with the Parquet
  (`Content-Type: application/vnd.apache.parquet`) or JSON
  (`application/json`) as the body, `?params=` a JSON object and `?note=` a
  line for the log. Another content type needs `?format=parquet|json`; a
  body sent as a form or as `text/plain`, which any web page can send, is
  refused (415).
- **Licences.** Terminal data usually comes with limits on who else may see
  it. A hub that others can read, or a share link, may take it past them:
  check the terms before pushing it somewhere shared.

Running the node itself on the desktop, with the hub scheduling it and the
code managed there, is planned: see
[rlancer/exuvia#127](https://github.com/rlancer/exuvia/issues/127).

## Views

A **view** is custom UI over node outputs, kept apart from the graph. In a
notebook the UI lives in each cell and is built from Python: widget calls, or
HTML and JavaScript held in strings. Here the nodes compute outputs in Python
and SQL, and a view is front-end code that only reads them: TypeScript, React
and CSS in files of their own, with components, imports and types. The
canvas, inspector and SQL box stay as they are.

How a view works, in short ("Rendering a view" below has the detail):

1. Nodes run and store their outputs. A view never runs anything; opening
   one re-runs only the nodes marked live (see [Live nodes](#live-nodes-current-data-when-a-view-opens)).
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
  not trigger runs. Work a view needs belongs in a node it reads. The one
  exception is a node its author marked live: opening the view re-runs it
  while the view shows the output it has (see
  [Live nodes](#live-nodes-current-data-when-a-view-opens)).
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
once and cache it for good. For a node with several outputs, `kind` is
`multi` and `parts` lists them (`name`, `kind`, `rows`). `live` lists the
live nodes it depends on, those it reads and those upstream of them. `hash` covers
every file of the view and the project's npm packages, so a client can tell
when what the view runs changed.

### Rendering a view

The UI's **Views** mode (next to the graph) lists the views with their state
and reason, shows the nodes each one reads (click one to open it on the
canvas), lets you pick the run parameters, and renders the selected view.
Compile and runtime errors from the view are listed under it with the file
they came from; click the file to open it. `examples/markets` ships one
(`views/vol_board`).

**A view as an app.** The view's expand button (or `?app=<view>` on the
project's address: `/?app=vol_board`, or `/p/<project>/?app=vol_board` on a
hub) opens the view on its own, full screen, with none of the workbench
around it. One floating **Tools** button in its corner holds what the
workbench offered around the view: the run parameters, a switch to the
project's other views, Run for read nodes that are missing or stale (for
editors), Copy link, Share (on a hub), Download, the colour theme and the way
back to the workbench. Parameters other than their defaults ride along in the
address as `p.<name>=<value>` (`?app=vol_board&p.as_of=2026-11-06`), so a
copied link opens the view as it was shown; a value the project refuses is
left out, with a note under Tools. `#views/<view>` opens a view in the
workbench's Views mode instead. On a hub, the Share dialog gives every view's
app address, and a share link lands on one when `?app=<view>` is added to it
(the dialog offers that when it shows a new link).

The view's **Code** tab lists its files (`view.json` first, then the entry)
and edits them in place, with TypeScript, JSX, CSS and JSON highlighting.
Ctrl+S (⌘S on a Mac) or **Save** writes the open file
(`PUT /api/views/{name}/files/{path}`), which versions it like any other edit
and rebuilds the view; compile errors from the new version show up under the
editor. Unsaved edits are kept per
file while you move between files, views and the graph, and a file the agent
or an editor on disk changes reloads unless you have unsaved edits to it.
The **History** tab (or **History** in the Code tab's bar, which opens it on
the file you are editing) lists every saved version of the view's files,
deleted files included, and restores one (see "Browsing and restoring code"
under "Execution model").

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
entry file's `export default` with React 19. A view can import:

- its own files by relative path, with or without the extension (`./Chart`,
  `../lib/format.ts`; `./dir` finds `dir/index.tsx`), never outside its folder;
- `.css` files (added to the page), `.json` (the parsed value), `.svg` (a
  `data:` URL);
- by bare name, what the runtime ships: `react` (+ `react/jsx-runtime`),
  `react-dom` (+ `/client`), `apache-arrow`, `vega-embed` (Vega-Lite charts)
  and `exuvia`;
- by bare name, the npm packages in the project's `package.json`
  (`recharts`, `date-fns`, `lodash-es/debounce`, `leaflet/dist/leaflet.css`;
  see [npm packages for views](#npm-packages-for-views)). Anything else is a
  compile error naming the file and the package, which the error offers to
  add (**Add recharts**).

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
| `useTable(node)`, `table(node)` | a read node's current table output, as an Arrow `Table`; `useTable("fit.coefs")` for one output of a node with several |
| `useJson(node)`, `json(node)` | a read node's current JSON output (`useJson("fit.metrics")`) |
| `useQuery(sql)`, `query(sql)` | DuckDB SQL that sees only the read nodes (`<node>` and `<node>__runs`; `<node>.<name>` for several outputs), as an Arrow `Table` |
| `useParams()`, `getParams()`, `setParams(values)` | the run parameters the view shows; setting them changes which outputs it reads, never runs anything |
| `onChange(fn)` | called with the nodes whose output was replaced |
| `rows(table)` | Arrow rows as plain objects (64-bit integers become numbers when they fit) |
| `useView()`, `useDark()` | the view's name, title and reads; whether the page is in dark mode |
| `useRefreshing(node?)` | true while a node the view reads (or `node`) is being re-run, such as a live node refreshing as the view opened; the hooks keep the data they have until the new output lands |
| `download(node \| {sql}, format, filename?)` | save a read node's whole output (`download("prices", "xlsx")`) or a query's whole result (`download({sql: "select …"}, "csv")`) as a file: `csv`, `xlsx`, `parquet` or `json`, for the parameters the view shows. The frame cannot save files itself, so the page does |

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
| page → frame | `load {view, params, dark, refreshing}` | the view's files (`{path: source}`), entry and reads, the parameter set, and the read nodes being re-run |
| frame → page | `request {id, method, args}` | `table {node}`, `json {node}`, `query {sql}`, `params {}`, `setParams {params}`, `download {node \| sql, format, filename}` |
| page → frame | `response {id, ok, result \| error}` | Arrow IPC bytes for `table`/`query` (transferred, not copied), the value for `json`, `{params, specs}` for the parameter calls |
| page → frame | `changed {nodes, params}` | outputs the view reads were replaced; driven by `/api/events` |
| page → frame | `refreshing {nodes}` | the read nodes being re-run now (empty when none) |
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
out, `ATTACH` a file or `INSTALL` anything. `GET
/api/views/{name}/data/{node}/export` and `POST /api/views/{name}/query/export`
are the same two routes as downloads (see "Downloads").

## Live nodes: current data when a view opens

Most outputs only change when their inputs or code do, or on a schedule.
Some are worth fetching again whenever someone looks: quotes, a queue's
depth, today's sign-ups. Mark such a node **live**, and opening a view that
depends on it re-runs it:

```python
@exuvia.node(live=True)          # every time a view that reads it opens
def quotes():
    ...

@exuvia.node(live=300)           # only when its output is over 5 minutes old
def queue_depth():
    ...
```

```sql
-- Open orders, as of now.
-- live: true
select * from orders where status = 'open'
```

- **The view opens at once.** It renders the outputs that are stored, as
  always, and says **Refreshing** (next to its state in the workbench, in
  the top corner as an app) while the live nodes run. When the new outputs
  land, its hooks reload and the indicator goes. A view's code can show its
  own with `useRefreshing()` (`useRefreshing("quotes")` for one node).
- **What runs.** The live nodes the view reads, and those upstream of what
  it reads, for the parameters the view shows; then the nodes between them
  and the view, each re-run only if the live node's output changed (cached
  otherwise), so what the view reads is current in the same batch. Changing
  the view's parameters refreshes again at the new values.
- **`live=True` or seconds.** `True` re-runs on every open; a number of
  seconds (up to a week) re-runs only when the output is older than that,
  which bounds how often a busy view re-runs a slow or rate-limited source.
  A live node that is already running for those parameters is not started
  again: every viewer waits for the same run.
- **Each refresh is a run** of the node, triggered `live`, with who opened
  the view as who started it, so it shows in the history like any other run,
  and a new output cascades as usual. A live node can still have a
  schedule, and runs by hand like any node.
- **Anyone who can open the view refreshes it,** viewers and share links
  included: making a node live says it may run whenever its view is looked
  at. Over HTTP it is `POST /api/views/{name}/refresh` with
  `{"params": {...}}`; it answers at once with `live` (the view's live
  nodes), `started` and `skipped` (node → why not: already running, or
  refreshed within its seconds).
- A push node can't be live: nothing on the hub runs it.

## Downloads

Anything you can see in the workbench you can save as a file. Every table
offers four formats: **CSV**, **Excel** (`.xlsx`: typed cells, a frozen
header row; text that starts with `=` stays text, never a formula),
**Parquet** (typed columns, for pandas, DuckDB or Spark) and **JSON** (an
array of rows). A JSON output downloads as `.json`.

- **A node's output**, every row, not just what the table shows: the
  download button in the node's header and under its Data tab. For a node
  with several outputs, each one on its own, or all of them as the stored
  `.zip`.
- **A past run's output:** the Runs tab, on any successful run.
- **A SQL result:** the button beside "Run query" saves the whole result.
- **What a view reads**, for the parameters it shows: the button in the
  view's header. A view can add its own buttons with `download()` (see
  "Rendering a view").
- **Code, as a .zip:** the whole project (the download button in the top bar,
  or the one beside the Files tab's filter), one node with the `lib/`
  helpers it imports (the node's header), or one view (the view's header).
  The archive holds the nodes, helpers and views with `exuvia.json`,
  `pyproject.toml`, `uv.lock`, `package.json`, `AGENTS.md` and `README.md` when present;
  never `.env`, run history or outputs.

The same routes serve scripts:

```bash
curl -OJ 'http://127.0.0.1:7420/api/nodes/prices/export?format=parquet'
curl -OJ 'http://127.0.0.1:7420/api/code/archive'
curl -OJ http://127.0.0.1:7420/api/query/export \
  -H 'Content-Type: application/json' \
  -d '{"sql": "select * from prices where close > 100", "format": "xlsx"}'
```

The file is named after the node and the run's parameter values
(`prices-2026-11-20.csv`); `filename` (without the extension) names it
otherwise. CSV and JSON stream as they are written, so a big table never sits
in memory. One Excel sheet holds 1,048,575 rows: a bigger table is refused
(413), so download it as CSV or Parquet. Lists, structs and maps become JSON
text in CSV and Excel cells. A viewer of a shared project can download
everything they can read.

## Packages

A project lists the packages its nodes import, beyond those exuvia comes
with, in the standard place: `[project] dependencies` in the project's
`pyproject.toml`. The packages travel with the project, so the same list
works on a laptop, in every project on a hub (each with its own list) and on
a remote executor, which installs from the copy in the bundle it receives.

```toml
[project]
name = "my-research"
version = "0"
requires-python = ">=3.11"
dependencies = ["scikit-learn==1.9.1", "acme-models>=2.3"]
```

**Adding a package in the workbench.** The **Packages** button above the graph, in the project's
bar lists what the project has and what is installed for it, adds packages
(`scikit-learn`, `acme-models>=2`) and removes them, edits `pyproject.toml`
directly, and shows the install's status and log. A package added without a
version is pinned to the exact version installed, so the project stays
reproducible. When a node fails on an import of something not installed,
its error offers the package (**Install scikit-learn** for `import
sklearn`): one click adds it, installs it and re-runs the node.

**From a terminal.** `exuvia init` writes a `pyproject.toml`, and `uv add
<package>` in the project folder adds to it, as does editing the list by
hand. exuvia picks the change up on the next run.

**The agent asks; a person approves.** The agent has `list_packages` and
`request_packages`. `list_packages` shows the project's list and its install;
with `environment: true` it also lists every package installed where nodes
import from, with its version, so the agent can check before it asks. On a
node that fails for want of a package it asks for
it, and the request waits in the Packages panel, with the agent's reason,
for someone to install or dismiss it. Set `EXUVIA_AGENT_PACKAGES=install` to
let the agent add packages itself, or `off` to keep it from asking. MCP
clients get the same tools and the same rule.

**Installed ahead of time.** A change to the list starts its install in the
background right away, and a hub checks every project's list when it
starts, so a project restored from an export or moved to a new exuvia
version is ready before anyone runs it. A run that starts during an install
waits for it. An install that fails (a package that doesn't exist, a
registry that refuses the credentials) shows uv's explanation in the panel,
and runs fail at once with the same error until the list changes or
someone retries. Every change to `pyproject.toml` is kept as a code
revision.

**Installed once, kept across restarts and upgrades.** An install is named
after what it holds: the packages and versions the list resolved to, the
index each came from, the Python version and the platform. So:

- A restart reuses it without resolving anything.
- When the list or exuvia's version changes, the list is resolved again
  (seconds, nothing installed), preferring the versions installed before.
  If that gives the same packages, which is usual after an exuvia upgrade,
  the install is reused. Only packages that changed, a new Python, or a new
  NumPy major version (compiled packages are built against it) install
  again.
- On a hub, projects whose lists resolve to the same packages share one
  install, kept in the hub's `.exuvia/packages/` beside the projects. A
  project on its own keeps its installs in its own `.exuvia/packages/`.
- uv's download cache sits in the hub's `.exuvia/packages/.uv-cache/`, on
  the hub's volume, so an install that does run after a restart copies files
  instead of downloading and building them again. A `UV_CACHE_DIR` set on
  the server wins. Deleting the cache is safe; it fills again as needed.
- An install no project uses any more is removed, at the earliest an hour
  after it was made. Hub exports leave installs out; a restored project
  installs from its `pyproject.toml`.

**They sit on top of exuvia's own packages.** Nodes keep using the pandas,
pyarrow and DuckDB that exuvia runs with, so the project's list is resolved
against those versions, and packages exuvia already has are not installed
again. A package that needs, say, `pandas<2` is refused with uv's
explanation and leaves `pyproject.toml` as it was. exuvia does not read
`uv.lock`; pins in the list are what make an install repeatable.

**When a package conflicts with exuvia's versions.** The conflict is often
a few levels down: `acme-grpc` needs `acme-proto==0.2.3`, which needs
`python-dateutil<3`, while exuvia runs `python-dateutil==4.0.0`. uv then
tries the package's older releases, and can stop at a different error in one
of them (a Git URL dependency, a build that fails). When that happens exuvia
resolves the list again without its own versions. If the newest releases
need other versions of exuvia's packages, the error shows that conflict
first (the `==` versions in it are exuvia's), then the error uv stopped at.
What to do about it:

- Pin a release of the package that works with exuvia's versions
  (`acme-grpc==1.8`), if there is one.
- Use an exuvia version that runs the version the package needs, or ask the
  package's maintainers to widen the requirement.
- If the requirement is narrower than it needs to be (the package works
  with the newer version), override it in the project's `pyproject.toml`.
  uv then takes exuvia's version and ignores what the package asked for:

  ```toml
  [tool.uv]
  override-dependencies = ["python-dateutil"]
  ```

  Nodes then import the package with a version it never asked for, so run
  them to check that it works.

**Private package indexes.** On a hub, set the index once for every project,
on the server:

```text
UV_INDEX=internal=https://pypi.example.com/simple
UV_INDEX_INTERNAL_USERNAME=deploy
UV_INDEX_INTERNAL_PASSWORD_FILE=/run/secrets/pypi_token
```

uv then looks for packages there, ahead of PyPI, so people add `acme-models`
by name and never see the URL or the credentials. `UV_INDEX_<NAME>_USERNAME`
and `_PASSWORD` (the index name in capitals) can each come from a file with
`_FILE`, for a Docker or Kubernetes secret. Any other uv setting works the
same way (`UV_DEFAULT_INDEX` to replace PyPI with a mirror, for one).

A single project can also name an index of its own in its `pyproject.toml`,
where uv reads it. `uv add --index internal=https://pypi.example.com/simple
acme-models` writes both parts:

```toml
[[tool.uv.index]]
name = "internal"
url = "https://pypi.example.com/simple"
explicit = true   # only the packages in tool.uv.sources come from it

[tool.uv.sources]
acme-models = { index = "internal" }
```

Its credentials go in the environment as above, or in the project's `.env`.
A remote executor installs the packages itself, so it needs the index
settings in its own environment.

**The installer needs a route to the index.** uv runs on the server, in the
hub's container (or the executor's), not on the machine of whoever clicks
**Install**, so that container has to reach the index and every host that
serves its package files. Those are not always the same host: PyPI's index
is `pypi.org`, and its files come from `files.pythonhosted.org`. uv takes
`HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` from the server's environment, so
a hub with no direct route out installs through a proxy once those are set
on it. An install that fails with `error resolving DNS` or `failed to lookup
address information` never reached the index: run the check from inside the
running hub, as in
[Package installs on an isolated network](#package-installs-on-an-isolated-network).

**Packages for every project.** A project's install is its own: projects
that list the same packages each install them. What every project on a
server should have without waiting for an install (your team's internal
libraries, a standard stack, packages that need system libraries) belongs
in the image, listed in a `pyproject.toml` of its own and installed with
`uv pip install` (not `uv add` or `uv sync`): see
[Packages your nodes import](#packages-your-nodes-import) under Deploying.

### npm packages for views

Views import npm packages the same way nodes import Python ones: the project
lists them in the standard place, `dependencies` in a `package.json` at its
root, and a view imports them by name.

```json
{
  "private": true,
  "dependencies": { "recharts": "3.11.1", "date-fns": "^4.1.0" }
}
```

```tsx
import { LineChart, Line, XAxis, YAxis } from "recharts";
import { format } from "date-fns";
```

Nothing is installed and no build step runs. When a view loads, the server
fetches the modules each package it imports is made of from an
[esm.sh](https://esm.sh) CDN, already built for the browser: one bundle per
package with its own dependencies inside, and with the packages every view
has (`react`, `react-dom`, `apache-arrow`, `vega-embed`) left out, so a
package and the view share one React. The page posts those modules into the
view's frame with its files, so the frame still fetches nothing and has no
network (see "Rendering a view"). A package therefore runs only inside that
sandbox, never on the server. One that fetches anything itself (map tiles,
web fonts, an API) will not work there.

- **Adding one.** The **Packages** button's **npm, for views** tab lists the
  packages, adds them (`recharts`, `date-fns@^4`, `@tanstack/react-table`)
  and removes them. Each is checked on the CDN first; one added without a
  version is pinned to its newest release, so the view shows the same thing
  tomorrow. A view that imports a package the list does not have fails with
  an error that offers to add it. Editing `package.json` by hand works too.
  The packages every view has can't be added: views always get the
  runtime's copy.
- **The agent** adds them itself with `add_view_packages`: they run only in
  the sandbox, so they need no approval. `EXUVIA_AGENT_PACKAGES=off` turns
  that off as well. `list_packages` lists them under `view_packages`.
- **Fetched once.** Fetched modules are kept in the project's `.exuvia/npm/`
  (on a hub, one `.exuvia/npm/` beside the projects, shared by them). A
  module whose address names an exact version never changes and is never
  fetched again; one named by a range (`^4.1.0`) is fetched again after an
  hour, and the copy kept is used while the CDN can't be reached. Exports,
  backups and code archives carry `package.json`, not the cache.
- **Your own CDN.** `EXUVIA_NPM_CDN` points at another esm.sh, such as one you
  host yourself. Every change to `package.json` is kept as a code revision.

## Environment variables and secrets

API keys, tokens and settings your nodes read go in the project's
**Environment** (the button next to Packages in the workbench), not in the
code. Every node gets them in `os.environ`:

```python
import os

import exuvia


@exuvia.node(schedule="0 18 * * 1-5")
def rates():
    import requests

    key = os.environ["FRED_API_KEY"]
    ...
```

- **Secret or plain.** A variable is secret unless you untick **Secret**.
  Once a secret is saved its value is never shown again, to you, to anyone
  the project is shared with or to the agent: the workbench, the API and the
  agent's `list_env` tool give its name only. To change it, type a new value.
  A plain variable (a region, a base URL) shows its value to anyone who can
  open the project.
- **Secrets stay out of run logs.** Wherever a secret's value shows up in
  what a node prints, or in a run's error, it is replaced with `***` before
  the log is saved, streamed or read by the agent. A multi-line secret (a PEM
  key) is masked line by line. A value shorter than 4 characters is not
  looked for, a secret a node transforms (encodes, slices) is not
  recognized, and a node's output is never masked, so don't print a secret
  or return it.
- **Where they apply.** Every run started after a change, on the local
  executor or a remote one (they travel with each run; a `.env` never
  reaches a remote executor). A project's variable wins over the server's
  variable of the same name, and over the project's `.env`. Changing one
  doesn't make nodes stale: run the nodes that read it to use the new value.
- **Shared by a group.** On a hub, a project in a group also gets the
  group's variables (see [Groups](#groups-shared-access-and-secrets)). When a
  name is set in more than one place, the project's own variable wins, then
  the group's, then the hub's, then the server's environment. The
  Environment dialog lists what the group gives, read-only and marked with
  the group's name (and *overridden* where the project sets the same name),
  and `GET /api/env` returns them as `inherited`, beside the project's own
  `variables`.
- **Shared by the whole hub.** A hub's admins can also set variables every
  project on the hub gets (see
  [Variables for every project](#variables-for-every-project)). The
  Environment dialog lists them under **From the hub**, and `inherited`
  carries them with `source: "hub"`.
- **Who can change them.** Anyone who can edit the project. Viewers of a
  shared project can read the names and the plain values (`GET /api/env`,
  `list_env`), never a secret.
- **What a project cannot set.** exuvia's own settings, which nodes never
  get (`EXUVIA_*`, `PORTKEY_*`, package index credentials
  `UV_INDEX_<NAME>_USERNAME` / `_PASSWORD`), and the variables that decide
  how a run starts (`PATH`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONSAFEPATH`,
  `PYTHONUSERBASE`, `VIRTUAL_ENV`). A name is letters, digits and
  underscores; a value is up to 32,768 characters; a project holds up to 200.
- **Several lines are fine.** A PEM key, a certificate chain or a service
  account's JSON goes in as it is: paste it, or load the file with **Or
  load it from a file**. When a value is saved its line endings become
  `\n` (a key pasted or loaded from Windows works) and surrounding
  whitespace and blank lines are dropped; a value of several lines then
  ends with exactly one newline, as PEM parsers and OpenSSH expect. JSON
  escapes such as `\n` inside a string are left as they are.
- **Where they are kept.** In the project's records database (see
  [Records](#records-sqlite-postgres-or-your-own-adapter)): `.exuvia/exuvia.db`,
  or the shared `EXUVIA_DB` on a hub, each project with its own. The code
  download never includes them.

### Encryption

Secrets are encrypted before they are saved, and decrypted only to start a
run: the database, its backups and a Postgres dump hold
`enc:v1:<key id>:<token>`, never a value. Plain variables are stored as
they are. The encryption is Fernet (AES-128 in CBC mode with an
HMAC-SHA256, from the `cryptography` package).

**The key.** With nothing set up, exuvia makes a key for each project the
first time it saves a secret, in `.exuvia/secrets.key` (readable only by
its owner), apart from the database. That keeps secrets out of a shared
`EXUVIA_DB` and out of database dumps, but anyone who has the project's
folder has its key too, and a hub's exports carry each project's key so
that a restore keeps its secrets.

For real protection, give the server its own key, kept outside the data
(a Docker or Kubernetes secret, a secret manager):

```bash
exuvia secrets-key              # prints a new key
EXUVIA_SECRETS_KEY=<that key>   # or EXUVIA_SECRETS_KEY_FILE=/run/secrets/exuvia_secrets_key
```

With `EXUVIA_SECRETS_KEY` set, every project's secrets are encrypted with
it, no key file is made, and a hub's export holds only encrypted values:
it needs the same key to be read. A malformed key stops exuvia at start.
Remote executors never need the key, since a run arrives with its values
decrypted (over the executor's authenticated connection).

**Changing keys.** List several keys, comma-separated, newest first:
`EXUVIA_SECRETS_KEY=<new>,<old>`. The first encrypts and all of them
decrypt; when a project loads, its secrets are encrypted again with the
first, after which the old key can go. Setting `EXUVIA_SECRETS_KEY` on a
server whose projects have their own key files works the same way: the
files still decrypt, and each project's secrets move to the server's key
when it loads. Secrets saved before encryption existed are encrypted the
same way, on load.

**A lost key.** A secret no key on the server can decrypt is never
guessed at: the workbench marks it *Can't be decrypted*, the API and
`list_env` give it `readable: false`, and runs go without it, with a line
at the top of their log saying so. Put the key back (`EXUVIA_SECRETS_KEY`,
or the project's `.exuvia/secrets.key`), or enter the value again.

### When the agent needs a key

The agent (the built-in one, or yours over MCP) can see which variables
exist, and writes nodes that read them by name. It never sees or sends a
secret's value, and it has no tool to set one. When a node needs a key that
isn't set, it calls `request_secret` with the name and why, and you enter
the value yourself:

- **In the workbench.** The request waits in **Environment** (a count on
  the button, and a banner with **Set it** and **Dismiss**), and the agent
  passes on a link that opens the dialog on it: `<project>/#env=FRED_API_KEY`.
- **Through your MCP client.** Over `exuvia mcp --url`, a client that
  supports MCP's URL elicitation asks you directly whether to open that
  link. The value is typed in the workbench, never in the client, which is
  what URL elicitation is for. Both the 2025-11-25 protocol
  (`elicitation/create`) and the 2026-07-28 one (an `input_required`
  result) work. Clients without it, and the built-in `/mcp` endpoint
  (stateless, so it can't ask the client anything), get the link and the
  terminal command in the tool's answer instead.
- **From your own terminal,** with `exuvia env set` (below).

Setting the variable, from anywhere, answers the request. A request lives
in the server's memory: a restart drops it, and the agent asks again.

### From a terminal: `exuvia env`

```bash
exuvia env set FRED_API_KEY --url https://exuvia.example.com/p/research   # asks for it, not shown
exuvia env set SIGNING_KEY --from-file signing.pem                         # the project in this folder
exuvia env set REGION --plain --stdin <<< eu-west-1
exuvia env list --url https://exuvia.example.com/p/research
exuvia env rm OLD_KEY
```

- **The value is never an argument,** where your shell's history and `ps`
  would keep it: `exuvia env set NAME=value` is refused. It is asked for
  without being shown, or read from `--from-file` (a PEM key, which a
  one-line prompt can't take) or `--stdin`. With no terminal to ask on and
  neither of those, it refuses, so a script or an agent driving the
  command can't send one by accident.
- **`--url`** is a running exuvia: an `exuvia serve`, or a hub project at
  `<hub>/p/<project>`. The request carries `EXUVIA_API_TOKEN` (or
  `_FILE`): the server's token, or your own agent token, which acts as you.
  Only HTTPS is used for another machine; plain `http` is for this one
  (`localhost`), unless you pass `--allow-http` for a private network.
- **Without `--url`** it opens the project folder (default `.`) the way
  the server does, reading `EXUVIA_SECRETS_KEY` from the environment or the
  project's `.env`. It is safe while a server runs the same project: it
  changes the variables and nothing else, and the server uses them on the
  next run.
- `list` shows names and plain values, never a secret, and the requests
  waiting to be set. For a hub project it also lists what its group and the
  hub give.
- **`--group <id>`** works on a hub group's variables instead: `--url` is
  then the hub's address (`exuvia env set SHARED_TOKEN --url
  https://exuvia.example.com --group g1a2b3c4d5e6`). The group's id is on
  its page in the hub and in `/api/groups`.
- **`--hub`** works on the hub's own variables, which every project on it
  gets: `--url` is the hub's address (`exuvia env set LLM_GATEWAY_KEY --url
  https://exuvia.example.com --hub`). Only an admin's agent token or the
  hub's API token can change them.

A `.env` in the project folder still works for `exuvia serve`, which reads
it at start.

## Execution model

- **Content-addressed outputs.** Each run writes its result to
  `.exuvia/artifacts/<node>/<sha>.parquet` (or `.json`, or `.zip` for
  several outputs: one member per output, written so the same outputs make
  the same bytes), keyed by the
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
- **A failed refresh doesn't block what reads it.** When an ancestor's latest
  run failed but its last good output is still current (same code, same
  inputs), runs below it read that output instead of retrying it. A monthly
  source whose API is down doesn't stop the node that runs every 5 minutes on
  top of it; the source retries on its own schedule or when you run it. An
  ancestor whose code or inputs changed since its last good output still runs
  first.
- **A failure stands until the node runs again.** A node is *failed* while
  its latest attempt (a run that succeeded or failed) failed; runs that were
  cached or skipped don't clear it. Refreshing everything stale
  (`POST /api/run-stale`, `exuvia run`) retries failed nodes.
- **Subprocess isolation.** Every node runs in its own Python process: crash
  isolation, clean per-run logs (stdout and stderr in the order printed,
  streamed live to the UI and readable with `read_run` while the run goes
  on), killable timeouts (`EXUVIA_RUN_TIMEOUT`, default 1800 s; per node,
  `timeout=<seconds>`, SQL `-- timeout: 300`). A run that times out fails
  with `timed out after ...`; a runner killed from outside says so (a
  SIGKILL exuvia did not send is usually the out-of-memory killer).
- **Stopping a run.** A queued or running run can be stopped from the
  workbench (Stop), the MCP `cancel_run` tool, `exuvia cancel <run id> --url
  <project>` or `POST /api/runs/{id}/cancel`: a running attempt is killed, a
  queued one (waiting behind another run of the node, or for its upstream
  nodes) ends at once. It ends failed with the error `cancelled`, is not
  retried, sends no failure webhook, and the runs downstream of it in its
  batch are skipped. Queued runs run the node's code as it is when they
  start, not as it was when they were queued.
- **Nodes don't get exuvia's own settings.** A node's process starts from the
  server's environment minus every `EXUVIA_*` and `PORTKEY_*` variable and
  package index credentials (`UV_INDEX_<NAME>_USERNAME` / `_PASSWORD`), with
  their `_FILE` forms. So node code, which the agent may have written, never
  sees the API or executor token, the database URL or the failure webhook.
  Everything else still reaches every node (`PATH`, proxies, a project's
  `.env`, credentials a node reads such as `AWS_*` or a model provider key),
  and the project's own variables are set on top (see
  [Environment variables and secrets](#environment-variables-and-secrets)).
  The installer keeps the index credentials, because uv needs them.
- **Ad-hoc SQL reads outputs only.** `query_data` and `POST /api/query` run
  DuckDB in the server's process, on a connection locked like a view's (see
  "Rendering a view"). It can read every node's outputs, but no other file
  (the server's environment through `/proc/self/environ`, the project's
  files, exuvia's database), no network and no extensions. Code that needs
  another file reads it in a node.
- **Per-node schedules + cascade.** A scheduled node is forced to re-run on its
  cron. If it produces *different* output, downstream nodes with
  `cascade=True` refresh automatically; identical output stops the cascade.
  Schedules can be paused from the UI without editing code.
- **The Schedule tab** (next to Graph and Views, `#schedule`) shows when the
  graph runs by itself: every scheduled node, soonest first, with its cron,
  its next run (its next few in a tooltip) on the project's clock, its last
  scheduled run, the nodes a new output from it re-runs, and Pause/Resume;
  the next dozen fires across the project; and the live nodes with the views
  that refresh them. `GET /api/schedule` is the same as JSON (times in Unix
  seconds).
- **A scheduled run is for its cron slot.** A `date` run parameter whose
  default is `today` gets the date of the slot the run was due at, in the
  project `timezone`, not the date it happened to start. A run delayed past
  midnight, or a catch-up run on Monday for Friday's missed 16:30, computes
  the day it was due for.
- **Overlapping fires.** A fire that comes while the node is still running
  waits and runs once that run ends (`overlap="queue"`, the default; at most
  one waits), or is dropped with `overlap="skip"` (SQL: `-- overlap: skip`).
- **Retries.** `retries=N` (0 to 10; SQL: `-- retries: N`) tries a failed run
  again up to N more times, `retry_delay` seconds after the failure (default
  30, SQL `-- retry_delay: 60`), twice as long before each further retry. The
  attempts share one run and one log, the run waits as `queued` between them,
  and a failure is reported (webhook, downstream skipped) only once the last
  attempt fails. Cancelling the run ends its wait and stops the retries.
- **Run history** lives in a database: status, trigger (manual / schedule /
  cascade / agent / cli / push), timings, logs, schema. Schedule state (last fire
  per node) is persisted too, so restarts neither drop nor double-fire
  schedules. By default that is `.exuvia/exuvia.db` (SQLite) inside the
  project; see "Records: SQLite, Postgres, or your own adapter".
- **Every version of the code is kept.** Each time a node file, a `lib/` helper,
  a view file, `exuvia.json`, `AGENTS.md` or `pyproject.toml` changes, by any
  route (UI, API, agent, an editor, a template), the new text is saved as a
  revision with who saved it (`agent`, `ui` for the workbench, `api` or the
  caller's `X-Exuvia-Origin`, `file`,
  `template`, `import`, `restore`, `load`). A deletion is a revision too.
  Revisions are keyed by the same hash each run records, so every output can
  show the exact code that produced it, and any version can be restored (a
  restore is itself a revision). The agent has `list_revisions`,
  `read_revision`, `restore_node` and `restore_view_file`.

**Browsing and restoring code.** The workbench shows the same history in
three places:

- a node's **History** tab in the inspector, next to Code, Data and Runs;
- a view's **History** tab (also reached from **History** in its Code tab);
- the side panel's **History** tab: the project's latest saves, and a
  **Deleted** list of deleted nodes, view files and other files by the last
  version saved before the deletion. Picking one opens the project's whole
  history in place of the graph (`#history`), on that file and version.

Each lists the revisions newest first with when, who (the signed-in
`author`, `—` without sign-in), how it was saved (`origin`) and, where
several files are listed, the path; a file picker narrows the list to one
file. Picking a version shows its text as a diff against the version before
it (**vs previous**: what that save changed) or against the file now (**vs
current**: what restoring it would change). **Restore** (with a
confirmation) makes a node's or a view file's version current again through
`POST /api/nodes/{name}/restore` or `POST /api/views/{name}/restore`, which
also brings back a deleted node, file or view. Other files (`lib/`,
`exuvia.json`, `AGENTS.md`, `pyproject.toml`) can be read and compared there
but not restored from the workbench.

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
  and cached on the executor, so unchanged code is not re-sent. So are the
  files of a [partitioned](#partitioned-nodes-a-history-that-grows-each-run)
  input: a run sends only the ones the executor lacks, and the executor drops
  a file no run has used for `EXUVIA_CHUNK_TTL_DAYS` (default 7).

Executor protocol (bearer `EXUVIA_EXECUTOR_TOKEN`):

| Route | |
|---|---|
| `HEAD/PUT /v1/bundles/{hash}` | is this project version cached / upload it (zip, verified against the hash) |
| `PUT /v1/runs/{id}/inputs/{dep}` | upload an upstream artifact |
| `POST /v1/chunks/missing`, `PUT /v1/chunks/{hash}` | which files of a partitioned input it lacks / upload one (verified against the hash, kept across runs) |
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
| First visit with `EXUVIA_API_TOKEN` set | `<url>/?token=<token>` (just `<url>/` with `EXUVIA_BROWSER_AUTH=open`) | `<url>/?token=<token>`, or open `<url>/` and enter the token (just `<url>/` with `EXUVIA_BROWSER_AUTH=open`) |
| First visit with `EXUVIA_AUTH=headers` | `<url>/`, through the sign-in proxy (no token) | `<url>/`, through the sign-in proxy (no token) |
| Project API prefix | `<url>/api` | `<url>/p/<project>/api` |
| Server-wide API | none | `<url>/api` (`/api/projects`, `/api/schedule/tick`, ...) |
| MCP over HTTP (no CLI) | `<url>/mcp` | `<url>/mcp` (every project), or `<url>/p/<project>/mcp` (just that one) |
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

On a hub that only a private network can reach, `EXUVIA_BROWSER_AUTH=open`
lets browsers in without the token ([auth](#schedules-auth-and-settings)):
put it in Compose's `.env`, in front of `docker stack deploy` on Swarm (the
file reads it from the shell), or uncomment it in `kubernetes.yaml`.

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
   rather than printing it. Also tell me how to let
   browsers in without the token, for a hub only a private
   network can reach (agents and the API still need it),
   and if I asked for that, do it now and check that
   GET /api/projects without the token still returns 401:
   - Compose: EXUVIA_BROWSER_AUTH=open in the .env, then
     step 4's command again.
   - Swarm: EXUVIA_BROWSER_AUTH=open in front of step 4's
     `docker stack deploy`, with the other variables it
     was deployed with set again.
   - Kubernetes: uncomment EXUVIA_BROWSER_AUTH in
     kubernetes.yaml and apply it again.
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

A project's `pyproject.toml` works in a container too (see
[Packages](#packages)): the container installs what it lists before the
first run that needs it. That install is per project, so a package that
ten projects list is installed ten times.

Packages that every project on the server should have go into the image
instead: your team's internal libraries, a standard stack, packages that
need system libraries, or anything a host with no route to a package index
can't install. Every project can import them from the start, no project
installs them again, and the packages a project adds are resolved against
their versions, as they are against exuvia's own.

**1. List them in a `pyproject.toml` of their own**, in a folder with the
`Dockerfile`, and keep both in git. This file belongs to the image, not to
a project: don't put it in a project folder.

```toml
[project]
name = "acme-exuvia-base"
version = "0"
requires-python = ">=3.11"
dependencies = ["acme-core==1.4.2", "acme-data==2.1.0", "scipy==1.16.2"]

# Only for packages from a private index. uv reads this at build time.
[[tool.uv.index]]
name = "internal"
url = "https://pypi.example.com/simple"
explicit = true

[tool.uv.sources]
acme-core = { index = "internal" }
acme-data = { index = "internal" }
```

**2. Install it into exuvia's environment** with this `Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1
FROM ghcr.io/rlancer/exuvia:0.3
USER root
WORKDIR /opt/exuvia-base
COPY pyproject.toml ./
# exuvia's packages, held at the versions it runs with (-c), plus the list.
RUN --mount=type=secret,id=index_password,env=UV_INDEX_INTERNAL_PASSWORD \
    UV_INDEX_INTERNAL_USERNAME=deploy \
    uv pip freeze --python /opt/venv/bin/python > /tmp/exuvia.txt \
 && uv pip install --no-cache --python /opt/venv/bin/python \
      -r pyproject.toml -c /tmp/exuvia.txt
WORKDIR /app
USER exuvia
```

**3. Build it**, passing the index password as a build secret so it stays
out of the image's layers. Without a private index, leave out the
`--mount=...` and `UV_INDEX_INTERNAL_USERNAME` lines and the `--secret`
flag.

```bash
docker build --secret id=index_password,src=pypi-token.txt -t my-exuvia .
```

Use the result in place of the published image: in `docker run`, or as
`EXUVIA_IMAGE` or the Deployment's `image` (Swarm and Kubernetes pull it
from a registry, so push it to one first). With a remote executor, the
executor is the one that needs the packages, so build the executor's image
this way.

**Rules for this Dockerfile, for people and agents alike.** Each one
prevents a build that succeeds and then fails later, when exuvia runs:

- **Use `uv pip install`. Never use `uv add` or `uv sync` here.** `/opt/venv`
  is exuvia's own environment, built from exuvia's `pyproject.toml` and
  `uv.lock` in `/app`, and the image sets `UV_PROJECT_ENVIRONMENT=/opt/venv`.
  `uv sync` run anywhere in the image makes `/opt/venv` match that one
  folder's `pyproject.toml` and uninstalls everything else in it, exuvia
  included. `uv add` in `/app` rewrites exuvia's own files and rebuilds
  exuvia from source with no version (the image has no `.git` to read it
  from).
- **Keep `-c` with exuvia's frozen versions.** Without it, uv upgrades or
  downgrades packages exuvia runs with to satisfy yours (a library that
  needs `pandas<2` silently replaces exuvia's pandas 3), and exuvia breaks at
  run time instead of at build time. With it, a conflict fails the build
  with uv's explanation, and the fixes are the ones under
  [When a package conflicts with exuvia's versions](#packages).
- **Keep `--no-cache`.** It keeps uv's download cache out of the image's
  layers, where it only makes the image bigger.
- **Install as root and end on `USER exuvia`.** The environment belongs to
  root; exuvia runs as its own user (uid 10001).
- **Pin exact versions.** The file is the record of what the image holds,
  and pins make a rebuild produce the same image.
- **Check the result** before you deploy it:
  `docker run --rm my-exuvia uv pip check --python /opt/venv/bin/python`
  reports any broken requirement, and
  `docker run --rm my-exuvia python -c "import acme_core"` (your import
  names) shows that the packages load.

What follows from baking packages in:

- **Projects get the image's versions.** A project can't use a different
  version of a package that the image has; it is held to that version, as
  it is to exuvia's pandas. A project may still list the package (for its
  editor, or to run somewhere else): on this image the install skips it,
  and the Packages panel shows it as exuvia's.
- **Upgrading a package means a new image.** Change the version in the
  file, rebuild and redeploy. On a new image (or exuvia release), each
  project's own packages are resolved again when the server starts, and
  installed again only if they no longer resolve to the same versions (see
  [Packages](#packages)).

### Kerberos

The image can connect nodes to services that sign in with Kerberos (Trino,
Impala, Hive, HDFS, SQL Server, HTTP APIs behind SPNEGO) without an image of
your own. On Linux, PyPI has
[gssapi](https://pypi.org/project/gssapi/), the package those clients use
underneath, only as source, and building it needs a compiler and the
Kerberos headers. So the image carries a wheel of it, built when the image
is, in `/opt/exuvia/wheels`, plus the system Kerberos library it loads.

**List what you use in the project's `pyproject.toml`,** like any other
package: `gssapi`, or a client that needs it (`requests-kerberos`,
`pyspnego[kerberos]`, `trino[kerberos]`). The install takes the wheel from
the image instead of building it. A project that doesn't list it installs
nothing.

**gssapi comes in the image's version** (1.12.0 now). Leave it unpinned or
pin that version. uv is set (`/etc/uv/uv.toml`) never to build gssapi from
source, so a project that pins another version fails at once with
`gssapi==… has no usable wheels`, rather than with a compiler error, and a
newer gssapi on PyPI isn't picked over the wheel. A newer gssapi comes with
a newer exuvia image.

**Sign in with a keytab, without `kinit`.** Mount your `krb5.conf` and the
service account's keytab into the container, and point Kerberos at them.
Given a client keytab, Kerberos gets a ticket from it when a node first
connects, and a new one when it expires:

```yaml
# added to compose.yaml (swarm.yaml and Kubernetes take the same two
# variables, with the files as secrets)
services:
  hub:
    environment:
      KRB5_CONFIG: /run/secrets/krb5.conf
      KRB5_CLIENT_KTNAME: /run/secrets/exuvia.keytab
    secrets: [krb5.conf, exuvia.keytab]

secrets:
  krb5.conf:
    file: ./krb5.conf
  exuvia.keytab:
    file: ./exuvia.keytab
```

Nodes see these variables (exuvia keeps only its own `EXUVIA_` settings
from them), and the ticket is cached for the user exuvia runs as. Set them
where the nodes run: on a remote executor's container, not the hub's. The
keytab has to be readable by uid 10001.

**Your own wheels next to it.** An image built on exuvia's (see
[Packages your nodes import](#packages-your-nodes-import)) can add wheels
to `/opt/exuvia/wheels` and the package's name to the lists in
`/etc/uv/uv.toml`. A `UV_FIND_LINKS` set on the server replaces the file's
`find-links` instead of adding to it, so list `/opt/exuvia/wheels` in it too.

### Package installs on an isolated network

A hub on a network with no route out (an internal-only Swarm overlay, a
Kubernetes namespace whose egress is denied) can't resolve or reach a
package index, so every install fails with `error resolving DNS`. To let
installs out without opening the hub's network, put an HTTPS proxy that
allows CONNECT next to it, give only the proxy a route out, and set
`HTTPS_PROXY` on the hub. The proxy resolves and connects to the index; the
hub only ever connects to the proxy, so the network in front of the hub (the
sign-in proxy, or the token) stays the only way in.

On Swarm, with [Squid](https://www.squid-cache.org/) allowing only the
index and the hosts that serve its files:

```text
# squid.conf
http_port 3128
acl SSL_ports port 443
acl CONNECT method CONNECT
acl package_hosts dstdomain pypi.example.com pypi.org files.pythonhosted.org
http_access deny CONNECT !SSL_ports
http_access allow CONNECT package_hosts
http_access deny all
```

```yaml
# added to swarm.yaml
services:
  hub:
    environment:
      HTTPS_PROXY: http://egress:3128
      NO_PROXY: localhost,127.0.0.1,.internal.example.com
      UV_INDEX: internal=https://pypi.example.com/simple
    networks: [exuvia]          # internal only: no route out
  egress:
    image: ubuntu/squid:latest  # pin a version you have checked
    configs:
      - source: squid
        target: /etc/squid/squid.conf
    networks: [exuvia, outside] # the one service that reaches out

configs:
  squid:
    file: ./squid.conf

networks:
  exuvia:
    driver: overlay
    internal: true
  outside:
    driver: overlay
```

Delete the hub's `ports` block and reach it through a proxy on the internal
network (Traefik or the sign-in proxy, as under "Behind a proxy" in
`swarm.yaml`): a published port attaches the hub to Swarm networks that do
route out.

`HTTPS_PROXY` names the proxy with `http://`: that is how uv talks to the
proxy, and the connection to the index inside it is still TLS. Put the hosts
the hub has to reach directly in `NO_PROXY`, comma-separated (a leading dot
covers a domain's subdomains): an index on the internal network, a
remote executor, a model gateway or S3 endpoint inside the network (a
`postgresql://` database is not HTTP and never goes through it). exuvia's
own outbound HTTPS from the hub follows the same variables, so the model
provider (`api.anthropic.com`, `openrouter.ai`, …), the artifact store,
failure webhooks and the PyPI update check go through the proxy too: add the
ones you use to the allowlist or to `NO_PROXY`. A proxy that decrypts TLS
needs its CA certificate in the hub's trust store, or
`SSL_CERT_FILE=/path/to/ca.pem` set for uv. On Kubernetes the same layout is
a proxy Deployment and Service, a NetworkPolicy that lets only the proxy's
pods out, and the two variables on the hub's container.

Check it from inside the running hub, where installs run, not from your
machine or the node. On the node that runs the task (`docker service ps
exuvia_hub` names it):

```bash
docker exec -it $(docker ps -q -f name=exuvia_hub) sh -c 'env | grep -i _proxy; echo acme-models | uv pip compile --no-cache -'
```

(`kubectl -n exuvia exec deploy/exuvia-hub -- sh -c '…'` on Kubernetes.)
With the proxy working, uv prints the resolved versions, or a resolver
error that names the package, which means it reached the index. What the
errors mean:

- `error resolving DNS` naming the index: the hub isn't using the proxy. The
  `env` line printed no `HTTPS_PROXY`, or the index's host matches
  `NO_PROXY`. Variables set on the stack only reach a task started after the
  change, so redeploy.
- An error that names the proxy (`egress`) as the host it can't resolve or
  connect to: the hub and the proxy aren't on a network they share, or the
  proxy isn't running.
- A 403 from the proxy, or the connection closed right after `CONNECT`: the
  host isn't on the proxy's allowlist. The error names the host, often the
  one serving the package files.
- A 401 or 403 from the index: the network works and the credentials don't
  (see [Packages](#packages)).

Requests a node makes itself, to a data provider's API, go out the same way
but from wherever the node runs: see
[Nodes that can't reach their data](#nodes-that-cant-reach-their-data).

### Nodes that can't reach their data

A package that imports and a node that can't reach its data provider are
two different problems. The install ran on the hub, through a route to the
package index; the node's own requests leave from wherever the node runs,
to a host that route may not allow. Each node runs in a subprocess of the
process that runs it, with that process's environment:

- **Local executor (the default):** the server's: the hub's container, or
  the machine running `exuvia serve`, which also reads the project's `.env`.
- **Remote executor:** the executor container's, on the executor's network.
  The control plane's environment never reaches it, and the bundle it
  receives leaves out hidden files such as `.env`, so `HTTPS_PROXY` and
  `NO_PROXY` have to be set on the executor itself. The provider's key can
  be too, or go in the project's Environment, which travels with each run
  (see [Environment variables and secrets](#environment-variables-and-secrets)).

So a `curl` in a terminal on your machine, or `docker exec` into the hub,
can succeed while the node fails. Check from the node instead: this one
runs through the same path as the node that fails. Put the provider's API
host in `HOSTS` (`host` or `host:port`: no path, no key), save it as
`nodes/net_check.py` and run it from the workbench, so it runs on the same
executor:

```python
"""Can nodes reach their data providers from where they run? Read-only."""
import exuvia

HOSTS = ["api.example-data.com"]


@exuvia.node()
def net_check():
    import socket
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    import pandas as pd

    proxies = urllib.request.getproxies()  # HTTPS_PROXY / NO_PROXY, as this process sees them
    rows = []
    for host in HOSTS:
        proxy = None if urllib.request.proxy_bypass(host) else proxies.get("https")
        p = urllib.parse.urlsplit(proxy) if proxy else None
        route = f"proxy {p.hostname}:{p.port}" if p else "direct"  # never the proxy's credentials
        try:
            # GET with no credentials; the body is never read.
            with urllib.request.urlopen(f"https://{host}/", timeout=15) as r:
                result, detail = "http", r.status
        except urllib.error.HTTPError as e:  # the provider answered: the route works
            result, detail = "http", e.code
        except (urllib.error.URLError, OSError) as e:
            err = getattr(e, "reason", e)
            text = str(err)
            if isinstance(err, socket.gaierror):
                result, detail = "dns", "proxy name" if p else "provider name"
            elif text.startswith("Tunnel connection failed"):
                result, detail = "proxy refused", text.split(":", 1)[1].split()[0]  # the status code
            elif isinstance(err, ssl.SSLError):
                result, detail = "tls", type(err).__name__
            elif isinstance(err, TimeoutError):
                result, detail = "timeout", ""
            else:
                result, detail = "connect", type(err).__name__
        rows.append({"host": host, "route": route, "result": result, "detail": str(detail),
                     "ran_on": socket.gethostname()})
    return pd.DataFrame(rows)
```

It sends one GET with no credentials to each host and keeps only the
status: no headers, no body. Its output has one row per host: `route` (`direct`,
or the proxy's host and port, never its credentials), `result`, `detail`
(a status code or an error's name) and `ran_on`, the host name of the
machine or container it ran in, which tells you whether it ran on the
executor you meant. Delete the node when you are done.

| `result` | What it means | Where to fix it |
|---|---|---|
| `http` | The provider answered with the status in `detail`. The route works; a 401 or 403 is the provider turning away a request with no key. | Not the network. If the node still fails, look at its key (on the executor, for a remote one) or the request it makes. |
| `proxy refused` | The proxy took the connection and refused the tunnel; `detail` is its status. 403: the host isn't on its allowlist. 407: it wants credentials. 502, 503 or 504: the proxy itself couldn't resolve or reach the provider. | The proxy: add the host to its allowlist (the Squid example above allows only the package hosts), or fix the proxy's own route out. A provider that redirects, or serves downloads from another host, needs those hosts too. |
| `dns`, `proxy name` | The proxy's name doesn't resolve where the node runs. | The node and the proxy aren't on a network they share: often a remote executor given the hub's `HTTPS_PROXY`. |
| `dns`, `provider name` | The request went direct and the provider's name doesn't resolve: no route out. | `HTTPS_PROXY` where the node runs, or take the host out of `NO_PROXY`. A running server or executor doesn't see new variables: restart or redeploy it. |
| `timeout`, `connect` | The name resolved and the connection timed out, was refused or was reset. Through a proxy, a reset or `RemoteDisconnected` is often the proxy refusing the tunnel without a status, as some do for a host or port they don't allow. | A firewall or NetworkPolicy dropping the traffic, the wrong port in `HTTPS_PROXY`, a proxy that isn't running, or the proxy's allowlist. |
| `tls` | A proxy that decrypts TLS, whose CA isn't trusted where the node runs. | `SSL_CERT_FILE=/path/to/ca.pem` on the server or executor (`requests` reads `REQUESTS_CA_BUNDLE` instead). |

The check uses Python's `urllib`, which reads `HTTPS_PROXY` and `NO_PROXY`
much as `requests` and `httpx` do. A provider's SDK with a proxy setting of
its own can choose differently: see its docs.

**A gRPC client may need a different route.** Some providers' SDKs speak
gRPC (`grpcio`), not HTTPS, and it finds its proxy its own way. It reads
only the lowercase `grpc_proxy`, `https_proxy` or `http_proxy`, and
`no_grpc_proxy` or `no_proxy`: where only `HTTPS_PROXY` is set, gRPC goes
direct while the check above goes through the proxy, so set the lowercase
names as well. Its tunnel is to the gRPC endpoint's own port, which isn't
always 443 (the Squid example refuses any other port: add it to
`SSL_ports`), and a proxy that decrypts TLS has to support HTTP/2 to carry
it. Where the proxy can't, give the executor a direct egress rule for the
endpoint and list it in `no_grpc_proxy`. Add the endpoint as `host:port`
to `HOSTS` to check the tunnel: `dns`, `proxy refused` and `timeout` mean
what they mean above, while an error past the tunnel may only be a gRPC
server declining a plain GET.

**Data only one desktop can read** (a Bloomberg Terminal, a licensed
desktop app, a file share only the office reaches) is no route to fix: make
it a [push node](#push-nodes-data-from-someones-desktop) and push it from
that machine.

### Schedules, auth and settings

**Hosts that sleep or scale to zero.** An in-process scheduler can't fire while
its process is asleep. Run the control plane with `--scheduler external` and
have something call `POST /api/schedule/tick` every minute or so: a Cloudflare
Durable Object alarm or Cron Trigger, a Kubernetes CronJob, or system cron.
Ticks are idempotent; fires missed while asleep are coalesced into one
catch-up run, for the latest missed slot (and its date), and paused schedules
skip their backlog. Replay any other missed dates with `POST /api/backfill`.

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

**Version and updates.** The **?** button in the workbench's top bar shows
the running version and whether a newer release is out (`GET /api/about`).
A new release is never a toast or a banner: the button gets a small dot until
someone opens the menu, and the menu says how to update. On someone's own
install that is the command for how it was installed (`uv tool upgrade
exuvia`, `pipx upgrade exuvia`, `uvx exuvia@latest …`, ...). On a deployed
server it says that whoever runs the server updates it by deploying the new
image, since the person looking at the workbench usually can't. The image
sets `EXUVIA_DEPLOYED=1` to mark a deployed server; set it yourself on one
installed another way. The server asks PyPI at most every 6 hours, and only
while someone has the workbench open; `EXUVIA_UPDATE_CHECK=off` turns the
check off, for a server that should not reach the internet.

**Auth.** Set `EXUVIA_API_TOKEN` on a hosted control plane: API clients send
`Authorization: Bearer <token>`; browsers open `/?token=<token>` once and get
an HttpOnly cookie. On a hub that only a private network can reach, set
`EXUVIA_BROWSER_AUTH=open` so people never handle the token: every page load
hands the browser that cookie (`SameSite=Lax`, so other websites can't send
requests with it), while API calls without the cookie or the token still get
a 401, so agents and MCP clients keep using the token. It lets in anyone who
can load the page, so use it only behind a VPN or an internal-only address. Always set `EXUVIA_EXECUTOR_TOKEN` on a networked executor
— it runs arbitrary code by design.

**Signed-in users from a proxy.** When something in front of exuvia
already signs people in (a company SSO gateway, oauth2-proxy, an ingress
with an auth step) and passes who they are in request headers, set
`EXUVIA_AUTH=headers`. exuvia reads the user name from
`X-Forwarded-User` and the email from `X-Forwarded-Email`; set
`EXUVIA_AUTH_USER_HEADER` and `EXUVIA_AUTH_EMAIL_HEADER` to read other
headers (say `Remote-User`). Either header is enough to be signed in. A
request without them gets a 401, the page and the API alike; `/healthz`
stays open. The signed-in person is named in the account menu (the profile
icon at the end of the top bar), on each
run they start (`started_by`; a cascade it sets off names them too, a
scheduled run names nobody) and on each code revision they save (`author`).
`GET /api/me` says who a request is from.

exuvia takes the headers as given, so nothing but the proxy may reach it:
anyone who can talk to exuvia directly can claim to be anyone. Set
`EXUVIA_AUTH_TRUSTED_PROXIES` to the proxy's addresses (IPs or CIDRs,
comma-separated) and exuvia ignores the headers on requests from anywhere
else. The proxy must also drop any of these headers a client sends itself.
`EXUVIA_API_TOKEN` still works alongside: agents and MCP clients that
cannot pass through the proxy send the token instead, and are recorded as
nobody. `EXUVIA_BROWSER_AUTH` has no effect in this mode.

**Agent tokens.** Signed-in people connect their own agents with a token of
their own, so nobody has to handle the shared `EXUVIA_API_TOKEN`. In the
account menu (the profile icon in the top bar) on a hub, **Connect an
agent** makes one (name it for where it goes, such as "Claude Code on my laptop") and shows the commands for Claude Code,
Codex, a JSON `mcpServers` entry or a prompt for any agent, with the token
already in them: copy one, paste it, done. They connect to the hub's own
MCP endpoint, `<hub>/mcp`, so nothing is installed. The token (`exv_...`) is
sent as `Authorization: Bearer` (with `exuvia mcp --url`, through
`EXUVIA_API_TOKEN` in its environment), and signs the agent in as the person
who made it, whatever its address: runs and revisions name them, an admin's token is an admin and a
member's is a member, and disabling the person shuts out their tokens too.
It is shown once (exuvia keeps only a hash), and the same dialog lists each
person's tokens, with when each was last used, and revokes them. A request
made with a token cannot make or revoke tokens, so a leaked one cannot mint
more. Scripts use the routes behind it: `GET`/`POST /api/me/tokens` and
`DELETE /api/me/tokens/{id}`, signed in through the proxy. If the agents
reach exuvia through the sign-in proxy, the proxy has to pass their requests
through to exuvia untouched, since exuvia checks the token itself: at least
the MCP endpoints, `/mcp` and `/p/<project>/mcp` (oauth2-proxy:
`--skip-auth-route='^/mcp/?$' --skip-auth-route='^/p/[^/]+/mcp/?$'`; or an
ingress rule that routes those paths around the auth step), and for scripts
and `exuvia mcp --url`, requests that carry an `Authorization: Bearer`
header. That is safe: exuvia turns away a request to those paths without a
valid token (401), whatever the proxy did. Behind such a proxy, a client
that the proxy stops gets the proxy's answer (a redirect to sign in, or its
own 401), not exuvia's. A hub that nobody signs in to keeps the
**Connect agent** button in its top bar, for the shared token. Hub exports carry the tokens' hashes with the user
list, so tokens keep working after an import.

**Users and admins.** Everyone who signs in is recorded in a user list on
their first request, with no approval step: the sign-in proxy already
decides who gets in. Each user has a stable id, the name and email their
latest sign-in gave, a role (`admin` or `member`), whether they are
disabled, and when they were first and last seen. Runs and revisions link to
that id as well as the name (`started_by_user`, `author_user`), so a renamed
person keeps their history. A user can have several *identities*, one per
way of signing in (the headers now, SSO later); a new identity whose email
an existing user already has joins that user, so adding a sign-in method
later does not split anyone in two.

`EXUVIA_ADMINS` (user names or emails, comma-separated) names the first
admins. They are admins whenever they sign in, and nobody can demote or
disable them through exuvia, so the hub always has a way back in. On a
hub, an admin finds **Users** on the admin page (`/admin`: **Admin** in
the hub's left nav, or in the account menu); on
`exuvia serve` it is **Users…** in the account menu. There, everyone who has signed in is listed, and an admin
can make others admins or members and disable or re-enable them. A
disabled user gets a 403 even though the proxy lets them through, which
locks them out of exuvia without touching the company sign-in. Nobody can
demote or disable themselves. A caller with the API token counts as an
admin for the user routes (`GET /api/users`, `PATCH /api/users/{id}`), so a
script can manage users too.

The list belongs to the server, not to a project: a hub keeps one list for
all its projects, in `.exuvia/hub.db` under the hub's directory (hub exports
carry it, and an import restores it with roles and disabled users), or in
the shared `EXUVIA_DB` under `EXUVIA_DB_SCOPE` when one is set. `exuvia
serve` keeps it in the project's database. Each server remembers a sign-in
for a minute, so with several replicas on one database a disabled user can
take up to a minute to be turned away by the others. Without `EXUVIA_AUTH`,
there is no user list.

**Secrets from files.** Each setting that holds a secret can come from a file
instead: set `<NAME>_FILE` to its path, such as
`EXUVIA_API_TOKEN_FILE=/run/secrets/exuvia_api_token` for a Docker or Swarm
secret, or a file in a Kubernetes secret volume. Surrounding whitespace is
dropped. This works for `EXUVIA_API_TOKEN`, `EXUVIA_EXECUTOR_TOKEN`,
`EXUVIA_DB`, `EXUVIA_FAILURE_WEBHOOK`, `EXUVIA_SECRETS_KEY`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `OPENROUTER_API_KEY`, `PORTKEY_API_KEY`, `OPENAI_API_KEY`, the
`AWS_*` keys, and the package index credentials `UV_INDEX_<NAME>_USERNAME`
and `UV_INDEX_<NAME>_PASSWORD` (see "Packages"). Setting both forms to different values, or naming a missing or
empty file, stops exuvia with an error.

| Env | Default | |
|---|---|---|
| `EXUVIA_EXECUTOR_URL` / `EXUVIA_EXECUTOR_TOKEN` | local pool | remote executor + its token |
| `EXUVIA_SCHEDULER` | `internal` | `external` = only `POST /api/schedule/tick` advances schedules |
| `EXUVIA_API_TOKEN` | off | shared-secret auth for the control plane API |
| `EXUVIA_AUTH` | `token` | `headers` = a sign-in proxy in front says who each request is from (see "Signed-in users from a proxy" above) |
| `EXUVIA_AUTH_USER_HEADER` / `EXUVIA_AUTH_EMAIL_HEADER` | `X-Forwarded-User` / `X-Forwarded-Email` | the headers that carry the user name and email with `EXUVIA_AUTH=headers` |
| `EXUVIA_AUTH_TRUSTED_PROXIES` | any peer | IPs or CIDRs (comma-separated) whose identity headers are trusted; others are ignored |
| `EXUVIA_ADMINS` | — | user names or emails (comma-separated) who are always admins of the user list (see "Users and admins" above) |
| `EXUVIA_DEFAULT_ACCESS` | `view` | on a hub: what everyone signed in gets on a new project unless its creator picks otherwise (`private`, `view` or `edit`). Unset, a project nobody owns, or with no settings of its own, gets `edit`; set, it applies to those too (see "Sharing projects" under [Hub](#hub-many-projects-from-one-process)) |
| `EXUVIA_BROWSER_AUTH` | `token` | `open` = every page load gives the browser the auth cookie, so people never enter the token (private networks only; see "Auth" above) |
| `<NAME>_FILE` | — | read a secret setting from a file, e.g. `EXUVIA_API_TOKEN_FILE=/run/secrets/exuvia_api_token` (see "Secrets from files" above) |
| `EXUVIA_SECRETS_KEY` | a key per project | the key (or `new,old` keys) that encrypts the projects' secrets; make one with `exuvia secrets-key` (see "Environment variables and secrets") |
| `EXUVIA_FAILURE_WEBHOOK` | off | URL that failed runs are POSTed to, for projects without their own `failure_webhook` |
| `EXUVIA_FAILURE_TRIGGERS` | `schedule,cascade` | which failed runs the env webhook reports (`all`, or any of `schedule`, `cascade`, `manual`, `backfill`) |
| `EXUVIA_PUBLIC_URL` | — | where this server is reachable (e.g. `https://exuvia.example.com`), for run links in failure webhooks |
| `EXUVIA_MAX_PARALLEL` | 4 | concurrent node runs (control plane and executor) |
| `EXUVIA_RUN_TIMEOUT` | 1800 | seconds before a node run is killed |
| `EXUVIA_BUNDLE_MAX_MB` | 50 | max project size shipped to a remote executor |
| `EXUVIA_AGENT_PACKAGES` | `ask` | what the agent may do with packages: `ask` (requests wait for a person to approve them), `install`, or `off` (see "Packages") |
| `EXUVIA_PACKAGES_CONCURRENCY` | 2 | package installs that run at once (a hub starting with many projects to install) |
| `EXUVIA_PACKAGES_TIMEOUT` | 900 | seconds before a package install is stopped |
| `EXUVIA_NPM_CDN` | `https://esm.sh` | where the npm packages views import are fetched from: an esm.sh, such as one you host (see "npm packages for views") |
| `UV_INDEX`, `UV_INDEX_<NAME>_USERNAME` / `_PASSWORD` | PyPI only | a package index for every project, and its credentials (see "Packages") |
| `HTTPS_PROXY` / `NO_PROXY` | direct | the proxy that package installs, exuvia's other outbound HTTPS and nodes' own requests go through, and the hosts reached without it (see "Package installs on an isolated network" and "Nodes that can't reach their data" above) |
| `EXUVIA_EXECUTOR_DIR` | temp | executor scratch space |
| `EXUVIA_CHUNK_TTL_DAYS` | 7 | days an executor keeps a partitioned input's file that no run has used |
| `EXUVIA_PROVIDER` | auto | `anthropic`, `openrouter`, `portkey`, or `openai` (any OpenAI-compatible server) |
| `EXUVIA_MODEL` | per provider | agent model (see below) |
| `EXUVIA_EFFORT` | `medium` | Claude effort level |
| `EXUVIA_BASE_URL` | OpenRouter | base URL for the `openai` provider (Ollama, vLLM, LiteLLM, …) |
| `PORTKEY_BASE_URL` | `https://api.portkey.ai/v1` | a self-hosted Portkey gateway, for the `portkey` provider |
| `PORTKEY_CONFIG` / `PORTKEY_PROVIDER` / `PORTKEY_VIRTUAL_KEY` | — | sent as `x-portkey-config`, `x-portkey-provider` and `x-portkey-virtual-key` (see "The agent") |
| `EXUVIA_AGENT_MCP` | `on` | `off`: the agent uses no outside MCP servers, and `/api/me/mcp` answers 404 (see "The agent") |
| `EXUVIA_AGENT_MCP_PRIVATE` | off | `1`: the agent's MCP servers may be on private addresses on a server that needs a sign-in or a token |
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
"Views"), `run_node` (waits up to `wait_seconds`, default 240, then returns
the run id of a run still going), `list_runs`, `read_run` (run history: what
a schedule or a cascade ran, with each run's error and log, or the log so far
of one still going) and `cancel_run`, `define_param` /
`delete_param`, `query_data` (DuckDB over every node's current output),
`list_packages`, `request_packages` and `add_view_packages` (see "Packages"), `list_env` and
`request_secret` (the project's environment variables by name, and asking
you to set one: the agent never sees a secret's value; see "Environment
variables and secrets"), `docs`, `version` and
`report_issue` (see "MCP"). It looks at real column names before writing, runs
what it writes, reads the traceback when something fails, and fixes it. Every
tool call shows up in the chat, and nodes it touches flash on the canvas.

Pick the model with environment variables; the provider is auto-detected from
whichever key is set (in this order: Claude, OpenRouter, Portkey):

| Provider | Key | Default model |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` (adaptive thinking, server-side fallback) |
| `openrouter` | `OPENROUTER_API_KEY` (or `OPEN_ROUTER_KEY`) | `deepseek/deepseek-v4-pro` |
| `portkey` | `PORTKEY_API_KEY` + `EXUVIA_MODEL` | none (must be set) |
| `openai` | `OPENAI_API_KEY` + `EXUVIA_BASE_URL` + `EXUVIA_MODEL` | none (must be set) |

```bash
# DeepSeek through OpenRouter
OPENROUTER_API_KEY=sk-or-... exuvia serve my-project
# any other OpenRouter model
EXUVIA_PROVIDER=openrouter EXUVIA_MODEL=qwen/qwen3-coder exuvia serve my-project
# a local model
EXUVIA_PROVIDER=openai EXUVIA_BASE_URL=http://localhost:11434/v1 EXUVIA_MODEL=qwen3:32b OPENAI_API_KEY=x exuvia serve .
# through a Portkey gateway: the model names a provider from your Model Catalog
PORTKEY_API_KEY=... EXUVIA_MODEL=@anthropic-prod/claude-opus-5-5 exuvia serve my-project
```

**Portkey.** The `portkey` provider sends the agent through the
[Portkey](https://portkey.ai) AI gateway, so its calls get your
organization's routing, budgets, logs and guardrails. Name the model as
`@<provider-slug>/<model>` from Portkey's Model Catalog. The calls use
Portkey's OpenAI-compatible API, so Claude through Portkey runs without the
`anthropic` provider's adaptive thinking and server-side fallback. Optional
settings, each sent as a gateway header:

- `PORTKEY_CONFIG`: a config id (`pc-...`) or JSON, for fallbacks, load
  balancing, caching or guardrails (`x-portkey-config`).
- `PORTKEY_PROVIDER`: the provider slug, when `EXUVIA_MODEL` is a bare model
  id (`x-portkey-provider`).
- `PORTKEY_VIRTUAL_KEY`: a virtual key, for accounts that still use them
  (`x-portkey-virtual-key`).
- `PORTKEY_BASE_URL`: a self-hosted gateway, such as
  `http://portkey-gateway:8787/v1`.

A `PORTKEY_API_KEY` on its own does not turn the agent on: auto-detection
picks Portkey only once `EXUVIA_MODEL` is set as well.

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
also published on `/api/events`, so a UI can reload and re-attach. The chat
does: a reload brings back the conversation that was open, and follows its
turn if one is still running. **New chat** keeps the one before, and a new
chat lists the latest under **Recent chats**, titled by what was first asked,
to pick up again. `GET /api/agent` lists them; a signed-in person sees the
conversations they started.

**Outside MCP servers.** The agent can use other tools as well as exuvia's
own: your tracker, your docs, or your company's tools behind
[Portkey's MCP gateway](https://portkey.ai/docs/product/mcp-gateway). Open
**MCP servers** (the plug in the agent panel's header) and add a remote MCP
server (Streamable HTTP) with a name, its URL and the headers that sign you
in: `Authorization: Bearer ...` for most servers, or `x-portkey-api-key` for
a Portkey gateway URL such as `https://mcp.portkey.ai/<server>/mcp`. **Test**
connects and lists its tools. The servers are connected for each message you
send, and their tools reach the model as `<name>__<tool>`. A server that
fails to connect is left out of that turn, and the agent tells you why.

- **Each person has their own list.** On a server where people sign in, your
  servers are used only in your chats, and nobody else sees them (an agent
  token reads its owner's list but cannot change it). On a server nobody
  signs in to, such as a local `exuvia serve`, there is one list for
  everyone who can open it. A share link alone has none.
- **Servers on offer.** An admin can offer servers to everyone on the
  server: on a hub, **MCP servers** on the admin page (`/admin`). Each has a
  name, a URL, a line on what it is and the names of the headers each
  person fills in (none for an OAuth server). Nobody's agent uses one until
  they press **Connect** under "On offer here" in the agent's MCP servers
  menu, which puts a copy on their own list with the
  values they type; from then on it is theirs. Changing an offer, or taking
  it back, leaves the copies alone. An offer holds no secret. The API:
  `GET /api/mcp/presets` for anyone, `PUT` and `DELETE
  /api/mcp/presets/{name}` for an admin (`{"url", "description",
  "headers": [names]}`), and `POST /api/me/mcp/presets/{name}/connect` with
  `{"headers": {name: value}}`.
- **Header values are write-only.** The API (`GET`, `PUT` and `DELETE
  /api/me/mcp/{name}`, `POST /api/me/mcp/{name}/test`) never returns them:
  a listing shows each header's name and its value's last 4 characters. They
  are kept in clear in the server's database, beside the user list (the
  hub's `.exuvia/hub.db`, the project's database, or the shared
  `EXUVIA_DB`), so anyone who can read that database can read them. A hub
  export leaves them out.
- **OAuth sign-in.** A server that signs you in with OAuth (MCP's
  authorization spec) needs no header: add it with its URL alone, and when
  **Test** finds it asking for a sign-in, **Sign in** opens its sign-in page
  in a popup. exuvia registers itself with the server's authorization server
  (dynamic client registration), the page sends you back to
  `/api/me/mcp/oauth/callback` on this exuvia server, and the tokens are kept
  beside your headers, as secret as they are. Each message sends the access
  token and refreshes it when it runs out; when that no longer works the
  agent says to sign in again. **Sign out** forgets the tokens, and so does
  changing the server's URL. The API: `POST /api/me/mcp/{name}/oauth` with
  `{"origin": "<this page's origin>"}` answers `{url}` to open, `DELETE`
  signs out. A sign-in in progress lives in the exuvia process that started
  it, so behind several replicas the callback must reach the same one
  (sticky sessions).
- **Where a server may point.** Any public address. On a server that needs
  a sign-in or a token, addresses on the machine itself, the private network
  or a cloud's metadata service are refused unless
  `EXUVIA_AGENT_MCP_PRIVATE=1`, for the MCP server and for the OAuth
  endpoints it names alike. A local server with no auth allows them, so an
  MCP server on your own machine works. `EXUVIA_AGENT_MCP=off` turns the
  feature off.
- **Not yet:** local servers started as a command (stdio).

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
keeps the token itself out of the MCP client's configuration. A server with
`EXUVIA_AUTH=headers` turns away callers without the sign-in proxy's headers:
there, each person's own agent token goes in `EXUVIA_API_TOKEN` (see "Agent
tokens" under [auth](#schedules-auth-and-settings)), and the agent works as
them.

**Over HTTP, with nothing installed.** `exuvia serve` and `exuvia hub` are
MCP servers themselves, over Streamable HTTP: a client needs only the
address. `<hub>/mcp` serves every project on a hub (the tools described
under "A whole hub" below), `<hub>/p/<project>/mcp` just that one, and
`<server>/mcp` an `exuvia serve`'s project. The tools run in the server, as
`POST /api/tools/{name}` does, behind the same auth as the API: send the
API token or your agent token as `Authorization: Bearer`, and the agent
works as you. The hub's **Connect an agent** writes these commands out with
your token in them:

```bash
claude mcp add --transport http --scope user exuvia https://hub.example.com/mcp --header "Authorization: Bearer exv_..."
codex mcp add exuvia --url https://hub.example.com/mcp --bearer-token-env-var EXUVIA_API_TOKEN
```

```json
{"mcpServers": {"exuvia": {"type": "http", "url": "https://hub.example.com/mcp", "headers": {"Authorization": "Bearer exv_..."}}}}
```

Codex reads the token from an environment variable, so set
`EXUVIA_API_TOKEN` where Codex starts (`export` in your shell profile, or
`setx` on Windows). Behind a sign-in proxy, the proxy has to let `/mcp` and
`/p/<project>/mcp` through without signing in (see "Agent tokens" under
[auth](#schedules-auth-and-settings)). The endpoint is stateless: every
request stands alone, so any replica can answer it. It can't ask the client
anything back, so when the agent asks for a secret (`request_secret`), the
link to set it comes in the tool's answer rather than as a prompt in the
client (see [When the agent needs a key](#when-the-agent-needs-a-key)). `exuvia mcp --url` (the
local relay, below) still works, and is how a client reads the shared API
token from a file (`EXUVIA_API_TOKEN_FILE`).

**A whole hub.** Given a hub's own address (its `/healthz` says `"mode":
"hub"`), `exuvia mcp --url` serves every project on it: `list_projects`,
`create_project`, and each project tool with a required `project` argument
(the project's id); `guide` with `project` returns that project's
instructions, and without it describes the hub and lists its projects.
`docs` and `version` are about exuvia rather than a project, so they take no
`project`. One
MCP server then covers all of them, including ones created later. A hub's
home page writes this setup out (**Connect agent**), with a person's own
agent token already in it on a hub where people sign in. For an agent that should
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

**Docs.** The `guide` covers building in a project; for everything else
(the `exuvia` command line, updating exuvia, deploying a hub, auth and agent
tokens, package indexes, executors, the HTTP API, setting up MCP itself) the
agent has a `docs` tool. It reads this README
as the installed version ships it, so the answer matches the exuvia that is
running and needs no network: with no input it returns the table of
contents, with `topic` one section (`Kerberos`, `Deploying > Kerberos` or an
anchor such as `#kerberos`; a section too long to send at once comes back as
its opening and a list of its subsections), and with `query` the passages
that best match some words. The agent's instructions tell it to look there
rather than answer such questions from memory. Clients that can fetch web
pages also have [llms.txt](https://exuvia.lobster.mp/llms.txt), which
describes the latest release.

**Version.** The `version` tool tells the agent which exuvia is running, how
it was installed, whether a newer release is out and the command that
updates it (see [Updating exuvia](#updating-exuvia)), so it can update
exuvia when you ask without a separate skill. Through `exuvia mcp --url` it
answers for both the server and the `exuvia` on your machine. The agent
updates nothing unless you ask.

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

## Command line

Every `exuvia` command, with or without an install (`uvx exuvia …` runs the
same commands). `exuvia <command> --help` prints a command's options; the
section named after each one has the details.

| Command | What it does | More |
| --- | --- | --- |
| `exuvia init DIR` | scaffold a project: `nodes/`, a first node, `pyproject.toml` | Quick start |
| `exuvia serve [DIR]` | the workbench for one project: UI, API, MCP at `/mcp`, scheduler. `--host`, `--port` (7420), `--executor URL`, `--scheduler` | Quick start, Deploying |
| `exuvia hub DIR` | every project under `DIR` from one process. `--templates DIR`, and the options of `serve` | Hub |
| `exuvia run [DIR] [NODE…]` | run stale nodes (or the named ones and their stale upstream) without the UI; non-zero exit when one fails. `--force`, `--param NAME=VALUE` | below |
| `exuvia status [DIR]` | each node's freshness. `--json`, `--param NAME=VALUE` | below |
| `exuvia mcp [DIR]` | the agent's tools over MCP on stdio: the project in `DIR`, or `--url URL` to relay to a running server or hub | MCP |
| `exuvia push NODE FILE --url URL` | upload a push node's output (`.parquet`, `.csv`, `.json`) from the machine that has the data. `--param`, `--note`, `--allow-http` | Push nodes |
| `exuvia cancel RUN_ID --url URL` | stop a queued or running run of a running exuvia (a hub project or `exuvia serve`); sends `EXUVIA_API_TOKEN`. `--allow-http` | Stopping a run |
| `exuvia env list`, `set`, `rm` | a project's (or, with `--group`, a hub group's; with `--hub`, the hub's) environment variables and secrets, in a folder or over `--url` | Environment variables and secrets |
| `exuvia executor` | serve node execution over HTTP for a control plane. `--host`, `--port` (8080) | Architecture |
| `exuvia secrets-key` | print a new key for `EXUVIA_SECRETS_KEY` | Encryption |

### Headless runs

```bash
exuvia run examples/markets                 # run everything stale
exuvia run examples/markets returns --force # force one node (+ stale upstream)
exuvia status examples/markets
```

`exuvia run` exits non-zero when any node fails, so it drops straight into CI
or a system cron.

### Updating exuvia

How to update depends on how exuvia was installed:

| Installed with | Update with |
| --- | --- |
| `uv tool install exuvia` | `uv tool upgrade exuvia` |
| `pipx install exuvia` | `pipx upgrade exuvia` |
| `pip install exuvia` | `pip install -U exuvia` (in the same environment) |
| `uvx exuvia …` (no install) | `uvx exuvia@latest …`: a plain `uvx exuvia` keeps running the copy it cached, or the `uv tool` install when there is one |
| a clone (`uv sync`) | `git pull && uv sync` |
| the image (`ghcr.io/rlancer/exuvia`) | deploy the new tag; whoever runs the server does this |

A running exuvia keeps its version until it restarts: restart `exuvia serve`
or `exuvia hub` after updating, and restart the MCP client (or its exuvia
server) for `exuvia mcp`. A project's records database written by an older
exuvia is upgraded in place when it opens. The workbench's **?** menu says
when a newer release is out (see "Version and updates" under
[Schedules, auth and settings](#schedules-auth-and-settings)), and the agent's
`version` tool answers the same for an agent: the running version, how it was
installed, the latest release and the command that updates it. Through
`exuvia mcp --url` it answers for both the server and the `exuvia` on the
agent's machine, which can differ.

## Hub: many projects from one process

`exuvia hub <dir>` serves every project under one directory, each with the
full API under its own prefix and the web UI at `/p/<project>/`. `/` is the
hub's home page: it lists the projects, creates one (from a template, when
the hub has any) and opens its workbench, and deletes one. Every page of
the hub, the workbench included, has the hub's left nav: **Home**, then every
project you can open, the one you used last first, with **+** for a new one,
and last, at its foot, **Admin** (for an admin, with the admin page's
sections under it). It folds to a rail of Home's and Admin's icons; the browser keeps
whether it is folded, separately for the hub's pages and for the workbench,
which starts folded unless the window is wide. Its **Connect
agent** button gives the [MCP](#mcp-the-agents-tools-in-your-own-agent) setup
for the whole hub (one server for every project): a prompt that has your
coding agent add the server itself, the `claude mcp add` and `codex mcp add`
commands, and `mcpServers` JSON for other clients. With a person's agent
token, or on a hub without a token, they connect to the hub's own `/mcp`.
With the hub's shared token, each reads it from a file
(`~/.config/exuvia/<host>.token`, which the page shows how to create) through
`exuvia mcp --url`, never from its own config. It is the shape a
multi-tenant host needs: one process (or one container) per tenant, one
project per workflow. With `EXUVIA_API_TOKEN` set, the home page asks for the
token, or a browser opens `/?token=<token>` or `/p/<project>/?token=<token>`
once; either way it gets the cookie. With `EXUVIA_BROWSER_AUTH=open` as well,
it gets the cookie without asking. With `EXUVIA_AUTH=headers`, the sign-in
proxy in front of the hub says who each browser is, so the home page and
every workbench open without a token, and the account menu names the person (see
"Signed-in users from a proxy" under
[Schedules, auth and settings](#schedules-auth-and-settings)).

```bash
exuvia hub ./projects --templates ./templates --scheduler external
```

| Route | |
|---|---|
| `GET /` | the hub's home page (the web UI): list, create, open and delete projects |
| `GET /admin` | the admin page (the web UI): an overview, users, every project, hub variables, MCP servers on offer, export and import, and the server's settings |
| `GET /api/admin/settings` | how the server is set up, for the admin page: each setting's value and the environment variable that sets it, read-only; secrets show only as set or not set (admins only) |
| `GET /p/{project}/` | that project's workbench (the web UI, talking to `/p/{project}/api`) |
| `GET /api/projects` | list projects (`{projects, templates, auth}`; `auth`: whether clients need the API token) |
| `GET /api/tools` | the project tools, as each project's `/guide` lists them (for `exuvia mcp --url <hub>`) |
| `POST /mcp` | MCP over Streamable HTTP for every project (see "MCP"); `POST /p/{project}/mcp` for that one |
| `POST /api/projects` | create one (`{id, name, template}`; templates come from `--templates` / `EXUVIA_TEMPLATES_DIR`) |
| `PUT /api/projects/{project}/template` | replace its code with a template's (`{template}`): node files and views not in the template are removed (each view folder is replaced whole), the name, run history and stored outputs are kept, and removed files stay restorable from their revisions |
| `DELETE /api/projects/{project}` | remove one |
| `ANY /p/{project}/api/...` | that project's API (everything in the table below) |
| `POST /api/schedule/tick` | evaluate every project's schedules; returns what started and the next due time. A project that fails to load or tick is skipped and named under `errors` (id → reason); the others still tick |
| `GET /api/hub/state` | whether anything is running, and a counter that changes when state does |
| `GET /api/hub/export`, `POST /api/hub/import` | the whole directory as one archive, for hosts with no persistent disk (without outputs already in `EXUVIA_ARTIFACT_STORE`, or records already in a shared `EXUVIA_DB`). With `EXUVIA_AUTH` set it carries the user list too, which an import puts in place of the hub's |
| `GET /api/users`, `PATCH /api/users/{id}` | the user list, for admins (see "Users and admins"): everyone who has signed in with their identities / change one's `role` or `disabled` |
| `GET /api/people?q=` | enabled users whose name or email contains `q` (id, name, email), for choosing who to share with; for anyone signed in |
| `GET /p/{project}/api/access` | what the request may do in the project: `role` (`viewer`, `editor`, `owner`, or null), owner, general access, whether a share link let it in, and its pending access request |
| `GET/PATCH /api/projects/{project}/sharing` | who the project is shared with, its general access and owner (the owner also gets its links and access requests) / change `general` (`private`, `view`, `edit`) or hand it to a new `owner` (a user id) |
| `PUT /api/projects/{project}/sharing/people`, `DELETE .../people/{principal}` | share it with someone (`{user}` or `{email}`, and `role`: `viewer` or `editor`), or change their role / stop sharing with `user:<id>` or `email:<address>`; anyone can take themselves off |
| `POST /api/projects/{project}/sharing/links`, `DELETE .../links/{id}` | a read-only share link (`{name, expires_in_days}`; the answer carries `url`, `/s/<token>`, once) / revoke one |
| `POST /api/projects/{project}/sharing/requests`, `POST .../requests/{user}/approve`, `DELETE .../requests/{user}` | ask the owner for access (`{role, message}`) / grant what was asked (or `{role}`) / dismiss it, or withdraw your own |
| `GET /s/{token}` | open a share link: the browser keeps it in a cookie for that project's pages and API, and lands on its workbench (with `?app=<view>`, on that view as an app) |

With `--scheduler external` nothing fires on its own: the host calls
`/api/schedule/tick` when it wants schedules evaluated and can sleep until the
`next` time it returns.

### Sharing projects

Each project on a hub is one workflow, and it is shared the way a document
is. Sharing needs a hub that people get into: one where they sign in
(`EXUVIA_AUTH=headers`, see "Signed-in users from a proxy"), or one with an
`EXUVIA_API_TOKEN`, which can share by link only. A hub nobody has to get in
to has no sharing: everyone can do everything.

- **Owner.** Whoever creates a project owns it. The owner decides who else
  can open it, can hand it to someone else (they stay on as an editor), and
  is the only one besides an admin who can delete it or replace its code
  with a template.
- **People.** The **Share** button (in the workbench's top bar, and on each
  project on the hub's home page) adds people as a **viewer** or an
  **editor**, by name, or by email for someone who has never signed in: the
  invite takes effect on their first sign-in with that email.
- **General access.** What everyone else signed in gets: nothing ("only
  people added here"), view, or edit. A new project starts at **view**
  unless its creator picks otherwise in the New project dialog: everyone
  signed in can open it, and only its owner and the editors they add can
  change it, run it or use its agent there. Two kinds of project start at
  edit instead, as hubs did before this default, since nobody but an admin
  could change them otherwise: one made by a script with the hub token,
  which has no owner, and one made before sharing existed, which has no
  settings of its own. Both have no owner until an admin hands them to
  someone. `EXUVIA_DEFAULT_ACCESS` (`private`, `view` or `edit`) replaces
  all of these defaults; set it to `edit` for every new project to start
  open to editing, as it did before.
- **Share links.** A link (`/s/<token>`) opens one project read-only for
  whoever holds it, signed in or not: the graph, code, outputs, runs and
  views, but nothing that changes them. Links are never more than view-only,
  since an editing link would let anyone run code on the hub. Each can
  expire, the owner sees when it was last opened, and revoking it shuts it
  at once. exuvia keeps only a hash of each token, and the link's cookie is
  scoped to that one project's paths.
- **Access requests.** Someone who opens a project they can't see gets a
  page to ask for view or edit access, with a note; a viewer can ask for
  edit access from the banner. The owner sees requests on the hub's home
  page and answers them in the Share dialog.

What each role may do:

| | viewer | editor | owner |
|---|---|---|---|
| Open it; read code, outputs, runs, history and views; query outputs; download them | yes | yes | yes |
| Change code, run nodes and backfills, packages, parameters, use the agent | | yes | yes |
| Share it, make links, answer requests, hand it over, delete it | | | yes |

Hub admins and callers with the hub's `EXUVIA_API_TOKEN` are owners of every
project. The project list, `/api/projects`, shows only the projects a person
can open, each with their role, its owner and its general access. The same
rules hold for agents: an agent token works as its person, so the hub's MCP
endpoint lists only that person's projects, and in a project they can only
view, the agent gets only the read-only tools (`list_*`, `read_*`,
`query_data`). Hub export and import cover every project,
so on a hub with sharing they are for admins (and the API token). The export
carries the sharing settings beside the user list, and an import restores
them.

Behind a sign-in proxy, a share link only helps people who aren't signed in
if the proxy lets them through to exuvia: let `/s/`, `/assets/` and
`/p/<project>/` through for requests that carry the `exuvia_link` cookie,
or put links behind the proxy and share them with people who can sign in
(oauth2-proxy: `--skip-auth-route='^/s/' --skip-auth-route='^/assets/'`,
plus `/p/` if links should work without signing in). That is safe in the
same way as the MCP routes: exuvia turns away a request to a project without
a valid sign-in, token or link for that project.

### Groups: shared access and secrets

A group is a folder of projects on a hub. The projects in it share the
group's people and the group's environment variables and secrets, so an API
key many projects need is set once, and a team gets access to all of its
projects at once.

- **Anyone signed in can create one** (**Groups** on the hub's home page)
  and owns it. The owner adds people as a **viewer** or an **editor**, by
  name or by email, as in a project's Share dialog, and can hand the group
  over (the old owner stays on as an editor), rename it or delete it.
  Deleting a group deletes its variables; its projects stay, outside any
  group.
- **A project is in at most one group.** A group role is also a role on
  every project in the group: a group viewer can open each of them, a group
  editor can change and run each of them. The group's owner gets editor on
  them too, not owner: deleting a project and its own sharing stay with the
  project's owner. A person's role on a project is the highest that the
  project's own sharing and its group give them.
- **Variables.** A group's variables work like a project's (same names,
  values, encryption, masking in logs; a secret's value is never shown
  again), and every project in the group gets them. The project's own
  variable of the same name wins, then the group's, then the hub's (see
  [Variables for every project](#variables-for-every-project)), then the
  server's environment. Secrets are encrypted with `EXUVIA_SECRETS_KEY`, or
  else with a key the hub makes in `<hub>/.exuvia/secrets.key`. A request
  from the agent (`request_secret`) stays with the project; setting the
  variable on the group answers it too.

| | group viewer | group editor | group owner |
|---|---|---|---|
| See the group, its people, its projects and its variables' names (and plain values) | yes | yes | yes |
| Open the group's projects | view | edit | edit |
| Set, rotate and remove the group's variables | | yes | yes |
| Add one of your own projects to it; take a project out | | yes | yes |
| Add people, hand it over, rename it, delete it | | | yes |

**Adding a project needs its owner and an editor of the group** (one person
who is both). Both sides give something away: the group's people get their
role on the project, and **anyone who can edit the project can read every
secret the group gives it**, because whoever can change a node can run code
that reads `os.environ`. Masking a secret in logs only stops accidents. So
before adding a project, look at who can edit it (its editors, and everyone
signed in when its general access is *edit*): they will be able to read the
group's secrets. The dialog that adds a project says so. Taking a project
out of its group needs its owner or an editor of the group. A group's page
lists its projects, so its people can see where its secrets go.

**Starting a project in a group.** A group's editors start one from the
group's page (**New project**), or pick the group in the New project dialog;
whoever creates it owns it, so it goes straight in. **Add a project** on the
group's page puts in one of your projects that is already there, and a
project's own row on the home page moves it between groups or takes it out.

Hub admins and the hub's API token own every group. The groups are kept
beside the sharing settings (the hub's `.exuvia/hub.db`, or the shared
`EXUVIA_DB`), and a hub export carries them, with the hub's key file when
there is no `EXUVIA_SECRETS_KEY` (as with each project's key). On a hub
nobody signs in to, groups still share variables; they just have no people.

The API: `GET /api/groups` (the groups you have a role on), `POST
/api/groups` `{name}`, `GET`, `PATCH` `{name, owner}` and `DELETE
/api/groups/{id}`, `PUT /api/groups/{id}/people` `{user | email, role}`,
`DELETE /api/groups/{id}/people/{principal}`, `GET /api/groups/{id}/env`,
`PUT` / `DELETE /api/groups/{id}/env/{name}` `{value, secret}`, `PUT
/api/projects/{project}/group` `{group}` (`null` takes it out), and `group`
in `POST /api/projects` to create a project in a group you edit. The agent's
`list_env` tool gives each variable's `source` (`project`, `group` or
`hub`).

### Variables for every project

Some keys every project on a hub needs: an LLM gateway's key, a
warehouse's address, an error tracker's DSN. Rather than set them in every
project or every group, a hub admin sets them once as **hub variables**
(**Hub variables** on the admin page, `/admin`), and every
project on the hub gets them.

- **They work like a project's variables** (same names, values,
  encryption, masking in logs; a secret's value is never shown again). Where
  a name is set in more than one place the project's own variable wins,
  then its group's, then the hub's, then the server's environment.
- **Only admins change them**: hub admins (`EXUVIA_ADMINS`, or made admin
  in **Users**) and the hub's API token. On a hub nobody signs in to,
  everyone who gets in is the operator and can. Anyone signed in sees their
  names and plain values, never a secret.
- **Everyone who can edit any project on the hub can read them.** Whoever
  can change a node can run code that reads `os.environ` and send it
  anywhere; masking in logs only stops accidents. So "admins only" is about
  who sets a key, not who can use it. Put a key here only when everyone on
  the hub may have it; one for some people belongs in a
  [group](#groups-shared-access-and-secrets).
- **Better than the server's environment** for this: they are set without a
  restart, masked in run logs, reach a remote executor with each run, and
  the Environment dialog and the agent's `list_env` show them, so the agent
  does not ask for a key that is already set.

They are encrypted with `EXUVIA_SECRETS_KEY`, or else with the hub's own
key in `<hub>/.exuvia/secrets.key`, kept beside the groups (the hub's
`.exuvia/hub.db`, or the shared `EXUVIA_DB`), and a hub export carries
them. The API: `GET /api/hub/env` (anyone on the hub), `PUT` / `DELETE
/api/hub/env/{name}` `{value, secret}` (admins). `exuvia env ... --url
<hub> --hub` does the same from a terminal.

## Embedding / using it inside another app

Everything the UI does is a plain HTTP call, so a host app can drive exuvia
headlessly or iframe the UI:

| Route | |
|---|---|
| `GET /api/project` | graph + per-node status, and every view with its state (`?params={...}` for one parameter set) |
| `PATCH /api/project` | rename, set the timezone |
| `GET /api/params`, `PUT/DELETE /api/params/{name}` | run parameters and the values that have run |
| `GET /api/env` | the project's environment variables and the requests to set one (`{variables: [{name, secret, value, readable, updated_at, updated_by}], requests: [{name, reason, origin, at}]}`); a secret's `value` is always `null`, and `readable` is false for one the server cannot decrypt |
| `DELETE /api/env/requests/{name}` | dismiss a request to set `name` (the agent's `request_secret`); `GET /api/env` lists them under `requests` |
| `PUT /api/env/{name}`, `DELETE /api/env/{name}` | set one (`{value, secret}`; secret by default) / remove one. The value is saved with `\n` line endings and no surrounding whitespace (one final newline when it has several lines), and a secret encrypted. Without `value` the saved one is kept, which makes a plain variable secret; a secret becomes plain only with a new value |
| `GET /api/packages` | the project's packages (from its `pyproject.toml`), what is installed, the install status and log, and agent requests waiting for approval |
| `POST /api/packages` | `{"add": [...], "remove": [...]}`: edit the list; a change that does not resolve is a 400 with uv's explanation and changes nothing. The install runs in the background (`packages` events) |
| `PUT /api/packages/manifest` | replace `pyproject.toml` (`{"text"}`) and install from it |
| `POST /api/packages/retry` | install the current list again after a failure |
| `POST /api/packages/requests/{id}/approve`, `DELETE /api/packages/requests/{id}` | install or dismiss what the agent asked for |
| `GET /api/npm` | the npm packages views may import (from the project's `package.json`) and the ones every view has |
| `POST /api/npm` | `{"add": [...], "remove": [...]}`: edit the list; each package added is checked on the CDN first, and one without a version is pinned. A package the CDN can't serve is a 400 and changes nothing |
| `GET /api/views/{name}/packages` | the npm modules a view imports, for the page to post into its frame: `entries` (import to module id), `modules` (module id to source), `errors` (import to why it could not be fetched) and `declared` |
| `GET/PUT/DELETE /api/nodes/{name}` | read / write / delete a node file |
| `GET /api/files`, `GET/PUT/DELETE /api/files/lib/{path}.py` | list / read / write (`{"source"}`) / delete shared helper modules; each answer names the nodes that import the file (`used_by`). Paths outside `lib/`, other extensions and `..` are rejected (400) |
| `GET /api/views` | every view with its state (`?params=`), as in `/api/project` |
| `GET/PUT/DELETE /api/views/{name}` | one view with its parsed manifest and every file's source / create it or replace its `view.json` (body: the manifest) / delete the folder |
| `GET/PUT/DELETE /api/views/{name}/files/{path}` | read / write (`{"source"}`) / delete one file of a view (`App.tsx`, `components/Chart.tsx`). Paths outside the view folder, `..`, absolute paths and other extensions are rejected (400); writes return the view |
| `POST /api/views/{name}/restore` | make a saved version (`{path, hash}`) of one view file current again; brings back a deleted file or view |
| `GET /api/views/{name}/data/{node}` | `/api/nodes/{node}/data` for a view: 403 unless the view's `reads` lists `node` |
| `POST /api/views/{name}/refresh` | re-run the live nodes a view depends on, for `{params}` (see "Live nodes"); viewers may call it |
| `POST /api/views/{name}/query` | `/api/query` for a view: only the nodes in its `reads` exist, and the connection cannot open other files (see "Rendering a view") |
| `GET /api/views/{name}/data/{node}/export`, `POST /api/views/{name}/query/export` | the two routes above as downloads, like `/api/nodes/{node}/export` and `/api/query/export` |
| `POST /api/nodes` | create from a template |
| `POST /api/nodes/{name}/run` | run a node; `{"wait": seconds}` waits and returns the same report the agent's `run_node` gets (status, error + log tail, or schema, rows and first rows; `running: true` if still going) |
| `POST /api/nodes/{name}/push` | upload a push node's output: Parquet or JSON as the body, `?params=` a JSON object, `?note=` a line for the log (see [Push nodes](#push-nodes-data-from-someones-desktop)); 422 when the data can't be read |
| `POST /api/run` | bring the graph (or named nodes) up to date for a parameter set (`wait` as above: per-node status and each failure's error + log tail) |
| `GET /api/guide` | the agent's instructions for this project (conventions, parameters, project notes) and its tools with their input schemas, for an outside agent writing nodes over HTTP |
| `POST /api/tools/{name}` | run one of those tools with the body as its input, as the agent would: `{"ok", "text", ...}`. A failed tool or a rejected input is still a 200 with `ok: false` and `text` saying why; an unknown tool is a 404. `exuvia mcp --url` relays MCP tool calls here (see "MCP") |
| `POST /api/run-stale` | refresh everything stale |
| `POST/GET/DELETE /api/backfill` | replay a list or date range of parameter values |
| `GET /api/nodes/{name}/data` | a node's output (`?params=` or `?run_id=`, `?limit=`, `?offset=`): JSON rows, or an Arrow stream (see "Binary data"). For a node with several outputs, `kind: "multi"` and its `parts`; `?part=` reads one |
| `GET /api/nodes/{name}/export` | a node's whole output as a file to save (see "Downloads"): `?format=` `csv`, `xlsx`, `parquet` or `json` (`json` only, for a JSON output), or `zip` for every output of a node with several; `?params=`, `?run_id=` and `?part=` pick the output as for `/data`; `?filename=` names it |
| `GET/HEAD /api/artifacts/{node}/{artifact_id}.parquet` (or `.json`, `.zip`) | the stored file of one output, byte for byte, with `Range` support (see "Binary data") |
| `GET/HEAD /api/artifacts/{node}/{artifact_id}/{part}.parquet` (or `.json`) | one output of a node with several, as a file of its own |
| `GET /api/nodes/{name}/history` | the node's stored output per parameter set (with the `code_hash` each was computed with) |
| `GET /api/code/revisions` | saved versions of the code, newest first (`?node=` for one node, `?view=` for one view's files, deleted ones included); each says how it was saved (`origin`), by whom (`author`, with `EXUVIA_AUTH=headers`) and whether it is current |
| `GET /api/code/blobs/{hash}` | the text of one saved version |
| `GET /api/code/archive` | the project's code as a .zip (nodes, `lib/` helpers, views, `exuvia.json`, `pyproject.toml`, `uv.lock`, `package.json`; never `.env` or outputs); `?node=` for one node and the helpers it imports, `?view=` for one view |
| `POST /api/nodes/{name}/restore` | make a saved version (`{hash}`) current again; brings back a deleted node |
| `GET /api/runs/{run_id}/source` | the exact code a run executed |
| `POST /api/query` | DuckDB SQL over all outputs (`{sql, limit, params}`): JSON rows, or an Arrow stream (see "Binary data"). It cannot open other files or the network (see "Execution model") |
| `POST /api/query/export` | the whole result of `{sql, params, format, filename}` as a file to save, in `csv`, `xlsx`, `parquet` or `json` |
| `POST /api/nodes/{name}/schedule` | set, clear, pause or resume a schedule |
| `POST /api/schedule/tick` | evaluate schedules now (for external schedulers) |
| `GET /api/schedule` | when the graph runs by itself: `nodes` (each scheduled node, soonest first: `cron`, `source`, `paused`, `next`, `upcoming` fire times, `last` scheduled run, `cascades`), `live` (live nodes and the views that refresh them), `timezone` and `scheduler` |
| `GET /api/events` | SSE: graph changes, run status, live logs |
| `GET /api/me` | who the request is from: `{auth, user}`, `user` being `{id, user, email, method, uid, role}` (`id` the name records show, `uid` the user id) or null (in a hub, at `<url>/api/me`) |
| `GET /api/users`, `PATCH /api/users/{id}` | the server's user list, for admins (`{role}` `admin`/`member`, `{disabled}`); in a hub, at `<url>/api/users` (see "Users and admins") |
| `POST /api/agent` | SSE: agent turn |
| `GET /api/agent` | saved conversations, latest first (`?limit=`): id, title, when, who started it, whether a turn is running |
| `GET/DELETE /api/agent/{session_id}`, `POST .../cancel` | saved transcript, clear it, stop a turn |

Requests that change code may send `X-Exuvia-Origin` (letters, digits, `-`,
`_`; e.g. `ui` or `mcp`) to label the revisions they create; otherwise they
are recorded as `api`. The workbench sends `ui` on every project request. With `EXUVIA_AUTH=headers` the signed-in person is
recorded as well: as `author` on each revision, and as `started_by` on each
run row (`/api/runs`, `/api/nodes/{name}/runs`, `/api/runs/{id}`), with
their user id beside each (`author_user`, `started_by_user`); all are null
for a caller with only the API token, a hand edit or a scheduled run.

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
`output_kind` / `kind` saying whether it is a table (`.parquet`), a JSON
value (`.json`) or several outputs (`.zip`, `multi`; each one is also served
on its own as `/api/artifacts/{node}/{artifact_id}/{part}.parquet` or
`.json`). `GET /api/artifacts/{node}/{artifact_id}.parquet` returns
that file as it is stored, downloading it from `EXUVIA_ARTIFACT_STORE` first
when this process does not have it. A partitioned node's output is several
files: `GET /api/nodes/{name}/partitions` lists them, each served the same way
(see [Partitioned nodes](#partitioned-nodes-a-history-that-grows-each-run)).

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
from the code that draws them (`site/src/lib/brand.ts`). The app wears the
same brand: `ui/src/brand.test.ts` fails when its mark (`ui/src/mark.ts`),
its accent or its dark theme's colours (`ui/src/theme/brand.ts`) stop matching the site's. Every page names a
social card, a PNG in `site/public/og/` listed in `site/src/lib/cards.ts`: a
picture with no words on it, one for the home page, one for the docs' front
page, one for each group of docs pages and one for the brand page. The build
fails when a page's card is missing.
`site/tools/capture.mjs` retakes the product screenshots
(`site/src/assets/shots/`) from a running `exuvia serve` of the example
project, a scratch `exuvia hub` and the getting-started project, and
photographs the social cards from the site's own `/card/` pages, which exist
only under `astro dev`. The docs pages show those screenshots without the
README holding any: `FIGURES` in `site/src/lib/docs.ts` places each one after
a paragraph of a section, and the build fails when that paragraph or the
picture is gone. The animals on the landing page are low-poly models built in code
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
  auth.py       who a request is from (EXUVIA_AUTH=headers: a sign-in proxy's headers)
  users.py      the user list: everyone who signed in, their identities, roles and disabled accounts
  arrowstream.py  query results as an Arrow IPC stream
  agent.py      agent tools + provider-neutral tool loop
  llm.py        model providers: Anthropic, OpenAI-compatible (OpenRouter / Portkey / DeepSeek)
  server.py     FastAPI routes + SSE + static UI
ui/             React + Astryx (components) + StyleX (styles) + React Flow + CodeMirror + Vega-Lite; ui/AGENTS.md says how it is built
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

Prototype. Not yet: SSO in exuvia itself (people are identified only by a sign-in
proxy's headers, or share a token), sharing a project with groups
rather than one person at a time, notifications when someone asks for access, executor autoscaling, merging rows on a key finer than a partition, non-Python runtimes, reading secrets from an external secret manager (Vault, a cloud's).

Found a bug, or something missing? Open an issue at
[github.com/rlancer/exuvia/issues](https://github.com/rlancer/exuvia/issues). Your agent can draft one
for you with `report_issue` (see "MCP").

## License

Apache-2.0
