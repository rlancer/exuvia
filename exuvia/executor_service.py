"""``exuvia executor``: run nodes as a stateless HTTP service.

This is the "container part". It holds no project state: each run ships the
project version (a hash-verified, cached zip), the upstream artifacts it
needs, and gets the artifact back. Point a control plane at it with
``EXUVIA_EXECUTOR_URL``. Protocol: see ``RemoteExecutor`` in ``executor.py``.

Env:
    EXUVIA_EXECUTOR_TOKEN  required bearer token (strongly recommended off-box)
    EXUVIA_EXECUTOR_DIR    scratch dir for bundles/runs (default: system temp)
    EXUVIA_MAX_PARALLEL    concurrent node runs (default 4)
    EXUVIA_CHUNK_TTL_DAYS  days a cached input chunk is kept unused (default 7)

Security: requests can run arbitrary code (that is the point). Only expose
this to a trusted control plane, and always set a token when it is reachable
over a network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from . import __version__, envvars
from .envvars import EnvError
from .executor import ExecRequest, LocalExecutor, bundle_hash_of_zip
from .project import NODE_NAME

RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BUNDLE_ID = re.compile(r"^[0-9a-f]{24}$")
CHUNK_ID = re.compile(r"^[0-9a-f]{16}$")
INPUT_EXT = {"table": ".parquet", "json": ".json", "multi": ".zip"}
RUN_TTL_S = 2 * 3600


def create_executor_app() -> FastAPI:
    token = os.environ.get("EXUVIA_EXECUTOR_TOKEN")
    workers = int(os.environ.get("EXUVIA_MAX_PARALLEL", "4"))
    base = Path(os.environ.get("EXUVIA_EXECUTOR_DIR") or Path(tempfile.gettempdir()) / "exuvia-executor")
    bundles, runs, chunks = base / "bundles", base / "runs", base / "chunks"
    for d in (bundles, runs, chunks):
        d.mkdir(parents=True, exist_ok=True)
    chunk_ttl_s = float(os.environ.get("EXUVIA_CHUNK_TTL_DAYS", "7")) * 86400
    last_chunk_sweep = [0.0]
    local = LocalExecutor(base, workers, packages_dir=base / "packages")
    slots = asyncio.Semaphore(workers)
    results: dict[str, dict[str, Any]] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        local.start()
        yield
        local.close()

    app = FastAPI(title="exuvia executor", lifespan=lifespan)

    def auth(request: Request) -> None:
        if not token:
            return
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "bad or missing bearer token")

    def run_dir(run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise HTTPException(400, "bad run id")
        return runs / run_id

    def sweep() -> None:
        cutoff = time.time() - RUN_TTL_S
        for d in runs.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                results.pop(d.name, None)
        # Chunks stay while runs use them (each use touches the file); the
        # rest go after EXUVIA_CHUNK_TTL_DAYS, checked at most once an hour.
        now = time.time()
        if now - last_chunk_sweep[0] > 3600:
            last_chunk_sweep[0] = now
            for f in chunks.glob("*.parquet"):
                try:
                    if f.stat().st_mtime < now - chunk_ttl_s:
                        f.unlink()
                except OSError:
                    pass

    def chunk_file(chunk: Any) -> Path:
        if not isinstance(chunk, str) or not CHUNK_ID.match(chunk):
            raise HTTPException(400, "bad chunk id")
        return chunks / f"{chunk}.parquet"

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "workers": workers, "auth": bool(token), "chunks": True}

    @app.post("/v1/chunks/missing", dependencies=[Depends(auth)])
    async def missing_chunks(request: Request) -> dict[str, Any]:
        """Which of these chunks (a partitioned output's Parquet files, by
        content hash) this executor doesn't have. The ones it has are
        touched, so they are kept a while longer."""
        body = await request.json()
        ids = body.get("chunks") if isinstance(body, dict) else None
        if not isinstance(ids, list) or len(ids) > 100_000:
            raise HTTPException(400, "chunks must be a list")
        missing = []
        for c in ids:
            f = chunk_file(c)
            try:
                os.utime(f)
            except OSError:
                missing.append(c)
        return {"missing": missing}

    @app.put("/v1/chunks/{chunk}", dependencies=[Depends(auth)])
    async def put_chunk(chunk: str, request: Request) -> dict[str, Any]:
        f = chunk_file(chunk)
        blob = await request.body()
        if hashlib.sha256(blob).hexdigest()[:16] != chunk:
            raise HTTPException(400, "chunk content does not match its hash")
        if not f.exists():
            fd, tmp = tempfile.mkstemp(dir=chunks, prefix=".incoming-")
            with os.fdopen(fd, "wb") as out:
                out.write(blob)
            os.replace(tmp, f)
        return {"ok": True}

    @app.head("/v1/bundles/{digest}", dependencies=[Depends(auth)])
    @app.get("/v1/bundles/{digest}", dependencies=[Depends(auth)])
    def has_bundle(digest: str) -> dict[str, Any]:
        if not BUNDLE_ID.match(digest) or not (bundles / digest / ".complete").exists():
            raise HTTPException(404, "unknown bundle")
        return {"ok": True}

    @app.put("/v1/bundles/{digest}", dependencies=[Depends(auth)])
    async def put_bundle(digest: str, request: Request) -> dict[str, Any]:
        if not BUNDLE_ID.match(digest):
            raise HTTPException(400, "bad bundle id")
        blob = await request.body()
        if bundle_hash_of_zip(blob) != digest:
            raise HTTPException(400, "bundle content does not match its hash")
        target = bundles / digest
        if (target / ".complete").exists():
            return {"ok": True}
        tmp = Path(tempfile.mkdtemp(dir=bundles, prefix=".incoming-"))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for name in z.namelist():
                dest = (tmp / name).resolve()
                if not dest.is_relative_to(tmp.resolve()):
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise HTTPException(400, f"unsafe path in bundle: {name}")
            z.extractall(tmp)
        (tmp / ".complete").touch()
        try:
            tmp.rename(target)
        except OSError:  # a concurrent upload of the same bundle won
            shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True}

    @app.put("/v1/runs/{run_id}/inputs/{dep}", dependencies=[Depends(auth)])
    async def put_input(run_id: str, dep: str, request: Request) -> dict[str, Any]:
        if not NODE_NAME.match(dep):
            raise HTTPException(400, "bad input name")
        d = run_dir(run_id) / "inputs"
        d.mkdir(parents=True, exist_ok=True)
        (d / dep).write_bytes(await request.body())
        return {"ok": True}

    @app.post("/v1/runs/{run_id}", dependencies=[Depends(auth)])
    async def start_run(run_id: str, request: Request) -> StreamingResponse:
        rd = run_dir(run_id)
        spec = await request.json()
        sweep()
        digest = spec.get("bundle", "")
        root = bundles / digest
        if not BUNDLE_ID.match(digest) or not (root / ".complete").exists():
            raise HTTPException(409, "bundle not uploaded")
        if not NODE_NAME.match(spec.get("node", "")) or spec.get("kind") not in ("python", "sql"):
            raise HTTPException(400, "bad node spec")
        node_path = (root / spec["path"]).resolve()
        if not node_path.is_relative_to(root.resolve()) or not node_path.is_file():
            raise HTTPException(400, "node path not in bundle")
        inputs = {}
        for dep, meta in (spec.get("inputs") or {}).items():
            if not NODE_NAME.match(dep):
                raise HTTPException(400, "bad input name")
            if meta.get("chunks") is not None:  # a partitioned output: its files, cached here by hash
                if not isinstance(meta["chunks"], list):
                    raise HTTPException(400, "bad chunks")
                paths = [chunk_file(c) for c in meta["chunks"]]
                gone = [p.stem for p in paths if not p.is_file()]
                if gone:
                    raise HTTPException(409, f"input {dep} chunks not uploaded: {', '.join(gone[:5])}")
                inputs[dep] = {"path": None, "paths": [str(p) for p in paths], "kind": meta.get("kind")}
                continue
            f = rd / "inputs" / dep
            if meta.get("has_file") and not f.is_file():
                raise HTTPException(409, f"input {dep} not uploaded")
            inputs[dep] = {"path": str(f) if meta.get("has_file") else None, "kind": meta.get("kind")}
        params = spec.get("params") or {}
        if not isinstance(params, dict) or not all(NODE_NAME.match(str(k)) for k in params):
            raise HTTPException(400, "bad params")
        env = spec.get("env") or {}
        try:
            if not isinstance(env, dict):
                raise EnvError("not an object")
            env = {envvars.check_name(str(k)): envvars.check_value(v) for k, v in env.items()}
        except EnvError as e:
            raise HTTPException(400, f"bad env: {e}") from e
        rd.mkdir(parents=True, exist_ok=True)
        req = ExecRequest(
            run_id=run_id,
            node=spec["node"],
            kind=spec["kind"],
            path=spec["path"],
            root=root,
            out_dir=rd / "out",
            inputs=inputs,
            params=params,
            env=env,
            partition_by=spec.get("partition_by") if isinstance(spec.get("partition_by"), str) else None,
            partitions=[str(v) for v in spec.get("partitions") or []],
        )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def on_log(text: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, json.dumps({"type": "log", "text": text}) + "\n")

        async def work() -> None:
            async with slots:
                res = await asyncio.to_thread(local.execute, req, on_log)
            results[run_id] = {"path": res.artifact_path}
            out = {k: v for k, v in res.__dict__.items() if k != "artifact_path"}
            await queue.put(json.dumps({"type": "result", "result": out}) + "\n")
            await queue.put(None)

        task = asyncio.create_task(work())

        async def stream():
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
            await task

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.get("/v1/runs/{run_id}/output", dependencies=[Depends(auth)])
    def output(run_id: str) -> FileResponse:
        run_dir(run_id)
        path = (results.get(run_id) or {}).get("path")
        if not path or not Path(path).is_file():
            raise HTTPException(404, "no output for this run")
        return FileResponse(path, media_type="application/octet-stream")

    @app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(auth)])
    def cancel(run_id: str) -> dict[str, Any]:
        run_dir(run_id)
        return {"ok": local.cancel(run_id)}

    @app.delete("/v1/runs/{run_id}", dependencies=[Depends(auth)])
    def delete(run_id: str) -> dict[str, Any]:
        shutil.rmtree(run_dir(run_id), ignore_errors=True)
        results.pop(run_id, None)
        return {"ok": True}

    return app
