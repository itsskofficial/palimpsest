"""Anthropic's Messages API, native rather than through its OpenAI-compatible shim.

Anthropic serves an OpenAI-compatible endpoint, so this file could have been deleted when
the compatible provider arrived. It was not, because three things palimpsest actually uses
only exist on the native API:

**Explicit cache breakpoints.** The classifier sends the same page context with every
claim in a source. Marking that prefix cached turns the per-claim cost into roughly the
tokens of the claim itself. OpenAI-compatible servers cache automatically and well, but
they cache what they choose; here we say where the boundary is.

**Server-side fallbacks on refusal.** A declined request is re-served on another model
inside the same call, so one awkward sentence in a PDF does not end the ingest.

**Server-side web search.** The agent's `web_search` runs on Anthropic's infrastructure
and returns results in the same response, with nothing to execute locally.

Lose those and the product still works — that is the point of the provider split — but it
costs more, stops more often, and the agent has one fewer tool.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from palimpsest.llm.base import (
    Conversation,
    ModelError,
    Reply,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSpec,
)

log = logging.getLogger("palimpsest.llm.anthropic")

__all__ = ["AnthropicConversation", "AnthropicProvider"]

#: The server-side web search tool. Runs on Anthropic's infrastructure — the model uses
#: it and results return in the same response, so there is nothing to execute locally.
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}


class AnthropicConversation:
    """A `messages` array in Anthropic's shape.

    Tool results are content blocks inside a single *user* message, which is the detail
    that makes this provider-owned: OpenAI wants one message per result instead.
    """

    def __init__(self, history: list[dict] | None = None) -> None:
        self._messages: list[dict] = []
        for turn in history or []:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                self._messages.append({"role": turn["role"], "content": turn["content"]})

    def user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def assistant(self, reply: Reply) -> None:
        # The raw content blocks, thinking included. Reconstructing this from `text` and
        # `tool_calls` would drop the model's own reasoning and break the next request.
        self._messages.append({"role": "assistant", "content": reply.raw})

    def results(self, results: list[ToolResult]) -> None:
        import json

        self._messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.id, "is_error": r.is_error,
             "content": (r.payload if isinstance(r.payload, str)
                         else json.dumps(r.payload, ensure_ascii=False,
                                         default=str))[:60000]}
            for r in results
        ]})

    def wire(self) -> list[dict]:
        return self._messages


class AnthropicProvider:
    """The native Messages API."""

    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5", *, api_key: str | None = None,
                 fallbacks: bool = True, max_retries: int = 3) -> None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - optional extra
            raise ImportError(
                "the Anthropic provider needs its SDK: pip install "
                "'palimpsest-notion[anthropic]'. Any OpenAI-compatible endpoint works "
                "with no extra install — set PALIMPSEST_MODEL_BASE_URL."
            ) from e
        if not api_key:
            raise ModelError("no ANTHROPIC_API_KEY for the Anthropic provider")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self.model = model
        self.fallbacks = fallbacks
        #: Cleared once a beta parameter is rejected, so we stop paying for the retry.
        self._beta_ok = True

    @property
    def can_see(self) -> bool:
        return True

    @property
    def can_search_web(self) -> bool:
        return True

    # -- structured output -----------------------------------------------------

    def json(self, *, system: str, prompt: str, schema: dict, effort: str = "high",
             cache_prefix: str | None = None,
             max_tokens: int = 16_000) -> tuple[dict, TokenUsage]:
        """Ask for one JSON object matching `schema`, and return it parsed.

        `cache_prefix` is content that repeats across calls — the page context handed to
        the classifier, for instance. It goes in the system block behind a cache
        breakpoint, so the second and subsequent claims from the same source read it at
        roughly a tenth of the price. Volatile content (the claim itself) always goes in
        the user turn, after the breakpoint, or the cache never hits.
        """
        import json as jsonlib

        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_prefix:
            system_blocks.append({
                "type": "text",
                "text": cache_prefix,
                # The breakpoint goes on the LAST stable block. Everything after it —
                # i.e. the user turn — is free to vary per call.
                "cache_control": {"type": "ephemeral"},
            })
        else:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        response = self._call({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": effort,
                              "format": {"type": "json_schema", "schema": schema}},
        })

        # Always check stop_reason before touching content: a refusal is HTTP 200 with an
        # empty or partial content array.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelError(
                f"the model declined this request (category={category or 'unspecified'}). "
                "The source was not processed. Nothing was written to Notion."
            )

        text = _text_of(response.content)
        if not text.strip():
            raise ModelError("empty response (stop_reason="
                             f"{getattr(response, 'stop_reason', '?')})")
        try:
            parsed = jsonlib.loads(text)
        except ValueError as e:
            # With output_config.format this should be unreachable; if the schema is ever
            # dropped it becomes the failure mode, so it fails loudly.
            raise ModelError(f"response was not valid JSON: {text[:200]}") from e
        if not isinstance(parsed, dict):
            raise ModelError(f"expected a JSON object, got {type(parsed).__name__}")
        return parsed, _usage(response)

    # -- conversation ----------------------------------------------------------

    def conversation(self, history: list[dict] | None = None) -> Conversation:
        return AnthropicConversation(history)

    def converse(self, *, system: str, conversation: Conversation,
                 tools: list[ToolSpec], effort: str = "high", max_tokens: int = 16_000,
                 thinking: bool = True, web_search: bool = False) -> Reply:
        specs: list[dict] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        if web_search:
            specs.append(dict(WEB_SEARCH))

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": conversation.wire(),
            "output_config": {"effort": effort},
        }
        if specs:
            params["tools"] = specs
        if thinking:
            params["thinking"] = {"type": "adaptive", "display": "summarized"}

        # Tools and server-side fallbacks together are more surface than we need here,
        # and a refusal in a conversational turn is handled by the loop, not by
        # re-serving on another model.
        response = self.client.messages.create(**params)
        content = list(response.content)

        calls = [
            ToolCall(id=b.id, name=b.name,
                     arguments=b.input if isinstance(b.input, dict) else {})
            for b in content if getattr(b, "type", None) == "tool_use"
        ]
        raw_stop = getattr(response, "stop_reason", None)
        if raw_stop == "refusal":
            stop = "refusal"
        elif calls:
            stop = "tools"
        elif raw_stop == "pause_turn":
            # A server tool ran mid-flight and the model has more to say. Continue.
            stop = "pause"
        elif raw_stop == "max_tokens":
            stop = "length"
        else:
            stop = "end"

        return Reply(text=_text_of(content), tool_calls=calls, stop=stop,
                     usage=_usage(response), raw=content)

    # -- transport -------------------------------------------------------------

    def _call(self, params: dict) -> Any:
        """Prefer the beta endpoint so refusals fall back; degrade if unsupported.

        `fallbacks="default"` lets Anthropic route a declined request to the recommended
        substitute model by refusal category, rather than us pinning one that later gets
        deprecated. If the installed SDK or the account does not have the beta, we drop to
        the plain endpoint once and remember.
        """
        if self.fallbacks and self._beta_ok:
            try:
                return self.client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default")
            except TypeError:
                self._beta_ok = False
                log.info("installed SDK does not accept server-side fallbacks; "
                         "using the standard endpoint")
            except self._anthropic.BadRequestError as e:
                self._beta_ok = False
                log.info("server-side fallbacks unavailable (%s); using the standard "
                         "endpoint", str(e)[:120])
        return self.client.messages.create(**params)

    # -- vision ----------------------------------------------------------------

    def describe_image(self, image_bytes: bytes, media_type: str, prompt: str,
                       max_tokens: int = 4000) -> tuple[str, TokenUsage]:
        """Read an image: a whiteboard, a slide, a screenshot, a photographed page."""
        # Built as a plain list first: the SDK types these blocks as TypedDicts, and
        # inlining the literal makes the checker infer `dict[str, Collection[str]]` for
        # the image block and reject it.
        content: list[Any] = [
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            }},
            {"type": "text", "text": prompt},
        ]
        response = self.client.messages.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}])
        if getattr(response, "stop_reason", None) == "refusal":
            raise ModelError("the model declined to read this image")
        return _text_of(response.content), _usage(response)


def _text_of(content: Any) -> str:
    """Concatenate the text blocks of a response.

    `getattr` rather than `b.text`: the content union has a dozen block types and only
    some carry `.text`. Filtering on `.type` is correct at runtime but does not narrow
    the union for a type checker.
    """
    return "".join(getattr(b, "text", "") for b in content
                   if getattr(b, "type", None) == "text")


def _usage(response: Any) -> TokenUsage:
    raw = getattr(response, "usage", None)
    if raw is None:
        return TokenUsage()
    return TokenUsage(
        input=int(getattr(raw, "input_tokens", 0) or 0),
        output=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        cache_write=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
    )
