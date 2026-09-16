# 16. A claim with no home gets a new page, never the closest unrelated one

**Status:** Accepted
**Date:** 2026-09-16

## Context

When the classifier judges a claim `new`, it can name the page the claim belongs on, or
name none. Three places overruled "none":

- The no-model shortcut, used when retrieval finds no block at all, took the top
  page-level search hit.
- The classifier prompt said a target page was required for `new`.
- A repair step filled in the top page hit whenever a judgement had no page.

Page-level search always returns something. For a claim about a subject the notes have
never covered, the top hit is whichever page happens to share a few words with it.

This was invisible until the knowledge base met a source about something new. A
ten-video playlist on neural networks, sent to notes about attention, sleep and gradient
clipping, was judged correctly: every claim `new`, every rationale saying nothing in the
notes covered it. Then 52 bullets about handwritten-digit recognition were appended to
*Gradient clipping*, with the rest spread across the index, the project log and the
reading queue.

A vault had a fourth problem. Creating a page needed a Notion root page to create it
under, and a vault has none.

## Decision

A `new` claim keeps `target_page_id = null` unless the page is genuinely about the claim's
subject. The shortcut no longer guesses, the prompt allows null and tells the model not to
pick the closest of several unrelated pages, and the repair step backfills a page only for
`extends`, which names another page by definition.

A claim with no page becomes a page. The planner already folds every page one source would
create into a single page, and the composer writes that page from the claims together. A
vault creates it at its top level, through a `VAULT_ROOT` parent that the markdown
workspace treats as "no parent".

The composed page keeps its citations. Each block ends with linked markers for the moments
its claims came from (`[0:37]` on a video), and the page closes with a line naming the
source.

## Rejected

**A relevance threshold on page-level search.** It would need tuning per knowledge base,
and a score cannot tell "shares vocabulary" from "is about". The classifier already makes
that judgement and says so in its rationale; the fault was overruling it.

**One page per claim.** Fragmentation is the failure this product exists to stop. An
earlier version produced six one-sentence pages from one article.

**Holding homeless claims for review.** A source about a new subject is the ordinary case,
not an exceptional one. Asking about every claim in a playlist would make capture useless.

## Consequences

A source about a new subject produces one well-structured page per source. In the live
run, the first two videos of the playlist became *Neural Networks: Structure and
Notation* and *Gradient Descent and How Networks Learn*, and no existing page was touched.

Later sources on the same subject find that page through retrieval, because applying a
patch refreshes the mirror, so they extend it rather than duplicate it. Videos running at
the same moment on different workers cannot see each other's pages yet, so a playlist
processed in parallel can produce one page per chapter. For a course that usually matches
how it is taught; merging is what the `duplicate` sweep is for.

On Notion, a claim with no page and no configured root page is still skipped and recorded
rather than written anywhere, because there is nowhere safe to put it.
