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
         default_parent: str | None = None,
         record_contradictions: bool = False) -> PlanResult:
    """Build a patch from judgements, routing what a human must see to `review`.

    An operation reaching the patch means "this may be applied, subject to the autonomy
    setting". An item reaching `review` means "a human must look at this before anything
    happens", and there are two ways in: confidence below the bar, or a contradiction
    while `record_contradictions` is off.

    `record_contradictions` is what `PALIMPSEST_AUTONOMY=everything` turns on. It does
    not make the system pick a winner — nothing here ever does. It emits an operation
    that writes the disagreement *next to* the contradicted sentence, with both sources
    cited, so the page tells you there is an argument rather than quietly asserting one
    side. The old sentence is left exactly as it was, which is what keeps the operation
    a single reversible append rather than a rewrite.
    """
    patch = Patch(patch_id=new_id("pch_"), source_id=source.source_id)
    result = PlanResult(patch=patch)
    #: Pages this patch is already creating, by lower-cased title — see `_merged`.
    new_pages: dict[str, Operation] = {}

    for judgement in judgements:
        claim = claims.get(judgement.claim_id)
        if claim is None:  # pragma: no cover - defensive
            continue

        if judgement.relation is Relation.CONTRADICTS:
            # Even when recording them automatically, a contradiction the classifier was
            # unsure about still goes to a human: "these two disagree" is only worth
            # writing into the page if we believe it.
            ops = (_contradiction_ops(judgement, claim, source, store, url=_anchor_url(
                source, claim)) if record_contradictions
                and judgement.confidence >= min_confidence else [])
            if not ops:
                result.review.append({
                    "reason": "contradiction",
                    "claim": claim.as_dict(),
                    "judgement": judgement.as_dict(),
                    "existing_text": judgement.existing_text,
                    "page": _page_title(store, judgement.target_page_id),
                })
                continue
            for op in ops:
                op.payload.setdefault("rationale", judgement.rationale)
                op.payload.setdefault("confidence", round(judgement.confidence, 3))
            patch.operations.extend(ops)
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

        # Stamp the reasoning onto every operation, in one place rather than in each
        # of the seven builders. The relation alone says *what* happened; this is the
        # only record of *why this claim, against this block* — and it is what the
        # Notion journal and the review UI both show. Losing it means an edit you can
        # revert but cannot account for, which is the failure the ledger exists to
        # prevent.
        for op in ops:
            op.payload.setdefault("rationale", judgement.rationale)
            op.payload.setdefault("confidence", round(judgement.confidence, 3))

        patch.operations.extend(_merged(ops, new_pages))

    return result


def _contradiction_ops(judgement: Judgement, claim: Claim, source: Source, store, *,
                       url: str | None) -> list[Operation]:
    """Write the disagreement into the page, without deciding it.

    One `append_block` on the page, inserted directly after the contradicted sentence,
    carrying the new claim, the source that made it, and a marker saying this conflicts
    with the line above. The existing text is not touched, struck, or edited — so the
    operation inverts by archiving one block, and a reader who disagrees with the machine
    loses nothing by undoing it.

    This is the automatic action for a contradiction and it is deliberately not a
    resolution. Choosing which of two sourced claims is true is the judgement a person
    keeps; noticing that the two exist and putting them next to each other is the work
    that otherwise never gets done.
    """
    anchor = judgement.target_block_id
    if not anchor:
        return []
    # The parent is the *page*; the contradicted block is only where to insert. Notion
    # rejects an append whose `after` is not a child of the target, so making the block
    # its own parent fails with a message about parentage that reads like a data problem.
    page_id = judgement.target_page_id or (store.get_block(anchor) or {}).get("page_id")
    if not page_id:
        return []
    return [Operation(
        kind=OpKind.APPEND_BLOCK,
        target=page_id,
        payload={
            "children": [
                _conflict(claim.text, _cite_text(source, claim), url),
            ],
            "after_block_id": anchor,
            "contradicts_block_id": anchor,
            "existing_text": judgement.existing_text,
            "anchor": claim.anchor.as_dict() if claim.anchor else None,
        },
        claim_id=claim.claim_id,
        relation=Relation.CONTRADICTS,
    )]


def _conflict(text: str, cite: str, url: str | None) -> dict:
    """A red callout: the competing claim, its source, and what it argues with."""
    return {"object": "block", "type": "callout",
            "callout": {
                "rich_text": [
                    {"type": "text",
                     "text": {"content": "Conflicts with the line above — ", "link": None},
                     "annotations": {"bold": True, "italic": False,
                                     "strikethrough": False, "underline": False,
                                     "code": False, "color": "red"}},
                    {"type": "text", "text": {"content": text[:1800], "link": None}},
                    {"type": "text",
                     "text": {"content": f"  ({cite})",
                              "link": {"url": url} if url else None},
                     "annotations": {"bold": False, "italic": True,
                                     "strikethrough": False, "underline": False,
                                     "code": False, "color": "gray"}},
                ],
                "icon": {"type": "emoji", "emoji": "\u26a0\ufe0f"},
                "color": "red_background"}}


def _merged(ops: list[Operation], new_pages: dict[str, Operation]) -> list[Operation]:
    """Fold a second page-creation with the same title into the first.

    Every claim is classified against the notes *as they were before this source*, which
    is right — the index cannot contain pages that do not exist yet. But it means two
    claims from one document about one topic each come back `new` with nothing to attach
    to, and each asks for its own page.

    The result was visible on the very first capture: a two-sentence note about attention
    produced two Notion pages, both called "Attention". A tool whose purpose is to stop
    notes fragmenting cannot ship that.

    So page creations are coalesced within the patch: the first claim creates the page,
    later ones about the same title become bullets on it. Titles are matched
    case-insensitively because they are derived from topic strings, which vary in case.
    Only `CREATE_PAGE` merges — everything else already targets an existing block or page
    and has nothing to collide with.
    """
    out: list[Operation] = []
    for op in ops:
        if op.kind is not OpKind.CREATE_PAGE:
            out.append(op)
            continue
        key = str(op.payload.get("title", "")).strip().lower()
        first = new_pages.get(key)
        if first is None:
            new_pages[key] = op
            out.append(op)
            continue
        # Fold this claim's blocks into the page already being created. The citation
        # callout comes along with it, so provenance is not lost by merging.
        first.payload.setdefault("children", []).extend(op.payload.get("children") or [])
        first.payload.setdefault("merged_claims", []).append(op.claim_id)
    return out


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
