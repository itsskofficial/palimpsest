"""The model layer: resolution, degradation and honest costs — all offline.

`palimpsest.llm` makes three promises that nothing else in the codebase can check for
it, and all three fail quietly rather than loudly when they break.

*"Any model works."* The claim is not that every endpoint speaks `json_schema`; it is
that the ones which do not still return a parsed object, after exactly one failed
request rather than one per call. That degradation is invisible from the outside — the
caller gets its dict either way — so it is pinned here by counting requests and reading
the bodies that went out.

*"Configuration decides the provider."* Resolution reads a handful of variables in a
fixed order, and the interesting case is the one where an explicit `Settings` says
nothing: it must mean "no keys", not "go and look in the shell". A suite that gets this
wrong passes on the developer's machine and fails in CI.

*"Costs are honest."* An unknown model has no price, and `None` is the answer. A
fabricated `$0.00` is worse than a blank, because it reads as a measurement.

Everything here runs with no key and no network. `_post` is replaced with a recorder
that replays canned response dicts, so the provider's own control flow is what is under
test, not any server's behaviour.
"""

from __future__ import annotations

import copy
import json

import pytest

from palimpsest.config import MODEL_PROVIDERS, Settings
from palimpsest.llm import (
    Model,
    ModelError,
    TokenUsage,
    ToolCall,
    ToolResult,
    Usage,
    available,
    describe_setup,
    resolve,
)
from palimpsest.llm.base import Reply
from palimpsest.llm.openai_api import (
    OpenAIConversation,
    OpenAIProvider,
    _about_a_parameter,
    _parse_json,
    _refused,
    _Unsupported,
    _usage,
    strictify,
)
from palimpsest.llm.pricing import cost_usd, rate_for

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def a_response(content: str, **message_extra) -> dict:
    """One well-formed `/chat/completions` body, in the shape every server returns."""
    message = {"role": "assistant", "content": content, **message_extra}
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


class Posts:
    """A stand-in for `OpenAIProvider._post` that records what was sent.

    Outcomes are replayed in order; the last one repeats, so a test only has to script
    the calls whose answers differ. An `Exception` outcome is raised rather than
    returned, which is how a server's rejection is expressed here.

    Bodies are deep-copied on the way past because the degradation path *mutates* the
    body it already sent — recording the object itself would show every request as the
    last one.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.bodies: list[dict] = []

    def __call__(self, path: str, body: dict) -> dict:
        self.bodies.append(copy.deepcopy(body))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def calls(self) -> int:
        return len(self.bodies)


class FakeProvider:
    """A `Provider` that answers from a script, so the façade is what is tested."""

    name = "fake"
    model = "gpt-5-mini"
    can_see = False
    can_search_web = False

    def __init__(self, result: dict | None = None, error: Exception | None = None,
                 usage: TokenUsage | None = None):
        self.result = result if result is not None else {"ok": True}
        self.error = error
        self.usage = usage or TokenUsage(input=1000, output=200, cache_read=40)
        self.seen: dict = {}

    def json(self, **kwargs):
        self.seen = kwargs
        if self.error:
            raise self.error
        return self.result, self.usage


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch):
    """Tracing is a no-op without Langfuse keys, but never depend on the machine."""
    monkeypatch.setattr("palimpsest.trace.generation", lambda *a, **k: None)


def a_provider(model: str = "gpt-5", **kwargs) -> OpenAIProvider:
    return OpenAIProvider(model, api_key="sk-not-a-real-key", **kwargs)


def scripted(provider: OpenAIProvider, monkeypatch, *outcomes) -> Posts:
    posts = Posts(*outcomes)
    monkeypatch.setattr(provider, "_post", posts)
    return posts


SCHEMA = {"type": "object", "properties": {"claim": {"type": "string"}},
          "required": ["claim"]}


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_no_model_configured_is_a_supported_state_not_an_error():
    """Half the product runs without a model, so "nothing set" must answer, not raise.

    `describe_setup` returning None and `available` returning False is what lets the
    mirror, retrieval, the duplicate sweep and undo work on a machine with no key.
    """
    assert describe_setup(None) is None
    assert available(None) is False


def test_resolving_with_nothing_configured_names_every_way_to_fix_it():
    """The error is the documentation someone reads at the worst moment."""
    with pytest.raises(ModelError) as caught:
        resolve(None)

    message = str(caught.value)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
                 "PALIMPSEST_MODEL_BASE_URL"):
        assert name in message


def test_anthropic_wins_when_several_keys_are_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")

    setup = describe_setup(None)
    assert setup is not None
    assert setup.provider == "anthropic"
    assert setup.model == "claude-opus-5"
    assert setup.base_url is None


def test_a_groq_key_alone_resolves_to_groq_with_its_own_default_model(monkeypatch):
    """Someone with only a Groq key must not be handed a Claude model id to call."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
    _, groq_url, groq_default = MODEL_PROVIDERS["groq"]

    provider = resolve(None)
    assert provider.name == "groq"
    assert provider.base_url == groq_url
    assert provider.model == groq_default


def test_describe_setup_says_which_variable_decided_it(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")

    setup = describe_setup(None)
    assert setup is not None
    assert setup.reason == "GROQ_API_KEY is set"


def test_an_explicit_provider_overrides_key_sniffing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("PALIMPSEST_MODEL_PROVIDER", "groq")

    setup = describe_setup(None)
    assert setup is not None
    assert setup.provider == "groq"
    assert setup.base_url == MODEL_PROVIDERS["groq"][1]


def test_a_base_url_beats_every_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("PALIMPSEST_MODEL_PROVIDER", "groq")
    monkeypatch.setenv("PALIMPSEST_MODEL_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("PALIMPSEST_MODEL", "qwen2.5:14b")

    setup = describe_setup(None)
    assert setup is not None
    assert setup.provider == "openai-compatible"
    assert setup.base_url == "http://localhost:11434/v1"
    assert setup.model == "qwen2.5:14b"


def test_a_base_url_without_a_model_refuses_to_guess_one(monkeypatch):
    """A compatible endpoint has no default. Picking one for the user would 404 later,
    somewhere much less obvious than here."""
    monkeypatch.setenv("PALIMPSEST_MODEL_BASE_URL", "http://localhost:11434/v1")

    with pytest.raises(ModelError) as caught:
        resolve(None)
    assert "PALIMPSEST_MODEL" in str(caught.value)


def test_a_settings_is_authoritative_not_merely_preferred(monkeypatch):
    """The regression that makes a suite pass only on the machine that exported a key.

    `Settings()` declares no provider keys. If resolution treated it as a *preference*
    and fell back to `os.environ` for the fields it left empty, this machine's ambient
    GROQ_API_KEY would make "no model is configured" quietly false — and every test
    asserting the keyless path would exercise the keyed one instead, on that machine
    only. An explicit Settings means "these values, and no others".
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk-really-set-in-the-shell")

    assert available(Settings()) is False
    assert describe_setup(Settings()) is None
    assert available(None) is True          # the environment alone still resolves


# ---------------------------------------------------------------------------
# strictify: an Anthropic-shaped schema, rewritten for OpenAI's strict mode
# ---------------------------------------------------------------------------


def test_strictify_forbids_extra_properties_on_every_object():
    out = strictify({"type": "object", "properties": {"a": {"type": "string"}}})
    assert out["additionalProperties"] is False


def test_strictify_promotes_every_property_into_required():
    out = strictify({"type": "object",
                     "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                     "required": ["a"]})
    assert out["required"] == ["a", "b"]


def test_a_formerly_optional_property_becomes_required_but_nullable():
    """Strict mode has no "optional", so the contract moves rather than disappearing:
    the model must always emit the key, and `null` is how it says "not present"."""
    out = strictify({"type": "object",
                     "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                     "required": ["a"]})
    assert out["properties"]["b"]["type"] == ["integer", "null"]


def test_a_property_that_was_required_keeps_its_type_untouched():
    out = strictify({"type": "object",
                     "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                     "required": ["a"]})
    assert out["properties"]["a"]["type"] == "string"


def test_strictify_recurses_through_arrays():
    out = strictify({
        "type": "object",
        "properties": {"claims": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"text": {"type": "string"},
                                     "note": {"type": "string"}},
                      "required": ["text"]},
        }},
        "required": ["claims"],
    })
    items = out["properties"]["claims"]["items"]
    assert items["additionalProperties"] is False
    assert items["required"] == ["text", "note"]
    assert items["properties"]["note"]["type"] == ["string", "null"]


def test_strictify_recurses_through_nested_objects():
    out = strictify({
        "type": "object",
        "properties": {"anchor": {"type": "object",
                                  "properties": {"locator": {"type": "string"},
                                                 "url": {"type": "string"}},
                                  "required": ["locator"]}},
        "required": ["anchor"],
    })
    anchor = out["properties"]["anchor"]
    assert anchor["additionalProperties"] is False
    assert anchor["required"] == ["locator", "url"]
    assert anchor["properties"]["url"]["type"] == ["string", "null"]


def test_strictify_does_not_mutate_the_schema_it_is_given():
    """Schemas are module-level constants shared by every call. One in-place rewrite on
    the first request would silently change the shape asked of every provider after it,
    including the ones that never needed strict mode."""
    schema = {
        "type": "object",
        "properties": {"claims": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"text": {"type": "string"},
                                     "note": {"type": "string"}},
                      "required": ["text"]},
        }},
        "required": ["claims"],
    }
    before = copy.deepcopy(schema)

    strictify(schema)
    assert schema == before


# ---------------------------------------------------------------------------
# reading a response
# ---------------------------------------------------------------------------


def test_parse_json_reads_a_bare_object():
    assert _parse_json('{"claim": "attention scales by 1/sqrt(d_k)"}', "groq") == {
        "claim": "attention scales by 1/sqrt(d_k)"}


def test_parse_json_reads_an_object_inside_a_code_fence():
    text = '```json\n{"claim": "AdamW decouples weight decay"}\n```'
    assert _parse_json(text, "groq") == {"claim": "AdamW decouples weight decay"}


def test_parse_json_reads_an_object_surrounded_by_prose():
    text = ('Sure — here is the object you asked for:\n'
            '{"claim": "the logits are divided by sqrt(d_k)", "confidence": 0.9}\n'
            'Let me know if you want the other sections too.')
    assert _parse_json(text, "groq")["confidence"] == 0.9


def test_parse_json_raises_rather_than_inventing_an_object():
    """A half-parsed extraction looks like a complete one downstream, which is why
    unparseable output is a `ModelError` and not an empty dict."""
    with pytest.raises(ModelError):
        _parse_json("I have thought about it and would rather not.", "groq")


def test_a_short_decline_is_read_as_a_refusal():
    assert _refused(a_response("I'm sorry, I can't help with that."),
                    "I'm sorry, I can't help with that.") is True


def test_an_explicit_refusal_field_is_read_as_a_refusal():
    """The spec has a dedicated field; a server that sets it is believed regardless of
    what the prose happens to say."""
    data = a_response("", refusal="This request violates the usage policy.")
    assert _refused(data, "") is True


def test_a_long_answer_that_merely_says_i_cant_is_still_an_answer():
    """The narrow rule is the point, not a limitation.

    A model summarising a page about rate limits will write "I can't" somewhere in six
    hundred characters of perfectly good analysis. Treating that as a refusal would
    throw away a real extraction and mark the source unprocessed — a silent data loss
    that only shows up as a gap in the knowledge base weeks later. So: a decline is
    short and opens with one, and everything longer is an answer.
    """
    text = (
        "The paper argues that scaled dot-product attention divides the logits by the "
        "square root of the key dimension in order to keep gradient variance stable as "
        "the dimension grows. I can't reproduce the layer-9 ablation from the figures "
        "alone, because the appendix omits the exact learning-rate schedule, but the "
        "reported numbers are consistent with the claim in section four. The authors "
        "also note that removing the scaling term destabilises training above a key "
        "dimension of roughly one hundred and twenty-eight, which matches the folklore."
    )
    assert len(text) > 400
    assert _refused(a_response(text), text) is False


def test_cached_tokens_are_not_billed_twice():
    """`prompt_tokens` already includes the cache hits, so counting both would price the
    same tokens at the full rate and the cache rate at once."""
    usage = _usage({"usage": {"prompt_tokens": 10_000, "completion_tokens": 300,
                              "prompt_tokens_details": {"cached_tokens": 8_000}}})
    assert usage.input == 2_000
    assert usage.cache_read == 8_000
    assert usage.output == 300


def test_usage_is_zero_when_the_response_reports_none():
    """Plenty of local servers omit the block entirely. Zeroes, not an exception."""
    usage = _usage(a_response("{}"))
    assert usage == TokenUsage(0, 0, 0, 0)


# ---------------------------------------------------------------------------
# degradation: "any model works" means models that cannot do everything
# ---------------------------------------------------------------------------


def test_json_falls_back_from_json_schema_to_json_object(monkeypatch):
    """The caller gets its dict either way — which is exactly why this needs a test."""
    provider = a_provider("gpt-5")
    posts = scripted(provider, monkeypatch,
                     _Unsupported("response_format.json_schema is not supported"),
                     a_response('{"claim": "attention scales by 1/sqrt(d_k)"}'))

    parsed, _ = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert parsed == {"claim": "attention scales by 1/sqrt(d_k)"}
    assert posts.calls == 2
    assert posts.bodies[0]["response_format"]["type"] == "json_schema"
    assert posts.bodies[1]["response_format"] == {"type": "json_object"}


def test_the_fallback_carries_the_schema_in_the_user_prompt(monkeypatch):
    """`json_object` guarantees valid JSON, not the right shape. The shape has to move
    into the prompt or the second attempt returns a well-formed wrong answer."""
    provider = a_provider("gpt-5")
    posts = scripted(provider, monkeypatch,
                     _Unsupported("invalid schema"),
                     a_response('{"claim": "x"}'))

    provider.json(system="s", prompt="extract the claims", schema=SCHEMA)

    sent = posts.bodies[1]["messages"][1]["content"]
    assert sent.startswith("extract the claims")
    assert json.dumps(SCHEMA, ensure_ascii=False) in sent


def test_a_degraded_provider_never_tries_json_schema_again(monkeypatch):
    """One failed request per process, not one per call.

    The capability flag is demoted permanently on the first rejection. Without that, a
    thousand-claim ingest against a server with no strict mode pays a wasted round trip
    a thousand times, and logs the same warning a thousand times.
    """
    provider = a_provider("gpt-5")
    posts = scripted(provider, monkeypatch,
                     _Unsupported("response_format is not supported"),
                     a_response('{"claim": "x"}'))

    provider.json(system="s", prompt="p", schema=SCHEMA)
    assert provider._schema_ok is False

    provider.json(system="s", prompt="p", schema=SCHEMA)

    assert posts.calls == 3                                   # 2 + 1, not 2 + 2
    # Down one rung, not off the ladder: `json_object` still guarantees parseable
    # output, and only a server that rejects that too gets plain text.
    assert posts.bodies[2]["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(posts.bodies[2])


def test_a_400_about_the_parameter_is_told_apart_from_one_about_the_input():
    """Degrading on a rejection that was actually the user's fault would hide a real
    error behind a permanently downgraded capability."""
    assert _about_a_parameter(
        "Invalid value: 'response_format.type' does not support 'json_schema'.") is True


def test_a_400_about_the_users_input_is_not_a_parameter_problem():
    assert _about_a_parameter(
        "Invalid 'messages[1].content': string too long. Expected a maximum length of "
        "256000 characters, but got 391204 instead.") is False


def test_reasoning_effort_is_sent_to_a_model_that_reasons(monkeypatch):
    provider = a_provider("gpt-5")
    posts = scripted(provider, monkeypatch, a_response('{"claim": "x"}'))

    provider.json(system="s", prompt="p", schema=SCHEMA, effort="high")
    assert posts.bodies[0]["reasoning_effort"] == "high"


def test_reasoning_effort_is_omitted_for_a_model_that_does_not(monkeypatch):
    """Sending it anyway is a 400 on every single call, for a parameter the model was
    never going to honour."""
    provider = a_provider("llama-3.3-70b-versatile", name="groq")
    posts = scripted(provider, monkeypatch, a_response('{"claim": "x"}'))

    provider.json(system="s", prompt="p", schema=SCHEMA, effort="high")
    assert "reasoning_effort" not in posts.bodies[0]


# ---------------------------------------------------------------------------
# conversation shape
# ---------------------------------------------------------------------------


def test_each_tool_result_is_its_own_message_keyed_by_call_id():
    """OpenAI wants one `role: "tool"` message per call, matched to the call by id.

    Merging them into a single message — which is how Anthropic's side looks — produces
    a request the server rejects, or worse, accepts while attributing every result to
    the first tool call.
    """
    convo = OpenAIConversation()
    convo.user("what do my notes say about attention?")
    convo.assistant(Reply(
        raw={"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "search_notes", "arguments": '{"query": "attention"}'}},
            {"id": "call_b", "type": "function",
             "function": {"name": "read_page", "arguments": '{"page_id": "pg_a"}'}},
        ]},
        tool_calls=[ToolCall(id="call_a", name="search_notes", arguments={}),
                    ToolCall(id="call_b", name="read_page", arguments={})],
        stop="tools"))
    convo.results([ToolResult(id="call_a", payload={"count": 1}),
                   ToolResult(id="call_b", payload={"error": "no such page"},
                              is_error=True)])

    wire = convo.wire()
    assert [m["role"] for m in wire] == ["user", "assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in wire[2:]] == ["call_a", "call_b"]
    assert json.loads(wire[2]["content"]) == {"count": 1}


def test_the_assistants_own_turn_goes_back_on_the_wire_verbatim():
    """`raw` carries the `tool_calls` ids the follow-up messages must reference. A turn
    rebuilt from `reply.text` loses them, and the tool results then match nothing."""
    raw = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "call_z", "type": "function",
                           "function": {"name": "list_pending", "arguments": "{}"}}]}
    convo = OpenAIConversation()
    convo.assistant(Reply(text="", raw=raw, stop="tools"))

    assert convo.wire() == [raw]


def test_a_reply_without_raw_still_lands_as_a_plain_assistant_turn():
    convo = OpenAIConversation()
    convo.assistant(Reply(text="Found it in your Attention page.", stop="end"))

    assert convo.wire() == [{"role": "assistant",
                             "content": "Found it in your Attention page."}]


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


def test_rate_for_matches_the_longest_prefix():
    """`gpt-5-mini` starts with `gpt-5`. Matching in dict order would price the cheap
    model at five times its rate, in the direction that looks plausible."""
    assert rate_for("gpt-5-mini") == (0.25, 2.0)
    assert rate_for("gpt-5") == (1.25, 10.0)
    assert rate_for("gpt-5.1-2026-03-02") == (1.25, 10.0)


def test_an_unknown_model_has_no_price_rather_than_a_free_one():
    """A fabricated $0.00 is misleading in both directions: it makes an expensive model
    that this table has not heard of look free, and it is indistinguishable from a
    genuinely free local one. `None` says "not known", which is the true answer."""
    assert rate_for("some-new-frontier-model-2027") is None
    assert cost_usd("some-new-frontier-model-2027",
                    input_tokens=1_000_000, output_tokens=500_000) is None

    usage = Usage(calls=1, input_tokens=1_000_000, output_tokens=500_000)
    assert "cost unknown" in usage.summary("some-new-frontier-model-2027")


def test_a_local_endpoint_costs_nothing_whatever_the_model_is_called():
    """Ollama serving a model named like a paid one must not invoice for it."""
    assert rate_for("claude-opus-5", base_url="http://localhost:11434/v1") == (0.0, 0.0)
    assert rate_for("gpt-5", base_url="http://127.0.0.1:8000/v1") == (0.0, 0.0)
    assert cost_usd("gpt-5", input_tokens=1_000_000, output_tokens=1_000_000,
                    base_url="http://localhost:11434/v1") == 0.0


def test_usage_accumulates_across_calls_and_tracks_each_task():
    usage = Usage()
    usage.add("extract", TokenUsage(input=1000, output=200, cache_read=50), 1.5)
    usage.add("extract", TokenUsage(input=500, output=100), 0.5)
    usage.add("classify", TokenUsage(input=300, output=60, cache_write=10), 0.25)

    assert usage.calls == 3
    assert usage.input_tokens == 1800
    assert usage.output_tokens == 360
    assert usage.cache_read == 50
    assert usage.cache_write == 10
    assert usage.by_task == {"extract": 2, "classify": 1}
    assert usage.seconds == pytest.approx(2.25)


# ---------------------------------------------------------------------------
# the façade
# ---------------------------------------------------------------------------


def test_the_facade_routes_json_through_the_provider_and_books_the_usage():
    provider = FakeProvider(result={"claim": "attention scales by 1/sqrt(d_k)"})
    model = Model(provider=provider)

    parsed = model.json(task="extract", system="s", prompt="p", schema=SCHEMA,
                        effort="medium")

    assert parsed == {"claim": "attention scales by 1/sqrt(d_k)"}
    assert provider.seen["schema"] is SCHEMA
    assert provider.seen["effort"] == "medium"
    assert model.usage.calls == 1
    assert model.usage.by_task == {"extract": 1}
    assert model.usage.input_tokens == 1000


def test_a_model_error_counts_as_a_refusal_and_still_propagates():
    """Accounting must not depend on the happy path: a run that spent money and got
    nothing back should say so, and the caller must still see the failure rather than a
    half-processed source."""
    model = Model(provider=FakeProvider(error=ModelError("the model declined")))

    with pytest.raises(ModelError):
        model.json(task="extract", system="s", prompt="p", schema=SCHEMA)

    assert model.usage.refusals == 1
    assert model.usage.calls == 0


def test_a_rejection_with_nothing_left_to_give_up_becomes_a_model_error():
    """The degradation ladder must end in `ModelError`, not in a bare RuntimeError.

    `_Unsupported` is an internal signal meaning "climb down a rung". Every caller in the
    pipeline catches `ModelError` and records the source as unprocessed; letting the
    internal exception escape instead crashed the ingest, which is the one outcome the
    error handling exists to prevent.
    """
    provider = a_provider("gpt-5")
    provider._effort_ok = False
    provider._schema_ok = False
    provider._json_object_ok = False

    def always_refuses(*_a, **_k):
        raise _Unsupported("response_format is not supported")

    provider._post = always_refuses                              # type: ignore[method-assign]
    with pytest.raises(ModelError):
        provider.json(system="s", prompt="p", schema=SCHEMA)


def test_a_rejected_reasoning_effort_demotes_effort_not_the_schema(monkeypatch):
    """Demote what the server actually named.

    A 400 about `reasoning_effort` used to be read as a schema problem: structured output
    was switched off, the offending parameter stayed in the body, and every rung of the
    ladder then failed for the same unaddressed reason. The model would have worked fine
    with the parameter simply dropped.
    """
    provider = a_provider("gpt-5")
    assert provider._effort_ok is True
    posts = scripted(provider, monkeypatch,
                     _Unsupported("Unsupported parameter: 'reasoning_effort'"),
                     a_response('{"claim": "x"}'))

    out, _usage_out = provider.json(system="s", prompt="p", schema=SCHEMA)

    assert out == {"claim": "x"}
    assert provider._effort_ok is False
    assert provider._schema_ok is True            # structured output was never at fault
    assert "reasoning_effort" not in posts.bodies[1]
    assert posts.bodies[1]["response_format"]["type"] == "json_schema"
