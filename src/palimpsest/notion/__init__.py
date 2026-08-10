"""Everything that touches Notion: the client, the block algebra, the mirror, the applier.

    from palimpsest.notion import NotionClient, mirror
    from palimpsest.notion.apply import apply_patch

`apply` is deliberately *not* re-exported here. It is the only module allowed to write
to Notion and an import-linter contract enforces that nothing but the pipeline and the
CLI reaches it — surfacing it as `palimpsest.notion.apply_patch` would make that easy
to violate by accident.
"""

from palimpsest.notion.client import NotionClient, NotionError, RateLimiter

__all__ = ["NotionClient", "NotionError", "RateLimiter", "blocks", "mirror"]


def __getattr__(name: str):
    # `importlib`, not `from palimpsest.notion import mirror`: the latter calls
    # getattr on this module first and recurses straight back into here.
    if name in ("mirror", "blocks"):
        import importlib

        return importlib.import_module(f"palimpsest.notion.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
