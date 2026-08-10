"""Turn judgements into a patch: small typed operations, each with an inverse.

This is where the seven relations become actual edits, and where most of the product's
character lives. The mapping:

| relation | what happens to your notes |
|---|---|
| `NEW` | append a block to the best-fitting page, or create one if nothing fits |
| `CORROBORATES` | **add a citation to the existing block. No prose is added.** |
| `REFINES` | edit the block in place; footnote the previous wording |
| `SUPERSEDES` | strike the old text, append the new, footnote both |
| `DUPLICATE` | link the two pages. Never add the text again. |
| `EXTENDS` | append, but to the page the classifier named rather than the obvious one |
| `CONTRADICTS` | **no write at all** — a review item carrying both sides |

`CORROBORATES` is the one to look at if you want to understand why this design fixes
the reported problem. It is the most common relation on a knowledge base you have been
keeping for a while, and it produces exactly one small grey marker rather than another
paragraph saying what you already said.

**Nothing here talks to the network.** The planner is a pure function from judgements
to a `Patch`, which is what makes the whole decision layer testable offline and what
lets `--dry-run` be genuinely informative rather than a guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from palimpsest.types import (
    Claim,
    Judgement,
    Operation,
    OpKind,
    Patch,
    Relation,
    Source,
    new_id,
)

__all__ = ["PlanResult", "plan"]

log = logging.getLogger("palimpsest.plan")

#: Roles whose pages should gain a *link* rather than prose. Appending a paragraph to a
#: hub page is how index pages turn into essays nobody reads.
LINK_ONLY_ROLES = frozenset({"hub"})


@dataclass
class PlanResult:
    patch: Patch
    review: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def auto(self) -> int:
        return len(self.patch.operations)

    def as_dict(self) -> dict:
        return {"patch": self.patch.as_dict(), "review": self.review,
                "skipped": self.skipped}

    def summary(self) -> str:  # pragma: no cover - display only
        return (f"{len(self.patch)} operation(s), {len(self.review)} needing review, "
                f"{len(self.skipped)} skipped")


def _label(source: Source) -> str:
    """The short marker that appears inline next to a corroborated sentence."""
    if source.author:
        return source.author.split(",")[0].strip()[:24]
    title = (source.title or source.kind).strip()
    return (title[:22] + "…") if len(title) > 24 else title


def _cite_text(source: Source, claim: Claim) -> str:
    bits = [source.title or source.kind]
    if claim.anchor and claim.anchor.locator:
        bits.append(claim.anchor.locator)
    return " · ".join(b for b in bits if b)


def _anchor_url(source: Source, claim: Claim) -> str | None:
    """Prefer the deep link — the timestamp, the page — over the bare source URL."""
    if claim.anchor and claim.anchor.url:
        return claim.anchor.url
    return source.url


def plan(judgements: list[Judgement], claims: dict[str, Claim], source: Source,
         store, *, min_confidence: float = 0.75, footnotes: bool = True,
         default_parent: str | None = None) -> PlanResult:
    """Build a patch from judgements, routing what a human must see to `review`.

    An operation reaching the patch means "this may be applied, subject to the autonomy
    setting". An item reaching `review` means "a human must look at this before
    anything happens", and there are exactly two ways in: the relation is
    `CONTRADICTS`, or confidence is below the bar.
    """
    patch = Patch(patch_id=new_id("pch_"), source_id=source.source_id)
    result = PlanResult(patch=patch)

    for judgement in judgements:
        claim = claims.get(judgement.claim_id)
        if claim is None:  # pragma: no cover - defensive
            continue

        # Contradictions never produce an operation, at any confidence.
        if judgement.relation is Relation.CONTRADICTS:
            result.review.append({
                "reason": "contradiction",
                "claim": claim.as_dict(),
                "judgement": judgement.as_dict(),
                "existing_text": judgement.existing_text,
                "page": _page_title(store, judgement.target_page_id),
            })
            continue

        if judgement.confidence < min_confidence:
            result.review.append({
                "reason": "low_confidence",
                "claim": claim.as_dict(),
                "judgement": judgement.as_dict(),
                "page": _page_title(store, judgement.target_page_id),
            })
            continue

        ops = _operations_for(judgement, claim, source, store, footnotes=footnotes,
                              default_parent=default_parent)
        if not ops:
            result.skipped.append({"claim": claim.as_dict(),
                                   "judgement": judgement.as_dict(),
                                   "reason": "no applicable target"})
            continue
        patch.operations.extend(ops)

    return result


def _page_title(store, page_id: str | None) -> str:
    if not page_id:
        return ""
    page = store.get_page(page_id)
    return page.get("title", "") if page else ""


def _page_role(store, page_id: str | None) -> str:
    if not page_id:
        return "reference"
    page = store.get_page(page_id)
    return (page.get("role") or "reference") if page else "reference"


def _footnote_op(judgement: Judgement, claim: Claim, source: Source, text: str,
                 store) -> Operation:
    block = store.get_block(judgement.target_block_id) if judgement.target_block_id else None
    return Operation(
        kind=OpKind.INSERT_FOOTNOTE,
        target=judgement.target_block_id or "",
        payload={
            "text": text,
            "source_title": _cite_text(source, claim),
            "locator": claim.anchor.locator if claim.anchor else None,
            "url": _anchor_url(source, claim),
            "parent_page_id": (block or {}).get("page_id") or judgement.target_page_id,
            "anchor": claim.anchor.as_dict() if claim.anchor else None,
        },
        claim_id=claim.claim_id,
        relation=judgement.relation,
    )


def _operations_for(judgement: Judgement, claim: Claim, source: Source, store, *,
                    footnotes: bool, default_parent: str | None) -> list[Operation]:
    relation = judgement.relation
    url = _anchor_url(source, claim)

    # -- CORROBORATES: a citation, and nothing else -------------------------
    if relation is Relation.CORROBORATES:
        if not judgement.target_block_id:
            return []
        return [Operation(
            kind=OpKind.ADD_CITATION,
            target=judgement.target_block_id,
            payload={"label": _label(source), "url": url,
                     "anchor": claim.anchor.as_dict() if claim.anchor else None},
            claim_id=claim.claim_id,
            relation=relation,
        )]

    # -- DUPLICATE: link the pages, never re-add the text --------------------
    if relation is Relation.DUPLICATE:
        if not judgement.target_page_id:
            return []
        page = store.get_page(judgement.target_page_id) or {}
        return [Operation(
            kind=OpKind.LINK_PAGES,
            target=judgement.target_page_id,
            payload={"label": f"also covered in {source.title}"[:180],
                     "url": url, "note": judgement.rationale},
            claim_id=claim.claim_id,
            relation=relation,
        )] if page else []

    # -- REFINES: edit in place, keep the old wording in a footnote ----------
    if relation is Relation.REFINES:
        if not judgement.target_block_id:
            return []
        block = store.get_block(judgement.target_block_id)
        if block is None:
            return []
        ops = [Operation(
            kind=OpKind.UPDATE_TEXT,
            target=judgement.target_block_id,
            payload={"block_type": block.get("type", "paragraph"), "text": claim.text,
                     "anchor": claim.anchor.as_dict() if claim.anchor else None},
            claim_id=claim.claim_id,
            relation=relation,
        )]
        if footnotes:
            previous = judgement.existing_text or block.get("text", "")
            ops.append(_footnote_op(judgement, claim, source,
                                    f"refined from: “{previous[:280]}”", store))
        return ops

    # -- SUPERSEDES: strike the old, add the new, footnote both -------------
    if relation is Relation.SUPERSEDES:
        if not judgement.target_block_id:
            return []
        block = store.get_block(judgement.target_block_id)
        if block is None:
            return []
        ops = [
            Operation(
                kind=OpKind.STRIKE_BLOCK,
                target=judgement.target_block_id,
                payload={"text": judgement.existing_text or block.get("text", "")},
                claim_id=claim.claim_id,
                relation=relation,
            ),
            Operation(
                kind=OpKind.APPEND_BLOCK,
                target=block.get("page_id") or judgement.target_page_id or "",
                payload={"block_type": "paragraph", "text": claim.text,
                         "after_block_id": judgement.target_block_id,
                         "anchor": claim.anchor.as_dict() if claim.anchor else None},
                claim_id=claim.claim_id,
                relation=relation,
            ),
        ]
        if footnotes:
            ops.append(_footnote_op(judgement, claim, source, "superseded", store))
        return ops

    # -- NEW / EXTENDS: append, or create a page if nothing fits -------------
    if relation in (Relation.NEW, Relation.EXTENDS):
        page_id = judgement.target_page_id
        if page_id:
            role = _page_role(store, page_id)
            if role in LINK_ONLY_ROLES:
                # Hubs get a link, not a paragraph. This is the page-role rule doing
                # real work: without it, index pages slowly become essays.
                return [Operation(
                    kind=OpKind.LINK_PAGES,
                    target=page_id,
                    payload={"label": claim.text[:180], "url": url},
                    claim_id=claim.claim_id,
                    relation=relation,
                )]
            ops = [Operation(
                kind=OpKind.APPEND_BLOCK,
                target=page_id,
                payload={"block_type": "bulleted_list_item", "text": claim.text,
                         "anchor": claim.anchor.as_dict() if claim.anchor else None},
                claim_id=claim.claim_id,
                relation=relation,
            )]
            if footnotes:
                ops.append(Operation(
                    kind=OpKind.APPEND_BLOCK,
                    target=page_id,
                    payload={
                        "children": [_callout(_cite_text(source, claim), url)],
                        "text": _cite_text(source, claim),
                    },
                    claim_id=claim.claim_id,
                    relation=relation,
                ))
            return ops

        if default_parent:
            # Nothing in the base fits. Create a page rather than forcing the claim
            # somewhere wrong — a misfiled claim is harder to find than a new page.
            title = (claim.topics[0].title() if claim.topics
                     else (source.title or "New note"))[:120]
            return [Operation(
                kind=OpKind.CREATE_PAGE,
                target=default_parent,
                payload={
                    "title": title,
                    "icon": "🌱",
                    "children": [
                        _bullet(claim.text),
                        _callout(_cite_text(source, claim), url),
                    ],
                    "anchor": claim.anchor.as_dict() if claim.anchor else None,
                },
                claim_id=claim.claim_id,
                relation=relation,
            )]
        return []

    return []


# -- block literals ---------------------------------------------------------
# Built here rather than imported from notion.blocks so the planner stays a pure
# data transformation with no dependency on the write path.


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [
                {"type": "text", "text": {"content": text[:2000], "link": None}}]}}


def _callout(text: str, url: str | None) -> dict:
    return {"object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text",
                               "text": {"content": text[:2000],
                                        "link": {"url": url} if url else None},
                               "annotations": {"bold": False, "italic": False,
                                               "strikethrough": False, "underline": False,
                                               "code": False, "color": "gray"}}],
                "icon": {"type": "emoji", "emoji": "📎"},
                "color": "gray_background"}}
