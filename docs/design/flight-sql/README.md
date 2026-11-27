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
| `jdbc_check.py`, `JdbcCheck.java` | The same with the Arrow Flight SQL **JDBC** driver (what DBeaver, Tableau and other JVM tools use): the calls DBeaver makes to connect, fill its navigator and run queries, and the key rules |

```bash
uv run --with adbc-driver-flightsql --with adbc-driver-manager --with httpx \
    python docs/design/flight-sql/check.py
```

The JDBC check needs Java 17 or later; it downloads the driver jar from
Maven Central into `~/.cache` (or set `FLIGHT_SQL_JDBC_JAR`):

```bash
uv run --with httpx python docs/design/flight-sql/jdbc_check.py
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
- **`CommandGetSqlInfo`**: name, version, Arrow version, read-only, no
  transactions, the identifier quote (`"`), "project" and "schema" as the
  catalog and schema terms, and DuckDB's keywords.
- **Keys**: `CommandGetPrimaryKeys`, `CommandGetImportedKeys`,
  `CommandGetExportedKeys`, `CommandGetCrossReference` answer no rows
  (outputs are views, with no keys). JDBC tools ask when they open a table.
- **`Handshake`**: the JDBC driver opens every connection with one, sending
  the bearer token. It exchanges nothing and hands the same token back; the
  key is still checked on every call.
- Not implemented: bound parameters, transactions, Substrait, DoPut,
  cancellation, session options (`SetSessionOptions`, which JDBC's
  `setCatalog` sends).

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
rows of int64, double and text; HTTP is the data API on the same server).
HTTP Arrow reads the body as it arrives, as ADBC does; "buffered" is the
benchmark's first client, which took the whole body and then parsed it:

| | Flight SQL | HTTP Arrow | (buffered) | HTTP JSON |
|---|---|---|---|---|
| small cross-project join | 15.3 ms | 7.3 ms | 7.5 ms | 8.1 ms |
| 5,000 rows | 21.8 ms | 16.5 ms | 16.0 ms | 4.2 ms |
| 2,000,000 rows | 92.6 ms | 103.2 ms | 172.3 ms | (capped at 5,000) |
| an aggregate over them | 36.4 ms | 28.1 ms | 28.2 ms | 1.7 ms |

Over runs, 2,000,000 rows took 89–102 ms over Flight, 103–117 ms over
streamed HTTP Arrow and 168–186 ms buffered.

- **Big results: HTTP is within about 10% of Flight.** Most of the old 2×
  gap was the benchmark. See "HTTP Arrow" below.
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

## HTTP Arrow: where the 2,000,000 rows go

The question was whether the data API's Arrow answers are slower than
Flight because they are HTTP, or because of something we can fix. The test
was `select * from market.ticks` over `POST /api/data/query` with
`Accept: application/vnd.apache.arrow.stream`, timed in stages, with
timestamps inside `ArrowStream` and py-spy on the server:

| stage | time |
|---|---|
| request to first byte (key, plan, connection, first batch) | ~16 ms |
| DuckDB producing the batches (`to_arrow_reader`, in process) | 65–80 ms |
| Arrow IPC writes of those batches (`write_batch`) | 10–20 ms of that |
| between handing Starlette a chunk and being asked for the next | ~30 ms (43 chunks, ~0.7 ms each) |
| all bytes to a client that discards them (httpx streaming; curl) | 95–110 ms |
| client decode of the body once it is in memory | 5–9 ms |
| buffered client: `r.content`, then parse | +45–50 ms over streaming |

The body is 46 MB, 41 record batches of up to 50,000 rows. DuckDB splits a
batch where a row group ends, giving 50,000, 48,576 and 1,424 rows. That
comes to 43 chunks with the schema and end marker. No middleware compresses
or re-encodes the body.

- **The gap was mostly the client.** The benchmark's HTTP client waited for
  the whole 46 MB body, joined about 720 reads into one buffer, then parsed
  it. ADBC decodes batches as they arrive. With the client reading the
  stream as it comes (`check.py` now does both), 2,000,000 rows went from
  about 172 ms to about 103 ms, against about 93 ms over Flight.
- **The server is close to DuckDB's floor.** Producing the Arrow batches
  takes 65–80 ms in process, and `to_arrow_table()` is no faster. The
  Flight prototype reads the same way (`to_arrow_reader`), so both pay that.
- **What is left, about 10 ms, is Python per chunk.** Over HTTP each batch
  is written to the IPC stream in Python, then handed through Starlette's
  thread hop and the auth middleware to uvicorn. Flight writes from C++.
  One fix was tried: read one batch ahead on a worker thread, so DuckDB
  works while the previous chunk is sent. In three alternating rounds it
  made no measurable difference (curl 103–110 ms with and without it), so
  it was not kept. Larger batches would change the bytes and the batch
  sizes clients see, for a few milliseconds at most (1,000,000-row batches
  took DuckDB 65 ms against 78 ms), so the server is unchanged.
- **The auth middleware costs nothing measurable.** `require_auth`
  (`server.py`) is a Starlette `@app.middleware("http")`, which re-streams
  every response body through itself, even for the data API it passes
  straight through. With it unregistered, in three alternating rounds:
  2,000,000 rows took 94–112 ms against 98–111 ms with it, and `select 1`
  3.6–3.9 ms against 3.7–4.0 ms. 50,000 rows took 23–26 ms against
  24–27 ms. Every difference is inside the run-to-run noise, so rewriting
  it as plain ASGI middleware would not buy throughput.
- **Clients have to stream too.** In JavaScript, `await tableFromIPC(res)`
  read the 2,000,000 rows in 119 ms against 172 ms for
  `tableFromIPC(new Uint8Array(await res.arrayBuffer()))`. In Python,
  `pa.ipc.open_stream(urllib.request.urlopen(...))` took 100 ms against
  162 ms for `requests` with the whole body first. The main README's
  "Binary data" examples now stream.
- **What this means for shipping Flight.** "Large pulls into dataframes"
  is not a reason to ship Flight, provided the client streams. A Python
  client can stream with pyarrow and httpx, as `check.py` does. Flight's
  remaining advantages are the drivers (ADBC, JDBC, and BI tools that
  speak Flight SQL), not throughput.

## What the JDBC check showed

Driver: `org.apache.arrow:flight-sql-jdbc-driver` 20.0.0, the one DBeaver
downloads for its "Arrow Flight SQL" connection type. `JdbcCheck.java` makes
the `DatabaseMetaData`, `Statement` and `ResultSet` calls DBeaver makes; it
is not DBeaver itself (that is a desktop app, not run here).

**It connects, browses and queries.** Every call DBeaver needs passes:

- **Connecting**: product name and version, read-only, no transactions,
  autocommit, the identifier quote, the keywords.
- **The navigator**: the key's projects as catalogs, `main` as their schema,
  each output as a `VIEW` with its columns and JDBC types, and nothing else:
  a narrower key sees only its own catalog. Outputs list under Views, not
  Tables, since they are DuckDB views.
- **Queries**: the cross-project join; `SELECT * FROM crm.main.accounts`,
  quoted or not, and the `COUNT(*)` around it (what the data viewer writes
  for a table); prepared statements and their metadata; `setMaxRows` (the
  data viewer's page); errors with DuckDB's message; writes refused and the
  connection still answering after.
- **Types**: `BIGINT`, `DOUBLE`, `BOOLEAN`, `DECIMAL` (as `BigDecimal`),
  `DATE`, `TIMESTAMP`, `TIMESTAMP_WITH_TIMEZONE`, `VARCHAR` (UTF-8) and
  lists as `ARRAY` all map, through `getObject`, `getString` and the typed
  getters.
- **The key rules**: no token is refused, a narrower key can't query
  another project.

**What it took** (all in `server.py` now):

1. **A handshake.** The driver calls Flight's `Handshake` before anything
   else, and the first version had none, so the driver could not connect at
   all: "This service does not have an authentication mechanism enabled".
   ADBC never sends one, so `check.py` hadn't caught it. `pyarrow.flight`
   only answers a handshake with an `auth_handler`, so there is a no-op one;
   the key middleware echoes the bearer token back, which the driver then
   sends on every call.
2. **The identifier quote.** Without `SQL_IDENTIFIER_QUOTE_CHAR` the driver
   reports an empty quote string, and a tool that quotes the names it writes
   would write them bare.
3. **Empty key answers.** `getPrimaryKeys`, `getImportedKeys` and
   `getExportedKeys` were "not implemented"; DBeaver asks for the primary
   key when it opens a table.

**What is still missing:** `Connection.setCatalog` sends the Flight SQL
session action `SetSessionOptions`, which isn't implemented. DBeaver calls it
when you pick a default catalog for an editor, so unqualified names
(`select * from orders`) won't resolve. Qualified names always work. Doing
it means session state (Flight SQL keeps it in a cookie), which the
stateless handles here avoid; answering "ok" and ignoring it would be worse.

`check.py` (ADBC) still passes with these changes.

## What shipping it would take

- `exuvia hub --flight-port` (or `EXUVIA_FLIGHT_PORT`) starting this server
  beside the HTTP one, with the code in `exuvia/` sharing `dataapi.py`'s key
  checks instead of copying them as `server.py` does.
- **TLS and routing at the proxy**: the key travels in a header, so Flight
  needs TLS. The proxy in front of the hub can terminate it, as it does for
  HTTP. See "Deploying it behind one address" below. Flight needs no public
  port of its own.
- **A second server process.** pyarrow's Flight server is its own gRPC
  server, written in C++, on its own socket. It can't be mounted in the
  FastAPI app, and uvicorn speaks only HTTP/1.1, so inside the container it
  listens on a port of its own (7421 below) beside uvicorn's 7420. That
  port is internal: it is not published, and only the proxy reaches it.
- A statement timeout and cancellation, which the HTTP API doesn't have
  either.
- Session options (`SetSessionOptions`) if `setCatalog` matters, with the
  state behind a cookie that every replica can read.
- Not tested yet: DBeaver itself (the JDBC driver is), Tableau and
  Superset, the ODBC driver, and the Go and Java ADBC drivers.

## Deploying it behind one address

gRPC is HTTP: it runs over HTTP/2, with its status in HTTP/2 trailers. What
it needs from a deployment is HTTP/2 kept end to end. It needs no second
public port. Clients reach Flight at the hub's own address,
`grpc+tls://exuvia.example.com:443`, and the proxy in front sends Flight's
requests to the Flight server and everything else to uvicorn.

```
client ──TLS, HTTP/2──▶ proxy :443 ──┬─ /arrow.flight.protocol.FlightService/* ─ h2c ─▶ Flight :7421
                                     └─ everything else ─────────── HTTP/1.1 ─▶ uvicorn :7420
```

- **Choosing what goes to Flight.** Every Flight and Flight SQL call is a
  gRPC method under `/arrow.flight.protocol.FlightService/`, so the proxy
  can route on that path prefix. That is more reliable than matching
  `Content-Type: application/grpc`, which also comes as
  `application/grpc+proto`. A hostname of its own, such as
  `flight.exuvia.example.com`, works as well, if Flight traffic should be
  kept apart in logs and certificates.
- **Two ports inside the container.** Two servers can't listen on one
  port, so the proxy sends Flight to 7421 and the rest to 7420. Neither is
  published. Getting down to one internal port would mean serving Flight
  from Python on an HTTP/2 ASGI server (hypercorn), which gives up the C++
  data path, or running a proxy inside the container, which is the same hop
  again. Neither is worth it.
- **Traefik**, which the Compose and Swarm files already use (as labels left
  commented out). It speaks HTTP/2 to clients on a TLS entrypoint and h2c
  (HTTP/2 without TLS) to a backend whose scheme says so. That makes one
  more router on the same host and entrypoint. Once the container has two
  services, each router names its own:

  ```yaml
  traefik.http.routers.exuvia.rule: Host(`${EXUVIA_HOST}`)
  traefik.http.routers.exuvia.service: exuvia
  traefik.http.services.exuvia.loadbalancer.server.port: "7420"

  traefik.http.routers.exuvia-flight.rule: Host(`${EXUVIA_HOST}`) && PathPrefix(`/arrow.flight.protocol.FlightService/`)
  traefik.http.routers.exuvia-flight.entrypoints: websecure
  traefik.http.routers.exuvia-flight.tls.certresolver: letsencrypt
  traefik.http.routers.exuvia-flight.service: exuvia-flight
  traefik.http.services.exuvia-flight.loadbalancer.server.port: "7421"
  traefik.http.services.exuvia-flight.loadbalancer.server.scheme: h2c
  ```

  On Kubernetes, Traefik reads Ingress resources and its own IngressRoute,
  not labels. The backend service needs the same `h2c` scheme there.
- **nginx** (`deploy/nginx/exuvia.conf`): `grpc_pass grpc://...:7421` in a
  `location /arrow.flight.protocol.FlightService/`, with `http2` on the
  `listen`. ingress-nginx takes the annotation
  `nginx.ingress.kubernetes.io/backend-protocol: "GRPC"` on an Ingress for
  that path.
- **Elsewhere**: a cloud load balancer has to be set up for gRPC (AWS's ALB
  needs a gRPC target group, Cloudflare needs gRPC switched on), and some
  PaaS hosts can't pass it at all. Those deployments would go without
  Flight, which is why it would be opt-in.
- **Browsers** can't make raw gRPC calls, so Flight serves drivers and
  tools only, never the workbench or a web app on the data API.

None of this has been run. The prototype listens on two ports and was
tested directly, without a proxy. The labels and settings above are what
the Traefik and nginx documentation call for, to try when Flight ships.

## Leaning

Worth shipping as an opt-in, for what HTTP can't do: BI tools and SQL
clients that connect through a Flight SQL driver, and code in another
language that already speaks ADBC. Speed for large pulls is not a reason:
streamed HTTP Arrow comes within about 10% of Flight (see "HTTP Arrow"
above). HTTP stays the data API's default, for agents and apps: it is faster
for small queries, needs no second server or HTTP/2 proxy, and has the OpenAPI spec
(`/api/data/openapi.json`).

The JDBC check took away the main unknown: with three small fixes the stock
JDBC driver connects, browses and queries, so DBeaver and other JDBC tools
should work, apart from choosing a default catalog. The deployment cost is
smaller than first thought. Flight needs no public port: behind Traefik it
is a few more labels on the same host and TLS entrypoint (see "Deploying it
behind one address"). What's left against it is upkeep: a second server
process in the container, key checks that must keep matching the data
API's, and a quirk or two for each BI tool. That is worth taking on only
once someone needs a BI tool to connect, and then as an opt-in, off by
default.
