"""Any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter, vLLM, Ollama, LM Studio.

One provider covers all of them because they all speak `POST /chat/completions` with the
same body. What changes between them is a base URL, a key and a model name — three
settings, not three integrations.

Written against `urllib` rather than the `openai` SDK, which is the same choice the
Notion client and the transcription adapters made. The wire format is a stable, widely
implemented spec; taking an SDK for it would add a dependency, pin a version, and buy
retry logic this module needs to own anyway because it has to distinguish "retry this"
from "this model cannot do that, degrade".

**Three degradations, because "any model" means models that cannot do everything.**

*Structured output.* The best case is `response_format: json_schema` with `strict`, which
guarantees the shape. Not every server implements it, and strict mode additionally
demands that every object forbid extra properties and mark every property required —
which our schemas, written for Anthropic, do not. So: rewrite the schema to satisfy
strict mode, try it, and on rejection fall back to `json_object` with the schema in the
prompt, and finally to plain text that we parse out of a code fence. Each step down is
logged once, not per call.

*Reasoning effort.* Anthropic takes `effort` on every model. Here it is `reasoning_effort`
and only some models accept it, so it is sent only to models known to reason and dropped
on rejection.

*Vision and web search.* Advertised honestly rather than attempted. A text-only model
says it cannot see, and the caller falls back to OCR or skips the image; no provider here
runs web search server-side, so the agent simply does not offer that tool.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
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

log = logging.getLogger("palimpsest.llm.openai")

__all__ = ["OpenAIConversation", "OpenAIProvider"]

USER_AGENT = "palimpsest/0.1 (+https://github.com/itsskofficial/palimpsest)"

#: Models that accept `reasoning_effort`. Everything else gets the parameter dropped
#: rather than a 400 on every call.
_REASONS = ("o1", "o3", "o4", "gpt-5", "deepseek-r", "qwq", "magistral")

#: Model families known to accept image content blocks.
_SEES = ("gpt-4o", "gpt-4.1", "gpt-5", "llava", "qwen2-vl", "qwen2.5-vl", "pixtral",
         "llama-4", "gemma-3", "scout", "maverick")

#: Effort levels the OpenAI spelling allows. Anthropic's `high` maps straight across;
#: anything unrecognised becomes `medium` rather than failing the call.
_EFFORTS = {"low", "medium", "high"}


class OpenAIConversation:
    """A `messages` array in OpenAI's shape.

    Tool results are separate `role: "tool"` messages here, one per call, keyed by
    `tool_call_id` — which is the main reason the loop cannot own this list itself.
    """

    def __init__(self, history: list[dict] | None = None) -> None:
        self._messages: list[dict] = []
        for turn in history or []:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                self._messages.append({"role": turn["role"], "content": turn["content"]})

    def user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def assistant(self, reply: Reply) -> None:
        # `raw` is the server's own message object, which carries the exact `tool_calls`
        # ids the follow-up messages must reference.
        self._messages.append(reply.raw or {"role": "assistant", "content": reply.text})

    def results(self, results: list[ToolResult]) -> None:
        for r in results:
            self._messages.append({
                "role": "tool",
                "tool_call_id": r.id,
                "content": _dump(r.payload)[:60000],
            })

    def wire(self) -> list[dict]:
        return self._messages


class OpenAIProvider:
    """One OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        name: str = "openai",
        timeout: float = 180.0,
        max_retries: int = 3,
    ) -> None:
        self.model = model
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._key = api_key
        #: Capability flags, each demoted permanently the first time the server says no,
        #: so a degraded setup costs one failed request rather than one per call.
        self._schema_ok = True
        self._json_object_ok = True
        self._effort_ok = any(m in model.lower() for m in _REASONS)

    # -- capabilities ----------------------------------------------------------

    @property
    def can_see(self) -> bool:
        return any(m in self.model.lower() for m in _SEES)

    @property
    def can_search_web(self) -> bool:
        # No OpenAI-compatible server runs a search tool the way Anthropic's
        # `web_search` does. The agent offers its own tools instead.
        return False

    # -- structured output -----------------------------------------------------

    def json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict,
        effort: str = "high",
        cache_prefix: str | None = None,
        max_tokens: int = 16_000,
    ) -> tuple[dict, TokenUsage]:
        # No explicit cache breakpoints here: OpenAI and Groq both cache long prefixes
        # automatically, so the stable part simply goes first and is left to them.
        system_text = f"{system}\n\n{cache_prefix}" if cache_prefix else system

        body: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": prompt},
            ],
        }
        # Climb down one rung at a time, and let the *server* say which rung failed.
        #
        # An earlier version assumed every rejected parameter was the schema, so a 400
        # about `reasoning_effort` switched off structured output, left the offending
        # parameter in the body, and failed again for the same reason at every level. A
        # rejection now demotes whichever capability the error actually names, and the
        # demotion sticks on the provider so a degraded endpoint costs one failed request
        # rather than one per call for the life of the process.
        data: dict | None = None
        last: _Unsupported | None = None
        for _attempt in range(4):
            self._build(body, prompt, schema, effort)
            try:
                data = self._post("/chat/completions", body)
                break
            except _Unsupported as e:
                last = e
                if not self._demote(str(e)):
                    # Nothing left to give up, so this is not a capability problem.
                    raise ModelError(f"{self.name} rejected the request: {e}") from e
        if data is None:
            raise ModelError(f"{self.name} rejected the request: {last}")

        message = _first_message(data)
        text = (message.get("content") or "").strip()
        if _refused(data, text):
            raise ModelError(
                "the model declined this request. The source was not processed. "
                "Nothing was written to Notion."
            )
        if not text:
            raise ModelError(f"empty response from {self.name}/{self.model}")
        return _parse_json(text, self.name), _usage(data)

    # -- conversation ----------------------------------------------------------

    def conversation(self, history: list[dict] | None = None) -> Conversation:
        return OpenAIConversation(history)

    def converse(
        self,
        *,
        system: str,
        conversation: Conversation,
        tools: list[ToolSpec],
        effort: str = "high",
        max_tokens: int = 16_000,
        thinking: bool = True,
        web_search: bool = False,
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *conversation.wire()],
        }
        self._apply_effort(body, effort)
        if tools:
            body["tools"] = [{
                "type": "function",
                "function": {"name": t.name, "description": t.description,
                             "parameters": t.input_schema},
            } for t in tools]

        try:
            data = self._post("/chat/completions", body)
        except _Unsupported as e:
            # Same rule as `json()`: the server named a parameter it will not take, so
            # give that one up and try once more rather than failing the whole turn.
            if not self._demote(str(e)):
                raise ModelError(f"{self.name} rejected the request: {e}") from e
            body.pop("reasoning_effort", None)
            self._apply_effort(body, effort)
            data = self._post("/chat/completions", body)

        message = _first_message(data)
        text = (message.get("content") or "").strip()

        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                # A malformed argument string is the model's mistake, not a transport
                # failure. Pass an empty dict; the tool will report what it needed and
                # the model gets a chance to correct itself.
                log.warning("%s: unparseable tool arguments for %s", self.name,
                            fn.get("name"))
                args = {}
            calls.append(ToolCall(id=call.get("id") or "", name=fn.get("name") or "",
                                  arguments=args if isinstance(args, dict) else {}))

        finish = (_first_choice(data).get("finish_reason") or "").lower()
        if calls:
            stop = "tools"
        elif finish == "length":
            stop = "length"
        elif _refused(data, text):
            stop = "refusal"
        else:
            stop = "end"

        return Reply(text=text, tool_calls=calls, stop=stop, usage=_usage(data),
                     raw=message)

    # -- vision ----------------------------------------------------------------

    def describe_image(
        self, image_bytes: bytes, media_type: str, prompt: str, max_tokens: int = 4000
    ) -> tuple[str, TokenUsage]:
        if not self.can_see:
            raise ModelError(
                f"{self.name}/{self.model} is a text-only model, so it cannot read an "
                "image. Set PALIMPSEST_MODEL to one that can see, or send the text."
            )
        import base64

        uri = (f"data:{media_type};base64,"
               f"{base64.standard_b64encode(image_bytes).decode('ascii')}")
        data = self._post("/chat/completions", {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": prompt},
            ]}],
        })
        return (_first_message(data).get("content") or ""), _usage(data)

    # -- degradation -----------------------------------------------------------

    def _build(self, body: dict, prompt: str, schema: dict, effort: str) -> None:
        """Fill in the optional parts of a request at whatever level we are down to."""
        body.pop("reasoning_effort", None)
        body.pop("response_format", None)
        self._apply_effort(body, effort)
        if self._schema_ok:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True,
                                "schema": strictify(schema)},
            }
            body["messages"][1]["content"] = prompt
        else:
            # No enforced schema, so the shape goes in the prompt where the model can
            # read it, and `json_object` at least guarantees parseable output.
            if self._json_object_ok:
                body["response_format"] = {"type": "json_object"}
            body["messages"][1]["content"] = (
                f"{prompt}\n\nReply with one JSON object matching this schema exactly:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )

    def _demote(self, detail: str) -> bool:
        """Give up the capability this error names. False when there is none left."""
        low = detail.lower()
        if "reasoning_effort" in low and self._effort_ok:
            self._effort_ok = False
            log.info("%s: %s does not take reasoning_effort", self.name, self.model)
            return True
        if self._schema_ok:
            self._schema_ok = False
            log.info("%s: json_schema not accepted (%s); asking for json_object",
                     self.name, detail[:120])
            return True
        if self._json_object_ok:
            self._json_object_ok = False
            log.info("%s: json_object not accepted either; parsing plain text", self.name)
            return True
        return False

    # -- transport -------------------------------------------------------------

    def _apply_effort(self, body: dict, effort: str) -> None:
        if self._effort_ok:
            body["reasoning_effort"] = effort if effort in _EFFORTS else "medium"

    def _post(self, path: str, body: dict) -> dict:
        """POST with retries. Distinguishes "wait and try again" from "never going to work"."""
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"

        last: Exception | None = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(
                f"{self.base_url}{path}", data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:600]
                if e.code in (400, 422) and _about_a_parameter(detail):
                    # The request is wrong, not unlucky. Let the caller degrade.
                    raise _Unsupported(detail) from e
                if e.code in (408, 409, 429) or e.code >= 500:
                    last = ModelError(f"{self.name} returned {e.code}: {detail}")
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise ModelError(f"{self.name} returned {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                last = ModelError(f"could not reach {self.base_url}: {e.reason}")
                time.sleep(min(2 ** attempt, 8))
            except TimeoutError as e:
                last = ModelError(f"{self.name} timed out after {self.timeout:.0f}s")
                time.sleep(min(2 ** attempt, 8))
                del e
        raise last or ModelError(f"{self.name}: request failed")


class _Unsupported(RuntimeError):
    """The server rejected a parameter. Retrying is pointless; degrading is not."""


# ---------------------------------------------------------------------------
# schema and response handling
# ---------------------------------------------------------------------------

#: Phrases in a 400 body that mean "this parameter", rather than "your input".
_PARAM_HINTS = ("response_format", "json_schema", "reasoning_effort", "tools",
                "unsupported", "not supported", "unrecognized", "unknown parameter",
                "invalid schema", "additionalproperties", "'strict'")


def _about_a_parameter(detail: str) -> bool:
    low = detail.lower()
    return any(hint in low for hint in _PARAM_HINTS)


def strictify(schema: Any) -> Any:
    """Rewrite a JSON schema to satisfy OpenAI's strict mode.

    Strict mode demands two things our Anthropic-shaped schemas do not provide: every
    object must set `additionalProperties: false`, and every property must appear in
    `required`. The second sounds destructive and is not — strict mode allows a nullable
    type, so an optional field becomes a required one that may be null, which is the same
    contract with the burden moved from the model to the schema.
    """
    if isinstance(schema, list):
        return [strictify(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out = {k: strictify(v) for k, v in schema.items()}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        names = list(out["properties"])
        was_required = set(out.get("required") or names)
        out["required"] = names
        for name in names:
            if name in was_required:
                continue
            prop = out["properties"][name]
            if isinstance(prop, dict) and "type" in prop:
                kind = prop["type"]
                types = kind if isinstance(kind, list) else [kind]
                if "null" not in types:
                    prop["type"] = [*types, "null"]
    return out


def _first_choice(data: dict) -> dict:
    choices = data.get("choices") or []
    if not choices:
        raise ModelError(f"no choices in response: {json.dumps(data)[:300]}")
    return choices[0] or {}


def _first_message(data: dict) -> dict:
    return _first_choice(data).get("message") or {}


def _refused(data: dict, text: str) -> bool:
    """Whether this response is a refusal rather than an answer.

    The spec has a dedicated `refusal` field; servers that do not implement it signal the
    same thing in prose, so a short reply that opens with a decline counts too. Kept
    narrow deliberately: a long answer that happens to contain "I can't" is an answer.
    """
    if _first_message(data).get("refusal"):
        return True
    if len(text) > 400:
        return False
    return bool(re.match(r"^(i'?m sorry|i can(no|')t|i am unable|i won'?t)\b",
                         text.strip().lower()))


_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def _parse_json(text: str, provider: str) -> dict:
    """Parse the object out of a reply, tolerating a code fence around it.

    Only reached on the degraded paths — with `json_schema` accepted, the body is already
    an object. A model asked for JSON in prose fences it about a third of the time.
    """
    for candidate in (text, *(m.group(1) for m in _FENCE.finditer(text))):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    # Last resort: the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    raise ModelError(f"{provider}: response was not valid JSON: {text[:200]}")


def _usage(data: dict) -> TokenUsage:
    raw = data.get("usage") or {}
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    return TokenUsage(
        # `prompt_tokens` includes cached tokens, so subtract them out to avoid
        # billing the same tokens twice at two different rates.
        input=max(0, int(raw.get("prompt_tokens") or 0) - cached),
        output=int(raw.get("completion_tokens") or 0),
        cache_read=cached,
    )


def _dump(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)
