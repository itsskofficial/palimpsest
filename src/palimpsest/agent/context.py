"""What the tools share: one store, the settings, and lazily-built clients.

A tool should not know how to build a Notion client or a retrieval index — it should
ask the context. Centralising that here means the expensive objects (the model, the
index) are built once per turn and reused, and that a tool written later cannot
accidentally open a second SQLite connection or a second Notion client with different
settings.

The factories return *fresh* clients where sharing would be unsafe across threads (the
queue's workers each need their own), and cache where it is safe within one turn (the
retrieval index, rebuilt only when the block count changes).
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

log = logging.getLogger("palimpsest.agent")

__all__ = ["ToolContext", "current_chat"]

#: The chat a turn is being run for, if any. A context variable rather than an attribute
#: on the shared context because turns for different chats may run on different threads,
#: and each thread must see its own value. `capture_source` reads it so a source the
#: agent ingests on a user's behalf reports its result back to that user's chat.
current_chat: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "palimpsest_current_chat", default=None)


class ToolContext:
    """Everything the tools reach through. One per agent process."""

    def __init__(self, settings: Any, *, queue: Any = None):
        from palimpsest.store import open_store

        self.settings = settings
        self.store = open_store(settings.database_url)
        self.queue = queue
        self._model = None
        self._index = None
        self._index_blocks = -1

    # -- lazily built ----------------------------------------------------------

    @property
    def model(self):
        from palimpsest.llm import Model

        if self._model is None:
            self._model = Model(self.settings.model,
                                api_key=self.settings.anthropic_api_key,
                                max_tokens=self.settings.max_tokens)
        return self._model

    @property
    def index(self):
        """The retrieval index, rebuilt only when the mirror's block count changes."""
        from palimpsest.retrieve import Index

        count = self.store.stats().get("blocks", 0)
        if self._index is None or self._index_blocks != count:
            self._index = Index(self.store)
            self._index_blocks = count
        return self._index

    def refresh_index(self) -> None:
        self._index = None

    # -- factories (fresh objects, for thread safety) --------------------------

    def new_notion(self):
        from palimpsest.notion.client import NotionClient

        if not self.settings.has_notion:
            raise RuntimeError("NOTION_TOKEN is not set")
        return NotionClient(self.settings.notion_token or "",
                            version=self.settings.notion_version)

    def new_journal(self):
        from palimpsest.notion.journal import Journal

        roots = self.settings.notion_root_pages
        return Journal(self.new_notion(), self.store, roots[0] if roots else None,
                       enabled=self.settings.journal)

    @property
    def root_page_id(self) -> str | None:
        roots = self.settings.notion_root_pages
        return roots[0] if roots else None

    # -- capture ---------------------------------------------------------------

    def enqueue(self, spec: str, **kw) -> dict:
        """Queue a capture. Uses the shared queue when present, the store otherwise so a
        worker elsewhere drains it — either way the call returns at once.

        The origin is stamped with the current chat when one is set, so an async
        ingestion the agent starts reports its result back to the person who asked for
        it — not to a generic 'agent' bucket nobody is watching.
        """
        chat = current_chat.get()
        kw.setdefault("origin", f"telegram:{chat}" if chat else "agent")
        if self.queue is not None:
            return self.queue.submit(spec, **kw)
        from palimpsest.jobs import submit_spec

        return submit_spec(self.store, spec, **kw)

    def close(self) -> None:
        with __import__("contextlib").suppress(Exception):
            self.store.close()
