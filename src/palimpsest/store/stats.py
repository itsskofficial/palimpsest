"""`StoreStats`, in its own module so the SQLite store never reaches the Postgres one.

This looks like over-separation for one small class, and it is not. `store/base.py`
holds the protocol *and* `open_store`, and `open_store` resolves `PostgresStore` — so
any module importing `base` acquires a static import path to `psycopg2`, even though
the import is function-scoped and never executed on the SQLite path.

That breaks the "the offline core stays dependency-free" contract in `pyproject.toml`
for a reason that is an artefact of where a type was declared rather than a real
dependency. Moving the type here makes the contract true rather than exempted, which is
the difference between an invariant and a comment.
"""

from __future__ import annotations

__all__ = ["StoreStats"]


class StoreStats(dict):
    """Row counts per table, for the dashboard header and `palimpsest status`."""

    def summary(self) -> str:  # pragma: no cover - display only
        return "  ".join(f"{k}={v}" for k, v in sorted(self.items()))
