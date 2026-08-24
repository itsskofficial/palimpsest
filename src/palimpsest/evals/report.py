"""Recording an eval run: to the store for history, to Langfuse for the dashboard.

A run's scores go to `eval_runs` so `palimpsest eval history` can show whether the
classifier is getting better or worse over time, and — when tracing is on — to Langfuse
as scores so the numbers live beside the traces that produced them. Both are best-effort
for the Langfuse half: a run that could not be uploaded is still a run that happened.
"""

from __future__ import annotations

import logging

from palimpsest.types import new_id

log = logging.getLogger("palimpsest.evals.report")

__all__ = ["record"]


def record(store, suite: str, metrics: dict, *, model: str | None = None) -> str:
    """Persist a run and push its headline numbers to Langfuse. Returns the run id."""
    run_id = new_id("run_")
    scores = {k: v for k, v in metrics.items()
              if isinstance(v, int | float) and k not in ("n",)}
    store.put_eval_run({
        "run_id": run_id, "suite": suite, "model": model,
        "scores": {**scores, "n": metrics.get("n", 0)},
        "passed": bool(metrics.get("passed", False)),
    })

    try:
        from palimpsest import trace

        if trace.enabled():
            for name, value in scores.items():
                trace.score(f"eval.{suite}.{name}", float(value))
            # Per-relation F1 too, so a regression on one relation is visible.
            for rel, m in (metrics.get("per_relation") or {}).items():
                trace.score(f"eval.{suite}.f1.{rel}", float(m["f1"]))
            trace.flush()
    except Exception as e:  # pragma: no cover - reporting is never load-bearing
        log.debug("could not push eval scores to Langfuse: %s", e)
    return run_id
