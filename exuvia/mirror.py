"""Mirror a project's code revisions into a git repository: ``exuvia mirror``.

exuvia keeps every version of every code file (code revisions). This turns
that history into git commits, so it can be browsed, diffed, blamed and
cloned with ordinary tools, and pushes them where you say:

    exuvia mirror ./sales-code --url https://hub.example.com/p/sales --remote origin
    exuvia mirror ./sales-code --url https://hub.example.com/p/sales --github me/sales-code

It runs on your machine with your own ``git``: a push uses whatever git
already uses there (SSH keys, a credential helper, ``gh auth``), so exuvia
never holds a git credential, and nothing runs on the server. With
``--github`` it also uses the ``gh`` CLI to find the repository, creating it
(private) when it does not exist.

The revisions stay the record; the repository is derived from them, one way:

- One commit per save. Files one save changed together (a template, a view's
  files) are one commit. The author is the signed-in person who saved it, or
  how it was saved (``agent``, ``ui``, ``api``, ``file``, ...), with an
  ``@exuvia.invalid`` address; the date is the save's.
- Each commit's message ends in ``Exuvia-Revision: <id>`` trailers. The last
  one on the branch's tip says where the next run carries on, so there is no
  state file: the repository is the cursor.
- The committer is always ``exuvia`` and every date comes from the revision,
  so mirroring the same history again gives the same commit hashes.
- It writes one branch (``exuvia`` unless ``--branch``) and never
  force-pushes. A commit on that branch that this command did not make stops
  it, rather than being overwritten: edit the code in exuvia, not here.
- Each code tag becomes an annotated git tag of the same name on the commit
  of the save it was made at (tagger and date from the tag, so these are
  the same every run too). A git tag of that name that points elsewhere is
  left as it is, here and on the remote, with a line saying so.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

BRANCH = "exuvia"
TRAILER = "Exuvia-Revision"
COMMITTER = "exuvia <mirror@exuvia.invalid>"
PAGE = 1000
# Where fast-import builds the new commits before the branch moves to them.
IMPORT_REF = "refs/exuvia/import"
ZERO = "0" * 40


class MirrorError(RuntimeError):
    """The mirror did not happen; the message says why and what to do."""


# -- where the revisions come from ---------------------------------------------------
class RemoteSource:
    """A project served by exuvia: a hub project (``<hub>/p/<project>``) or an
    ``exuvia serve`` address, read with ``EXUVIA_API_TOKEN``."""

    def __init__(self, url: str, token: str | None = None, allow_http: bool = False):
        import httpx

        from .envcli import check_url
        from .pushing import PushError, _token

        try:
            self.where = check_url(url, allow_http)
            tok = _token(token)
        except (SystemExit, PushError) as e:
            raise MirrorError(str(e)) from None
        headers = {"X-Exuvia-Origin": "mirror"}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        from .nethttp import client_options

        self.http = httpx.Client(base_url=f"{self.where}/api", headers=headers, timeout=60.0, **client_options(self.where))

    def _get(self, path: str, **params: Any) -> Any:
        import httpx

        try:
            r = self.http.get(path, params=params)
        except httpx.HTTPError as e:
            raise MirrorError(f"could not reach exuvia at {self.where} ({type(e).__name__}): is it running?") from e
        if r.status_code == 401:
            raise MirrorError(
                f"{self.where} needs a token: set EXUVIA_API_TOKEN (or EXUVIA_API_TOKEN_FILE) to your own agent "
                "token from the workbench's account menu, or to the server's token"
            )
        if r.status_code == 404:
            try:
                detail = str(r.json().get("detail", ""))
            except ValueError:
                detail = ""
            raise _NotFound(detail)
        if r.status_code >= 400:
            raise MirrorError(f"{self.where}: {r.text[:500]}")
        return r.json()

    def revisions_after(self, after: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                page = self._get("/code/revisions", after=after or "", limit=PAGE)
            except _NotFound as e:
                if str(e).startswith("no revision"):
                    raise KeyError(after) from None
                raise MirrorError(f"no exuvia project at {self.where}" + (f" ({e})" if str(e) else "")) from None
            # An exuvia from before ``after`` ignores it and answers newest first.
            if any(r["id"] == after for r in page) or page != sorted(page, key=lambda r: (r["saved_at"], r["id"])):
                raise MirrorError(f"{self.where} is too old to mirror (it cannot page through revisions): upgrade it")
            out += page
            if len(page) < PAGE:
                return out
            after = page[-1]["id"]

    def source(self, digest: str) -> str | None:
        try:
            return self._get(f"/code/blobs/{quote(digest, safe='')}")["source"]
        except _NotFound:
            return None

    def tags(self) -> list[dict[str, Any]]:
        try:
            return list(self._get("/code/tags"))
        except _NotFound:
            return []  # an exuvia from before tags


class _NotFound(Exception):
    """A 404 from the server, with its detail."""


class LocalSource:
    """A project folder on this machine, its records opened as the server
    opens them (``EXUVIA_DB`` included), without starting a workspace."""

    def __init__(self, root: Path):
        from .project import Project
        from .store import open_store

        if not (root / "nodes").is_dir():
            raise MirrorError(f"{root} is not an exuvia project (no nodes/ folder): pass its folder, or --url")
        self.where = str(root)
        self.root = root
        self.store = open_store(Project(root))

    def revisions_after(self, after: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            page = self.store.code_revisions_after(after, PAGE)
            out += page
            if len(page) < PAGE:
                return out
            after = page[-1]["id"]

    def source(self, digest: str) -> str | None:
        return self.store.code_source(digest)

    def tags(self) -> list[dict[str, Any]]:
        return self.store.code_tags()


# -- revisions -> a git fast-import stream ---------------------------------------------
def saves(revisions: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Revisions (oldest first) grouped by the save that wrote them: one save
    records all its files at one time, by one person, through one route."""
    out: list[list[dict[str, Any]]] = []
    for r in revisions:
        key = (r["saved_at"], r["origin"], r.get("author"), r.get("author_user"))
        if out and (out[-1][0]["saved_at"], out[-1][0]["origin"], out[-1][0].get("author"), out[-1][0].get("author_user")) == key:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def _clean(text: str) -> str:
    """A name git takes in an ident: no angle brackets or line breaks."""
    return re.sub(r"[<>\n\r]", "", text).strip() or "exuvia"


def _quote(path: str) -> str:
    """A path as fast-import's C-style quoted string (always quoted, so a
    space, quote or backslash in a file name cannot break the stream)."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _data(blob: bytes) -> bytes:
    return b"data %d\n" % len(blob) + blob + b"\n"


def message(save: list[dict[str, Any]]) -> str:
    """The commit message for one save: what it changed, then trailers."""
    origin = save[0]["origin"]

    def what(r: dict[str, Any]) -> str:
        return ("delete " if r["hash"] is None else "") + r["path"]

    if len(save) == 1:
        lines = [f"{origin}: {what(save[0])}", ""]
    else:
        lines = [f"{origin}: {len(save)} files", "", *(f"- {what(r)}" for r in save), ""]
    lines.append(f"Exuvia-Origin: {origin}")
    lines += [f"{TRAILER}: {r['id']}" for r in save]
    return "\n".join(lines) + "\n"


def ident(save: list[dict[str, Any]]) -> str:
    r = save[0]
    name = _clean(r.get("author") or r["origin"])
    who = re.sub(r"[^A-Za-z0-9._-]", "-", r.get("author_user") or r["origin"]).strip("-.") or "exuvia"
    return f"{name} <{who}@exuvia.invalid> {int(r['saved_at'])} +0000"


def fast_import_stream(
    groups: list[list[dict[str, Any]]], source: Callable[[str], str | None], parent: str | None
) -> bytes:
    """The fast-import commands that add one commit per save onto
    ``parent`` (a root commit when None), building ``IMPORT_REF``."""
    out = bytearray()
    texts: dict[str, bytes] = {}
    for i, save in enumerate(groups):
        when = int(save[0]["saved_at"])
        out += f"commit {IMPORT_REF}\n".encode()
        out += f"author {ident(save)}\n".encode()
        out += f"committer {COMMITTER} {when} +0000\n".encode()
        out += _data(message(save).encode("utf-8"))
        if i == 0 and parent:
            out += f"from {parent}\n".encode()
        for r in save:
            path = _quote(r["path"]).encode("utf-8")
            if r["hash"] is None:
                out += b"D " + path + b"\n"
                continue
            if r["hash"] not in texts:
                text = source(r["hash"])
                if text is None:
                    raise MirrorError(f"the text of {r['path']} (revision {r['id']}) is missing from the project's records")
                texts[r["hash"]] = text.encode("utf-8")
            out += b"M 100644 inline " + path + b"\n" + _data(texts[r["hash"]])
        out += b"\n"
    out += b"done\n"
    return bytes(out)


# -- git ------------------------------------------------------------------------------------
@dataclass
class Git:
    exe: str
    cwd: Path

    def run(
        self, *args: str, input: bytes | None = None, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        import os

        p = subprocess.run(
            [self.exe, *args], cwd=self.cwd, input=input, capture_output=True, env={**os.environ, **env} if env else None
        )
        if check and p.returncode != 0:
            err = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
            raise MirrorError(f"git {args[0]} failed: {err}")
        return p

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.decode("utf-8", "replace").strip()

    def sha(self, ref: str) -> str | None:
        p = self.run("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
        return p.stdout.decode().strip() or None if p.returncode == 0 else None

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def find(exe: str) -> str | None:
    return shutil.which(exe)


def open_repo(dest: Path, branch: str) -> Git:
    """``dest`` as a git repository, made (on ``branch``) when it is missing
    or an empty folder."""
    exe = find("git")
    if exe is None:
        raise MirrorError("exuvia mirror needs git: install it (https://git-scm.com) and put it on PATH")
    if dest.exists() and not dest.is_dir():
        raise MirrorError(f"{dest} is a file, not a folder")
    git = Git(exe, dest)
    if dest.is_dir() and git.run("rev-parse", "--git-dir", check=False).returncode == 0:
        top = git.run("rev-parse", "--show-toplevel", check=False)  # fails in a bare repository: fine
        where = top.stdout.decode("utf-8", "replace").strip()
        if top.returncode == 0 and where and Path(where).resolve() != dest.resolve():
            raise MirrorError(f"{dest} is inside the git repository {where}: pass its top folder, or a new one")
        return git
    if dest.is_dir() and any(dest.iterdir()):
        raise MirrorError(f"{dest} is not a git repository and not empty: pass a repository, or a new folder")
    dest.mkdir(parents=True, exist_ok=True)
    git.run("init", "-q", "-b", branch)
    return git


def github_remote(repo: str, run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> str:
    """The URL to push to for GitHub repository ``owner/name``, through the
    ``gh`` CLI and its sign-in: created (private) when it does not exist. SSH
    or HTTPS as ``gh`` is set to use (``gh config get git_protocol``)."""
    import json

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise MirrorError(f"--github takes owner/name, e.g. me/sales-code: got {repo!r}")
    if run is None:
        exe = find("gh")
        if exe is None:
            raise MirrorError("--github needs the GitHub CLI (https://cli.github.com), signed in (gh auth login); or pass --remote")

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([exe, *args], capture_output=True, text=True)

    view = run("repo", "view", repo, "--json", "url,sshUrl")
    if view.returncode != 0:
        if "could not resolve" not in (view.stderr or "").lower() and "not found" not in (view.stderr or "").lower():
            raise MirrorError(f"gh repo view {repo} failed: {(view.stderr or view.stdout).strip()}")
        made = run("repo", "create", repo, "--private", "--description", "Code history mirrored from exuvia")
        if made.returncode != 0:
            raise MirrorError(f"gh repo create {repo} failed: {(made.stderr or made.stdout).strip()}")
        view = run("repo", "view", repo, "--json", "url,sshUrl")
        if view.returncode != 0:
            raise MirrorError(f"gh repo view {repo} failed: {(view.stderr or view.stdout).strip()}")
    urls = json.loads(view.stdout)
    proto = run("config", "get", "git_protocol")
    if proto.returncode == 0 and proto.stdout.strip() == "ssh":
        return urls["sshUrl"]
    return urls["url"] + ".git"


def cursor(git: Git, tip: str) -> str:
    """The last revision the commit ``tip`` mirrored."""
    found = re.findall(rf"^{TRAILER}: (\S+)\s*$", git.out("log", "-1", "--format=%B", tip), re.M)
    if not found:
        raise MirrorError(
            f"the tip of this branch ({tip[:12]}) was not made by exuvia mirror: something else committed to it. "
            "Mirror into another branch (--branch) or repository"
        )
    return found[-1]


def tag_message(tag: dict[str, Any]) -> str:
    return f"{(tag.get('message') or tag['name']).strip()}\n\n{TRAILER}: {tag['revision']}\n"


def commits_by_revision(git: Git, tip: str) -> dict[str, str]:
    """Every revision the branch up to ``tip`` mirrored -> its commit."""
    out: dict[str, str] = {}
    log = git.run("log", "--format=%H%x1f%B%x1e", tip).stdout.decode("utf-8", "replace")
    for entry in log.split("\x1e"):
        sha, _, body = entry.strip().partition("\x1f")
        for rid in re.findall(rf"^{TRAILER}: (\S+)\s*$", body, re.M):
            out[rid] = sha
    return out


def mirror_tags(
    git: Git, source: RemoteSource | LocalSource, tip: str, remote: str | None, log: Callable[[str], None]
) -> tuple[int, int]:
    """An annotated git tag for each of ``source``'s code tags on the commit
    of its revision, pushed to ``remote`` when given. Returns (made here,
    pushed); a git tag of the same name on another commit is left alone."""
    tags = source.tags()
    if not tags:
        return 0, 0
    commits = commits_by_revision(git, tip)
    made, ready = 0, []
    for tag in sorted(tags, key=lambda t: (t["created_at"], t["name"])):
        name, commit = tag["name"], commits.get(tag["revision"])
        if commit is None or git.run("check-ref-format", f"refs/tags/{name}", check=False).returncode != 0:
            log(f"tag {name}: its revision {tag['revision']} is not on this branch; left out")
            continue
        have = git.sha(f"refs/tags/{name}")
        if have is None:
            who = _clean(tag.get("author") or "exuvia")
            email = re.sub(r"[^A-Za-z0-9._-]", "-", tag.get("author_user") or "exuvia").strip("-.") or "exuvia"
            env = {
                "GIT_COMMITTER_NAME": who,
                "GIT_COMMITTER_EMAIL": f"{email}@exuvia.invalid",
                "GIT_COMMITTER_DATE": f"{int(tag['created_at'])} +0000",
            }
            git.run("-c", "tag.gpgSign=false", "tag", "-a", "-F", "-", name, commit, input=tag_message(tag).encode("utf-8"), env=env)
            made += 1
        elif have != commit:
            log(f"tag {name}: a git tag of that name is on another commit here; left as it is")
            continue
        ready.append((name, commit))
    if not remote or not ready:
        return made, 0
    theirs: dict[str, str] = {}
    for line in git.out("ls-remote", "--tags", remote).splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.removeprefix("refs/tags/")
        if ref.endswith("^{}"):
            theirs[ref[:-3]] = sha  # an annotated tag, peeled to its commit
        else:
            theirs.setdefault(ref, sha)
    push = []
    for name, commit in ready:
        if name not in theirs:
            push.append(f"refs/tags/{name}")
        elif theirs[name] != commit:
            log(f"tag {name}: {remote} has a tag of that name on another commit; left as it is")
    if push:
        git.run("push", "-q", remote, *push)
    return made, len(push)


def remote_tip(git: Git, remote: str, branch: str) -> str | None:
    """Fetch ``branch`` from ``remote``; its tip, or None when it has none yet."""
    listed = git.run("ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if not listed.stdout.strip():
        return None
    ref = f"refs/exuvia/remote/{branch}"
    git.run("fetch", "-q", "--no-tags", remote, f"+refs/heads/{branch}:{ref}")
    return git.sha(ref)


def mirror(
    source: RemoteSource | LocalSource,
    dest: Path,
    *,
    branch: str = BRANCH,
    remote: str | None = None,
    github: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Add a commit to ``branch`` in the repository at ``dest`` for each
    save of ``source`` not mirrored yet, then push the branch to ``remote``
    (a remote's name or URL) or ``github`` (``owner/name``, through ``gh``)
    when given, with a git tag for each code tag. Returns ``{commits, tip,
    pushed, tags, tags_pushed}``; raises MirrorError."""
    if remote and github:
        raise MirrorError("give --remote or --github, not both")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("-", "/")) or ".." in branch:
        raise MirrorError(f"not a branch name: {branch!r}")
    git = open_repo(dest, branch)
    if github:
        remote = github_remote(github)
        log(f"github: {github} -> {remote}")

    local = git.sha(f"refs/heads/{branch}")
    theirs = remote_tip(git, remote, branch) if remote else None
    if local and theirs and local != theirs:
        if git.is_ancestor(local, theirs):
            base = theirs
        elif git.is_ancestor(theirs, local):
            base = local  # an earlier push did not happen; this one sends it too
        else:
            raise MirrorError(
                f"{branch} here and on {remote} have gone different ways: something other than exuvia mirror "
                "committed to one of them. Mirror into another branch (--branch) or repository"
            )
    else:
        base = local or theirs
    after = cursor(git, base) if base else None

    try:
        revisions = source.revisions_after(after)
    except KeyError:
        raise MirrorError(
            f"{branch} last mirrored revision {after}, which {source.where} does not have: "
            "is this the mirror of another project? Mirror into another branch (--branch) or repository"
        ) from None
    groups = saves(revisions)
    tip = base
    if groups:
        stream = fast_import_stream(groups, source.source, base)
        git.run("update-ref", "-d", IMPORT_REF, check=False)
        git.run("fast-import", "--quiet", "--done", input=stream)
        tip = git.sha(IMPORT_REF)
        git.run("update-ref", "-d", IMPORT_REF)
    if tip and tip != local:
        head = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
        bare = git.out("rev-parse", "--is-bare-repository") == "true"
        if head == f"refs/heads/{branch}" and not bare:
            # The branch is checked out here: move its files along too (refused,
            # with git's message, if that would overwrite a change made here).
            git.run("merge", "--ff-only", "-q", tip)
        else:
            git.run("update-ref", f"refs/heads/{branch}", tip, local or ZERO)
    pushed = False
    if remote and tip and tip != theirs:
        git.run("push", "-q", remote, f"{tip}:refs/heads/{branch}")
        pushed = True
    tags, tags_pushed = mirror_tags(git, source, tip, remote, log) if tip else (0, 0)
    return {"commits": len(groups), "tip": tip, "pushed": pushed, "remote": remote, "tags": tags, "tags_pushed": tags_pushed}
