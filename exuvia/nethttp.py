"""HTTP client settings for the commands that call a running exuvia
(``exuvia tool``, ``exuvia mcp --url``, ``push``, ``cancel``, ``mirror``,
``env --url``), so they work on a machine behind a corporate proxy.

* **A workbench on this machine is reached directly.** ``localhost``,
  ``127.0.0.1`` and ``::1`` never go through ``HTTP_PROXY`` /
  ``HTTPS_PROXY``. A corporate proxy cannot reach your loopback interface,
  and a ``NO_PROXY`` that leaves it out is common.
* **TLS is checked against the operating system's trust store**
  (truststore), which is where IT installs the certificate of a proxy that
  inspects HTTPS, as well as any company CA. Python's own certifi bundle
  knows neither. ``SSL_CERT_FILE`` or ``SSL_CERT_DIR``, when set, are used
  instead.
* Anything else follows the environment as usual: ``HTTPS_PROXY``,
  ``HTTP_PROXY``, ``ALL_PROXY`` and ``NO_PROXY``.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlsplit

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def client_options(url: str) -> dict[str, Any]:
    """Keyword arguments for ``httpx.Client`` / ``AsyncClient`` (or
    ``httpx.post``) that call ``url``."""
    if loopback(urlsplit(url).hostname):
        return {"trust_env": False}
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return {}
    try:
        import ssl

        import truststore
    except ImportError:  # pragma: no cover - a dependency; certifi then
        return {}
    return {"verify": truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)}


def proxy_hint(url: str, error: Exception | None = None) -> str:
    """A sentence on the proxy settings that may explain why ``url`` could
    not be reached, or ``""`` when none apply."""
    parts = urlsplit(url)
    if loopback(parts.hostname):
        return ""
    proxy = next((f"{v}={_safe(os.environ[v])}" for v in PROXY_VARS if os.environ.get(v)), None)
    tls = error is not None and ("SSL" in type(error).__name__ or "CERTIFICATE" in str(error).upper())
    if tls:
        return (
            " The TLS certificate was not trusted: if a proxy inspects HTTPS here, install its certificate in the "
            "system trust store, or set SSL_CERT_FILE to a PEM bundle that includes it."
        )
    if proxy:
        return f" Requests go through the proxy {proxy}: check that it lets {parts.hostname} through, or add it to NO_PROXY."
    return ""


def _safe(proxy: str) -> str:
    """A proxy URL without its password."""
    parts = urlsplit(proxy)
    if parts.password:
        return proxy.replace(f":{parts.password}@", ":***@", 1)
    return proxy
