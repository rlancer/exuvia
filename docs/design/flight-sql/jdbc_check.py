"""Drive the Flight SQL prototype (server.py) with the Arrow Flight SQL JDBC
driver, the driver DBeaver, Tableau and other JVM tools connect with.

    uv run --with httpx python docs/design/flight-sql/jdbc_check.py

It needs Java 17 or later. It starts server.py on a fresh hub, makes four
projects over HTTP (two small ones, one of 2,000 rows, and one with a column
of each common type), makes data keys, downloads the driver jar from Maven
Central into ~/.cache (or uses $FLIGHT_SQL_JDBC_JAR), and runs
JdbcCheck.java with it: the metadata calls DBeaver makes when it connects
and fills its navigator, the queries its SQL editor and data viewer run, and
the key rules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx

HERE = Path(__file__).parent
HTTP, FLIGHT = 7460, 7461
DRIVER = "20.0.0"
JAR_URL = (f"https://repo1.maven.org/maven2/org/apache/arrow/flight-sql-jdbc-driver/{DRIVER}/"
           f"flight-sql-jdbc-driver-{DRIVER}.jar")


def node(name: str, body: str) -> str:
    return f"import exuvia\n\n\n@exuvia.node()\ndef {name}():\n" + "".join(f"    {line}\n" for line in body.splitlines())


PROJECTS = {
    "sales": ("orders", node("orders", 'import pandas as pd\n\nreturn pd.DataFrame({"account": [1, 1, 2], "amount": [10.0, 5.0, 7.5]})')),
    "crm": ("accounts", node("accounts", 'import pandas as pd\n\nreturn pd.DataFrame({"account": [1, 2], "name": ["Acme", "Globex"]})')),
    "big": ("many", node("many", 'import pandas as pd\n\nreturn pd.DataFrame({"n": range(2000), "s": [f"row {i}" for i in range(2000)]})')),
    "types": ("typed", node("typed", '''import datetime as dt
from decimal import Decimal

import pyarrow as pa

return pa.table({
    "i": pa.array([1, 2], pa.int64()),
    "small": pa.array([1, None], pa.int16()),
    "f": pa.array([1.5, None], pa.float64()),
    "flag": pa.array([True, False]),
    "amount": pa.array([Decimal("12.34"), Decimal("-0.01")], pa.decimal128(10, 2)),
    "d": pa.array([dt.date(2026, 11, 27), None], pa.date32()),
    "ts": pa.array([dt.datetime(2026, 11, 27, 9, 30, 15), None], pa.timestamp("us")),
    "tstz": pa.array([dt.datetime(2026, 11, 27, 9, 30, 15, tzinfo=dt.timezone.utc), None], pa.timestamp("us", "UTC")),
    "txt": pa.array(["héllo", None]),
    "tags": pa.array([["a", "b"], []], pa.list_(pa.string())),
})''')),
}


def jar() -> Path:
    if os.environ.get("FLIGHT_SQL_JDBC_JAR"):
        return Path(os.environ["FLIGHT_SQL_JDBC_JAR"])
    path = Path.home() / ".cache" / "exuvia-flight-sql" / f"flight-sql-jdbc-driver-{DRIVER}.jar"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {JAR_URL}", file=sys.stderr)
        urllib.request.urlretrieve(JAR_URL, path.with_suffix(".part"))
        path.with_suffix(".part").rename(path)
    return path


def main() -> None:
    driver = jar()
    root = Path(tempfile.mkdtemp(prefix="exuvia-jdbc-"))
    env = {**os.environ, "EXUVIA_AUTH": "token"}
    env.pop("EXUVIA_API_TOKEN", None)
    srv = subprocess.Popen(
        [sys.executable, str(HERE / "server.py"), str(root), "--http", str(HTTP), "--flight", str(FLIGHT)], env=env,
    )
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{HTTP}", timeout=300)
        for _ in range(200):
            try:
                http.get("/healthz")
                break
            except httpx.TransportError:
                time.sleep(0.1)
        time.sleep(0.5)  # Flight starts once HTTP answers
        for pid, (name, src) in PROJECTS.items():
            http.post("/api/projects", json={"id": pid}).raise_for_status()
            http.put(f"/p/{pid}/api/nodes/{name}", json={"source": src}).raise_for_status()
            run = http.post(f"/p/{pid}/api/nodes/{name}/run", json={"wait": 300}).json()
            assert run["ok"], run
        key = http.post("/api/data-keys", json={"name": "jdbc", "projects": list(PROJECTS)}).json()["key"]
        narrow = http.post("/api/data-keys", json={"name": "sales only", "projects": ["sales"]}).json()["key"]
        url = f"jdbc:arrow-flight-sql://127.0.0.1:{FLIGHT}/?useEncryption=false&token="
        java = ["java", "--add-opens=java.base/java.nio=ALL-UNNAMED", "-cp", str(driver), str(HERE / "JdbcCheck.java"),
                url + key, url + narrow]
        code = subprocess.run(java).returncode
    finally:
        srv.terminate()
        srv.wait(10)
    sys.exit(code)


if __name__ == "__main__":
    main()
