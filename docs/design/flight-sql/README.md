# Arrow Flight SQL for the data API: prototype

Can exuvia's data API (`/api/data/`, see the README's "Data API") also be
served over [Arrow Flight SQL](https://arrow.apache.org/docs/format/FlightSql.html),
so BI tools and other languages connect with a standard driver instead of
our REST shape? This is a working prototype and what it showed.
Exploration code, not part of the exuvia package: nothing imports it and CI
doesn't run it.

| File | What it is |
|---|---|
| `server.py` | A Flight SQL server beside the real hub, in the same process: `create_hub_app` on one port, Flight on another, sharing the hub's data keys, sharing rules, loaded projects and cached query connections |
| `check.py` | Starts `server.py` on a fresh hub, makes projects and data keys over HTTP, and drives Flight SQL with the stock ADBC driver: answers, discovery, the key rules, timings |

```bash
uv run --with adbc-driver-flightsql --with adbc-driver-manager --with httpx \
    python docs/design/flight-sql/check.py
```

To try it by hand, run the server on a hub directory and connect any Flight
SQL client to `grpc://127.0.0.1:7421` with the header
`authorization: Bearer exd_...` (make a key in the hub's UI on port 7420):

```bash
uv run python docs/design/flight-sql/server.py ./my-hub --http 7420 --flight 7421
```

```python
import adbc_driver_flightsql.dbapi as flight_sql

conn = flight_sql.connect("grpc://127.0.0.1:7421",
                          db_kwargs={"adbc.flight.sql.authorization_header": "Bearer exd_..."})
cur = conn.cursor()
cur.execute('select * from "sales".orders join crm.accounts using (account)')
table = cur.fetch_arrow_table()   # or cur.fetch_df()
```

## What it implements

The same contract as `POST /api/data/query`: a data key, one read-only
SELECT, each project a catalog. Of Flight SQL:

- **Queries**: `CommandStatementQuery`, and prepared statements
  (`CreatePreparedStatement`, `CommandPreparedStatementQuery`,
  `ClosePreparedStatement`), which ADBC's DB-API cursor uses for every
  query. exuvia's SQL takes no parameters, so a prepared statement is
  stateless: its handle is the SQL and the projects, the same as a ticket.
  Any replica can answer any step.
- **Discovery**: `CommandGetCatalogs`, `CommandGetDbSchemas`,
  `CommandGetTables` (with `include_schema`), `CommandGetTableTypes`, from
  DuckDB's `information_schema` over the key's projects only. That is what
  `adbc_get_objects`, `adbc_get_table_schema` and a SQL client's schema
  browser call.
- **`CommandGetSqlInfo`**: name, version, Arrow version, read-only.
- Not implemented: bound parameters, transactions, Substrait, DoPut,
  cancellation.

The protobuf messages are decoded by hand (about 60 lines): they are a few
string, bool and repeated fields, and this keeps generated code and a
protobuf dependency out. `pyarrow.flight` is in the `pyarrow` wheel exuvia
already depends on, so serving Flight adds no dependency.

**Auth** is the HTTP data API's, unchanged: `authorization: Bearer exd_...`
on every call, checked against the hub's data keys and against what the
key's maker can still open. DoGet checks again before it runs a ticket, so
a key revoked, or a project unshared, between planning and fetching stops
the fetch.

## What `check.py` showed

All checks pass with the stock ADBC Flight SQL driver (1.13):

- **Same answers** as the HTTP API for a cross-project join; `adbc_get_info`
  names the server.
- **Discovery works**: `adbc_get_objects` lists the key's catalogs, schemas
  and tables and nothing else (a narrower key sees only its catalog);
  `adbc_get_table_schema` gives the columns.
- **The key rules hold**: no key, a bad key, a revoked key and a project the
  key doesn't read all fail; `DROP`, `ATTACH`, `SET`, `COPY`, `read_csv`
  and two statements in one are rejected, and the shared connection still
  answers afterwards.

Timings, median, one client on the same machine (`market.ticks` is 2,000,000
rows of int64, double and text; HTTP is the data API on the same server):

| | Flight SQL | HTTP Arrow | HTTP JSON |
|---|---|---|---|
| small cross-project join | 13.1 ms | 5.6 ms | 5.4 ms |
| 5,000 rows | 18.5 ms | 10.9 ms | 21.4 ms |
| 2,000,000 rows | 86.7 ms | 133.2 ms | (capped at 5,000) |
| an aggregate over them | 27.4 ms | 21.2 ms | 21.2 ms |

- **Big results are faster over Flight**, about 1.5× for 2 million rows:
  gRPC streams record batches as they are, where HTTP sends one chunked body
  the client reads and parses.
- **Small queries cost about 7 ms more.** ADBC makes four calls per query
  (prepare, plan, fetch, close), and each checks the key and reaches the
  hub's loaded projects from the Flight thread. Some of that can go: carry
  the planned schema in the handle so the plan call needn't bind again, and
  keep the workspaces the server last used, but the round trips are the
  protocol's.
- **The first version ran every query three times.** Reading a result's
  schema with `to_arrow_reader` executes the query; the server now binds a
  `LIMIT 0` of it instead, which took the aggregate from 64 ms to 27 ms.
  Worth remembering for any code that needs a schema before the data.

## What shipping it would take

- `exuvia hub --flight-port` (or `EXUVIA_FLIGHT_PORT`) starting this server
  beside the HTTP one, with the code in `exuvia/` sharing `dataapi.py`'s key
  checks instead of copying them as `server.py` does.
- **TLS**: the key travels in a header, so Flight needs `grpc+tls`, with its
  own certificate settings, or a TLS-terminating proxy that speaks HTTP/2.
- **A second port to deploy**: the Compose, Swarm and Kubernetes files, and
  an ingress that passes gRPC (HTTP/2 end to end). That is the real cost:
  every deployment gets one more thing to expose and secure.
- A statement timeout and cancellation, which the HTTP API doesn't have
  either.
- Not tested yet: the Flight SQL JDBC driver (DBeaver, Tableau, Superset)
  and the Go and Java ADBC drivers. They speak the same protocol, but a
  JDBC driver can ask for `SqlInfo` values or metadata commands this
  prototype doesn't answer.

## Leaning

Worth shipping as an opt-in, for what HTTP can't do: BI tools and SQL
clients that connect through a Flight SQL driver, and code that pulls large
results into dataframes in another language. HTTP stays the data API's
default, for agents and apps: it is faster for small queries, needs no
second port, and has the OpenAPI spec (`/api/data/openapi.json`). Before
shipping, connect DBeaver through the JDBC driver to find what else it asks
for.
