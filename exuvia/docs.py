"""``docs``: exuvia's documentation, for the agent to read.

The ``guide`` (the agent's instructions) says how to build in a project. This
tool answers the rest, such as deploying a hub, auth, package indexes,
executors or the HTTP API, from the README: the same text the docs site and
PyPI show. It reads the README of the exuvia that is running, so the answer
matches the installed version, and it needs no network. That matters on
servers that can't reach the internet, and it means nothing the user asks
leaves the machine.

A wheel carries the README as its long description (``readme`` in
pyproject.toml), and ``importlib.metadata`` reads it from there. A source
checkout (development, the Docker image's /app) reads the file itself.

The README is cut at its ``##`` and ``###`` headings. With no input the tool
returns the table of contents. With ``topic`` it returns one section, or for
a section too long to send at once, its opening and the list of its
subsections. With ``query`` it returns the passages that best match the
words. Sections that are only about working on exuvia itself are left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

SITE = "https://exuvia.lobster.mp/"
OMITTED = {"Contributing", "License"}  # notes for working on exuvia from a clone, and the licence text
MAX_SECTION = 24_000  # characters: a longer section is sent as its opening and a list of subsections
MAX_PASSAGE = 1_500  # characters of one matching paragraph
MAX_MATCHES = 4  # sections a query returns passages from
PASSAGES_PER_MATCH = 3
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "i", "in",
    "is", "it", "me", "my", "of", "on", "or", "the", "this", "that", "to", "we", "what", "when", "where",
    "which", "with", "you", "your",
}
HEADING = re.compile(r"^(#{2,3}) (.+?)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Section:
    title: str
    level: int  # 2 for `##`, 3 for `###`
    parent: str | None  # the `##` a `###` is under
    text: str  # from its heading to the next `##` or `###`

    @property
    def path(self) -> str:
        return f"{self.parent} > {self.title}" if self.parent else self.title


def _checkout_readme() -> str | None:
    root = Path(__file__).resolve().parent.parent
    pyproject, readme = root / "pyproject.toml", root / "README.md"
    if readme.is_file() and pyproject.is_file() and 'name = "exuvia"' in pyproject.read_text(encoding="utf-8"):
        return readme.read_text(encoding="utf-8")
    return None


def _installed_readme() -> str | None:
    try:
        md: Any = metadata.metadata("exuvia")
    except metadata.PackageNotFoundError:
        return None
    body = md.get_payload() if hasattr(md, "get_payload") else None
    return body or md.get("Description")


@lru_cache(maxsize=1)
def readme() -> str:
    """The README of the exuvia that is running ("" if neither copy is there)."""
    return _checkout_readme() or _installed_readme() or ""


def _split(text: str) -> list[Section]:
    """``text`` cut at its ``##``/``###`` headings (not ones inside code
    blocks), without what comes before the first ``##``."""
    out: list[Section] = []
    title: str | None = None
    level, parent, lines = 0, None, []
    in_code = False

    def flush() -> None:
        if title is not None:
            out.append(Section(title, level, parent if level == 3 else None, "\n".join(lines).strip()))

    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            flush()
            level, title, lines = len(m.group(1)), m.group(2), []
            if level == 2:
                parent = title
        lines.append(line)
    flush()
    return [s for s in out if (s.parent or s.title) not in OMITTED]


@lru_cache(maxsize=1)
def sections() -> tuple[Section, ...]:
    return tuple(_split(readme()))


def _children(top: Section) -> list[Section]:
    return [s for s in sections() if s.parent == top.title]


def _full(top: Section) -> str:
    return "\n\n".join([top.text, *(c.text for c in _children(top))])


def _paragraphs(text: str) -> list[str]:
    """Blocks separated by blank lines, a fenced code block kept whole."""
    out, cur, in_code = [], [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        if not line.strip() and not in_code:
            if cur:
                out.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def _plain(md: str) -> str:
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)  # [text](link) -> text
    return " ".join(md.replace("**", "").split())


NOT_PROSE = re.compile(r"\s*(```|~~~|\||<|#|>|!\[|([-*+]|\d+\.)\s)")
LEAD_IN = re.compile(r"\s*(?:[-*+]\s+)?\*\*(.+?)\*\*")  # a paragraph or item that opens with **A title.**
SENTENCE_END = re.compile(r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?])\s")
LIST_ITEM = re.compile(r"\s*([-*+]|\d+\.)\s")


def _lead(para: str) -> str:
    """The bold title a paragraph or list item opens with ("" if none)."""
    m = LEAD_IN.match(para)
    return m.group(1).strip().rstrip(".:,") if m else ""


def _clip(text: str, n: int = 200) -> str:
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def summary(section: Section) -> str:
    """One line on a section: the first sentence of its first paragraph of
    prose (else of its first list or quote), or, for a section made of
    paragraphs that open with a bold title, those titles."""
    paras = [p for p in _paragraphs(section.text)[1:] if not FENCE.match(p)]
    prose = [p for p in paras if not NOT_PROSE.match(p)]
    first = (prose or paras or [""])[0]
    leads = [lead for p in paras if (lead := _lead(p))] + [
        lead for p in paras for line in p.splitlines()[1:] if LIST_ITEM.match(line) and (lead := _lead(line))
    ]
    if LEAD_IN.match(first) and len(leads) > 1 or not prose and len(leads) > 1:
        return _clip("Covers: " + "; ".join(_plain(x) for x in leads) + ".")
    sentences = SENTENCE_END.split(_plain(re.sub(r"^\s*([-*+>]|\d+\.)\s+", "", first)))
    line = sentences[0]
    if len(line) < 50 and len(sentences) > 1:  # "Prototype." says little alone
        line += " " + sentences[1]
    return _clip(line)


def _size(text: str) -> str:
    tokens = len(text) / 4
    return f"~{tokens / 1000:.1f}k tokens" if tokens >= 1000 else f"~{round(tokens, -2) or 100:.0f} tokens"


def _version() -> str:
    try:
        return metadata.version("exuvia")
    except metadata.PackageNotFoundError:
        return "(version unknown)"


def contents() -> str:
    intro = (
        f"exuvia {_version()} documentation: the README of this install, by section. Call `docs` with `topic` "
        "set to a heading below to read that section, or with `query` to search every section."
    )
    lines = [intro, ""]
    for s in sections():
        if s.level == 2:
            lines.append(f"- {s.title} ({_size(_full(s))}): {summary(s)}")
        else:
            lines.append(f"  - {s.title} ({_size(s.text)}): {summary(s)}")
    lines += ["", f"The same pages are online at {SITE} (for the latest release; this tool answers for the installed one)."]
    return "\n".join(lines)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find(topic: str) -> tuple[Section | None, list[Section]]:
    """The section ``topic`` names: by heading, ``Parent > Heading`` or a
    README anchor (``#kerberos``), case and punctuation aside. Else the
    sections whose heading contains it, when that is not exactly one."""
    key = _key(topic.split(">")[-1])
    parent = _key(topic.split(">")[0]) if ">" in topic else None
    if not key:
        return None, []
    secs = sections()
    exact = [s for s in secs if _key(s.title) == key and (parent is None or _key(s.parent or "") == parent)]
    if exact:
        return min(exact, key=lambda s: s.level), []
    near = [s for s in secs if key in _key(s.title)]
    if len(near) == 1:
        return near[0], []
    return None, near


def section_text(s: Section) -> str:
    if s.level == 3:
        return s.text if len(s.text) <= MAX_SECTION else _truncated(s.text)
    full = _full(s)
    if len(full) <= MAX_SECTION:
        return full
    kids = _children(s)
    listing = "\n".join(f"- {c.title} ({_size(c.text)}): {summary(c)}" for c in kids)
    return (
        f"{_truncated(s.text) if len(s.text) > MAX_SECTION else s.text}\n\n"
        f"(This section is {_size(full)} in all. Its subsections, each one `topic` for `docs`:)\n{listing}"
    )


def _truncated(text: str) -> str:
    cut = text.rfind("\n\n", 0, MAX_SECTION)
    return text[: cut if cut > 0 else MAX_SECTION] + "\n\n(Cut short here: search with `query` for the rest.)"


def _terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+", query.lower())
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS and len(w) > 1))


def search(query: str) -> str | None:
    """Passages that match ``query``'s words, from the best few sections
    (None: no words to search for)."""
    terms = _terms(query)
    if not terms:
        return None
    patterns = [re.compile(r"\b" + re.escape(t)) for t in terms]

    def hits(text: str) -> int:
        low = text.lower()
        return sum(1 for p in patterns if p.search(low))

    ranked = []
    for i, s in enumerate(sections()):
        # A paragraph's bold title counts again, as a small heading; the
        # section's heading counts once for all of it.
        paras = _paragraphs(s.text)[1:]
        scored = [(hits(p) + hits(_lead(p)), j, p) for j, p in enumerate(paras)]
        best = max((h for h, _, _ in scored), default=0)
        title = hits(s.path)
        if not best and not title:
            continue
        count = sum(len(p.findall(s.text.lower())) for p in patterns)
        ranked.append(((best + title, count), i, s, scored))
    if not ranked:
        return f"Nothing in the documentation matches {query!r}. Call `docs` with no input for the table of contents."
    ranked.sort(key=lambda r: (r[0], -r[1]), reverse=True)

    out = [f"Passages matching {query!r}, best first. Call `docs` with a section's heading as `topic` to read all of it."]
    for _, _, s, scored in ranked[:MAX_MATCHES]:
        top = sorted((x for x in scored if x[0]), key=lambda x: (-x[0], x[1]))[:PASSAGES_PER_MATCH]
        passages = [p if len(p) <= MAX_PASSAGE else p[:MAX_PASSAGE].rstrip() + " ..." for _, _, p in sorted(top, key=lambda x: x[1])]
        if not passages:  # matched on the heading alone
            passages = _paragraphs(s.text)[1:2]
        out.append(f"## {s.path}\n\n" + "\n\n".join(passages))
    rest = [r[2].path for r in ranked[MAX_MATCHES : MAX_MATCHES + 5]]
    if rest:
        out.append("Also mentioned in: " + "; ".join(rest))
    return "\n\n".join(out)


def tool(args: dict[str, Any]) -> tuple[bool, str]:
    """The ``docs`` tool: ``(ok, text)``."""
    topic, query = args.get("topic"), args.get("query")
    if any(v is not None and not isinstance(v, str) for v in (topic, query)):
        return False, "INVALID_INPUT: `topic` and `query` are strings"
    extra = set(args) - {"topic", "query"}
    if extra:
        return False, f"INVALID_INPUT: unexpected field(s): {', '.join(sorted(extra))}"
    topic, query = (topic or "").strip(), (query or "").strip()
    if not sections():
        return False, f"This install of exuvia has no copy of its documentation. It is online at {SITE}"
    if topic and query:
        return False, "INVALID_INPUT: give `topic` or `query`, not both"
    if query:
        found = search(query)
        return (True, found) if found else (False, "INVALID_INPUT: `query` needs words to search for")
    if topic:
        s, near = find(topic)
        if s:
            return True, section_text(s)
        if near:
            return False, f"{topic!r} matches more than one section: " + "; ".join(n.path for n in near)
        return False, f"No section named {topic!r}. Call `docs` with no input for the table of contents, or search with `query`."
    return True, contents()
