This page takes you from nothing to a small graph of three nodes, shows what
exuvia does when you change one of them, puts a view on top, and turns on
the agent. It takes about ten minutes.

## Install

exuvia is a Python package. It needs Python 3.11 or newer.

```bash
pip install exuvia
```

The package includes the web UI, so there is nothing else to install.

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
  .gitignore      ignores .exuvia/ and .env
```

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
as long as the project exists.

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

## Turn on the agent

The agent is a model with tools over your project. It can list and read
nodes, write them, run them, query their outputs, and read the traceback when
a run fails. Give exuvia an API key and restart the server:

```bash
# Claude
echo "ANTHROPIC_API_KEY=sk-ant-..." >> my-research/.env

# or any model on OpenRouter
echo "OPENROUTER_API_KEY=sk-or-..." >> my-research/.env
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

## Where to go next

- [Nodes](#nodes) has everything the decorator and SQL header accept.
- [Run parameters and replays](#run-parameters-and-replays) runs one graph
  for many dates and keeps an output for each.
- [Views](#views) has the view API, the sandbox, and what a view can import.
- [Deploying](#deploying) moves node execution into a container and outputs
  into object storage.
