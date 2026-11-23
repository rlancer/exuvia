"""Provider layer: the neutral agent loop, OpenAI-style stream assembly, selection."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from exuvia import llm
from exuvia.agent import Agent
from exuvia.llm import OpenAICompatProvider, ToolCall, Turn, resolve_provider
from exuvia.workspace import Workspace

NODE = '''import exuvia


@exuvia.node()
def nums():
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''


class ScriptedProvider:
    """Plays back turns; records the history it is handed."""

    name, model, label = "fake", "fake-1", "fake-1"

    def __init__(self, turns):
        self.turns = list(turns)
        self.results = []

    def add_user(self, history, text):
        history.append({"role": "user", "content": text})

    def add_tool_results(self, history, results):
        self.results.append(results)
        history.append({"role": "tool", "results": results})

    async def turn(self, system, tools, history):
        assert "exuvia" in system and {t["name"] for t in tools} >= {"write_node", "run_node"}
        text, turn = self.turns.pop(0)
        if text:
            yield {"type": "text", "text": text}
        yield {"type": "turn_end", "turn": turn}


def collect(agent, msg):
    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        return [ev async for ev in agent.chat(None, msg, None)]

    return asyncio.run(go())


def test_neutral_loop_runs_tools_and_feeds_results_back(tmp_path):
    agent = Agent(Workspace(tmp_path))
    agent.provider = ScriptedProvider([
        ("", Turn("tool_use", [ToolCall("c1", "write_node", {"name": "nums", "kind": "python", "source": NODE})])),
        ("", Turn("tool_use", [
            ToolCall("c2", "run_node", {"name": "nums"}),
            ToolCall("c3", "run_node", {}),  # invalid: missing name
            ToolCall("c4", "read_node", None, "INVALID_JSON: bad"),
        ])),
        ("Built and ran `nums`.", Turn("end")),
    ])
    events = collect(agent, "make a nums node")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session" and kinds[-1] == "done"
    results = {e["id"]: e for e in events if e["type"] == "tool_result"}
    assert results["c1"]["ok"] and results["c2"]["ok"]
    assert not results["c3"]["ok"] and "missing" in results["c3"]["summary"]
    assert not results["c4"]["ok"] and "INVALID_JSON" in results["c4"]["summary"]
    assert json.loads(results["c2"]["summary"])["runs"] == {"nums": "success"}
    assert any(e["type"] == "text" and "nums" in e["text"] for e in events)
    assert agent.ws.status()["nums"]["state"] == "fresh"


def test_fatal_turn_stops_the_loop(tmp_path):
    agent = Agent(Workspace(tmp_path))
    agent.provider = ScriptedProvider([("", Turn("fatal"))])
    assert [e["type"] for e in collect(agent, "hi")] == ["session", "done"]


def test_a_conversation_is_saved_with_its_title_and_who_started_it(tmp_path):
    from exuvia import auth

    agent = Agent(Workspace(tmp_path))
    agent.provider = ScriptedProvider([("Hello.", Turn("end")), ("Again.", Turn("end"))])

    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        with auth.signed_in(auth.Identity("alice", None, "headers", uid="u-alice")):
            first = [ev async for ev in agent.chat(None, "  what   does\nthis graph do? " + "x" * 100, None)]
        sid = first[0]["session_id"]
        [ev async for ev in agent.chat(sid, "and now?", None)]  # a later turn, by nobody, keeps both
        return sid

    sid = asyncio.run(go())
    [row] = agent.conversations()
    assert row["id"] == sid and not row["running"]
    assert row["title"].startswith("what does this graph do? xxx") and len(row["title"]) == 80
    assert row["title"].endswith("…") and row["started_by"] == "alice"
    assert row["created_at"] <= row["updated_at"]
    assert agent.conversations(user="u-alice") == [row]
    assert agent.conversations(user="u-bob") == []
    assert agent.conversations(name="alice") == [row]
    assert [e["type"] for e in agent.transcript(sid)] == ["user", "text", "user", "text"]


# -- OpenAI-compatible stream assembly ------------------------------------------
def chunk(content=None, tool_calls=None, finish=None, reasoning=None):
    delta = NS(content=content, tool_calls=tool_calls, model_extra={"reasoning": reasoning} if reasoning else {})
    return NS(choices=[NS(delta=delta, finish_reason=finish)])


def tc(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self.chunks:
                yield c

        return gen()


def make_compat(chunks):
    p = OpenAICompatProvider.__new__(OpenAICompatProvider)
    p.name, p.model = "openrouter", "deepseek/test"
    sent = {}

    async def create(**kw):
        sent.update(kw)
        return FakeStream(chunks)

    p.client = NS(chat=NS(completions=NS(create=create)))
    return p, sent


def run_turn(p, history):
    async def go():
        return [ev async for ev in p.turn("SYS", [{"name": "run_node", "description": "d", "input_schema": {"type": "object"}}], history)]

    return asyncio.run(go())


def test_compat_assembles_split_tool_calls():
    p, sent = make_compat([
        chunk(reasoning="hmm"),
        chunk(content="Running "),
        chunk(content="it."),
        chunk(tool_calls=[tc(0, id="call_a", name="run_node", args='{"na')]),
        chunk(tool_calls=[tc(0, args='me": "x"}'), tc(1, id="call_b", name="run_node", args="{bad")]),
        chunk(finish="tool_calls"),
    ])
    history = [{"role": "user", "content": "go"}]
    events = run_turn(p, history)
    assert events[0] == {"type": "thinking"}
    assert "".join(e.get("text", "") for e in events) == "Running it."
    turn = events[-1]["turn"]
    assert turn.status == "tool_use"
    assert turn.calls[0] == ToolCall("call_a", "run_node", {"name": "x"})
    assert turn.calls[1].parse_error and turn.calls[1].args is None
    # request shape
    assert sent["messages"][0] == {"role": "system", "content": "SYS"}
    assert sent["tools"][0]["function"]["name"] == "run_node" and sent["stream"] is True
    # assistant message appended in OpenAI format
    msg = history[-1]
    assert msg["role"] == "assistant" and msg["content"] == "Running it."
    assert [c["id"] for c in msg["tool_calls"]] == ["call_a", "call_b"]
    p.add_tool_results(history, [("call_a", "ok", False), ("call_b", "nope", True)])
    assert history[-1] == {"role": "tool", "tool_call_id": "call_b", "content": "ERROR: nope"}


def test_compat_sends_reasoning_back_under_the_key_it_came_on():
    # OpenRouter streams `reasoning`; the assistant message keeps it so the next
    # request in the tool loop carries the model's earlier thinking.
    p, sent = make_compat([
        chunk(reasoning="plan: "),
        chunk(reasoning="write the node"),
        chunk(tool_calls=[tc(0, id="c", name="run_node", args='{"name": "x"}')]),
        chunk(finish="tool_calls"),
    ])
    history = [{"role": "user", "content": "go"}]
    run_turn(p, history)
    assert history[-1]["reasoning"] == "plan: write the node"
    assert "reasoning_content" not in history[-1]
    p.add_tool_results(history, [("c", "ok", False)])
    p2, sent2 = make_compat([chunk(content="Done."), chunk(finish="stop")])
    run_turn(p2, history)
    assert sent2["messages"][2]["reasoning"] == "plan: write the node"

    # DeepSeek's own API (and vLLM) use `reasoning_content`.
    delta = NS(content=None, tool_calls=None, model_extra={"reasoning_content": "think"})
    p, _ = make_compat([NS(choices=[NS(delta=delta, finish_reason=None)]), chunk(content="Hi."), chunk(finish="stop")])
    history = []
    run_turn(p, history)
    assert history[-1] == {"role": "assistant", "content": "Hi.", "reasoning_content": "think"}

    # No reasoning streamed: the message stays plain.
    p, _ = make_compat([chunk(content="Hi."), chunk(finish="stop")])
    history = []
    run_turn(p, history)
    assert history[-1] == {"role": "assistant", "content": "Hi."}


def test_compat_text_only_ends_and_truncated_calls_retry():
    p, _ = make_compat([chunk(content="Done."), chunk(finish="stop")])
    assert run_turn(p, [])[-1]["turn"].status == "end"
    p, _ = make_compat([chunk(tool_calls=[tc(0, id="c", name="run_node", args='{"name": "x"}')]), chunk(finish="length")])
    history = []
    assert run_turn(p, history)[-1]["turn"].status == "retry"
    assert history[-1]["role"] == "tool" and "cut off" in history[-1]["content"]


# -- selection ----------------------------------------------------------------------
@pytest.fixture
def clean_env(monkeypatch):
    for k in ("EXUVIA_PROVIDER", "EXUVIA_MODEL", "EXUVIA_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
              "OPENROUTER_API_KEY", "OPEN_ROUTER_KEY", "OPENAI_API_KEY", "PORTKEY_API_KEY", "PORTKEY_BASE_URL",
              *llm.PORTKEY_HEADERS):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_no_keys_means_agent_off(clean_env):
    assert resolve_provider() is None


def test_openrouter_autodetect_and_lobster_key_name(clean_env):
    clean_env.setenv("OPEN_ROUTER_KEY", "sk-or-test")
    p = resolve_provider()
    assert p.name == "openrouter" and p.model == llm.OPENROUTER_DEFAULT
    assert str(p.client.base_url).rstrip("/") == llm.OPENROUTER_URL


def test_anthropic_wins_autodetect_unless_overridden(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert resolve_provider().name == "anthropic"
    clean_env.setenv("EXUVIA_PROVIDER", "openrouter")
    clean_env.setenv("EXUVIA_MODEL", "deepseek/deepseek-v4-flash")
    p = resolve_provider()
    assert p.name == "openrouter" and p.model == "deepseek/deepseek-v4-flash"


def test_generic_openai_needs_url_and_model(clean_env):
    clean_env.setenv("EXUVIA_PROVIDER", "openai")
    clean_env.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(ValueError):
        resolve_provider()
    clean_env.setenv("EXUVIA_BASE_URL", "http://localhost:11434/v1")
    clean_env.setenv("EXUVIA_MODEL", "qwen3:32b")
    assert resolve_provider().label == "qwen3:32b · openai"


def test_portkey_needs_a_model_and_sends_its_headers(clean_env):
    clean_env.setenv("EXUVIA_PROVIDER", "portkey")
    assert resolve_provider() is None  # no key: the agent is off
    clean_env.setenv("PORTKEY_API_KEY", "pk-test")
    with pytest.raises(ValueError, match="EXUVIA_MODEL"):
        resolve_provider()
    clean_env.setenv("EXUVIA_MODEL", "@anthropic-prod/claude-opus-5-5")
    clean_env.setenv("PORTKEY_CONFIG", "pc-exuvia-123")
    p = resolve_provider()
    assert p.name == "portkey" and p.label == "@anthropic-prod/claude-opus-5-5 · portkey"
    assert str(p.client.base_url).rstrip("/") == llm.PORTKEY_URL
    assert p.client.api_key == "pk-test"
    headers = p.client.default_headers
    assert headers["x-portkey-api-key"] == "pk-test" and headers["x-portkey-config"] == "pc-exuvia-123"
    assert "x-portkey-provider" not in headers and "X-Title" not in headers

    clean_env.setenv("PORTKEY_BASE_URL", "http://portkey.internal:8787/v1")
    assert str(resolve_provider().client.base_url).rstrip("/") == "http://portkey.internal:8787/v1"


def test_portkey_autodetects_only_with_a_model_and_after_the_others(clean_env):
    clean_env.setenv("PORTKEY_API_KEY", "pk-test")
    assert resolve_provider() is None  # a stray key without a model leaves the agent off
    clean_env.setenv("EXUVIA_MODEL", "@openai-prod/gpt-5")
    assert resolve_provider().name == "portkey"
    clean_env.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert resolve_provider().name == "openrouter"


def test_complete_asks_once_and_returns_the_text(monkeypatch):
    import openai

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY", "OPEN_ROUTER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EXUVIA_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="no model is configured"):
        llm.complete("hi")

    monkeypatch.setenv("EXUVIA_PROVIDER", "openai")
    monkeypatch.setenv("EXUVIA_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("EXUVIA_MODEL", "some/model")
    monkeypatch.setenv("OPENAI_API_KEY", "attached-by-gateway")
    sent = {}
    answer = {"content": "  Two sentences.  ", "finish": "stop"}

    class FakeClient:
        def __init__(self, **kwargs):
            sent["client"] = kwargs
            self.chat = NS(completions=NS(create=self.create))

        def create(self, **kwargs):
            sent["request"] = kwargs
            return NS(choices=[NS(message=NS(content=answer["content"]), finish_reason=answer["finish"])])

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    assert llm.complete("Describe it.", system="Data only.", max_tokens=900) == "Two sentences."
    assert sent["client"]["base_url"] == "http://llm.internal/v1"
    assert sent["request"] == {
        "model": "some/model",
        "messages": [{"role": "system", "content": "Data only."}, {"role": "user", "content": "Describe it."}],
        "max_tokens": 900,
    }

    assert sent["client"]["default_headers"] is None

    monkeypatch.setenv("EXUVIA_PROVIDER", "portkey")
    monkeypatch.setenv("PORTKEY_API_KEY", "pk-test")
    monkeypatch.delenv("EXUVIA_BASE_URL")
    monkeypatch.delenv("PORTKEY_BASE_URL", raising=False)
    for name in llm.PORTKEY_HEADERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PORTKEY_PROVIDER", "@bedrock-prod")
    llm.complete("Describe it.")
    assert sent["client"]["base_url"] == llm.PORTKEY_URL
    assert sent["client"]["default_headers"] == {"x-portkey-api-key": "pk-test", "x-portkey-provider": "@bedrock-prod"}
    monkeypatch.setenv("EXUVIA_PROVIDER", "openai")
    monkeypatch.setenv("EXUVIA_BASE_URL", "http://llm.internal/v1")

    answer.update(content="", finish="stop")
    with pytest.raises(llm.LLMError, match="no text"):
        llm.complete("Describe it.")
    answer.update(content="Half a sen", finish="length")
    with pytest.raises(llm.LLMError, match="cut off"):
        llm.complete("Describe it.")
