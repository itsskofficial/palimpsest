"""Where pages live, rather than what they say.

Everything else in this project fixes the *prose*: a claim arrives, it is classified
against what you already wrote, and a small typed edit lands inside some page. None of
that touches the second way a knowledge base decays, which is the filing. Pages
accumulate at the top level because creating one there is the path of least resistance;
six pages about the same subject sit in five different places; half of them are called
"Notes" and the other half "notes 2". No amount of editing prose fixes that, because
the problem is not in any page — it is in the shape of the whole.

So this module plans the shape. It reads the mirror, proposes a taxonomy of hubs, and
emits the same kind of object every other planner here emits: a `Patch` of small typed
operations, each with an exact inverse. The operations are `create_page`, `move_page`,
`rename_page` and `set_icon`, and there is deliberately nothing bigger — no "restructure
workspace", because an operation you cannot render as a diff is one you cannot review,
and that is as true of filing as it is of sentences.

**Nothing here writes to Notion.** An import-linter contract enforces it: this module
may not import `notion.apply`. It proposes; the applier disposes.

Three things it refuses to do, each because the alternative is unrecoverable:

- **It never moves a page whose parent is the workspace root.** Notion's move endpoint
  takes a page or a data source and has no workspace variant, so such a move cannot be
  undone through the API. Those pages become review items explaining the one-way door
  rather than operations that quietly walk through it.
- **It never creates a cycle.** Moving a page under its own descendant detaches both
  from the tree. The check is local and cheap, and it runs before anything is emitted.
- **It never moves the roots themselves**, since they are the ground the structure is
  built on.

Confidence gates the rest. An assignment the model is sure of becomes an operation; one
it is not becomes a review item carrying its own reasoning. That is the whole autonomy
story for structure — automatic when confident, asked when not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from palimpsest.llm import Model, ModelError
from palimpsest.types import Operation, OpKind, Patch, new_id

__all__ = ["OrganiseResult", "organise"]

log = logging.getLogger("palimpsest.organise")

#: Pages described to the model in one pass. A personal workspace is comfortably under
#: this; a larger one is truncated to the most recently edited, and `stats` says so
#: rather than letting the omission pass silently.
MAX_PAGES = 300

#: Characters of body text shown per page. Enough to tell a page about attention
#: mechanisms from one about attention in cognitive psychology, which titles alone
#: routinely fail to do.
EXCERPT_CHARS = 240

SYSTEM = """You organise a personal Notion workspace. You decide **where pages belong**,
never what they say. You cannot edit page content and must not suggest it.

You are given every page: its title, its current parent, its role, its topics and a
short excerpt. You return a taxonomy of hubs and an assignment of pages to them.

What makes a good taxonomy here:

- **Few hubs, each earning its place.** Eight to fifteen for a typical workspace. A hub
  holding one page is not a hub; put that page in a broader one.
- **Named the way the person thinks, not the way a librarian would.** Use the
  vocabulary already present in their titles and topics. Do not invent corporate
  categories like "Miscellaneous", "Resources" or "General".
- **Reuse existing pages as hubs wherever one already does the job.** A page that is
  mostly links to other pages already *is* a hub — set `existing_page_id` to it rather
  than creating a duplicate beside it.
- **Group by subject, not by source or format.** "Papers", "Videos" and "Bookmarks" are
  bad hubs: they scatter one subject across three places, which is the exact failure
  being repaired.

Confidence is load-bearing. Assign a confidence you would defend:

- **0.9+** the page is unambiguously about this hub's subject.
- **0.7-0.9** it fits, with a defensible alternative.
- **below 0.7** you are guessing. Say so — a human will look at it, which is a far
  better outcome than a page filed confidently in the wrong place.

Only propose a rename when the current title is actively unhelpful — "Untitled",
"notes", "notes 2", "New page" — or when it is inconsistent with a clear convention the
other titles follow. A title that merely differs from your taste is not a rename. Never
rename a page to something that discards information the old title carried.

Leave a page alone when it is already well placed. `leave_alone` is a good answer and
an empty `assignments` list is a legitimate result for a tidy workspace."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hubs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "icon": {"type": "string"},
                    "rationale": {"type": "string"},
                    "existing_page_id": {"type": ["string", "null"]},
                },
                "required": ["name", "icon", "rationale", "existing_page_id"],
                "additionalProperties": False,
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string"},
                    "hub": {"type": "string"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                    "suggested_title": {"type": ["string", "null"]},
                },
                "required": ["page_id", "hub", "confidence", "rationale",
                             "suggested_title"],
                "additionalProperties": False,
            },
        },
        "leave_alone": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["page_id", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["hubs", "assignments", "leave_alone"],
    "additionalProperties": False,
}


@dataclass
class OrganiseResult:
    """A proposed shape for the workspace, plus what needs a human."""

    patch: Patch
    review: list[dict] = field(default_factory=list)
    hubs: list[dict] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.patch)

    def as_dict(self) -> dict:
        return {"patch": self.patch.as_dict(), "review": self.review,
                "hubs": self.hubs, "stats": self.stats}

    def summary(self) -> str:  # pragma: no cover - display only
        kinds: dict[str, int] = {}
        for op in self.patch.operations:
            kinds[op.kind.value] = kinds.get(op.kind.value, 0) + 1
        parts = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "nothing"
        return (f"{len(self.hubs)} hub(s) proposed\n"
                f"  {parts}\n"
                f"  {len(self.review)} needing review")


# ---------------------------------------------------------------------------
# reading the workspace
# ---------------------------------------------------------------------------


def _census(store, *, max_pages: int = MAX_PAGES) -> tuple[list[dict], dict]:
    """Every non-archived page with enough context to place it."""
    pages = [p for p in store.get_pages(limit=None) if not p.get("archived")]
    total = len(pages)
    pages.sort(key=lambda p: p.get("last_edited") or "", reverse=True)
    truncated = max(0, total - max_pages)
    pages = pages[:max_pages]

    by_id = {p["page_id"]: p for p in pages}
    for page in pages:
        blocks = store.get_blocks(page["page_id"], limit=40) or []
        text = " ".join((b.get("text") or "").strip() for b in blocks)
        page["_excerpt"] = " ".join(text.split())[:EXCERPT_CHARS]
        page["_n_blocks"] = len(blocks)
        parent = by_id.get(page.get("parent_id") or "")
        page["_parent_title"] = (parent or {}).get("title") if parent else (
            "(workspace root)" if page.get("parent_kind") in ("workspace", None)
            else "(outside the mirror)")
    return pages, {"pages_total": total, "pages_considered": len(pages),
                   "pages_truncated": truncated}


def _render(pages: list[dict]) -> str:
    lines = []
    for p in pages:
        topics = ", ".join(p.get("topics") or [])
        lines.append(
            f"- id={p['page_id']}\n"
            f"  title: {p.get('title') or '(untitled)'}\n"
            f"  parent: {p.get('_parent_title')}\n"
            f"  role: {p.get('role') or 'unknown'}"
            f"{'  topics: ' + topics if topics else ''}"
            f"  blocks: {p.get('_n_blocks', 0)}\n"
            f"  excerpt: {p.get('_excerpt') or '(empty)'}"
        )
    return "\n".join(lines)


def _descendants(pages: list[dict], page_id: str) -> set[str]:
    """Every page beneath `page_id`, for the cycle check."""
    children: dict[str, list[str]] = {}
    for p in pages:
        children.setdefault(p.get("parent_id") or "", []).append(p["page_id"])
    seen: set[str] = set()
    stack = list(children.get(page_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, []))
    return seen


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def organise(store, model: Model, *, root_page_id: str | None = None,
             min_confidence: float = 0.75, max_pages: int = MAX_PAGES,
             rename: bool = True, effort: str = "high") -> OrganiseResult:
    """Propose a shape for the workspace. Writes nothing.

    `root_page_id` is where new hubs are created. Without one, existing hubs can still
    be used and pages still moved between them, but nothing new is created — there is
    nowhere to put it, and inventing a location is not this function's decision.
    """
    pages, stats = _census(store, max_pages=max_pages)
    if not pages:
        return OrganiseResult(patch=Patch(patch_id="", source_id="organise"),
                              stats={**stats, "reason": "the mirror is empty; run "
                                                        "`palimpsest sync` first"})

    by_id = {p["page_id"]: p for p in pages}
    protected = {root_page_id} if root_page_id else set()

    try:
        proposal = model.json(
            task="organise", system=SYSTEM, effort=effort, schema=SCHEMA,
            cache_prefix=f"The workspace as it stands:\n\n{_render(pages)}",
            prompt=("Propose the taxonomy and the assignments. Every `page_id` you "
                    "return must be one of the ids above."),
        )
    except ModelError as e:
        log.warning("organisation planning failed: %s", e)
        return OrganiseResult(patch=Patch(patch_id="", source_id="organise"),
                              stats={**stats, "error": str(e)})

    hubs = proposal.get("hubs") or []
    review: list[dict] = []
    operations: list[Operation] = []

    # -- 1. hubs: reuse an existing page, or create one -------------------------
    hub_target: dict[str, Any] = {}  # hub name -> page id, or a deferred reference
    hub_rows: list[dict] = []

    for hub in hubs:
        name = (hub.get("name") or "").strip()
        if not name:
            continue
        existing = (hub.get("existing_page_id") or "").strip() or None
        if existing and existing in by_id:
            hub_target[name] = existing
            hub_rows.append({**hub, "status": "existing", "page_id": existing})
            protected.add(existing)
            icon = hub.get("icon")
            if icon and not by_id[existing].get("icon"):
                operations.append(Operation(
                    kind=OpKind.SET_ICON, target=existing, risk="low",
                    payload={"icon": icon}))
            continue

        if not root_page_id:
            review.append({
                "kind": "hub_needs_a_home", "hub": name,
                "detail": "no root page is configured, so there is nowhere to create "
                          "this hub. Set PALIMPSEST_NOTION_ROOTS to the page new "
                          "structure should be built under.",
                "rationale": hub.get("rationale", ""),
            })
            continue

        op = Operation(kind=OpKind.CREATE_PAGE, target=root_page_id, risk="low",
                       payload={"title": name, "icon": hub.get("icon") or None})
        operations.append(op)
        # The hub's Notion id does not exist yet. Moves into it therefore carry a
        # reference to this operation, which the applier resolves once it has run.
        hub_target[name] = {"from_op": op.op_id, "key": "page_id"}
        hub_rows.append({**hub, "status": "new", "op_id": op.op_id})

    # -- 2. assignments ---------------------------------------------------------
    moved: set[str] = set()
    for item in proposal.get("assignments") or []:
        page_id = (item.get("page_id") or "").strip()
        hub_name = (item.get("hub") or "").strip()
        confidence = float(item.get("confidence") or 0.0)
        page = by_id.get(page_id)

        if page is None:
            log.debug("assignment names a page not in the census: %s", page_id)
            continue
        if page_id in protected:
            continue
        if hub_name not in hub_target:
            review.append({"kind": "unknown_hub", "page_id": page_id,
                           "title": page.get("title"), "hub": hub_name,
                           "detail": "assigned to a hub that was never defined"})
            continue

        destination = hub_target[hub_name]

        # Already there: nothing to do, and emitting a no-op move would make the
        # review list look like work that is not work.
        if isinstance(destination, str) and page.get("parent_id") == destination:
            continue

        if confidence < min_confidence:
            review.append({
                "kind": "uncertain_placement", "page_id": page_id,
                "title": page.get("title"), "hub": hub_name,
                "confidence": confidence, "rationale": item.get("rationale", ""),
                "current_parent": page.get("_parent_title"),
                "detail": f"confidence {confidence:.2f} is below the {min_confidence:.2f} "
                          "floor; decide this one yourself",
            })
            continue

        # A page parented by the workspace can be filed but never restored there by
        # the API, so the move has no inverse. Surface it instead of performing it.
        parent_kind = page.get("parent_kind")
        if parent_kind in ("workspace", None) or not page.get("parent_id"):
            review.append({
                "kind": "one_way_move", "page_id": page_id,
                "title": page.get("title"), "hub": hub_name,
                "confidence": confidence, "rationale": item.get("rationale", ""),
                "detail": "this page sits at the workspace root. Notion's API can move "
                          "it into a hub but cannot move it back out, so palimpsest "
                          "will not do it automatically — drag it in Notion if you "
                          "want it filed.",
            })
            continue

        if isinstance(destination, str) and destination in _descendants(pages, page_id):
            review.append({
                "kind": "would_create_a_cycle", "page_id": page_id,
                "title": page.get("title"), "hub": hub_name,
                "detail": "the proposed hub is inside this page, so the move would "
                          "detach both from the tree",
            })
            continue

        operations.append(Operation(
            kind=OpKind.MOVE_PAGE, target=page_id, risk="medium",
            payload={"parent_page_id": destination, "hub": hub_name,
                     "rationale": item.get("rationale", ""),
                     "confidence": confidence}))
        moved.add(page_id)

        # -- 3. renames, only alongside a confident placement -------------------
        suggested = (item.get("suggested_title") or "").strip()
        if (rename and suggested and suggested != (page.get("title") or "").strip()
                and confidence >= min_confidence):
            operations.append(Operation(
                kind=OpKind.RENAME_PAGE, target=page_id, risk="medium",
                payload={"title": suggested, "was": page.get("title", "")}))

    patch = Patch(patch_id=new_id("pch_"), source_id="organise",
                  operations=operations, status="proposed",
                  notes="workspace organisation")

    return OrganiseResult(
        patch=patch, review=review, hubs=hub_rows,
        stats={**stats, "hubs_proposed": len(hub_rows),
               "hubs_new": sum(1 for h in hub_rows if h["status"] == "new"),
               "pages_moved": len(moved),
               "left_alone": len(proposal.get("leave_alone") or []),
               "operations": len(operations), "review": len(review)},
    )
