"""The tool surface — defined once, as data.

Each tool is a `Tool`: a name, a description that is genuine prompt surface, a JSON
schema the model's arguments are validated against, and a handler that does the work
through the shared `ToolContext`. The registry is a plain list so it can be rendered
into the Anthropic `tools` parameter, counted, and eval'd — nothing about it is magic.

The tools divide by how much trust they need, and the division is real, not cosmetic:

- **read** tools touch only the local mirror and never spend a token or write anything;
- **analyse** tools may spend tokens (extraction, classification) but still write
  nothing to Notion — they queue work or run a sweep;
- **act** tools are the only ones that can change your notes, and every one of them
  goes through `palimpsest.approval`, so "the agent applied it" always means "within the
  autonomy you set, or with your tap".

A handler returns a plain dict. It never raises for an expected condition — a missing
page is a `{"error": ...}` the model can read and recover from, not an exception that
ends the turn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from palimpsest.agent.context import ToolContext

log = logging.getLogger("palimpsest.agent.registry")

__all__ = ["Tool", "build_registry"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., dict]
    group: str = "read"  # read | analyse | act | memory
    #: Whether calling this can change Notion. Only `act` tools are, and the safety
    #: evals assert that the set of writing tools is exactly the gated ones.
    writes: bool = False

    def spec(self) -> dict:
        """The shape the Anthropic API wants in `tools`."""
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


def _s(**properties) -> dict:
    """A small JSON-schema helper: required = every property without a default marker."""
    required = [k for k, v in properties.items() if not v.pop("_optional", False)]
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _search_notes(ctx: ToolContext, query: str, limit: int = 8) -> dict:
    hits = ctx.index.pages_for(query, top=min(int(limit), 20))
    out = []
    for h in hits:
        page = ctx.store.get_page(h.page_id) or {}
        blocks = ctx.store.get_blocks(h.page_id, limit=3) or []
        out.append({
            "page_id": h.page_id, "title": h.title, "role": h.role,
            "score": round(h.score, 2), "url": page.get("url"),
            "excerpt": " ".join((b.get("text") or "") for b in blocks)[:400],
        })
    return {"query": query, "results": out, "count": len(out)}


def _read_page(ctx: ToolContext, page_id: str) -> dict:
    page = ctx.store.get_page(page_id)
    if page is None:
        return {"error": f"no page {page_id} in the mirror; run sync_mirror or check the id"}
    blocks = ctx.store.get_blocks(page_id) or []
    return {
        "page_id": page_id, "title": page.get("title"), "role": page.get("role"),
        "url": page.get("url"),
        "blocks": [{"block_id": b.get("block_id"), "type": b.get("type"),
                    "text": b.get("text")} for b in blocks[:80]],
        "history": ctx.store.page_history(page_id, limit=10),
    }


def _get_provenance(ctx: ToolContext, block_id: str) -> dict:
    rows = ctx.store.provenance_for_block(block_id)
    return {"block_id": block_id, "provenance": rows,
            "note": "empty means this block was not written by palimpsest" if not rows else None}


def _get_patch(ctx: ToolContext, patch_id: str) -> dict:
    patch = ctx.store.get_patch(patch_id)
    if patch is None:
        return {"error": f"no patch {patch_id}"}
    d = patch.as_dict()
    # Trim to what the model needs to explain it — full inverses are noise here.
    return {"patch_id": patch_id, "status": d["status"], "by_relation": d["by_relation"],
            "operations": [{"kind": o["kind"], "relation": o["relation"],
                            "target": o["target"], "why": (o["payload"] or {}).get("rationale"),
                            "text": (o["payload"] or {}).get("text")
                            or (o["payload"] or {}).get("title")}
                           for o in d["operations"][:40]]}


def _list_pending(ctx: ToolContext, limit: int = 20) -> dict:
    approvals = ctx.store.list_approvals(status="pending", limit=int(limit))
    patches = ctx.store.list_patches(status="proposed", limit=int(limit))
    return {"approvals": [{"approval_id": a["approval_id"], "patch_id": a["patch_id"],
                           "summary": a.get("summary"),
                           "operations": len(a.get("operation_ids") or [])}
                          for a in approvals],
            "proposed_patches": [{"patch_id": p["patch_id"], "operations": p["n_ops"]}
                                 for p in patches]}


# ---------------------------------------------------------------------------
# analyse
# ---------------------------------------------------------------------------


def _capture_source(ctx: ToolContext, spec: str, kind: str | None = None,
                    title: str | None = None, url: str | None = None) -> dict:
    # No explicit origin: enqueue stamps the current chat so the async result reports
    # back to whoever asked, falling back to "agent" outside a chat turn.
    job = ctx.enqueue(spec, source_kind=kind, title=title, url=url)
    return {"job_id": job["job_id"], "status": "queued",
            "note": "ingestion runs in the background; check_job for the result"}


def _check_job(ctx: ToolContext, job_id: str) -> dict:
    job = ctx.store.get_job(job_id)
    if job is None:
        return {"error": f"no job {job_id}"}
    result = job.get("result") or {}
    return {"job_id": job_id, "status": job["status"], "error": job.get("error"),
            "claims": result.get("claims"), "patch_id": (result.get("patch") or {}).get("patch_id"),
            "applied": (result.get("auto_applied") or {}).get("applied"),
            "approval_id": (result.get("auto_applied") or {}).get("approval_id")}


def _sync_mirror(ctx: ToolContext, incremental: bool = True) -> dict:
    from palimpsest.notion import mirror

    if not ctx.settings.has_notion:
        return {"error": "NOTION_TOKEN is not set"}
    result = mirror.sync(ctx.new_notion(), ctx.store, incremental=bool(incremental),
                         roots=ctx.settings.notion_root_pages)
    ctx.refresh_index()
    return result.as_dict()


def _run_sweep(ctx: ToolContext, kind: str) -> dict:
    from palimpsest import sweep as sweeps

    if kind == "duplicates":
        return sweeps.duplicates(ctx.store, index=ctx.index).as_dict()
    if kind == "stale":
        return sweeps.stale(ctx.store).as_dict()
    if kind == "questions":
        return sweeps.open_questions(ctx.store).as_dict()
    if kind == "contradictions":
        if not ctx.settings.has_model:
            return {"error": "the contradiction sweep needs a model"}
        return sweeps.contradictions(ctx.store, ctx.model, index=ctx.index).as_dict()
    return {"error": f"unknown sweep {kind!r}; use duplicates, contradictions, stale, questions"}


def _propose_organisation(ctx: ToolContext) -> dict:
    from palimpsest.organise import organise

    if not ctx.settings.has_model:
        return {"error": "planning a taxonomy needs a model"}
    result = organise(ctx.store, ctx.model, root_page_id=ctx.root_page_id,
                      min_confidence=ctx.settings.min_confidence)
    if len(result.patch):
        ctx.store.put_patch(result.patch)
    d = result.as_dict()
    return {"patch_id": result.patch.patch_id if len(result.patch) else None,
            "hubs": [h.get("name") for h in result.hubs], "stats": d["stats"],
            "review": len(result.review)}


# ---------------------------------------------------------------------------
# act — every one goes through the gate
# ---------------------------------------------------------------------------


def _apply_patch(ctx: ToolContext, patch_id: str,
                 operation_ids: list | None = None) -> dict:
    from palimpsest import approval

    patch = ctx.store.get_patch(patch_id)
    if patch is None:
        return {"error": f"no patch {patch_id}"}
    if operation_ids:
        keep = set(operation_ids)
        patch.operations = [op for op in patch.operations if op.op_id in keep]

    outcome = approval.gate(
        ctx.store, patch, ctx.settings,
        notion_factory=ctx.new_notion if ctx.settings.has_notion else None,
        journal_factory=ctx.new_journal if ctx.settings.has_notion else None,
        reviewer="agent")
    ctx.refresh_index()
    # The agent must not imply it wrote something it only queued for approval.
    if outcome.get("approval_id"):
        outcome["note"] = ("held for the user's approval — it is NOT applied yet; "
                           "tell them it is waiting for their tap")
    return outcome


def _undo_patch(ctx: ToolContext, patch_id: str) -> dict:
    from palimpsest.notion.apply import revert_patch

    patch = ctx.store.get_patch(patch_id)
    if patch is None:
        return {"error": f"no patch {patch_id}"}
    if not (ctx.settings.apply and ctx.settings.has_notion):
        return {"error": "writes are off (PALIMPSEST_APPLY=0) or NOTION_TOKEN is unset; "
                         "nothing to undo was applied"}
    result = revert_patch(ctx.new_notion(), ctx.store, patch, reviewer="agent",
                          journal=ctx.new_journal())
    ctx.refresh_index()
    return result.as_dict()


def _reject_patch(ctx: ToolContext, patch_id: str, reason: str = "") -> dict:
    ctx.store.set_patch_status(patch_id, "rejected", reviewer="agent", notes=reason)
    # Also close any pending approval that pointed at it.
    for a in ctx.store.list_approvals(status="pending", limit=50):
        if a["patch_id"] == patch_id:
            ctx.store.resolve_approval(a["approval_id"], "rejected", "agent")
    return {"patch_id": patch_id, "status": "rejected"}


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def _remember(ctx: ToolContext, fact: str, key: str,
              kind: str = "preference") -> dict:
    ctx.store.put_memory(kind, key, fact, source="agent")
    return {"remembered": True, "kind": kind, "key": key}


def _recall(ctx: ToolContext, query: str = "") -> dict:
    memories = ctx.store.get_memories(limit=100)
    q = query.lower()
    if q:
        memories = [m for m in memories
                    if q in m["key"].lower() or q in m["value"].lower()]
    return {"memories": [{"kind": m["kind"], "key": m["key"], "value": m["value"]}
                         for m in memories[:40]]}


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def build_registry(ctx: ToolContext) -> list[Tool]:
    """The tools, bound to a context. Order is the order the model sees them."""

    def bind(fn):
        def call(**kwargs):
            return fn(ctx, **kwargs)
        return call

    return [
        # -- read --
        Tool("search_notes",
             "Search the knowledge base for pages relevant to a query. Returns page "
             "titles, roles, URLs and short excerpts. Use this to answer questions about "
             "what the user already knows before saying you don't know something.",
             _s(query={"type": "string", "description": "what to look for"},
                limit={"type": "integer", "description": "max pages (default 8)", "_optional": True}),
             bind(_search_notes), group="read"),
        Tool("read_page",
             "Read one page in full: its blocks and recent change history. Use after "
             "search_notes to quote a page accurately. Cite the page_id you read.",
             _s(page_id={"type": "string"}),
             bind(_read_page), group="read"),
        Tool("get_provenance",
             "For a block, show which source, claim and anchor produced its text and who "
             "approved it. This answers 'why does this say that / where did it come from'.",
             _s(block_id={"type": "string"}),
             bind(_get_provenance), group="read"),
        Tool("get_patch",
             "Show a proposed patch: its operations, the relation behind each, and the "
             "reasoning. Use before apply_patch so you can explain what will change.",
             _s(patch_id={"type": "string"}),
             bind(_get_patch), group="read"),
        Tool("list_pending",
             "List patches and approvals waiting for the user. Use for 'what needs my "
             "review' or 'anything pending'.",
             _s(limit={"type": "integer", "_optional": True}),
             bind(_list_pending), group="read"),

        # -- analyse --
        Tool("capture_source",
             "Ingest a source into the knowledge base: a URL, a YouTube link, a file "
             "path, or 'text:...' / 'transcript:...'. Returns a job_id; ingestion runs in "
             "the background. Use when the user shares something to remember. Note: files "
             "the user sends are captured automatically, so you rarely need this for those.",
             _s(spec={"type": "string", "description": "url | path | text:... | transcript:..."},
                kind={"type": ["string", "null"], "_optional": True},
                title={"type": ["string", "null"], "_optional": True},
                url={"type": ["string", "null"], "_optional": True}),
             bind(_capture_source), group="analyse"),
        Tool("check_job",
             "Check a capture job by id: whether it finished, how many claims it found, "
             "and the patch it produced.",
             _s(job_id={"type": "string"}),
             bind(_check_job), group="analyse"),
        Tool("sync_mirror",
             "Pull the latest from Notion into the local mirror. Use when the user says "
             "they edited Notion directly, or before organising.",
             _s(incremental={"type": "boolean", "_optional": True}),
             bind(_sync_mirror), group="analyse"),
        Tool("run_sweep",
             "Audit the existing notes. 'duplicates' finds the same thing written twice, "
             "'contradictions' finds notes that disagree with themselves, 'stale' finds "
             "claims past their half-life, 'questions' lists open questions. Writes nothing.",
             _s(kind={"type": "string", "enum": ["duplicates", "contradictions", "stale", "questions"]}),
             bind(_run_sweep), group="analyse"),
        Tool("propose_organisation",
             "Propose a tidier shape for the workspace — hubs, moves, renames — as a "
             "patch to review. Writes nothing until applied.",
             _s(),
             bind(_propose_organisation), group="analyse"),

        # -- act (gated) --
        Tool("apply_patch",
             "Apply a proposed patch to Notion. Operations within the user's autonomy "
             "setting apply immediately; the rest are HELD for the user to approve — you "
             "cannot force those through, and you must not tell the user something was "
             "applied when it is only held. You cannot change the autonomy setting.",
             _s(patch_id={"type": "string"},
                operation_ids={"type": ["array", "null"], "items": {"type": "string"},
                               "_optional": True}),
             bind(_apply_patch), group="act", writes=True),
        Tool("undo_patch",
             "Revert a previously applied patch, exactly. Use when the user regrets a "
             "change.",
             _s(patch_id={"type": "string"}),
             bind(_undo_patch), group="act", writes=True),
        Tool("reject_patch",
             "Reject a proposed patch so it stops appearing as pending. Writes nothing to "
             "Notion.",
             _s(patch_id={"type": "string"},
                reason={"type": "string", "_optional": True}),
             bind(_reject_patch), group="act"),

        # -- memory --
        Tool("remember",
             "Store a durable preference or fact about how the user wants you to behave, "
             "e.g. 'never rename my pages' or 'arXiv links are dense, read them closely'. "
             "Use a short stable key so it updates rather than duplicates.",
             _s(fact={"type": "string"}, key={"type": "string"},
                kind={"type": "string", "_optional": True}),
             bind(_remember), group="memory"),
        Tool("recall",
             "Recall stored preferences and facts. Optionally filter by a query.",
             _s(query={"type": "string", "_optional": True}),
             bind(_recall), group="memory"),
    ]
