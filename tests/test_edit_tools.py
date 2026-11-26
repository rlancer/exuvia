"""edit_node and edit_file: change part of a file by exact string replacement."""

import json

from exuvia.agent import TOOLS, Agent, apply_edit, edit_diff
from exuvia.workspace import Workspace
from test_helpers import call

NODE = '''import exuvia

AGREE_BP = 3.0


@exuvia.node()
def synth(prices):
    """Synthetic index."""
    return prices.assign(bp=AGREE_BP)
'''

PRICES = "import exuvia\n\n\n@exuvia.node()\ndef prices():\n    return {'x': 1}\n"


def project(tmp_path):
    ws = Workspace(tmp_path)
    ws.project.write_node("prices", PRICES, "python")
    ws.project.write_node("synth", NODE, "python")
    ws.reload()
    return Agent(ws)


def test_apply_edit_needs_one_exact_match():
    assert apply_edit("a b a", {"old_string": "b", "new_string": "c"}) == ("a c a", 1, None)
    same, n, error = apply_edit("a b a", {"old_string": "a", "new_string": "c"})
    assert same == "a b a" and n == 0 and "appears 2 times" in error
    assert apply_edit("a b a", {"old_string": "a", "new_string": "c", "replace_all": True}) == ("c b c", 2, None)
    assert "not in the file" in apply_edit("a", {"old_string": "z", "new_string": "c"})[2]
    assert "empty" in apply_edit("a", {"old_string": "", "new_string": "c"})[2]
    assert "the same" in apply_edit("a", {"old_string": "a", "new_string": "a"})[2]


def test_a_last_line_without_a_newline_keeps_a_line_of_its_own_in_the_diff():
    diff = edit_diff("select 1 as a", "select 2 as a", "nodes/one.sql")
    assert diff.endswith(
        "-select 1 as a\n\\ No newline at end of file\n+select 2 as a\n\\ No newline at end of file\n"
    )
    assert "\\ No newline" not in edit_diff("a\nb\n", "a\nc\n", "x")


def test_edit_node_changes_one_line_and_returns_the_diff(tmp_path):
    agent = project(tmp_path)
    ok, text, extra = call(agent, "edit_node", {"name": "synth", "old_string": "AGREE_BP = 3.0", "new_string": "AGREE_BP = 5.0"})
    assert ok and extra == {"node": "synth"}
    out = json.loads(text)
    assert out["edited"] == "synth" and out["replacements"] == 1 and out["deps"] == ["prices"]
    assert "-AGREE_BP = 3.0\n+AGREE_BP = 5.0\n" in out["diff"] and "a/nodes/synth.py" in out["diff"]
    assert agent.ws.project.source("synth") == NODE.replace("3.0", "5.0")
    # Saved like any other write: a revision by the agent.
    assert agent.ws.code_revisions("synth")[0]["current"]


def test_edit_node_refuses_what_it_cannot_place(tmp_path):
    agent = project(tmp_path)
    ok, text, _ = call(agent, "edit_node", {"name": "synth", "old_string": "AGREE_BP = 4.0", "new_string": "x"})
    assert not ok and text.startswith("synth not changed: `old_string` is not in the file")
    ok, text, _ = call(agent, "edit_node", {"name": "synth", "old_string": "AGREE_BP", "new_string": "BP"})
    assert not ok and "appears 2 times" in text
    assert agent.ws.project.source("synth") == NODE
    ok, text, _ = call(agent, "edit_node", {"name": "synth", "old_string": "AGREE_BP", "new_string": "BP", "replace_all": True})
    assert ok and json.loads(text)["replacements"] == 2
    ok, text, _ = call(agent, "edit_node", {"name": "nope", "old_string": "a", "new_string": "b"})
    assert not ok and text == "no node named nope"


def test_edit_node_reports_a_parse_error_with_the_diff(tmp_path):
    agent = project(tmp_path)
    ok, text, _ = call(agent, "edit_node", {"name": "synth", "old_string": "def synth(prices):", "new_string": "def synth(prices:"})
    assert not ok and "does not parse" in text and "+def synth(prices:" in text


def test_edit_node_on_a_sql_node_keeps_it_sql(tmp_path):
    agent = project(tmp_path)
    agent.ws.project.write_node("total", "-- Total.\nselect sum(x) as x from prices\n", "sql")
    ok, text, _ = call(agent, "edit_node", {"name": "total", "old_string": "sum(x)", "new_string": "max(x)"})
    assert ok and json.loads(text)["deps"] == ["prices"]
    assert agent.ws.graph.nodes["total"].kind == "sql"


def test_edit_file_changes_a_helper(tmp_path):
    agent = project(tmp_path)
    call(agent, "write_file", {"path": "lib/stats.py", "source": "TOL = 0.0025\n\n\ndef tol():\n    return TOL\n"})
    ok, text, extra = call(agent, "edit_file", {"path": "lib/stats.py", "old_string": "0.0025", "new_string": "0.005"})
    assert ok and extra == {"file": "lib/stats.py"}
    out = json.loads(text)
    assert out["replacements"] == 1 and "+TOL = 0.005" in out["diff"] and out["path"] == "lib/stats.py"
    ok, text, _ = call(agent, "edit_file", {"path": "lib/stats.py", "old_string": "nope", "new_string": "x"})
    assert not ok and "lib/stats.py not changed" in text
    ok, text, _ = call(agent, "edit_file", {"path": "lib/none.py", "old_string": "a", "new_string": "b"})
    assert not ok and text == "no file lib/none.py"


def test_edit_tools_are_listed_and_need_an_editor():
    from exuvia.agent import READ_TOOLS

    names = {t["name"] for t in TOOLS}
    assert {"edit_node", "edit_file"} <= names
    assert not {"edit_node", "edit_file"} & READ_TOOLS
