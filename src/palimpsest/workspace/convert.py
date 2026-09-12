"""Markdown in, Notion blocks out, and back again.

Notion's block JSON and a markdown file describe the same thing in different alphabets.
This module is the dictionary between them, and it exists so that everything above the
workspace layer -- the mirror, the planner, the applier -- can keep speaking one
language while the bytes on the other side are a `.md` file in an Obsidian vault rather
than rows in Notion's database.

Two properties matter more than completeness, and both are tested as round trips:

**Structure survives.** A heading is a heading, a nested bullet keeps its nesting, a
checked to-do stays checked. What a page *means* must not degrade by being written to
disk and read back, because the classifier's judgement about a page is made from its
structure as much as its prose.

**Formatting survives.** Bold, italic, inline code, strikethrough and links round trip
as annotations rather than as literal asterisks. This is not cosmetic: `strike_payload`
marks a superseded claim by setting `strikethrough`, so a backend that lost annotations
would lose the difference between "this was true" and "this is true" -- and undo would
restore the wrong thing.

What is deliberately *not* modelled: databases, synced blocks, and Notion's more exotic
embeds. A vault is a vault. `block_to_text` already renders those as a readable
placeholder, and that is what lands in the file.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["md_to_rich", "rich_to_md", "to_blocks", "to_markdown"]


# ---------------------------------------------------------------------------
# inline runs
# ---------------------------------------------------------------------------

#: Ordered because the first match wins, and the longer fences must be tried first:
#: `**` before `*`, or every bold run parses as two empty italics.
_INLINE = [
    ("code", re.compile(r"`([^`]+)`")),
    ("bold", re.compile(r"\*\*([^*]+)\*\*")),
    ("strikethrough", re.compile(r"~~([^~]+)~~")),
    ("italic", re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")),
    ("italic_", re.compile(r"(?<![A-Za-z0-9_])_([^_]+)_(?![A-Za-z0-9_])")),
    ("link", re.compile(r"\[([^\]]*)\]\(([^)]+)\)")),
]

_DEFAULTS = {"bold": False, "italic": False, "strikethrough": False,
             "underline": False, "code": False, "color": "default"}


def _run(text: str, *, link: str | None = None, style: str | None = None) -> dict:
    """One rich-text run. At most one style, because `md_to_rich` is single-pass."""
    out: dict[str, Any] = {
        "type": "text",
        "text": {"content": text, "link": {"url": link} if link else None},
        "annotations": {**_DEFAULTS, **({style: True} if style else {})},
        "plain_text": text,
    }
    if link:
        out["href"] = link
    return out


def md_to_rich(text: str) -> list[dict]:
    """Parse one line of markdown into Notion rich-text runs.

    Deliberately single-pass and non-recursive: nested emphasis (`**bold *and italic***`)
    resolves to the outer style only. Notion itself accepts the nesting, but guessing at
    it from markdown is where converters start silently corrupting text, and a knowledge
    base is not a place to be clever at the cost of being right.
    """
    if not text:
        return []
    runs: list[dict] = []
    cursor = 0
    while cursor < len(text):
        best: tuple[int, str, re.Match] | None = None
        for kind, pattern in _INLINE:
            match = pattern.search(text, cursor)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), kind, match)
        if best is None:
            runs.append(_run(text[cursor:]))
            break
        start, kind, match = best
        if start > cursor:
            runs.append(_run(text[cursor:start]))
        if kind == "link":
            runs.append(_run(match.group(1), link=match.group(2)))
        else:
            runs.append(_run(match.group(1), style=kind.rstrip("_")))
        cursor = match.end()
    return [r for r in runs if r["text"]["content"]]


def rich_to_md(rich: list[dict] | None) -> str:
    """Render Notion rich text back to markdown."""
    out = []
    for run in rich or []:
        content = (run.get("text") or {}).get("content")
        if content is None:
            content = run.get("plain_text", "")
        if not content:
            continue
        ann = run.get("annotations") or {}
        # Emphasis must not wrap surrounding whitespace. `**bold **` is not bold in any
        # renderer -- it prints the asterisks -- and Notion hands out runs with trailing
        # spaces constantly, because a sentence built from a bold lead-in and a plain
        # tail is exactly that shape. Hoist the padding outside the markers.
        stripped = content.strip()
        # A run that is *only* whitespace has no inside: lead and tail would each be the
        # whole string and it would come back doubled.
        lead = content[:len(content) - len(content.lstrip())] if stripped else content
        tail = content[len(content.rstrip()):] if stripped else ""
        content = stripped
        if content:
            # Code first, then outward, so the nesting matches how it parses back.
            if ann.get("code"):
                content = f"`{content}`"
            if ann.get("italic"):
                content = f"*{content}*"
            if ann.get("bold"):
                content = f"**{content}**"
            if ann.get("strikethrough"):
                content = f"~~{content}~~"
        content = f"{lead}{content}{tail}"
        href = run.get("href") or ((run.get("text") or {}).get("link") or {}).get("url")
        if href:
            content = f"[{content}]({href})"
        out.append(content)
    return "".join(out)


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

#: Markdown has no toggle and no callout, so both borrow Obsidian's callout syntax:
#: `> [!note]` is a callout, `> [!note]-` (the trailing dash means "starts folded") is a
#: toggle. A vault written by palimpsest therefore renders correctly in Obsidian, and a
#: callout written by hand in Obsidian arrives here as a callout rather than a quote.
_CALLOUT = re.compile(r"^>\s*\[!(?P<kind>[A-Za-z]+)\](?P<fold>[-+]?)\s*(?P<title>.*)$")
_ADMONITION_ICON = {"note": "📝", "info": "🔖", "tip": "💡", "warning": "⚠️",
                    "danger": "🚨", "quote": "💬", "example": "📎", "bug": "🐛",
                    "success": "✅", "question": "❓", "abstract": "📄"}
_ICON_ADMONITION = {v: k for k, v in _ADMONITION_ICON.items()}

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\d+[.)]\s+(.*)$")
_TODO = re.compile(r"^[-*+]\s+\[([ xX])\]\s*(.*)$")
_IMAGE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]+)\)$")
_FENCE = re.compile(r"^```(\w*)\s*$")
_DIVIDER = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")

#: One indent level. Two spaces is what most editors emit for a nested bullet; four is
#: what the CommonMark spec wants. Accepting either and normalising on write means a
#: vault survives being edited in something other than this tool, which is the whole
#: point of storing it as markdown.
_INDENT = 2


def _body(kind: str, text: str, **extra: Any) -> dict:
    block: dict[str, Any] = {"type": kind, kind: {"rich_text": md_to_rich(text), **extra}}
    return block


def _indent_of(line: str) -> int:
    stripped = line.lstrip(" \t")
    spaces = len(line) - len(stripped)
    spaces += line[:spaces].count("\t") * (_INDENT - 1)
    return spaces // _INDENT


def _starts_a_block(text: str) -> bool:
    """Whether this line begins something other than more of the paragraph above."""
    return bool(_HEADING.match(text) or _BULLET.match(text) or _NUMBER.match(text)
                or _TODO.match(text) or _IMAGE.match(text) or _DIVIDER.match(text)
                or _FENCE.match(text) or text.startswith(">"))


def _prose_runs(lines: list[str]) -> list[dict]:
    """Turn a callout's body lines into blocks, joining wrapped prose."""
    out: list[dict] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append(_body("paragraph", "\n".join(run)))
            run.clear()

    for line in lines:
        if not line:
            flush()
        elif _starts_a_block(line):
            flush()
            out.append(_line_to_block(line))
        else:
            run.append(line)
    flush()
    return out


def _line_to_block(text: str) -> dict:
    """One already-dedented line to one block."""
    todo = _TODO.match(text)
    if todo:
        return _body("to_do", todo.group(2), checked=todo.group(1).lower() == "x")
    heading = _HEADING.match(text)
    if heading:
        return _body(f"heading_{len(heading.group(1))}", heading.group(2),
                     is_toggleable=False)
    bullet = _BULLET.match(text)
    if bullet:
        return _body("bulleted_list_item", bullet.group(1))
    number = _NUMBER.match(text)
    if number:
        return _body("numbered_list_item", number.group(1))
    image = _IMAGE.match(text)
    if image:
        return {"type": "image", "image": {
            "type": "external", "external": {"url": image.group("url")},
            "caption": md_to_rich(image.group("alt"))}}
    if _DIVIDER.match(text):
        return {"type": "divider", "divider": {}}
    if text.startswith(">"):
        return _body("quote", text[1:].strip())
    return _body("paragraph", text)


def to_blocks(markdown: str) -> list[dict]:
    """Parse a markdown document into a nested list of Notion-shaped blocks."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    root: list[dict] = []
    # A stack of (indent, child-list) so nesting is a push and a pop, not recursion.
    stack: list[tuple[int, list[dict]]] = [(-1, root)]
    index = 0

    def place(indent: int, block: dict) -> None:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            stack.append((-1, root))
        stack[-1][1].append(block)
        kind = block["type"]
        body = block.get(kind)
        if isinstance(body, dict):
            body.setdefault("children", [])
            stack.append((indent, body["children"]))

    while index < len(lines):
        raw = lines[index]
        text = raw.strip()
        if not text:
            index += 1
            continue
        indent = _indent_of(raw)

        fence = _FENCE.match(text)
        if fence:
            index += 1
            body_lines = []
            while index < len(lines) and not _FENCE.match(lines[index].strip()):
                body_lines.append(lines[index])
                index += 1
            index += 1
            place(indent, {"type": "code", "code": {
                "rich_text": [_run("\n".join(body_lines))],
                "language": fence.group(1) or "plain text"}})
            continue

        callout = _CALLOUT.match(text)
        if callout:
            kind = callout.group("kind").lower()
            icon = _ADMONITION_ICON.get(kind, "🔖")
            # A folded admonition is a toggle; an open one is a callout.
            if callout.group("fold") == "-":
                block = _body("toggle", callout.group("title"))
            else:
                block = _body("callout", callout.group("title"),
                              icon={"type": "emoji", "emoji": icon},
                              color="gray_background")
            place(indent, block)
            # Continuation lines (`> more text`) become children of the callout, with
            # wrapped prose joined the same way it is joined outside one.
            index += 1
            inner_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                inner_lines.append(lines[index].strip()[1:].strip())
                index += 1
            for child in _prose_runs(inner_lines):
                place(indent + 1, child)
            continue

        # A run of consecutive plain lines is *one* paragraph, which is what markdown
        # says and what anybody writing in an editor with a wrap column assumes.
        #
        # Treating each line as its own block looked harmless and was not. A vault
        # written by hand is full of soft-wrapped prose, so on first sync every sentence
        # fragment became a separate block: separate ids, separate retrieval candidates,
        # separate things for the classifier to anchor a footnote to. And a round trip
        # inserted a blank line at every wrap point, so the tool rewrote the file it had
        # just read.
        if _line_to_block(text)["type"] == "paragraph" and not _IMAGE.match(text):
            run = [text]
            look = index + 1
            while look < len(lines):
                nxt = lines[look].strip()
                if not nxt or _indent_of(lines[look]) != indent:
                    break
                if _starts_a_block(nxt):
                    break
                run.append(nxt)
                look += 1
            place(indent, _body("paragraph", "\n".join(run)))
            index = look
            continue

        place(indent, _line_to_block(text))
        index += 1

    _prune(root)
    return root


def _prune(blocks: list[dict]) -> None:
    """Drop the empty `children` lists the parser adds speculatively."""
    for block in blocks:
        body = block.get(block["type"])
        if not isinstance(body, dict):
            continue
        children = body.get("children")
        if children:
            _prune(children)
        elif "children" in body:
            body.pop("children")


#: Blocks that read as a run-on when written without a blank line between them. Lists
#: and to-dos are the exception -- a blank line between two bullets splits the list.
_TIGHT = frozenset({"bulleted_list_item", "numbered_list_item", "to_do"})


def _is_table_row(text: str) -> bool:
    """Whether this paragraph is really a row of a markdown table.

    A vault has no databases, so the activity ledger is a table and each row arrives as
    its own paragraph block. Separated by blank lines the way paragraphs normally are,
    those rows stop being a table at all -- every renderer shows twelve pipe-delimited
    sentences instead. Detecting the shape is unpleasant, and the alternative is a
    `table` block type that only this one feature would ever use.
    """
    stripped = text.strip()
    return len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|")


def to_markdown(blocks: list[dict] | None, depth: int = 0) -> str:
    """Render a nested block list back to markdown.

    The inverse of `to_blocks` for everything `to_blocks` produces, which is the property
    the round-trip tests pin. Block types it cannot express -- a database view, a synced
    block -- degrade to their readable text rather than vanishing, so a vault never
    silently loses a page's content even when it cannot reproduce its chrome.
    """
    pad = " " * (_INDENT * depth)
    out: list[str] = []
    previous = previous_kind = previous_text = ""

    for block in blocks or []:
        kind = block.get("type", "paragraph")
        raw = block.get(kind)
        body: dict[str, Any] = raw if isinstance(raw, dict) else {}
        text = rich_to_md(body.get("rich_text"))
        children = body.get("children") or []

        tight = (kind in _TIGHT and previous == kind) or (
            kind == "paragraph" == previous_kind and _is_table_row(text)
            and _is_table_row(previous_text))
        if out and not tight:
            out.append("")

        if kind.startswith("heading_") and kind[-1].isdigit():
            out.append(f"{pad}{'#' * int(kind[-1])} {text}")
        elif kind == "bulleted_list_item":
            out.append(f"{pad}- {text}")
        elif kind == "numbered_list_item":
            out.append(f"{pad}1. {text}")
        elif kind == "to_do":
            out.append(f"{pad}- [{'x' if body.get('checked') else ' '}] {text}")
        elif kind == "quote":
            out.append(f"{pad}> {text}")
        elif kind == "divider":
            out.append(f"{pad}---")
        elif kind == "code":
            language = body.get("language") or ""
            if language == "plain text":
                language = ""
            out.append(f"{pad}```{language}")
            out.extend(f"{pad}{line}" for line in text.split("\n"))
            out.append(f"{pad}```")
        elif kind == "image":
            source = body.get("external") or body.get("file") or {}
            out.append(f"{pad}![{rich_to_md(body.get('caption'))}]({source.get('url', '')})")
        elif kind in ("callout", "toggle"):
            icon = ((body.get("icon") or {}).get("emoji")) if kind == "callout" else None
            admonition = _ICON_ADMONITION.get(icon or "", "note" if kind == "callout" else "tip")
            fold = "" if kind == "callout" else "-"
            out.append(f"{pad}> [!{admonition}]{fold} {text}".rstrip())
            nested = to_markdown(children, 0)
            # An empty string splits to [""], which used to leave a bare `>` under
            # every childless callout -- a stray blank quote line in the file.
            if nested:
                out.extend(f"{pad}> {line}".rstrip()
                           for line in nested.splitlines())
            children = []
        elif kind in ("child_page", "child_database"):
            out.append(f"{pad}- [[{body.get('title', 'Untitled')}]]")
        elif kind in ("bookmark", "embed", "link_preview", "video", "pdf", "file"):
            url = body.get("url") or (body.get("external") or {}).get("url", "")
            label = text or url
            out.append(f"{pad}[{label}]({url})" if url else f"{pad}{label}")
        else:
            # A paragraph may hold the newlines of its original soft wrap; re-indent each
            # line rather than emitting one very long one.
            out.append("\n".join(f"{pad}{line}" for line in text.split("\n"))
                       if text else "")

        if children:
            nested = to_markdown(children, depth + 1)
            if nested:
                out.append(nested)
        previous = previous_kind = kind
        previous_text = text

    return "\n".join(line for line in out).strip("\n")
