"""Raw GitHub links in the docs point at files that exist.

The deploy prompt and the deploy/ headers hand people (and their agents)
raw.githubusercontent.com URLs to download. Those URLs serve files only: a
directory, or a file that was moved, is a 404 on someone else's machine.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RAW = re.compile(r"https://raw\.githubusercontent\.com/rlancer/exuvia/main/([^\s'\"`)<>]*)")


def doc_files() -> list[Path]:
    files = [REPO / "README.md", REPO / "AGENTS.md", *sorted((REPO / "deploy").iterdir())]
    site = REPO / "site" / "src"
    if site.is_dir():
        files += [p for p in sorted(site.rglob("*")) if p.is_file()]
    return [p for p in files if p.is_file()]


def test_raw_links_name_files_in_the_repo() -> None:
    found, broken = 0, []
    for doc in doc_files():
        for path in RAW.findall(doc.read_text(encoding="utf-8", errors="replace")):
            found += 1
            path = path.rstrip(".,;:")
            if not path or path.endswith("/") or not (REPO / path).is_file():
                broken.append(f"{doc.relative_to(REPO)}: {path or '(repo root)'}")
    assert found, "expected raw links in the docs"
    assert not broken, "raw.githubusercontent.com serves files only:\n" + "\n".join(broken)
