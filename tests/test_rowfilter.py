"""A table's filter from words: the model writes the SQL condition, and it is
checked against the table before it is used (exuvia/rowfilter.py,
``POST /api/nodes/{name}/filter``)."""

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exuvia import llm, rowfilter
from exuvia.server import _needed_role, create_app

ORDERS = '''
    import exuvia

    @exuvia.node()
    def orders():
        import pandas as pd
        n = 30
        return pd.DataFrame({
            "id": range(n),
            "amount": [k * 10.0 for k in range(n)],
            "country": [("DE", "US", "it's")[k % 3] for k in range(n)],
            "customer": [f"customer {k}" for k in range(n)],
            "note": ["x" * 200] * n,
        })
'''
FIT = '''
    import exuvia

    @exuvia.node()
    def fit():
        import pandas as pd
        return {"coefs": pd.DataFrame({"term": ["a", "b"], "value": [0.5, 1.0]}), "metrics": {"r2": 0.9}}
'''
NOTE = '''
    import exuvia

    @exuvia.node()
    def note():
        return {"summary": "hello"}
'''


def write(root: Path, name: str, body: str) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def project(root: Path) -> Path:
    for name, body in (("orders.py", ORDERS), ("fit.py", FIT), ("note.py", NOTE)):
        write(root, name, body)
    return root


def ran(root: Path) -> TestClient:
    c = TestClient(create_app(root))
    c.__enter__()
    done = c.post("/api/run", json={"wait": 120, "force": True}).json()
    assert done["ok"] and not done["running"], done
    return c


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """A project whose agent has a model (no call is made to it: ``model`` answers)."""
    env = pytest.MonkeyPatch()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY", "OPEN_ROUTER_KEY", "PORTKEY_API_KEY"):
        env.delenv(name, raising=False)
    env.setenv("EXUVIA_PROVIDER", "openai")
    env.setenv("EXUVIA_BASE_URL", "http://llm.internal/v1")
    env.setenv("EXUVIA_MODEL", "some/model")
    env.setenv("OPENAI_API_KEY", "attached-by-gateway")
    c = ran(project(tmp_path_factory.mktemp("rowfilter")))
    yield c
    c.__exit__(None, None, None)
    env.undo()


@pytest.fixture
def model(monkeypatch):
    """The model: answers with ``answers`` in turn, and keeps what it was asked."""
    asked: list[dict] = []
    answers: list[str] = []

    def complete(prompt, *, system=None, max_tokens=4000, timeout=None):
        asked.append({"prompt": prompt, "system": system, "timeout": timeout})
        if not answers:
            raise AssertionError("the model was asked more often than expected")
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(llm, "complete", complete)
    complete.asked, complete.answers = asked, answers
    return complete


def test_parse_takes_the_condition_out_of_the_answer():
    assert rowfilter.parse(' "amount" > 100 ') == '"amount" > 100'
    assert rowfilter.parse('```sql\n"amount" > 100;\n```') == '"amount" > 100'
    assert rowfilter.parse('WHERE "amount" > 100;') == '"amount" > 100'
    assert rowfilter.parse('`"a" = 1`') == '"a" = 1'
    # A column whose name starts with "where" is not the keyword.
    assert rowfilter.parse('"whereabouts" = 1') == '"whereabouts" = 1'
    with pytest.raises(rowfilter.FilterError, match="not a filter on this table's rows: it asks for a total"):
        rowfilter.parse("CANNOT: it asks for\na total.")
    with pytest.raises(rowfilter.FilterError, match="no condition"):
        rowfilter.parse("```sql\n```")


def test_table_names_are_quoted():
    assert rowfilter.table_of("orders") == '"orders"'
    assert rowfilter.table_of("fit", "coefs") == '"fit"."coefs"'
    assert rowfilter.table_of('a"b') == '"a""b"'


def test_the_model_is_shown_the_table_and_its_condition_is_returned(client, model):
    model.answers.append('"amount" > 100 and "country" = \'DE\'')
    res = client.post("/api/nodes/orders/filter", json={"prompt": "  German orders over 100  "})
    assert res.status_code == 200, res.text
    where = res.json()["where"]
    assert where == '"amount" > 100 and "country" = \'DE\''
    # What comes back filters the table as the Data tab applies it.
    rows = client.post("/api/query", json={"sql": f'select * from "orders" where ({where})'}).json()["rows"]
    assert [r[0] for r in rows] == [12, 15, 18, 21, 24, 27]

    (ask,) = model.asked
    assert ask["system"] == rowfilter.SYSTEM and ask["timeout"] == rowfilter.ANSWER_SECONDS
    prompt = ask["prompt"]
    assert prompt.startswith('Table: "orders"\nColumns:\n- "id" BIGINT\n- "amount" DOUBLE\n')
    # A text column with few values has them listed (quoted as SQL); one with many is left to the rows.
    assert "- \"country\" VARCHAR (values: 'DE', 'US', 'it''s')" in prompt
    assert '- "customer" VARCHAR\n' in prompt
    assert prompt.count('{"id": ') == rowfilter.SAMPLE_ROWS and '"customer": "customer 0"' in prompt
    # A long value is cut.
    assert "x" * rowfilter.VALUE_CHARS + "…" in prompt and "x" * (rowfilter.VALUE_CHARS + 1) not in prompt
    assert prompt.endswith("\n\nThe rows wanted: German orders over 100")


def test_a_condition_duckdb_refuses_goes_back_to_the_model_once(client, model):
    model.answers += ['"total" > 100', '"amount" > 100']
    res = client.post("/api/nodes/orders/filter", json={"prompt": "over 100"})
    assert res.status_code == 200 and res.json() == {"where": '"amount" > 100'}
    again = model.asked[1]["prompt"]
    assert again.startswith(model.asked[0]["prompt"])
    assert 'You answered:\n"total" > 100\nDuckDB refused it:' in again and "total" in again.split("DuckDB refused it:")[1]

    # Twice wrong: the person is told what was tried and why it does not run.
    model.answers += ['"total" > 1', '"total" > 2']
    bad = client.post("/api/nodes/orders/filter", json={"prompt": "over 100"})
    assert bad.status_code == 422
    assert 'does not run: "total" > 2' in bad.json()["detail"] and "Binder Error" in bad.json()["detail"]

    # A comment would swallow the bracket the condition is closed with: refused like any other.
    model.answers += ['"amount" > 100 -- over 100', '"amount" > 100']
    assert client.post("/api/nodes/orders/filter", json={"prompt": "over 100"}).json() == {"where": '"amount" > 100'}


def test_a_request_that_is_no_filter_and_a_model_that_fails(client, model):
    model.answers.append("CANNOT: it asks for a total, not rows.")
    no = client.post("/api/nodes/orders/filter", json={"prompt": "the total amount"})
    assert no.status_code == 422 and no.json()["detail"] == "That is not a filter on this table's rows: it asks for a total, not rows"

    model.answers.append(llm.LLMError("openai API error: rate limited"))
    down = client.post("/api/nodes/orders/filter", json={"prompt": "over 100"})
    assert down.status_code == 502 and down.json()["detail"] == "openai API error: rate limited"


def test_one_output_of_a_node_with_several(client, model):
    model.answers.append('"value" >= 1')
    res = client.post("/api/nodes/fit/filter", json={"prompt": "at least one", "part": "coefs"})
    assert res.status_code == 200 and res.json() == {"where": '"value" >= 1'}
    assert model.asked[0]["prompt"].startswith('Table: "fit"."coefs"\nColumns:\n- "term" VARCHAR (values: \'a\', \'b\')\n- "value" DOUBLE\n')


def test_what_is_not_a_table_or_not_a_request_never_reaches_the_model(client, model):
    assert client.post("/api/nodes/nope/filter", json={"prompt": "x"}).status_code == 404
    # JSON, not a table; a node with several outputs asked for as one.
    for name, said in (("note", "note's output is JSON, not a table"), ("fit", "fit has several outputs")):
        res = client.post(f"/api/nodes/{name}/filter", json={"prompt": "x"})
        assert res.status_code == 400 and said in res.json()["detail"], res.text
    assert client.post("/api/nodes/orders/filter", json={"prompt": "   "}).status_code == 422
    assert client.post("/api/nodes/orders/filter", json={"prompt": ""}).status_code == 422
    assert client.post("/api/nodes/orders/filter", json={"prompt": "x" * (rowfilter.PROMPT_MAX + 1)}).status_code == 422
    assert model.asked == []


def test_without_a_model_the_route_says_how_to_get_one(tmp_path, monkeypatch, model):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY", "OPEN_ROUTER_KEY", "PORTKEY_API_KEY",
                 "OPENAI_API_KEY", "EXUVIA_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    write(tmp_path, "orders.py", ORDERS)
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/config").json()["ai_enabled"] is False
        res = c.post("/api/nodes/orders/filter", json={"prompt": "over 100"})
        assert res.status_code == 400 and "No model is configured" in res.json()["detail"]
    assert model.asked == []


def test_a_prompt_spends_the_model_so_it_is_for_editors():
    class Req:
        method = "POST"

        def __init__(self, path):
            self.url = type("U", (), {"path": path})

    assert _needed_role(Req("/p/a/api/query")) == "viewer"
    assert _needed_role(Req("/p/a/api/nodes/orders/filter")) == "editor"
