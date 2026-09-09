"""Retrieval eval: can it find the page, before anything tries to judge it?

The classifier gets measured and the classifier gets blamed, but it can only choose among
the pages retrieval handed it. A page that was never retrieved cannot be corroborated or
contradicted — the claim is filed as `new`, a second page about the same topic appears,
and the tool that exists to stop notes fragmenting has caused it. That failure is
invisible downstream: the patch is well-formed, the reasoning reads well, and it is wrong
for a reason no amount of classifier work can fix.

So retrieval is measured first and separately, and this suite is the one eval that needs
**no model and no key**. That is why it runs in CI as an ordinary test: a change that
makes ranking worse fails the build, rather than being discovered later as "the
classifier seems off lately".

**recall@k, not precision.** Retrieval here is a shortlist for a model that will read all
of it. Handing over six pages when one was needed costs a few hundred tokens; missing the
one costs a wrong decision about someone's notes. So the headline is whether the right
page is in the top k at all, and MRR is reported alongside to say how far down it was.

Cases marked `needs_vectors` share no vocabulary at all with their answer. They are scored
separately, because failing them with no embedding provider configured is the documented
behaviour of a lexical index, not a regression.
"""

from __future__ import annotations

import logging

from palimpsest.evals.fixture import cases, seed

log = logging.getLogger("palimpsest.evals.retrieval")

__all__ = ["PASS_MRR", "PASS_RECALL", "run"]

#: The bar a lexical-only index must clear on the cases it can reach. Set from measured
#: behaviour, not aspiration: raise it when the number rises, so it ratchets.
PASS_RECALL = 0.90

#: How far down the list the right page sits, on average. 1.0 means always first.
#: Ratcheted from 0.75 once the fixture stopped being uniformly first-place.
PASS_MRR = 0.85

#: Where the shortlist is cut. Matches the pipeline's own default for `pages_for`.
K = 6


def run(store, *, embedder=None, k: int = K, seed_store: bool = True) -> dict:
    """Score retrieval over the committed fixture. Returns a scorecard dict."""
    from palimpsest.retrieve import Index

    if seed_store:
        seed(store)
    index = Index(store, embedder=embedder)
    retrieval_cases, _ = cases()

    rows: list[dict] = []
    for case in retrieval_cases:
        hits = index.pages_for(case.query, top=k)
        found = [h.page_id for h in hits]
        rank = _first_rank(found, case.pages)
        rows.append({
            "id": case.id,
            "hit": rank is not None,
            "rank": rank,
            "reciprocal": (1.0 / rank) if rank else 0.0,
            "needs_vectors": case.needs_vectors,
            "expected": list(case.pages),
            "got": found[:k],
            "why": case.why,
        })

    lexical = [r for r in rows if not r["needs_vectors"]]
    semantic = [r for r in rows if r["needs_vectors"]]

    recall = _mean(r["hit"] for r in lexical)
    mrr = _mean(r["reciprocal"] for r in lexical)
    metrics: dict = {
        "n": len(rows),
        "k": k,
        "embedder": getattr(embedder, "model", None),
        f"recall_at_{k}": recall,
        "mrr": mrr,
        "semantic_recall": _mean(r["hit"] for r in semantic) if semantic else None,
        "semantic_n": len(semantic),
        "misses": [{"id": r["id"], "expected": r["expected"], "got": r["got"],
                    "why": r["why"]}
                   for r in lexical if not r["hit"]],
        "rows": rows,
    }
    metrics["passed"] = recall >= PASS_RECALL and mrr >= PASS_MRR
    return metrics


def _first_rank(found: list[str], expected: tuple[str, ...]) -> int | None:
    """1-based position of the first correct page, or None if it is not in the list.

    Any of the expected pages counts. Several cases have more than one legitimate home —
    "how do I shrink the KV cache" is genuinely covered on both the attention page and the
    serving page — and insisting on a single right answer would measure the fixture's
    arbitrariness rather than the ranking.
    """
    for position, page_id in enumerate(found, start=1):
        if page_id in expected:
            return position
    return None


def _mean(values) -> float:
    items = [float(v) for v in values]
    return round(sum(items) / len(items), 4) if items else 0.0


def summary(metrics: dict) -> str:  # pragma: no cover - display
    k = metrics.get("k", K)
    line = (f"retrieval: recall@{k} {metrics[f'recall_at_{k}']:.0%}  "
            f"mrr {metrics['mrr']:.2f}  over {metrics['n']} case(s)")
    if metrics.get("semantic_recall") is not None:
        state = "with vectors" if metrics.get("embedder") else "lexical only"
        line += (f"\n  semantic cases ({state}): "
                 f"{metrics['semantic_recall']:.0%} of {metrics['semantic_n']}")
    for miss in metrics.get("misses", []):
        line += f"\n  MISS {miss['id']}: expected {miss['expected']}, got {miss['got']}"
    return line
