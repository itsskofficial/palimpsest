"""Reading and writing Notion blocks: plain text out, rich text in.

Notion's content model is a tree of blocks, each carrying a `rich_text` array of runs
with their own annotations and links. Two conversions matter:

- **Block → text**, for retrieval, the classifier and the diff view. Lossy on purpose:
  the classifier reasons about what a sentence *says*, and bold-vs-italic is noise.
- **Text → blocks**, for the applier. This is where footnotes and citations get built,
  and where the house style of an edit is decided.

The citation format is worth stating explicitly, because it is the visible product of
the whole system. A claim added from a source gets a superscript-style marker appended
to the block, and the source itself lands in a small collapsed callout. It is compact
enough that a page with forty citations still reads like notes rather than a
bibliography, and complete enough that every sentence traces back.

**Nothing here talks to the network.** These are pure functions over dicts, which is
what lets the entire patch planner be tested offline.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_TEXT",
    "block_to_text",
    "callout",
    "citation_marker",
    "make_block",
    "paragraph",
    "plain_text",
    "rich_text",
    "strike_payload",
    "text_payload",
]

#: Notion rejects a single rich-text run longer than 2000 characters. Longer content is
#: split across runs rather than truncated — losing the tail of a claim silently is
#: exactly the kind of quiet corruption this project exists to avoid.
MAX_TEXT = 2000

#: Block types that carry a `rich_text` array we can read and edit.
TEXT_BLOCKS = frozenset({
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
})


def plain_text(rich: list[dict] | None) -> str:
    """Concatenate a rich_text array into a plain string."""
    if not rich:
        return ""
    out = []
    for run in rich:
        if isinstance(run, dict):
            out.append(run.get("plain_text") or run.get("text", {}).get("content") or "")
    return "".join(out)


def block_to_text(block: dict) -> str:
    """The readable content of a block, with a marker for structural types."""
    btype = block.get("type", "")
    body = block.get(btype)
    if not isinstance(body, dict):
        return ""
    text = plain_text(body.get("rich_text"))
    if btype == "to_do":
        mark = "x" if body.get("checked") else " "
        return f"[{mark}] {text}"
    if btype in ("heading_1", "heading_2", "heading_3"):
        level = int(btype[-1])
        return f"{'#' * level} {text}"
    if btype in ("bulleted_list_item", "toggle"):
        return f"- {text}"
    if btype == "numbered_list_item":
        return f"1. {text}"
    if btype == "quote":
        return f"> {text}"
    if btype == "code":
        return f"```{body.get('language', '')}\n{text}\n```"
    if btype == "divider":
        return "---"
    if btype == "callout":
        icon = (body.get("icon") or {}).get("emoji", "")
        return f"{icon} {text}".strip()
    if btype in ("child_page", "child_database"):
        return f"[{body.get('title', '')}]"
    return text


def links_in(block: dict) -> list[str]:
    """Page ids this block links to — mentions and inline links to Notion pages."""
    btype = block.get("type", "")
    body = block.get(btype)
    out: list[str] = []
    if not isinstance(body, dict):
        return out
    for run in body.get("rich_text") or []:
        if not isinstance(run, dict):
            continue
        if run.get("type") == "mention":
            mention = run.get("mention") or {}
            if mention.get("type") == "page":
                pid = (mention.get("page") or {}).get("id")
                if pid:
                    out.append(pid.replace("-", ""))
        href = run.get("href") or ""
        if href.startswith("/") and len(href) >= 33:
            out.append(href.lstrip("/").split("?")[0].split("-")[-1].replace("-", ""))
    return out


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------


def rich_text(text: str, *, bold: bool = False, italic: bool = False,
              strike: bool = False, code: bool = False, color: str = "default",
              link: str | None = None) -> list[dict]:
    """Build a rich_text array, splitting at Notion's 2000-character run limit."""
    runs: list[dict] = []
    remaining = text or ""
    if not remaining:
        return []
    while remaining:
        chunk, remaining = remaining[:MAX_TEXT], remaining[MAX_TEXT:]
        run: dict[str, Any] = {
            "type": "text",
            "text": {"content": chunk, "link": {"url": link} if link else None},
            "annotations": {"bold": bold, "italic": italic, "strikethrough": strike,
                            "underline": False, "code": code, "color": color},
        }
        runs.append(run)
    return runs


def paragraph(text: str, **kw) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": rich_text(text, **kw)}}


def bullet(text: str, **kw) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rich_text(text, **kw)}}


def heading(text: str, level: int = 2) -> dict:
    level = max(1, min(3, level))
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": rich_text(text)}}


def callout(text: str, icon: str = "🔖", color: str = "gray_background",
            link: str | None = None) -> dict:
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": rich_text(text, link=link),
            "icon": {"type": "emoji", "emoji": icon},
            "color": color,
        },
    }


def make_block(kind: str, text: str, **kw) -> dict:
    """Build one block of a supported type from plain text."""
    if kind in ("heading_1", "heading_2", "heading_3"):
        return heading(text, int(kind[-1]))
    if kind == "bulleted_list_item":
        return bullet(text, **kw)
    if kind == "callout":
        return callout(text, **kw)
    if kind == "quote":
        return {"object": "block", "type": "quote",
                "quote": {"rich_text": rich_text(text, **kw)}}
    if kind == "divider":
        return {"object": "block", "type": "divider", "divider": {}}
    return paragraph(text, **kw)


# ---------------------------------------------------------------------------
# citations and footnotes — the visible product
# ---------------------------------------------------------------------------


def citation_marker(label: str, url: str | None = None) -> list[dict]:
    """The inline ` [label]` appended to a block when a source corroborates it.

    Small, greyed, linked. This is what `CORROBORATES` produces instead of prose, and
    it is the single reason the base stops growing a new paragraph every time you read
    a second article about something you already know.
    """
    return rich_text(f"  [{label}]", color="gray", link=url)


def footnote_block(text: str, source_title: str, locator: str | None = None,
                   url: str | None = None) -> dict:
    """The collapsed provenance note that sits under an edited block."""
    bits = [source_title]
    if locator:
        bits.append(locator)
    tail = " · ".join(b for b in bits if b)
    return callout(f"{text} — {tail}" if text else tail, icon="📎",
                   color="gray_background", link=url)


def text_payload(block_type: str, text: str, **kw) -> dict:
    """The body of a `PATCH /v1/blocks/{id}` that replaces a block's text.

    Notion wants `{<type>: {"rich_text": [...]}}`, keyed by the block's own type — you
    cannot change a paragraph into a heading this way, and trying returns a validation
    error rather than converting it.
    """
    btype = block_type if block_type in TEXT_BLOCKS else "paragraph"
    return {btype: {"rich_text": rich_text(text, **kw)}}


def strike_payload(block_type: str, text: str) -> dict:
    """Strike a block through rather than deleting it.

    This is what `SUPERSEDES` does to the value it replaces. The old text stays
    visible, struck, with the new value and a footnote beside it — which is the whole
    palimpsest idea, and the reason you can audit an edit six months later.
    """
    return text_payload(block_type, text, strike=True, color="gray")


def append_runs(block: dict, extra: list[dict]) -> dict:
    """Append runs to a block's existing rich_text, preserving its formatting."""
    btype = block.get("type", "paragraph")
    body = block.get(btype) or {}
    existing = list(body.get("rich_text") or [])
    return {btype: {"rich_text": existing + extra}}
