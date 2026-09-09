"""Writing a page, rather than appending a line to one.

The claim pipeline is deliberately incremental: a claim arrives, it is classified, and it
becomes one small edit. That is right for maintenance and wrong for authorship. Run it on
an empty workspace and you get pages that are a stack of bullets in arrival order —
every fact present, nothing readable. A knowledge base like that is worse than the
sources it was built from, because at least the sources had headings.

So this module does the other job. Given a set of claims that belong together, it asks
the model to *lay out a page*: a summary worth reading first, headings that reflect the
shape of the topic, prose rather than fragments, a callout for the thing that matters,
code where code is clearer than a sentence. And given an existing page it can propose the
same treatment as a rewrite.

Three properties are kept, because they are what makes handing over this much authority
reasonable rather than reckless:

**Every claim survives.** The layout is checked against the claims that went in, and a
composition that dropped one is rejected rather than applied. Rewriting a page is allowed
to change how it reads; it is not allowed to quietly lose a fact.

**Every claim keeps its citation.** The composer is told which citation belongs to which
claim and emits them inline, so a rewritten page traces back exactly as an incrementally
built one does.

**The old version is kept.** A rewrite is a `REWRITE_SECTION` operation, which snapshots
the blocks it replaces, so undo restores the page as it was.
"""

from __future__ import annotations

import logging
from typing import Any

from palimpsest.llm import Model
from palimpsest.notion import blocks as B
from palimpsest.types import Claim, Operation, OpKind, Relation, Source

__all__ = ["ComposeResult", "compose_page", "rewrite_page"]

log = logging.getLogger("palimpsest.compose")

SYSTEM = """\
You are laying out a page in someone's personal knowledge base. You are given a set of \
claims that belong on it, and you decide how the page should read.

Write the page you would want to find in six months when you have forgotten the detail.

- Open with one or two sentences that say what the topic *is*. No preamble, no "this page \
covers".
- Group related claims under `heading_2`s. Do not invent a heading for a single claim.
- Write prose. Join claims that belong in one paragraph into one paragraph; a page of \
one-sentence bullets is the thing this exists to replace. Use lists only for things that \
are genuinely a list.
- Use a `callout` at most once or twice, for the caveat or open question that would \
otherwise be missed.
- Use `code` when the claim is about an API, a command or a formula, with the language set.
- Use `divider` sparingly, `toggle` for detail worth collapsing.

Rules that are not style:

- **Every claim id given to you must appear in `claim_ids` on exactly one block.** You may \
merge several claims into one paragraph — then that block lists all their ids. Dropping a \
claim silently loses a fact the user asked to keep.
- Do not add facts that are not in the claims. You are laying out what is there, not \
writing an essay about the topic.
- Do not include the citation text; citations are attached afterwards from the claim ids.
- `title` is the page's title: a noun phrase, no more than about six words.
- `icon` is one emoji that suits the topic."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "icon": {"type": "string"},
        "summary": {"type": "string"},
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                    "language": {"type": ["string", "null"]},
                    "icon": {"type": ["string", "null"]},
                    "checked": {"type": ["boolean", "null"]},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "text", "language", "icon", "checked", "claim_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "icon", "summary", "blocks"],
    "additionalProperties": False,
}


class ComposeResult:
    """A laid-out page: the blocks to write, and what was checked about them."""

    def __init__(self, title: str, icon: str, summary: str, children: list[dict],
                 covered: set[str], missing: set[str]) -> None:
        self.title = title
        self.icon = icon
        self.summary = summary
        self.children = children
        self.covered = covered
        self.missing = missing

    @property
    def ok(self) -> bool:
        return bool(self.children) and not self.missing

    def as_dict(self) -> dict:
        return {"title": self.title, "icon": self.icon, "blocks": len(self.children),
                "claims_covered": len(self.covered), "claims_missing": sorted(self.missing)}


def compose_page(claims: list[Claim], source: Source, model: Model, *,
                 effort: str = "high", existing: str | None = None) -> ComposeResult:
    """Ask the model to lay out a page for these claims.

    `existing` is the current text of a page being rewritten, so the composer can keep
    what is there and fold the new claims in rather than replacing the page with only
    the new material — which is the obvious way to make a rewrite destructive while
    every individual step looks correct.
    """
    numbered = "\n".join(
        f"  [{c.claim_id}] {c.text}" for c in claims)
    prompt = (
        f"SOURCE\n  title: {source.title}\n  kind: {source.kind}\n\n"
        f"CLAIMS ({len(claims)}), each with the id you must reference:\n{numbered}\n\n"
    )
    if existing:
        prompt += (
            "THE PAGE AS IT STANDS. Keep what is worth keeping and fold the claims in; "
            "do not throw this away and write only the new material.\n"
            f"{existing[:6000]}\n\n")
    prompt += "Lay out the page."

    payload = model.json(task="compose", system=SYSTEM, prompt=prompt, schema=SCHEMA,
                         effort=effort)

    wanted = {c.claim_id for c in claims}
    spec: list[dict] = []
    covered: set[str] = set()
    for item in payload.get("blocks") or []:
        if not isinstance(item, dict):
            continue
        ids = [str(i) for i in (item.get("claim_ids") or []) if str(i) in wanted]
        covered.update(ids)
        spec.append({k: v for k, v in item.items() if k != "claim_ids"})

    try:
        children = B.blocks_from_spec(spec)
    except B.SpecError as e:
        log.warning("composition produced no usable blocks: %s", e)
        children = []

    return ComposeResult(
        title=str(payload.get("title") or source.title or "Untitled")[:120],
        icon=str(payload.get("icon") or "\U0001f331")[:8],
        summary=str(payload.get("summary") or "")[:400],
        children=children,
        covered=covered,
        missing=wanted - covered,
    )


def rewrite_page(page_id: str, block_ids: list[str], anchor_block_id: str | None,
                 result: ComposeResult, *, claim_id: str | None = None) -> Operation:
    """Turn a composition into one reversible operation over an existing page."""
    return Operation(
        kind=OpKind.REWRITE_SECTION,
        target=page_id,
        payload={
            "children": result.children,
            "replaced": list(block_ids),
            "anchor_block_id": anchor_block_id,
            "title": result.title,
            "summary": result.summary,
        },
        claim_id=claim_id,
        relation=Relation.REFINES,
    )


def compose_operation(parent_page_id: str, result: ComposeResult,
                      claim_id: str | None = None) -> Operation:
    """Turn a composition into a page-creation operation."""
    return Operation(
        kind=OpKind.CREATE_PAGE,
        target=parent_page_id,
        payload={
            "title": result.title,
            "icon": result.icon,
            "children": result.children,
            "summary": result.summary,
        },
        claim_id=claim_id,
        relation=Relation.NEW,
    )


def page_text(store: Any, page_id: str, limit: int = 120) -> str:
    """The current page as plain text, for handing to a rewrite."""
    lines = []
    for block in store.get_blocks(page_id, limit=limit) or []:
        text = (block.get("text") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)
