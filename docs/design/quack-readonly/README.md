# Read-only Quack tokens: prototype and tests

Supporting code for "Read-only policy" in
[`../incremental-nodes-and-ducklake.md`](../incremental-nodes-and-ducklake.md).
Exploration code, not part of the exuvia package: nothing imports it and CI
doesn't run it.

| File | What it is |
|---|---|
| `policy.py` | The policy as Python UDFs: token roles, the reader allowlist, `lock_down`, `self_test` |
| `policy_sql.py` | The same policy as SQL macros (writer = a registered client id) |
| `policy_native.py` | Loads the same policy as a C extension, `native/exuvia_policy.c` |
| `native/` | The extension's source, `build.sh` (zig, no MSVC or cmake) and the metadata footer script |
| `serve.py` | A Quack server with a policy (`--sql-policy`, `--native-policy EXT`, default Python), the lockdown and a watchdog that cancels long reader statements |
| `serve_log.py` | A Quack server that logs every statement with its token's role |
| `workload.py` | A DuckLake writer and two readers doing everything a reader normally does |
| `attacks.py` | 110 attempts to write, read files, escape or stop the server with the reader token |
| `concurrency.py` | Six readers in a loop while a writer commits |
| `sandbox_reader.py` | A DuckLake reader inside a connection locked like exuvia's query sandbox: reads, and escapes from the lock (sections 1 and 3 are expected to show `!!`: they are the failures) |
| `control_plane.py` | exuvia's real server (`create_app`) with Quack, the policy, the watchdog, an in-process writer and a sandboxed reader in the same process (`serve`), and a load that measures exuvia's HTTP API and the lake under it (`load`) |
| `control_plane.sh` | Starts `control_plane.py serve`, runs `load` against it, stops it |
| `shadow_test.py` | Shows why the hook names are qualified with `system.main.` |
| `bench.py` | Cost of the Python hook per statement, over a log from `serve_log.py` |
| `bench_hooks.py` | CPU cost per statement of all three hooks and a no-op floor, in one process |
| `analyze.py` | Summarises what a log's reader and writer statements touch |
| `run.sh` | Starts a server on a fresh catalog, runs one client against it, stops the server |

Everything runs on DuckDB 2.0.0 with the PyPI extension wheels, loaded by
file path. `run.sh` takes a work directory that must not exist yet:

```bash
sh run.sh /tmp/qro-workload 9601 serve.py /tmp/qro-workload.json -- workload.py
```

```bash
sh run.sh /tmp/qro-attacks 9602 serve.py /tmp/qro-attacks.json --timeout 3 -- attacks.py --expect-policy
```

```bash
sh run.sh /tmp/qro-lockdown 9604 serve.py /tmp/qro-lockdown.json --timeout 3 --no-policy -- attacks.py
```

```bash
sh run.sh /tmp/qro-log 9606 serve_log.py /tmp/qro-sql.jsonl -- workload.py
```

The other two policies, against the same clients:

```bash
sh run.sh /tmp/qro-sql 9607 serve.py /tmp/qro-sql.json --sql-policy --timeout 3 -- attacks.py --expect-policy
```

```bash
sh native/build.sh /tmp/qro-native-build
```

```bash
sh run.sh /tmp/qro-native 9608 serve.py /tmp/qro-native.json --native-policy /tmp/qro-native-build/exuvia_policy.duckdb_extension --timeout 3 -- attacks.py --expect-policy
```

```bash
sh run.sh /tmp/qro-sandbox 9609 serve.py /tmp/qro-sandbox.json -- sandbox_reader.py
```

Quack inside exuvia's server, with no Quack (the baseline), the Python hook
and the C extension. Each run takes about two minutes:

```bash
sh control_plane.sh /tmp/qro-cp-none 9611 9612 none
```

```bash
sh control_plane.sh /tmp/qro-cp-python 9613 9614 python
```

```bash
sh control_plane.sh /tmp/qro-cp-native 9615 9616 native /tmp/qro-native-build/exuvia_policy.duckdb_extension
```

`serve.py` takes `--no-policy` or `--no-lockdown` to test either layer alone.
`bench.py` and `analyze.py` take the log that `serve_log.py` writes.

`run.sh` stops the server by killing whatever listens on the given port, and
uses PowerShell to find it (written on Windows).

When the DuckLake or Quack extension version changes, rerun `workload.py`
against `serve.py`: a reader statement the allowlist doesn't know is denied,
and the workload fails. Then rerun `serve_log.py` and `analyze.py` to see
what the new version sends.
