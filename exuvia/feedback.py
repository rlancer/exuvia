"""``report_issue``: how an agent tells the exuvia maintainers something is wrong.

Nothing is sent from here. The tool turns what the agent noticed into a draft
GitHub issue and a link that opens it prefilled, so the person the agent works
for reads it and presses Submit (or lets the agent file it with ``gh``). That
keeps a public post a decision the user makes, and needs no credentials.
"""

from __future__ import annotations

import platform
from importlib import metadata
from typing import Any
from urllib.parse import quote, urlencode

REPO = "rlancer/exuvia"
ISSUES_URL = f"https://github.com/{REPO}/issues"
KINDS = {"bug": ["bug"], "idea": ["enhancement"], "feedback": []}
MAX_URL = 7000  # GitHub refuses a request line much past 8 KB


def _version() -> str:
    try:
        return metadata.version("exuvia")
    except metadata.PackageNotFoundError:
        return "unknown (not installed as a package)"


def draft(title: str, details: str, kind: str = "bug", origin: str = "agent") -> dict[str, Any]:
    """The issue for ``title`` and ``details``: its text, labels, the prefilled
    new-issue link, a link to search for duplicates, and what the agent does next."""
    title = " ".join(title.split())[:200]
    details = details.strip()
    labels = KINDS.get(kind, [])
    footer = (
        "\n\n---\n"
        "Filed through exuvia's `report_issue` tool.\n"
        f"- exuvia: {_version()}\n"
        f"- Python: {platform.python_version()}\n"
        f"- OS: {platform.platform()}\n"
        f"- Reported via: {origin}\n"
    )
    body = details + footer

    def link(text: str) -> str:
        query = {"title": title, "body": text}
        if labels:
            query["labels"] = ",".join(labels)
        return f"{ISSUES_URL}/new?{urlencode(query, quote_via=quote)}"

    url, shortened = link(body), False
    if len(url) > MAX_URL:  # the link carries what fits; the draft below stays whole
        shortened = True
        note = "\n\n_(Shortened to fit a link. The agent has the full text.)_"
        keep = len(details)
        while keep > 200 and len(link(details[:keep] + note + footer)) > MAX_URL:
            keep = int(keep * 0.8)
        url = link(details[:keep] + note + footer)

    search = f"{ISSUES_URL}?{urlencode({'q': f'is:issue {title}'}, quote_via=quote)}"
    next_step = (
        "NOTHING HAS BEEN SENT. Show the user the title and body and ask whether to file it. "
        "Leave out anything private: API keys and tokens, file paths, and the contents of their data. "
        "Check `search_url` for an existing issue first and add to it if there is one. "
        "To file it, either give the user `url` (it opens a new issue with this text already filled in; "
        "they press Submit), or, if the `gh` CLI is installed and signed in and the user agrees, run "
        f"`gh issue create --repo {REPO} --title <title> --body-file <file holding body>"
        + (f" --label {','.join(labels)}" if labels else "")
        + "`."
    )
    if shortened:
        next_step += " The link's text is shortened, so prefer `gh` (which sends the whole body) when it is available."
    return {
        "title": title,
        "body": body,
        "labels": labels,
        "url": url,
        "search_url": search,
        "repo": REPO,
        "next": next_step,
    }
