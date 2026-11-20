"""Where node outputs are kept.

Every successful run writes its output as a content-addressed file under the
project, ``.exuvia/artifacts/<node>/<digest>.<ext>``. By default that folder
is the only copy, and a hub export carries it.

With ``EXUVIA_ARTIFACT_STORE=s3://<bucket>/<prefix>`` the durable copy lives
in an S3-compatible object store (AWS S3, Cloudflare R2, MinIO, …) and the
local folder is only a cache:

* a run's output is uploaded as soon as the run finishes;
* a read in a process that does not have the file yet downloads it first, so
  a fresh container starts with an empty cache and fills it on demand;
* the project database records which outputs are uploaded, and a hub export
  leaves those out, so the export holds code and history but not data.

Objects are keyed ``<prefix><project>/<node>/<digest>.<ext>``. Configuration:

    EXUVIA_ARTIFACT_STORE   s3://bucket/optional/prefix
    EXUVIA_S3_ENDPOINT      e.g. https://<account>.r2.cloudflarestorage.com
                            (default: AWS, https://s3.<region>.amazonaws.com)
    EXUVIA_S3_REGION        default AWS_REGION, then us-east-1 (R2: auto)
    EXUVIA_S3_ADDRESSING    path (default) or virtual (bucket in the host name)
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN

Requests are signed with AWS Signature Version 4 over httpx; only
GetObject, HeadObject, PutObject, DeleteObject and ListObjectsV2 are used.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
ARTIFACTS_DIR = ".exuvia/artifacts/"


class ArtifactStoreError(RuntimeError):
    pass


# -- signing ------------------------------------------------------------------
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    access_key: str,
    secret_key: str,
    region: str,
    payload_hash: str,
    now: dt.datetime | None = None,
    session_token: str | None = None,
    service: str = "s3",
) -> dict[str, str]:
    """``headers`` plus the SigV4 ``Authorization`` (and date/hash) headers.

    Every header passed in is signed, along with ``host``. The URL path must
    already be percent-encoded the way S3 expects (``S3Store._url`` does).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    parts = urlsplit(url)
    out = dict(headers)
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    if session_token:
        out["x-amz-security-token"] = session_token
    signed = {k.lower(): " ".join(str(v).strip().split()) for k, v in out.items()}
    signed["host"] = parts.netloc
    names = sorted(signed)
    query = "&".join(
        sorted(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in parse_qsl(parts.query, keep_blank_values=True))
    )
    canonical = "\n".join(
        [
            method.upper(),
            parts.path or "/",
            query,
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return out


# -- store --------------------------------------------------------------------
class S3Store:
    """An S3-compatible bucket (or a prefix of one) holding node outputs."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        endpoint: str | None = None,
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        session_token: str | None = None,
        virtual_host: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        if not bucket:
            raise ValueError("an S3 artifact store needs a bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.endpoint = (endpoint or f"https://s3.{region}.amazonaws.com").rstrip("/")
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.virtual_host = virtual_host
        self.http = httpx.Client(transport=transport, timeout=httpx.Timeout(30.0, read=300.0))

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _bucket_url(self) -> str:
        if self.virtual_host:
            u = urlsplit(self.endpoint)
            return f"{u.scheme}://{self.bucket}.{u.netloc}{u.path}"
        return f"{self.endpoint}/{self.bucket}"

    def _url(self, key: str) -> str:
        return f"{self._bucket_url()}/{quote(self.prefix + key, safe='/-_.~')}"

    def _signed(self, method: str, url: str, payload_hash: str = EMPTY_SHA256) -> dict[str, str]:
        return sigv4_headers(
            method,
            url,
            {},
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            payload_hash=payload_hash,
            session_token=self.session_token,
        )

    def _request(self, method: str, url: str, content: bytes | None = None) -> httpx.Response:
        payload_hash = hashlib.sha256(content).hexdigest() if content is not None else EMPTY_SHA256
        return self.http.request(method, url, content=content, headers=self._signed(method, url, payload_hash))

    def put(self, key: str, path: Path) -> None:
        res = self._request("PUT", self._url(key), Path(path).read_bytes())
        if res.status_code >= 300:
            raise ArtifactStoreError(f"PUT {key} -> {res.status_code} {res.text[:200]}")

    def exists(self, key: str) -> bool:
        res = self._request("HEAD", self._url(key))
        if res.status_code == 404:
            return False
        if res.status_code >= 300:
            raise ArtifactStoreError(f"HEAD {key} -> {res.status_code}")
        return True

    def fetch(self, key: str, dest: Path) -> bool:
        """Download ``key`` to ``dest`` (atomically). False when it does not exist."""
        url = self._url(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
        os.close(fd)
        try:
            with self.http.stream("GET", url, headers=self._signed("GET", url)) as res:
                if res.status_code == 404:
                    return False
                if res.status_code >= 300:
                    res.read()
                    raise ArtifactStoreError(f"GET {key} -> {res.status_code} {res.text[:200]}")
                with open(tmp, "wb") as f:
                    for chunk in res.iter_bytes():
                        f.write(chunk)
            os.replace(tmp, dest)
            return True
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, key: str) -> None:
        res = self._request("DELETE", self._url(key))
        if res.status_code >= 300 and res.status_code != 404:
            raise ArtifactStoreError(f"DELETE {key} -> {res.status_code}")

    def keys(self, under: str) -> Iterator[str]:
        """Every key below ``under`` (relative to this store's prefix)."""
        base = self._bucket_url() + ("/" if self.virtual_host else "")
        token: str | None = None
        while True:
            query = f"list-type=2&prefix={quote(self.prefix + under, safe='')}"
            if token:
                query += f"&continuation-token={quote(token, safe='')}"
            res = self._request("GET", f"{base}?{query}")
            if res.status_code >= 300:
                raise ArtifactStoreError(f"LIST {under} -> {res.status_code} {res.text[:200]}")
            root = ET.fromstring(res.content)
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            for item in root.iter(f"{ns}Contents"):
                k = item.findtext(f"{ns}Key") or ""
                if k.startswith(self.prefix):
                    yield k[len(self.prefix):]
            if (root.findtext(f"{ns}IsTruncated") or "").lower() != "true":
                return
            token = root.findtext(f"{ns}NextContinuationToken")
            if not token:
                return

    def delete_under(self, under: str) -> int:
        """Delete every key below ``under``; returns how many."""
        doomed = list(self.keys(under))
        for key in doomed:
            self.delete(key)
        return len(doomed)


# -- configuration --------------------------------------------------------------
def parse_store_url(url: str) -> tuple[str, str]:
    """``s3://bucket/prefix`` → (bucket, prefix)."""
    u = urlsplit(url)
    if u.scheme != "s3" or not u.netloc:
        raise ValueError(f"EXUVIA_ARTIFACT_STORE must look like s3://bucket/prefix, got {url!r}")
    return u.netloc, u.path.strip("/")


_ENV = (
    "EXUVIA_ARTIFACT_STORE",
    "EXUVIA_S3_ENDPOINT",
    "EXUVIA_S3_REGION",
    "EXUVIA_S3_ADDRESSING",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
_lock = threading.Lock()
_configured: tuple[str, S3Store | None] | None = None


def _env_spec() -> str:
    return "|".join(os.environ.get(k, "") for k in _ENV)


def _from_env() -> S3Store | None:
    url = os.environ.get("EXUVIA_ARTIFACT_STORE", "").strip()
    if not url:
        return None
    bucket, prefix = parse_store_url(url)
    region = os.environ.get("EXUVIA_S3_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    return S3Store(
        bucket,
        prefix,
        endpoint=os.environ.get("EXUVIA_S3_ENDPOINT") or None,
        region=region,
        access_key=os.environ.get("AWS_ACCESS_KEY_ID", ""),
        secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
        virtual_host=os.environ.get("EXUVIA_S3_ADDRESSING", "path").lower() == "virtual",
    )


def artifact_store() -> S3Store | None:
    """The configured remote store, or None when outputs stay on local disk.

    One client per process, rebuilt when the environment changes."""
    global _configured
    spec = _env_spec()
    with _lock:
        if _configured is None or _configured[0] != spec:
            _configured = (spec, _from_env())
        return _configured[1]


def set_artifact_store(store: S3Store | None) -> None:
    """Pin this process's store until the environment changes (tests, or a
    host that builds its own client)."""
    global _configured
    with _lock:
        _configured = (_env_spec(), store)


def artifact_key(project_id: str, rel_path: str) -> str | None:
    """Object key for an output recorded at ``rel_path`` (relative to the
    project root), or None when the path is not under the artifacts folder."""
    if not rel_path.startswith(ARTIFACTS_DIR):
        return None
    rest = rel_path[len(ARTIFACTS_DIR):]
    if not rest or any(part in ("", ".", "..") for part in rest.split("/")):
        return None
    return f"{project_id}/{rest}"
