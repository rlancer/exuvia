# Read-only Quack tokens: prototype and tests

Supporting code for "Read-only policy" in
[`../incremental-nodes-and-ducklake.md`](../incremental-nodes-and-ducklake.md).
Exploration code, not part of the exuvia package: nothing imports it and CI
doesn't run it.

| File | What it is |
|---|---|
| `policy.py` | The policy: token roles, the reader allowlist, `lock_down`, `self_test` |
| `serve.py` | A Quack server with the policy, the lockdown and a watchdog that cancels long reader statements |
| `serve_log.py` | A Quack server that logs every statement with its token's role |
| `workload.py` | A DuckLake writer and two readers doing everything a reader normally does |
| `attacks.py` | 110 attempts to write, read files, escape or stop the server with the reader token |
| `concurrency.py` | Six readers in a loop while a writer commits |
| `shadow_test.py` | Shows why the hook names are qualified with `system.main.` |
| `bench.py` | Cost of the hook per statement, over a log from `serve_log.py` |
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

`serve.py` takes `--no-policy` or `--no-lockdown` to test either layer alone.
`bench.py` and `analyze.py` take the log that `serve_log.py` writes.

`run.sh` stops the server by killing whatever listens on the given port, and
uses PowerShell to find it (written on Windows).

When the DuckLake or Quack extension version changes, rerun `workload.py`
against `serve.py`: a reader statement the allowlist doesn't know is denied,
and the workload fails. Then rerun `serve_log.py` and `analyze.py` to see
what the new version sends.
