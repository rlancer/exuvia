This page takes you from nothing to a small graph of three nodes, shows what
exuvia does when you change one of them, puts a view on top, and turns on
the agent. It takes about ten minutes.

## Install

exuvia is a Python package with the web UI inside, so it is the only thing
to install. Any of these puts an `exuvia` command on your PATH:

```bash
uv tool install exuvia
pipx install exuvia
pip install exuvia         # into the active virtual environment
```

It needs Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) downloads one
when yours is older.

To try exuvia without installing it, put `uvx` (or `pipx run`) in front of
any command on this page. uv downloads the package into its cache on the
first run:

```bash
uvx exuvia init my-research
uvx exuvia serve my-research
```

The packages your nodes import go in the project's own `pyproject.toml`
(see [Create a project](#create-a-project)), not into exuvia's install.

Would you rather have your coding agent do all of this?
[Set up with your agent](#set-up-with-your-agent) has a prompt to paste into
Claude Code, Cursor, Codex, Grok or any agent with a terminal. It installs
exuvia, starts the workbench and connects itself to the project.

## Create a project

A project is a folder. `exuvia init` creates one with a first node in it.

```bash
exuvia init my-research
```

```text
my-research/
  nodes/
    hello.py      the first node
  lib/            shared Python helpers, imported by nodes
  views/          custom UI over node outputs
  pyproject.toml  the packages the nodes import
  .gitignore      ignores .exuvia/, .env and .venv/
```

`pyproject.toml` lists the packages your nodes import beyond those exuvia
comes with, under `[project] dependencies`. Add one with uv in the project
folder, and exuvia installs it before the next run:

```bash
cd my-research && uv add scipy
```

A package from your team's own index works the same way, with the index in
`pyproject.toml` and its credentials in the environment: see
[Packages](#packages).

Open `nodes/hello.py`. A node is a function with the `@exuvia.node()`
decorator, in a file named after it. This one takes no parameters, so it
depends on nothing, and it returns a table.

```python file=nodes/hello.py
"""A first node: returns a tiny table."""
import exuvia


@exuvia.node()
def hello():
    import pandas as pd

    return pd.DataFrame({"x": [1, 2, 3], "y": [1, 4, 9]})
```

## Open the workbench

```bash
exuvia serve my-research
```

Go to [http://127.0.0.1:7420](http://127.0.0.1:7420). You see one node on the
canvas, marked `new` because it has never run. Select it and run it. Its
output appears beside the code: the rows, the column types, and the log.

Leave the server running. It watches the project folder, so files you add or
change in an editor show up on the canvas by themselves.

## Add a node that depends on another

Create a second file. Its parameter is named `hello`, and that is the whole
dependency declaration: exuvia passes it the output of the `hello` node.

```python file=nodes/squares.py
"""Keep the rows of `hello` where y is above a threshold."""
import exuvia


@exuvia.node()
def squares(hello):
    return hello[hello["y"] > 1]
```

Nodes can be SQL too. In a SQL node, upstream nodes are tables. The SQL runs
in DuckDB.

```sql file=nodes/summary.sql
-- How many rows survived, and their total.
select count(*) as n, sum(y) as total
from squares
```

The canvas now shows `hello → squares → summary`. Run `summary`: exuvia runs
the node you asked for and every stale node upstream of it, in order.

![The workbench with three nodes, hello, squares and summary, all fresh. summary is selected, and its Data tab shows one row: n is 2 and total is 13.](shot:tutorial-graph "The graph after running `summary`. Every node is fresh, and the output of `summary` is in its Data tab.")

The same thing from a terminal, without the UI:

```bash
exuvia run my-research
```

```text
 success  hello
 success  squares
 success  summary
```

Run it again and nothing happens, because nothing changed:

```text
everything is fresh
```

## Change something and see what re-runs

Every output is stored under a hash of its content, in
`.exuvia/artifacts/<node>/`. A node is fresh while its code is the code that
last ran and its inputs are the outputs that run consumed. So exuvia can tell
exactly what a change affects.

Edit `squares.py` and change the threshold from `1` to `3`, then ask for the
status:

```bash
exuvia status my-research
```

```text
   fresh  hello
   stale  squares  (code changed)
   stale  summary  (squares is stale)
```

Now run it:

```text
 success  squares
  cached  summary
```

`squares` ran again, but `y > 3` keeps the same two rows `y > 1` did. Its
output is identical, so it has the same hash, and `summary` did not need to
run. Change the threshold to `4` and the output is different, so `summary`
runs too:

```text
 success  squares
 success  summary
```

Both outputs of `squares` are still on disk. Earlier outputs stay readable for
as long as the project exists. The code is kept the same way: select
`squares` and open its **History** tab to see each save, what it changed, and
restore any of them (see [Execution model](#execution-model)).

## Put a node on a schedule

A schedule belongs to a node, not to the whole graph. Give the decorator a
cron expression:

```python
@exuvia.node(schedule="0 17 * * 1-5")   # 17:00 on weekdays, local time
def hello():
    ...
```

While `exuvia serve` is running, that node re-runs on its cron. If its output
changed, the nodes downstream refresh as well. If it did not, they are left
alone. In a SQL node the same thing is a comment: `-- schedule: 0 17 * * 1-5`.

## Add a view

Nodes compute. A **view** is the interface you put on top of them, and it is
front-end code, not Python: a folder of TypeScript, React and CSS under
`views/`. Make one called `overview` with three files.

The manifest names the nodes the view reads. It can see those outputs and
nothing else.

```json file=views/overview/view.json
{
  "title": "Overview",
  "reads": ["squares", "summary"]
}
```

The entry file is a React component. `useTable` gives it a node's output and
`useQuery` runs DuckDB SQL over the nodes it reads.

```tsx file=views/overview/App.tsx
import { rows, useQuery, useTable } from "exuvia";
import "./styles.css";

interface Square {
  x: number;
  y: number;
}

export default function App() {
  const squares = useTable("squares");
  const totals = useQuery("select n, total from summary");
  const [summary] = rows<{ n: number; total: number }>(totals.data);

  if (squares.error) return <p>{squares.error}</p>;
  return (
    <main>
      <h1>{summary ? `${summary.n} rows, total ${summary.total}` : "Loading"}</h1>
      <ul>
        {rows<Square>(squares.data).map((r) => (
          <li key={r.x} style={{ width: `${r.y * 2}rem` }}>
            {r.x} squared is {r.y}
          </li>
        ))}
      </ul>
    </main>
  );
}
```

The stylesheet is an ordinary CSS file. The workbench's colours are there as
variables, so the view follows light and dark mode.

```css file=views/overview/styles.css
main {
  padding: 1.5rem;
  color: var(--text);
  font-family: var(--sans);
}
ul {
  display: grid;
  gap: 0.5rem;
  padding: 0;
  list-style: none;
}
li {
  padding: 0.4rem 0.6rem;
  background: var(--accent);
  color: var(--bg);
  white-space: nowrap;
}
```

Open **Views** at the top of the workbench and pick Overview. There is no
build step and nothing to install: exuvia compiles the files in the browser
and renders them in a sandboxed frame. Change the threshold in `squares.py`
and run it again, and the view redraws with the new rows. It never runs a
node itself. [Views](#views) covers the whole API and what the sandbox
allows.

![The Overview view in the workbench: the heading 2 rows, total 13, and one bar for each row of squares.](shot:tutorial-view "Overview over the first version of `squares`, where `y > 1` keeps two rows.")

## Turn on the agent

The agent is a model with tools over your project. It can list and read
nodes, write them, run them, query their outputs, and read the traceback when
a run fails. Give exuvia an API key and restart the server:

```bash
# Claude
echo "ANTHROPIC_API_KEY=sk-ant-..." >> my-research/.env

# or any model on OpenRouter
echo "OPENROUTER_API_KEY=sk-or-..." >> my-research/.env

# or through a Portkey gateway (a model from your Model Catalog)
echo "PORTKEY_API_KEY=..." >> my-research/.env
echo "EXUVIA_MODEL=@anthropic-prod/claude-opus-5-5" >> my-research/.env
```

`exuvia init` already added `.env` to the project's `.gitignore`. Then ask for
something in the agent panel:

- "Add a node that ranks the rows of `squares` by `y`."
- "Why did `summary` fail?" with the node selected.
- "Schedule `hello` every 30 minutes during market hours."
- "Add a chart of `squares` to the Overview view."

Every tool call is shown in the conversation, and the nodes the agent touches
flash on the canvas. What it writes are ordinary files in `nodes/`, which you
can read, edit and commit like the ones you wrote. [The agent](#the-agent)
covers providers, local models and project notes.

![The workbench after an agent turn. The agent panel lists its steps: looked at the graph, read prices, wrote drawdown, ran drawdown, then a summary. The new drawdown node is selected on the canvas, with its chart.](shot:agent "The agent on the example project in the repository (`examples/markets`), asked for a drawdown node per ticker with a chart.")

## Where to go next

- [Nodes](#nodes) has everything the decorator and SQL header accept.
- [Run parameters and replays](#run-parameters-and-replays) runs one graph
  for many dates and keeps an output for each.
- [Views](#views) has the view API, the sandbox, and what a view can import.
- [Downloads](#downloads) saves any table as CSV, Excel, Parquet or JSON,
  and the project's code as a zip.
- [Hub](#hub-many-projects-from-one-process) serves many projects from one
  process, and shares each one with the people who should see it.
- [Deploying](#deploying) moves node execution into a container and outputs
  into object storage.
