"""Score several models against the same golden set, and print the table.

    palimpsest eval leaderboard --models anthropic/claude-sonnet-5,ollama/qwen3:8b

The model is configuration, not code, which is a good property that quietly transfers a
question to the user: *which* model should maintain your knowledge base? That question has
an answer, it differs by an enormous margin, and nobody can guess it — so this measures it
rather than leaving people to find out from a page that got quietly rewritten wrong.

**Contradiction recall is the column to read**, not the headline F1. A model that misses
two contradictions in three is not a model to hand write access to, whatever its weighted
average, because the failure mode of a missed contradiction is a knowledge base that
confidently states something false. That metric is weighted 3x in the score and is printed
on its own.

Nothing here is new machinery: it runs the existing `component` suite once per model
against the committed fixture, so a row in this table means exactly what
`palimpsest eval component` means, and the comparison is like for like by construction.
"""

from __future__ import annotations

import logging
import time
from typing import Any

__all__ = ["render", "run"]

log = logging.getLogger("palimpsest.evals")


def run(store, index, specs: list[str], *, settings: Any = None,
        effort: str = "high", examples: Any = None) -> list[dict]:
    """Score each `provider/model` spec. Returns one row per model, best first.

    A model that cannot be reached gets a row saying so rather than aborting the run: the
    common case for this command is a mixed list where one local runtime is not up, and
    losing four good measurements to one missing container helps nobody.
    """
    from palimpsest.evals import component

    rows: list[dict] = []
    for spec in specs:
        provider, _, name = spec.partition("/")
        if not name:
            provider, name = "", spec
        started = time.perf_counter()
        try:
            model = _build(provider, name, settings)
            metrics = component.run(store, model, index, effort=effort,
                                    examples=examples)
        except Exception as e:
            log.warning("could not score %s: %s", spec, e)
            rows.append({"model": spec, "error": str(e)[:120]})
            continue
        if metrics.get("error"):
            # `component.run` reports a failure inside the metrics rather than by
            # raising, so a row that only checked for an exception showed an empty score
            # as if it were a real one.
            rows.append({"model": spec, "error": str(metrics["error"])[:120]})
            continue
        rows.append({
            "model": spec,
            "weighted_f1": metrics.get("weighted_f1"),
            "contradiction_recall": _recall(metrics, "contradicts"),
            "duplicate_recall": _recall(metrics, "duplicate"),
            "n": metrics.get("n"),
            "passed": bool(metrics.get("passed")),
            "seconds": round(time.perf_counter() - started, 1),
            "cost_usd": model.usage.cost_usd(model.model, model.base_url),
        })

    return sorted(rows, key=lambda r: (r.get("weighted_f1") is None,
                                       -(r.get("weighted_f1") or 0)))


def _build(provider: str, name: str, settings: Any):
    """A `Model` for one spec, without disturbing the configured default."""
    import dataclasses

    from palimpsest.config import LOCAL_RUNTIMES, Settings
    from palimpsest.llm import Model

    settings = settings or Settings.load()
    if provider and provider in LOCAL_RUNTIMES:
        base_url, _, _ = LOCAL_RUNTIMES[provider]
        settings = dataclasses.replace(settings, model_provider=provider,
                                       model_base_url=base_url, model=name)
    elif provider:
        settings = dataclasses.replace(settings, model_provider=provider,
                                       model_base_url=None, model=name)
    else:
        settings = dataclasses.replace(settings, model=name)
    return Model(settings=settings)


def _recall(metrics: dict, relation: str) -> float | None:
    per = (metrics.get("per_relation") or {}).get(relation) or {}
    value = per.get("recall")
    return round(float(value), 2) if value is not None else None


def render(rows: list[dict]) -> str:  # pragma: no cover - display only
    """The table, as markdown, so it can be pasted straight into the README."""
    out = ["| model | weighted F1 | contradiction recall | duplicate recall | n | |",
           "|---|---|---|---|---|---|"]
    best = next((r.get("weighted_f1") for r in rows
                 if r.get("weighted_f1") is not None), None)
    for row in rows:
        f1 = row.get("weighted_f1")
        if row.get("error") or f1 is None:
            note = row.get("error") or "no score"
            out.append(f"| `{row['model']}` | — | — | — | — | {note} |")
            continue
        shown = f"**{f1:.2f}**" if f1 == best else f"{f1:.2f}"
        out.append(
            f"| `{row['model']}` | {shown} | {_cell(row['contradiction_recall'])} | "
            f"{_cell(row['duplicate_recall'])} | {row.get('n', '?')} | "
            f"{'PASS' if row['passed'] else 'fail'} |")
    out.append("")
    out.append("Contradiction recall is the column to read. A model that misses two "
               "contradictions in three")
    out.append("is not a model to hand write access to, whatever its headline number.")
    return "\n".join(out)


def _cell(value: float | None) -> str:  # pragma: no cover - display only
    return "—" if value is None else f"{value:.2f}"
