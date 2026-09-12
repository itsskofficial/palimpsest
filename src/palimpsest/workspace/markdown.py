"""A knowledge base that is a folder of markdown files.

Palimpsest was written against Notion, and everything above the write door speaks
Notion's block JSON. This module speaks it too — and stores it as `.md` files in a
directory you can open in Obsidian, commit to git, or read with `cat`.

It is a full backend, not a preview. The mirror syncs from it, the planner plans against
it, the applier writes through it, and undo reverses it, all through the same seventeen
methods `NotionClient` exposes. Three things follow, each worth more than the feature:

**You can try the product without giving anything write access to your real notes.**
`palimpsest demo` is this backend pointed at a sample vault. No token, no account, no
risk — the difference between a project people read about and a project people run.

**The whole pipeline becomes testable offline.** Ingest, classify, plan, apply and undo
run end to end in CI with no network and no key. Before this, the only honest test of
the write path was against a live workspace.

**Your notes are not hostage to one vendor.** A knowledge base you feed for years should
outlive the app that maintains it, and a directory of markdown is the most durable format
we have.

Fidelity has one deliberate limit. Notion assigns block ids; markdown has none, so ids
live in a sidecar under `.palimpsest/` and are re-aligned against the file every time it
is read. Edit a page in Obsidian and the ids survive — that alignment is what makes
provenance and exact undo hold across an edit this tool did not make.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from palimpsest.notion.client import NotionError
from palimpsest.workspace.convert import md_to_rich, rich_to_md, to_blocks, to_markdown

__all__ = ["MarkdownWorkspace"]

log = logging.getLogger("palimpsest.workspace")

_SLUG = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return prefix + os.urandom(12).hex()


def _slug(title: str) -> str:
    base = _SLUG.sub("-", (title or "untitled").lower()).strip("-")
    return base[:60] or "untitled"


# ---------------------------------------------------------------------------
# the file format
# ---------------------------------------------------------------------------


def _read_front_matter(text: str) -> tuple[dict, str]:
    """Split a `---` YAML-ish header off the top of a file.

    A deliberately tiny parser rather than a YAML dependency: the core of this package
    has no third-party imports and an import-linter contract keeps it that way. The
    header only holds flat strings this module wrote, so the subset is enough — and a
    header written by hand that it cannot read degrades to "no header", which means the
    file gets adopted with a fresh id rather than crashing a sync.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta: dict[str, Any] = {}
    for line in head.split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"')
        if value:
            meta[key.strip()] = value
    return meta, body.lstrip("\n")


def _write_front_matter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value in (None, ""):
            continue
        lines.append(f'{key}: "{value}"')
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _flatten(blocks: list[dict], depth: int = 0) -> list[tuple[int, dict]]:
    """Depth-first (depth, block) pairs, in reading order."""
    out: list[tuple[int, dict]] = []
    for block in blocks:
        out.append((depth, block))
        body = block.get(block.get("type", ""), {})
        if isinstance(body, dict):
            out.extend(_flatten(body.get("children") or [], depth + 1))
    return out


def _text_of(block: dict) -> str:
    body = block.get(block.get("type", ""), {})
    return rich_to_md(body.get("rich_text")) if isinstance(body, dict) else ""


def _children_of(block: dict) -> list[dict]:
    body = block.get(block.get("type", ""), {})
    return body.get("children") or [] if isinstance(body, dict) else []


def _search(blocks: list[dict], block_id: str) -> tuple[dict, list[dict]] | None:
    """Find a block anywhere in the tree, together with the list it sits in."""
    for block in blocks:
        if block.get("id") == block_id:
            return block, blocks
        deeper = _search(_children_of(block), block_id)
        if deeper is not None:
            return deeper
    return None


def _page_object(page_id: str, meta: dict, path: Path) -> dict:
    """A page in the shape Notion returns one, which is what the mirror reads."""
    icon, cover, parent_id = meta.get("icon"), meta.get("cover"), meta.get("parent")
    return {
        "object": "page",
        "id": page_id,
        "created_time": meta.get("created") or _now(),
        "last_edited_time": meta.get("last_edited") or _now(),
        "in_trash": False,
        "archived": False,
        "icon": {"type": "emoji", "emoji": icon} if icon else None,
        "cover": {"type": "external", "external": {"url": cover}} if cover else None,
        "parent": ({"type": "page_id", "page_id": parent_id} if parent_id
                   else {"type": "workspace", "workspace": True}),
        "properties": {"title": {"id": "title", "type": "title",
                                 "title": md_to_rich(meta.get("title", "Untitled"))}},
        "url": path.resolve().as_uri(),
        "public_url": None,
    }


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------


class MarkdownWorkspace:
    """A vault of markdown files, behind the interface `NotionClient` exposes.

    Every method matches the signature and return shape of its counterpart on
    `NotionClient`, because the code calling them is the same code — `mirror.sync`,
    `apply._execute`, `journal`. Where Notion returns a partial object, so does this;
    where Notion raises `NotionError`, so does this, with the same status codes, so the
    applier's error handling is exercised identically against both backends.

    The file on disk is the source of truth and is re-read on every call. That is slower
    than holding a tree in memory and it is the right trade: you can edit the vault in
    Obsidian while this is running, and the next operation sees your edit.
    """

    #: Matches `NotionClient`, whose value the mirror reports as API cost.
    calls: int

    def __init__(self, root: str | Path, *, name: str = "vault"):
        self.root = Path(root).expanduser().resolve()
        self.name = name
        self.calls = 0
        # Every write is a read-modify-write over a whole file, so two threads appending
        # to one page would lose one of the appends. The queue worker and the web
        # handlers share a workspace, so this is not hypothetical.
        self._lock = threading.RLock()
        self._meta = self.root / ".palimpsest"
        for directory in (self.root, self._meta, self._meta / "ids", self._meta / "trash"):
            directory.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<MarkdownWorkspace {self.root}>"

    # -- locating pages ------------------------------------------------------

    def _pages(self) -> Iterator[Path]:
        for path in sorted(self.root.rglob("*.md")):
            if ".palimpsest" not in path.parts:
                yield path

    def _index(self) -> dict[str, str]:
        path = self._meta / "pages.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):  # pragma: no cover - a corrupt index self-heals
            return {}

    def _remember(self, page_id: str, path: Path) -> None:
        index = self._index()
        relative = str(path.relative_to(self.root)).replace("\\", "/")
        if index.get(page_id) == relative:
            return
        index[page_id] = relative
        (self._meta / "pages.json").write_text(json.dumps(index, indent=2),
                                               encoding="utf-8")

    def _path_for(self, page_id: str) -> Path:
        index = self._index()
        if page_id in index:
            candidate = self.root / index[page_id]
            if candidate.exists():
                return candidate
        # The index goes stale when the vault changes outside this process — a file
        # renamed in Obsidian, pages pulled in by git. Scan and repair, rather than
        # reporting a page that plainly exists as missing.
        for path in self._pages():
            meta, _ = _read_front_matter(path.read_text(encoding="utf-8"))
            if meta.get("id") == page_id:
                self._remember(page_id, path)
                return path
        raise NotionError(404, "object_not_found", f"no page {page_id} in {self.root}")

    # -- block identity ------------------------------------------------------

    def _align(self, page_id: str, blocks: list[dict]) -> None:
        """Give every block a stable id, stamped in place.

        The one genuinely hard part of storing Notion blocks as markdown. Notion hands
        out ids; a file has none, so they live in a sidecar and must be matched back onto
        the file's contents every time it is read — including after somebody edited it in
        Obsidian, which is the case that matters.

        Two passes, narrowest first. Identical text keeps its id wherever it moved to, so
        reordering a page preserves provenance. Then same-type blocks match by nearest
        position, so *editing* a paragraph keeps its id rather than orphaning the
        footnote that points at it. Only genuinely new content gets a new id.
        """
        sidecar = self._meta / "ids" / f"{page_id}.json"
        saved: list[dict] = []
        if sidecar.exists():
            try:
                saved = json.loads(sidecar.read_text(encoding="utf-8"))
            except (ValueError, OSError):  # pragma: no cover - self-heals
                saved = []

        flat = _flatten(blocks)
        current = [{"type": b.get("type", ""), "text": _text_of(b)} for _, b in flat]
        assigned: list[str | None] = [None] * len(current)
        taken: set[int] = set()

        def claim(match: Callable[[dict, dict], bool]) -> None:
            for i, entry in enumerate(current):
                if assigned[i] is not None:
                    continue
                best: int | None = None
                distance: int | None = None
                for j, old in enumerate(saved):
                    if j in taken or not match(entry, old):
                        continue
                    gap = abs(j - i)
                    if distance is None or gap < distance:
                        best, distance = j, gap
                if best is not None:
                    assigned[i] = str(saved[best]["id"])
                    taken.add(best)

        claim(lambda new, old: new["type"] == old.get("type")
              and new["text"] == old.get("text"))
        claim(lambda new, old: new["type"] == old.get("type"))

        for i, (_, block) in enumerate(flat):
            block["id"] = assigned[i] or _new_id("mb_")
            block["object"] = "block"
            block.setdefault("last_edited_time", _now())
            block["has_children"] = bool(_children_of(block))
            block["in_trash"] = False
            block["archived"] = False

        sidecar.write_text(json.dumps(
            [{"id": b["id"], "type": c["type"], "text": c["text"]}
             for (_, b), c in zip(flat, current, strict=True)], indent=2), encoding="utf-8")

    # -- file I/O ------------------------------------------------------------

    def _load(self, path: Path) -> tuple[str, dict, list[dict]]:
        """A page file as (id, metadata, blocks-with-ids)."""
        meta, body = _read_front_matter(path.read_text(encoding="utf-8"))
        page_id = meta.get("id") or ""
        if not page_id:
            # A file dropped into the vault by hand. Adopt it: mint an id, keep the name.
            page_id = _new_id("mp_")
            meta["id"] = page_id
            meta.setdefault("title", path.stem.replace("-", " "))
            meta.setdefault("created", _now())
            path.write_text(_write_front_matter(meta) + body, encoding="utf-8")
        meta.setdefault("title", path.stem.replace("-", " "))
        blocks = to_blocks(body)
        self._align(page_id, blocks)
        self._remember(page_id, path)
        return page_id, meta, blocks

    def _save(self, path: Path, meta: dict, blocks: list[dict]) -> None:
        meta = dict(meta)
        meta["last_edited"] = _now()
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = to_markdown(blocks)
        path.write_text(_write_front_matter(meta) + rendered + "\n", encoding="utf-8")
        self._align(str(meta["id"]), blocks)
        self._remember(str(meta["id"]), path)

    def _load_page(self, page_id: str) -> tuple[Path, dict, list[dict]]:
        path = self._path_for(page_id)
        _, meta, blocks = self._load(path)
        return path, meta, blocks

    def _locate(self, block_id: str) -> tuple[Path, dict, list[dict], dict, list[dict]]:
        """A block, its file, the page's blocks, and the sibling list it lives in."""
        for path in self._pages():
            _, meta, blocks = self._load(path)
            found = _search(blocks, block_id)
            if found is not None:
                block, siblings = found
                return path, meta, blocks, block, siblings
        raise NotionError(404, "object_not_found", f"no block {block_id}")

    # -- reading -------------------------------------------------------------

    def whoami(self) -> dict:
        self.calls += 1
        return {"object": "user", "type": "bot", "id": "local",
                "name": f"palimpsest ({self.name})",
                "bot": {"workspace_name": str(self.root)}}

    def search_pages(self, query: str = "") -> Iterator[dict]:
        self.calls += 1
        needle = (query or "").lower()
        for path in self._pages():
            page_id, meta, _ = self._load(path)
            if needle and needle not in str(meta.get("title", "")).lower():
                continue
            yield _page_object(page_id, meta, path)

    def search_data_sources(self, query: str = "") -> Iterator[dict]:
        self.calls += 1
        return iter(())

    def get_page(self, page_id: str) -> dict:
        self.calls += 1
        path, meta, _ = self._load_page(page_id)
        return _page_object(page_id, meta, path)

    def get_block(self, block_id: str) -> dict:
        self.calls += 1
        return self._locate(block_id)[3]

    def block_children(self, block_id: str) -> Iterator[dict]:
        """Children of a page (its top-level blocks) or of a block (its nested ones)."""
        self.calls += 1
        try:
            _, _, blocks = self._load_page(block_id)
        except NotionError:
            return iter(list(_children_of(self._locate(block_id)[3])))
        return iter(list(blocks))

    # -- writing -------------------------------------------------------------

    def append_children(self, parent_id: str, children: list[dict],
                        after_block_id: str | None = None) -> dict:
        """Add blocks to a page or block, optionally after a given sibling.

        Returns every block it created, nested descendants included — which is what the
        real API does, and the reason `undo` has to tolerate archiving a child that its
        parent already took with it.
        """
        with self._lock:
            created: list[dict] = []
            try:
                path, meta, blocks = self._load_page(parent_id)
                siblings = blocks
            except NotionError:
                path, meta, blocks, parent, _ = self._locate(parent_id)
                body = parent.setdefault(parent.get("type", "paragraph"), {})
                siblings = body.setdefault("children", [])

            fresh = [_strip_ids(child) for child in children]
            at = len(siblings)
            if after_block_id:
                for index, sibling in enumerate(siblings):
                    if sibling.get("id") == after_block_id:
                        at = index + 1
                        break
            siblings[at:at] = fresh
            self._save(path, meta, blocks)

            for _, block in _flatten(fresh):
                created.append({"object": "block", "id": block["id"],
                                "type": block.get("type", "paragraph")})
            return {"object": "list", "results": created}

    def update_block(self, block_id: str, payload: dict) -> dict:
        """Replace a block's body, or archive it when the payload says so."""
        with self._lock:
            if payload.get("archived") or payload.get("in_trash"):
                return self.archive_block(block_id)
            if payload.get("archived") is False or payload.get("in_trash") is False:
                return self.restore_block(block_id)
            path, meta, blocks, block, _ = self._locate(block_id)
            kind = next((k for k in payload if k not in ("archived", "in_trash")), None)
            if kind is None:
                return block
            body = dict(payload[kind] or {})
            # A payload that renames the type takes the old children with it: the applier
            # only ever rewrites text, and dropping them would delete content silently.
            body.setdefault("children", _children_of(block))
            if not body["children"]:
                body.pop("children")
            block.clear()
            block.update({"type": kind, kind: body, "id": block_id})
            self._save(path, meta, blocks)
            return self._locate(block_id)[3]

    def archive_block(self, block_id: str) -> dict:
        """Move a block (and its subtree) to the vault's trash, reversibly."""
        with self._lock:
            # The trash is checked *before* the lookup, and the order is the whole point.
            # A block that went to the trash with its parent is no longer in the file, so
            # locating it first reports a 404 -- and `_archive_created` forgives only the
            # word "archived". Undo would then stop halfway on a vault for exactly the
            # reason it used to stop halfway on Notion.
            if _trash_path(self._meta, block_id).exists():
                raise NotionError(400, "validation_error",
                                  "Can't edit block that is archived. "
                                  "You must unarchive the block before editing.")
            path, meta, blocks, block, siblings = self._locate(block_id)
            page_id = str(meta["id"])
            for _, descendant in _flatten([block]):
                _trash_path(self._meta, str(descendant["id"])).write_text(
                    json.dumps({"page_id": page_id, "block": descendant}, indent=2),
                    encoding="utf-8")
            siblings.remove(block)
            self._save(path, meta, blocks)
            return {"object": "block", "id": block_id, "in_trash": True,
                    "archived": True}

    def restore_block(self, block_id: str) -> dict:
        """Put a trashed block back — at the end of its page, exactly like Notion.

        Not a quirk being emulated for its own sake. `apply` rebuilds a rewritten section
        from its snapshot precisely *because* Notion restores to the end and would
        otherwise scramble reading order, and a backend that restored in place would let
        that bug back in unnoticed the next time someone touched the inverse logic.
        """
        with self._lock:
            trashed = _trash_path(self._meta, block_id)
            if not trashed.exists():
                raise NotionError(404, "object_not_found", f"no trashed block {block_id}")
            record = json.loads(trashed.read_text(encoding="utf-8"))
            path, meta, blocks = self._load_page(record["page_id"])
            blocks.append(record["block"])
            self._save(path, meta, blocks)
            for _, descendant in _flatten([record["block"]]):
                _trash_path(self._meta, str(descendant["id"])).unlink(missing_ok=True)
            return {"object": "block", "id": block_id, "in_trash": False}

    def create_page(self, parent_page_id: str, title: str,
                    children: list[dict] | None = None,
                    icon: str | None = None) -> dict:
        with self._lock:
            page_id = _new_id("mp_")
            name = _slug(title)
            path = self.root / f"{name}.md"
            suffix = 2
            while path.exists():
                path = self.root / f"{name}-{suffix}.md"
                suffix += 1
            meta = {"id": page_id, "title": title, "parent": parent_page_id or "",
                    "icon": icon or "", "created": _now()}
            self._save(path, meta, [_strip_ids(c) for c in (children or [])])
            return {"object": "page", "id": page_id, "url": path.resolve().as_uri(),
                    "properties": {"title": {"type": "title",
                                             "title": md_to_rich(title)}}}

    def archive_page(self, page_id: str, *, restore: bool = False) -> dict:
        with self._lock:
            if restore:
                source = self._meta / "trash" / f"{page_id}.md"
                if not source.exists():
                    raise NotionError(404, "object_not_found", f"no page {page_id}")
                meta, _ = _read_front_matter(source.read_text(encoding="utf-8"))
                target = self.root / f"{_slug(str(meta.get('title', page_id)))}.md"
                shutil.move(str(source), str(target))
                self._remember(page_id, target)
                return {"object": "page", "id": page_id, "in_trash": False}
            path = self._path_for(page_id)
            shutil.move(str(path), str(self._meta / "trash" / f"{page_id}.md"))
            index = self._index()
            index.pop(page_id, None)
            (self._meta / "pages.json").write_text(json.dumps(index, indent=2),
                                                   encoding="utf-8")
            return {"object": "page", "id": page_id, "in_trash": True, "archived": True}

    def update_page(self, page_id: str, payload: dict) -> dict:
        with self._lock:
            path, meta, blocks = self._load_page(page_id)
            if "icon" in payload:
                icon = payload["icon"]
                meta["icon"] = (icon or {}).get("emoji", "") if icon else ""
            if "cover" in payload:
                cover = payload["cover"]
                meta["cover"] = ((cover or {}).get("external") or {}).get("url", "")
            properties = payload.get("properties") or {}
            if "title" in properties:
                meta["title"] = rich_to_md(properties["title"].get("title"))
            if payload.get("in_trash") or payload.get("archived"):
                return self.archive_page(page_id)
            self._save(path, meta, blocks)
            return _page_object(page_id, meta, path)

    def rename_page(self, page_id: str, title: str) -> dict:
        return self.update_page(page_id, {"properties": {
            "title": {"title": md_to_rich(title)}}})

    def set_page_icon(self, page_id: str, icon: str | None) -> dict:
        return self.update_page(
            page_id, {"icon": {"type": "emoji", "emoji": icon} if icon else None})

    def set_page_cover(self, page_id: str, url: str | None) -> dict:
        return self.update_page(
            page_id,
            {"cover": {"type": "external", "external": {"url": url}} if url else None})

    def move_page(self, page_id: str, parent_page_id: str) -> dict:
        with self._lock:
            path, meta, blocks = self._load_page(page_id)
            meta["parent"] = parent_page_id
            self._save(path, meta, blocks)
            return _page_object(page_id, meta, path)

    # -- the journal ---------------------------------------------------------
    #
    # Notion's journal is a database of rows. A vault has no databases, so it is a page
    # with a markdown table — which is what a database view is for a reader anyway, and
    # it renders correctly in Obsidian and on GitHub.

    def create_database(self, parent_page_id: str, title: str, properties: dict,
                        icon: str | None = None,
                        description: str | None = None) -> dict:
        with self._lock:
            columns = list((properties or {}).keys()) or ["Name"]
            header = "| " + " | ".join(columns) + " |"
            rule = "| " + " | ".join("---" for _ in columns) + " |"
            intro = to_blocks(description) if description else []
            page = self.create_page(parent_page_id, title, icon=icon,
                                    children=intro + to_blocks(f"{header}\n{rule}"))
            database_id = page["id"]
            (self._meta / f"db-{database_id}.json").write_text(
                json.dumps({"columns": columns, "title": title}), encoding="utf-8")
            return {"object": "database", "id": database_id, "title": title,
                    "url": page["url"],
                    "data_sources": [{"id": database_id, "name": title}]}

    def get_database(self, database_id: str) -> dict:
        self.calls += 1
        page = self.get_page(database_id)
        return {"object": "database", "id": database_id, "url": page["url"],
                "data_sources": [{"id": database_id}]}

    def data_source_id(self, database: dict) -> str | None:
        sources = database.get("data_sources") or []
        return str(sources[0]["id"]) if sources else None

    def create_row(self, data_source_id: str, properties: dict,
                   children: list[dict] | None = None) -> dict:
        """Append one row to the table page a `create_database` call made."""
        with self._lock:
            spec = self._meta / f"db-{data_source_id}.json"
            columns = (json.loads(spec.read_text(encoding="utf-8"))["columns"]
                       if spec.exists() else list(properties))
            cells = [_property_text(properties.get(column)) for column in columns]
            row = "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"
            path, meta, blocks = self._load_page(data_source_id)
            blocks.append({"type": "paragraph",
                           "paragraph": {"rich_text": md_to_rich(row)}})
            self._save(path, meta, blocks)
            return {"object": "page", "id": _new_id("mr_"), "url": path.as_uri()}

    def query_data_source(self, data_source_id: str, filter_: dict | None = None,
                          page_size: int = 100) -> Iterator[dict]:
        self.calls += 1
        return iter(())


def _trash_path(meta_dir: Path, block_id: str) -> Path:
    return meta_dir / "trash" / f"{block_id}.json"


def _strip_ids(block: dict) -> dict:
    """A fresh copy with no ids, so `_align` mints new ones for genuinely new content."""
    out = {k: v for k, v in block.items()
           if k not in ("id", "object", "has_children", "in_trash", "archived",
                        "last_edited_time", "created_time", "parent")}
    kind = out.get("type", "")
    body = out.get(kind)
    if isinstance(body, dict) and body.get("children"):
        out[kind] = {**body, "children": [_strip_ids(c) for c in body["children"]]}
    return out


def _property_text(value: Any) -> str:
    """Flatten one Notion property value to a table cell."""
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    for key in ("title", "rich_text"):
        if key in value:
            return rich_to_md(value[key])
    if "select" in value:
        return str((value["select"] or {}).get("name", ""))
    if "date" in value:
        return str((value["date"] or {}).get("start", ""))
    if "url" in value:
        return str(value["url"] or "")
    if "number" in value:
        return "" if value["number"] is None else str(value["number"])
    if "checkbox" in value:
        return "yes" if value["checkbox"] else "no"
    return ""
