"""Failure webhook: POST a small JSON message when a run fails.

Configured per project in ``exuvia.json``::

    "failure_webhook": "https://hooks.slack.com/services/..."
    "failure_webhook": {"url": "https://...", "triggers": "all"}

or for a whole host with ``EXUVIA_FAILURE_WEBHOOK`` (and optionally
``EXUVIA_FAILURE_TRIGGERS``). A project setting wins; ``"failure_webhook":
false`` turns the host's webhook off for one project.

By default only scheduled and cascade failures are sent: a manual run has
someone watching it. Delivery is one POST on a short-lived thread with a short
timeout, so it never holds up the run; a failed delivery is printed to stderr
and dropped (no retries).
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any

import httpx

DEFAULT_TRIGGERS = frozenset({"schedule", "cascade"})
TIMEOUT = 5.0
MAX_ERROR = 2000  # characters of the error kept in the payload

# Tests pin a mock transport here (see tests/test_notify.py).
_transport: httpx.BaseTransport | None = None
_threads: set[threading.Thread] = set()
_lock = threading.Lock()


def _triggers(value: Any) -> frozenset[str] | None:
    """None means every trigger."""
    if value is None or value == "":
        return DEFAULT_TRIGGERS
    if isinstance(value, str):
        value = [t.strip() for t in value.split(",")]
    names = frozenset(str(t).strip() for t in value if str(t).strip())
    return None if "all" in names else names or DEFAULT_TRIGGERS


def failure_webhook(config: dict[str, Any]) -> tuple[str, frozenset[str] | None] | None:
    """(url, triggers) for a project config, falling back to the environment.
    None when no webhook is configured (or the project turned it off)."""
    if "failure_webhook" in config:
        raw = config["failure_webhook"]
        if not raw:
            return None
        if isinstance(raw, str):
            return raw, DEFAULT_TRIGGERS
        if isinstance(raw, dict) and raw.get("url"):
            return str(raw["url"]), _triggers(raw.get("triggers"))
        return None
    url = os.environ.get("EXUVIA_FAILURE_WEBHOOK", "").strip()
    if not url:
        return None
    return url, _triggers(os.environ.get("EXUVIA_FAILURE_TRIGGERS"))


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def payload(project: str, run: dict[str, Any], url: str | None = None) -> dict[str, Any]:
    """The JSON body. ``text`` makes it a valid Slack (or Discord ``/slack``)
    incoming-webhook message as it is; the other fields are for receivers
    that want structure."""
    error = (run.get("error") or "").strip()
    if len(error) > MAX_ERROR:
        error = error[: MAX_ERROR - 1] + "…"
    first = error.splitlines()[-1] if error else "no error message"  # tracebacks end with the exception
    text = f"exuvia: {project} / {run['node']} failed ({run.get('trigger')}): {first[:300]}"
    if url:
        text += f"\n{url}"
    return {
        "text": text,
        "event": "run.failed",
        "project": project,
        "node": run["node"],
        "run_id": run["id"],
        "trigger": run.get("trigger"),
        "status": run.get("status"),
        "error": error,
        "params": run.get("params") or {},
        "queued_at": _iso(run.get("queued_at")),
        "finished_at": _iso(run.get("finished_at")),
        "url": url,
    }


def _post(url: str, body: dict[str, Any]) -> None:
    try:
        with httpx.Client(transport=_transport, timeout=TIMEOUT) as client:
            resp = client.post(url, json=body)
        if resp.status_code >= 400:
            print(
                f"exuvia: failure webhook for {body['node']} answered {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001 - a webhook that fails must never fail the run it reports
        print(f"exuvia: could not deliver failure webhook for {body['node']}: {e}", file=sys.stderr)
    finally:
        with _lock:
            _threads.discard(threading.current_thread())


def send(url: str, body: dict[str, Any]) -> None:
    """POST in the background. Not a daemon thread, so a CLI run that fails
    still delivers before the process exits (waiting at most TIMEOUT)."""
    t = threading.Thread(target=_post, args=(url, body), name="exuvia-webhook")
    with _lock:
        _threads.add(t)
    t.start()


def wait(timeout: float = TIMEOUT + 1) -> None:
    """Block until deliveries in flight finish (tests)."""
    with _lock:
        threads = list(_threads)
    for t in threads:
        t.join(timeout)
