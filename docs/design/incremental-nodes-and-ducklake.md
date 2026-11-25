# Incremental nodes and DuckLake

Status: **exploration, nothing built.** Notes from a design discussion
(November 2026) on nodes that add a little data each run, and on whether
that data should live in DuckLake or in partitioned Parquet that exuvia
manages itself (prototyped, and the current leaning). Code references are
to `main` at the time of writing. The DuckLake and Quack sections were checked against
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

## Partitioned outputs without DuckLake (prototyped)

Using DuckLake for incremental nodes only would leave exuvia with two
storage systems: content-addressed files for ordinary nodes and a lake for
incremental ones. Using it for every output would put a young system under
all of exuvia, and gain little: exuvia already keeps every run's output.
This option instead generalizes the artifact exuvia already has.

**An output is a manifest of content-addressed Parquet chunks.** A manifest
is a small JSON artifact. It maps each partition value (a date) to a digest
of that partition's rows, and lists the chunk files with the partitions each
holds. An ordinary node's output is a manifest with one chunk, so there is
one storage model.

- **A commit replaces partitions.** The control plane, the only writer,
  splits the rows a run returns by the key and writes one chunk per
  partition. A daily run writes one, a backfill writes ten years in one call,
  and a correction overwrites its day. The new manifest is recorded as the
  run's artifact: one database write, so it's atomic.
- **Two ids.** The *logical id* hashes the partition-to-digest map. It
  changes only when data changes, and freshness and cascades would key on
  it. The *physical id* hashes the file list, which compaction changes.
  Rewriting a partition with identical rows writes nothing and leaves the
  logical id alone, so a cascade stops as it does today.
- **Readers get a file list.** `read_parquet([...], union_by_name = true)`
  over the manifest's chunks. The query sandbox allows exactly those files.
  exuvia can drop chunks whose partitions a filter excludes before handing
  over the list.
- **Compaction** merges chunks into one file per group of partitions (a year
  of days), copy-on-write. A correction to a compacted day rewrites that
  file without the day, and adds the new day's chunk.
- **Old manifests stay readable,** so time travel and pinned reads work as
  they do now. Chunks are shared between manifests, so storage grows
  linearly instead of quadratically.

[`partitioned-outputs/`](partitioned-outputs/) has the prototype
(`manifest.py`) and the measurements (`bench.py`). It uses only the `duckdb`
exuvia depends on (2.0.1), on the same shared 4-core Windows machine as the
Quack tests.

### What was measured

A daily node with 500 tickers a day, built from one backfill of 10,000 days
(5 million rows). Each read builds a fresh sandbox connection as exuvia does
per request: `allowed_paths` set to the files, a view, external access off,
the lock. Medians of 3 runs (1 for 10,000 files), in ms:

| Days | Layout | Files | Sandbox setup | Count and sum | Last 30 days |
|---|---|---|---|---|---|
| 1,000 | One file | 1 | 61 | 20 | 10 |
| 1,000 | Union per file (today's `__runs`) | 1,000 | 1,533 | 2,604 | 1,958 |
| 1,000 | File list | 1,000 | 688 | 1,342 | 1,350 |
| 1,000 | File list, pruned to 30 days | 30 | | | 104 (with setup) |
| 1,000 | Compacted by year | 3 | 49 | 18 | 12 |
| 10,000 | One file | 1 | 49 | 123 | 14 |
| 10,000 | File list | 10,000 | 8,042 | 15,731 | 17,407 |
| 10,000 | File list, pruned to 30 days | 30 | | | 138 (with setup) |
| 10,000 | Compacted by year | 28 | 77 | 153 | 46 |

All four layouts returned the same answers.

- **Each file costs about 1.5 ms a query, plus about 0.8 ms to set up the
  sandbox.** That cost depends on the number of files, not their size: 10,000
  small files take 15 s to count 5 million rows, while one file takes 0.1 s.
- **Compacted, a manifest reads like one file.** 28 yearly files were within
  noise of the single file for full scans. For the last 30 days they were
  46 ms against 14, because Parquet statistics skip most of each year.
- **Pruning by partition makes recent reads cheap** whatever the history's
  size: the last 30 days' chunks took 104–138 ms, setup included.
- **Commits are cheap.** A daily commit took 20–28 ms. A correction to a day
  inside a compacted year took 153 ms, rewriting that year's file. The
  10,000-day backfill took 71 s in one pass, and compacting it by year took
  17 s.
- **Identity holds.** A daily rewrite with identical rows, and an identical
  rewrite of a day first written by the backfill, both left the manifest
  unchanged. Compaction kept every row (`EXCEPT ALL` was empty) and the
  logical id. A correction changed the logical id and touched only its day.
- **Hash rows, not files.** The same day's rows written alone and written by
  the backfill's one-pass partitioned `COPY` gave different Parquet bytes.
  A partition's digest is therefore SHA-256 of its rows as text, sorted,
  computed in one SQL pass. A DuckDB upgrade that formats a value
  differently would change digests once, and cause one spurious cascade.
- **Storage.** The chunks took 40 MB; compacted by year, 31 MB, the same as
  one file. A full-history node writing one full file a day would have
  written about 156 GB over the same 10,000 days (estimated from the final
  file's size).
- **The sandbox holds.** A chunk outside the manifest's list was refused.
- **The manifest grows with partitions.** 10,000 partitions made 2.1 MB of
  JSON, parsed in 22 ms. In exuvia it would belong in the records database
  rather than one JSON value.
- **Today's `__runs` is about twice as slow as a file list.** It joins one
  `read_parquet` per run with `UNION ALL BY NAME`. One `read_parquet` over
  the list, with `union_by_name`, would halve it. That change stands on its
  own, but it wasn't tried in exuvia.

What it implies:

- **Compaction is required, not an optimization.** Keep each output to a few
  dozen files. One rule that would do it: when the uncompacted tail passes
  about 30 chunks, fold it into the current period's file, months into
  years. Folding rewrites the open period's file, about 150 ms here.
- **Not tested:**
  - chunks on S3 (`EXUVIA_ARTIFACT_STORE`), where each file costs HTTP
    requests and compaction matters even more;
  - a remote executor caching chunks between runs;
  - a node taking the manifest as input, and freshness and cascades keyed on
    the logical id;
  - merge on a key finer than the partition;
  - schema changes between partitions beyond added columns;
  - when unreferenced chunks get deleted. exuvia never deletes outputs
    today.

**Against DuckLake** it gives up row-level merge, inlining small writes, a
row-level change feed, several writers and outside DuckDB attach. Merge at
partition granularity, compaction and manifest diffs stand in for the first
three. The others aren't needed now. In return it needs no extensions, no
Quack, no catalog migrations and no read-only policy, and ordinary and
incremental outputs share one model.

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
gets past it).

The docs also say Python UDFs registered with `create_function` "cannot be
used as authentication or authorization callbacks". Supported callbacks are
SQL macros, built-in functions and functions registered by an extension.
**On DuckDB 2.0.0 Python UDFs do work as callbacks:** every test below used
them, and Quack called them on its own fresh connection and enforced their
answers. They are still unsupported, so the prototype checks at startup that
its hook is enforcing.

A denied statement reached the hook three times before the client gave up;
the docs don't mention that.

### Read-only policy (designed and tested)

Prototype and tests: [`quack-readonly/`](quack-readonly/). Everything ran on
DuckDB 2.0.0 with the 2.0.0 extension wheels, with the server and each client
in separate processes.

**What a DuckLake reader sends.** A logging hook recorded a workload that
covers what readers normally do: plain and filtered reads, joins, views,
nested schemas, partitioned tables, time travel by version and by timestamp,
`table_changes`/`table_insertions`/`table_deletions`, `snapshots()`,
`table_info()`, `options()`, `ducklake_list_files`, and a long-lived reader
picking up new commits. It made 142 reader statements, 81 of them distinct.
Every one was a single `SELECT`. They touch only:

- `ducklake_*` metadata tables in the default catalog, including the
  per-table inlined-data tables;
- CTEs the statement defines itself (`schema_tree`, which is recursive,
  `main_results`, `col_N_stats`);
- `information_schema.tables`;
- four table functions with no arguments: `duckdb_functions`,
  `duckdb_schemas`, `duckdb_tables`, `duckdb_views`;
- twelve plain functions: `-`, `array_agg`, `count_star`, `current_database`,
  `length`, `list`, `list_append`, `list_value`, `max`, `min`,
  `struct_pack`, `sum`;
- a small set of parse-tree shapes: `SELECT`, set operations, recursive CTEs,
  joins, subqueries, comparisons, `CASE`, `CAST`, `BETWEEN`, `IN`, `IS NULL`,
  `COALESCE`, `ORDER BY`, `LIMIT`, `DISTINCT`.

A writer also sends `CREATE TEMPORARY TABLE ducklake_staged_commit` and
multi-statement `BEGIN TRANSACTION; INSERT …; UPDATE …` commits.

The policy has three layers.

1. **Tokens map to roles.** The authentication hook maps a writer token or a
   reader token to its role and binds the role to the server-issued
   connection id. Authorization lets writer statements through untouched.
   Each reader statement is checked:
   - it is parsed with `json_serialize_sql` in a private in-memory DuckDB of
     the same build, which serializes only `SELECT`;
   - there must be exactly one statement;
   - the whole parse tree is walked, and anything not on the allowlist above
     is denied, including unknown node types, so an upgrade fails closed;
   - a table must be a `ducklake_*` table in `main` with no catalog, one of
     the statement's own CTEs, or `information_schema.tables`;
   - a CTE name must be a plain identifier that doesn't collide with any
     table or view the server knows about (`pg_settings`,
     `duckdb_databases`, …). Otherwise a reference outside the CTE's scope
     would read the real view, or a file through a replacement scan;
   - functions must be unqualified and on the list;
   - a statement over 64 KB is denied;
   - verdicts are cached per SQL text, since DuckLake repeats its queries.
2. **The serving process is locked down** after `quack_serve`:
   `enable_external_access = false` and `lock_configuration = true`.
3. **A watchdog** in the control plane polls `quack_active_connections()`.
   It cancels reader statements that run past a time limit (`quack_cancel`,
   through its own writer connection) and drops roles of connections that
   are gone. A connection is dropped only once it is missing from the list
   *and* was bound more than 30 s ago. Without that grace period the
   concurrency test lost a reader's role mid-attach: the reader
   authenticated after the watchdog took its snapshot, so it looked gone.

**Two details matter.**

- **Qualify the hook names.** With the setting naming
  `exuvia_policy_authz`, a client holding the writer token created a macro
  of that name in the lake's `main` schema. The macro shadowed the hook, and
  a reader `DELETE` then ran. Naming the hooks `system.main.exuvia_policy_*`
  closed that, because no client can create functions in `system`.
- **Parse outside the serving database.** A macro that called
  `json_serialize_sql` inside a `CASE` was tried first, to skip the parse for
  writers. DuckDB evaluated the `ELSE` branch anyway, so every writer
  statement paid for a full parse (169 ms for a 3,000-column statement).

**Results.**

| Test | Result |
|---|---|
| Legitimate workload (writer plus two readers, everything listed above) | All worked, results identical to an unprotected server, 0 denials |
| Concurrency: 6 reader processes × 25 rounds, alongside 60 writer commits | 0 errors, 0 denials, counts never went backwards, all 1,060 rows present |
| 104 attacks with the reader token | All 104 denied by the policy, with or without the lockdown. Catalog unchanged, no file written, the secret file never read |
| A runaway query the policy allows (an infinite recursive CTE) | Cancelled by the watchdog after about 3 s |
| 5 plain reads with the raw reader token | All worked |

The 104 attacks:

| Category | Attempts |
|---|---|
| Catalog writes | `DELETE`, `UPDATE`, `INSERT`, `DROP`, `TRUNCATE` and `ALTER` on `ducklake_snapshot`; `CREATE TABLE`/`TEMP TABLE`/`MACRO`/`VIEW`; DML inside a CTE |
| Statement smuggling | stacked `SELECT 1; DELETE …`; the same behind `--` plus a newline, behind `/* ; */`, and after a quoted `';'`; two harmless statements |
| Session and server control | `BEGIN`, `CHECKPOINT`, `VACUUM`, `USE`, `ATTACH`, `SET`, `RESET`, `SET GLOBAL quack_authorization_function`, `SET VARIABLE`, `PRAGMA`, `CALL`, `INSTALL`, `LOAD`, `PREPARE`, `COPY … TO`, `EXPORT DATABASE`, `DESCRIBE`, `SUMMARIZE`, `SHOW` |
| Files and network | `read_csv`/`read_text`/`read_blob`, `FROM '<path>'`, `glob`; the same inside a subquery, a `UNION` or a scalar subquery; a CTE named after a file path, both in scope and out of scope; an HTTP URL; `quack_query` from the server |
| Settings, secrets, other sessions | `pg_settings`, `duckdb_settings()` (also qualified as `system.main`), `duckdb_secrets()`, `current_setting`, `getenv`, `quack_active_connections()`, `duckdb_databases`; CTEs named `pg_settings`, `duckdb_databases` and `ducklake_snapshot`; `information_schema.columns` |
| Functions | `query()`, `query_table()`, calling the policy's own UDFs, lambdas, list comprehensions, `COLUMNS(lambda)`, quoted and upper-case names, `main.max`, `system.main.max`, window functions, `range`, a table function with arguments, `\|\|` |
| Catalogs and shapes | the catalog qualified by name, `temp`, `system`, the policy's catalog, a Cyrillic look-alike table name, `"ducklake_snapshot.csv"`, `VALUES`, parameters, `PIVOT` |
| DuckLake through a writable attach with the reader token | insert, update, delete, merge, create, drop, alter, `set_option`, expire snapshots, flush inlined data, merge files |
| Plain Quack attach with the reader token | delete, create, drop |
| Identity | a wrong token; a reader reusing the writer's `CLIENT_ID` |
| Server takedown | `DETACH` of the catalog, `quack_stop` |

**Each layer alone.** With the policy and no lockdown, all 104 attacks were
still denied. With the lockdown and no policy, the lockdown stopped file and
network access, `COPY`/`EXPORT`, `ATTACH`, `SET`/`RESET` and `INSTALL`. The
reader token could still:

- delete and drop the catalog tables, which left the lake unreadable;
- create tables and macros;
- read every setting and the secret list;
- see other connections' SQL;
- stop the server with `quack_stop`.

The policy is the boundary. The lockdown is a second layer for anything the
allowlist might miss.

**Cost per statement**, measured by calling the hook directly over the logged
workload:

| Path | Mean | p95 |
|---|---|---|
| A no-op hook, for comparison | 0.76 ms | |
| Writer statement | 0.83 ms | 0.97 ms |
| Reader statement, first time | 3.5 ms | 6.6 ms |
| Reader statement, with the cache | 2.2 ms | 5.1 ms |

About half of a first-time check is the parse.

**Caveats.**

- **Python UDF callbacks are unsupported by Quack's docs** (above).
  `self_test` runs right after `quack_serve`: if a forbidden reader statement
  isn't denied by the hook, the server refuses to run. Both supported kinds
  of callback have been built and tested too; see the next section.
- **The allowlist is tied to what DuckLake 1.1 sends.** A new extension
  version that sends another function or table has its reads denied. That
  fails closed, and `workload.py` run against `serve.py` is the check to
  repeat on every DuckDB or extension upgrade.
- **Read access is all-or-nothing.** A reader token can read every table and
  all metadata, including per-file encryption keys if the lake uses
  `ENCRYPTED`.
- **The token only governs the catalog.** Data files at `DATA_PATH` are read
  and written by clients directly, so filesystem or bucket permissions
  protect them. exuvia's sandbox already limits reads with
  `allowed_directories`.
- **The lockdown has limits.** It doesn't stop SQL that writes the catalog's
  own tables; only the policy does. Quack issue #366 reports that
  `quack_serve` itself ignores `enable_external_access`; the prototype locks
  down only after serving, so that doesn't affect it.
- **Cost limits are coarse.** Readers can still run expensive queries over
  the metadata until the watchdog's limit, which applies per statement.
  Memory isn't limited separately.
- **The writer token is full control.** It must never leave the control
  plane. Quack issue #325 points out that SQL-defined callbacks can be
  replaced by any client allowed to write; qualifying the hook names covers
  the shadowing case.

### Three ways to build the hook (tested)

Quack's docs support two kinds of callback that the policy could use instead
of Python UDFs: a SQL macro, and a function from an extension. Both were
built with the same rules ([`policy_sql.py`](quack-readonly/policy_sql.py),
and [`native/exuvia_policy.c`](quack-readonly/native/exuvia_policy.c) on
DuckDB's stable C extension API) and run against the same tests.

| | Python UDF | SQL macro | C extension |
|---|---|---|---|
| Supported by Quack's docs | No (works on 2.0.0; startup self-test) | Yes | Yes |
| 110 attempts: 104 attacks, the runaway query, 5 plain reads | As expected | As expected | As expected |
| Logged reader statements allowed | 81 of 81 | 81 of 81 | 81 of 81 |
| Verdicts on the other 124 logged statements, sent as a reader | (reference) | Same as the Python UDF | Same as the Python UDF |
| Concurrency test (6 readers, 60 commits): errors; wall time | 0; 26 s | 0; 37 s | 0; 34 s |
| CPU per reader statement (no-op hook: 0.7 ms) | 2.7 ms | 19 ms | 2.6 ms |
| CPU per writer statement | 0.65 ms | 18 ms | 1.2 ms |
| How a writer is recognised | Writer token, bound at authentication | A secret client id registered at startup | Writer token, bound at authentication |
| A denied reader sees | `read-only token: <reason>` | `Authorization failed` | `read-only token: <reason>` |
| Ships as | Python in the control plane | SQL in the control plane | A binary per platform, loaded unsigned |

CPU time is this process's CPU per call, from
[`bench_hooks.py`](quack-readonly/bench_hooks.py), which calls each hook the
way Quack does. Wall-clock times were unusable: other processes kept the
machine at 100% CPU during these runs. The concurrency times are wall
clock, one run each back to back, so they are rough: earlier SQL-macro
runs took 51 s and 131 s.

**The SQL macro is secure but slow.**

- A macro can't keep state, and Quack calls the authentication function
  before the connection exists, so the macro can't bind a token to a role.
  A writer is recognised by its client id instead. The control plane sets
  `quack_default_client_id` to a secret on its writer clients. Quack keeps
  only an HMAC-SHA256 of a client id, keyed with a random per-server key, so
  the control plane connects once after `quack_serve`, reads its own hash
  from `quack_active_connections()` and stores it. Every writer reconnect
  then matches. The client id becomes the write credential: a reader-token
  client that knows it is a writer, and a writer-token client without it is
  a reader.
- Readers can list macro definitions, because `duckdb_functions()` is on
  the allowlist. Token hashes live in a table in the policy's own catalog,
  not in the macro's text.
- The rules are JSON paths over the `json_serialize_sql` tree (`$..type`,
  `$..function_name`, `$..schema_name` …).
- Every statement costs about 18 ms of CPU, writers' too. Quack prepares
  `SELECT hook(?, ?)` afresh for each statement, so DuckDB binds and plans
  the whole macro every time. Other forms were slower: one flat expression
  took 40–90 ms and regular expressions over the JSON text 25 ms (DuckDB
  compiled each constant pattern on every call). A 150-item list literal
  alone cost 5–7 ms, so the reserved names moved into a table. DuckDB
  evaluates both sides of `OR` and both arms of `CASE`, so a writer's
  statement is swapped for a constant before the parse. Otherwise a
  3,000-column writer statement took 0.7 s.

**The C extension is supported, but no faster than Python.**

- It targets the stable C extension API (v1.5.6 in DuckDB 2.0.0). The point
  of that API is that the binary doesn't need a rebuild for each DuckDB
  release. Running it on another DuckDB version wasn't tested.
- It builds with `zig cc` from the `ziglang` wheel on PyPI, with no MSVC,
  gcc or cmake ([`native/build.sh`](quack-readonly/native/build.sh)). The
  same build cross-compiled for Linux x86_64 and aarch64 and for macOS
  aarch64. Only the Windows build was loaded and tested.
- A native function keeps state, so it binds roles at authentication like
  the Python UDFs do. It adds `role()` and `forget()` functions for the
  watchdog.
- **The stable C API has no access to DuckDB's parser.** The extension
  parses exactly like the Python version: `json_serialize_sql` in a private
  in-memory instance, then a walk of the JSON. That query is most of the
  cost, so native code only speeds up the small part. Parsing directly would
  take a C++ extension built against DuckDB's internal headers, which must be
  rebuilt for every DuckDB release.
- It is unsigned. DuckDB signs only core and community extensions, so the
  serving instance needs `allow_unsigned_extensions` at startup.
- Its JSON reader is about 200 lines of C that read attacker-influenced
  strings. They are bounds-checked and depth-limited, but a Rust version
  would rule out that class of bug. Rust here has only the MSVC target and
  no MSVC linker. zig could link it (`cargo-zigbuild`), but that wasn't
  tried.
- Checks go through one parser connection behind a lock, so concurrent
  reader checks queue. The Python UDFs queue on the GIL the same way.

### How it would fit exuvia

- **The control plane serves Quack from inside its own process,** from
  something like `.exuvia/lake.duckdb`. That adds no new service, since the
  control plane is already the one long-running process per project. A test
  of exactly that found no deadlock and little effect on exuvia's API
  ("Quack inside the control plane" below).
- **Only the control plane writes.** A lake-backed node returns just its new
  rows, and the control plane appends or merges them in one commit. The
  node's output id becomes the snapshot id, and freshness compares the
  snapshot a downstream run read with the current one.
- **Readers get a reader token.** The control plane serves Quack with the
  read-only policy, the lockdown and the watchdog. Nodes and the query/view
  sandbox attach the lake `READ_ONLY` with a reader token. The query sandbox
  pins a snapshot and allowlists its files; its lock as it is would block
  the reader (next section). DuckLake then
  handles snapshots, inlined rows and time travel for them. The writer token
  never leaves the control plane. The fallback, if the policy proves too
  brittle across upgrades, is for the control plane to hand out a snapshot's
  files (`ducklake_list_files`) or its change feed as parquet. Inlined rows
  would have to be flushed first.
- **Quack's main payoff is outside readers:** remote executors, more than one
  hub replica, or analysts querying the lake from their own DuckDB or BI
  tool. If only the control plane ever touches the lake, a plain DuckDB file
  attached in-process would be enough. Quack keeps those uses open.

### A lake reader in exuvia's query sandbox (tested)

exuvia's query/view sandbox (`Workspace.query_connection`) turns off
extension autoloading, allows reads of only the node files behind its views
(`allowed_paths`), then sets `enable_external_access = false` and
`lock_configuration = true`.
[`sandbox_reader.py`](quack-readonly/sandbox_reader.py) attaches the lake
over Quack with the reader token in a connection set up the same way, then
tries reads and escapes. The serving side was `serve.py` with the Python UDF
policy.

**exuvia's lock as it is breaks the reader.** The extensions load and the
lake attaches before the lock, but every read afterwards fails: Quack talks
to the catalog server over HTTP, and with external access off that URL is
refused like any other ("Cannot access http://127.0.0.1:…/quack"). Attaching
after the lock fails too.

Two ways to open it up were tested:

| | A: catalog URL + `allowed_directories = DATA_PATH` | B: pinned snapshot + `allowed_paths` = the files on disk + the catalog URL |
|---|---|---|
| Reads, time travel, `table_changes` | Work | Work |
| A commit after the lock | Seen at once | Not seen; the next request (new pin, new list) sees it |
| Read another file, `ATTACH`, `COPY` elsewhere, another URL, `SET`, `INSTALL` | Fail | Fail |
| Write the lake or its metadata, `quack_query` with the stored secret | Fail (read-only attach, server policy) | Fail |
| `COPY` a new file into `DATA_PATH` | **Succeeds** | Fails |
| `COPY` over an existing data file | **Succeeds, and corrupts the lake** for every client ("Invalid footer length") | Fails |
| A writable re-attach, then a 100-row insert | Attach works; the file is written, the commit is denied | Attach works; the file write is refused |

**B is the one to use.** `allowed_directories` grants writes as well as
reads, so A lets sandboxed SQL destroy the lake. `allowed_paths` with exact
files doesn't let `COPY` overwrite them: exuvia's current sandbox doesn't
let SQL overwrite node outputs either, which was checked separately. B fits
how exuvia already works. It builds a fresh sandbox connection for each
request, so each one pins the current snapshot
(`ATTACH … (SNAPSHOT_VERSION n)`) and allows exactly the files that exist
when it is built. A query then sees one consistent snapshot even if a commit
lands mid-request.

What B implies:

- **The catalog URL must stay reachable, so the server policy is a needed
  layer even here.** A sandboxed query can send raw SQL with the stored
  reader secret through `quack_query`. The policy denied it in the test, but
  without the policy the reader secret would be the writer's power.
- **The reader token doesn't leak into SQL:** `duckdb_secrets()` shows the
  secret with its token redacted.
- **A writable re-attach is possible** because attaching only reads. Writes
  through it fail twice over: locally the new data file isn't on the
  allowlist, and the server denies the commit.
- **Not tested:**
  - the cost of attaching per request, measured later at about 1.1 s on a
    loaded machine ("Quack inside the control plane" below), with nothing
    tried to cut it;
  - an allowlist of thousands of files;
  - `DATA_PATH` on S3, where the allowlist would hold `s3://` paths and the
    file list could come from `ducklake_list_files` at the pinned snapshot
    instead of a directory listing;
  - node subprocesses, which run Python under exuvia's own sandbox rather
    than this SQL lock.

### Quack inside the control plane (tested)

[`control_plane.py`](quack-readonly/control_plane.py) builds exuvia's real
app (`exuvia.server.create_app`) and serves Quack from the same process,
with the policy, the lockdown and the watchdog. Two threads stand for the
control plane's own use of the lake. One is a writer committing every 0.2 s.
The other is a sandboxed reader, set up as in fix B above, attaching and
counting every 0.5 s. Both connect back to their own process's Quack server.
That is the case that could deadlock: the caller waits while its statement's
hook runs on a Quack thread, and the Python hook needs the GIL.

The load ran three 25-second phases:

1. exuvia's HTTP API (`GET /api/project`, and `POST /api/query` with
   `SELECT 42`) and a Quack "ping" (`quack_query` with the reader token);
2. the same plus two DuckLake reader processes;
3. the same with a pure-Python busy loop in the server holding the GIL in
   5 ms slices, standing for control-plane work in Python.

Each hook ran twice, interleaved, plus a run with no Quack at all. The
machine has 4 cores and was near 100% busy with other work, so the numbers
are comparisons, not capacity.

| p50 / p95 ms | No Quack | Python hook | C extension |
|---|---|---|---|
| `GET /api/project`, phase 1 | 4.1 / 5.5 | 4.2 / 6.5 | 4.0–4.4 / 6.3–6.7 |
| `POST /api/query`, phase 1 | 43 / 49 | 55 / 230–244 | 55 / 235–256 |
| `GET /api/project`, GIL busy | | 106–118 / 243–279 | 99–104 / 245–295 |
| Quack ping, phase 1 | | 10 / 200–206 | 11 / 201 |
| Quack ping, GIL busy | | **33–60** / 212–321 | **18–20** / 219–230 |
| Lake read, other process, GIL busy | | **75–201** / 328–694 | **60–73** / 291–304 |
| In-process writer commit, GIL busy | | 217–337 / 534–1194 | 270–299 / 644–729 |
| In-process sandboxed read, phase 1 | | 1,107–1,127 | 1,128–1,139 |
| In-process sandboxed read, GIL busy | | **2,464–3,846** | **1,938–2,156** |

- **No deadlock and no errors.** With the Python hook, the in-process writer
  and sandboxed reader kept completing through every phase, including the
  GIL-busy one. DuckDB releases the GIL while a statement waits on Quack, so
  a Quack thread can take it to run the hook.
- **exuvia's own API barely notices Quack.** `GET /api/project` is
  unchanged. `POST /api/query` keeps its median but its p95 goes from 49 to
  about 240 ms: the in-process writer and sandboxed reader compete with it
  for the same CPUs. When Python work holds the GIL, the API slows to
  about 100 ms at the median with either hook. That is exuvia's own Python,
  not Quack.
- **The Python hook pays for a busy GIL; the C extension doesn't.** With the
  GIL busy, every reader statement waits for it. The ping's median doubles
  to sextuples, and the sandboxed read takes 2.5–3.8 s against 1.9–2.2 s
  with the C extension. Both runs showed it. With the GIL free the two hooks
  are the same speed, as `bench_hooks.py` found.
- **Attaching per request is slow.** The sandboxed read builds a fresh
  connection, loads three extensions, attaches at a pinned snapshot and
  counts. That takes about 1.1 s here, against about 10 ms for a single
  statement. On a loaded machine it is the main cost of fix B. Keeping a
  warm reader attached and moving its pin, instead of building one per
  request, wasn't tried.
- **The watchdog's limit must allow for a slow attach.** At 5 s it cancelled
  legitimate reader attaches once the machine was overloaded. These runs
  used 30 s. The limit stands for a runaway reader statement, so it should
  be generous, and a cancelled attach should be retried.
- **The reader token serves only DuckLake readers.** A plain Quack `ATTACH`
  with the reader token fails on its first query: the statements it sends
  aren't on the allowlist. A denial reaches the client as "Python exception
  occurred" with the Python hook. `quack_query` with an allowed statement
  works.

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

## Upstream roadmap: anything to wait for?

Checked on 2026-11-22: the DuckLake roadmap (last updated October 2026),
the duckdb/ducklake and duckdb/duckdb-quack repositories, and the DuckDB
release calendar.

- **Access control: don't wait.** "Roles and access control" is a "Top
  Priority" item for DuckLake 2.0. The roadmap gives no date and says there
  are "no guarantees". I found no code for it. In 2025 a maintainer called
  fine-grained RBAC out of scope (duckdb/ducklake#74). Quack has no planned
  read-only mode or token scopes. Per-user principals for its callbacks are
  a community PR that hasn't been reviewed (duckdb/duckdb-quack#347). A
  client-side `ATTACH … (READ_ONLY)` on a plain Quack database, which never
  blocked writes, is refused on Quack's `main` branch (#329).
- **The DuckLake REST API could replace the SQL policy.** It merged to
  DuckLake's `main` on 2026-11-13 (duckdb/ducklake#1930). Clients read and
  commit through nine fixed endpoints and never send SQL, with a
  bearer-token server, `ducklake_rest_serve`. Read-only would then mean
  refusing the commit and compaction endpoints, instead of parsing SQL. It
  isn't in a release yet. It probably arrives with the next DuckDB minor
  release (2.1, planned for the first half of 2027), but that's an inference.
  Keep the reader-token boundary behind an interface so the transport can
  change.
- **Expect a DuckLake 2.0 catalog migration.** `main` is at catalog version
  `2.0-dev1`, which adds snapshot tags, branching and a new snapshot primary
  key. The 1.1 migration was opt-in and one-way. Write the catalog only
  through DuckLake, never with hand-written SQL, so a migration stays
  mechanical. Snapshot tags (merged on `main`: expiry skips tagged
  snapshots) would give readers stable pins.
- **Nothing else to wait for.**
  - Compaction stays manual: the control plane schedules `CHECKPOINT`.
  - Upsert beyond `MERGE INTO` is "not for the foreseeable future"
    (duckdb/ducklake#66).
  - Unenforced `PRIMARY KEY` syntax and materialized views are "Future
    Work".
  - The change feed has shipped. Open work (#1914) concerns compaction
    rewrites showing up in it.
- **Quack is still beta in practice.** Its README says experimental, and on
  Talk Python #562 its developer advised against production use for now.
  Open issues cover heartbeats, cancellation and timeouts. Extension wheels
  have lagged DuckDB releases by 9 to 30 days (2.0.1 has none yet), so pin
  exact versions.

## Current leaning

- Store incremental nodes as partitioned outputs: a manifest of
  content-addressed Parquet chunks, with compaction in exuvia ("Partitioned
  outputs without DuckLake"). An ordinary output is the one-chunk case, so
  exuvia keeps one storage model. The prototype read as fast as one file
  once compacted; S3 and the graph integration are untested.
- Keep DuckLake and Quack for later: for row-level merge, several writers,
  or outside DuckDB readers, and once the DuckLake REST API ships. The work
  below stands if that happens. Quack isn't exuvia's outside read API: the
  HTTP API already serves Arrow streams and Parquet files.
- If the lake is adopted, readers get a read-only Quack token. The policy is built and tested
  three ways. Drop the SQL macro: every statement, a writer's included,
  costs about 25 times a no-op hook. Keep the Python UDF hook, with its
  startup self-test, while Quack is experimental anyway. Serve it from the
  control plane's own process: that was tested, with no deadlock. The C
  extension is the fallback if a release stops calling Python UDFs, or if
  the control plane's Python work keeps the GIL busy enough to slow readers,
  since it doesn't need the GIL. It is the same speed otherwise. Shipping it
  means a binary per platform, built in CI with zig, and
  `allow_unsigned_extensions` on the serving instance. A C++
  extension would be faster only by calling DuckDB's parser directly, at
  the price of a rebuild for every DuckDB release. Revisit all of it when
  the DuckLake REST API ships.
- Option A (`__runs` as a node input) could ship first. With partitioned
  outputs, `__runs` becomes a manifest over each date's latest output, so A
  and C converge. Reading it as one `read_parquet` over a list would halve
  its cost today.

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
- [DuckLake roadmap](https://ducklake.select/roadmap)
- [DuckLake REST API PR](https://github.com/duckdb/ducklake/pull/1930)
- Quack issues: [#325 replaceable SQL callbacks](https://github.com/duckdb/duckdb-quack/issues/325),
  [#347 per-user principals](https://github.com/duckdb/duckdb-quack/pull/347),
  [#366 quack_serve and enable_external_access](https://github.com/duckdb/duckdb-quack/issues/366)
- Quack docs: [overview](https://duckdb.org/docs/2.0/quack/overview.html),
  [reference](https://duckdb.org/docs/2.0/quack/reference.html),
  [security](https://duckdb.org/docs/2.0/quack/security.html)
- [Using DuckDB Quack as the DuckLake catalog](https://www.definite.app/blog/duckdb-quack-ducklake-catalog)
- [Talk Python: DuckLake episode](https://talkpython.fm/episodes/show/562/ducklake-the-lakehouse-thats-just-sql-and-parquet)
- [MotherDuck: DuckLake architecture deep dive](https://motherduck.com/blog/ducklake-architecture-deep-dive/)
