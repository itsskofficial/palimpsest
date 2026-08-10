"""The vocabulary of the whole system: claims, relations, operations, patches.

Everything downstream is a function over these types, so it is worth being precise
about why they are shaped the way they are.

**A claim, not a document.** The unit that moves through the pipeline is a single
atomic assertion with a source anchor, not a chunk of text. "Llama-3-8B reproduces the
effect at 20% at layer 9" is three claims, and they can have three different
relationships to what you already wrote. Chunk-level processing cannot express that,
which is why every "sync my notes" tool ends up appending instead of merging.

**A relation, not a decision.** The classifier does not answer "update or create". It
answers "how does this claim relate to that block", and the seven answers each imply a
different edit. That indirection is what makes the system reviewable: you can disagree
with a *relation* and understand what you are disagreeing with.

**A patch, not a rewrite.** An edit is a list of small typed operations, each of which
knows how to undo itself. Nothing in this system can express "rewrite the page", which
is deliberate — an operation you cannot render as a diff is an operation you cannot
review, and an unreviewable operation on your own notes is one you will eventually
stop trusting.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Anchor",
    "Claim",
    "ClaimType",
    "Judgement",
    "OpKind",
    "Operation",
    "Patch",
    "Relation",
    "Source",
    "new_id",
]


def new_id(prefix: str = "") -> str:
    """A short, sortable-enough identifier. Not a Notion id — ours."""
    return f"{prefix}{uuid.uuid4().hex[:16]}" if prefix else uuid.uuid4().hex[:16]


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# sources and anchors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """Where in a source a claim came from, precisely enough to go back to it.

    This is the part every comparable tool skips, and skipping it is why their
    citations rot. "Source: that YouTube video" is decoration. `t=862` is a link that
    opens the video at the moment the claim was made, and it is still true in two
    years. The same applies to a PDF page plus character range, or a spreadsheet cell.

    `locator` is the human-facing form (`p. 14`, `14:22`, `Sheet1!B7`) and `url` is the
    deep link when the source has one. `start`/`end` are character offsets into the
    normalised text, which is what lets the reviewer highlight the exact sentence.
    """

    kind: str  # page | timestamp | cell | region | offset
    locator: str
    start: int | None = None
    end: int | None = None
    url: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "locator": self.locator, "start": self.start,
                "end": self.end, "url": self.url}

    @classmethod
    def from_dict(cls, d: dict) -> Anchor:
        return cls(d["kind"], d["locator"], d.get("start"), d.get("end"), d.get("url"))

    def cite(self) -> str:  # pragma: no cover - display only
        return f"{self.locator}"


@dataclass
class Source:
    """A normalised artifact: text plus enough metadata to cite and re-fetch it.

    `content_hash` is what makes re-ingestion free — drop the same PDF twice and the
    second run is a cache hit rather than another extraction bill.
    """

    source_id: str
    kind: str  # web | youtube | pdf | image | tabular | text | audio
    title: str
    text: str
    url: str | None = None
    author: str | None = None
    published_at: str | None = None
    fetched_at: float = field(default_factory=time.time)
    archive_key: str | None = None  # where the original bytes live, if archived
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return _hash(self.kind, self.url or "", self.text)

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id, "kind": self.kind, "title": self.title,
            "text": self.text, "url": self.url, "author": self.author,
            "published_at": self.published_at, "fetched_at": self.fetched_at,
            "archive_key": self.archive_key, "meta": self.meta,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Source:
        return cls(
            source_id=d["source_id"], kind=d["kind"], title=d.get("title", ""),
            text=d.get("text", ""), url=d.get("url"), author=d.get("author"),
            published_at=d.get("published_at"), fetched_at=d.get("fetched_at", 0.0),
            archive_key=d.get("archive_key"), meta=d.get("meta", {}),
        )

    def citation(self) -> str:  # pragma: no cover - display only
        bits = [self.title or self.url or self.kind]
        if self.author:
            bits.append(self.author)
        if self.published_at:
            bits.append(self.published_at)
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------


class ClaimType(str, Enum):
    """What kind of assertion this is. Drives how conservatively it is handled.

    `NUMBER` and `QUOTE` are separated from `FACT` on purpose: a number is the thing
    most likely to be *refined* by a later source, and a quote is the thing that must
    never be silently paraphrased.
    """

    FACT = "fact"
    DEFINITION = "definition"
    PROCEDURE = "procedure"
    NUMBER = "number"
    QUOTE = "quote"
    OPINION = "opinion"
    QUESTION = "question"


@dataclass
class Claim:
    """One atomic assertion, with the anchor that proves where it came from."""

    claim_id: str
    text: str
    type: ClaimType = ClaimType.FACT
    topics: tuple[str, ...] = ()
    confidence: float = 1.0
    anchor: Anchor | None = None
    source_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "claim_id": self.claim_id, "text": self.text, "type": self.type.value,
            "topics": list(self.topics), "confidence": self.confidence,
            "anchor": self.anchor.as_dict() if self.anchor else None,
            "source_id": self.source_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Claim:
        return cls(
            claim_id=d.get("claim_id") or new_id("clm_"),
            text=d["text"],
            type=ClaimType(d.get("type", "fact")),
            topics=tuple(d.get("topics", ())),
            confidence=float(d.get("confidence", 1.0)),
            anchor=Anchor.from_dict(d["anchor"]) if d.get("anchor") else None,
            source_id=d.get("source_id"),
        )


# ---------------------------------------------------------------------------
# relations — the core of the product
# ---------------------------------------------------------------------------


class Relation(str, Enum):
    """How a new claim relates to what the knowledge base already holds.

    The taxonomy *is* the product. Three of these deserve their reasoning stated:

    - `CORROBORATES` is why the base stops bloating. Most "new information" about a
      topic you already studied is the same claim from a different source. Under any
      other design that becomes a new paragraph, or a new page. Here it becomes one
      extra citation on a sentence that already exists, and no prose is added at all.
    - `DUPLICATE` is the direct fix for the reported symptom — the same thing written
      twice on two different pages. Its edit is a merge or a link, never an insert.
    - `CONTRADICTS` is the one relation that is never auto-applied at any autonomy
      level. A knowledge base that silently replaces a true claim with a false one is
      worse than no automation, because it destroys your ability to trust the parts
      that are still right.
    """

    NEW = "new"
    CORROBORATES = "corroborates"
    REFINES = "refines"
    SUPERSEDES = "supersedes"
    DUPLICATE = "duplicate"
    EXTENDS = "extends"
    CONTRADICTS = "contradicts"

    @property
    def risk(self) -> str:
        """How much damage a wrong call does. Gates the autonomy ladder."""
        return {
            Relation.NEW: "low",
            Relation.CORROBORATES: "low",
            Relation.REFINES: "medium",
            Relation.SUPERSEDES: "medium",
            Relation.DUPLICATE: "medium",
            Relation.EXTENDS: "medium",
            Relation.CONTRADICTS: "high",
        }[self]

    @property
    def auto_appliable(self) -> bool:
        """Whether this relation may *ever* be applied without a human.

        `CONTRADICTS` is `False` unconditionally — not "False until the classifier
        proves itself". Precision on the other six is earned by measurement; this one
        is a policy, and policies do not have thresholds.
        """
        return self is not Relation.CONTRADICTS

    def describe(self) -> str:  # pragma: no cover - display only
        return {
            Relation.NEW: "nothing in the base covers this",
            Relation.CORROBORATES: "already known; this is a second source",
            Relation.REFINES: "a more precise version of what you have",
            Relation.SUPERSEDES: "same fact, newer or more authoritative",
            Relation.DUPLICATE: "you already wrote this, on another page",
            Relation.EXTENDS: "related, but belongs on a different page",
            Relation.CONTRADICTS: "the source disagrees with the page",
        }[self]


@dataclass
class Judgement:
    """The classifier's verdict for one claim against one candidate location."""

    claim_id: str
    relation: Relation
    confidence: float
    target_page_id: str | None = None
    target_block_id: str | None = None
    rationale: str = ""
    # For REFINES / SUPERSEDES: the existing text this claim sharpens or replaces.
    existing_text: str | None = None
    model: str = ""

    @property
    def needs_human(self) -> bool:
        return not self.relation.auto_appliable

    def as_dict(self) -> dict:
        return {
            "claim_id": self.claim_id, "relation": self.relation.value,
            "confidence": self.confidence, "target_page_id": self.target_page_id,
            "target_block_id": self.target_block_id, "rationale": self.rationale,
            "existing_text": self.existing_text, "model": self.model,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Judgement:
        return cls(
            claim_id=d["claim_id"], relation=Relation(d["relation"]),
            confidence=float(d.get("confidence", 0.0)),
            target_page_id=d.get("target_page_id"), target_block_id=d.get("target_block_id"),
            rationale=d.get("rationale", ""), existing_text=d.get("existing_text"),
            model=d.get("model", ""),
        )


# ---------------------------------------------------------------------------
# operations and patches
# ---------------------------------------------------------------------------


class OpKind(str, Enum):
    """The complete set of edits this system can express.

    Note what is absent: there is no `rewrite_page`, no `set_content`, no free-form
    mutation of any kind. Every operation is small enough to render in a diff and to
    invert exactly. That constraint is load-bearing — it is what makes `undo` real
    rather than best-effort, and it is why the review UI can exist at all.

    `archive_block` rather than `delete_block` is also deliberate: Notion's API can
    delete, and this system never does. The worst case is a struck-through block with
    a footnote saying why.
    """

    APPEND_BLOCK = "append_block"
    UPDATE_TEXT = "update_text"
    ADD_CITATION = "add_citation"
    INSERT_FOOTNOTE = "insert_footnote"
    STRIKE_BLOCK = "strike_block"
    ARCHIVE_BLOCK = "archive_block"
    CREATE_PAGE = "create_page"
    LINK_PAGES = "link_pages"


@dataclass
class Operation:
    """A single typed edit, plus everything needed to undo it.

    `inverse` is computed *before* the operation runs and stored with it. That ordering
    matters: Notion has no transactions, so a patch can be interrupted halfway. Writing
    the undo first means a half-applied patch is still fully reversible, which is the
    difference between a bad afternoon and a lost page.
    """

    kind: OpKind
    target: str  # block id, page id, or a parent page id for creates
    payload: dict[str, Any] = field(default_factory=dict)
    inverse: dict[str, Any] | None = None
    claim_id: str | None = None
    relation: Relation | None = None
    op_id: str = field(default_factory=lambda: new_id("op_"))
    applied_at: float | None = None
    result: dict[str, Any] | None = None

    @property
    def applied(self) -> bool:
        return self.applied_at is not None

    def summary(self) -> str:  # pragma: no cover - display only
        verb = {
            OpKind.APPEND_BLOCK: "append",
            OpKind.UPDATE_TEXT: "edit",
            OpKind.ADD_CITATION: "cite",
            OpKind.INSERT_FOOTNOTE: "footnote",
            OpKind.STRIKE_BLOCK: "strike",
            OpKind.ARCHIVE_BLOCK: "archive",
            OpKind.CREATE_PAGE: "create page",
            OpKind.LINK_PAGES: "link",
        }[self.kind]
        text = self.payload.get("text") or self.payload.get("title") or ""
        return f"{verb}: {text[:80]}" if text else verb

    def as_dict(self) -> dict:
        return {
            "op_id": self.op_id, "kind": self.kind.value, "target": self.target,
            "payload": self.payload, "inverse": self.inverse, "claim_id": self.claim_id,
            "relation": self.relation.value if self.relation else None,
            "applied_at": self.applied_at, "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Operation:
        return cls(
            kind=OpKind(d["kind"]), target=d["target"], payload=d.get("payload", {}),
            inverse=d.get("inverse"), claim_id=d.get("claim_id"),
            relation=Relation(d["relation"]) if d.get("relation") else None,
            op_id=d.get("op_id") or new_id("op_"), applied_at=d.get("applied_at"),
            result=d.get("result"),
        )


@dataclass
class Patch:
    """An ordered, reviewable, reversible set of operations from one source."""

    patch_id: str
    source_id: str
    operations: list[Operation] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "proposed"  # proposed | applied | rejected | reverted | partial
    reviewer: str | None = None
    reviewed_at: float | None = None
    notes: str = ""

    def __len__(self) -> int:
        return len(self.operations)

    def __iter__(self):
        return iter(self.operations)

    @property
    def pages_touched(self) -> list[str]:
        return sorted({op.target for op in self.operations})

    @property
    def needs_human(self) -> bool:
        """True when any operation came from a relation a human must decide."""
        return any(op.relation and not op.relation.auto_appliable for op in self.operations)

    def by_relation(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for op in self.operations:
            key = op.relation.value if op.relation else "other"
            out[key] = out.get(key, 0) + 1
        return out

    def reverse(self) -> Patch:
        """The patch that undoes this one.

        Operations invert in reverse order, and only the ones that actually ran are
        included — a partially-applied patch inverts to exactly the part that landed.
        """
        ops: list[Operation] = []
        for op in reversed(self.operations):
            if not op.applied or op.inverse is None:
                continue
            ops.append(Operation(
                kind=OpKind(op.inverse["kind"]),
                target=op.inverse["target"],
                payload=op.inverse.get("payload", {}),
                claim_id=op.claim_id,
                relation=op.relation,
            ))
        return Patch(patch_id=new_id("rev_"), source_id=self.source_id, operations=ops,
                     status="proposed", notes=f"reverse of {self.patch_id}")

    def summary(self) -> str:  # pragma: no cover - display only
        counts = self.by_relation()
        parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        return (f"patch {self.patch_id}  {len(self.operations)} op(s)  "
                f"[{', '.join(parts) or 'empty'}]  status={self.status}")

    def as_dict(self) -> dict:
        return {
            "patch_id": self.patch_id, "source_id": self.source_id,
            "operations": [op.as_dict() for op in self.operations],
            "created_at": self.created_at, "status": self.status,
            "reviewer": self.reviewer, "reviewed_at": self.reviewed_at,
            "notes": self.notes, "by_relation": self.by_relation(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Patch:
        return cls(
            patch_id=d["patch_id"], source_id=d.get("source_id", ""),
            operations=[Operation.from_dict(o) for o in d.get("operations", [])],
            created_at=d.get("created_at", 0.0), status=d.get("status", "proposed"),
            reviewer=d.get("reviewer"), reviewed_at=d.get("reviewed_at"),
            notes=d.get("notes", ""),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, ensure_ascii=False, default=str)
