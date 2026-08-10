"""The relation classifier: how does this claim relate to what you already wrote?

This module is the product. Everything else is plumbing that gets a claim and its
candidate neighbourhood in front of this question, and turns the answer into an edit.

The question is deliberately *not* "should I update or create". That framing forces a
decision with no vocabulary to express the interesting cases, and the escape hatch —
make a new page — is always available, which is why every tool that asks it produces a
pile. Asking for a *relation* instead means the ambiguous cases have names, and a
reviewer can disagree with a specific one.

Three design choices worth defending:

**A ladder, not one big call.** Cheap deterministic checks run first: an exact-ish
match against an existing block is a `DUPLICATE` or `CORROBORATES` without spending a
token, and a claim whose retrieval turned up nothing at all is `NEW` by construction.
The model is asked only about the genuinely ambiguous middle, which is where the
judgment actually lives and where the money is worth spending.

**One call per claim, over all candidates.** Not one call per (claim × candidate). The
candidates share a cached prefix, so the marginal cost of a claim is roughly its own
tokens, and — more importantly — the model can compare candidates against each other,
which is exactly what distinguishes `DUPLICATE` from `EXTENDS`.

**Contradictions are never resolved here.** The classifier reports one; the planner
refuses to auto-apply it; the reviewer decides. Adjudication exists as a separate,
explicitly-invoked function that lays out both sides — it never picks a winner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from palimpsest.llm import Model, ModelError
from palimpsest.retrieve import Candidate, Index, PageHit, tokenize
from palimpsest.types import Claim, Judgement, Relation, Source

__all__ = ["ClassifyResult", "adjudicate", "classify", "classify_one"]

log = logging.getLogger("palimpsest.relate")

SYSTEM = """\
You maintain a personal knowledge base. For one new claim from a source, decide how it \
relates to what the notes already contain, and where it should go.

Choose exactly one relation:

- `new` — nothing in the candidates covers this. It should be added.
- `corroborates` — a candidate block already says this. The source is independent \
support. NOTHING IS ADDED; the existing block just gains a citation. Prefer this \
whenever the substance already exists, even if the wording differs.
- `refines` — a candidate says this less precisely. The claim sharpens it (a rounded \
number becomes exact, a vague statement gains a condition). The block is edited and \
the old wording is footnoted.
- `supersedes` — a candidate states the same fact but this source is newer or more \
authoritative and the old value is now wrong (a changed price, a revised API, an \
updated result). The old text is struck through, not deleted.
- `duplicate` — the SAME content already exists on a DIFFERENT page from the one this \
belongs on. The fix is to merge or link the pages, never to add the text again.
- `extends` — related to a candidate but belongs on a different page than the \
best-scoring one. Say which page in `target_page_id`.
- `contradicts` — a candidate asserts something incompatible with this claim. Both \
cannot be true. NEVER choose this merely because the claim is new, more detailed, or \
differently worded — only when they genuinely conflict.

Guidance:
- Prefer `corroborates` over `new` when the substance is already present. Avoiding \
duplicate prose is more valuable than capturing a nicer phrasing.
- Prefer `new` over `refines` when you are unsure the candidate is really about the \
same thing. A wrong `refines` edits a sentence that was fine.
- `contradicts` is the most consequential answer and always goes to a human. Use it \
when it is true, and only then.
- `target_block_id` is required for corroborates, refines, supersedes, duplicate and \
contradicts. `target_page_id` is required for new and extends.
- For `refines` and `supersedes`, put the existing sentence you are changing in \
`existing_text`, copied exactly.
- `confidence` is your confidence in the RELATION, not in the claim.
- `rationale` is one sentence a human will read while deciding. Be concrete."""

SCHEMA = {
    "type": "object",
    "properties": {
        "relation": {"type": "string", "enum": [r.value for r in Relation]},
        "confidence": {"type": "number"},
        "target_page_id": {"type": ["string", "null"]},
        "target_block_id": {"type": ["string", "null"]},
        "existing_text": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["relation", "confidence", "target_page_id", "target_block_id",
                 "existing_text", "rationale"],
    "additionalProperties": False,
}

ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["factual", "temporal", "scope", "terminology", "apparent"]},
        "summary": {"type": "string"},
        "case_for_existing": {"type": "string"},
        "case_for_new": {"type": "string"},
        "what_would_settle_it": {"type": "string"},
        "recommendation": {"type": "string",
                           "enum": ["keep_existing", "accept_new", "keep_both", "ask_user"]},
    },
    "required": ["kind", "summary", "case_for_existing", "case_for_new",
                 "what_would_settle_it", "recommendation"],
    "additionalProperties": False,
}


@dataclass
class ClassifyResult:
    judgements: list[Judgement] = field(default_factory=list)
    model_calls: int = 0
    shortcut: int = 0
    errors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.judgements)

    def by_relation(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for j in self.judgements:
            out[j.relation.value] = out.get(j.relation.value, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {"judgements": len(self.judgements), "model_calls": self.model_calls,
                "shortcut": self.shortcut, "by_relation": self.by_relation(),
                "errors": self.errors}

    def summary(self) -> str:  # pragma: no cover - display only
        counts = ", ".join(f"{v} {k}" for k, v in sorted(self.by_relation().items()))
        return (f"{len(self.judgements)} judgement(s) [{counts or 'none'}]  "
                f"{self.model_calls} model call(s), {self.shortcut} decided without one")


# ---------------------------------------------------------------------------
# the cheap tier
# ---------------------------------------------------------------------------


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _shortcut(claim: Claim, candidates: list[Candidate], pages: list[PageHit],
              ) -> Judgement | None:
    """Decide without the model where the answer is not in doubt.

    Two cases only, both chosen because getting them wrong is nearly impossible:

    - Retrieval found nothing → `NEW`. There is no candidate to relate to.
    - A candidate is a near-verbatim restatement → `CORROBORATES`. At 0.9 token
      overlap the two sentences say the same thing.

    Everything else goes to the model. The temptation to add more heuristics here is
    exactly how a system starts making confident wrong edits.
    """
    if not candidates:
        target = pages[0].page_id if pages else None
        return Judgement(claim_id=claim.claim_id, relation=Relation.NEW, confidence=0.9,
                         target_page_id=target,
                         rationale="nothing in the notes matched this claim",
                         model="heuristic")

    best = candidates[0]
    if _jaccard(claim.text, best.text) >= 0.90:
        return Judgement(
            claim_id=claim.claim_id, relation=Relation.CORROBORATES, confidence=0.95,
            target_page_id=best.page_id, target_block_id=best.block_id,
            existing_text=best.text,
            rationale="near-verbatim restatement of an existing block",
            model="heuristic",
        )
    return None


# ---------------------------------------------------------------------------
# the model tier
# ---------------------------------------------------------------------------


def _render_candidates(candidates: list[Candidate], pages: list[PageHit]) -> str:
    lines = ["EXISTING BLOCKS (candidates for corroborate / refine / supersede / "
             "duplicate / contradict)"]
    if not candidates:
        lines.append("  (none)")
    for c in candidates:
        lines.append(f"  - block_id: {c.block_id}\n    page: {c.page_title} "
                     f"({c.page_id})\n    text: {c.text[:600]}")
    lines.append("")
    lines.append("CANDIDATE PAGES (for new / extends)")
    if not pages:
        lines.append("  (none — the base is empty or unrelated)")
    for p in pages:
        lines.append(f"  - page_id: {p.page_id}\n    title: {p.title}\n    role: {p.role}")
    return "\n".join(lines)


def classify_one(claim: Claim, source: Source, index: Index, model: Model, *,
                 effort: str = "high", top_blocks: int = 8, top_pages: int = 5,
                 ) -> Judgement:
    """Classify one claim against the neighbourhood retrieval found for it."""
    candidates = index.blocks_for(claim.text, top=top_blocks)
    pages = index.pages_for(claim.text, claim.topics, top=top_pages)

    quick = _shortcut(claim, candidates, pages)
    if quick is not None:
        return quick

    # The candidate set is stable for this claim but the claim text is not — so the
    # candidates go in the cached prefix and the claim in the user turn.
    prefix = _render_candidates(candidates, pages)
    prompt = (
        f"SOURCE\n  title: {source.title}\n  kind: {source.kind}\n"
        f"  url: {source.url or '(local)'}\n"
        f"  published: {source.published_at or 'unknown'}\n\n"
        f"NEW CLAIM\n  {claim.text}\n"
        f"  type: {claim.type.value}\n"
        f"  topics: {', '.join(claim.topics) or '(none)'}\n\n"
        "How does this claim relate to the existing notes?"
    )

    try:
        payload = model.json(task="classify", system=SYSTEM, prompt=prompt,
                             schema=SCHEMA, effort=effort, cache_prefix=prefix)
    except ModelError as e:
        # A failed classification must not become a silent edit. `NEW` with low
        # confidence routes it to review, which is the safe direction.
        log.warning("classification failed for %s: %s", claim.claim_id, e)
        return Judgement(claim_id=claim.claim_id, relation=Relation.NEW, confidence=0.0,
                         target_page_id=pages[0].page_id if pages else None,
                         rationale=f"classification failed: {e}", model="error")

    try:
        relation = Relation(payload["relation"])
    except (KeyError, ValueError):
        relation = Relation.NEW

    judgement = Judgement(
        claim_id=claim.claim_id,
        relation=relation,
        confidence=max(0.0, min(1.0, float(payload.get("confidence", 0.0)))),
        target_page_id=payload.get("target_page_id") or None,
        target_block_id=payload.get("target_block_id") or None,
        existing_text=payload.get("existing_text") or None,
        rationale=(payload.get("rationale") or "").strip(),
        model=model.model,
    )

    # Repair references the model invented. A judgement pointing at a block that does
    # not exist would fail at apply time with a confusing 404; catching it here turns
    # it into a routine low-confidence review item.
    valid_blocks = {c.block_id for c in candidates}
    valid_pages = {p.page_id for p in pages} | {c.page_id for c in candidates}

    if judgement.target_block_id and judgement.target_block_id not in valid_blocks:
        log.debug("model named an unknown block %s; dropping the reference",
                  judgement.target_block_id)
        judgement.target_block_id = None
        if relation in (Relation.CORROBORATES, Relation.REFINES, Relation.SUPERSEDES,
                        Relation.DUPLICATE, Relation.CONTRADICTS):
            judgement.relation = Relation.NEW
            judgement.confidence = min(judgement.confidence, 0.4)
            judgement.rationale += " (target block not found; routed to review as new)"

    if judgement.target_page_id and judgement.target_page_id not in valid_pages:
        judgement.target_page_id = None
    if judgement.target_page_id is None:
        if judgement.target_block_id:
            judgement.target_page_id = next(
                (c.page_id for c in candidates if c.block_id == judgement.target_block_id),
                None)
        elif pages:
            judgement.target_page_id = pages[0].page_id

    return judgement


def classify(claims: list[Claim], source: Source, index: Index, model: Model, *,
             effort: str = "high", top_blocks: int = 8, top_pages: int = 5,
             ) -> ClassifyResult:
    """Classify every claim from a source."""
    result = ClassifyResult()
    before = model.usage.calls
    for claim in claims:
        try:
            judgement = classify_one(claim, source, index, model, effort=effort,
                                     top_blocks=top_blocks, top_pages=top_pages)
        except Exception as e:
            result.errors.append(f"{claim.claim_id}: {e}")
            continue
        if judgement.model == "heuristic":
            result.shortcut += 1
        result.judgements.append(judgement)
    result.model_calls = model.usage.calls - before
    return result


# ---------------------------------------------------------------------------
# contradictions
# ---------------------------------------------------------------------------


def adjudicate(claim: Claim, source: Source, existing_text: str,
               existing_page: str, model: Model, *, effort: str = "high") -> dict:
    """Lay out a contradiction for a human. Does not resolve it.

    The `recommendation` field is advisory and is rendered as a suggestion in the
    review UI, never acted on. This function exists to save you the ten minutes of
    working out *what kind* of disagreement you are looking at — most "contradictions"
    turn out to be temporal (both were true, at different times) or scope (they are
    about different things), and knowing which is most of the work.
    """
    prompt = (
        f"EXISTING NOTE (page: {existing_page})\n  {existing_text}\n\n"
        f"NEW CLAIM (source: {source.title}"
        f"{', ' + source.published_at if source.published_at else ''})\n  {claim.text}\n\n"
        "These appear to conflict. Characterise the disagreement for the person who "
        "has to decide."
    )
    return model.json(
        task="adjudicate",
        system=(
            "You analyse conflicts between a personal note and a new source. You do NOT "
            "decide the outcome — a human does. Your job is to make the decision fast "
            "and well-informed.\n\n"
            "Classify the conflict:\n"
            "- `factual` — one is simply wrong.\n"
            "- `temporal` — both were true, at different times (prices, versions, roles).\n"
            "- `scope` — they are about different cases and only look like a conflict.\n"
            "- `terminology` — the same words are being used for different things.\n"
            "- `apparent` — no real conflict; the classifier was wrong.\n\n"
            "Be concrete and brief. Name the specific evidence that would settle it."
        ),
        prompt=prompt,
        schema=ADJUDICATE_SCHEMA,
        effort=effort,
    )
