"""The golden set: the labelled examples the classifier is measured against.

A golden example is a `(claim, source) → expected relation` triple, stored in the
`eval_examples` table. It is a **test set, not training data** — nothing is fine-tuned;
the classifier is run over these and its answers are compared to the labels.

Two ways in, and the second is why this is cheap to maintain:

- **Hand-labelled** — you (or a script) write down what the relation should be for a
  claim against your notes. A few dozen bootstraps the measurement.
- **From your own history** — every approval you resolve is a label. Approving a patch
  says its operations' relations were right; rejecting says they were wrong. So the set
  grows for free as you use the thing, and the measurement gets more honest over time
  without more work.

The examples reference the live mirror: the candidates a claim is classified against
come from the same retrieval index the real pipeline uses, so the eval measures the
system as it actually runs, not a frozen snapshot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from palimpsest.types import new_id

log = logging.getLogger("palimpsest.evals.golden")

__all__ = ["GoldenExample", "bootstrap_from_history", "load", "put"]


@dataclass
class GoldenExample:
    id: str
    claim_text: str
    claim_type: str
    topics: tuple[str, ...]
    source_title: str
    source_kind: str
    expected_relation: str
    label_source: str = "human"

    def as_row(self) -> dict:
        return {
            "id": self.id, "suite": "relation",
            "input": {"claim_text": self.claim_text, "claim_type": self.claim_type,
                      "topics": list(self.topics), "source_title": self.source_title,
                      "source_kind": self.source_kind},
            "expected": {"relation": self.expected_relation},
            "label_source": self.label_source,
        }

    @classmethod
    def from_row(cls, row: dict) -> GoldenExample:
        inp, exp = row.get("input") or {}, row.get("expected") or {}
        return cls(
            id=row["id"], claim_text=inp.get("claim_text", ""),
            claim_type=inp.get("claim_type", "fact"),
            topics=tuple(inp.get("topics") or ()),
            source_title=inp.get("source_title", ""),
            source_kind=inp.get("source_kind", "text"),
            expected_relation=exp.get("relation", "new"),
            label_source=row.get("label_source", "human"))


def put(store, example: GoldenExample) -> str:
    return store.put_eval_example(example.as_row())


def load(store, limit: int = 2000) -> list[GoldenExample]:
    return [GoldenExample.from_row(r)
            for r in store.get_eval_examples(suite="relation", limit=limit)]


def bootstrap_from_history(store, limit: int = 500) -> int:
    """Turn resolved approvals into labelled examples. Returns how many were added.

    An approved operation is a label that its relation was the right call for that claim
    against the notes at the time; this harvests those into the golden set. It is
    conservative — only operations that carry a claim and a relation, and only from
    approvals a human resolved — because a wrong label is worse than a missing one.
    """
    added = 0
    for approval in store.list_approvals(status="approved", limit=limit):
        patch = store.get_patch(approval["patch_id"])
        if patch is None:
            continue
        source = store.get_source(patch.source_id) if patch.source_id else None
        for op in patch.operations:
            if op.relation is None or op.claim_id is None:
                continue
            claim = _claim_for(store, op.claim_id)
            if claim is None:
                continue
            example = GoldenExample(
                id=new_id("ex_"), claim_text=claim.text, claim_type=claim.type.value,
                topics=claim.topics,
                source_title=(source.title if source else "") or "",
                source_kind=(source.kind if source else "text") or "text",
                expected_relation=op.relation.value, label_source="approval")
            store.put_eval_example(example.as_row())
            added += 1
    log.info("bootstrapped %d example(s) from approval history", added)
    return added


def _claim_for(store, claim_id: str):
    """Fetch a claim by id via its source's claims. The store has no direct getter, and
    adding one for a maintenance path is not worth the surface — this is rare and small."""
    # A claim id encodes nothing about its source, so we look through recent sources.
    for src in store.list_sources(limit=200):
        for claim in store.get_claims(src["source_id"]):
            if claim.claim_id == claim_id:
                return claim
    return None


def now() -> float:
    return time.time()
