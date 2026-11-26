"""The `docs` tool: exuvia's README, by section, for the agent to read."""

import asyncio
import re
from pathlib import Path

import pytest

from exuvia import docs
from exuvia.agent import SYSTEM, Agent
from exuvia.workspace import Workspace

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def headings(level: str) -> list[str]:
    return re.findall(rf"^{level} (.+?)\s*$", README, flags=re.MULTILINE)


def test_contents_lists_every_section_but_the_contributor_notes():
    ok, text = docs.tool({})
    assert ok
    for title in headings("##"):
        if title in docs.OMITTED:
            assert f"- {title} (" not in text
        else:
            assert f"- {title} (" in text, title
    assert "  - Kerberos (" in text and "  - Releasing (" not in text  # Releasing is under Contributing
    assert re.search(r"- Deploying \(~[\d.]+k tokens\): \S", text)  # sizes and a summary


def test_a_topic_by_heading_anchor_or_path():
    for topic in ("Kerberos", "kerberos", "#kerberos", "Deploying > Kerberos", "deploying>KERBEROS"):
        ok, text = docs.tool({"topic": topic})
        assert ok and text.startswith("### Kerberos\n"), topic
    ok, text = docs.tool({"topic": "mcp-the-agents-tools-in-your-own-agent"})
    assert ok and text.startswith("## MCP: the agent's tools in your own agent")
    # A `##` and its `###` subsections come back together when they fit.
    ok, text = docs.tool({"topic": "Views"})
    assert ok and text.startswith("## Views\n") and "### Rendering a view" in text


def test_a_long_section_lists_its_subsections():
    ok, text = docs.tool({"topic": "Deploying"})
    full = docs._full(docs.find("Deploying")[0])
    assert len(full) > docs.MAX_SECTION  # else this test checks nothing
    assert ok and text.startswith("## Deploying\n") and len(text) < docs.MAX_SECTION
    assert "### " not in text and "- Kerberos (" in text and "- Schedules, auth and settings (" in text


def test_unknown_and_ambiguous_topics_say_what_to_do():
    ok, text = docs.tool({"topic": "nope"})
    assert not ok and "table of contents" in text
    ok, text = docs.tool({"topic": "Packages"})  # an exact heading wins over the ones that contain it
    assert ok and text.startswith("## Packages\n")
    ok, text = docs.tool({"topic": "pack"})
    assert not ok and "more than one section" in text and "Deploying > Packages your nodes import" in text


def test_a_query_returns_the_best_passages_first():
    ok, text = docs.tool({"query": "kerberos keytab"})
    assert ok
    first = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)[0]
    assert first == "Deploying > Kerberos" and "keytab" in text
    ok, text = docs.tool({"query": "agent tokens"})
    assert ok and re.findall(r"^## (.+)$", text, flags=re.MULTILINE)[0] == "Deploying > Schedules, auth and settings"
    ok, text = docs.tool({"query": "postgres"})
    assert ok and re.findall(r"^## (.+)$", text, flags=re.MULTILINE)[0].startswith("Records")
    assert len(text) < 4 * docs.MAX_SECTION

    ok, text = docs.tool({"query": "zzzqqq"})
    assert ok and "Nothing in the documentation matches" in text


def test_bad_input_comes_back_to_the_model():
    assert docs.tool({"query": "the of"}) == (False, "INVALID_INPUT: `query` needs words to search for")
    ok, text = docs.tool({"topic": "Views", "query": "react"})
    assert not ok and "not both" in text
    ok, text = docs.tool({"topic": 3})
    assert not ok and "strings" in text
    ok, text = docs.tool({"page": "x"})
    assert not ok and "unexpected" in text


def test_headings_inside_code_blocks_are_not_sections():
    text = "## One\n\ntext\n\n```bash\n## not a heading\n```\n\n### Two\n\nmore\n\n## Contributing\n\nhidden\n"
    secs = docs._split(text)
    assert [(s.title, s.level, s.parent) for s in secs] == [("One", 2, None), ("Two", 3, "One")]
    assert "## not a heading" in secs[0].text


def test_an_installed_copy_reads_the_readme_from_the_package_metadata(monkeypatch):
    """A wheel has no README.md file: its METADATA carries it, and that is
    what the tool reads outside a source checkout."""
    installed = docs._installed_readme()
    if installed is None:
        pytest.skip("exuvia is not installed as a package here")
    assert "## Deploying" in installed and "### Kerberos" in installed
    monkeypatch.setattr(docs, "_checkout_readme", lambda: None)
    docs.readme.cache_clear(), docs.sections.cache_clear()
    try:
        assert docs.find("Kerberos")[0] is not None
    finally:
        docs.readme.cache_clear(), docs.sections.cache_clear()


def test_the_agent_has_the_tool_and_is_told_to_use_it(tmp_path):
    agent = Agent(Workspace(tmp_path))
    ok, text, _ = asyncio.run(agent.call_tool("docs", {"topic": "Hub"}))
    assert ok and text.startswith("## Hub: many projects from one process")
    ok, text, _ = asyncio.run(agent.call_tool("docs", {"topic": 1}))
    assert not ok and text.startswith("INVALID_INPUT")
    assert "read `docs` before you answer" in SYSTEM


def test_the_agent_is_pointed_at_exuvia_mirror_and_finds_it(tmp_path):
    # Nothing in a project's tools makes a git copy of its history, so the
    # guide and the tools that touch history name the command, and the
    # words a user would use find its section.
    from exuvia.agent import TOOLS

    assert "`exuvia mirror`" in SYSTEM and "topic `The history in git`" in SYSTEM
    described = {t["name"]: t["description"] for t in TOOLS}
    assert "exuvia mirror" in described["docs"] and "exuvia mirror" in described["list_revisions"]
    section, _ = docs.find("The history in git")
    assert section is not None and "exuvia mirror" in section.text
    for words in ("back up code to github", "git history", "push code to a github repository"):
        best = re.search(r"^## (.+)$", docs.search(words) or "", re.M)
        assert best and "The history in git" in best.group(1), (words, best and best.group(1))
