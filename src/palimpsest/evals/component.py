"""Component eval: how well does the classifier assign relations?

Runs the real classifier over the golden set and scores its answers. The output is the
number the autonomy ladder was always supposed to rest on — per-relation precision and
recall — plus the one metric weighted above the rest.

**Contradiction recall is weighted 3×.** The costs are asymmetric: a missed
contradiction puts something false into the base and you do not find out, poisoning
trust in the parts that are still right; a false contradiction costs you thirty seconds.
So the headline score penalises misses on that relation triple-weight, and a run that
misses contradictions cannot pass on the strength of the easy relations.

This needs a model and a populated mirror, so it is a command you run, not a CI test —
`palimpsest eval component`. The offline guarantee that matters (the gate) lives in
`tests/unit/test_safety.py` and does run in CI.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from palimpsest.evals.golden import GoldenExample, load
from palimpsest.types import Claim, ClaimType, Source, new_id

log = logging.getLogger("palimpsest.evals.component")

__all__ = ["run"]

#: How much more a contradiction miss counts than any other error, in the headline.
CONTRADICTION_RECALL_WEIGHT = 3.0

#: A run passes only if every relation clears this, and contradiction recall clears the
#: stricter bar below. Deliberately conservative — these gate autonomy.
PASS_F1 = 0.7
PASS_CONTRADICTION_RECALL = 0.9


def run(store, model, index, *, effort: str = "high",
        examples: list[GoldenExample] | None = None) -> dict:
    """Classify every golden example and score the results. Returns a scorecard dict."""
    from palimpsest.relate import classify_one

    examples = examples if examples is not None else load(store)
    if not examples:
        return {"error": "no golden examples; run `palimpsest eval bootstrap` or label "
                         "some by hand first", "n": 0}

    # confusion[expected][predicted] = count
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = []
    for ex in examples:
        claim = Claim(claim_id=new_id("clm_"), text=ex.claim_text,
                      type=_claim_type(ex.claim_type), topics=ex.topics)
        source = Source(source_id=new_id("src_"), kind=ex.source_kind,
                        title=ex.source_title, text=ex.claim_text)
        try:
            judgement = classify_one(claim, source, index, model, effort=effort)
            predicted = judgement.relation.value
        except Exception as e:  # a classifier failure is a wrong answer, not a crash
            log.warning("classify failed for %s: %s", ex.id, e)
            predicted = "error"
        confusion[ex.expected_relation][predicted] += 1
        rows.append({"expected": ex.expected_relation, "predicted": predicted,
                     "claim": ex.claim_text[:80]})

    metrics = _metrics(confusion)
    metrics["n"] = len(examples)
    metrics["confusion"] = {k: dict(v) for k, v in confusion.items()}
    metrics["rows"] = rows
    metrics["passed"] = _passed(metrics)
    return metrics


def _metrics(confusion: dict[str, dict[str, int]]) -> dict:
    """Per-relation precision/recall/F1 from the confusion matrix, plus a weighted
    headline that makes contradiction recall dominate."""
    relations = set(confusion) | {p for row in confusion.values() for p in row}
    relations.discard("error")

    per: dict[str, dict[str, float]] = {}
    for rel in sorted(relations):
        tp = confusion.get(rel, {}).get(rel, 0)
        fn = sum(v for p, v in confusion.get(rel, {}).items() if p != rel)
        fp = sum(confusion.get(other, {}).get(rel, 0)
                 for other in confusion if other != rel)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        per[rel] = {"precision": round(precision, 3), "recall": round(recall, 3),
                    "f1": round(f1, 3), "support": tp + fn}

    # Weighted headline: mean F1, but a contradiction *miss* is penalised triple.
    contra = per.get("contradicts", {"recall": 1.0, "support": 0})
    weights = {rel: (CONTRADICTION_RECALL_WEIGHT if rel == "contradicts" else 1.0)
               for rel in per}
    total_w = sum(weights.values()) or 1.0
    weighted_f1 = sum(per[rel]["f1"] * weights[rel] for rel in per) / total_w

    return {"per_relation": per,
            "contradiction_recall": contra.get("recall", 1.0),
            "weighted_f1": round(weighted_f1, 3),
            "macro_f1": round(sum(m["f1"] for m in per.values()) / len(per), 3)
            if per else 0.0}


def _passed(metrics: dict) -> bool:
    if metrics.get("contradiction_recall", 0) < PASS_CONTRADICTION_RECALL:
        # Only fail on this when there were contradictions to catch.
        contra = metrics["per_relation"].get("contradicts", {})
        if contra.get("support", 0) > 0:
            return False
    return all(m["f1"] >= PASS_F1 for m in metrics["per_relation"].values()
               if m["support"] > 0)


def _claim_type(value: str) -> ClaimType:
    try:
        return ClaimType(value)
    except ValueError:
        return ClaimType.FACT


def scorecard(metrics: dict) -> str:  # pragma: no cover - display only
    if metrics.get("error"):
        return metrics["error"]
    lines = [f"Component eval — {metrics['n']} example(s)", ""]
    lines.append(f"  {'relation':<14} {'prec':>6} {'recall':>7} {'f1':>6} {'n':>4}")
    for rel, m in sorted(metrics["per_relation"].items()):
        flag = "  ⚠" if rel == "contradicts" and m["recall"] < PASS_CONTRADICTION_RECALL else ""
        lines.append(f"  {rel:<14} {m['precision']:>6.2f} {m['recall']:>7.2f} "
                     f"{m['f1']:>6.2f} {m['support']:>4}{flag}")
    lines += ["",
              f"  contradiction recall : {metrics['contradiction_recall']:.2f} "
              f"(weighted {CONTRADICTION_RECALL_WEIGHT}×)",
              f"  weighted F1          : {metrics['weighted_f1']:.2f}",
              f"  {'PASS' if metrics['passed'] else 'FAIL'}"]
    return "\n".join(lines)
