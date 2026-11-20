"""LLM providers for the agent, and ``complete()`` for node code.

The agent loop is provider-neutral; each provider owns its wire format and
its own message history shape. Two implementations:

* ``AnthropicProvider`` — Claude via the Anthropic SDK (default when
  ``ANTHROPIC_API_KEY`` is set).
* ``OpenAICompatProvider`` — any OpenAI-compatible chat-completions endpoint:
  OpenRouter (DeepSeek, Qwen, Llama, …), DeepSeek's own API, vLLM, Ollama,
  Azure OpenAI, a gateway at work.

Selection (``resolve_provider``):
    EXUVIA_PROVIDER   anthropic | openrouter | openai   (auto-detected if unset)
    EXUVIA_MODEL      model id (provider default if unset)
    EXUVIA_BASE_URL   OpenAI-compatible base URL (openai provider)
    keys             ANTHROPIC_API_KEY · OPENROUTER_API_KEY (or OPEN_ROUTER_KEY) · OPENAI_API_KEY

Every provider's ``turn`` yields UI events (``thinking`` / ``text`` /
``tool_start`` / ``error``) and ends with exactly one
``{"type": "turn_end", "status": ..., "calls": [...]}`` where status is
``tool_use`` (run the calls), ``end`` (done), ``retry`` (loop again; the
provider already appended a corrective note), or ``fatal`` (stop).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

ANTHROPIC_DEFAULT = "claude-opus-5-5"
OPENROUTER_DEFAULT = "deepseek/deepseek-v4-pro"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] | None
    parse_error: str | None = None


@dataclass
class Turn:
    status: str
    calls: list[ToolCall] = field(default_factory=list)


class Provider(Protocol):
    name: str
    model: str

    @property
    def label(self) -> str: ...
    def add_user(self, history: list[Any], text: str) -> None: ...
    def add_tool_results(self, history: list[Any], results: list[tuple[str, str, bool]]) -> None: ...
    def turn(self, system: str, tools: list[dict[str, Any]], history: list[Any]) -> AsyncIterator[dict[str, Any]]: ...


# -- Anthropic -----------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str, effort: str):
        import anthropic

        self.model = model
        self.effort = effort
        self.client = anthropic.AsyncAnthropic()

    @property
    def label(self) -> str:
        return self.model

    def add_user(self, history: list[Any], text: str) -> None:
        history.append({"role": "user", "content": text})

    def add_tool_results(self, history: list[Any], results: list[tuple[str, str, bool]]) -> None:
        history.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": i, "content": t, "is_error": e} for i, t, e in results],
        })

    async def turn(self, system: str, tools: list[dict[str, Any]], history: list[Any]) -> AsyncIterator[dict[str, Any]]:
        import anthropic

        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
                tools=[{**t, "eager_input_streaming": True} for t in tools],
                messages=history,
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "thinking":
                        yield {"type": "thinking"}
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield {"type": "text", "text": event.delta.text}
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield {"type": "tool_start", "id": event.content_block.id, "name": event.content_block.name}
                response = await stream.get_final_message()
        except ValueError as e:  # tool-input JSON the SDK could not parse at all
            history.append({"role": "user", "content": f"(Your last tool call had malformed JSON input: {e}. Please retry.)"})
            yield {"type": "turn_end", "turn": Turn("retry")}
            return
        except anthropic.AuthenticationError:
            for ev in fatal("Anthropic authentication failed — check ANTHROPIC_API_KEY."):
                yield ev
            return
        except anthropic.RateLimitError:
            for ev in fatal("Rate limited by the Anthropic API — try again shortly."):
                yield ev
            return
        except anthropic.APIStatusError as e:
            for ev in fatal(f"Anthropic API error {e.status_code}: {e.message}"):
                yield ev
            return
        except anthropic.APIConnectionError:
            for ev in fatal("Could not reach the Anthropic API."):
                yield ev
            return

        history.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            yield {"type": "error", "error": "The model declined this request."}
            yield {"type": "turn_end", "turn": Turn("fatal")}
            return
        if response.stop_reason == "pause_turn":
            yield {"type": "turn_end", "turn": Turn("retry")}
            return
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "max_tokens" and uses:
            self.add_tool_results(history, [
                (b.id, "Output was cut off (max_tokens); input incomplete. Retry with a smaller edit.", True) for b in uses
            ])
            yield {"type": "turn_end", "turn": Turn("retry")}
            return
        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else None) for b in uses]
        yield {"type": "turn_end", "turn": Turn("tool_use" if calls else "end", calls)}


# -- OpenAI-compatible (OpenRouter, DeepSeek, vLLM, Ollama, …) ------------------
# Where thinking models stream their reasoning: OpenRouter uses ``reasoning``;
# DeepSeek's own API, vLLM and most other servers use ``reasoning_content``.
# The assistant message keeps it under the same key, so the next request sends
# it back. Thinking models need that across a tool loop: DeepSeek's thinking
# mode expects every earlier ``reasoning_content`` once tools are in play, and
# without it the model loses its plan after each tool result — mistakes pile
# up until it writes its own tool-call template out as plain text.
REASONING_KEYS = ("reasoning", "reasoning_content")


class OpenAICompatProvider:
    def __init__(self, name: str, model: str, base_url: str, api_key: str):
        from openai import AsyncOpenAI

        self.name = name
        self.model = model
        headers = {"X-Title": "exuvia"} if "openrouter.ai" in base_url else None
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=headers)

    @property
    def label(self) -> str:
        return f"{self.model} · {self.name}"

    def add_user(self, history: list[Any], text: str) -> None:
        history.append({"role": "user", "content": text})

    def add_tool_results(self, history: list[Any], results: list[tuple[str, str, bool]]) -> None:
        for call_id, text, is_error in results:
            history.append({"role": "tool", "tool_call_id": call_id, "content": ("ERROR: " if is_error else "") + text})

    @staticmethod
    def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools
        ]

    async def turn(self, system: str, tools: list[dict[str, Any]], history: list[Any]) -> AsyncIterator[dict[str, Any]]:
        import openai

        text = ""
        reasoning = ""
        reasoning_key: str | None = None
        acc: dict[int, dict[str, str]] = {}
        finish = None
        thinking_sent = False
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *history],
                tools=self._tools(tools),
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                extra = getattr(delta, "model_extra", None) or {}
                piece = next(((k, v) for k in REASONING_KEYS if isinstance(v := extra.get(k), str) and v), None)
                if piece:
                    reasoning_key = reasoning_key or piece[0]
                    if piece[0] == reasoning_key:
                        reasoning += piece[1]
                if piece and not thinking_sent and not text:
                    thinking_sent = True
                    yield {"type": "thinking"}
                if delta.content:
                    text += delta.content
                    yield {"type": "text", "text": delta.content}
                for tc in delta.tool_calls or []:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        if not slot["name"]:
                            yield {"type": "tool_start", "id": tc.id or f"call_{tc.index}", "name": tc.function.name}
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                if choice.finish_reason:
                    finish = choice.finish_reason
        except openai.AuthenticationError:
            for ev in fatal(f"{self.name} authentication failed — check the API key."):
                yield ev
            return
        except openai.RateLimitError:
            for ev in fatal(f"Rate limited by {self.name} — try again shortly."):
                yield ev
            return
        except openai.APIStatusError as e:
            for ev in fatal(f"{self.name} API error {e.status_code}: {_err_text(e)}"):
                yield ev
            return
        except openai.APIConnectionError:
            for ev in fatal(f"Could not reach {self.name}."):
                yield ev
            return

        calls: list[ToolCall] = []
        wire_calls = []
        for i in sorted(acc):
            slot = acc[i]
            call_id = slot["id"] or f"call_{i}"
            wire_calls.append({"id": call_id, "type": "function", "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}})
            try:
                args = json.loads(slot["args"] or "{}")
                calls.append(ToolCall(call_id, slot["name"], args if isinstance(args, dict) else None))
            except json.JSONDecodeError as e:
                calls.append(ToolCall(call_id, slot["name"], None, f"INVALID_JSON: {e}"))
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if reasoning_key:
            msg[reasoning_key] = reasoning
        if wire_calls:
            msg["tool_calls"] = wire_calls
        history.append(msg)

        if finish == "content_filter":
            yield {"type": "error", "error": "The model declined this request."}
            yield {"type": "turn_end", "turn": Turn("fatal")}
            return
        if finish == "length" and calls:
            self.add_tool_results(history, [
                (c.id, "Output was cut off (length); input incomplete. Retry with a smaller edit.", True) for c in calls
            ])
            yield {"type": "turn_end", "turn": Turn("retry")}
            return
        yield {"type": "turn_end", "turn": Turn("tool_use" if calls else "end", calls)}


def _err_text(e: Any) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
    return str(getattr(e, "message", e))[:300]


def fatal(msg: str) -> list[dict[str, Any]]:
    """An error event followed by a fatal turn end."""
    return [{"type": "error", "error": msg}, {"type": "turn_end", "turn": Turn("fatal")}]


# -- selection -------------------------------------------------------------------
def openrouter_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPEN_ROUTER_KEY")


@dataclass
class Endpoint:
    """Which model to call and how, read from the environment."""

    provider: str  # anthropic | openrouter | openai
    model: str
    base_url: str | None = None
    api_key: str | None = None


def resolve_endpoint() -> Endpoint | None:
    """The configured model endpoint; None when no provider is set up."""
    choice = (os.environ.get("EXUVIA_PROVIDER") or "").lower()
    model = os.environ.get("EXUVIA_MODEL")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not choice:
        if anthropic_key:
            choice = "anthropic"
        elif openrouter_key():
            choice = "openrouter"
        elif os.environ.get("OPENAI_API_KEY") and os.environ.get("EXUVIA_BASE_URL"):
            choice = "openai"
        else:
            return None
    if choice == "anthropic":
        if not anthropic_key:
            return None
        return Endpoint("anthropic", model or ANTHROPIC_DEFAULT)
    if choice == "openrouter":
        key = openrouter_key()
        if not key:
            return None
        return Endpoint("openrouter", model or OPENROUTER_DEFAULT, os.environ.get("EXUVIA_BASE_URL", OPENROUTER_URL), key)
    if choice == "openai":
        key, url = os.environ.get("OPENAI_API_KEY"), os.environ.get("EXUVIA_BASE_URL")
        if not key or not url or not model:
            raise ValueError("EXUVIA_PROVIDER=openai needs OPENAI_API_KEY, EXUVIA_BASE_URL and EXUVIA_MODEL")
        return Endpoint("openai", model, url, key)
    raise ValueError(f"unknown EXUVIA_PROVIDER {choice!r} (anthropic | openrouter | openai)")


def resolve_provider() -> Provider | None:
    """Pick a provider from the environment; None means the agent is off."""
    endpoint = resolve_endpoint()
    if endpoint is None:
        return None
    if endpoint.provider == "anthropic":
        return AnthropicProvider(endpoint.model, os.environ.get("EXUVIA_EFFORT", "medium"))
    assert endpoint.base_url and endpoint.api_key
    return OpenAICompatProvider(endpoint.provider, endpoint.model, endpoint.base_url, endpoint.api_key)


# -- one-shot completions for node code ---------------------------------------------
class LLMError(RuntimeError):
    """The model could not be reached or returned no text."""


def complete(prompt: str, *, system: str | None = None, max_tokens: int = 4000) -> str:
    """Ask the configured model one question from inside a node and return its text.

        from exuvia import llm

        @exuvia.node()
        def note(ranked):
            return {"text": llm.complete(f"Describe this table in two sentences:\\n{ranked.to_csv(index=False)}")}

    The model is the one the agent uses (``EXUVIA_PROVIDER`` / ``EXUVIA_MODEL``,
    see the module docstring). The call is blocking, has no tools and keeps no
    history. A node that calls it is not reproducible: a re-run can word the
    answer differently, so compute every number in code and pass it in.
    ``max_tokens`` covers the model's reasoning as well as its answer.
    Raises ``LLMError`` when no provider is configured, the call fails, or the
    answer is empty or cut off.
    """
    endpoint = resolve_endpoint()
    if endpoint is None:
        raise LLMError("no model is configured (set EXUVIA_PROVIDER and its key)")
    if endpoint.provider == "anthropic":
        import anthropic

        try:
            message = anthropic.Anthropic().messages.create(
                model=endpoint.model,
                max_tokens=max_tokens,
                system=system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic API error: {_err_text(e)}") from e
        text = "".join(block.text for block in message.content if block.type == "text")
        cut_off = message.stop_reason == "max_tokens"
    else:
        import openai

        headers = {"X-Title": "exuvia"} if "openrouter.ai" in (endpoint.base_url or "") else None
        client = openai.OpenAI(api_key=endpoint.api_key, base_url=endpoint.base_url, default_headers=headers)
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        try:
            response = client.chat.completions.create(model=endpoint.model, messages=messages, max_tokens=max_tokens)
        except openai.APIError as e:
            raise LLMError(f"{endpoint.provider} API error: {_err_text(e)}") from e
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice else ""
        cut_off = bool(choice) and choice.finish_reason == "length"
    text = text.strip()
    if not text:
        raise LLMError("the model returned no text")
    if cut_off:
        raise LLMError(f"the answer was cut off at max_tokens={max_tokens}; raise it or ask for less")
    return text
