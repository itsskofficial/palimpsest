"""What the mirror, the applier and the journal require of a workspace.

This is the interface `NotionClient` happens to have and `MarkdownWorkspace` was written
to match. It lives here, at the bottom, rather than up in `palimpsest.workspace` where
the backends are chosen, because the modules that *consume* it are here: `mirror` reads
through it, `apply` writes through it, `journal` logs through it. A protocol belongs next
to its callers, and the layering contract would forbid the other direction anyway —
`notion` sits below `workspace` and must not import upward.

`palimpsest.workspace` re-exports `Workspace` so callers have one obvious name.

Seventeen methods is the whole surface area of "somewhere a knowledge base can live",
which is a surprisingly small number and is the reason a second backend was a week's work
rather than a fork.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

__all__ = ["METHODS", "Workspace", "check"]

#: Every method the pipeline calls on a workspace, grouped by who calls it.
METHODS: dict[str, tuple[str, ...]] = {
    # mirror.sync — reading the workspace into the local copy
    "read": ("whoami", "search_pages", "get_page", "get_block", "block_children"),
    # notion.apply — the one write door
    "write": ("append_children", "update_block", "archive_block", "restore_block",
              "create_page", "archive_page", "update_page", "rename_page",
              "set_page_icon", "set_page_cover", "move_page"),
    # notion.journal — the activity log written back into the workspace
    "journal": ("create_database", "create_row", "data_source_id"),
}


@runtime_checkable
class Workspace(Protocol):
    """Somewhere a knowledge base can live. See `METHODS` for the grouped list."""

    #: How many backend calls have been made. The mirror reports it as cost.
    calls: int

    # -- reading ---------------------------------------------------------------
    def whoami(self) -> dict: ...
    def search_pages(self, query: str = "") -> Iterator[dict]: ...
    def get_page(self, page_id: str) -> dict: ...
    def get_block(self, block_id: str) -> dict: ...
    def block_children(self, block_id: str) -> Iterator[dict]: ...

    # -- writing ---------------------------------------------------------------
    def append_children(self, parent_id: str, children: list[dict],
                        after_block_id: str | None = None) -> dict: ...
    def update_block(self, block_id: str, payload: dict) -> dict: ...
    def archive_block(self, block_id: str) -> dict: ...
    def restore_block(self, block_id: str) -> dict: ...
    def create_page(self, parent_page_id: str, title: str,
                    children: list[dict] | None = None,
                    icon: str | None = None) -> dict: ...
    def archive_page(self, page_id: str, *, restore: bool = False) -> dict: ...
    def update_page(self, page_id: str, payload: dict) -> dict: ...
    def rename_page(self, page_id: str, title: str) -> dict: ...
    def set_page_icon(self, page_id: str, icon: str | None) -> dict: ...
    def set_page_cover(self, page_id: str, url: str | None) -> dict: ...
    def move_page(self, page_id: str, parent_page_id: str) -> dict: ...

    # -- the journal -----------------------------------------------------------
    def create_database(self, parent_page_id: str, title: str, properties: dict,
                        icon: str | None = None,
                        description: str | None = None) -> dict: ...
    def create_row(self, data_source_id: str, properties: dict,
                   children: list[dict] | None = None) -> dict: ...
    def data_source_id(self, database: dict) -> str | None: ...


def check(backend: Any) -> list[str]:
    """Method names `backend` is missing. Empty means it can stand in for the other.

    Deliberately a name check rather than `isinstance`. `NotionClient` predates this
    protocol and takes extra optional arguments on a couple of methods, so it is a
    structural superset and a strict runtime check would reject it for being *more*
    capable than required.
    """
    return [name for group in METHODS.values() for name in group
            if not callable(getattr(backend, name, None))]
