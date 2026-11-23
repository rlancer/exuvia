"""A project's environment variables: the API keys, tokens and settings its
nodes read with ``os.environ``, set in the workbench instead of a ``.env`` file.

They live in the project's database (``Store.env_vars``), so they work the
same on ``exuvia serve``, on a hub (each project its own) and with a shared
``EXUVIA_DB``, and they reach a remote executor with each run, which a
``.env`` never does. A run gets them on top of the environment it would get
anyway (``pool.node_environ``): a project variable wins over the server's
variable of the same name.

A variable is *secret* or *plain*. A plain one is configuration anyone who
can open the project may read (the agent too). A secret one is write-only:
once saved its value is never sent to a client or the agent again, and
wherever it shows up in a run's log or error it is replaced with ``***``
before the log is stored, streamed or read by the agent.

Names are checked so a project cannot set exuvia's own settings for its
nodes (``EXUVIA_*``, ``PORTKEY_*``, package index credentials), which nodes
are never given (see pool.py).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# What a node's process never gets from the server (pool.OWN_PREFIXES,
# pool.INDEX_CREDENTIAL), so a project cannot give it either.
RESERVED_PREFIXES = ("EXUVIA_", "PORTKEY_")
INDEX_CREDENTIAL = re.compile(r"^UV_INDEX_[A-Z0-9_]+_(USERNAME|PASSWORD)(_FILE)?$", re.IGNORECASE)
# Variables that would change how the runner itself starts or finds its code.
RESERVED_NAMES = frozenset({"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "PYTHONUSERBASE", "VIRTUAL_ENV"})
# A service account's JSON or a PEM key fits; a dataset does not belong here.
MAX_VALUE_CHARS = 32_768
MAX_VARIABLES = 200
# A secret shorter than this is not looked for in logs: "1" or "on" would
# mask every digit or word that happens to match.
MIN_REDACT_CHARS = 4
MASK = "***"


class EnvError(ValueError):
    """A variable that cannot be set as asked."""


def check_name(name: str) -> str:
    if not NAME.match(name or ""):
        raise EnvError(
            "A variable's name is letters, digits and underscores, starting with a letter or underscore "
            "(e.g. FRED_API_KEY), at most 128 characters."
        )
    upper = name.upper()
    if upper.startswith(RESERVED_PREFIXES) or INDEX_CREDENTIAL.match(name):
        raise EnvError(
            f"{name} is one of exuvia's own settings, which nodes never get. Set it in the server's environment."
        )
    if upper in RESERVED_NAMES:
        raise EnvError(f"{name} is set by exuvia for every run and cannot be changed per project.")
    return name


def check_value(value: str) -> str:
    if not isinstance(value, str):
        raise EnvError("A variable's value is text.")
    if "\x00" in value:
        raise EnvError("A variable's value cannot contain a NUL character.")
    if len(value) > MAX_VALUE_CHARS:
        raise EnvError(f"A variable's value is at most {MAX_VALUE_CHARS:,} characters; put larger data in a node.")
    return value


def public(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a client (or the agent) may see of the variables: every name, a
    plain variable's value, and never a secret's."""
    return [
        {
            "name": r["name"],
            "secret": bool(r["secret"]),
            "value": None if r["secret"] else r["value"],
            "updated_at": r.get("updated_at"),
            "updated_by": r.get("updated_by"),
        }
        for r in rows
    ]


def run_env(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """The variables a run sets, name to value."""
    return {r["name"]: r["value"] for r in rows}


class Redactor:
    """Replaces each secret value in a text with ``***``. Longer secrets go
    first, so one that contains another is masked whole."""

    def __init__(self, rows: Iterable[dict[str, Any]]):
        values: set[str] = set()
        for r in rows:
            if not r["secret"]:
                continue
            v = r["value"]
            # A multi-line secret (a PEM key) is printed line by line as often as whole.
            for part in (v, *v.splitlines()):
                part = part.strip()
                if len(part) >= MIN_REDACT_CHARS:
                    values.add(part)
        self._pattern = (
            re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))) if values else None
        )

    def __bool__(self) -> bool:
        return self._pattern is not None

    def __call__(self, text: str | None) -> str | None:
        if not text or self._pattern is None:
            return text
        return self._pattern.sub(MASK, text)
