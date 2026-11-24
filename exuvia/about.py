"""``GET /api/about``: the running version, how it was installed, and whether
a newer release is out.

The workbench's help menu shows this. The check asks PyPI (every release goes
to PyPI and to the image at the same version, see release.yml), at most once
every few hours per process, and only when someone opens the workbench.
``EXUVIA_UPDATE_CHECK=off`` turns it off, for a server that should not reach
the internet.

How exuvia was installed decides what the menu tells people to do about a new
release. On a laptop that is one command (``uv tool upgrade exuvia``, ...).
On a deployed server it is the operator's job (pull the new image and
restart), and the person looking at the workbench usually can't do it, so the
menu says so instead of handing them a command for a machine they don't have.
``EXUVIA_DEPLOYED=1`` marks a deployed server; the image sets it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from importlib import metadata
from typing import Any

import httpx

from . import __version__

PYPI_URL = "https://pypi.org/pypi/exuvia/json"
REPO_URL = "https://github.com/rlancer/exuvia"
IMAGE = "ghcr.io/rlancer/exuvia"
DOCS_URL = "https://exuvia.lobster.mp/"
CHECK_EVERY = 6 * 3600  # seconds between checks that answered
RETRY_AFTER = 15 * 60  # seconds before a failed check is tried again
REFRESH_MIN = 60  # "Check again" asks PyPI at most this often
TIMEOUT = 5.0

_OFF = {"0", "off", "false", "no", "never"}
_ON = {"1", "on", "true", "yes"}

# One answer per process, shared by every project on a hub.
_cache: dict[str, Any] = {"latest": None, "error": None, "checked_at": 0.0}
_lock: asyncio.Lock | None = None


def deployed() -> bool:
    """Whether this is a deployed server (``EXUVIA_DEPLOYED``) rather than
    someone's own install. The image sets it; set it on any other server
    whose users don't run its command line themselves."""
    return os.environ.get("EXUVIA_DEPLOYED", "").strip().lower() in _ON


def check_enabled() -> bool:
    return os.environ.get("EXUVIA_UPDATE_CHECK", "").strip().lower() not in _OFF


def install_kind() -> str:
    """How this exuvia was installed: ``deployed``, ``source`` (an editable
    install from a clone), ``uvx``, ``uv-tool``, ``pipx`` or ``pip``."""
    if deployed():
        return "deployed"
    try:
        raw = metadata.distribution("exuvia").read_text("direct_url.json")
        if raw and json.loads(raw).get("dir_info", {}).get("editable"):
            return "source"
    except (metadata.PackageNotFoundError, ValueError):
        return "source"  # imported from a tree that is not installed
    prefix = sys.prefix.replace("\\", "/").lower()
    if "/uv/tools/" in prefix:
        return "uv-tool"
    if "/archive-v" in prefix:  # uvx runs tools from uv's cache
        return "uvx"
    if "pipx" in prefix:
        return "pipx"
    return "pip"


# What updates exuvia, per install kind; null where it is not a command the
# person in front of the workbench runs.
UPGRADE = {
    "uvx": "uvx exuvia@latest {command} {target}",
    "uv-tool": "uv tool upgrade exuvia",
    "pipx": "pipx upgrade exuvia",
    "pip": "pip install -U exuvia",
    "source": "git pull && uv sync",
    "deployed": None,
}


def parse(v: str) -> tuple[int, ...] | None:
    """A release version as a tuple of ints; None for anything else (a
    development build such as ``0.3.9.dev2+g1a2b3c``, or ``0.0.0`` from a tree
    without git metadata), which an update check can't place."""
    if not re.fullmatch(r"\d+(\.\d+)*", v) or not any(int(p) for p in v.split(".")):
        return None
    return tuple(int(p) for p in v.split("."))


def is_newer(latest: str | None, current: str) -> bool:
    a, b = parse(latest or ""), parse(current)
    return a is not None and b is not None and a > b


async def _fetch() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(PYPI_URL, headers={"Accept": "application/json", "User-Agent": f"exuvia/{__version__}"})
        r.raise_for_status()
        latest = r.json()["info"]["version"]
        if not isinstance(latest, str) or not latest:
            raise ValueError("PyPI's answer has no version")
        return latest


async def latest_version(refresh: bool = False) -> tuple[str | None, str | None, float]:
    """``(latest, error, checked_at)``, from the cache while it is fresh."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        age = time.time() - _cache["checked_at"]
        stale = age > (RETRY_AFTER if _cache["error"] else CHECK_EVERY)
        if stale or (refresh and age > REFRESH_MIN):
            try:
                _cache.update(latest=await _fetch(), error=None)
            except Exception as e:  # offline, blocked, PyPI down: say so, don't fail the page
                _cache.update(error=f"couldn't reach PyPI ({type(e).__name__})")
            _cache["checked_at"] = time.time()
        return _cache["latest"], _cache["error"], _cache["checked_at"]


async def about(refresh: bool = False, command: str = "serve", target: str = "<folder>") -> dict[str, Any]:
    """What ``GET /api/about`` answers.

    ``update`` is ``off`` (checks turned off), ``dev`` (a development build:
    nothing to compare), ``error`` (PyPI didn't answer), ``available`` or
    ``current``. ``upgrade`` is the command that updates this install, or null
    on a deployed server, whose operator updates it by redeploying ``image``.
    ``command`` is the one this process runs (``serve``, ``hub`` or ``mcp``)
    and ``target`` its argument (a folder, or ``--url URL`` for a relay), for
    uvx, where the update is running it again with ``@latest``.
    """
    kind = install_kind()
    out: dict[str, Any] = {
        "version": __version__,
        "install": kind,
        "deployed": kind == "deployed",
        "upgrade": (UPGRADE[kind] or "").format(command=command, target=target) or None,
        "image": IMAGE,
        "latest": None,
        "update": "off",
        "error": None,
        "checked_at": None,
        "changes_url": None,
        "repo_url": REPO_URL,
        "docs_url": DOCS_URL,
        "issues_url": f"{REPO_URL}/issues",
    }
    if not check_enabled():
        return out
    if parse(__version__) is None:
        out["update"] = "dev"
        return out
    latest, error, checked_at = await latest_version(refresh)
    out.update(latest=latest, error=error, checked_at=checked_at or None)
    if is_newer(latest, __version__):
        out["update"] = "available"
        out["changes_url"] = f"{REPO_URL}/compare/v{__version__}...v{latest}"
    elif error and latest is None:
        out["update"] = "error"
    else:
        out["update"] = "current"
    return out
