"""What a call probably cost, and honesty about when we do not know.

Published rates, USD per million tokens, as (input, output). Matched by prefix so a
dated model id — `claude-sonnet-5-20260514`, `gpt-5.1-2026-03-02` — finds its family
without needing a row per snapshot.

The important behaviour here is the miss. An unknown model returns `None`, and every
caller renders that as "cost unknown" rather than as `$0.00`. Guessing a default rate
was the previous behaviour and it was actively misleading in both directions: it made a
free local model look like it cost dollars, and it would have quietly under-reported a
model priced above the guess. The invoice is the invoice; this is a running estimate,
and an estimate that cannot be made is worth saying out loud.
"""

from __future__ import annotations

__all__ = ["cost_usd", "rate_for"]

#: Prefix → (input, output) USD per million tokens.
RATES: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    # OpenAI
    "gpt-5.1": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "o4-mini": (1.1, 4.4),
    # Groq — cheap enough that the estimate is mostly there to show it is cheap.
    "llama-3.3-70b": (0.59, 0.79),
    "llama-3.1-8b": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.1, 0.5),
    "moonshotai/kimi-k2": (1.0, 3.0),
    "qwen": (0.29, 0.59),
    "deepseek": (0.75, 0.99),
}

#: Anything served from here is someone's own machine, so the marginal cost is zero.
#: Electricity is real but it is not a token price, and pretending otherwise would put a
#: fictional number in the ledger.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "[::1]")


def rate_for(model: str, base_url: str | None = None) -> tuple[float, float] | None:
    """The (input, output) rate for a model, or `None` if we have no published figure."""
    if base_url and any(host in base_url for host in LOCAL_HOSTS):
        return (0.0, 0.0)
    name = (model or "").lower()
    # Longest prefix wins, so `gpt-5-mini` is not swallowed by `gpt-5`.
    for prefix in sorted(RATES, key=len, reverse=True):
        if name.startswith(prefix) or f"/{prefix}" in name:
            return RATES[prefix]
    return None


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    base_url: str | None = None,
) -> float | None:
    """Estimated spend, or `None` when the model's price is not known.

    Cache reads bill at roughly a tenth of the input rate and writes at roughly 1.25x,
    which is Anthropic's published multiplier. Providers with automatic caching report no
    cache tokens at all, so those terms fall out to zero on their own.
    """
    rate = rate_for(model, base_url)
    if rate is None:
        return None
    rate_in, rate_out = rate
    billed_in = input_tokens + cache_read * 0.1 + cache_write * 1.25
    return (billed_in * rate_in + output_tokens * rate_out) / 1_000_000
