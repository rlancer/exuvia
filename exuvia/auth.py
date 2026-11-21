"""Who is making a request.

``EXUVIA_AUTH`` picks how people are identified:

* ``token`` (the default): no per-person identity. ``EXUVIA_API_TOKEN``, when
  set, is a shared secret every caller presents (see ``server.py``).
* ``headers``: a proxy in front of exuvia (an SSO gateway, oauth2-proxy, an
  ingress with auth) has already signed the person in and passes who they are
  in request headers: ``EXUVIA_AUTH_USER_HEADER`` (default
  ``X-Forwarded-User``) and ``EXUVIA_AUTH_EMAIL_HEADER`` (default
  ``X-Forwarded-Email``). exuvia trusts them as given, so it must only be
  reachable through that proxy; ``EXUVIA_AUTH_TRUSTED_PROXIES`` (IPs or
  CIDRs) ignores the headers on requests from anywhere else.

The identity of the request being served is in a context variable, so code
deep in the workspace (a run starting, a file being saved) can record who did
it without every call passing it along. Tasks started while serving a request
inherit it; background loops start with an empty context.
"""

from __future__ import annotations

import ipaddress
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Mapping

MODES = ("token", "headers")
DEFAULT_USER_HEADER = "X-Forwarded-User"
DEFAULT_EMAIL_HEADER = "X-Forwarded-Email"
MAX_VALUE = 256


@dataclass(frozen=True)
class Identity:
    """A signed-in person. ``id`` is what records name them by: the user
    name, or the email when the proxy sends no user name."""

    user: str | None
    email: str | None
    method: str

    @property
    def id(self) -> str:
        return self.user or self.email or ""

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "id": self.id}


_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class HeaderAuth:
    user_header: str
    email_header: str
    trusted: tuple[_Network, ...]  # empty: any peer

    def trusts(self, peer: str | None) -> bool:
        if not self.trusted:
            return True
        try:
            addr = ipaddress.ip_address(peer or "")
        except ValueError:
            return False  # no peer address (or not an IP): not a known proxy
        return any(addr in net for net in self.trusted)

    def identify(self, headers: Mapping[str, str], peer: str | None) -> Identity | None:
        """The person the proxy says this is, or None (no headers, or a peer
        that is not a trusted proxy)."""
        if not self.trusts(peer):
            return None
        user = _clean(headers.get(self.user_header))
        email = _clean(headers.get(self.email_header))
        if not user and not email:
            return None
        return Identity(user=user, email=email, method="headers")


def _clean(value: str | None) -> str | None:
    """A header value as an identity: trimmed, printable, bounded."""
    v = (value or "").strip()
    if not v or len(v) > MAX_VALUE or any(ord(c) < 32 or ord(c) == 127 for c in v):
        return None
    return v


def mode() -> str:
    m = os.environ.get("EXUVIA_AUTH", "token").strip().lower() or "token"
    if m not in MODES:
        raise SystemExit(f"EXUVIA_AUTH must be one of {', '.join(MODES)}, not {m!r}")
    return m


def header_auth() -> HeaderAuth | None:
    """The header settings when ``EXUVIA_AUTH=headers``, else None."""
    if mode() != "headers":
        return None
    nets: list[_Network] = []
    for part in os.environ.get("EXUVIA_AUTH_TRUSTED_PROXIES", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise SystemExit(f"EXUVIA_AUTH_TRUSTED_PROXIES: {part!r} is not an IP address or CIDR") from None
    return HeaderAuth(
        user_header=os.environ.get("EXUVIA_AUTH_USER_HEADER", "").strip() or DEFAULT_USER_HEADER,
        email_header=os.environ.get("EXUVIA_AUTH_EMAIL_HEADER", "").strip() or DEFAULT_EMAIL_HEADER,
        trusted=tuple(nets),
    )


_IDENTITY: ContextVar[Identity | None] = ContextVar("exuvia_identity", default=None)


def current() -> Identity | None:
    """Who the request being served is from (None: nobody signed in)."""
    return _IDENTITY.get()


def current_id() -> str | None:
    ident = _IDENTITY.get()
    return ident.id if ident else None


@contextmanager
def signed_in(identity: Identity | None) -> Iterator[None]:
    """Attribute what happens inside this block to ``identity``."""
    token = _IDENTITY.set(identity)
    try:
        yield
    finally:
        _IDENTITY.reset(token)
