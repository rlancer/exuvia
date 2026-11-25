"""Remote artifact store: SigV4 signing, the S3 client, and a hub whose
outputs live in a bucket instead of its export."""

import datetime as dt
import io
import json
import shutil
import tarfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from exuvia import artifacts
from exuvia.artifacts import S3Store, artifact_key, parse_store_url, set_artifact_store, sigv4_headers
from exuvia.server import create_hub_app

from test_hub import template, wait_idle

AS_OF = {"params": json.dumps({"as_of": "2026-03-02"})}


class FakeS3:
    """An in-memory, path-style S3 bucket behind an httpx transport."""

    def __init__(self, bucket: str = "outputs"):
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.fail_puts = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=key/")
        assert "x-amz-content-sha256" in request.headers and "x-amz-date" in request.headers
        url = urlsplit(str(request.url))
        bucket, _, key = url.path.lstrip("/").partition("/")
        assert bucket == self.bucket
        key = unquote(key)
        self.calls.append(f"{request.method} {key}")
        if request.method == "GET" and not key:
            query = parse_qs(url.query)
            prefix = query.get("prefix", [""])[0]
            items = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(self.objects) if k.startswith(prefix))
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"{items}<IsTruncated>false</IsTruncated></ListBucketResult>"
            )
            return httpx.Response(200, content=body.encode())
        if request.method == "PUT":
            if self.fail_puts:
                return httpx.Response(503, content=b"slow down")
            self.objects[key] = request.read()
            return httpx.Response(200)
        if request.method in ("GET", "HEAD"):
            if key not in self.objects:
                return httpx.Response(404)
            return httpx.Response(200, content=self.objects[key] if request.method == "GET" else b"")
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    # Any non-empty setting turns the remote store on; the pinned client is the fake.
    monkeypatch.setenv("EXUVIA_ARTIFACT_STORE", "s3://outputs/wf-tests")
    set_artifact_store(S3Store("outputs", "wf-tests", endpoint="http://s3.test", access_key="key", secret_key="secret", transport=fake.transport()))
    yield fake
    set_artifact_store(None)
    artifacts._configured = None


def test_sigv4_matches_the_aws_documented_example():
    # "GET Object" example from the AWS Signature Version 4 docs for S3.
    headers = sigv4_headers(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        {"Range": "bytes=0-9"},
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        payload_hash=artifacts.EMPTY_SHA256,
        now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_store_url_and_keys():
    assert parse_store_url("s3://bucket/some/prefix/") == ("bucket", "some/prefix")
    assert parse_store_url("s3://bucket") == ("bucket", "")
    with pytest.raises(ValueError):
        parse_store_url("https://bucket/x")
    assert artifact_key("wf", ".exuvia/artifacts/prices/abc.parquet") == "wf/prices/abc.parquet"
    assert artifact_key("wf", "nodes/prices.py") is None
    assert artifact_key("wf", ".exuvia/artifacts/../exuvia.db") is None


def test_client_round_trip(tmp_path):
    fake = FakeS3()
    store = S3Store("outputs", "pre", endpoint="http://s3.test", access_key="key", secret_key="s", transport=fake.transport())
    src = tmp_path / "a.json"
    src.write_text("[1]", encoding="utf-8")
    store.put("wf/n/a b.json", src)
    assert fake.objects == {"pre/wf/n/a b.json": b"[1]"}
    assert store.exists("wf/n/a b.json") and not store.exists("wf/n/missing.json")
    assert store.fetch("wf/n/a b.json", tmp_path / "out" / "a.json")
    assert (tmp_path / "out" / "a.json").read_text(encoding="utf-8") == "[1]"
    assert not store.fetch("wf/n/missing.json", tmp_path / "out" / "m.json")
    assert not (tmp_path / "out" / "m.json").exists()
    assert list(store.keys("wf/")) == ["wf/n/a b.json"]
    assert store.delete_under("wf/") == 1 and fake.objects == {}


def exported_names(blob: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return tar.getnames()


def test_outputs_live_in_the_store_not_the_export(tmp_path, s3):
    with TestClient(create_hub_app(tmp_path / "hub", template(tmp_path))) as hub:
        hub.post("/api/projects", json={"id": "wf", "template": "dated"})
        hub.post("/p/wf/api/run", json={"params": {"as_of": "2026-03-02"}})
        wait_idle(hub)
        [key] = s3.objects
        assert key.startswith("wf-tests/wf/prices/") and key.endswith(".parquet")
        names = exported_names(hub.get("/api/hub/export").content)
        assert "wf/nodes/prices.py" in names and "wf/.exuvia/exuvia.db" in names
        assert not any("/artifacts/" in n for n in names)
        blob = hub.get("/api/hub/export").content

    # A fresh disk: the restored project reads its output back from the store.
    with TestClient(create_hub_app(tmp_path / "elsewhere")) as fresh:
        assert fresh.post("/api/hub/import", content=blob).json()["projects"] == ["wf"]
        assert not (tmp_path / "elsewhere" / "wf" / ".exuvia" / "artifacts").exists()
        assert fresh.get("/p/wf/api/project", params=AS_OF).json()["nodes"][0]["state"] == "fresh"
        assert fresh.get("/p/wf/api/nodes/prices/data", params=AS_OF).json()["rows"] == [["2026-03-02", 2]]
        assert any(p.suffix == ".parquet" for p in (tmp_path / "elsewhere" / "wf").rglob("*"))

        # Deleting the workflow deletes its objects too.
        assert fresh.delete("/api/projects/wf").json() == {"ok": True}
        assert s3.objects == {}


def test_outputs_made_before_the_store_are_uploaded_later(tmp_path, s3):
    s3.fail_puts = True  # as if the store were down (or not configured yet)
    with TestClient(create_hub_app(tmp_path / "hub", template(tmp_path))) as hub:
        hub.post("/api/projects", json={"id": "wf", "template": "dated"})
        hub.post("/p/wf/api/run", json={"params": {"as_of": "2026-03-02"}})
        wait_idle(hub)
        assert s3.objects == {}
        names = exported_names(hub.get("/api/hub/export").content)
        assert any("/artifacts/prices/" in n for n in names)  # still carried: nothing lost
        blob = hub.get("/api/hub/export").content

    s3.fail_puts = False
    with TestClient(create_hub_app(tmp_path / "elsewhere")) as fresh:
        fresh.post("/api/hub/import", content=blob)
        v0 = fresh.get("/api/hub/state").json()["version"]
        fresh.get("/p/wf/api/project")  # loading the project starts its sync
        # The sync runs in the background and bumps the version once it has
        # uploaded everything (the hub is not "busy" meanwhile): wait for that.
        deadline = time.time() + 30
        while fresh.get("/api/hub/state").json()["version"] == v0 and time.time() < deadline:
            time.sleep(0.05)
        assert fresh.get("/api/hub/state").json()["version"] > v0  # the host saves the slimmer export
        assert len(s3.objects) == 1
        assert not any("/artifacts/" in n for n in exported_names(fresh.get("/api/hub/export").content))


def test_missing_local_copy_is_downloaded_for_downstream_runs(tmp_path, s3):
    root = tmp_path / "hub"
    with TestClient(create_hub_app(root, template(tmp_path))) as hub:
        hub.post("/api/projects", json={"id": "wf", "template": "dated"})
        hub.put("/p/wf/api/nodes/doubled", json={"source": "select px * 2 as px2 from prices\n", "kind": "sql"})
        hub.post("/p/wf/api/run", json={"params": {"as_of": "2026-03-02"}})
        wait_idle(hub)
        assert len(s3.objects) == 2
        shutil.rmtree(root / "wf" / ".exuvia" / "artifacts")  # a cold cache
        r = hub.post("/p/wf/api/nodes/doubled/run", json={"params": {"as_of": "2026-03-02"}, "force": True})
        assert r.status_code == 200
        wait_idle(hub)
        assert hub.get("/p/wf/api/nodes/doubled/data", params=AS_OF).json()["rows"] == [[4]]
        assert any(Path(p).parts[-2] == "prices" for p in (root / "wf" / ".exuvia" / "artifacts").rglob("*.parquet"))


def test_partitioned_chunks_live_in_the_store(tmp_path, s3):
    """Each chunk of a partitioned output is uploaded under the same keys
    as any output, and a cold cache downloads what a read needs."""
    import asyncio

    from exuvia.workspace import Workspace

    root = tmp_path / "proj"
    (root / "nodes").mkdir(parents=True)
    (root / "nodes" / "daily.py").write_text(
        "import exuvia\n\n\n@exuvia.node(partition_by='d')\ndef daily():\n    import pandas as pd\n"
        "    n = len(exuvia.partitions())\n    return pd.DataFrame({'d': [f'2026-01-0{n + 1}'], 'v': [n]})\n",
        encoding="utf-8",
    )
    ws = Workspace(root)

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        for _ in range(3):
            finals = await ws.run(["daily"], force={"daily"})
            assert finals["daily"]["status"] == "success", finals["daily"]["error"]

    asyncio.run(go())
    files = ws.partitions_of("daily")["files"]
    assert len(files) == 3
    assert {f"proj/daily/{f['artifact_id']}.parquet" for f in files} <= {k.removeprefix("wf-tests/") for k in s3.objects}
    assert ws.store.unstored_outputs() == []
    shutil.rmtree(root / ".exuvia" / "artifacts")  # a cold cache
    assert ws.query("select sum(v) from daily")["rows"] == [[3]]
