# Partitioned outputs: prototype and measurements

Supporting code for "Partitioned outputs without DuckLake (prototyped)" in
[`../incremental-nodes-and-ducklake.md`](../incremental-nodes-and-ducklake.md).
Exploration code, not part of the exuvia package: nothing imports it and CI
doesn't run it.

| File | What it is |
|---|---|
| `manifest.py` | A node output as a manifest of content-addressed Parquet chunks: replace partitions (a daily run or a backfill), compact, prune, and the logical and physical ids |
| `bench.py` | Builds a daily node's history in one backfill, then times exuvia's sandboxed reads over one file, today's `__runs` union, the manifest's file list and the manifest compacted by year; then commits, a correction, identical rewrites and a sandbox escape |

It needs only the `duckdb` that exuvia already depends on. From this folder,
with a work directory that doesn't exist yet:

```bash
uv run --project ../../.. python bench.py /tmp/po-small --days 1200 --tickers 20
```

```bash
uv run --project ../../.. python bench.py /tmp/po-full /tmp/po-full.json
```

The full run (10,000 days of 500 tickers) takes about 6 minutes, most of it
reading 10,000 uncompacted chunks.
