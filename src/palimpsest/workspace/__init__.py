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

The interface itself is `palimpsest.notion.protocol.Workspace`, re-exported here.
"""

from __future__ import annotations

from typing import Any, cast

from palimpsest.notion.protocol import METHODS, Workspace, check

__all__ = ["METHODS", "Workspace", "check", "describe", "open"]

#: `Workspace`, `METHODS` and `check` are defined in `palimpsest.notion.protocol` and
#: re-exported here. They live down there because the modules that consume them —
#: `mirror`, `apply`, `journal` — live down there, and `notion` sits below `workspace`
#: in the layering contract so it cannot import upward. This is the name to use.


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
