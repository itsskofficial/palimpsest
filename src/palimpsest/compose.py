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

__all__ = ["COMPOSE_GROUP", "ComposeResult", "compose_page", "compose_sections",
           "rewrite_page"]

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
        #: How many composition calls wrote it, and how many of those fell back to bullets.
        self.sections = 1
        self.fallback_sections = 0

    @property
    def ok(self) -> bool:
        return bool(self.children) and not self.missing

    def as_dict(self) -> dict:
        return {"title": self.title, "icon": self.icon, "blocks": len(self.children),
                "claims_covered": len(self.covered), "claims_missing": sorted(self.missing)}


def compose_page(claims: list[Claim], source: Source, model: Model, *,
                 effort: str = "high", existing: str | None = None,
                 part: tuple[int, int] | None = None) -> ComposeResult:
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
    if part is not None:
        n, total = part
        prompt += (
            f"This is PART {n} OF {total} of one page, in the order the source presents "
            "it. Write only this part's sections, starting with a heading_2 that names what "
            "this part covers. "
            + ("Open with one or two sentences saying what the whole topic is. "
               if n == 1 else "Do not repeat an opening; the page already has one. ")
            + "`title` and `icon` describe the whole page.\n\n")
    prompt += "Lay out the page."

    payload = model.json(task="compose", system=SYSTEM, prompt=prompt, schema=SCHEMA,
                         effort=effort)

    wanted = {c.claim_id for c in claims}
    by_id = {c.claim_id: c for c in claims}
    spec: list[dict] = []
    covered: set[str] = set()
    for item in payload.get("blocks") or []:
        if not isinstance(item, dict):
            continue
        ids = [str(i) for i in (item.get("claim_ids") or []) if str(i) in wanted]
        covered.update(ids)
        # Keys starting with `_` are ours, so one arriving from the model is dropped
        # rather than trusted -- `_cite` becomes rich text on the page.
        block = {k: v for k, v in item.items() if k != "claim_ids" and not str(k).startswith("_")}
        # The claim ids are the only record of which moment in the source each block came
        # from. Stripping them without keeping that was how a composed page lost every
        # citation: a page written from a lecture, with no way back to the lecture.
        cite = _citations([by_id[i] for i in ids], source)
        if cite:
            block["_cite"] = cite
        spec.append(block)

    try:
        children = B.blocks_from_spec(spec)
    except B.SpecError as e:
        log.warning("composition produced no usable blocks: %s", e)
        children = []

    # After, not before: a composition that produced nothing must still come back empty,
    # rather than as a page holding only the line that says where it came from. A part of a
    # longer page leaves it to the caller, which adds it once at the end.
    source_line = _source_block(source)
    if source_line and children and part is None:
        children += B.blocks_from_spec([source_line])

    return ComposeResult(
        title=str(payload.get("title") or source.title or "Untitled")[:120],
        icon=str(payload.get("icon") or "\U0001f331")[:8],
        summary=str(payload.get("summary") or "")[:400],
        children=children,
        covered=covered,
        missing=wanted - covered,
    )


#: Claims per composition call. One call laying out a whole long source -- 183 claims
#: from a 77-minute talk -- has to emit every block of the page in a single response, and
#: it came back unusable, so the page fell back to 183 one-line bullets. Composed in parts
#: of this size, in source order, each part is a response the model writes well, and a
#: part that fails costs only its own sections.
COMPOSE_GROUP = 40


def compose_sections(claims: list[Claim], source: Source, model: Model, *,
                     effort: str = "high", group: int = COMPOSE_GROUP) -> ComposeResult:
    """Lay out a page of any length, in parts, as one page.

    Short sources take one call, exactly as before. Longer ones are split in the order the
    source presents them, each part composed as sections of the same page, and joined. A
    part that fails, or drops a claim, falls back to cited bullets for that part alone --
    the rest of the page is still written properly, and no claim is lost either way.
    """
    ordered = sorted(claims, key=lambda c: (c.anchor.start or 0) if c.anchor else 0)
    if len(ordered) <= group:
        result = compose_page(ordered, source, model, effort=effort)
        result.sections, result.fallback_sections = 1, 0 if result.ok else 1
        return result

    parts = [ordered[i:i + group] for i in range(0, len(ordered), group)]
    children: list[dict] = []
    title = icon = summary = None
    fell_back = 0
    for n, part in enumerate(parts, start=1):
        piece: ComposeResult | None
        try:
            piece = compose_page(part, source, model, effort=effort,
                                  part=(n, len(parts)))
        except Exception as e:
            log.warning("could not compose part %d of %d: %s", n, len(parts), e)
            piece = None
        if piece is not None and piece.ok:
            children.extend(piece.children)
            title = title or piece.title
            icon = icon or piece.icon
            summary = summary or piece.summary
        else:
            fell_back += 1
            children.extend(_bullets(part, source))

    source_line = _source_block(source)
    if source_line:
        children += B.blocks_from_spec([source_line])

    composed = ComposeResult(
        title=(title or source.title or "Untitled")[:120],
        icon=icon or "\U0001f331",
        summary=summary or "",
        children=children,
        covered={c.claim_id for c in ordered},
        missing=set(),
    )
    composed.sections, composed.fallback_sections = len(parts), fell_back
    return composed


def _bullets(claims: list[Claim], source: Source) -> list[dict]:
    """The fallback for one part: each claim a bullet, carrying its own citation."""
    return B.blocks_from_spec([
        {"type": "bulleted_list_item", "text": c.text, "_cite": _citations([c], source)}
        for c in claims])


#: How many citation markers one block carries. A paragraph folding eight claims from
#: eight moments of a lecture would otherwise end in a row of timestamps longer than
#: the sentence.
MAX_CITATIONS_PER_BLOCK = 3


def _citations(claims: list[Claim], source: Source) -> list[dict]:
    """Small linked markers for where in the source a block's claims were made.

    Only markers that link somewhere. A locator with no URL -- a pasted note's
    "document" -- is a bracketed word that cannot be followed, which is noise rather than
    a citation.
    """
    runs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        anchor = claim.anchor
        url = (anchor.url if anchor else None) or source.url
        if not url:
            continue
        label = (anchor.locator if anchor and anchor.locator
                 and len(anchor.locator) <= 24 else "source")
        if (label, url) in seen:
            continue
        seen.add((label, url))
        runs.extend(B.citation_marker(label, url))
        if len(seen) >= MAX_CITATIONS_PER_BLOCK:
            break
    return runs


def _source_block(source: Source) -> dict | None:
    """A closing line naming the source, so the page can be traced even where a block
    carries no marker of its own."""
    if not source.url:
        return None
    return {"type": "callout", "icon": "\U0001f4ce", "color": "gray_background",
            "text": f"Source: {source.title or source.url}", "_link": source.url}


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
