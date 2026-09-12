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

import re
from typing import Any

__all__ = [
    "AUTHOR_BLOCKS",
    "MAX_BLOCKS",
    "MAX_TEXT",
    "SpecError",
    "block_to_text",
    "blocks_from_spec",
    "callout",
    "citation_marker",
    "drop_nulls",
    "make_block",
    "paragraph",
    "plain_text",
    "rich_text",
    "strike_payload",
    "strip_readonly",
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


#: `[[Page title]]` -- how a markdown vault refers to another page.
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


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
    # A markdown vault writes page references as `[[Title]]`, which carry no href
    # at all. Without this a vault's index pages look like ordinary prose to
    # `guess_role`, and the planner appends paragraphs to a hub instead of adding
    # a link to it -- which is the one thing a hub must never get.
    out.extend(_WIKILINK.findall(plain_text(body.get("rich_text"))))
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
                   url: str | None = None, why: str | None = None) -> dict:
    """The provenance note that sits under an edited block.

    `why` is the classifier's reasoning, and it is here rather than only in the ledger
    because of when the question gets asked. Nobody runs `palimpsest provenance` on a
    block; they are reading the page, six months later, wondering why it says that. The
    answer has to be on the page, next to the sentence, or it may as well not exist.
    """
    bits = [source_title]
    if locator:
        bits.append(locator)
    tail = " · ".join(b for b in bits if b)
    head = f"{text} — {tail}" if text else tail
    body = f"{head}\n{why}" if why else head
    return callout(body, icon="📎", color="gray_background", link=url)


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

# ---------------------------------------------------------------------------
# authoring: a whole page, not a line
# ---------------------------------------------------------------------------
#
# Everything above builds one block at a time, which is all the claim pipeline ever
# needed: it appends a sentence, marks one up, or footnotes one. Composing a *page* is a
# different job. A page the model designed — headings, a summary callout, a table, a
# diagram — cannot be expressed as a list of paragraphs, and a knowledge base made only
# of paragraphs is a worse artefact than the sources it was built from.
#
# So the model is allowed to emit a small block *spec*, and this is the boundary that
# turns that spec into Notion blocks. It is a boundary rather than a passthrough on
# purpose: model output goes through `_spec_to_block`, which accepts a known vocabulary
# and drops everything else. Handing arbitrary JSON to the Notion API would mean any
# malformed field became a 400 in the middle of a patch — halfway through rewriting
# somebody's page.

#: Block types the author vocabulary accepts, and how each carries its text.
#:
#: `children` is allowed on the containers that Notion nests: a toggle holds its
#: contents, a column holds blocks, a callout can hold a paragraph.
AUTHOR_BLOCKS = frozenset({
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "toggle",
    "quote", "callout", "code", "divider", "equation",
    "image", "video", "file", "pdf", "bookmark", "embed",
    "table_of_contents", "breadcrumb", "column_list", "column",
})

#: Types that nest other blocks.
NESTING = frozenset({"toggle", "callout", "column_list", "column",
                     "bulleted_list_item", "numbered_list_item", "to_do", "quote"})

#: Types whose content is a URL rather than text. Notion calls these "file objects" and
#: takes either an uploaded file or an `external` URL; only the second is available
#: without an upload round-trip, so that is what the author vocabulary offers.
MEDIA = frozenset({"image", "video", "file", "pdf"})

#: Languages Notion accepts on a code block. Anything else becomes `plain text` rather
#: than a 400 — a wrong highlight is a cosmetic problem, a rejected patch is not.
CODE_LANGUAGES = frozenset({
    "abap", "arduino", "bash", "basic", "c", "c#", "c++", "clojure", "coffeescript",
    "css", "dart", "diff", "docker", "elixir", "elm", "erlang", "flow", "fortran",
    "f#", "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "html", "java",
    "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "lua", "makefile", "markdown", "markup", "matlab", "mermaid", "nix",
    "objective-c", "ocaml", "pascal", "perl", "php", "plain text", "powershell",
    "prolog", "protobuf", "python", "r", "reason", "ruby", "rust", "sass", "scala",
    "scheme", "scss", "shell", "sql", "swift", "typescript", "vb.net", "verilog",
    "vhdl", "visual basic", "webassembly", "xml", "yaml",
})

#: How deep a spec may nest. Notion itself allows more; this is a guard against a model
#: emitting a self-similar structure that expands into thousands of blocks.
MAX_DEPTH = 3

#: How many blocks one spec may produce, all levels counted.
MAX_BLOCKS = 200


class SpecError(ValueError):
    """A block spec the author vocabulary will not build."""


def blocks_from_spec(spec: list[dict] | None) -> list[dict]:
    """Turn a model-authored block spec into Notion blocks.

    Unknown types are dropped rather than raising: one unsupported block in a page of
    thirty should cost that block, not the page. A spec that produces *nothing at all*
    does raise, because silently writing an empty page is worse than failing loudly.
    """
    out = _spec_list(spec or [], depth=0, budget=[MAX_BLOCKS])
    if not out:
        raise SpecError("the block spec produced no blocks")
    return out


def _spec_list(spec: Any, depth: int, budget: list[int]) -> list[dict]:
    if not isinstance(spec, list) or depth > MAX_DEPTH:
        return []
    out: list[dict] = []
    for item in spec:
        if budget[0] <= 0:
            break
        block = _spec_to_block(item, depth, budget)
        if block is not None:
            budget[0] -= 1
            out.append(block)
    return out


def _spec_to_block(item: Any, depth: int, budget: list[int]) -> dict | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "paragraph").strip()
    if kind not in AUTHOR_BLOCKS:
        return None

    text = str(item.get("text") or "")
    body: dict[str, Any]

    if kind == "divider" or kind == "breadcrumb":
        body = {}
    elif kind == "table_of_contents":
        body = {"color": "default"}
    elif kind == "equation":
        if not text:
            return None
        body = {"expression": text[:MAX_TEXT]}
    elif kind == "code":
        language = str(item.get("language") or "plain text").lower()
        body = {
            "rich_text": rich_text(text),
            "language": language if language in CODE_LANGUAGES else "plain text",
        }
        if item.get("caption"):
            body["caption"] = rich_text(str(item["caption"]))
    elif kind in MEDIA:
        url = str(item.get("url") or "").strip()
        # Only http(s). A `file://` or `data:` URL would either fail at Notion or, worse,
        # ask it to fetch something from the machine this happens to be running on.
        if not url.startswith(("http://", "https://")):
            return None
        body = {"type": "external", "external": {"url": url}}
        if item.get("caption"):
            body["caption"] = rich_text(str(item["caption"]))
    elif kind in ("bookmark", "embed"):
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        body = {"url": url}
        if item.get("caption"):
            body["caption"] = rich_text(str(item["caption"]))
    elif kind == "column_list":
        columns = _spec_list(item.get("children"), depth + 1, budget)
        columns = [c for c in columns if c.get("type") == "column"]
        # Notion rejects a column_list with fewer than two columns, and rejects it at
        # apply time rather than at author time, so it is caught here instead.
        if len(columns) < 2:
            return None
        body = {"children": columns}
    elif kind == "column":
        children = _spec_list(item.get("children"), depth + 1, budget)
        if not children:
            return None
        body = {"children": children}
    elif kind == "callout":
        body = {
            "rich_text": rich_text(text),
            "icon": {"type": "emoji", "emoji": str(item.get("icon") or "\U0001f4a1")[:8]},
            "color": str(item.get("color") or "gray_background"),
        }
    elif kind == "to_do":
        body = {"rich_text": rich_text(text), "checked": bool(item.get("checked"))}
    else:
        body = {"rich_text": rich_text(text)}

    if kind in NESTING and kind not in ("column", "column_list"):
        children = _spec_list(item.get("children"), depth + 1, budget)
        if children:
            body["children"] = children

    return {"object": "block", "type": kind, kind: body}


def strip_readonly(block: dict) -> dict | None:
    """A Notion block as fetched, reduced to something that can be created again.

    This is what makes a whole-section rewrite reversible. The mirror keeps every
    block's raw JSON, and the undo of "replace these six paragraphs" is "put those exact
    six back" — which means rebuilding them from the snapshot, because Notion restores a
    trashed block *at the end of the page* rather than where it was. Order is what a
    reader notices; block ids are not.

    Server-owned fields are dropped: sending `id`, `created_time` or `has_children` back
    is a 400. Unsupported types return None so a rewrite of a page containing one does
    not fail wholesale.
    """
    kind = block.get("type")
    if not kind or kind not in AUTHOR_BLOCKS | {"table", "table_row", "synced_block",
                                                "child_page", "child_database"}:
        return None
    body = block.get(kind)
    if not isinstance(body, dict):
        return None
    # Children come back from their own mirror rows, and explicit nulls are dropped:
    # Notion *returns* `"icon": null` on a paragraph and then rejects the same field on
    # create with "should be an object or `undefined`, instead was `null`". Feeding a
    # block's own response straight back is the obvious way to restore it and fails on
    # exactly this — which is how the first live undo of a rewrite left the page rewritten.
    clean = drop_nulls({k: v for k, v in body.items() if k != "children"})
    return {"object": "block", "type": kind, kind: clean}


def drop_nulls(value: Any) -> Any:
    """Recursively remove keys whose value is `None`, so a fetched block can be created."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value
