# 5. Every operation carries its own inverse

**Status:** Accepted
**Date:** 2026-09-09

## Context

Notion has no transactions. A patch is an ordered list of writes performed one at a time
over HTTP, and it can be interrupted between operations five and six — process killed,
network dropped, rate limit exhausted. Notion also has no diff primitive and no usable
version history, so there is nothing on its side to roll back to.

Compute undo after the fact and a crash at operation six leaves five changes with no
recorded way back: the `rich_text` that operation three overwrote only ever existed in
Notion.

The related failure is at the vocabulary level: an operation that cannot be rendered as a
diff cannot be inverted either, for the same reason it cannot be reviewed.

## Decision

`OpKind` is closed — `append_block`, `update_text`, `add_citation`, `insert_footnote`,
`strike_block`, `archive_block`, `create_page`, `link_pages`, and the three structural
ones. There is no `rewrite_page`, no `set_content`, no free-form mutation.

`Operation.inverse` is computed *before* the operation runs and stored with it.
`apply_patch` does this first in the loop, before the `dry_run` check and before
`_execute`, so a crash at any point after it is still reversible.

`archive_block` rather than `delete_block` is the same rule: Notion's API can delete and
this project never does. `Patch.reverse()` includes only operations that actually ran, in
reverse order, so a `partial` patch inverts to precisely the part that landed. And an
operation whose inverse cannot be built is refused: for the kinds in `_INVERSE_REQUIRED`,
a `None` inverse makes the applier record the failure, set status `partial`, and break.

## Consequences

### What this buys

`palimpsest undo` is exact rather than best-effort, including for a patch that failed
halfway: the applier stops at the first failure, so the worst outcome is a fully recorded,
fully reversible prefix of a patch you approved. The closed vocabulary is also what makes
the review UI possible — a proposal renders the new sentence over the struck-through line
it replaces, because the operation says exactly which line.

### What this costs

Some things this project would otherwise do, it refuses to do. Notion's move endpoint
takes a page or data source as destination and has no workspace variant, so a page at the
top level can be filed into a hub and never moved back out through the API. That has no
inverse, so the applier refuses it and the organiser surfaces the page as a review item
explaining the one-way door. A user who wanted the move does not get it.

Every new operation kind must also ship with its inverse builder, and inverse construction
reads from the mirror — so an operation against a block the mirror has not seen fails
rather than proceeding optimistically.

## Revisit if

Notion gains transactions or a usable version API. Inverse-first exists because there is
nothing on their side to roll back to; if there were, the inverse could become a
verification rather than a prerequisite.
