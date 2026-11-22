# Incremental nodes and DuckLake

Status: **exploration, nothing built.** Notes from a design discussion
(November 2026) on nodes that add a little data each run, and on whether
DuckLake should be where that data lives. Code references are to `main` at
the time of writing. The DuckLake and Quack sections were checked against
the DuckDB 2.0 and DuckLake 1.1 docs on 2026-11-22.

## The problem

A common shape: a node scheduled daily that adds one record (an end-of-day
price), or a node that produces a historical time series where each day only
the new numbers should be added. exuvia handles this inefficiently today.

### Pattern 1: the "full history" node

What `examples/markets/nodes/prices.py` does: fetch the whole history on
every scheduled run and return the full table.

- Refetches the entire history from the source every run: slow, and it uses
  up API rate limits.
- Writes a whole new artifact each day. Outputs are content-addressed
  (`exuvia/runner.py`, `save_artifact`), so one new row means a new file, and
  nothing ever deletes old artifacts. Stored history grows roughly
  quadratically. Rough guess for a 20-year, 500-ticker series: tens of MB a
  day, about 10 GB a year. With `EXUVIA_ARTIFACT_STORE` set, the full file is
  uploaded again every day.
- Every downstream node recomputes the full history. A remote executor also
  re-uploads the whole input file on every run (`RemoteExecutor.execute` in
  `exuvia/executor.py`).
- Cannot build a history at all from a source that only reports "now".

### Pattern 2: one output per day (`params=["as_of"]`)

The node returns just that day's rows. Storage is efficient, replays and
backfills work, and `<node>__runs` unions every day. But:

- `__runs` exists only in ad-hoc SQL and views (`Workspace._outputs_db` in
  `exuvia/workspace.py`). **A node cannot take it as an input.**
- A downstream node inherits `as_of` (`Graph.effective_params` in
  `exuvia/project.py`), and `Workspace._execute` hands it only that day's
  slice of its upstream. A 20-day rolling volatility can't be written this
  way.
- `__runs` is capped at 400 parameter sets (`HISTORY_VIEW_LIMIT`), about 1.5
  years of weekdays. A schema change between days silently drops the view.
- Backfilling 10 years means about 2,500 runs, one process each, even when
  the source could return the whole range in one call.

**Verdict: a real gap, but a narrow one.** Pattern 2 already stores the data
correctly; what's missing is a way for a node to read the accumulated
history. Pattern 1 is usable but wasteful.

## Options

### A. `<node>__runs` as a node input

```python
@exuvia.node(params=["as_of"], schedule="0 17 * * 1-5")
def eod_price():
    return fetch_close(exuvia.param("as_of"))      # one day's rows

@exuvia.node(params=["as_of"])
def rolling_vol(eod_price__runs):                  # every day so far, with an _as_of column
    ...
```

- **Point-in-time reads**: a run for date D sees only days up to D. Backfills
  then don't leak future data, and today's row arriving doesn't make past
  outputs stale.
- Freshness: the input's identity is a hash over the list of daily outputs
  it read.
- Reuses what already exists (params, backfill, `__runs`) and keeps every
  output reproducible from its code and inputs.
- Work involved:
  - the graph parser learns `__runs` dependencies (Python parameters and SQL
    table names);
  - lift the 400 cap for node inputs;
  - combine the daily files into one before shipping them to a remote
    executor;
  - compaction later, if reading thousands of small files gets slow.
- Does not fix range backfills: still one run per day.

### B. A node reads its own previous output (like dbt's `is_incremental()`)

```python
@exuvia.node(schedule="0 17 * * 1-5", incremental=True)
def prices(universe):
    prev = exuvia.previous()          # None on the first run or a full refresh
    return fetch(universe, since=next_day(prev))   # exuvia appends these rows
```

- Matches how people already think about the problem, and covers sources
  that only report "now".
- Breaks a core promise: the output depends on the node's run history, not
  only its code and inputs. Needs a rule for what a code change means
  (probably "rebuild from scratch") and a "full refresh" action.
- Only fixes storage if appends are stored as small chunks plus a list of
  chunks. Otherwise it's Pattern 1 with less fetching.

### C. Partitioned outputs (`partition_by="date"`)

The node returns rows for one day or many, and exuvia replaces those days in
place.

- The most capable option: a daily run returns today's rows, a backfill
  returns 10 years in one call, and corrections to past days just overwrite
  them.
- The most machinery: partition storage, upsert by key, and freshness per
  partition. It's also a second partitioning system alongside run params.

### Questions that decide between them

1. Do sources return one day per call, or a whole date range cheaply? (A
   range favours C for backfills.)
2. Do past values get corrected after the fact, such as late prints or
   corporate actions? (That favours C.)
3. Do downstream nodes need point-in-time correctness? (That favours A's
   design.)

## DuckLake as the storage layer

DuckLake keeps a lakehouse catalog in a SQL database and the data in parquet.
It would give B and C most of their storage machinery ready-made: append and
upsert, snapshots and time travel, a change feed, inlining of small writes,
and compaction. It does **not** decide how incremental nodes fit the graph
(freshness, what a node receives as input, cascade); that design work
remains.

The catalog would be a DuckDB file served over **Quack**, DuckDB's
client-server protocol, on **DuckDB 2.0**.

### What was tested

Run in a scratch environment on DuckDB 2.0.0 (the version `uv.lock` pins)
with the `duckdb-extension-{ducklake,quack,httpfs}==2.0.0` wheels from PyPI.

Setup. The process that owns the catalog serves it; `quack_serve` returns at
once and serves in the background:

```sql
-- server process, with catalog.duckdb open
LOAD httpfs; LOAD ducklake; LOAD quack;
SELECT * FROM quack_serve('quack://127.0.0.1:9494', disable_ssl := true, token := 'secret-token');
```

Clients attach it as a DuckLake catalog:

```sql
LOAD httpfs; LOAD ducklake; LOAD quack;
CREATE SECRET (TYPE quack, TOKEN 'secret-token');
ATTACH 'ducklake:quack:quack://127.0.0.1:9494' AS lake (DATA_PATH '/srv/lake/data/');
```

The Quack docs write the URI as `quack:host:port`; the `quack://` form above
also parses. `disable_ssl` already defaults to true for local addresses
(`localhost`, `127.0.0.1`, `::1`). A relative `DATA_PATH` is resolved against
each client's working directory, so every client must use the same absolute
path (or an object-store URL).

| Test | Result |
|---|---|
| Concurrent writers | Works. 8 processes × 20 one-row commits: all 160 succeeded in 6.6 s, process start-up included. |
| One-row daily appends | Works. The rows were inlined into the catalog; no parquet files written. The default inlining limit is 10 rows (`ducklake_default_data_inlining_row_limit`). A catalog or table option, `data_inlining_row_limit` (set with `CALL lake.set_option(…)`), is stored in the catalog and overrides that setting. |
| Time travel | Works. `SELECT … FROM lake.prices AT (VERSION => n)` gives point-in-time reads. |
| Change feed | Works. `lake.table_changes('prices', from, to)` returned only the newest rows. |
| Corrections | Works. `MERGE INTO` updated a past day in place. |
| Range backfill | Works. 750k rows in one commit went straight to parquet. |
| Compaction | Works. Old files stay on disk until a cleanup runs. A flush turned 5 inlined rows into a second file, and the merge wrote a third that replaces both. The merge only adds the two old files to `ducklake_files_scheduled_for_deletion`; `ducklake_cleanup_old_files` deleted them, leaving 1 file. Time travel to versions from before the merge still worked afterwards. `CHECKPOINT` runs the whole sequence: flush, expire snapshots, merge, rewrite, cleanup, delete orphans. |
| Server down | Attach fails. Every reader and writer needs the catalog server running. |

### Security: the Quack token is full control of the catalog server

exuvia's ad-hoc query connection is locked: `enable_external_access = false`
and `lock_configuration = true`.

- **Reads break under that lock.** They work again once the Quack URL is
  added to `allowed_directories`, and other files stay blocked.
- **Writes get through the sandbox**, unless the lake is attached
  `READ_ONLY`.
- **But `READ_ONLY` is only a flag on the client.** From inside the locked
  sandbox, a connection holding the token could still:
  - attach the lake again, writable;
  - call `quack_query('quack://…', '<any SQL>')`, which runs on the catalog
    server with the server's filesystem access (the test read `/etc/passwd`
    on the server host).

Quack's security page says the same: a server exposes the full SQL surface,
and a valid token grants full SQL access unless the authorization hook is
replaced. Quack has no built-in roles or read-only tokens.

So neither node processes nor the query/view sandbox can be given the token.
That matches the existing rule that nodes never see exuvia's own settings.

### Quack's hooks (documented, checked locally)

Two global settings (`SET GLOBAL …`) each name a scalar function that the
server calls:

- `quack_authentication_function(connection_id, client_token, server_token)`,
  default `quack_check_token`. Called once per new connection.
  `connection_id` is a random 32-character id the server generates. It is
  **not** the `client_id` a client can set (`CLIENT_ID` attach option,
  `quack_default_client_id` setting), which the hooks never see.
- `quack_authorization_function(connection_id, query)`, default
  `quack_nop_authorization`. Called before **every statement**, with its SQL
  text. Returning `false`, `NULL` or raising an error denies the statement. A
  `VARCHAR` result is run *instead of* the client's SQL. Any other result
  lets the statement through unchanged.

The docs suggest a hook that inspects parsed statement types. They warn that
their own sample regex for read-only access is not robust (`WITH … INSERT`
gets past it). A local check on DuckDB 2.0.0 confirmed that a Python UDF
registered on the serving connection with `create_function` works as either
hook. A denied statement reached the hook three times before the client gave
up; the docs don't mention that.

So a read-only token is technically possible. What remains is designing a
policy that holds up against everything a token holder could send, and
testing it.

### How it would fit exuvia

- **The control plane serves Quack from inside its own process,** from
  something like `.exuvia/lake.duckdb`. That adds no new service, since the
  control plane is already the one long-running process per project.
- **Only the control plane writes.** A lake-backed node returns just its new
  rows, and the control plane appends or merges them in one commit. The
  node's output id becomes the snapshot id, and freshness compares the
  snapshot a downstream run read with the current one.
- **Readers get files, not the token.** Nodes and the query sandbox get a
  snapshot's files (`ducklake_list_files`) or its change feed, written out as
  parquet by the control plane. Inlined rows have to be flushed or written
  out first, since they aren't in any file. That holds until a read-only
  policy in the authorization hook is designed and tested.
- **Quack's main payoff is outside readers:** remote executors, more than one
  hub replica, or analysts querying the lake from their own DuckDB or BI
  tool. If only the control plane ever touches the lake, a plain DuckDB file
  attached in-process would be enough. Quack keeps those uses open.

### Practical notes

- **Extensions aren't in the `duckdb` wheel.** exuvia turns off automatic
  extension installs, and air-gapped deployments can't reach
  `extensions.duckdb.org`. The PyPI wheels `duckdb-extension-ducklake`,
  `duckdb-extension-quack` and `duckdb-extension-httpfs` (2.0.0) install
  normally and load by file path. Each one requires exactly `duckdb==2.0.0`.
  `duckdb` 2.0.1 reached PyPI on 2026-11-20 without matching extension
  wheels, so depending on them holds `duckdb` back until new ones ship.
- **`httpfs` is required on both sides.** A server without it fails at
  `quack_serve` because only a read-only crypto module is loaded. A client
  uses it as its HTTP transport.
- **No primary keys.** DuckLake supports only `NOT NULL` constraints, and
  `MERGE INTO` is the only way to upsert. "Merge on a key" is whatever the
  `MERGE` condition says, and nothing stops an append from duplicating a key.
- **A DuckDB-file catalog is single-process** unless every process attaches it
  `READ_ONLY`. That is why sharing it needs Quack (or Postgres).
- **Maturity.** DuckLake 1.0 shipped on 2026-04-13. DuckDB 2.0.0 LTS and
  DuckLake 1.1 shipped together on 2026-10-21, and 1.1 requires DuckDB 2.0.
  Quack first shipped, as experimental, in DuckDB 1.5.3. The 2.0 release
  calls it "polished", but its repository README still says experimental, and
  no released doc gives it a 1.0 version number. The "about 200
  transactions/s for a Quack catalog against about 5/s for Postgres, with
  about 20 contending writers" figure is a spoken claim by Pedro Holanda
  (DuckLake's lead developer) on Talk Python #562, not a published benchmark.
- **Still open:**
  - hub exports (catalog plus data files);
  - how `EXUVIA_ARTIFACT_STORE` relates to a lake `DATA_PATH` on S3 (which
    would need `httpfs` S3 secrets, separate from exuvia's own S3 client in
    `exuvia/artifacts.py`);
  - one catalog per project, or one per hub with a schema per project.

## Current leaning

- Add DuckLake as the storage for a new kind of incremental node (append, or
  merge on a key). Keep ordinary per-run outputs as they are.
- Before anything else, settle how readers get data without the writer
  token: either a read-only policy in the authorization hook (the hook works
  as documented; the policy is the unbuilt part), or the control plane hands
  out snapshot files.
- Option A (`__runs` as a node input) doesn't need DuckLake and could ship
  first, depending on how soon the lake is wanted.

## Sources

- [DuckLake v1.0 announcement](https://ducklake.select/2026/04/13/ducklake-10/)
- [Data Inlining in DuckLake](https://duckdb.org/2026/04/02/data-inlining-in-ducklake)
- [DuckLake FAQ](https://ducklake.select/faq)
- [A Preview of DuckDB v2.0](https://duckdb.org/2026/08/17/duckdb-20-highlights)
- [Announcing DuckDB 2.0.0 LTS](https://duckdb.org/2026/10/21/announcing-duckdb-200)
- [What's New in DuckLake 1.1](https://ducklake.select/2026/10/21/ducklake-11/)
- DuckLake docs: [data inlining](https://ducklake.select/docs/stable/duckdb/advanced_features/data_inlining),
  [merge adjacent files](https://ducklake.select/docs/stable/duckdb/maintenance/merge_adjacent_files),
  [cleanup of files](https://ducklake.select/docs/stable/duckdb/maintenance/cleanup_of_files),
  [choosing a catalog database](https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database),
  [unsupported features](https://ducklake.select/docs/stable/duckdb/unsupported_features)
- Quack docs: [overview](https://duckdb.org/docs/2.0/quack/overview.html),
  [reference](https://duckdb.org/docs/2.0/quack/reference.html),
  [security](https://duckdb.org/docs/2.0/quack/security.html)
- [Using DuckDB Quack as the DuckLake catalog](https://www.definite.app/blog/duckdb-quack-ducklake-catalog)
- [Talk Python: DuckLake episode](https://talkpython.fm/episodes/show/562/ducklake-the-lakehouse-thats-just-sql-and-parquet)
- [MotherDuck: DuckLake architecture deep dive](https://motherduck.com/blog/ducklake-architecture-deep-dive/)
