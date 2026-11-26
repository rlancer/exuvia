"""`exuvia mirror`: code revisions as git commits, pushed with the user's own git."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from exuvia.mirror import LocalSource, MirrorError, RemoteSource, github_remote, mirror
from exuvia.server import create_app
from exuvia.workspace import Workspace, acting_as

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TOKEN = "mirror-test-token"
V1 = "import exuvia\n\n\n@exuvia.node()\ndef nums():\n    return [1, 2, 3]\n"
V2 = V1.replace("[1, 2, 3]", "[4, 5]")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def project(root):
    (root / "nodes").mkdir(parents=True)
    (root / "nodes" / "nums.py").write_text(V1, encoding="utf-8")
    (root / "exuvia.json").write_text('{"name": "p"}\n', encoding="utf-8")
    return Workspace(root)


def save(ws, name, source, origin="agent"):
    with acting_as(origin):
        ws.project.write_node(name, source, "python")
        ws.reload()


def log(dest, branch="exuvia"):
    """(author name, author email, subject, files at that commit) oldest first."""
    out = []
    for line in git(dest, "log", "--reverse", "--format=%H%x00%an%x00%ae%x00%s", branch).splitlines():
        sha, name, email, subject = line.split("\0")
        out.append((name, email, subject, sorted(git(dest, "ls-tree", "-r", "--name-only", sha).splitlines())))
    return out


def test_the_api_pages_through_revisions_oldest_first(tmp_path):
    ws = project(tmp_path)
    save(ws, "nums", V2)
    save(ws, "more", V1)
    with TestClient(create_app(tmp_path)) as c:
        newest_first = c.get("/api/code/revisions").json()
        everything = c.get("/api/code/revisions", params={"after": ""}).json()
        assert [r["id"] for r in everything] == [r["id"] for r in reversed(newest_first)]
        assert {"path", "hash", "origin", "author", "saved_at", "view"} <= set(everything[0])
        page = c.get("/api/code/revisions", params={"after": "", "limit": 2}).json()
        rest = c.get("/api/code/revisions", params={"after": page[-1]["id"]}).json()
        assert page + rest == everything
        assert c.get("/api/code/revisions", params={"after": everything[-1]["id"]}).json() == []
        assert c.get("/api/code/revisions", params={"after": "nope"}).status_code == 404
        assert c.get("/api/code/revisions", params={"after": "", "node": "nums"}).status_code == 400


def test_each_save_is_a_commit_and_a_rerun_carries_on(tmp_path):
    ws = project(tmp_path / "p")
    save(ws, "nums", V2, origin="agent")
    save(ws, "more", V1, origin="ui")
    (tmp_path / "p" / "nodes" / "more.py").unlink()  # deleted in an editor
    with acting_as("file"):
        ws.reload()
    dest = tmp_path / "mirror"

    done = mirror(LocalSource(tmp_path / "p"), dest)
    # The load recorded two files at once: one commit. Then one per save.
    assert done["commits"] == 4 and not done["pushed"]
    commits = log(dest)
    assert commits[0] == ("load", "load@exuvia.invalid", "load: 2 files", ["exuvia.json", "nodes/nums.py"])
    assert commits[1][:3] == ("agent", "agent@exuvia.invalid", "agent: nodes/nums.py")
    assert commits[2][2:] == ("ui: nodes/more.py", ["exuvia.json", "nodes/more.py", "nodes/nums.py"])
    assert commits[3][2:] == ("file: delete nodes/more.py", ["exuvia.json", "nodes/nums.py"])
    # A fresh folder gets the branch checked out, files and all, with the text exactly as saved.
    assert git(dest, "symbolic-ref", "HEAD") == "refs/heads/exuvia"
    assert git(dest, "show", "exuvia:nodes/nums.py") + "\n" == V2
    assert (dest / "nodes" / "nums.py").is_file()
    body = git(dest, "log", "-1", "--format=%B")
    revs = ws.code_revisions()
    assert f"Exuvia-Revision: {revs[0]['id']}" in body and "Exuvia-Origin: file" in body

    tip = done["tip"]
    again = mirror(LocalSource(tmp_path / "p"), dest)
    assert again == {"commits": 0, "tip": tip, "pushed": False, "remote": None}

    save(ws, "nums", V1, origin="restore")
    more = mirror(LocalSource(tmp_path / "p"), dest)
    assert more["commits"] == 1 and git(dest, "rev-parse", "exuvia~1") == tip
    assert (dest / "nodes" / "nums.py").read_text(encoding="utf-8").replace("\r\n", "\n") == V1

    # The same history mirrored again from nothing gives the same commits.
    assert mirror(LocalSource(tmp_path / "p"), tmp_path / "again")["tip"] == more["tip"]


def test_a_signed_in_author_is_the_commit_author(tmp_path):
    ws = project(tmp_path / "p")
    from exuvia.project import _hash

    ws.store.record_code(
        [("nodes/nums.py", "nums", None, _hash(V2), V2), ("lib/x.py", None, None, _hash("X = 1\n"), "X = 1\n")],
        "ui", author="Ada <Lovelace>", author_user="u/1 2",
    )
    mirror(LocalSource(tmp_path / "p"), tmp_path / "m")
    name, email, subject, files = log(tmp_path / "m")[-1]
    assert (name, email, subject) == ("Ada Lovelace", "u-1-2@exuvia.invalid", "ui: 2 files")
    assert "- lib/x.py" in git(tmp_path / "m", "log", "-1", "--format=%B")


def test_pushing_to_a_remote_and_picking_up_from_it(tmp_path):
    ws = project(tmp_path / "p")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))

    first = mirror(LocalSource(tmp_path / "p"), tmp_path / "a", remote=str(remote))
    assert first["pushed"] and git(remote, "rev-parse", "exuvia") == first["tip"]

    # Another machine (or a CI job with a fresh checkout) carries on from the remote's branch.
    save(ws, "nums", V2)
    second = mirror(LocalSource(tmp_path / "p"), tmp_path / "b", remote=str(remote))
    assert second["commits"] == 1 and second["pushed"]
    assert git(remote, "rev-parse", "exuvia~1") == first["tip"]
    # The first folder is behind the remote: it fast-forwards and sends nothing new.
    assert mirror(LocalSource(tmp_path / "p"), tmp_path / "a", remote=str(remote)) == {
        "commits": 0, "tip": second["tip"], "pushed": False, "remote": str(remote),
    }

    # Someone commits to the branch by hand: the mirror stops rather than overwrite it.
    b = tmp_path / "b"
    (b / "notes.txt").write_text("hand edit\n", encoding="utf-8")
    git(b, "add", "notes.txt")
    git(b, "-c", "user.name=x", "-c", "user.email=x@example.com", "commit", "-qm", "hand edit")
    git(b, "push", "-q", str(remote), "exuvia")
    save(ws, "nums", V1)
    with pytest.raises(MirrorError, match="not made by exuvia mirror"):
        mirror(LocalSource(tmp_path / "p"), tmp_path / "c", remote=str(remote))
    with pytest.raises(MirrorError, match="not made by exuvia mirror"):
        mirror(LocalSource(tmp_path / "p"), tmp_path / "a", remote=str(remote))
    a = tmp_path / "a"
    git(a, "-c", "user.name=x", "-c", "user.email=x@example.com", "commit", "-q", "--allow-empty", "-m", "local edit")
    with pytest.raises(MirrorError, match="different ways"):
        mirror(LocalSource(tmp_path / "p"), a, remote=str(remote))


def test_refusals(tmp_path):
    project(tmp_path / "p")
    other = tmp_path / "other"
    project(other)
    mirror(LocalSource(tmp_path / "p"), tmp_path / "m")
    with pytest.raises(MirrorError, match="does not have"):
        mirror(LocalSource(other), tmp_path / "m")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x", encoding="utf-8")
    with pytest.raises(MirrorError, match="not empty"):
        mirror(LocalSource(tmp_path / "p"), tmp_path / "full")
    with pytest.raises(MirrorError, match="inside the git repository"):
        mirror(LocalSource(tmp_path / "p"), tmp_path / "m" / "nodes")
    with pytest.raises(MirrorError, match="not an exuvia project"):
        LocalSource(tmp_path / "full")
    with pytest.raises(MirrorError, match="not a branch name"):
        mirror(LocalSource(tmp_path / "p"), tmp_path / "m", branch="-x")


def test_github_through_gh(tmp_path):
    calls = []

    def fake(created, proto):
        def run(*args):
            calls.append(args)
            if args[:2] == ("repo", "view"):
                if not created:
                    return subprocess.CompletedProcess(args, 1, "", "GraphQL: Could not resolve to a Repository")
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"url": "https://github.com/me/code", "sshUrl": "git@github.com:me/code.git"}), ""
                )
            if args[:2] == ("repo", "create"):
                created.append(True)
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ("config", "get"):
                return subprocess.CompletedProcess(args, 0, proto + "\n", "")
            raise AssertionError(args)

        return run

    assert github_remote("me/code", fake([True], "https")) == "https://github.com/me/code.git"
    assert not any(c[:2] == ("repo", "create") for c in calls)
    assert github_remote("me/code", fake([], "ssh")) == "git@github.com:me/code.git"
    assert ("repo", "create", "me/code", "--private", "--description", "Code history mirrored from exuvia") in calls
    with pytest.raises(MirrorError, match="owner/name"):
        github_remote("not a repo", fake([True], "https"))


@pytest.fixture
def served(tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setenv("EXUVIA_API_TOKEN", TOKEN)
    ws = project(tmp_path / "p")
    save(ws, "nums", V2)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "p"), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    monkeypatch.delenv("EXUVIA_API_TOKEN")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_mirroring_a_served_project_from_the_command_line(served, tmp_path):
    with pytest.raises(MirrorError, match="needs a token"):
        mirror(RemoteSource(served), tmp_path / "x")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    cmd = [sys.executable, "-m", "exuvia.cli", "mirror", str(tmp_path / "m"), "--url", served, "--remote", str(remote)]
    env = {**os.environ, "EXUVIA_API_TOKEN": TOKEN}
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("2 new commits on exuvia in ") and ", pushed to " in out.stdout
    assert git(remote, "show", "exuvia:nodes/nums.py") + "\n" == V2
    # The same history read over HTTP and from the folder makes the same commits.
    assert mirror(LocalSource(tmp_path / "p"), tmp_path / "local")["tip"] == git(remote, "rev-parse", "exuvia")

    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "is up to date" in out.stdout
    out = subprocess.run(cmd + ["--github", "me/x"], capture_output=True, text=True, env=env)
    assert out.returncode == 1 and "not both" in out.stderr
