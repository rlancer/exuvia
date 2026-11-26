"""One client for the commands that call a running exuvia (``exuvia tool``,
``exuvia mcp --url``, ``push``, ``cancel``, ``mirror``, ``env --url``).

* :func:`check_url`: the address must be http(s), and plain http only to
  this machine unless the caller allows it. Raises ValueError.
* :func:`token`: the bearer token, from ``EXUVIA_API_TOKEN`` or the file
  ``EXUVIA_API_TOKEN_FILE`` names. Raises ValueError when that file cannot
  be read.
* :func:`connect`: an httpx client for the address, with the proxy and TLS
  settings of nethttp.py, the token and the ``X-Exuvia-Origin`` header. A
  request that gets no answer raises :class:`Unreachable` instead of an
  httpx error.
* :func:`unreachable`: the one "could not reach" message, with the proxy or
  TLS hint when one applies.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .nethttp import client_options, loopback, proxy_hint

if TYPE_CHECKING:
    import httpx

FROM_ENV: Any = object()  # connect(token=...): read EXUVIA_API_TOKEN / EXUVIA_API_TOKEN_FILE


def check_url(url: str, allow_http: bool = False) -> str:
    """``url`` without its trailing slash, if it is an address to send a
    token to: https, or plain http to this machine (or when ``allow_http``)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"--url must be an http(s) address, e.g. https://exuvia.example.com/p/research: got {url!r}")
    if parts.scheme == "http" and not allow_http and not loopback(parts.hostname):
        raise ValueError(
            f"{url} is plain http on another machine: the value and your token would cross the network "
            "unencrypted. Use its https address, or --allow-http if the network is private."
        )
    return url.rstrip("/")


def token(explicit: str | None = None) -> str | None:
    """``explicit``, else ``EXUVIA_API_TOKEN``, else the contents of the file
    ``EXUVIA_API_TOKEN_FILE`` names, else None."""
    if explicit:
        return explicit
    if os.environ.get("EXUVIA_API_TOKEN"):
        return os.environ["EXUVIA_API_TOKEN"]
    path = os.environ.get("EXUVIA_API_TOKEN_FILE")
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
        except OSError as e:
            raise ValueError(f"EXUVIA_API_TOKEN_FILE: cannot read {path}: {e.strerror or e}") from e
    return None


def unreachable(url: str, error: BaseException, *, what: str = "exuvia", advice: str = "is it running?") -> str:
    """Every "could not reach" message: where, the error's type, what to do,
    and the proxy or TLS hint when either may be what failed."""
    if isinstance(error, Unreachable):
        error = error.error
    return f"could not reach {what} at {url} ({type(error).__name__}): {advice}{proxy_hint(url, error)}"


class Unreachable(Exception):
    """A running exuvia did not answer: refused, timed out, or the proxy or
    TLS failed. ``str()`` is :func:`unreachable`'s message; ``url`` and
    ``error`` let a caller word its own advice."""

    def __init__(self, url: str, error: BaseException):
        self.url, self.error = url, error
        super().__init__(unreachable(url, error))


def connect(
    url: str,
    allow_http: bool = False,
    *,
    origin: str,
    token: str | None = FROM_ENV,
    timeout: Any = 30.0,
    asynchronous: bool = False,
) -> Any:
    """An ``httpx.Client`` (``AsyncClient`` when ``asynchronous``) for the
    exuvia at ``url``: relative paths resolve against it. ``token``: the
    bearer token, by default :func:`token`'s; None sends none. Raises
    ValueError for an address :func:`check_url` refuses or a token file
    that cannot be read."""

    base = check_url(url, allow_http)
    headers = {"X-Exuvia-Origin": origin}
    tok = _env_token() if token is FROM_ENV else token
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    client, async_client = _client_classes()
    cls = async_client if asynchronous else client
    return cls(base, base_url=base, headers=headers, timeout=timeout, **client_options(base))


_env_token = token


@functools.cache
def _client_classes() -> tuple[type[httpx.Client], type[httpx.AsyncClient]]:
    """httpx clients that raise Unreachable when nothing answers (defined on
    first use: httpx is imported then, not with this module)."""
    import httpx

    class Client(httpx.Client):
        def __init__(self, where: str, **kw: Any):
            super().__init__(**kw)
            self.where = where

        def send(self, request: httpx.Request, **kw: Any) -> httpx.Response:
            try:
                return super().send(request, **kw)
            except httpx.HTTPError as e:
                raise Unreachable(self.where, e) from e

    class AsyncClient(httpx.AsyncClient):
        def __init__(self, where: str, **kw: Any):
            super().__init__(**kw)
            self.where = where

        async def send(self, request: httpx.Request, **kw: Any) -> httpx.Response:
            try:
                return await super().send(request, **kw)
            except httpx.HTTPError as e:
                raise Unreachable(self.where, e) from e

    return Client, AsyncClient

