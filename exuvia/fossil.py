"""Mirror a project's code revisions into a Fossil repository.

exuvia already records every version of every code file (see ``store.py``,
"code revisions"). When the ``fossil`` binary (https://fossil-scm.org/) is
available, each project also gets a Fossil repository holding the same
history: every save becomes one check-in, made by the save's origin (``agent``,
``api``, ``file``, ...) at the time it was saved. That gives a project a
real version-control repository to browse (timeline, diffs, annotate), to
``fossil clone``, or to ``fossil git export``, without exuvia becoming
dependent on it.

The repository is derived data. It lives in ``.exuvia/fossil/`` next to the
project's other state (``project.fossil``, a check-out exuvia commits from,
and the cursor of the last revision mirrored), and it is rebuilt from the
recorded revisions whenever any of those is missing. The project's own
directory never gets a Fossil check-out, so a project that is itself a git
or Fossil check-out is left alone.

``EXUVIA_FOSSIL`` controls it: unset (or ``auto``) mirrors when ``fossil`` is
on PATH, ``off`` never does, and anything else is the path of the binary.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("exuvia.fossil")

FOSSIL_DIR = "fossil"  # under the project's .exuvia/
ADMIN_USER = "exuvia"
# Capabilities of "nobody" (every request that reaches it, after exuvia's own
# auth): clone (g), read wiki (j), check out (o), read tickets (r), download
# zips (z), hyperlinks (h). No check-in (i): the repository mirrors exuvia's
# history, so a push into it would fork that history.
READER_CAPS = "ghjorz"
RETRY_SECONDS = 30.0
# Response headers that describe the connection to fossil, not the response.
_HOP_HEADERS = {"connection", "content-length", "transfer-encoding", "keep-alive"}
# Request headers worth passing on to fossil.
_PASS_HEADERS = ("content-type", "user-agent", "accept", "accept-language", "if-none-match",
                 "if-modified-since", "cookie", "referer")
_MISSING = object()
_OFF = {"0", "off", "false", "no"}
_AUTO = {"", "1", "on", "true", "yes", "auto"}


def fossil_binary() -> str | None:
    """The fossil binary to mirror with, or None when mirroring is off."""
    value = os.environ.get("EXUVIA_FOSSIL", "").strip()
    if value.lower() in _OFF:
        return None
    if value.lower() in _AUTO:
        return shutil.which("fossil")
    return shutil.which(value) or (value if Path(value).is_file() else None)


class FossilError(RuntimeError):
    pass


class FossilMirror:
    """Keeps ``.exuvia/fossil/project.fossil`` in step with a store's code
    revisions. ``sync()`` mirrors whatever is new (safe to call any time, from
    any thread); ``start()`` runs it in a background thread each time
    ``notify()`` says revisions were recorded."""

    def __init__(self, store: "Store", state_dir: Path, project_name: str, binary: str):
        self.store = store
        self.binary = binary
        self.project_name = project_name
        self.dir = state_dir / FOSSIL_DIR
        self.repo = self.dir / "project.fossil"
        self.checkout = self.dir / "checkout"
        self.cursor_file = self.dir / "mirrored.json"
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._wake.set()  # catch up on anything recorded while nothing ran
        self._thread = threading.Thread(target=self._loop, name="exuvia-fossil", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread, letting a check-in in progress finish."""
        if self._thread is None:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join()
        self._thread = None

    def notify(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            # After a failure, try again in a while even if nothing is saved.
            self._wake.wait(RETRY_SECONDS if self.last_error else None)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.sync()

    # -- mirroring ---------------------------------------------------------
    def sync(self) -> int:
        """Commit every revision recorded since the last one mirrored. Returns
        the number of check-ins made; failures are logged and kept in
        ``last_error`` rather than raised, since saving code must not depend
        on the mirror."""
        with self._lock:
            try:
                made = self._sync()
            except (FossilError, OSError) as e:
                self.last_error = str(e)
                log.warning("fossil mirror of %s: %s", self.project_name, e)
                return 0
            self.last_error = None
            return made

    def _sync(self) -> int:
        cursor = self._cursor()
        if cursor is _MISSING:
            # Build (or rebuild) the repository from the store. It starts with
            # the first save, so its initial check-in can carry that save's
            # time and every later check-in sorts after it.
            first = self.store.code_revisions_after(None, limit=1)
            if not first:
                return 0
            self._build(first[0]["saved_at"])
            cursor = None
        made = 0
        while True:
            revs = self.store.code_revisions_after(cursor, limit=500)
            if not revs:
                return made
            for group in _saves(revs):
                made += self._commit(group)
                last = group[-1]
                cursor = (last["saved_at"], last["id"])
                self._write_cursor(cursor)

    def _cursor(self) -> Any:
        """The ``(saved_at, id)`` of the last revision mirrored (None: none
        yet), or ``_MISSING`` when the repository, its check-out or the
        cursor is missing or unreadable and everything must be rebuilt."""
        opened = (self.checkout / ".fslckout").is_file() or (self.checkout / "_FOSSIL_").is_file()
        if not (self.repo.is_file() and opened and self.cursor_file.is_file()):
            return _MISSING
        try:
            data = json.loads(self.cursor_file.read_text(encoding="utf-8"))
            return (float(data["saved_at"]), str(data["id"])) if data else None
        except (ValueError, KeyError, TypeError):
            return _MISSING

    def _build(self, started: float) -> None:
        """A fresh repository whose initial check-in is dated ``started``, and
        a check-out of it. The cursor is written last, so a build cut short
        is redone."""
        shutil.rmtree(self.dir, ignore_errors=True)
        self.checkout.mkdir(parents=True)
        self._run(
            "init", "-A", ADMIN_USER, "--project-name", self.project_name,
            "--date-override", _fossil_time(started), str(self.repo), cwd=self.dir,
        )
        self._run("user", "capabilities", "nobody", READER_CAPS, "-R", str(self.repo), cwd=self.dir)
        self._run("settings", "dotfiles", "on", "-R", str(self.repo), cwd=self.dir)
        # Backoffice work (email alerts, ...) runs after a request and keeps
        # that `fossil http` process alive; a mirror has none to do.
        self._run("settings", "backoffice-disable", "on", "-R", str(self.repo), cwd=self.dir)
        self._run("open", str(self.repo), "--workdir", str(self.checkout), "--nested", "-f", cwd=self.checkout)
        self._write_cursor(None)

    def _commit(self, group: list[dict[str, Any]]) -> int:
        """Apply one save's files to the check-out and commit them."""
        paths: list[str] = []
        for rev in group:
            target = self.checkout / rev["path"]
            if not target.resolve().is_relative_to(self.checkout.resolve()):
                continue  # recorded paths are relative to the project; be sure
            if rev["hash"] is None:
                target.unlink(missing_ok=True)
                paths.append(f"delete {rev['path']}")
                continue
            source = self.store.code_source(rev["hash"])
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.encode("utf-8"))
            paths.append(rev["path"])
        if not paths:
            return 0
        self._run("addremove", "--dotfiles", cwd=self.checkout)
        origin = group[0]["origin"]
        shown = ", ".join(paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
        out = self._run(
            "commit", "-m", f"{origin}: {shown}",
            "--user-override", origin,
            "--date-override", _fossil_time(group[0]["saved_at"]),
            "--if-changes", "--allow-older", "--no-warnings", "--no-verify-comment",
            "--no-prompt", "--nosync", "--ignore-clock-skew",
            cwd=self.checkout,
        )
        return 1 if "New_Version" in out else 0

    def _write_cursor(self, cursor: tuple[float, str] | None) -> None:
        data = {"saved_at": cursor[0], "id": cursor[1]} if cursor else None
        tmp = self.cursor_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.cursor_file)

    def _env(self) -> dict[str, str]:
        # A private global config: fossil records every repository it opens
        # in it, and the user's own ~/.fossil is none of exuvia's business.
        return {**os.environ, "FOSSIL_HOME": str(self.dir), "FOSSIL_USER": ADMIN_USER}

    def _run(self, *args: str, cwd: Path, stdin: bytes | None = None) -> str:
        proc = subprocess.run(
            [self.binary, *args],
            cwd=cwd,
            env=self._env(),
            input=stdin,
            capture_output=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        out = proc.stdout.decode("utf-8", "replace")
        if proc.returncode != 0:
            err = (proc.stderr.decode("utf-8", "replace") or out).strip()
            raise FossilError(f"fossil {args[0]} failed ({proc.returncode}): {err[-500:]}")
        return out

    # -- serving -----------------------------------------------------------
    def http(
        self, method: str, path: str, query: str, headers: dict[str, str], body: bytes, base_url: str
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """Answer one request with ``fossil http``: the web UI, and the sync
        protocol ``fossil clone`` speaks. ``path`` is relative to ``base_url``
        (the URL the repository is served at). Returns status, headers, body."""
        self.sync()
        if not self.repo.is_file():
            raise FossilError(self.last_error or "nothing has been saved yet; the repository starts with the first save")
        target = (path if path.startswith("/") else "/" + path) + (f"?{query}" if query else "")
        lines = [f"{method} {target} HTTP/1.0", f"Host: {headers.get('host', 'localhost')}"]
        lines += [f"{k}: {headers[k]}" for k in _PASS_HEADERS if k in headers]
        if body or method in ("POST", "PUT"):
            lines.append(f"Content-Length: {len(body)}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        # --ipaddr: never let fossil take the request for a local one (it
        # trusts those more); exuvia's own auth already let the caller in.
        args = ["http", str(self.repo), "--baseurl", base_url, "--nossl", "--nocompress", "--nodelay", "--ipaddr", "10.0.0.1"]
        if base_url.startswith("https:"):
            args.append("--https")
        proc = subprocess.run(
            [self.binary, *args],
            cwd=self.dir,
            env=self._env(),
            input=request,
            capture_output=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return _parse_response(proc.stdout)


def _saves(revs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split revisions (oldest first) into saves: one ``record_code`` call
    stamps all its files with the same time and origin."""
    groups: list[list[dict[str, Any]]] = []
    for r in revs:
        if groups and groups[-1][0]["saved_at"] == r["saved_at"] and groups[-1][0]["origin"] == r["origin"]:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


def _fossil_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _parse_response(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise FossilError("fossil http returned no response")
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as e:
        raise FossilError(f"fossil http returned {lines[0]!r}") from e
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name and name.strip().lower() not in _HOP_HEADERS:
            headers.append((name.strip(), value.strip()))
    return status, headers, body
