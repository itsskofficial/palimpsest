"""The Claude layer: one wrapper, structured output, cached prefixes, honest costs.

Everything that needs a model goes through `Model.json()`. Centralising it buys four
things that would otherwise be scattered across the extraction, classification and
adjudication code:

**Structured output, not prompt-and-pray.** Every call declares a JSON schema through
`output_config.format`, so the response is valid against it or the request fails. Claim
extraction and relation classification are both "produce this exact shape" problems,
and parsing free text into them is a source of silent corruption — a dropped field
becomes a claim with no anchor, which becomes a citation that goes nowhere.

**Prompt caching where the prefix is stable.** The system prompt and the page context
handed to the classifier are identical across every claim in a source. Marking that
prefix cached turns the per-claim cost into roughly the tokens of the claim itself,
which is the difference between a viable per-source bill and an absurd one.

**Effort per job.** Extraction is mechanical and runs at `medium`; relation
classification is the judgment call the whole product rests on and runs at `high`.
Both are settings, because the right answer depends on your notes.

**Refusals handled, and fallbacks on by default.** Claude Opus 5 can decline a request
with `stop_reason: "refusal"` and HTTP 200. Code that reads `content[0]` without
checking breaks on that. Server-side fallbacks re-serve a declined request on another
model inside the same call, so the pipeline keeps moving; it is opt-in on the API and
opted into here.

Costs are accumulated per process so `palimpsest ingest` can print what a source
actually cost rather than leaving you to find out at the end of the month.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Model", "ModelError", "Usage", "available"]

log = logging.getLogger("palimpsest.llm")

#: Published rates, USD per million tokens, for the models this project uses.
#: Only used to render an estimate — the invoice is the invoice.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class ModelError(RuntimeError):
    """The model could not produce a usable answer."""


def available() -> bool:
    """Whether a model call could succeed right now."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


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

    def add(self, task: str, usage: Any, seconds: float) -> None:
        self.calls += 1
        self.seconds += seconds
        self.by_task[task] = self.by_task.get(task, 0) + 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.cache_write += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    def cost_usd(self, model: str) -> float:
        """Rough cost. Cache reads bill at ~0.1x input, writes at ~1.25x."""
        rate_in, rate_out = PRICES.get(model, (5.0, 25.0))
        billed_in = self.input_tokens + self.cache_read * 0.1 + self.cache_write * 1.25
        return (billed_in * rate_in + self.output_tokens * rate_out) / 1_000_000

    def as_dict(self, model: str = "claude-opus-5") -> dict:
        return {
            "calls": self.calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "cache_read": self.cache_read,
            "cache_write": self.cache_write, "refusals": self.refusals,
            "seconds": round(self.seconds, 1), "by_task": self.by_task,
            "estimated_cost_usd": round(self.cost_usd(model), 4),
        }

    def summary(self, model: str = "claude-opus-5") -> str:  # pragma: no cover
        return (f"{self.calls} call(s), {self.input_tokens:,} in / "
                f"{self.output_tokens:,} out, {self.cache_read:,} cached, "
                f"~${self.cost_usd(model):.3f}")


class Model:
    """A thin, opinionated wrapper over the Anthropic Messages API."""

    def __init__(self, model: str = "claude-opus-5", api_key: str | None = None,
                 max_tokens: int = 16_000, fallbacks: bool = True,
                 max_retries: int = 3):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - optional extra
            raise ImportError(
                "the model layer needs the Anthropic SDK: pip install 'palimpsest[anthropic]'"
            ) from e
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ModelError(
                "no ANTHROPIC_API_KEY. Extraction and classification need a model.\n"
                "Everything that does not — the mirror, retrieval, the duplicate sweep, "
                "undo — works without one."
            )
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=key, max_retries=max_retries)
        self.model = model
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.usage = Usage()
        #: Set once a beta parameter is rejected, so we stop paying the retry.
        self._beta_ok = True

    # -- the one entry point ---------------------------------------------------

    def json(self, *, task: str, system: str, prompt: str, schema: dict,
             effort: str = "high", cache_prefix: str | None = None,
             max_tokens: int | None = None) -> dict:
        """Ask for one JSON object matching `schema`, and return it parsed.

        `cache_prefix` is content that repeats across calls — the page context handed
        to the classifier, for instance. It is placed in the system block behind a
        cache breakpoint, so the second and subsequent claims from the same source
        read it at roughly a tenth of the price. Volatile content (the claim itself)
        always goes in the user turn, after the breakpoint, or the cache never hits.
        """
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

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }

        started = time.perf_counter()
        response = self._call(params)
        elapsed = time.perf_counter() - started
        self.usage.add(task, getattr(response, "usage", None), elapsed)

        # Always check stop_reason before touching content: a refusal is HTTP 200 with
        # an empty or partial content array.
        if getattr(response, "stop_reason", None) == "refusal":
            self.usage.refusals += 1
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelError(
                f"the model declined this request (category={category or 'unspecified'}). "
                "The source was not processed. Nothing was written to Notion."
            )

        # `getattr` rather than `b.text`: the content union has a dozen block types
        # and only some carry `.text`. Filtering on `.type` is correct at runtime but
        # does not narrow the union for a type checker.
        text = "".join(getattr(b, "text", "") for b in response.content
                       if getattr(b, "type", None) == "text")
        if not text.strip():
            raise ModelError(f"{task}: empty response (stop_reason="
                             f"{getattr(response, 'stop_reason', '?')})")
        try:
            return json.loads(text)
        except ValueError as e:
            # With output_config.format this should be unreachable; if the schema is
            # ever dropped it becomes the failure mode, so it fails loudly.
            raise ModelError(f"{task}: response was not valid JSON: {text[:200]}") from e

    # -- transport -------------------------------------------------------------

    def _call(self, params: dict):
        """Prefer the beta endpoint so refusals fall back; degrade if unsupported.

        `fallbacks="default"` lets Anthropic route a declined request to the
        recommended substitute model by refusal category, rather than us pinning one
        that later gets deprecated. If the installed SDK or the account does not have
        the beta, we drop to the plain endpoint once and remember.
        """
        if self.fallbacks and self._beta_ok:
            try:
                return self.client.beta.messages.create(
                    **params,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except TypeError:
                self._beta_ok = False
                log.info("installed SDK does not accept server-side fallbacks; "
                         "using the standard endpoint")
            except self._anthropic.BadRequestError as e:
                self._beta_ok = False
                log.info("server-side fallbacks unavailable (%s); "
                         "using the standard endpoint", str(e)[:120])
        return self.client.messages.create(**params)

    # -- vision ----------------------------------------------------------------

    def describe_image(self, image_bytes: bytes, media_type: str, prompt: str,
                       max_tokens: int = 4000) -> str:
        """Read an image: a whiteboard, a slide, a screenshot, a photographed page."""
        import base64

        # Built as a plain list first: the SDK types these blocks as TypedDicts, and
        # inlining the literal makes the checker infer `dict[str, Collection[str]]`
        # for the image block and reject it.
        content: list[Any] = [
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            }},
            {"type": "text", "text": prompt},
        ]

        started = time.perf_counter()
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        self.usage.add("vision", getattr(response, "usage", None),
                       time.perf_counter() - started)
        if getattr(response, "stop_reason", None) == "refusal":
            self.usage.refusals += 1
            raise ModelError("the model declined to read this image")
        return "".join(getattr(b, "text", "") for b in response.content
                       if getattr(b, "type", None) == "text")
