"""The model layer: one façade, any provider, honest costs.

Everything in palimpsest that needs a model goes through `Model`. Below it sits a
`Provider` — Anthropic's native API, or any OpenAI-compatible endpoint — and the choice
is configuration, not code. Above it, nothing knows or cares which one is in use.

That split is not architecture for its own sake. A knowledge base you feed for two years
outlives any particular model, and the pipeline's judgement calls — is this claim new, or
does it contradict page 41? — are the kind of task where you want to re-run last month's
decisions against a better model, or a cheaper one, without touching the code that made
them. It also means the product works for someone with a Groq key, an Ollama container,
or an OpenRouter account, none of whom should have to care that it was written against
Claude.

**Resolution order** — the policy itself lives in `config.describe_setup`, because the
store layer must be able to read settings without dragging the model code in with it.
When nothing is set explicitly:

1. `PALIMPSEST_MODEL_BASE_URL` — anything OpenAI-compatible, and the escape hatch for
   endpoints this file has never heard of.
2. `ANTHROPIC_API_KEY` → the native Anthropic provider.
3. `OPENAI_API_KEY` → OpenAI.
4. `GROQ_API_KEY` → Groq.
5. `OPENROUTER_API_KEY` → OpenRouter.
6. Nothing. Which is a supported state: the mirror, retrieval, the duplicate sweep and
   undo all work with no model at all, and `available()` says so rather than failing at
   the first call.

`Model` owns the two things that should not be duplicated per provider: **usage
accounting**, so `palimpsest ingest` can print what a source cost instead of leaving you
to find out at the end of the month, and **tracing**, so every call lands in Langfuse
with the same shape whoever served it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from palimpsest.config import (
    MODEL_PROVIDERS,
    ModelSetup,
    describe_setup,
    model_getter,
)
from palimpsest.llm.base import (
    Conversation,
    ModelError,
    Provider,
    Reply,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from palimpsest.llm.pricing import cost_usd

__all__ = [
    "Conversation",
    "Model",
    "ModelError",
    "ModelSetup",
    "Provider",
    "Reply",
    "TokenUsage",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "available",
    "describe_setup",
    "resolve",
]

#: `describe_setup` and `ModelSetup` are re-exported from `config`, which owns the
#: resolution policy — see the note on `config.MODEL_PROVIDERS` for why it lives there.

log = logging.getLogger("palimpsest.llm")

# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Token and cost accounting for one process."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    refusals: int = 0
    seconds: float = 0.0
    by_task: dict[str, int] = field(default_factory=dict)
    #: Claims are classified concurrently, so several threads reach `add` at once and
    #: `self.calls += 1` is a read-modify-write that quietly loses increments under
    #: contention. The numbers this guards are the ones printed as "what did this cost",
    #: so an undercount is a bill that looks smaller than it was.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, task: str, usage: TokenUsage, seconds: float) -> None:
        with self._lock:
            self.calls += 1
            self.seconds += seconds
            self.by_task[task] = self.by_task.get(task, 0) + 1
            self.input_tokens += usage.input
            self.output_tokens += usage.output
            self.cache_read += usage.cache_read
            self.cache_write += usage.cache_write

    def cost_usd(self, model: str, base_url: str | None = None) -> float | None:
        """Estimated spend, or `None` when this model's price is not published."""
        return cost_usd(model, input_tokens=self.input_tokens,
                        output_tokens=self.output_tokens, cache_read=self.cache_read,
                        cache_write=self.cache_write, base_url=base_url)

    def as_dict(self, model: str = "", base_url: str | None = None) -> dict:
        return {
            "calls": self.calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "cache_read": self.cache_read,
            "cache_write": self.cache_write, "refusals": self.refusals,
            "seconds": round(self.seconds, 1), "by_task": self.by_task,
            "estimated_cost_usd": _round(self.cost_usd(model, base_url)),
        }

    def summary(self, model: str = "", base_url: str | None = None) -> str:
        cost = self.cost_usd(model, base_url)
        money = f"~${cost:.3f}" if cost is not None else "cost unknown"
        return (f"{self.calls} call(s), {self.input_tokens:,} in / "
                f"{self.output_tokens:,} out, {self.cache_read:,} cached, {money}")


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def available(settings: Any = None) -> bool:
    """Whether a model call could succeed right now.

    Stricter than `Settings.has_model`: a configuration can name Anthropic without the
    optional SDK being installed, and that call would fail. False is a supported state,
    not an error — the mirror, retrieval, the duplicate sweep and undo all work without
    a model.
    """
    setup = describe_setup(settings)
    if setup is None:
        return False
    if setup.provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
    return True


def resolve(settings: Any = None, *, model: str | None = None,
            max_retries: int = 3) -> Provider:
    """Build the provider this configuration describes. Raises `ModelError` if none."""
    setup = describe_setup(settings)
    if setup is None:
        raise ModelError(
            "no model is configured. Set ANTHROPIC_API_KEY, or OPENAI_API_KEY, or "
            "GROQ_API_KEY — or PALIMPSEST_MODEL_BASE_URL for anything else that speaks "
            "the OpenAI API, including a local Ollama.\n"
            "Everything that does not need a model — the mirror, retrieval, the "
            "duplicate sweep, undo — works without one."
        )
    get = model_getter(settings)
    chosen = model or setup.model

    if setup.provider == "anthropic":
        from palimpsest.llm.anthropic_api import AnthropicProvider

        return AnthropicProvider(chosen, api_key=get("ANTHROPIC_API_KEY"),
                                 max_retries=max_retries)

    from palimpsest.llm.openai_api import OpenAIProvider

    # An explicit base URL may point anywhere, so its key can come from the generic
    # setting as well as from a vendor's own variable — a local Ollama needs no key at
    # all, which is why an empty one is allowed through here.
    key_env = MODEL_PROVIDERS.get(setup.provider, ("PALIMPSEST_MODEL_API_KEY",))[0]
    key = get("PALIMPSEST_MODEL_API_KEY") or get(key_env)
    base = setup.base_url or "https://api.openai.com/v1"
    if not chosen:
        raise ModelError(
            "PALIMPSEST_MODEL_BASE_URL is set but PALIMPSEST_MODEL is not. A compatible "
            "endpoint cannot be asked to pick a model for you — name one."
        )
    return OpenAIProvider(chosen, api_key=key, base_url=base, name=setup.provider,
                          max_retries=max_retries)


# ---------------------------------------------------------------------------
# the façade
# ---------------------------------------------------------------------------


class Model:
    """One model, whoever serves it.

    Construct with no arguments to take whatever the environment describes, or pass a
    `provider` to pin one. `model` overrides the model id without changing the provider,
    which is what the evals use to score two models against the same golden set.
    """

    def __init__(self, model: str | None = None, *, api_key: str | None = None,
                 max_tokens: int = 16_000, provider: Provider | None = None,
                 settings: Any = None, max_retries: int = 3):
        if provider is None:
            if api_key and settings is None:
                import os

                # A bare `Model(api_key=...)` still means Anthropic, which is what the
                # library's own README example does.
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            provider = resolve(settings, model=model, max_retries=max_retries)
        self.provider = provider
        self.max_tokens = max_tokens
        self.usage = Usage()

    # -- identity --------------------------------------------------------------

    @property
    def model(self) -> str:
        return self.provider.model

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def base_url(self) -> str | None:
        return getattr(self.provider, "base_url", None)

    @property
    def can_see(self) -> bool:
        return self.provider.can_see

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<Model {self.name}/{self.model}>"

    # -- structured output -----------------------------------------------------

    def json(self, *, task: str, system: str, prompt: str, schema: dict,
             effort: str = "high", cache_prefix: str | None = None,
             max_tokens: int | None = None) -> dict:
        """Ask for one JSON object matching `schema`, and return it parsed."""
        started = time.perf_counter()
        try:
            parsed, usage = self.provider.json(
                system=system, prompt=prompt, schema=schema, effort=effort,
                cache_prefix=cache_prefix, max_tokens=max_tokens or self.max_tokens)
        except ModelError:
            self.usage.refusals += 1
            raise
        elapsed = time.perf_counter() - started
        self.usage.add(task, usage, elapsed)
        self._trace(task, usage, elapsed,
                    {"effort": effort, "cached": bool(cache_prefix)})
        return parsed

    # -- conversation ----------------------------------------------------------

    def conversation(self, history: list[dict] | None = None) -> Conversation:
        return self.provider.conversation(history)

    def converse(self, *, system: str, conversation: Conversation,
                 tools: list[ToolSpec] | None = None, effort: str = "high",
                 max_tokens: int | None = None, thinking: bool = True,
                 web_search: bool = False, task: str = "agent") -> Reply:
        """One turn of a tool-using conversation."""
        started = time.perf_counter()
        reply = self.provider.converse(
            system=system, conversation=conversation, tools=tools or [], effort=effort,
            max_tokens=max_tokens or self.max_tokens, thinking=thinking,
            web_search=web_search and self.provider.can_search_web)
        elapsed = time.perf_counter() - started
        self.usage.add(task, reply.usage, elapsed)
        self._trace(task, reply.usage, elapsed, {"effort": effort, "stop": reply.stop})
        return reply

    # -- vision ----------------------------------------------------------------

    def describe_image(self, image_bytes: bytes, media_type: str, prompt: str,
                       max_tokens: int = 4000) -> str:
        """Read an image: a whiteboard, a slide, a screenshot, a photographed page."""
        started = time.perf_counter()
        text, usage = self.provider.describe_image(image_bytes, media_type, prompt,
                                                   max_tokens)
        elapsed = time.perf_counter() - started
        self.usage.add("vision", usage, elapsed)
        self._trace("vision", usage, elapsed, {})
        return text

    # -- tracing ---------------------------------------------------------------

    def _trace(self, task: str, usage: TokenUsage, elapsed: float,
               metadata: dict) -> None:
        """`trace` is a no-op without Langfuse keys, so this costs nothing when off."""
        from palimpsest import trace

        trace.generation(task, model=f"{self.name}/{self.model}", usage=usage,
                         metadata={**metadata, "seconds": round(elapsed, 2),
                                   "provider": self.name})
