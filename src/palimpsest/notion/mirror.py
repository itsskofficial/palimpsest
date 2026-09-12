"""Sync Notion into the local mirror.

The mirror is the single most load-bearing piece of infrastructure here, and it exists
because of what Notion's API does *not* give you: no diff, no transactions, no readable
version history. Exact undo, "which source produced this sentence" and time travel are
all properties of our copy, not of Notion.

Two modes:

- **Full** walks every page shared with the integration and every block under it.
  Thousands of requests at ~2.5/second, so a large workspace is minutes, not seconds.
- **Incremental** compares `last_edited_time` against what we already hold and refetches
  only the pages that moved. This is what you run on a schedule, and it is the reason a
  large workspace stays fresh without re-reading itself hourly.

**Page profiles** are the part that has no Notion equivalent. A page's *role* — hub,
deep dive, literature note, scratchpad, project log — determines what an edit to it
should look like: you append to a deep dive, but you add a *link* to a hub. Notion does
not know a page's role, so we infer it, cheaply and heuristically first, with the model
only where the heuristic is unsure.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from palimpsest.notion.blocks import block_to_text, links_in, plain_text
from palimpsest.notion.client import NotionError
from palimpsest.notion.protocol import Workspace

__all__ = [
    "MirrorResult",
    "guess_role",
    "page_title",
    "refresh_pages",
    "sync",
]

log = logging.getLogger("palimpsest.mirror")

#: The roles a page can play. Drives how the planner edits it.
ROLES = ("hub", "deep_dive", "literature_note", "scratchpad", "project_log", "reference")


@dataclass
class MirrorResult:
    pages: int = 0
    blocks: int = 0
    links: int = 0
    skipped: int = 0
    archived: int = 0
    api_calls: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"pages": self.pages, "blocks": self.blocks, "links": self.links,
                "skipped": self.skipped, "archived": self.archived,
                "api_calls": self.api_calls, "seconds": round(self.seconds, 1),
                "errors": self.errors}

    def summary(self) -> str:  # pragma: no cover - display only
        return (f"{self.pages} page(s), {self.blocks} block(s), {self.links} link(s) in "
                f"{self.seconds:.1f}s ({self.api_calls} API calls"
                + (f", {self.skipped} unchanged" if self.skipped else "")
                + (f", {self.archived} gone" if self.archived else "")
                + (f", {len(self.errors)} error(s)" if self.errors else "") + ")")


def page_title(page: dict) -> str:
    """Pull a title out of a page object.

    Titles live in different places depending on how the page was created — a plain
    page has a `title` property, a database row has a title-typed property under
    whatever name the schema gave it. Checking both is why this is a function.
    """
    props = page.get("properties") or {}
    title_prop = props.get("title")
    if isinstance(title_prop, dict) and title_prop.get("type") == "title":
        text = plain_text(title_prop.get("title"))
        if text:
            return text
    for value in props.values():
        if isinstance(value, dict) and value.get("type") == "title":
            text = plain_text(value.get("title"))
            if text:
                return text
    return "Untitled"


def _cover_url(page: dict) -> str | None:
    """The page cover, when it is one we could set again.

    Notion covers are either an uploaded file or an external URL. Only the URL can be
    restored through the API, so an uploaded cover is remembered as `None` — and
    `set_cover` refuses to run on such a page rather than silently replacing something
    it could not put back.
    """
    cover = page.get("cover")
    if not isinstance(cover, dict):
        return None
    external = cover.get("external")
    if isinstance(external, dict) and external.get("url"):
        return str(external["url"])
    return None


def _parent(page: dict) -> tuple[str | None, str | None]:
    parent = page.get("parent") or {}
    kind = parent.get("type")
    if kind == "page_id":
        return parent.get("page_id", "").replace("-", ""), "page"
    if kind == "data_source_id":
        return parent.get("data_source_id", "").replace("-", ""), "data_source"
    if kind == "database_id":
        return parent.get("database_id", "").replace("-", ""), "database"
    if kind == "block_id":
        return parent.get("block_id", "").replace("-", ""), "block"
    return None, kind


def guess_role(title: str, blocks: list[dict]) -> str:
    """Infer what job a page does, from its shape.

    Cheap and deterministic, and right most of the time because page roles have
    strong structural tells: a hub is mostly links and little prose; a literature note
    opens with a source; a scratchpad is short and unstructured. The model is only
    consulted where this is unsure, which keeps profiling a one-off cost rather than a
    per-page bill.
    """
    texts = [b.get("text", "") for b in blocks]
    n = len(texts)
    words = sum(len(t.split()) for t in texts)
    headings = sum(1 for b in blocks if str(b.get("type", "")).startswith("heading"))
    children = sum(1 for b in blocks if b.get("type") == "child_page")
    link_lines = sum(1 for b in blocks if b.get("raw", {}) and links_in(b.get("raw", {})))
    lowered = f"{title}\n" + "\n".join(texts[:12])
    lowered = lowered.lower()

    if children >= 3 or (n and (children + link_lines) / max(n, 1) > 0.5 and words < 400):
        return "hub"
    if any(k in lowered for k in ("arxiv", "doi:", "paper:", "et al", "abstract:")):
        return "literature_note"
    # Whole words, and the ambiguous ones only in the title.
    #
    # This was a substring scan over title and body, which read "psychology",
    # "biology", "blog" and "log-log axes" as evidence of a project log. That is not
    # a rare shape in a knowledge base -- it mislabelled a good fraction of any
    # science notebook -- and a page role decides whether an edit appends prose or
    # adds a link, so the mistake changes what the agent writes to the page.
    #
    # Tokenising fixes the substrings but not "log", which is a real word inside
    # "log-log" and "log scale". Somebody who keeps a project log says so in the
    # title; nobody titles a page "log" and means logarithms.
    def words_in(text: str) -> set[str]:
        return set(re.findall("[a-z]+", text.lower()))

    if words_in(title) & {"log", "journal", "diary", "standup", "changelog"}:
        return "project_log"
    if words_in(lowered) & {"todo", "standup"} or "meeting notes" in lowered:
        return "project_log"
    if words < 120 and headings == 0:
        return "scratchpad"
    if headings >= 2 and words > 300:
        return "deep_dive"
    return "reference"


def _content_hash(texts: list[str]) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(t.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _collect_blocks(client: Workspace, page_id: str, root: str,
                    max_depth: int = 4) -> list[dict]:
    """Flatten a page's block tree, depth-first, recording position and depth.

    Bounded at `max_depth` on purpose: deeply nested toggles are rare in notes and
    unbounded recursion over a pathological page is how a sync becomes a runaway bill.
    Nested content beyond the bound is still visible in its parent's text.
    """
    out: list[dict] = []
    position = 0

    def walk(parent_id: str, depth: int) -> None:
        nonlocal position
        if depth > max_depth:
            return
        for raw in client.block_children(parent_id):
            bid = (raw.get("id") or "").replace("-", "")
            if not bid:
                continue
            text = block_to_text(raw)
            out.append({
                "block_id": bid,
                "page_id": root,
                "parent_id": parent_id.replace("-", ""),
                "type": raw.get("type", "paragraph"),
                "text": text,
                "position": position,
                "depth": depth,
                "has_children": bool(raw.get("has_children")),
                "archived": bool(raw.get("in_trash") or raw.get("archived")),
                "raw": raw,
                "last_edited": raw.get("last_edited_time"),
            })
            position += 1
            if raw.get("has_children") and raw.get("type") != "child_page":
                walk(bid, depth + 1)

    walk(page_id, 0)
    return out


def refresh_pages(client: Workspace, store, page_ids: Iterable[str], *,
                  profile: bool = True) -> int:
    """Pull specific pages into the mirror by id. Returns how many were refreshed.

    `sync` enumerates the workspace through `search`, which is eventually consistent: a
    page created seconds ago is reliably absent from it. That is fine for a periodic
    sync and useless for the case that matters most — the mirror must know about a page
    the moment palimpsest itself creates one, or the next capture about the same topic
    finds nothing to corroborate and creates a second copy.

    So this fetches each page directly. A handful of API calls, no search, no waiting for
    an index to catch up. Errors are collected rather than raised: the write that
    prompted this has already succeeded, and a page that could not be re-read is a stale
    mirror row, which the next sync fixes.
    """
    refreshed = 0
    for raw_id in page_ids:
        pid = (raw_id or "").replace("-", "")
        if not pid:
            continue
        try:
            page = client.get_page(pid)
            blocks = _collect_blocks(client, pid, pid)
        except NotionError as e:
            log.warning("could not refresh %s in the mirror: %s %s", pid[:8], e.code,
                        e.message[:120])
            continue

        texts = [b["text"] for b in blocks if b["text"]]
        parent_id, parent_kind = _parent(page)
        title = page_title(page)
        row: dict[str, Any] = {
            "page_id": pid,
            "parent_id": parent_id,
            "parent_kind": parent_kind,
            "title": title,
            "url": page.get("url"),
            "icon": ((page.get("icon") or {}).get("emoji")
                     if isinstance(page.get("icon"), dict) else None),
            "cover": _cover_url(page),
            "archived": bool(page.get("in_trash") or page.get("archived")),
            "created_time": page.get("created_time"),
            "last_edited": page.get("last_edited_time") or "",
            "content_hash": _content_hash(texts),
            "topics": [],
        }
        if profile:
            row["role"] = guess_role(title, blocks)
            row["summary"] = " ".join(texts)[:400] or None

        store.put_pages([row])
        if blocks:
            store.put_blocks(blocks)
        # Retire whatever is no longer on the page. `put_blocks` only ever adds and
        # updates, so without this a deleted block stays a live retrieval candidate.
        store.drop_missing_blocks(pid, {b["block_id"] for b in blocks})
        refreshed += 1
    return refreshed


def sync(client: Workspace, store, *, incremental: bool = True,
         roots: tuple[str, ...] = (), limit: int | None = None,
         profile: bool = True, on_progress: Callable[[int, int, str], None] | None = None,
         ) -> MirrorResult:
    """Pull Notion into the store.

    `roots`, when given, restricts the mirror to those page ids and their descendants —
    useful when your integration is shared with a large workspace but you only want
    palimpsest looking at part of it.

    `incremental=True` skips any page whose `last_edited_time` matches what we already
    hold. On a workspace that has not changed, this costs one search call and nothing
    else.
    """
    started = time.perf_counter()
    result = MirrorResult()
    known = store.page_last_synced() if incremental else {}
    seen: set[str] = set()

    pages = list(client.search_pages())
    if roots:
        wanted = {r.replace("-", "") for r in roots}
        visible = len(pages)
        pages = [p for p in pages
                 if (p.get("id") or "").replace("-", "") in wanted
                 or (_parent(p)[0] or "") in wanted]
        if visible and not pages:
            # "0 page(s)" on its own is the least helpful thing this can say, and it is
            # the single most common way a Notion setup goes wrong: the integration is
            # shared with a page, the root id names a different one, and everything the
            # mirror can see is filtered away. It is also what happens to anyone who
            # moves to a vault and leaves PALIMPSEST_NOTION_ROOTS behind, since a vault
            # page id can never match a Notion one.
            result.errors.append(
                f"PALIMPSEST_NOTION_ROOTS names {len(wanted)} page(s), and none of them "
                f"is among the {visible} this integration can see — so everything was "
                f"filtered out. Check the root ids, or unset it to mirror everything "
                f"that has been shared.")
    if limit:
        pages = pages[:limit]

    total = len(pages)
    for index, page in enumerate(pages, start=1):
        pid = (page.get("id") or "").replace("-", "")
        if not pid:
            continue
        seen.add(pid)
        title = page_title(page)
        if on_progress:
            on_progress(index, total, title)

        last_edited = page.get("last_edited_time") or ""
        if incremental and known.get(pid) == last_edited and last_edited:
            result.skipped += 1
            continue

        parent_id, parent_kind = _parent(page)
        try:
            blocks = _collect_blocks(client, pid, pid)
        except NotionError as e:
            # A 404 here almost always means "shared with the integration at page level
            # but a child is not" — worth surfacing, not worth aborting the whole sync.
            result.errors.append(f"{title} ({pid[:8]}): {e.code} {e.message[:120]}")
            continue

        texts = [b["text"] for b in blocks if b["text"]]
        row: dict[str, Any] = {
            "page_id": pid,
            "parent_id": parent_id,
            "parent_kind": parent_kind,
            "title": title,
            "url": page.get("url"),
            "icon": ((page.get("icon") or {}).get("emoji")
                     if isinstance(page.get("icon"), dict) else None),
            "cover": _cover_url(page),
            "archived": bool(page.get("in_trash") or page.get("archived")),
            "created_time": page.get("created_time"),
            "last_edited": last_edited,
            "content_hash": _content_hash(texts),
            "topics": [],
        }
        if profile:
            row["role"] = guess_role(title, blocks)
            row["summary"] = " ".join(texts)[:400] or None

        store.put_pages([row])
        if blocks:
            store.put_blocks(blocks)
        # Retire whatever is no longer on the page. `put_blocks` only ever adds and
        # updates, so without this a deleted block stays a live retrieval candidate.
        store.drop_missing_blocks(pid, {b["block_id"] for b in blocks})
        result.pages += 1
        result.blocks += len(blocks)

        link_rows: list[tuple[str, str, str | None]] = []
        for b in blocks:
            for target in links_in(b.get("raw") or {}):
                if target and target != pid:
                    link_rows.append((pid, target, b["block_id"]))
        if link_rows:
            store.put_links(link_rows)
            result.links += len(link_rows)

    if not incremental and not limit:
        # Only a complete walk can tell what has genuinely disappeared — and when the
        # mirror is restricted to roots, "complete" means complete *within those roots*.
        # Skipping the sweep entirely whenever roots were given, which is what this did
        # before, left every deleted page in the mirror for ever. That is not a cosmetic
        # staleness: a deleted page stays a retrieval candidate, so a later claim can be
        # corroborated against a block that no longer exists and the apply fails with a
        # 404 nobody can explain. Roots are the default configuration, so this was the
        # common case rather than the edge one.
        result.archived = store.drop_missing(seen, within=roots or None)

    result.api_calls = client.calls
    result.seconds = time.perf_counter() - started
    store.put_record("mirror_sync", result.as_dict(),
                     label="incremental" if incremental else "full")
    return result


def page_context(store, page_id: str, max_blocks: int = 60) -> dict[str, Any]:
    """Everything the classifier needs to reason about one page.

    Deliberately capped: a classifier prompt that grows with the page turns a large
    note into an expensive one, and the first sixty blocks carry the shape.
    """
    page = store.get_page(page_id)
    if page is None:
        return {}
    blocks = store.get_blocks(page_id)[:max_blocks]
    return {
        "page_id": page_id,
        "title": page.get("title", ""),
        "role": page.get("role") or "reference",
        "url": page.get("url"),
        "blocks": [{"block_id": b["block_id"], "type": b["type"], "text": b["text"]}
                   for b in blocks if b.get("text")],
    }
