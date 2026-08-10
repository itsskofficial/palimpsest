"""palimpsest — a self-maintaining knowledge base on top of Notion.

    import palimpsest as pal

    store  = pal.open_store("sqlite:///palimpsest.db")
    client = pal.NotionClient(token)
    pal.mirror.sync(client, store)                    # pull Notion in

    dupes = pal.sweep.duplicates(store)               # no API key needed
    result = pal.ingest("https://example.com/post", store, pal.Model())
    print(result.patch.summary())                     # nothing written yet

The core idea: the unit of work is a **claim**, not a document, and the question is not
"update or create" but *how does this claim relate to what I already wrote* — one of
seven relations, each implying a different small, reversible edit.

Nothing here writes to Notion. Applying a patch is a separate, explicit call
(`palimpsest.notion.apply.apply_patch`), and contradictions are never applied
automatically at any autonomy level.
"""

from palimpsest._version import __version__
from palimpsest.config import Settings
from palimpsest.store import open_store
from palimpsest.types import (
    Anchor,
    Claim,
    ClaimType,
    Judgement,
    Operation,
    OpKind,
    Patch,
    Relation,
    Source,
)

__all__ = [
    "Anchor",
    "Claim",
    "ClaimType",
    "Index",
    "Judgement",
    "Model",
    "NotionClient",
    "OpKind",
    "Operation",
    "Patch",
    "Relation",
    "Settings",
    "Source",
    "__version__",
    "extract",
    "ingest",
    "mirror",
    "open_store",
    "plan",
    "sweep",
]


def __getattr__(name: str):
    """Lazy re-exports, so `import palimpsest` never pulls an optional dependency.

    Submodules are resolved with `importlib.import_module` rather than
    `from palimpsest import sweep`: the latter calls `getattr` on this module first,
    which lands back in here and recurses until the stack runs out.
    """
    import importlib

    if name in ("sweep", "mirror"):
        target = "palimpsest.sweep" if name == "sweep" else "palimpsest.notion.mirror"
        return importlib.import_module(target)
    if name == "Model":
        from palimpsest.llm import Model

        return Model
    if name == "NotionClient":
        from palimpsest.notion.client import NotionClient

        return NotionClient
    if name == "Index":
        from palimpsest.retrieve import Index

        return Index
    if name == "ingest":
        from palimpsest.pipeline import ingest

        return ingest
    if name == "extract":
        from palimpsest.extract import extract

        return extract
    if name == "plan":
        from palimpsest.plan import plan

        return plan
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
