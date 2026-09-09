"""The committed workspace the evals run against.

Every eval needs notes to run against, and where those notes come from decides what the
number means. Harvesting the user's own approvals is cheap and it is what `golden.py`
does — but a set drawn from one person's history inherits their habits, cannot be
compared between machines, and is empty on a fresh clone, which is where it matters most.

So a small workspace ships with the package: ten pages, two dozen blocks, and a set of
labelled questions about them. It is deliberately uneven the way real notes are — careful
prose next to a line of shorthand, two pages that disagree with each other, one price that
has since changed — because a clean corpus makes retrieval and classification look better
than they are.

The two sets are separate on purpose. Retrieval cases need no model, so they run in CI as
an ordinary test and a regression in ranking fails the build. Relation cases need one, so
they run from `palimpsest eval component` when you ask for them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = ["RelationCase", "RetrievalCase", "cases", "golden_examples", "load_workspace",
           "seed"]

DATA = Path(__file__).parent / "data" / "workspace.json"


@dataclass(frozen=True)
class RetrievalCase:
    """One question, and the pages that genuinely answer it."""

    id: str
    query: str
    pages: tuple[str, ...]
    blocks: tuple[str, ...]
    why: str
    #: True when no amount of lexical cleverness can reach the answer, because the note
    #: and the query share no vocabulary at all. Scored separately: failing these with no
    #: embedding provider configured is expected, not a regression.
    needs_vectors: bool = False


@dataclass(frozen=True)
class RelationCase:
    """One claim, and how it ought to relate to the workspace."""

    id: str
    claim: str
    topics: tuple[str, ...]
    source_title: str
    source_kind: str
    expect: str
    why: str


@lru_cache(maxsize=1)
def load_workspace() -> dict:
    """The raw fixture. Cached: it is read once and never mutated."""
    return json.loads(DATA.read_text(encoding="utf-8"))


def seed(store) -> tuple[int, int]:
    """Write the fixture workspace into a store. Returns (pages, blocks).

    Used by the evals and by tests. Safe to call twice — the store upserts.
    """
    data = load_workspace()
    pages = [{**p, "last_edited": "2026-01-01T00:00:00.000Z"} for p in data["pages"]]
    blocks = [{**b, "type": "paragraph"} for b in data["blocks"]]
    store.put_pages(pages)
    store.put_blocks(blocks)
    links = data.get("links") or []
    if links:
        store.put_links([(link["from_page"], link["to_page"], None) for link in links])
    return len(pages), len(blocks)


def golden_examples() -> list:
    """The relation cases as `GoldenExample`s, so the component eval can run on them.

    Imported lazily: `golden` reaches into the store and this module is also used by the
    retrieval eval, which must stay loadable with nothing configured.
    """
    from palimpsest.evals.golden import GoldenExample

    _, relations = cases()
    return [
        GoldenExample(
            id=case.id, claim_text=case.claim, claim_type="fact", topics=case.topics,
            source_title=case.source_title, source_kind=case.source_kind,
            expected_relation=case.expect, label_source="fixture")
        for case in relations
    ]


def cases() -> tuple[list[RetrievalCase], list[RelationCase]]:
    data = load_workspace()
    retrieval = [
        RetrievalCase(id=c["id"], query=c["query"], pages=tuple(c["pages"]),
                      blocks=tuple(c.get("blocks") or ()), why=c["why"],
                      needs_vectors=bool(c.get("needs_vectors")))
        for c in data["retrieval"]
    ]
    relations = [
        RelationCase(id=c["id"], claim=c["claim"], topics=tuple(c.get("topics") or ()),
                     source_title=c["source_title"], source_kind=c["source_kind"],
                     expect=c["expect"], why=c["why"])
        for c in data["relations"]
    ]
    return retrieval, relations
