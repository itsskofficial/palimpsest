"""The vocabulary every provider is translated into.

palimpsest does not care which model reads your notes, and this module is where that
claim is made concrete. Everything above it — extraction, classification, the agent loop
— speaks only the types defined here. Everything below it is a translation layer for one
vendor's wire format.

The types are deliberately small. A `Reply` is text, some tool calls, and a reason for
stopping. That is the whole of what the pipeline ever needed from a model; the rest of
what an SDK returns is packaging.

**`Reply.raw` is the one concession**, and it is load-bearing. A tool-using conversation
has to send the assistant's own previous turn back on the next request, byte for byte —
Anthropic wants its content blocks and its `thinking` blocks, OpenAI wants its
`tool_calls` array, and reconstructing either from neutral types loses something. So the
provider keeps its own turn opaque in `raw`, and the `Conversation` it hands out is the
only thing that ever touches it. Callers never open it.

That is why history is a `Conversation` object rather than a list of dicts: the shape of
"here are the results of the tools you asked for" differs between vendors — one appends a
user message full of `tool_result` blocks, the other appends one message per result — and
the loop should not have to know which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "Conversation",
    "ModelError",
    "Provider",
    "Reply",
    "TokenUsage",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
]


class ModelError(RuntimeError):
    """The model could not produce a usable answer.

    Raised for a refusal, an empty response, unparseable output, or a transport failure
    that survived retries. Callers treat it as "this source was not processed", never as
    "process it partially" — a half-extracted source is worse than a skipped one, because
    it looks like a complete one.
    """


@dataclass(frozen=True)
class TokenUsage:
    """What one call consumed, in the only four numbers every provider reports.

    Providers that do not break out cache hits report zeroes for them rather than
    guessing, so a cost estimate is never inflated by an invented cache read.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class ToolCall:
    """One tool the model wants run, with its arguments already parsed."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    """What a tool returned, on its way back to the model."""

    id: str
    payload: Any
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the pipeline defines it, before any vendor gets hold of it."""

    name: str
    description: str
    input_schema: dict


#: Why a turn stopped, normalised.
#:
#: `pause` is the one that is not obvious: a server-side tool (web search) ran mid-turn
#: and the model has more to say, so the loop must call again rather than treat the turn
#: as finished. Providers without server-side tools never emit it.
STOP_REASONS = ("end", "tools", "refusal", "pause", "length")


@dataclass
class Reply:
    """One assistant turn, in the only terms the loop needs."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"
    usage: TokenUsage = field(default_factory=TokenUsage)
    #: The provider's own representation of this turn. Opaque; only its own
    #: `Conversation` ever reads it. See the module docstring.
    raw: Any = None


class Conversation(Protocol):
    """A message list in one provider's shape, appended to through neutral types."""

    def user(self, text: str) -> None:
        """Add a turn from the person."""

    def assistant(self, reply: Reply) -> None:
        """Add the model's own turn back, preserving whatever it needs preserved."""

    def results(self, results: list[ToolResult]) -> None:
        """Add the output of tools the model asked for."""

    def wire(self) -> list[dict]:
        """The messages array to send. Provider-shaped; do not inspect it."""


class Provider(Protocol):
    """One vendor's API, reduced to the four things palimpsest asks of a model."""

    #: Short identifier for logs, traces and `palimpsest status` — "anthropic", "groq".
    name: str
    #: The model id being called, as the provider spells it.
    model: str

    def json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict,
        effort: str,
        cache_prefix: str | None,
        max_tokens: int,
    ) -> tuple[dict, TokenUsage]:
        """One JSON object matching `schema`. Raises `ModelError` rather than guessing."""
        ...

    def converse(
        self,
        *,
        system: str,
        conversation: Conversation,
        tools: list[ToolSpec],
        effort: str,
        max_tokens: int,
        thinking: bool,
        web_search: bool,
    ) -> Reply:
        """One turn of a tool-using conversation."""
        ...

    def conversation(self, history: list[dict] | None = None) -> Conversation:
        """A fresh message list, optionally seeded with plain role/content turns."""
        ...

    def describe_image(
        self, image_bytes: bytes, media_type: str, prompt: str, max_tokens: int
    ) -> tuple[str, TokenUsage]:
        """Read an image. Raises `ModelError` if the provider cannot see."""
        ...

    @property
    def can_see(self) -> bool:
        """Whether this model accepts images. Text-only models say so up front."""
        ...

    @property
    def can_search_web(self) -> bool:
        """Whether the provider runs web search on its own infrastructure."""
        ...
