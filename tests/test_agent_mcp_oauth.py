"""OAuth sign-in to the agent's outside MCP servers (agent_mcp.py): Sign in
registers exuvia with the server's authorization server and hands back the
page to open; the callback trades the code for tokens and keeps them; turns
send them and refresh them; a refused or someone else's callback signs
nobody in. Against a real server over HTTP: another exuvia project behind a
small OAuth authorization server."""

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from exuvia.db import close_shared
from exuvia.project import Project
from exuvia.server import create_app

pytest.importorskip("mcp")

SRC = '''import exuvia


@exuvia.node()
def nums():
    """Three numbers."""
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''
ALICE = {"X-Forwarded-User": "alice", "X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-User": "bob", "X-Forwarded-Email": "bob@example.com"}
HERE = "http://testserver"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    close_shared()
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH", "EXUVIA_AUTH_USER_HEADER",
                 "EXUVIA_AUTH_EMAIL_HEADER", "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_DB_SCOPE",
                 "EXUVIA_AGENT_MCP", "EXUVIA_AGENT_MCP_PRIVATE"):
        monkeypatch.delenv(name, raising=False)


class OAuthServer:
    """An MCP server that signs people in with OAuth: an authorization server
    (metadata, dynamic registration, /authorize, /token with PKCE and refresh)
    in front of another exuvia project's /mcp, which it lets in with that
    project's API token once a request carries an access token it issued."""

    def __init__(self, inner):
        self.inner = inner
        self.base = ""
        self.clients: dict[str, list[str]] = {}
        self.codes: dict[str, str] = {}
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.expires_in = 3600
        self.issued = 0

    def tokens(self):
        self.issued += 1
        access, refresh = f"at-{self.issued}", f"rt-{self.issued}"
        self.access.add(access)
        self.refresh.add(refresh)
        return {"access_token": access, "token_type": "bearer", "expires_in": self.expires_in, "refresh_token": refresh}

    async def __call__(self, scope, receive, send):
        from starlette.requests import Request
        from starlette.responses import JSONResponse, RedirectResponse

        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        req = Request(scope, receive)
        path = req.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            res = JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [self.base]})
        elif path.startswith("/.well-known/oauth-authorization-server"):
            res = JSONResponse({
                "issuer": self.base, "authorization_endpoint": f"{self.base}/authorize",
                "token_endpoint": f"{self.base}/token", "registration_endpoint": f"{self.base}/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            })
        elif path == "/register":
            body = await req.json()
            cid = f"client-{len(self.clients) + 1}"
            self.clients[cid] = body["redirect_uris"]
            res = JSONResponse({**body, "client_id": cid, "token_endpoint_auth_method": "none"}, status_code=201)
        elif path == "/authorize":
            q = req.query_params
            assert q["redirect_uri"] in self.clients[q["client_id"]] and q["code_challenge_method"] == "S256"
            code = secrets.token_urlsafe(8)
            self.codes[code] = q["code_challenge"]
            res = RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}")
        elif path == "/token":
            form = {k: v[0] for k, v in parse_qs((await req.body()).decode()).items()}
            if form["grant_type"] == "authorization_code":
                challenge = self.codes.pop(form["code"])
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
                assert digest.decode() == challenge
                res = JSONResponse(self.tokens())
            elif form.get("refresh_token") in self.refresh:
                self.refresh.discard(form["refresh_token"])
                res = JSONResponse(self.tokens())
            else:
                res = JSONResponse({"error": "invalid_grant"}, status_code=400)
        elif req.headers.get("authorization", "")[7:] in self.access:
            headers = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
            scope = {**scope, "headers": [*headers, (b"authorization", b"Bearer s3cret")]}
            return await self.inner(scope, receive, send)
        else:
            res = JSONResponse({"error": "invalid_token"}, status_code=401, headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource"'})
        await res(scope, receive, send)


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    import uvicorn

    root = tmp_path / "other"
    Project(root).write_node("nums", "python", SRC)
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    fake = OAuthServer(create_app(root))
    monkeypatch.delenv("EXUVIA_API_TOKEN")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake.base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield fake
    server.should_exit = True
    thread.join(timeout=10)


def start(client, name="o", headers=None):
    """Press Sign in: the authorization server's page to open."""
    r = client.post(f"/api/me/mcp/{name}/oauth", headers=headers, json={"origin": HERE})
    assert r.status_code == 200, r.text
    return r.json()["url"]


def authorize(url):
    """The person approves: where the authorization server sends them back."""
    back = httpx.get(url, follow_redirects=False).headers["location"]
    assert back.startswith(f"{HERE}/api/me/mcp/oauth/callback?")
    return back[len(HERE):]


def server_row(client, headers=None):
    return client.get("/api/me/mcp", headers=headers).json()["servers"][0]


def test_sign_in_then_connections_use_and_refresh_the_tokens(tmp_path, oauth):
    root = tmp_path / "proj"
    Project(root)
    with TestClient(create_app(root)) as client:
        client.put("/api/me/mcp/o", json={"url": f"{oauth.base}/mcp"})
        before = client.post("/api/me/mcp/o/test").json()
        assert not before["ok"] and before["signin"] and "Sign in" in before["error"]
        assert server_row(client)["signed_in"] is False

        url = start(client)
        assert url.startswith(f"{oauth.base}/authorize?")
        page = client.get(authorize(url))
        assert page.status_code == 200 and "Signed in to o" in page.text and "postMessage" in page.text
        row = server_row(client)
        assert row["signed_in"] is True and "at-1" not in json.dumps(row) and row["headers"] == []
        r = client.post("/api/me/mcp/o/test").json()
        assert r["ok"] and "list_nodes" in {t["name"] for t in r["tools"]}

        # A callback whose sign-in is over, or never was, signs nobody in.
        assert "Not signed in" in client.get("/api/me/mcp/oauth/callback?code=x&state=nope").text

        # The server forgets the token and refreshing isn't due: sign in again.
        oauth.access.clear()
        r = client.post("/api/me/mcp/o/test").json()
        assert not r["ok"] and r["signin"] and "sign in again" in r["error"]

        # An access token that has run out is refreshed, and the new one kept.
        oauth.expires_in = 1  # kept as already run out (TOKEN_MARGIN)
        client.get(authorize(start(client)))
        assert oauth.issued == 2
        assert client.post("/api/me/mcp/o/test").json()["ok"]
        assert oauth.issued == 3
        oauth.expires_in = 3600
        assert client.post("/api/me/mcp/o/test").json()["ok"]
        assert oauth.issued == 4  # rt-3 was kept, so it refreshed again rather than asking for a sign-in

        # Signing out forgets the tokens; so does pointing the server elsewhere.
        assert client.delete("/api/me/mcp/o/oauth").json() == {"ok": True}
        assert server_row(client)["signed_in"] is False
        client.get(authorize(start(client)))
        assert server_row(client)["signed_in"] is True
        client.put("/api/me/mcp/o", json={"url": f"{oauth.base}/mcp/"})
        assert server_row(client)["signed_in"] is False


def test_a_refused_sign_in_and_bad_requests(tmp_path, oauth):
    root = tmp_path / "proj"
    Project(root)
    with TestClient(create_app(root)) as client:
        client.put("/api/me/mcp/o", json={"url": f"{oauth.base}/mcp"})
        state = httpx.URL(start(client)).params["state"]
        page = client.get(f"/api/me/mcp/oauth/callback?error=access_denied&state={state}")
        assert "Not signed in to o" in page.text and "access_denied" in page.text
        assert server_row(client)["signed_in"] is False
        assert client.post("/api/me/mcp/o/oauth", json={"origin": "javascript:alert(1)"}).status_code == 400
        assert client.post("/api/me/mcp/o/oauth", json={"origin": "https://x.example/path"}).status_code == 400
        assert client.post("/api/me/mcp/missing/oauth", json={}).status_code == 404
        assert client.delete("/api/me/mcp/missing/oauth").status_code == 404


def test_each_person_signs_in_for_themselves(tmp_path, oauth, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    monkeypatch.setenv("EXUVIA_AGENT_MCP_PRIVATE", "1")
    root = tmp_path / "proj"
    Project(root)
    with TestClient(create_app(root)) as client:
        client.put("/api/me/mcp/o", headers=ALICE, json={"url": f"{oauth.base}/mcp"})
        client.put("/api/me/mcp/o", headers=BOB, json={"url": f"{oauth.base}/mcp"})
        back = authorize(start(client, headers=ALICE))
        assert "Not signed in" in client.get(back, headers=BOB).text  # Bob can't finish Alice's sign-in
        assert "Signed in to o" in client.get(back, headers=ALICE).text
        assert server_row(client, ALICE)["signed_in"] is True
        assert server_row(client, BOB)["signed_in"] is False
        # An agent token can't start one.
        token = client.post("/api/me/tokens", headers=ALICE, json={"name": "t"}).json()["token"]
        r = client.post("/api/me/mcp/o/oauth", headers={"Authorization": f"Bearer {token}"}, json={"origin": HERE})
        assert r.status_code == 403


def test_a_sign_in_in_the_same_tab_returns_to_the_page(tmp_path, oauth):
    root = tmp_path / "proj"
    Project(root)
    with TestClient(create_app(root)) as client:
        client.put("/api/me/mcp/o", json={"url": f"{oauth.base}/mcp"})
        r = client.post("/api/me/mcp/o/oauth", json={"origin": HERE, "back": f"{HERE}/p/x/?view=a"})
        page = client.get(authorize(r.json()["url"]))
        assert "Signed in to o" in page.text and 'href="http://testserver/p/x/?view=a"' in page.text
        assert "location.replace(m.back)" in page.text
        # Only a page on this server.
        r = client.post("/api/me/mcp/o/oauth", json={"origin": HERE, "back": "https://evil.example/"})
        assert "Back to exuvia" not in client.get(authorize(r.json()["url"])).text
