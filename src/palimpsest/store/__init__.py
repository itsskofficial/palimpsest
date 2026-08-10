"""Persistence: the Notion mirror, the pipeline, and the patch ledger.

    from palimpsest.store import open_store

    store = open_store("sqlite:///palimpsest.db")   # default, stdlib, one file
    store = open_store("postgresql://...")          # Supabase works unchanged

The store is where every guarantee this project makes actually lives. Notion's API has
no diff primitive, no transactions and no usable version history, so exact undo, "which
source produced this sentence", and time travel are all properties of *our* ledger, not
of Notion.
"""

from palimpsest.store.base import Store, open_store
from palimpsest.store.sqlite import SQLiteStore
from palimpsest.store.stats import StoreStats

__all__ = ["PostgresStore", "SQLiteStore", "Store", "StoreStats", "open_store"]


def __getattr__(name: str):
    """`PostgresStore` is resolved lazily so psycopg2 stays an optional extra."""
    if name == "PostgresStore":
        from palimpsest.store.postgres import PostgresStore

        return PostgresStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
