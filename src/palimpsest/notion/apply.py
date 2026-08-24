"""The only code path in this project that writes to Notion.

That is enforced, not merely intended: an import-linter contract in `pyproject.toml`
forbids every other module from importing this one. The whole safety story — every
change reversible, every change provenanced — depends on there being exactly one door.

The rules this module keeps, in order of how badly it goes when they are broken:

1. **Write the inverse before the operation.** Notion has no transactions, so a patch
   can be interrupted between operations five and six. Computing undo *first* means a
   half-applied patch is still exactly reversible; computing it after means a crash
   leaves changes you cannot take back.
2. **Never hard-delete.** `STRIKE_BLOCK` marks text struck and grey; `ARCHIVE_BLOCK`
   sends it to Notion's trash, which is restorable from the UI. There is no code path
   here that destroys content.
3. **Record before returning.** Each applied operation is written to the ledger with
   its inverse and its provenance as it happens, not batched at the end. A process
   killed mid-patch leaves a complete record of what it managed to do.
4. **Stop on the first failure.** A patch that fails at operation six leaves ops one
   through five applied and recorded, status `partial`, and `palimpsest undo` will
   revert exactly those five. Ploughing on would produce a page in a state no one
   chose.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from palimpsest.notion import blocks as B
from palimpsest.notion.client import NotionClient, NotionError
from palimpsest.types import Operation, OpKind, Patch

__all__ = ["ApplyResult", "apply_patch", "revert_patch"]

log = logging.getLogger("palimpsest.apply")


@dataclass
class ApplyResult:
    patch_id: str
    applied: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "applied"

    def as_dict(self) -> dict:
        return {"patch_id": self.patch_id, "applied": self.applied, "failed": self.failed,
                "skipped": self.skipped, "errors": self.errors, "status": self.status}

    def summary(self) -> str:  # pragma: no cover - display only
        base = f"{self.applied} applied"
        if self.skipped:
            base += f", {self.skipped} skipped"
        if self.failed:
            base += f", {self.failed} FAILED"
        return f"{base}  ({self.status})"


def _source_row(store, source_id: str) -> dict | None:
    """The source behind a patch, as a plain dict for the journal row."""
    if not source_id:
        return None
    try:
        source = store.get_source(source_id)
        return source.as_dict() if source is not None else None
    except Exception:  # pragma: no cover - the journal is never load-bearing
        return None


def _page_of(store, target: str) -> str | None:
    """Which page an operation touches — for the ledger's per-page history."""
    block = store.get_block(target)
    if block:
        return block.get("page_id")
    page = store.get_page(target)
    return page.get("page_id") if page else None


# ---------------------------------------------------------------------------
# building inverses
# ---------------------------------------------------------------------------


def _build_inverse(store, op: Operation) -> dict | None:
    """The operation that undoes `op`, computed from current state before it runs.

    Returns `None` only for operations whose inverse cannot be known until after they
    run (creating a block or a page — you do not have the new id yet). Those get their
    inverse patched in from the API response, in `_stamp_inverse`.
    """
    if op.kind in (OpKind.APPEND_BLOCK, OpKind.CREATE_PAGE):
        return None  # filled in from the response

    if op.kind in (OpKind.UPDATE_TEXT, OpKind.ADD_CITATION, OpKind.STRIKE_BLOCK):
        block = store.get_block(op.target)
        if block is None:
            return None
        raw = block.get("raw") or {}
        btype = block.get("type", "paragraph")
        candidate = raw.get(btype)
        body: dict = candidate if isinstance(candidate, dict) else {}
        return {
            "kind": OpKind.UPDATE_TEXT.value,
            "target": op.target,
            "payload": {
                "block_type": btype,
                # Restore the exact rich_text array, not a re-rendered plain string:
                # re-rendering would silently drop bold, links and inline code.
                "rich_text": list(body.get("rich_text") or []),
                "text": block.get("text", ""),
            },
        }

    if op.kind == OpKind.ARCHIVE_BLOCK:
        return {"kind": "restore_block", "target": op.target, "payload": {}}

    # Structural operations invert to the page's current filing, which the mirror
    # already holds. A page missing from the mirror gets no inverse and is therefore
    # refused below rather than applied irreversibly.
    if op.kind is OpKind.MOVE_PAGE:
        page = store.get_page(op.target)
        if page is None:
            return None
        parent, kind = page.get("parent_id"), page.get("parent_kind")
        # Notion's move endpoint takes a page or a data source, never the workspace —
        # so a page sitting at the top level can be filed but not put back. Refusing to
        # compute an inverse here is what makes the applier refuse the move.
        if not parent or kind not in ("page_id", "page", None):
            return None
        return {"kind": OpKind.MOVE_PAGE.value, "target": op.target,
                "payload": {"parent_page_id": parent}}

    if op.kind is OpKind.RENAME_PAGE:
        page = store.get_page(op.target)
        if page is None:
            return None
        return {"kind": OpKind.RENAME_PAGE.value, "target": op.target,
                "payload": {"title": page.get("title", "")}}

    if op.kind is OpKind.SET_ICON:
        page = store.get_page(op.target)
        if page is None:
            return None
        # A page with no icon inverts to clearing the icon, not to leaving it — hence
        # an explicit `None` rather than an absent key.
        return {"kind": OpKind.SET_ICON.value, "target": op.target,
                "payload": {"icon": page.get("icon")}}

    if op.kind == OpKind.LINK_PAGES:
        return None
    return None


#: Operations that must not run without an inverse. For everything else a missing
#: inverse means "the response will supply it"; for these it means the mirror does not
#: know the page's current state, and applying would produce a change nobody can undo.
_INVERSE_REQUIRED = (OpKind.MOVE_PAGE, OpKind.RENAME_PAGE, OpKind.SET_ICON)


#: Operations that create blocks, and therefore only learn their inverse from the
#: response. All three go through `append_children`, so all three invert the same way —
#: missing any one of them leaves blocks that `undo` silently cannot remove.
_CREATES_BLOCKS = (OpKind.APPEND_BLOCK, OpKind.INSERT_FOOTNOTE, OpKind.LINK_PAGES)


def _stamp_inverse(op: Operation, response: dict) -> None:
    """Fill in the inverse for operations whose target did not exist beforehand."""
    if op.kind in _CREATES_BLOCKS:
        created = response.get("results") or []
        ids = [(b.get("id") or "").replace("-", "") for b in created if b.get("id")]
        op.result = {"created_block_ids": ids}
        if ids:
            # Undo of "append N blocks" is "archive those N blocks".
            op.inverse = {"kind": "archive_blocks", "target": op.target,
                          "payload": {"block_ids": ids}}
    elif op.kind is OpKind.CREATE_PAGE:
        pid = (response.get("id") or "").replace("-", "")
        op.result = {"page_id": pid, "url": response.get("url")}
        if pid:
            op.inverse = {"kind": "archive_page", "target": pid, "payload": {}}


# ---------------------------------------------------------------------------
# executing one operation
# ---------------------------------------------------------------------------


#: A payload value of `{"from_op": "op_…", "key": "page_id"}` means "the id of the page
#: that operation created". It exists because an organise patch both creates a hub and
#: moves pages into it, and the hub's id is not known until the create has run.
#:
#: The alternative — apply the creates, re-sync, then plan the moves — would split one
#: decision the reviewer made across two approvals, and the second half would be
#: reviewed against a workspace that had already changed under it.
REF_KEY = "from_op"


def _resolve_refs(patch: Patch, op: Operation) -> dict:
    """Replace deferred references in a payload with ids from already-applied ops.

    Returns a copy; `op.payload` keeps the reference so the patch stays re-appliable
    and the review UI can still say "into the hub created above" rather than showing a
    raw id the reviewer has never seen.
    """
    payload = dict(op.payload)
    for field_name, value in list(payload.items()):
        if not (isinstance(value, dict) and REF_KEY in value):
            continue
        ref_id = value[REF_KEY]
        source = next((o for o in patch.operations if o.op_id == ref_id), None)
        resolved = (source.result or {}).get(value.get("key", "page_id")) if source else None
        if not resolved:
            raise ValueError(
                f"{field_name} refers to operation {ref_id}, which has not produced "
                "an id — it either failed or has not run yet")
        payload[field_name] = resolved
    return payload


def _execute(client: NotionClient, store, op: Operation, patch: Patch | None = None) -> dict:
    """Perform one operation against Notion and return the raw response."""
    p = _resolve_refs(patch, op) if patch is not None else op.payload

    if op.kind is OpKind.APPEND_BLOCK:
        children = p.get("children")
        if not children:
            children = [B.make_block(p.get("block_type", "paragraph"), p.get("text", ""))]
        # Notion caps a single append at 100 children; chunk rather than fail.
        first: dict = {}
        results: list[dict] = []
        for i in range(0, len(children), B.MAX_TEXT and 100):
            chunk = children[i:i + 100]
            resp = client.append_children(op.target, chunk,
                                          after_block_id=p.get("after_block_id"))
            results.extend(resp.get("results") or [])
            first = first or resp
        return {"results": results}

    if op.kind is OpKind.UPDATE_TEXT:
        block = store.get_block(op.target)
        btype = p.get("block_type") or (block.get("type") if block else "paragraph")
        if "rich_text" in p:
            return client.update_block(op.target, {btype: {"rich_text": p["rich_text"]}})
        return client.update_block(op.target, B.text_payload(btype, p.get("text", "")))

    if op.kind is OpKind.ADD_CITATION:
        block = store.get_block(op.target)
        if block is None:
            raise NotionError(404, "not_in_mirror",
                              f"block {op.target} is not in the mirror; re-sync first")
        payload = B.append_runs(block.get("raw") or {"type": block.get("type")},
                                B.citation_marker(p.get("label", "src"), p.get("url")))
        return client.update_block(op.target, payload)

    if op.kind is OpKind.INSERT_FOOTNOTE:
        note = B.footnote_block(p.get("text", ""), p.get("source_title", ""),
                                p.get("locator"), p.get("url"),
                                why=p.get("rationale"))
        parent = p.get("parent_page_id") or (store.get_block(op.target) or {}).get("page_id")
        if not parent:
            raise NotionError(400, "no_parent", f"cannot place a footnote for {op.target}")
        return client.append_children(parent, [note], after_block_id=op.target)

    if op.kind is OpKind.STRIKE_BLOCK:
        block = store.get_block(op.target)
        btype = (block or {}).get("type", "paragraph")
        text = p.get("text") or (block or {}).get("text", "")
        return client.update_block(op.target, B.strike_payload(btype, text))

    if op.kind is OpKind.ARCHIVE_BLOCK:
        return client.archive_block(op.target)

    if op.kind is OpKind.CREATE_PAGE:
        children = p.get("children") or (
            [B.paragraph(p["text"])] if p.get("text") else None)
        return client.create_page(op.target, p.get("title", "Untitled"), children,
                                  icon=p.get("icon"))

    if op.kind is OpKind.LINK_PAGES:
        target_url = p.get("url") or ""
        label = p.get("label", "related")
        block = B.bullet(f"↔ {label}", link=target_url) if target_url else B.bullet(
            f"↔ {label}")
        return client.append_children(op.target, [block])

    if op.kind is OpKind.MOVE_PAGE:
        parent = p.get("parent_page_id")
        if not parent:
            raise ValueError("a move needs a destination parent page")
        return client.move_page(op.target, parent)

    if op.kind is OpKind.RENAME_PAGE:
        return client.rename_page(op.target, p.get("title", ""))

    if op.kind is OpKind.SET_ICON:
        return client.set_page_icon(op.target, p.get("icon"))

    raise ValueError(f"unhandled operation kind {op.kind}")


def _execute_inverse(client: NotionClient, store, inverse: dict) -> None:
    """Perform an inverse. Inverses have their own small vocabulary."""
    kind = inverse["kind"]
    target = inverse["target"]
    payload = inverse.get("payload", {})

    if kind == OpKind.UPDATE_TEXT.value:
        btype = payload.get("block_type", "paragraph")
        if payload.get("rich_text") is not None:
            client.update_block(target, {btype: {"rich_text": payload["rich_text"]}})
        else:
            client.update_block(target, B.text_payload(btype, payload.get("text", "")))
    elif kind == "archive_blocks":
        for bid in payload.get("block_ids", []):
            client.archive_block(bid)
    elif kind == "archive_page":
        client.archive_page(target)
    elif kind == "restore_block":
        client.restore_block(target)
    elif kind == OpKind.MOVE_PAGE.value:
        client.move_page(target, payload["parent_page_id"])
    elif kind == OpKind.RENAME_PAGE.value:
        client.rename_page(target, payload.get("title", ""))
    elif kind == OpKind.SET_ICON.value:
        client.set_page_icon(target, payload.get("icon"))
    else:  # pragma: no cover - defensive
        raise ValueError(f"unhandled inverse kind {kind!r}")


# ---------------------------------------------------------------------------
# the public surface
# ---------------------------------------------------------------------------


def apply_patch(client: NotionClient, store, patch: Patch, *,
                dry_run: bool = False, reviewer: str | None = None,
                journal=None) -> ApplyResult:
    """Apply a patch to Notion, recording everything needed to undo it.

    `dry_run=True` walks the whole patch, builds every inverse, and writes nothing —
    which is how you find out that a target block has vanished from the mirror without
    discovering it halfway through a real run.
    """
    result = ApplyResult(patch_id=patch.patch_id)

    for op in patch.operations:
        if op.applied:
            result.skipped += 1
            continue

        # (1) Inverse first, always. A crash after this point is still reversible.
        if op.inverse is None:
            op.inverse = _build_inverse(store, op)

        # A structural operation with no inverse is one whose undo does not exist —
        # the page is not in the mirror, or it sits at the workspace top level, which
        # Notion's move endpoint cannot restore it to. Refusing is the whole point:
        # "every edit is exactly reversible" has to be enforced somewhere, and the
        # applier is the last place it can be.
        if op.kind in _INVERSE_REQUIRED and op.inverse is None:
            result.failed += 1
            result.errors.append(
                f"{op.kind.value} {op.target[:8]}: refused — no inverse could be "
                "computed. Either the page is not in the mirror (run `palimpsest "
                "sync`), or it is a top-level page and Notion's API cannot move one "
                "back to the workspace root.")
            result.status = "partial"
            log.error("refusing irreversible %s on %s", op.kind.value, op.target)
            break

        if dry_run:
            result.applied += 1
            continue

        try:
            response = _execute(client, store, op, patch)
            _stamp_inverse(op, response)
            op.applied_at = time.time()
            result.applied += 1

            page_id = _page_of(store, op.target) or (
                op.result or {}).get("page_id") or op.target
            store.record_applied_op(patch.patch_id, op, page_id)

            # (3) Provenance as it happens, not batched at the end.
            if op.claim_id:
                created = (op.result or {}).get("created_block_ids") or []
                for bid in (created or [op.target]):
                    store.put_provenance([{
                        "block_id": bid,
                        "page_id": page_id,
                        "source_id": patch.source_id,
                        "claim_id": op.claim_id,
                        "relation": op.relation.value if op.relation else None,
                        "anchor": op.payload.get("anchor"),
                        "op_id": op.op_id,
                    }])

            # The same record, in Notion, where you will actually be when you ask why a
            # sentence says what it says. Best-effort by construction: `record_change`
            # swallows its own failures, because losing a log line must never be a
            # reason to stop editing.
            if journal is not None:
                journal.record_change(
                    op, patch_id=patch.patch_id, page=store.get_page(page_id),
                    source=_source_row(store, patch.source_id), reviewer=reviewer)
        except (NotionError, ValueError) as e:
            # (4) Stop on first failure. `partial` is an honest status.
            result.failed += 1
            result.errors.append(f"{op.kind.value} {op.target[:8]}: {e}")
            result.status = "partial"
            log.error("operation failed, stopping patch %s: %s", patch.patch_id, e)
            break

    if not dry_run:
        if result.failed == 0:
            result.status = "applied"
        patch.status = result.status
        store.put_patch(patch)
        store.set_patch_status(patch.patch_id, result.status, reviewer=reviewer)
    return result


def revert_patch(client: NotionClient, store, patch: Patch,
                 reviewer: str | None = None, journal=None) -> ApplyResult:
    """Undo an applied patch, exactly.

    Operations invert in reverse order and only the ones that actually ran are
    included, so a `partial` patch reverts to precisely the state before it started.
    """
    result = ApplyResult(patch_id=patch.patch_id, status="reverted")
    for op in reversed(patch.operations):
        if not op.applied or not op.inverse:
            result.skipped += 1
            continue
        try:
            _execute_inverse(client, store, op.inverse)
            store.mark_op_reverted(op.op_id)
            # A revert is a change too, and the journal must say so — a row that still
            # reads "Applied" for an edit that was taken back is worse than no row.
            if journal is not None:
                journal.record_change(
                    op, patch_id=patch.patch_id,
                    page=store.get_page(_page_of(store, op.target) or op.target),
                    source=_source_row(store, patch.source_id), reviewer=reviewer,
                    status="Reverted")
            op.applied_at = None
            result.applied += 1
        except (NotionError, ValueError) as e:
            result.failed += 1
            result.errors.append(f"undo {op.kind.value} {op.target[:8]}: {e}")
            result.status = "partial"
            break
    patch.status = result.status
    store.put_patch(patch)
    store.set_patch_status(patch.patch_id, result.status, reviewer=reviewer)
    return result
