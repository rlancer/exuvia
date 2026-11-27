"""What a hub's admin page shows about how the server is set up.

Most of exuvia is set by environment variables when the server starts
(README, "Configuration"), so this is a report, not a form: each setting's
name, the variable that sets it, its value as the server sees it, and whether
it was set or is the default. Changing one means changing the variable and
restarting.

Nothing secret leaves here: a token, key, password or webhook URL shows only
as set or not set, and a URL loses its user name, password, query and
fragment, which is where credentials hide (``postgresql://user:pw@host/db``,
``https://hooks.example/...?token=``).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def shown_url(url: str) -> str:
    """A URL without the parts credentials go in: user, password, query, fragment."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "(not a URL)"
    if not parts.scheme:
        return "(not a URL)"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _item(label: str, env: str | None, value: str, *, is_set: bool | None = None, note: str = "") -> dict[str, Any]:
    """One row. ``is_set``: the variable was given (default: it is non-empty)."""
    return {
        "label": label, "env": env, "value": value,
        "set": bool(_env(env)) if is_set is None and env else bool(is_set),
        **({"note": note} if note else {}),
    }


def _secret(label: str, env: str, note: str = "") -> dict[str, Any]:
    given = bool(_env(env)) or bool(_env(f"{env}_FILE"))
    return _item(label, env, "set" if given else "not set", is_set=given, note=note)


def _url(label: str, env: str, default: str, note: str = "") -> dict[str, Any]:
    v = _env(env)
    return _item(label, env, shown_url(v) if v else default, note=note)


def _sign_in(auth_method: str) -> list[dict[str, Any]]:
    from .sharing import new_project_general
    from .users import pinned_admins

    how = {"headers": "a sign-in proxy names each person", "token": "the shared API token", "none": "nobody signs in"}
    admins = sorted(pinned_admins())
    return [
        _item("How people get in", "EXUVIA_AUTH", how.get(auth_method, auth_method), is_set=bool(_env("EXUVIA_AUTH"))),
        _secret("API token", "EXUVIA_API_TOKEN", "Scripts and agents send it as a bearer token."),
        _item("Admins always", "EXUVIA_ADMINS", ", ".join(admins) if admins else "nobody named",
              note="Always admins, and nobody can demote or disable them here."),
        _item("Identity headers", "EXUVIA_AUTH_USER_HEADER",
              f"{_env('EXUVIA_AUTH_USER_HEADER') or 'X-Forwarded-User'} / {_env('EXUVIA_AUTH_EMAIL_HEADER') or 'X-Forwarded-Email'}"),
        _item("Proxies trusted for them", "EXUVIA_AUTH_TRUSTED_PROXIES", _env("EXUVIA_AUTH_TRUSTED_PROXIES") or "any peer"),
        _item("Browsers get the token", "EXUVIA_BROWSER_AUTH",
              "on every page load" if _env("EXUVIA_BROWSER_AUTH").lower() == "open" else "after entering it once"),
        _item("New projects' general access", "EXUVIA_DEFAULT_ACCESS", new_project_general(True),
              note="What everyone signed in gets on a new project its creator leaves at the default."),
    ]


def _runs() -> list[dict[str, Any]]:
    from .workspace import SCHEDULER

    executor = _env("EXUVIA_EXECUTOR_URL")
    return [
        _item("Schedules", "EXUVIA_SCHEDULER",
              "advance here" if SCHEDULER == "internal" else "advance only when POST /api/schedule/tick is called"),
        _item("Where nodes run", "EXUVIA_EXECUTOR_URL", shown_url(executor) if executor else "on this server"),
        _item("Node runs at once", "EXUVIA_MAX_PARALLEL", _env("EXUVIA_MAX_PARALLEL") or "4"),
        _item("Warm runners on a hub", "EXUVIA_HUB_WARM_RUNNERS",
              _env("EXUVIA_HUB_WARM_RUNNERS") or _env("EXUVIA_MAX_PARALLEL") or "4",
              note="Shared by all the hub's projects. A run with none warm starts one, which takes about a second longer."),
        _item("Run timeout", "EXUVIA_RUN_TIMEOUT", f"{_env('EXUVIA_RUN_TIMEOUT') or '1800'} s"),
        _item("Largest bundle for a remote executor", "EXUVIA_BUNDLE_MAX_MB", f"{_env('EXUVIA_BUNDLE_MAX_MB') or '50'} MB"),
        _secret("Failed runs reported to", "EXUVIA_FAILURE_WEBHOOK", "Projects with their own webhook use theirs."),
        _item("Failures reported", "EXUVIA_FAILURE_TRIGGERS", _env("EXUVIA_FAILURE_TRIGGERS") or "schedule, cascade"),
        _url("This server's address", "EXUVIA_PUBLIC_URL", "not set", "For run links in failure reports."),
    ]


def _storage() -> list[dict[str, Any]]:
    from .db import database_url

    db = database_url()
    if not db:
        where = "a SQLite file in each project"
    elif db.startswith("sqlite"):
        where = f"SQLite at {db.split(':///', 1)[-1]}"
    else:
        parts = urlsplit(db)
        where = f"{parts.scheme.split('+')[0]} on {parts.hostname or '?'}{parts.path}"
    store = _env("EXUVIA_ARTIFACT_STORE")
    return [
        _item("Run history, schedules and chats", "EXUVIA_DB", where),
        _item("Its scope prefix", "EXUVIA_DB_SCOPE", _env("EXUVIA_DB_SCOPE") or "none"),
        _item("Node outputs", "EXUVIA_ARTIFACT_STORE", shown_url(store) if store else "on this server's disk"),
        _url("Bucket endpoint", "EXUVIA_S3_ENDPOINT", "AWS"),
        _item("Secrets key", "EXUVIA_SECRETS_KEY",
              "the server's key" if _env("EXUVIA_SECRETS_KEY") or _env("EXUVIA_SECRETS_KEY_FILE") else "a key per project",
              is_set=bool(_env("EXUVIA_SECRETS_KEY") or _env("EXUVIA_SECRETS_KEY_FILE")),
              note="What encrypts the projects', groups' and hub's secrets."),
    ]


def _agent(auth_method: str) -> list[dict[str, Any]]:
    from . import agent_mcp
    from .llm import resolve_endpoint
    from .packages import agent_mode

    try:
        endpoint = resolve_endpoint()
        model = f"{endpoint.provider} · {endpoint.model}" if endpoint else "off: no provider key is set"
    except ValueError as e:
        model = f"misconfigured: {e}"
    return [
        _item("Model", "EXUVIA_PROVIDER", model, is_set=bool(_env("EXUVIA_PROVIDER") or _env("EXUVIA_MODEL"))),
        _item("Effort (Claude)", "EXUVIA_EFFORT", _env("EXUVIA_EFFORT") or "medium"),
        _url("Base URL", "EXUVIA_BASE_URL", "the provider's own"),
        _item("Outside MCP servers", "EXUVIA_AGENT_MCP", "on" if agent_mcp.enabled() else "off"),
        _item("MCP servers on private addresses", "EXUVIA_AGENT_MCP_PRIVATE",
              "allowed" if agent_mcp.private_allowed(auth_method) else "refused"),
        _item("Packages the agent asks for", "EXUVIA_AGENT_PACKAGES",
              {"ask": "wait for a person to approve", "install": "installed", "off": "refused"}.get(agent_mode(), agent_mode())),
    ]


def _packages() -> list[dict[str, Any]]:
    proxy = _env("HTTPS_PROXY") or _env("https_proxy")
    return [
        _url("Package index", "UV_INDEX", "PyPI"),
        _item("Installs at once", "EXUVIA_PACKAGES_CONCURRENCY", _env("EXUVIA_PACKAGES_CONCURRENCY") or "2"),
        _item("Install timeout", "EXUVIA_PACKAGES_TIMEOUT", f"{_env('EXUVIA_PACKAGES_TIMEOUT') or '900'} s"),
        _url("npm packages for views from", "EXUVIA_NPM_CDN", "https://esm.sh"),
        _item("Outbound proxy", "HTTPS_PROXY", shown_url(proxy) if proxy else "none: direct", is_set=bool(proxy)),
        _item("Reached without it", "NO_PROXY", _env("NO_PROXY") or _env("no_proxy") or "—"),
    ]


def settings(auth_method: str) -> list[dict[str, Any]]:
    """The server's setup, in groups of rows (see the module doc)."""
    return [
        {"title": "Sign-in", "items": _sign_in(auth_method)},
        {"title": "Runs", "items": _runs()},
        {"title": "Storage", "items": _storage()},
        {"title": "The agent", "items": _agent(auth_method)},
        {"title": "Packages and network", "items": _packages()},
    ]
