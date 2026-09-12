"""Where the knowledge base lives — Notion, or a folder of markdown.

One function matters here:

    from palimpsest import workspace
    client = workspace.open(settings)

Everything downstream — the mirror, the planner, the applier, undo, the journal — takes
that object as `client` and never asks what it is. That is possible because the project
has exactly one write door (`notion.apply`) and a seventeen-method surface behind it, so
a second backend is a second implementation of an interface rather than a second copy of
the pipeline.

**Why a second backend exists at all**, given the product is a Notion tool:

- *You can try it without risking your notes.* `palimpsest demo` is the markdown backend
  pointed at a sample vault: no token, no account, nothing of yours is writable.
- *The write path becomes testable offline.* Ingest → classify → plan → apply → undo now
  runs end to end in CI with no network. It used to be that the only honest test of a
  write was against a live workspace, which meant it mostly did not happen.
- *Notes outlive tools.* A knowledge base you feed for years should not be trapped in one
  vendor's database, and markdown is the most durable format there is.

The protocol is stated as a `Protocol` rather than a base class on purpose: `NotionClient`
predates it and does not inherit from anything. `check()` is what proves they agree, and
a test calls it for both.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, cast, runtime_checkable

__all__ = ["METHODS", "Workspace", "check", "describe", "open"]

#: Every method the pipeline calls on a workspace, grouped by who calls it. The list is
#: the interface: a backend that implements these seventeen is a backend, and `check`
#: refuses one that is missing any.
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
    """The surface every backend presents. See `METHODS` for the full list."""

    #: How many backend calls have been made, which the mirror reports as cost.
    calls: int

    def whoami(self) -> dict: ...
    def search_pages(self, query: str = "") -> Iterator[dict]: ...
    def get_page(self, page_id: str) -> dict: ...
    def get_block(self, block_id: str) -> dict: ...
    def block_children(self, block_id: str) -> Iterator[dict]: ...
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
    def create_database(self, parent_page_id: str, title: str,
                        properties: dict | None = None) -> dict: ...
    def create_row(self, data_source_id: str, properties: dict,
                   children: list[dict] | None = None) -> dict: ...
    def data_source_id(self, database: dict) -> str | None: ...


def check(backend: Any) -> list[str]:
    """Method names `backend` is missing. Empty means it can stand in for the other."""
    return [name for group in METHODS.values() for name in group
            if not callable(getattr(backend, name, None))]


def open(settings: Any = None) -> Workspace:
    """Build the workspace this configuration describes.

    Shadows the builtin deliberately: `workspace.open(settings)` reads as what it does,
    and the builtin is still one `builtins.open` away in the two places this module needs
    a file.
    """
    from palimpsest.config import Settings

    settings = settings or Settings.load()
    if getattr(settings, "backend", "notion") == "markdown":
        from palimpsest.workspace.markdown import MarkdownWorkspace

        path = getattr(settings, "vault_path", None)
        if not path:
            raise ValueError("PALIMPSEST_BACKEND=markdown needs PALIMPSEST_VAULT")
        return MarkdownWorkspace(path)

    from palimpsest.notion.client import NotionClient

    # `NotionClient` predates the protocol and takes extra optional arguments on two
    # methods, so it is a structural superset rather than an exact match. `check()` is
    # what proves the seventeen names are all there, and a test calls it for both.
    client = NotionClient(settings.notion_token or "",
                          version=getattr(settings, "notion_version", "2026-03-11"))
    return cast("Workspace", client)


def describe(settings: Any = None) -> str:
    """One line naming where the knowledge base lives, for `status` and the UI."""
    from palimpsest.config import Settings

    settings = settings or Settings.load()
    if getattr(settings, "backend", "notion") == "markdown":
        return f"markdown vault at {getattr(settings, 'vault_path', '?')}"
    return "Notion" if settings.notion_token else "Notion (no token set)"
