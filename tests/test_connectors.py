"""The Lobster connector: auth is optional, errors surface, empty results keep columns."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from exuvia.connectors import lobster


class Recorder(BaseHTTPRequestHandler):
    seen: list[dict] = []
    reply: tuple[int, dict] = (200, {})

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        Recorder.seen.append(
            {"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        status, payload = Recorder.reply
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _handle

    def log_message(self, *args):  # keep test output quiet
        pass


@pytest.fixture
def api(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Recorder.seen = []
    monkeypatch.setenv("LOBSTER_API", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    yield Recorder
    server.shutdown()
    server.server_close()


def test_query_returns_a_frame_and_sends_no_auth_without_a_token(api):
    api.reply = (200, {"columns": ["symbol", "px"], "rows": [{"symbol": "A", "px": 1.5}]})
    df = lobster.query("select 1", limit=7)
    assert df.to_dict("records") == [{"symbol": "A", "px": 1.5}]
    assert api.seen == [
        {"method": "POST", "path": "/api/query", "auth": None, "body": {"sql": "select 1", "limit": 7}}
    ]


def test_token_is_sent_when_set(api, monkeypatch):
    monkeypatch.setenv("LOBSTER_TOKEN", "t0ken")
    api.reply = (200, {"columns": [], "rows": []})
    lobster.query("select 1")
    assert api.seen[0]["auth"] == "Bearer t0ken"


def test_empty_result_keeps_its_columns(api):
    api.reply = (200, {"columns": ["symbol", "px"], "rows": []})
    df = lobster.query("select 1")
    assert list(df.columns) == ["symbol", "px"] and df.empty


def test_query_error_in_a_200_body_raises(api):
    api.reply = (200, {"columns": [], "rows": [], "error": "No field named nope"})
    with pytest.raises(lobster.LobsterError, match="No field named nope"):
        lobster.query("select nope")


def test_truncated_result_raises_unless_allowed(api):
    rows = [{"n": i} for i in range(3)]
    api.reply = (200, {"columns": ["n"], "rows": rows, "row_count": 3, "truncated": True, "limit": 3})
    with pytest.raises(lobster.LobsterError, match="more than 3 rows"):
        lobster.query("select n from t", limit=3)
    df = lobster.query("select n from t order by n", limit=3, allow_truncated=True)
    assert len(df) == 3


def test_complete_result_is_returned(api):
    api.reply = (200, {"columns": ["n"], "rows": [{"n": 1}], "row_count": 1, "truncated": False, "limit": 10000})
    assert len(lobster.query("select n from t")) == 1


def test_http_error_carries_the_api_message(api):
    api.reply = (403, {"error": "not available to workflows"})
    with pytest.raises(lobster.LobsterError, match="HTTP 403: not available to workflows"):
        lobster.get("/api/admin/users")


class FakeDuck:
    def __init__(self, loaded=True):
        self.calls: list[tuple] = []
        self.loaded = loaded

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("LOAD") and not self.loaded:
            self.loaded = True  # first LOAD fails, as without the extension installed
            raise RuntimeError("extension not installed")
        return self


@pytest.fixture
def duck(monkeypatch):
    fake = FakeDuck()
    monkeypatch.setattr(lobster, "_connect", lambda: fake)
    monkeypatch.setattr(lobster, "_lake_con", None)
    monkeypatch.setenv("LOBSTER_ICEBERG_ENDPOINT", "http://iceberg.internal")
    monkeypatch.setenv("LOBSTER_ICEBERG_WAREHOUSE", "acct_bucket")
    monkeypatch.delenv("LOBSTER_ICEBERG_TOKEN", raising=False)
    return fake


def test_lake_attaches_read_only_through_the_endpoint(duck):
    con = lobster.lake()
    assert con is duck and lobster.lake() is duck  # opened once per process
    sqls = [sql for sql, _ in duck.calls]
    assert "LOAD httpfs" in sqls and "LOAD iceberg" in sqls
    attach = next(sql for sql in sqls if sql.startswith("ATTACH"))
    assert "'acct_bucket' AS lake" in attach
    assert "ENDPOINT 'http://iceberg.internal'" in attach and "READ_ONLY" in attach
    secret_sql, secret_params = next(c for c in duck.calls if "CREATE OR REPLACE SECRET" in c[0])
    assert secret_params == {"token": "attached-by-gateway"}  # placeholder: the gateway attaches the real one
    assert "attached-by-gateway" not in secret_sql  # bound, never interpolated


def test_lake_binds_a_real_token_for_local_use(duck, monkeypatch):
    monkeypatch.setenv("LOBSTER_ICEBERG_TOKEN", "tok'en")
    lobster.lake()
    secret_sql, params = next(c for c in duck.calls if "SECRET" in c[0])
    assert params == {"token": "tok'en"} and "tok'en" not in secret_sql


def test_lake_installs_a_missing_extension(duck):
    duck.loaded = False
    lobster.lake()
    sqls = [sql for sql, _ in duck.calls]
    assert sqls[:3] == ["LOAD httpfs", "INSTALL httpfs", "LOAD httpfs"]


def test_lake_needs_configuration(duck, monkeypatch):
    monkeypatch.delenv("LOBSTER_ICEBERG_ENDPOINT")
    with pytest.raises(lobster.LobsterError, match="not configured"):
        lobster.lake()


def test_lake_refuses_odd_configuration(duck, monkeypatch):
    monkeypatch.setenv("LOBSTER_ICEBERG_WAREHOUSE", "x' AS y (TYPE ICEBERG); --")
    with pytest.raises(lobster.LobsterError, match="unexpected characters"):
        lobster.lake()


def test_get_encodes_params_and_drops_none(api):
    api.reply = (200, {"items": [1]})
    assert lobster.get("/api/news", symbol="BRK.B", limit=None) == {"items": [1]}
    assert api.seen[0]["path"] == "/api/news?symbol=BRK.B" and api.seen[0]["method"] == "GET"
    with pytest.raises(ValueError):
        lobster.get("api/news")
