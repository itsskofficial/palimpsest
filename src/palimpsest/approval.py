"""The gate between a proposed patch and Notion.

Everything that would write to your notes — a capture that produced edits, the agent
calling `apply_patch`, an organise proposal — passes through `gate()`. It splits a
patch into what may apply on its own authority and what a human must see, applies the
first half, and files the second as an `approval` a person resolves with a tap.

This is where the product's safety model becomes one function, so there is a single
place to read and a single place to test:

1. **Contradictions never pass.** The planner already refuses to emit one and the
   applier rejects one; this is the third lock, checked before anything else, so an
   operation derived from a contradiction cannot apply no matter which surface asked.
2. **`PALIMPSEST_APPLY` is an absolute veto.** With writes off, *everything* is held —
   which is exactly the propose-only experience: you send something, and you are asked
   to approve every edit before it lands.
3. **Autonomy gates the rest.** An operation applies on its own only when its risk tier
   is within `PALIMPSEST_AUTONOMY`. Everything above the line is held.
4. **Held is split, not stranded.** The obvious citations apply; the one `supersedes`
   waits under its own approval. Holding the many hostage to the one is how a review
   queue becomes something you stop opening.

The planners may not import this module — an import-linter contract forbids it, the same
one that protects `notion/apply.py`. A thing that proposes edits must not be able to
reach the thing that lets them through.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from palimpsest.types import Patch, Relation, new_id

__all__ = ["APPROVAL_TTL_S", "gate", "resolve"]

log = logging.getLogger("palimpsest.approval")

#: How long a held approval stands before it is dropped. A day is long enough to answer
#: after a night's sleep and short enough that a stale approval never applies against a
#: workspace that has moved on.
APPROVAL_TTL_S = 24 * 3600


def _split(patch: Patch, settings: Any) -> tuple[list, list, list]:
    """Partition operations into (blocked, auto, held).

    One authority, `settings.may_auto_apply`, rather than two. This used to consult the
    autonomy setting *and* short-circuit on the relation, so a contradiction was refused
    twice — which was right while no setting could ever permit one, and became a silent
    contradiction of the settings screen the moment `everything` existed. A gate that
    disagrees with the switch the operator just moved is worse than a strict gate.

    `blocked` is now what no *current* setting permits: a contradiction below the top
    rung, which never applies and never even becomes an approval, because "approve this
    conflicting rewrite" is not a question with a good answer — it is surfaced as a
    review item with both sides instead. `auto` is what the setting permits right now.
    `held` is the remainder, waiting for a human.
    """
    blocked, auto, held = [], [], []
    for op in patch.operations:
        permitted = settings.may_auto_apply(op.risk_tier)
        if op.relation is Relation.CONTRADICTS and not permitted:
            blocked.append(op)
        elif permitted and (op.auto_appliable
                            or op.relation is Relation.CONTRADICTS):
            auto.append(op)
        else:
            held.append(op)
    return blocked, auto, held


def gate(store, patch: Patch, settings: Any, *, notion_factory=None,
         journal_factory=None, session_id: str | None = None,
         chat_id: str | None = None, reviewer: str = "auto",
         summary: str | None = None) -> dict:
    """Apply what may apply; hold the rest as an approval.

    A contradiction is written only at `PALIMPSEST_AUTONOMY=everything`, and what is
    written is a record of the disagreement beside the line it argues with — never an
    edit to that line. See `plan._contradiction_ops`.

    Returns a dict describing the outcome: how many applied, how many are held, and the
    `approval_id` a surface can turn into an Approve/Reject control.
    """
    if patch is None or not len(patch):
        return {"applied": 0, "held": 0, "blocked": 0, "reason": "empty patch"}

    blocked, auto, held = _split(patch, settings)
    outcome: dict[str, Any] = {"applied": 0, "held": len(held), "blocked": len(blocked)}

    # -- apply the auto slice, if writing is possible at all --------------------
    if auto and settings.apply and settings.has_workspace and notion_factory is not None:
        applied = _apply_slice(store, patch, auto, notion_factory, journal_factory,
                               reviewer=reviewer)
        outcome.update(applied=applied.get("applied", 0),
                       errors=applied.get("errors", []),
                       status=applied.get("status"))
    elif auto:
        # There were auto-eligible ops but writing is off — they join the held set, so
        # propose-only surfaces the whole patch for approval rather than dropping it.
        held = auto + held
        outcome["held"] = len(held)
        outcome["reason"] = ("PALIMPSEST_APPLY is off" if not settings.apply
                             else settings.no_workspace_reason)

    # -- file the held slice as one approval -----------------------------------
    if held:
        approval_id = new_id("apr_")
        store.put_approval({
            "approval_id": approval_id,
            "patch_id": patch.patch_id,
            "session_id": session_id,
            "chat_id": chat_id,
            "operation_ids": [op.op_id for op in held],
            "kind": "apply",
            "status": "pending",
            "summary": summary or _summarise(held),
            "requested_at": time.time(),
            "expires_at": time.time() + APPROVAL_TTL_S,
        })
        outcome["approval_id"] = approval_id

    if blocked:
        outcome["blocked_note"] = (
            f"{len(blocked)} operation(s) derive from a contradiction and are never "
            "applied automatically — resolve them on the page.")
    return outcome


def resolve(store, approval_id: str, decision: str, *, by: str | None,
            notion_factory=None, journal_factory=None) -> dict:
    """Act on a human decision. `decision` is 'approved' or 'rejected'.

    On approval the held operations are applied — re-checked against the live patch, so
    an operation edited or already applied since the request is handled rather than
    double-run. On rejection nothing is written and the row is closed.
    """
    approval = store.get_approval(approval_id)
    if approval is None:
        return {"ok": False, "error": f"no approval {approval_id}"}
    if approval["status"] != "pending":
        return {"ok": False, "error": f"already {approval['status']}",
                "status": approval["status"]}
    if approval.get("expires_at") and approval["expires_at"] < time.time():
        store.resolve_approval(approval_id, "expired", by)
        return {"ok": False, "error": "this approval has expired", "status": "expired"}

    if decision == "rejected":
        store.resolve_approval(approval_id, "rejected", by)
        return {"ok": True, "status": "rejected", "applied": 0}

    patch = store.get_patch(approval["patch_id"])
    if patch is None:
        store.resolve_approval(approval_id, "gone", by)
        return {"ok": False, "error": "the patch is gone"}

    keep = set(approval.get("operation_ids") or [])
    ops = [op for op in patch.operations if op.op_id in keep and not op.applied]
    if not ops:
        store.resolve_approval(approval_id, "approved", by)
        return {"ok": True, "status": "approved", "applied": 0,
                "note": "nothing left to apply"}

    if notion_factory is None:
        return {"ok": False, "error": "no Notion client to apply with"}

    result = _apply_slice(store, patch, ops, notion_factory, journal_factory,
                          reviewer=by or "human")
    store.resolve_approval(approval_id, "approved", by)
    # `status` last: the apply result carries its own `status` (`applied`/`partial`),
    # and the approval's own outcome — that it was approved — is what the caller asked
    # about. Spreading result first and overriding is the difference between the two.
    return {"ok": True, **result, "status": "approved",
            "apply_status": result.get("status")}


def _apply_slice(store, patch: Patch, ops: list, notion_factory, journal_factory,
                 *, reviewer: str) -> dict:
    """Apply a subset of a patch's operations through the one write door."""
    from palimpsest.notion.apply import apply_patch

    slice_ = Patch(patch_id=patch.patch_id, source_id=patch.source_id,
                   operations=ops, status="proposed", notes=patch.notes)
    client = notion_factory()
    journal = journal_factory() if journal_factory is not None else None
    result = apply_patch(client, store, slice_, reviewer=reviewer, journal=journal)
    # `apply_patch` persists the patch it was handed, and it was handed the slice. Left
    # there, the stored patch is the slice: the held operations vanish, the approval
    # that names them expands to nothing, and approving it applies nothing. The slice's
    # operation objects are the full patch's own, already stamped, so writing the full
    # patch back is the whole fix.
    patch.status = slice_.status
    store.put_patch(patch)
    return result.as_dict()


def _summarise(ops: list) -> str:
    counts: dict[str, int] = {}
    for op in ops:
        key = op.relation.value if op.relation else op.kind.value
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
