"""A project's own secrets, and keeping exuvia's out of its nodes.

Nodes read credentials from environment variables. Each project keeps its own
secrets in ``.exuvia/secrets.json``, set through the API (``PUT
/api/secrets/{name}``) and never served back. The file sits in the project's
state folder, so code revisions, project bundles for a remote executor and hub
exports never carry it.

A node's process gets:

* the server's environment, minus exuvia's own settings (``node_environ``):
  its API and executor tokens, database URL, failure webhook, model provider
  keys, artifact store keys and package index credentials;
* the project's secrets on top, handed to the runner over its stdin
  (``runner.py``) or in the run request to a remote executor, never written to
  a file on the way.

Every value is then redacted (``Redactor``) from the run logs and errors
exuvia stores and from what the agent's tools return, so a node that prints a
secret, or an agent that asks for it, sees ``***``.
"""

from __future__ import annotations

import functools
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

FILE = "secrets.json"
# An environment variable name. EXUVIA_ is exuvia's own (see node_environ).
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
VALUE_MAX = 64 * 1024
MASK = "***"
# Values shorter than this are not redacted: masking every "1" or "true" in a
# log would hide more than it protects.
REDACT_MIN = 4

# Settings that configure exuvia itself. A project may set a name from the
# second group as its own secret (its own model provider key, say); it never
# inherits exuvia's.
OWN_PREFIXES = ("EXUVIA_", "PORTKEY_")
INDEX_CREDENTIAL = re.compile(r"^UV_INDEX_[A-Z0-9_]+_(USERNAME|PASSWORD)(_FILE)?$")


@functools.cache
def _own_names() -> frozenset[str]:
    from .cli import SECRET_ENV

    names = set(SECRET_ENV) | {"OPEN_ROUTER_KEY"}
    return frozenset(names | {f"{n}_FILE" for n in names})


def is_own(name: str, *, index: bool = True) -> bool:
    """Whether ``name`` is one of exuvia's own settings. ``index=False`` keeps
    package index credentials, which uv needs to install a project's packages."""
    if name.startswith(OWN_PREFIXES) or name in _own_names():
        return True
    return index and bool(INDEX_CREDENTIAL.match(name))


def node_environ(base: Mapping[str, str] | None = None, *, index: bool = True) -> dict[str, str]:
    """``base`` (the server's environment) without exuvia's own settings: the
    environment a node, or a package install, starts from."""
    env = os.environ if base is None else base
    return {k: v for k, v in env.items() if not is_own(k, index=index)}


def check_name(name: str) -> None:
    if not NAME.match(name):
        raise ValueError("a secret's name is an environment variable name: letters, digits and _, not starting with a digit")
    if name.startswith(OWN_PREFIXES):
        raise ValueError(f"names starting with {' or '.join(OWN_PREFIXES)} are exuvia's own settings")


def check_env(env: Any) -> dict[str, str]:
    """A run request's secrets (a remote executor checks what it is sent)."""
    if not isinstance(env, dict):
        raise ValueError("env must be an object")
    for k, v in env.items():
        check_name(k)
        if not isinstance(v, str) or len(v) > VALUE_MAX:
            raise ValueError(f"the value of {k} must be a string of at most {VALUE_MAX} characters")
    return env


class Redactor:
    """Replaces known secret values in text with ``***``. A value that spans
    lines is also masked line by line, because run logs arrive a line at a
    time."""

    def __init__(self, values: Iterable[str] = ()):
        needles: set[str] = set()
        for v in values:
            for part in {v, *(line.strip() for line in v.splitlines())}:
                if len(part) >= REDACT_MIN:
                    needles.add(part)
                    # The same value inside JSON text (a tool's output).
                    escaped = json.dumps(part)[1:-1]
                    if escaped != part:
                        needles.add(escaped)
        # Longest first, so a value that contains another is masked whole.
        self._re = re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True))) if needles else None

    def __bool__(self) -> bool:
        return self._re is not None

    def text(self, s: str) -> str:
        return self._re.sub(MASK, s) if self._re is not None and s else s

    def obj(self, value: Any) -> Any:
        """``value`` with every string in it (dict keys and values, list
        items) redacted."""
        if self._re is None:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.obj(k): self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


class ProjectSecrets:
    """The secrets in one project's ``.exuvia/secrets.json``: ``{name: {value,
    updated_at}}``, readable only by the server's user."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / FILE
        self._lock = threading.Lock()
        self._cache: tuple[float, int, dict[str, dict[str, Any]]] | None = None

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return {}
        if self._cache and self._cache[:2] == (st.st_mtime, st.st_size):
            return self._cache[2]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("value"), str)} if isinstance(data, dict) else {}
        self._cache = (st.st_mtime, st.st_size, data)
        return data

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        if not data:
            self.path.unlink(missing_ok=True)
            self._cache = None
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".secrets-", suffix=".tmp")
        try:
            os.chmod(tmp, 0o600)  # mkstemp already does on POSIX; be explicit
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._cache = None

    def names(self) -> list[dict[str, Any]]:
        """Each secret's name and when it was last set, never its value."""
        with self._lock:
            return [{"name": k, "updated_at": v.get("updated_at")} for k, v in sorted(self._read().items())]

    def values(self) -> dict[str, str]:
        with self._lock:
            return {k: v["value"] for k, v in self._read().items()}

    def redactor(self) -> Redactor:
        return Redactor(self.values().values())

    def set(self, name: str, value: str) -> None:
        check_name(name)
        if not isinstance(value, str) or not value:
            raise ValueError("a secret needs a value; delete it to remove it")
        if len(value) > VALUE_MAX:
            raise ValueError(f"a secret's value is at most {VALUE_MAX // 1024} KB")
        with self._lock:
            data = dict(self._read())
            data[name] = {"value": value, "updated_at": time.time()}
            self._write(data)

    def delete(self, name: str) -> bool:
        with self._lock:
            data = dict(self._read())
            if data.pop(name, None) is None:
                return False
            self._write(data)
            return True
