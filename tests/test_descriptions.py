"""Rewriting a node's description (a docstring, a SQL node's leading `--`
lines) leaves the node fresh: freshness compares the code without them."""

import textwrap

from exuvia.project import logic_text, python_logic
from exuvia.workspace import Workspace
from test_engine import run, write

PY = '''
"""{module_doc}"""
import exuvia


@exuvia.node()
def a():
    r"""{doc}"""
    import pandas as pd
    return pd.DataFrame({{"x": [{x}]}})
'''

SQL = """
-- {doc}
-- schedule: {cron}
--
-- More about it.
select * from a where x > {x}
"""


def py_node(root, doc="Rows.", module_doc="Module.", x=1):
    write(root, "a.py", PY.format(doc=doc, module_doc=module_doc, x=x))


def sql_node(root, doc="Filtered.", cron="0 17 * * 1-5", x=0):
    write(root, "b.sql", SQL.format(doc=doc, cron=cron, x=x))


def test_python_logic_drops_docstrings_and_keeps_code():
    src = textwrap.dedent(
        '''
        """Module doc."""

        import os


        class K:
            """Class doc."""

            n = 1


        async def f(x):
            r"""$x^2$, squared.

            - a list item
            """

            # a comment stays
            return x  # so does this


        def g(): """One-line."""; return "keep this string"
        '''
    ).lstrip()
    out = python_logic(src)
    assert not any(doc in out for doc in ("Module doc", "Class doc", "squared", "list item", "One-line"))
    assert "import os" in out and "# a comment stays" in out and '"keep this string"' in out
    assert out.startswith("import os\n")
    # No docstring at all hashes the same as a docstring (and the blank lines after it).
    bare = src.replace('    """Class doc."""\n\n', "")
    assert python_logic(bare) == python_logic(src)
    assert python_logic(src.replace("\n", "\r\n")) == out
    assert python_logic("def (:\n") == "def (:\n"  # doesn't parse: as written


def test_sql_logic_keeps_settings_and_the_query():
    text = "-- Doc.\n-- schedule: 0 1 * * *\n--\n-- note: not a setting\n-- tags:\n\nselect 1 -- trailing\n"
    assert logic_text("sql", text) == "-- schedule: 0 1 * * *\nselect 1 -- trailing\n"


def test_editing_a_python_docstring_keeps_the_node_fresh(tmp_path):
    py_node(tmp_path)
    ws = Workspace(tmp_path)
    run(ws, ["a"])
    before = ws.graph.nodes["a"].code_hash

    py_node(tmp_path, doc="Rows, with $x \\geq 1$.\n\n    - one\n    - two", module_doc="")
    ws.reload()
    assert ws.graph.nodes["a"].code_hash != before
    assert ws.graph.nodes["a"].description.startswith("Rows, with $x")
    assert ws.status()["a"]["state"] == "fresh"
    assert len([r for r in ws.code_revisions("a") if r["hash"]]) == 2  # the edit is still a revision

    py_node(tmp_path, doc="Rows.", x=2)
    ws.reload()
    assert ws.status()["a"]["state"] == "stale" and ws.status()["a"]["reason"] == "code changed"


def test_editing_a_sql_description_keeps_the_node_fresh(tmp_path):
    py_node(tmp_path)
    sql_node(tmp_path)
    ws = Workspace(tmp_path)
    run(ws, ["b"])
    assert ws.status()["b"]["state"] == "fresh"

    sql_node(tmp_path, doc="Rows of `a` above **zero**.\n--\n-- | col | meaning |\n-- |---|---|\n-- | x | value |")
    ws.reload()
    assert "| col | meaning |" in ws.graph.nodes["b"].description
    assert ws.status()["b"]["state"] == "fresh"

    sql_node(tmp_path, cron="0 18 * * 1-5")  # a setting is not description
    ws.reload()
    assert ws.status()["b"]["reason"] == "code changed"


def test_editing_a_helper_docstring_keeps_its_readers_fresh(tmp_path):
    write(
        tmp_path,
        "scaled.py",
        """
        import exuvia


        @exuvia.node()
        def scaled():
            from lib.stats import factor
            return {"f": factor()}
        """,
    )
    ws = Workspace(tmp_path)
    ws.write_helper("lib/stats.py", "def factor():\n    return 2\n")
    run(ws, ["scaled"])
    ws.write_helper("lib/stats.py", '"""Stats."""\n\n\ndef factor():\n    """The factor, $k$."""\n    return 2\n')
    assert ws.status()["scaled"]["state"] == "fresh"
    ws.write_helper("lib/stats.py", "def factor():\n    return 3\n")
    assert ws.status()["scaled"]["reason"] == "code changed"


def test_runs_from_before_logic_hash_compare_the_whole_text(tmp_path):
    py_node(tmp_path)
    ws = Workspace(tmp_path)
    finals = run(ws, ["a"])
    ws.store.update_run(finals["a"]["id"], logic_hash=None)
    assert ws.status()["a"]["state"] == "fresh"
    py_node(tmp_path, doc="Rewritten.")
    ws.reload()
    assert ws.status()["a"]["reason"] == "code changed"
