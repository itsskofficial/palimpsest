"""Observability, and the discipline that it is never load-bearing.

Every model call, every tool call and every agent turn can be traced to Langfuse. But
the mirror, the sweeps, `undo` and the whole offline story must keep working with no
Langfuse, no keys, and no network — so this module is built so that *absence is the
normal case handled first*, not an error caught later.

Three rules hold that line:

1. **No keys means no-op.** With `LANGFUSE_*` unset, `configure()` returns without
   importing anything, and every span/score/generation call is a cheap nothing. The
   import of `langfuse` itself is lazy and inside a `try`, so a machine without the
   package installed is fine.
2. **A tracing failure never propagates.** Langfuse being down, slow, or
   misconfigured must not fail an ingest or an agent turn. Every public function here
   swallows its own exceptions and logs at debug.
3. **Secrets are masked before they leave.** Tool arguments and results can carry a
   Notion token or a source's raw text; the mask strips anything that looks like a
   credential before it is sent.

The surface is deliberately small — `span`, `generation`, `score`, `update_current`,
and the `configure`/`flush` lifecycle — because a thin seam is one you can reason about
when a trace looks wrong at 1 a.m.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterator
from typing import Any

__all__ = ["configure", "enabled", "flush", "generation", "score", "span",
           "update_current"]

log = logging.getLogger("palimpsest.trace")

_client: Any = None
_configured = False

#: Patterns whose values are blanked before anything is sent. Ordered cheapest first.
_SECRET_RE = re.compile(
    r"(ntn_[A-Za-z0-9]+|sk-[A-Za-z0-9_\-]+|Bearer\s+[A-Za-z0-9._\-]+|"
    r"\d{6,}:[A-Za-z0-9_\-]{30,})"  # a telegram bot token
)


def configure(settings: Any = None) -> bool:
    """Initialise Langfuse if keys are present. Idempotent. Never raises.

    Returns whether tracing is live, so a caller can log it once at startup rather than
    wondering later why the dashboard is empty.
    """
    global _client, _configured
    if _configured:
        return _client is not None
    _configured = True

    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        log.debug("Langfuse keys not set; tracing is a no-op")
        return False

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=public,
            secret_key=secret,
            host=os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"),
        )
        # auth_check is cheap and tells us now, not on first flush, that the keys work.
        with contextlib.suppress(Exception):
            _client.auth_check()
        log.info("Langfuse tracing enabled")
        return True
    except Exception as e:  # pragma: no cover - depends on the environment
        log.warning("could not start Langfuse (%s); continuing without tracing", e)
        _client = None
        return False


def enabled() -> bool:
    if not _configured:
        configure()
    return _client is not None


def _mask(value: Any, _depth: int = 0) -> Any:
    """Blank anything credential-shaped, recursively, before it leaves the process."""
    if _depth > 6:
        return "…"
    if isinstance(value, str):
        return _SECRET_RE.sub("«redacted»", value)
    if isinstance(value, dict):
        return {k: ("«redacted»" if _looks_secret(k) else _mask(v, _depth + 1))
                for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask(v, _depth + 1) for v in value][:200]
    return value


def _looks_secret(key: str) -> bool:
    k = str(key).lower()
    return any(h in k for h in ("token", "secret", "api_key", "apikey", "password",
                                "authorization", "credential"))


@contextlib.contextmanager
def span(name: str, *, kind: str = "span", input: Any = None,
         metadata: dict | None = None) -> Iterator[Any]:
    """A traced unit of work. Yields a handle whose `.update(...)` sets the output.

    `kind` maps to a Langfuse observation type — `agent`, `tool`, `chain`, `generation`.
    Without tracing this yields a small inert handle, so call sites read identically on
    and off.
    """
    if not enabled():
        yield _Inert()
        return

    # `start_as_current_observation` is a context manager that also sets this span as
    # the active one — which is what makes children nest under it and what lets
    # `score_current_trace` / `update_current` find something to attach to. The detached
    # `start_observation` does neither, and warns.
    try:
        cm = _client.start_as_current_observation(
            name=name, as_type=kind if kind in _AS_TYPES else "span",
            input=_mask(input) if input is not None else None,
            metadata=_mask(metadata) if metadata else None,
        )
    except Exception as e:  # pragma: no cover - defensive
        log.debug("span start failed: %s", e)
        yield _Inert()
        return

    # `start_as_current_observation` returns a context manager that ends the span on
    # exit and sets it active for the duration — so children nest and `score_current`
    # resolves. On an error inside the body we stamp the span before it closes.
    with cm as observation:
        handle = _Handle(observation)
        try:
            yield handle
        except Exception as exc:
            handle.update(level="ERROR", status_message=str(exc)[:400])
            raise


_AS_TYPES = frozenset({"span", "agent", "tool", "chain", "generation", "retriever",
                       "evaluator", "guardrail", "embedding", "event"})


def generation(name: str, *, model: str, usage: Any = None, input: Any = None,
               output: Any = None, metadata: dict | None = None) -> None:
    """Record a completed model call as a generation observation. Never raises."""
    if not enabled():
        return
    try:
        obs = _client.start_observation(
            name=name, as_type="generation", model=model,
            input=_mask(input) if input is not None else None,
            metadata=_mask(metadata) if metadata else None,
        )
        if output is not None:
            obs.update(output=_mask(output))
        if usage is not None:
            obs.update(usage_details={
                "input": int(getattr(usage, "input_tokens", 0) or 0),
                "output": int(getattr(usage, "output_tokens", 0) or 0),
                "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            })
        obs.end()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("generation record failed: %s", e)


def score(name: str, value: float, *, comment: str | None = None,
          trace_id: str | None = None) -> None:
    """Attach a numeric score — an eval result, or an accept/reject. Never raises."""
    if not enabled():
        return
    try:
        if trace_id:
            _client.create_score(name=name, value=value, comment=comment,
                                 trace_id=trace_id)
        else:
            _client.score_current_trace(name=name, value=value, comment=comment)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("score failed: %s", e)


def update_current(**fields: Any) -> None:
    """Set metadata or output on the current span. Never raises."""
    if not enabled():
        return
    try:
        _client.update_current_span(**{k: _mask(v) for k, v in fields.items()})
    except Exception as e:  # pragma: no cover - defensive
        log.debug("update_current failed: %s", e)


def flush() -> None:
    """Send buffered events. Call on shutdown; safe to call when disabled."""
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.flush()


class _Handle:
    """A thin wrapper over a Langfuse observation, so callers never see the SDK type."""

    __slots__ = ("_obs",)

    def __init__(self, obs: Any):
        self._obs = obs

    def update(self, **fields: Any) -> None:
        with contextlib.suppress(Exception):
            if "output" in fields:
                fields["output"] = _mask(fields["output"])
            if "input" in fields:
                fields["input"] = _mask(fields["input"])
            self._obs.update(**fields)

    def end(self) -> None:
        with contextlib.suppress(Exception):
            self._obs.end()


class _Inert:
    """What every call site gets when tracing is off. Does nothing, cheaply."""

    __slots__ = ()

    def update(self, **_fields: Any) -> None:
        pass

    def end(self) -> None:
        pass
