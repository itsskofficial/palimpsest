"""The Anthropic provider, which is the default path and had no tests at all.

`openai_api.py` was well covered and this was at zero, which is backwards: this is the
provider almost everybody will actually use. What follows drives it against a fake SDK —
a stand-in for `anthropic.Anthropic` that records what it was sent and returns what it is
told to — so the request shapes and the response handling are exercised without a key,
without a network, and without spending anything.

Two areas get most of the attention, because both are silent when they go wrong:

**Prompt caching.** The breakpoint has to sit on the last *stable* block. Put it one
block later and the cache never hits, which is not an error — it is a bill roughly ten
times larger than it should be, with identical output. Nothing fails; you just pay.

**Refusals.** A declined request is HTTP 200 with an empty or partial content array. Code
that reads `response.content` before checking `stop_reason` sees empty text and reports a
parse error, so what actually happened — the model declined, and nothing was written —
never reaches the user.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from palimpsest.llm.base import ModelError, ToolSpec

# ---------------------------------------------------------------------------
# a fake SDK
# ---------------------------------------------------------------------------


class Block:
    """One content block, shaped the way the SDK returns them."""

    def __init__(self, type="text", text="", **kw):
        self.type = type
        self.text = text
        for key, value in kw.items():
            setattr(self, key, value)


class Response:
    def __init__(self, content=None, stop_reason="end_turn", usage=None, **kw):
        self.content = content if content is not None else [Block(text="{}")]
        self.stop_reason = stop_reason
        self.usage = usage or types.SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0)
        for key, value in kw.items():
            setattr(self, key, value)


class Messages:
    def __init__(self, owner, beta: bool):
        self.owner = owner
        self.beta = beta

    def create(self, **params):
        self.owner.calls.append({"beta": self.beta, **params})
        behaviour = self.owner.behaviour
        if callable(behaviour):
            return behaviour(self.beta, params)
        return behaviour


class FakeAnthropic:
    def __init__(self, api_key=None, max_retries=3):
        self.api_key = api_key
        self.max_retries = max_retries
        self.calls: list[dict] = []
        self.behaviour = Response()
        self.messages = Messages(self, beta=False)
        self.beta = types.SimpleNamespace(messages=Messages(self, beta=True))


class FakeBadRequest(Exception):
    pass


@pytest.fixture()
def sdk(monkeypatch):
    """Install a fake `anthropic` module for the duration of one test."""
    module = types.ModuleType("anthropic")
    module.Anthropic = FakeAnthropic          # type: ignore[attr-defined]
    module.BadRequestError = FakeBadRequest   # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


@pytest.fixture()
def provider(sdk):
    from palimpsest.llm.anthropic_api import AnthropicProvider

    return AnthropicProvider("claude-opus-5", api_key="sk-ant-test")


def _schema():
    return {"type": "object", "properties": {"relation": {"type": "string"}}}


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_no_key_is_a_clear_error_rather_than_a_401_later(sdk):
    from palimpsest.llm.anthropic_api import AnthropicProvider

    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key=None)


def test_a_missing_sdk_names_the_extra_and_the_alternative(monkeypatch):
    """The SDK is an optional extra, so this is a routine state rather than a bug — and
    the message has to say that any OpenAI-compatible endpoint needs no install at all,
    because that is usually the better answer."""
    import builtins

    from palimpsest.llm.anthropic_api import AnthropicProvider

    real = builtins.__import__

    def missing(name, *a, **k):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", missing)

    with pytest.raises(ImportError) as caught:
        AnthropicProvider(api_key="sk-ant-x")

    assert "palimpsest-notion[anthropic]" in str(caught.value)
    assert "PALIMPSEST_MODEL_BASE_URL" in str(caught.value)


def test_the_provider_reports_what_it_can_do(provider):
    assert provider.name == "anthropic"
    assert provider.can_see is True
    assert provider.can_search_web is True


# ---------------------------------------------------------------------------
# prompt caching — the expensive thing to get wrong
# ---------------------------------------------------------------------------


def test_the_cache_breakpoint_sits_on_the_last_stable_block(provider):
    """The classifier sends the same page context with every claim from one source.

    That context goes behind a breakpoint so the second and subsequent claims read it at
    roughly a tenth of the price. The breakpoint must be on the *last* stable block: put
    it earlier and everything after it is uncached; put it on the volatile turn and the
    cache never hits at all. Neither failure raises anything — the output is identical
    and the bill is ten times larger.
    """
    provider.client.behaviour = Response([Block(text='{"relation": "new"}')])

    provider.json(system="you classify claims", prompt="the claim",
                  schema=_schema(), cache_prefix="forty pages of context")

    blocks = provider.client.calls[0]["system"]
    assert [b["text"] for b in blocks] == ["you classify claims",
                                           "forty pages of context"]
    assert "cache_control" not in blocks[0], "the breakpoint must be on the last block"
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}

    # And the volatile part stays in the user turn, after the breakpoint.
    assert provider.client.calls[0]["messages"] == [
        {"role": "user", "content": "the claim"}]


def test_with_no_prefix_the_system_prompt_itself_is_cached(provider):
    provider.client.behaviour = Response([Block(text="{}")])

    provider.json(system="a long stable system prompt", prompt="x", schema=_schema())

    blocks = provider.client.calls[0]["system"]
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_the_schema_is_sent_so_the_model_cannot_improvise(provider):
    provider.client.behaviour = Response([Block(text="{}")])

    provider.json(system="s", prompt="p", schema=_schema(), effort="low",
                  max_tokens=2048)

    call = provider.client.calls[0]
    assert call["output_config"]["format"] == {"type": "json_schema",
                                               "schema": _schema()}
    assert call["output_config"]["effort"] == "low"
    assert call["max_tokens"] == 2048


# ---------------------------------------------------------------------------
# refusals and malformed replies
# ---------------------------------------------------------------------------


def test_a_refusal_says_the_source_was_not_processed(provider):
    """A declined request is HTTP 200 with empty content. Read naively it looks like a
    parse failure, so the user is told the response was malformed rather than that the
    model declined and nothing was written."""
    provider.client.behaviour = Response(
        content=[], stop_reason="refusal",
        stop_details=types.SimpleNamespace(category="harmful_content"))

    with pytest.raises(ModelError) as caught:
        provider.json(system="s", prompt="p", schema=_schema())

    message = str(caught.value)
    assert "declined" in message
    assert "harmful_content" in message
    assert "Nothing was written" in message


def test_a_refusal_with_no_category_still_explains_itself(provider):
    provider.client.behaviour = Response(content=[], stop_reason="refusal")

    with pytest.raises(ModelError, match="unspecified"):
        provider.json(system="s", prompt="p", schema=_schema())


def test_an_empty_response_names_the_stop_reason(provider):
    """Without the stop reason, "empty response" is unactionable — hitting the token
    ceiling and the model having nothing to say look identical."""
    provider.client.behaviour = Response([Block(text="   ")], stop_reason="max_tokens")

    with pytest.raises(ModelError) as caught:
        provider.json(system="s", prompt="p", schema=_schema())

    assert "max_tokens" in str(caught.value)


def test_text_that_is_not_json_fails_loudly(provider):
    """Unreachable while the schema is attached, which is exactly why it must fail
    loudly rather than degrade — if the schema is ever dropped this is the symptom."""
    provider.client.behaviour = Response([Block(text="I think the answer is yes.")])

    with pytest.raises(ModelError, match="not valid JSON"):
        provider.json(system="s", prompt="p", schema=_schema())


def test_a_json_array_is_refused_because_the_caller_expects_an_object(provider):
    provider.client.behaviour = Response([Block(text='["a", "b"]')])

    with pytest.raises(ModelError, match="expected a JSON object"):
        provider.json(system="s", prompt="p", schema=_schema())


def test_several_text_blocks_are_joined_before_parsing(provider):
    """A long structured answer can arrive split across blocks. Reading only the first
    truncates the JSON and turns a good response into a parse error."""
    payload = json.dumps({"relation": "corroborates", "confidence": 0.9})
    half = len(payload) // 2
    provider.client.behaviour = Response(
        [Block(text=payload[:half]), Block(text=payload[half:])])

    parsed, _ = provider.json(system="s", prompt="p", schema=_schema())

    assert parsed["relation"] == "corroborates"


# ---------------------------------------------------------------------------
# the beta endpoint, and degrading off it
# ---------------------------------------------------------------------------


def test_server_side_fallbacks_are_used_when_available(provider):
    provider.client.behaviour = Response([Block(text="{}")])

    provider.json(system="s", prompt="p", schema=_schema())

    call = provider.client.calls[0]
    assert call["beta"] is True
    assert call["fallbacks"] == "default"


def test_an_sdk_without_the_beta_degrades_once_and_remembers(provider):
    """An older SDK raises `TypeError` on the unknown argument. Retrying the beta on
    every subsequent call would pay for a failed request each time, forever."""
    def behaviour(beta, params):
        if beta:
            raise TypeError("create() got an unexpected keyword argument 'fallbacks'")
        return Response([Block(text="{}")])

    provider.client.behaviour = behaviour

    provider.json(system="s", prompt="p", schema=_schema())
    provider.json(system="s", prompt="p", schema=_schema())

    attempts = [c["beta"] for c in provider.client.calls]
    assert attempts == [True, False, False], "it must stop trying the beta"


def test_an_account_without_the_beta_degrades_the_same_way(provider, sdk):
    def behaviour(beta, params):
        if beta:
            raise sdk.BadRequestError("beta not enabled for this account")
        return Response([Block(text="{}")])

    provider.client.behaviour = behaviour

    provider.json(system="s", prompt="p", schema=_schema())
    provider.json(system="s", prompt="p", schema=_schema())

    assert [c["beta"] for c in provider.client.calls] == [True, False, False]


def test_fallbacks_can_be_switched_off_entirely(sdk):
    from palimpsest.llm.anthropic_api import AnthropicProvider

    provider = AnthropicProvider(api_key="sk-ant-x", fallbacks=False)
    provider.client.behaviour = Response([Block(text="{}")])

    provider.json(system="s", prompt="p", schema=_schema())

    assert provider.client.calls[0]["beta"] is False


# ---------------------------------------------------------------------------
# conversation
# ---------------------------------------------------------------------------


def test_a_tool_call_comes_back_as_a_neutral_ToolCall(provider):
    """The agent loop speaks a vendor-neutral vocabulary. A provider that leaked SDK
    objects would make the loop Anthropic-only, which is the whole thing the layer
    exists to prevent."""
    provider.client.behaviour = Response(
        [Block(type="text", text="Looking that up."),
         Block(type="tool_use", id="tu_1", name="search_notes",
               input={"query": "attention"})],
        stop_reason="tool_use")

    conversation = provider.conversation()
    conversation.user("what did I write about attention?")
    reply = provider.converse(
        system="s", conversation=conversation,
        tools=[ToolSpec(name="search_notes", description="d", input_schema={})])

    assert reply.stop == "tools"
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "search_notes"
    assert reply.tool_calls[0].arguments == {"query": "attention"}
    assert reply.text == "Looking that up."


def test_every_stop_reason_maps_onto_the_neutral_vocabulary(provider):
    from palimpsest.llm.base import STOP_REASONS

    cases = {"end_turn": "end", "max_tokens": "length", "refusal": "refusal",
             "pause_turn": "pause"}
    for raw, expected in cases.items():
        provider.client.behaviour = Response([Block(text="hi")], stop_reason=raw)
        reply = provider.converse(system="s", conversation=provider.conversation(),
                                  tools=[])
        assert reply.stop == expected, raw
        assert reply.stop in STOP_REASONS


def test_web_search_is_only_offered_when_asked_for(provider):
    provider.client.behaviour = Response([Block(text="hi")])

    provider.converse(system="s", conversation=provider.conversation(), tools=[],
                      web_search=False)
    assert "tools" not in provider.client.calls[0]

    provider.converse(system="s", conversation=provider.conversation(), tools=[],
                      web_search=True)
    names = [t.get("name") or t.get("type") for t in provider.client.calls[1]["tools"]]
    assert any("search" in str(n) for n in names)


def test_thinking_is_on_by_default_and_can_be_turned_off(provider):
    provider.client.behaviour = Response([Block(text="hi")])

    provider.converse(system="s", conversation=provider.conversation(), tools=[])
    assert provider.client.calls[0]["thinking"]["type"] == "adaptive"

    provider.converse(system="s", conversation=provider.conversation(), tools=[],
                      thinking=False)
    assert "thinking" not in provider.client.calls[1]


def test_a_conversation_round_trips_a_tool_result(provider):
    """The loop's actual shape: ask, get a tool call, answer it, ask again. The result
    has to reach the wire in the shape the API expects or the model sees no answer."""
    from palimpsest.llm.base import ToolResult

    provider.client.behaviour = Response(
        [Block(type="tool_use", id="tu_1", name="search_notes", input={})],
        stop_reason="tool_use")

    conversation = provider.conversation()
    conversation.user("find it")
    reply = provider.converse(system="s", conversation=conversation, tools=[])
    conversation.assistant(reply)
    conversation.results([ToolResult(id="tu_1", payload="three pages matched")])

    wire = conversation.wire()
    assert wire[0]["role"] == "user"
    assert wire[1]["role"] == "assistant"
    assert wire[2]["role"] == "user"
    assert "three pages matched" in json.dumps(wire[2]["content"])


# ---------------------------------------------------------------------------
# usage, which is what the cost line is built from
# ---------------------------------------------------------------------------


def test_cache_reads_and_writes_are_counted_separately(provider):
    """They are priced differently — a cache read is a fraction of a fresh input token —
    so collapsing them into one number makes the reported cost wrong in the direction
    that flatters us."""
    provider.client.behaviour = Response(
        [Block(text="{}")],
        usage=types.SimpleNamespace(input_tokens=50, output_tokens=10,
                                    cache_read_input_tokens=4000,
                                    cache_creation_input_tokens=800))

    _, usage = provider.json(system="s", prompt="p", schema=_schema())

    assert usage.input == 50
    assert usage.output == 10
    assert usage.cache_read == 4000
    assert usage.cache_write == 800


def test_a_response_with_no_usage_block_does_not_crash_the_call(provider):
    """Accounting is never worth losing a result over."""
    provider.client.behaviour = Response([Block(text="{}")], usage=None)
    provider.client.behaviour.usage = types.SimpleNamespace()

    _, usage = provider.json(system="s", prompt="p", schema=_schema())

    assert usage.input == 0 and usage.output == 0


# ---------------------------------------------------------------------------
# vision
# ---------------------------------------------------------------------------


def test_an_image_is_sent_base64_encoded_with_its_media_type(provider):
    import base64

    provider.client.behaviour = Response([Block(text="a whiteboard with three boxes")])

    # `(text, usage)`, not just text: a vision call is the most expensive thing the
    # pipeline does per byte, so the caller needs the accounting too.
    text, usage = provider.describe_image(b"\x89PNG\r\n", "image/png", "what is this?")

    assert text == "a whiteboard with three boxes"
    assert usage.input > 0
    content = provider.client.calls[0]["messages"][0]["content"]
    image = next(b for b in content if b["type"] == "image")
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == b"\x89PNG\r\n"
