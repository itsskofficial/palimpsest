"""The OpenAI-compatible provider, which is how every model that is not Claude is run.

That is the whole point of it: Groq, OpenRouter, Together, Ollama, LM Studio, vLLM and
OpenAI itself all speak this wire format, and "provider invariance" is only true if this
one adapter copes with what they actually do rather than with what the spec says.

Two behaviours carry the weight.

**Degradation.** These endpoints disagree about `json_schema`, `json_object` and
`reasoning_effort`, and they disagree by returning a 400. Giving up one capability at a
time and retrying is what makes a local Ollama and a frontier API the same three lines
of configuration apart. Getting it wrong does not crash — it fails the first call
against half the supported runtimes.

**Retries.** A 429 is "wait"; a 401 is "never". Retrying the second wastes eight seconds
to produce the same error, and *not* retrying the first turns an ordinary rate limit
into a failed capture. And both must stop: a retry loop with no end is a worker thread
that never comes back.

Nothing here reaches the network — `urlopen` is the seam.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from palimpsest.llm import ModelError
from palimpsest.llm.openai_api import (
    OpenAIProvider,
    _about_a_parameter,
    _parse_json,
    _refused,
    _usage,
    strictify,
)

SCHEMA = {
    "type": "object",
    "properties": {"relation": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["relation", "confidence"],
}


@pytest.fixture()
def provider():
    return OpenAIProvider(model="qwen2.5:7b", api_key="k",
                          base_url="http://127.0.0.1:11434/v1", name="ollama")


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The backoff is real and correct; waiting it out in a test is not."""
    slept: list[float] = []
    monkeypatch.setattr("palimpsest.llm.openai_api.time.sleep", slept.append)
    return slept


class Wire:
    """A scripted `urlopen`. Each entry is a reply dict, or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []

    def __call__(self, request, timeout=None):
        self.requests.append({
            "url": request.full_url,
            "body": json.loads(request.data.decode("utf-8")),
            "headers": dict(request.header_items()),
        })
        answer = self.script.pop(0) if self.script else _completion("{}")
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    @property
    def bodies(self):
        return [r["body"] for r in self.requests]


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _completion(content: str, *, finish="stop", tool_calls=None, usage=None) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _http(code: int, detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/v1/chat/completions", code, "err", {},
                                  io.BytesIO(detail.encode("utf-8")))


def _wire(monkeypatch, *script) -> Wire:
    wire = Wire(*script)
    monkeypatch.setattr("palimpsest.llm.openai_api.urllib.request.urlopen", wire)
    return wire


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_a_json_request_asks_for_the_schema_and_parses_the_answer(provider, monkeypatch):
    wire = _wire(monkeypatch, _completion('{"relation": "refines", "confidence": 0.8}'))

    out, _ = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert out == {"relation": "refines", "confidence": 0.8}
    body = wire.bodies[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][0]["content"] == "s"


def test_the_key_is_sent_as_a_bearer_token(provider, monkeypatch):
    wire = _wire(monkeypatch, _completion("{}"))

    provider.json(system="s", prompt="p", schema=SCHEMA)

    assert wire.requests[0]["headers"]["Authorization"] == "Bearer k"


def test_a_runtime_with_no_key_sends_no_authorization_header(monkeypatch):
    """Ollama and LM Studio want no credential, and some reject the header outright."""
    local = OpenAIProvider(model="qwen2.5:7b", api_key=None,
                           base_url="http://127.0.0.1:11434/v1", name="ollama")
    wire = _wire(monkeypatch, _completion("{}"))

    local.json(system="s", prompt="p", schema=SCHEMA)

    assert "Authorization" not in wire.requests[0]["headers"]


def test_usage_is_reported_so_a_run_can_be_costed(provider, monkeypatch):
    _wire(monkeypatch, _completion("{}", usage={"prompt_tokens": 1200,
                                                "completion_tokens": 340}))

    _, usage = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert usage.input == 1200
    assert usage.output == 340


# ---------------------------------------------------------------------------
# degradation — what makes "any OpenAI-compatible endpoint" true
# ---------------------------------------------------------------------------


def test_an_endpoint_that_will_not_take_a_json_schema_falls_back_to_json_object(
        provider, monkeypatch):
    """Most local runtimes do not implement structured outputs. Failing here would mean
    the whole offline story works on paper and not on anybody's laptop."""
    wire = _wire(monkeypatch,
                 _http(400, "response_format.json_schema is not supported"),
                 _completion('{"relation": "new", "confidence": 0.5}'))

    out, _ = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert out["relation"] == "new"
    assert wire.bodies[1]["response_format"] == {"type": "json_object"}
    assert "matching this schema exactly" in wire.bodies[1]["messages"][1]["content"], \
        "with no enforcement, the shape has to go where the model can read it"


def test_an_endpoint_that_takes_neither_is_asked_in_plain_text(provider, monkeypatch):
    wire = _wire(monkeypatch,
                 _http(400, "response_format.json_schema is not supported"),
                 _http(400, "invalid parameter: response_format"),
                 _completion('{"relation": "new", "confidence": 0.5}'))

    out, _ = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert out["relation"] == "new"
    assert "response_format" not in wire.bodies[2]


def test_a_capability_given_up_is_not_asked_for_again(provider, monkeypatch):
    """The demotion is remembered on the provider. Re-offering it every call would cost
    a wasted round trip on every single request against that runtime."""
    _wire(monkeypatch,
          _http(400, "response_format.json_schema is not supported"),
          _completion("{}"), _completion("{}"))

    provider.json(system="s", prompt="p", schema=SCHEMA)
    wire = _wire(monkeypatch, _completion("{}"))
    provider.json(system="s", prompt="p", schema=SCHEMA)

    assert wire.bodies[0]["response_format"] == {"type": "json_object"}


def test_an_endpoint_that_will_not_take_reasoning_effort_gives_up_that_one_first(
        monkeypatch):
    """And only that one: dropping the schema as well would lose enforcement over a
    parameter that has nothing to do with it. A reasoning model, because only those are
    sent `reasoning_effort` in the first place."""
    reasoner = OpenAIProvider(model="deepseek-r1", api_key="k",
                              base_url="https://api.together.xyz/v1", name="together")
    wire = _wire(monkeypatch,
                 _http(400, "unknown parameter: reasoning_effort"),
                 _completion("{}"))

    reasoner.json(system="s", prompt="p", schema=SCHEMA)

    assert "reasoning_effort" in wire.bodies[0]
    assert "reasoning_effort" not in wire.bodies[1]
    assert wire.bodies[1]["response_format"]["type"] == "json_schema",         "the schema was not collateral"


def test_a_model_that_does_not_reason_is_never_sent_the_parameter(provider, monkeypatch):
    """Sending it and waiting for the 400 would cost a wasted request on every first
    call against every local runtime."""
    wire = _wire(monkeypatch, _completion("{}"))

    provider.json(system="s", prompt="p", schema=SCHEMA)

    assert "reasoning_effort" not in wire.bodies[0]


def test_when_there_is_nothing_left_to_give_up_the_error_is_reported(provider,
                                                                    monkeypatch):
    """Every rung refused: schema, then json_object, then nothing left to drop."""
    _wire(monkeypatch, *[_http(400, "response_format is not supported")
                         for _ in range(4)])

    with pytest.raises(ModelError, match="rejected the request"):
        provider.json(system="s", prompt="p", schema=SCHEMA)


def test_a_400_that_is_not_about_a_parameter_fails_at_once(provider, monkeypatch):
    """Billing, context length, a malformed prompt. None of those is fixed by giving up
    structured output, and trying costs three more requests to reach the same answer."""
    wire = _wire(monkeypatch, _http(400, "your credit balance is too low"))

    with pytest.raises(ModelError, match="credit balance"):
        provider.json(system="s", prompt="p", schema=SCHEMA)

    assert len(wire.requests) == 1


@pytest.mark.parametrize("detail,is_parameter", [
    ("unknown parameter: reasoning_effort", True),
    ("response_format.json_schema is not supported", True),
    ("Invalid schema for response_format", True),
    ("your credit balance is too low", False),
    ("context length exceeded", False),
])
def test_only_a_complaint_about_a_parameter_triggers_degradation(detail, is_parameter):
    """A 400 about billing is not a capability to give up, and treating it as one burns
    three more requests to arrive at the same answer."""
    assert _about_a_parameter(detail) is is_parameter


# ---------------------------------------------------------------------------
# retries — "wait" against "never"
# ---------------------------------------------------------------------------


def test_a_rate_limit_is_waited_out_and_retried(provider, monkeypatch, _no_sleeping):
    wire = _wire(monkeypatch, _http(429, "slow down"), _completion('{"ok": true}'))

    out, _ = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert out == {"ok": True}
    assert len(wire.requests) == 2
    assert _no_sleeping, "it waited rather than hammering"


def test_a_server_error_is_retried_too(provider, monkeypatch):
    wire = _wire(monkeypatch, _http(503, "upstream is down"), _completion("{}"))

    provider.json(system="s", prompt="p", schema=SCHEMA)

    assert len(wire.requests) == 2


def test_a_bad_key_is_not_retried(provider, monkeypatch, _no_sleeping):
    """Eight seconds of backoff to arrive at the same 401 is eight seconds somebody
    spends wondering whether it is working."""
    wire = _wire(monkeypatch, _http(401, "invalid api key"))

    with pytest.raises(ModelError, match="401"):
        provider.json(system="s", prompt="p", schema=SCHEMA)

    assert len(wire.requests) == 1
    assert _no_sleeping == []


def test_an_unreachable_endpoint_is_retried_and_then_named(provider, monkeypatch):
    """This is Ollama not running, which is the single most common local failure. The
    message has to say the URL, because the fix is to start the thing listening on it."""
    _wire(monkeypatch, *[urllib.error.URLError("connection refused")
                         for _ in range(provider.max_retries)])

    with pytest.raises(ModelError) as caught:
        provider.json(system="s", prompt="p", schema=SCHEMA)

    assert "127.0.0.1:11434" in str(caught.value)


def test_retrying_stops(provider, monkeypatch):
    """A loop with no end is a queue worker that never comes back."""
    wire = _wire(monkeypatch, *[_http(429, "slow down") for _ in range(20)])

    with pytest.raises(ModelError):
        provider.json(system="s", prompt="p", schema=SCHEMA)

    assert len(wire.requests) == provider.max_retries


# ---------------------------------------------------------------------------
# what comes back
# ---------------------------------------------------------------------------


def test_json_wrapped_in_a_fence_is_still_json():
    """Models that lost the schema argument answer in a markdown fence, and failing on
    it would make the plain-text fallback useless in exactly the case it exists for."""
    assert _parse_json('```json\n{"a": 1}\n```', "ollama") == {"a": 1}
    assert _parse_json('```\n{"a": 1}\n```', "ollama") == {"a": 1}


def test_json_with_a_sentence_in_front_of_it_is_still_json():
    assert _parse_json('Sure! Here you go:\n{"a": 1}', "ollama") == {"a": 1}


def test_something_that_is_not_json_at_all_names_the_provider():
    """Which one of five configured endpoints produced this is the first question."""
    with pytest.raises(ModelError) as caught:
        _parse_json("I'm afraid I can't help with that.", "together")

    assert "together" in str(caught.value)


def test_a_refusal_is_recognised_rather_than_parsed_as_an_answer():
    stop = {"choices": [{"message": {}, "finish_reason": "stop"}]}

    assert _refused({"choices": [{"message": {"refusal": "no"}}]}, "") is True
    assert _refused(stop, "I'm sorry, I can't help with that.") is True
    assert _refused(stop, "a real answer") is False


def test_a_content_filter_stop_is_a_refusal_not_an_empty_response():
    """OpenAI and Azure stop with `content_filter` and leave the content empty. Reported
    as "empty response" it reads like a flaky endpoint and invites a retry, for a request
    that will be filtered identically every time."""
    filtered = {"choices": [{"message": {"content": ""},
                             "finish_reason": "content_filter"}]}

    assert _refused(filtered, "") is True


def test_a_long_answer_that_happens_to_say_i_cant_is_an_answer():
    stop = {"choices": [{"message": {}, "finish_reason": "stop"}]}

    assert _refused(stop, "I can't overstate this: " + "detail. " * 80) is False


def test_json_on_a_filtered_request_says_it_was_declined(provider, monkeypatch):
    _wire(monkeypatch, _completion("", finish="content_filter"))

    with pytest.raises(ModelError) as caught:
        provider.json(system="s", prompt="p", schema=SCHEMA)

    assert "declined" in str(caught.value)
    assert "Nothing was written" in str(caught.value)


def test_usage_survives_a_provider_that_reports_none():
    """Ollama omits it. A KeyError here would fail a call that succeeded."""
    usage = _usage({"choices": []})

    assert usage.input == 0 and usage.output == 0


# ---------------------------------------------------------------------------
# the schema, made strict
# ---------------------------------------------------------------------------


def test_strictify_closes_every_object_it_finds():
    """OpenAI's strict mode refuses a schema with an open object anywhere in it, and the
    error names the whole schema rather than the branch that is open."""
    out = strictify({
        "type": "object",
        "properties": {
            "inner": {"type": "object", "properties": {"a": {"type": "string"}}},
            "list": {"type": "array",
                     "items": {"type": "object", "properties": {"b": {"type": "string"}}}},
        },
    })

    assert out["additionalProperties"] is False
    assert out["properties"]["inner"]["additionalProperties"] is False
    assert out["properties"]["list"]["items"]["additionalProperties"] is False


def test_strictify_requires_every_property_because_strict_mode_does():
    out = strictify({"type": "object",
                     "properties": {"a": {"type": "string"}, "b": {"type": "number"}}})

    assert sorted(out["required"]) == ["a", "b"]


def test_strictify_leaves_things_that_are_not_objects_alone():
    assert strictify({"type": "string"}) == {"type": "string"}
    assert strictify([1, 2]) == [1, 2]
    assert strictify("plain") == "plain"


# ---------------------------------------------------------------------------
# a conversational turn, which is what the agent runs
# ---------------------------------------------------------------------------


def test_a_turn_with_no_tool_call_ends(provider, monkeypatch):
    from palimpsest.llm import ToolSpec

    _wire(monkeypatch, _completion("Here is what I found."))

    reply = provider.converse(system="s", conversation=provider.conversation(),
                              tools=[ToolSpec("search", "d", {"type": "object"})])

    assert reply.stop == "end"
    assert reply.text == "Here is what I found."
    assert reply.tool_calls == []


def test_a_tool_call_comes_back_parsed(provider, monkeypatch):
    from palimpsest.llm import ToolSpec

    _wire(monkeypatch, _completion("", finish="tool_calls", tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": "search_notes",
                     "arguments": '{"query": "attention"}'}}]))

    reply = provider.converse(system="s", conversation=provider.conversation(),
                              tools=[ToolSpec("search_notes", "d", {"type": "object"})])

    assert reply.stop == "tools"
    assert reply.tool_calls[0].name == "search_notes"
    assert reply.tool_calls[0].arguments == {"query": "attention"}


def test_unparseable_tool_arguments_become_an_empty_call_rather_than_a_crash(provider,
                                                                            monkeypatch):
    """The model's mistake, not a transport failure. An empty dict lets the tool report
    what it needed and gives the model a chance to correct itself; an exception ends the
    turn with nothing to show for it."""
    from palimpsest.llm import ToolSpec

    _wire(monkeypatch, _completion("", finish="tool_calls", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "search_notes", "arguments": "{not json"}}]))

    reply = provider.converse(system="s", conversation=provider.conversation(),
                              tools=[ToolSpec("search_notes", "d", {"type": "object"})])

    assert reply.tool_calls[0].name == "search_notes"
    assert reply.tool_calls[0].arguments == {}


def test_a_turn_cut_off_by_the_token_limit_says_so(provider, monkeypatch):
    """`length` is not `end`. Treating it as a finished answer presents half a reply as
    the whole one."""
    _wire(monkeypatch, _completion("It was going to say more but", finish="length"))

    reply = provider.converse(system="s", conversation=provider.conversation(), tools=[])

    assert reply.stop == "length"


def test_tools_are_sent_in_the_shape_this_wire_format_expects(provider, monkeypatch):
    from palimpsest.llm import ToolSpec

    wire = _wire(monkeypatch, _completion("done"))

    provider.converse(system="s", conversation=provider.conversation(),
                      tools=[ToolSpec("search_notes", "Find things",
                                      {"type": "object", "properties": {}})])

    tool = wire.bodies[0]["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "search_notes"
    assert tool["function"]["description"] == "Find things"


def test_a_conversational_turn_degrades_the_same_way_a_json_one_does(provider,
                                                                    monkeypatch):
    """Otherwise the agent is unusable on exactly the runtimes `json()` was taught to
    cope with, and the failure is a dead first turn rather than a slow one."""
    wire = _wire(monkeypatch,
                 _http(400, "unknown parameter: reasoning_effort"),
                 _completion("answered"))

    reply = provider.converse(system="s", conversation=provider.conversation(),
                              tools=[])

    assert reply.text == "answered"
    assert "reasoning_effort" not in wire.bodies[1]


# ---------------------------------------------------------------------------
# vision
# ---------------------------------------------------------------------------


def test_a_text_only_model_says_so_rather_than_sending_an_image(monkeypatch):
    """A screenshot sent from a phone is an ordinary thing to send. The message names
    the setting, because the fix is a different model rather than a different file."""
    text_only = OpenAIProvider(model="qwen2.5:7b", api_key="k",
                               base_url="http://127.0.0.1:11434/v1", name="ollama")
    wire = _wire(monkeypatch, _completion("never reached"))

    if text_only.can_see:
        pytest.skip("this model is considered able to see")

    with pytest.raises(ModelError) as caught:
        text_only.describe_image(b"\x89PNG", "image/png", "what is this?")

    assert "PALIMPSEST_MODEL" in str(caught.value)
    assert wire.requests == []
