"""The change log, as a Notion database rather than a table you cannot see.

palimpsest already records every applied operation, its inverse and its provenance — in
SQLite, where none of it is visible from the tool you actually read your notes in. That
is fine for `undo`, which is a command, and useless for the question people actually
ask, which is *why does this sentence say that, and who decided* — asked six months
later, in Notion, on a phone.

So the ledger is mirrored into two Notion databases under your root page:

**`palimpsest · Changes`** — one row per applied operation. What changed, on which page,
which of the seven relations produced it, **the classifier's reasoning in a `Why`
column**, how confident it was, which source and which anchor, who approved it, and the
patch id that reverts it. It is a real database, so you can filter it to every
`supersedes` you ever accepted, sort by confidence to find the close calls, or read one
page's history without leaving Notion.

**`palimpsest · Sources`** — one row per thing you fed it, with how many claims came out
and how many edits landed. This is the answer to "where did all this come from".

Two properties of the implementation matter:

1. **A journal failure never fails a patch.** The row is written after the operation
   succeeded, inside a `try` that swallows everything. Losing a log line is a nuisance;
   refusing to edit your notes because the log line failed would be absurd.
2. **The databases are found, not re-created.** Their ids are cached in the local store,
   so restarting does not scatter a fresh pair of databases into your workspace.

Like `apply.py`, this module writes to Notion, and the same import-linter contract keeps
every planner out of it. Two doors, both in `notion/`, both reachable only from the
applier.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from palimpsest.notion.client import NotionClient, NotionError
from palimpsest.types import Operation, OpKind

__all__ = ["CHANGES", "SOURCES", "Journal"]

log = logging.getLogger("palimpsest.journal")

CHANGES = "palimpsest · Changes"
SOURCES = "palimpsest · Sources"

#: The store record kind under which the database ids are cached.
_RECORD = "journal_databases"


def _opt(name: str, color: str = "default") -> dict:
    return {"name": name, "color": color}


#: Colour carries the risk tier, so the database reads at a glance: green is a citation
#: that added no prose, orange and red are text that was replaced.
_RELATION_OPTIONS = [
    _opt("new", "blue"), _opt("corroborates", "green"), _opt("refines", "yellow"),
    _opt("supersedes", "orange"), _opt("duplicate", "purple"), _opt("extends", "brown"),
    _opt("contradicts", "red"),
]

_OPERATION_OPTIONS = [
    _opt("append_block", "blue"), _opt("update_text", "yellow"),
    _opt("add_citation", "green"), _opt("insert_footnote", "gray"),
    _opt("strike_block", "orange"), _opt("archive_block", "red"),
    _opt("create_page", "blue"), _opt("link_pages", "purple"),
    _opt("move_page", "brown"), _opt("rename_page", "brown"), _opt("set_icon", "gray"),
]

CHANGES_SCHEMA: dict[str, Any] = {
    "Change": {"title": {}},
    "When": {"date": {}},
    "Page": {"rich_text": {}},
    "Operation": {"select": {"options": _OPERATION_OPTIONS}},
    "Relation": {"select": {"options": _RELATION_OPTIONS}},
    # The column this whole module exists for.
    "Why": {"rich_text": {}},
    "Confidence": {"number": {"format": "percent"}},
    "Source": {"url": {}},
    "Cites": {"rich_text": {}},
    "Approved by": {"rich_text": {}},
    "Patch": {"rich_text": {}},
    "Status": {"select": {"options": [_opt("Applied", "green"),
                                      _opt("Reverted", "gray")]}},
}

SOURCES_SCHEMA: dict[str, Any] = {
    "Source": {"title": {}},
    "Kind": {"select": {"options": [
        _opt("web", "blue"), _opt("youtube", "red"), _opt("transcript", "orange"),
        _opt("audio", "purple"), _opt("pdf", "brown"), _opt("image", "pink"),
        _opt("tabular", "green"), _opt("text", "gray"),
    ]}},
    "URL": {"url": {}},
    "Ingested": {"date": {}},
    "Claims": {"number": {"format": "number"}},
    "Changes": {"number": {"format": "number"}},
    "Archive": {"rich_text": {}},
}


def _text(value: str | None, limit: int = 1900) -> list[dict]:
    if not value:
        return []
    return [{"type": "text", "text": {"content": str(value)[:limit]}}]


def _select(value: str | None) -> dict | None:
    return {"name": str(value)[:100]} if value else None


def _iso(when: float | None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when or time.time()))


def _headline(op: Operation) -> str:
    """A row title someone can scan down."""
    verb = {
        OpKind.APPEND_BLOCK: "Added", OpKind.UPDATE_TEXT: "Rewrote",
        OpKind.ADD_CITATION: "Cited", OpKind.INSERT_FOOTNOTE: "Footnoted",
        OpKind.STRIKE_BLOCK: "Struck", OpKind.ARCHIVE_BLOCK: "Archived",
        OpKind.CREATE_PAGE: "Created page", OpKind.LINK_PAGES: "Linked",
        OpKind.MOVE_PAGE: "Filed", OpKind.RENAME_PAGE: "Renamed",
        OpKind.SET_ICON: "Set icon on",
    }.get(op.kind, op.kind.value)
    payload = op.payload or {}
    detail = payload.get("text") or payload.get("title") or ""
    return f"{verb}: {detail}".strip(": ") if detail else verb


def _why(op: Operation, payload: dict) -> str:
    """The reasoning, assembled from whatever the planner recorded.

    A relation is already an explanation of a kind — `corroborates` means "you had
    written this, so nothing was added but a citation" — so a row is never left with an
    empty `Why` even when the classifier offered no prose of its own.
    """
    parts: list[str] = []
    rationale = payload.get("rationale") or payload.get("note")
    if rationale:
        parts.append(str(rationale))
    elif op.relation is not None:
        parts.append(op.relation.describe())
    if op.kind is OpKind.MOVE_PAGE and payload.get("hub"):
        parts.append(f"filed under {payload['hub']}")
    if payload.get("was"):
        parts.append(f"previously “{payload['was']}”")
    return " · ".join(parts)


class Journal:
    """Writes the ledger into Notion. Built once per process and reused."""

    def __init__(self, client: NotionClient, store, root_page_id: str | None,
                 enabled: bool = True):
        self.client = client
        self.store = store
        self.root_page_id = root_page_id
        # Without a root page there is nowhere to put the databases, and guessing a
        # location for something this visible is not a decision to make silently.
        self.enabled = enabled and bool(root_page_id)
        self._ids: dict[str, str] | None = None

    # -- finding or making the databases ---------------------------------------

    def data_sources(self) -> dict[str, str]:
        """`{"changes": <data source id>, "sources": <data source id>}`.

        Cached in the local store, because creating these is the one operation here
        that must not happen twice — a second pair of databases beside the first is
        confusing and cannot be merged afterwards.
        """
        if self._ids is not None:
            return self._ids
        if not self.enabled:
            self._ids = {}
            return self._ids

        cached = next(iter(self.store.get_records(kind=_RECORD, limit=1)), None)
        if cached:
            payload = cached.get("payload") or {}
            if payload.get("root") == self.root_page_id and payload.get("changes"):
                self._ids = {"changes": payload["changes"],
                             "sources": payload.get("sources", "")}
                return self._ids

        self._ids = self._create()
        if self._ids:
            self.store.put_record(_RECORD, {**self._ids, "root": self.root_page_id},
                                  label="notion journal")
        return self._ids

    def _create(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, title, schema, icon, description in (
            ("changes", CHANGES, CHANGES_SCHEMA, "\U0001fab6",
             "Every edit palimpsest applied, why it applied it, and what reverts it."),
            ("sources", SOURCES, SOURCES_SCHEMA, "\U0001f4e5",
             "Everything fed to palimpsest, and how much of it landed."),
        ):
            try:
                database = self.client.create_database(
                    self.root_page_id or "", title, schema, icon=icon,
                    description=description)
                source_id = self.client.data_source_id(database)
                if source_id:
                    out[key] = source_id
                    log.info("created %s", title)
            except (NotionError, ValueError) as e:
                log.warning("could not create %s: %s", title, e)
        return out

    # -- writing ---------------------------------------------------------------

    def record_change(self, op: Operation, *, patch_id: str, page: dict | None,
                      source: dict | None, reviewer: str | None,
                      status: str = "Applied") -> None:
        """One row for one applied operation. Never raises."""
        if not self.enabled:
            return
        try:
            ids = self.data_sources()
            if not ids.get("changes"):
                return

            payload = op.payload or {}
            anchor = payload.get("anchor") or {}
            page_title = (page or {}).get("title") or op.target
            page_url = (page or {}).get("url")

            properties: dict[str, Any] = {
                "Change": {"title": _text(_headline(op), 190)},
                "When": {"date": {"start": _iso(op.applied_at)}},
                "Page": {"rich_text": _text(page_title, 200)},
                "Operation": {"select": _select(op.kind.value)},
                "Relation": {"select": _select(op.relation.value if op.relation else None)},
                "Why": {"rich_text": _text(_why(op, payload))},
                "Source": {"url": (source or {}).get("url") or payload.get("url") or None},
                "Cites": {"rich_text": _text(anchor.get("locator"), 120)},
                "Approved by": {"rich_text": _text(reviewer or "auto", 80)},
                "Patch": {"rich_text": _text(patch_id, 80)},
                "Status": {"select": _select(status)},
            }
            confidence = payload.get("confidence")
            if isinstance(confidence, int | float):
                properties["Confidence"] = {"number": round(float(confidence), 3)}

            # The row body carries the text that was actually written and a link back to
            # the page, so a row stands on its own without cross-referencing anything.
            children: list[dict] = []
            body = payload.get("text") or payload.get("title")
            if body:
                children.append({"object": "block", "type": "quote",
                                 "quote": {"rich_text": _text(body, 1900)}})
            if page_url:
                children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{
                        "type": "text",
                        "text": {"content": "open the page", "link": {"url": page_url}},
                    }]},
                })

            self.client.create_row(ids["changes"], properties, children=children or None)
        except Exception as e:  # a log line must never break an edit
            log.warning("journal write failed (the edit itself succeeded): %s", e)

    def record_source(self, source: dict, *, claims: int, changes: int) -> None:
        """One row per ingested source. Never raises."""
        if not self.enabled:
            return
        try:
            ids = self.data_sources()
            if not ids.get("sources"):
                return
            self.client.create_row(ids["sources"], {
                "Source": {"title": _text(source.get("title") or "Untitled", 190)},
                "Kind": {"select": _select(source.get("kind"))},
                "URL": {"url": source.get("url") or None},
                "Ingested": {"date": {"start": _iso(source.get("fetched_at"))}},
                "Claims": {"number": int(claims)},
                "Changes": {"number": int(changes)},
                "Archive": {"rich_text": _text(source.get("archive_key"), 200)},
            })
        except Exception as e:
            log.warning("journal source write failed: %s", e)
