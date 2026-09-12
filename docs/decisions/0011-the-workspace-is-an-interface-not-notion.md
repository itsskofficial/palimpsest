# 11. The workspace is an interface, not Notion

**Status:** Accepted
**Date:** 2026-09-12

## Context

This was built as a Notion tool, and Notion is still the product. But three separate
problems kept arriving at the same answer.

**Nobody could try it.** To see the central claim work — a new fact read, related to what
you already wrote, turned into a reversible cited edit — you had to create a Notion
integration, share a page with it, buy an API key, and then hand a stranger's agent write
access to your real notes. That is a great deal to ask of somebody deciding whether a
project is worth ten minutes, and it meant almost nobody found out whether it worked.

**The write path was barely tested.** Ingest, classify and plan are pure functions and
were well covered. Apply and undo were not, because the only honest test of a write was
against a live workspace, which needs a token and network and leaves debris. So the most
dangerous code in the project had the weakest offline coverage — and the bugs found there
over the last week (a stale mirror after undo, blocks that never retired, a cascade that
left an undo stuck halfway) were all found by hand, live, one at a time.

**A knowledge base outlives its tools.** Notes fed for years should not be trapped in one
vendor's database, and "export to markdown" is not the same promise as "runs on markdown".

The thing that made this tractable is that the project already had exactly one write door
(ADR 3). Counting what the pipeline actually calls on a Notion client gave **seventeen
methods**. That is the entire surface area of "somewhere a knowledge base can live".

## Decision

`Workspace` is a protocol of those seventeen methods, and there are two implementations:
`NotionClient` and `MarkdownWorkspace`. `palimpsest.workspace.open(settings)` returns one,
keyed on `PALIMPSEST_BACKEND`. Nothing above the write door knows which it has.

The protocol lives in `palimpsest/notion/protocol.py` rather than beside the factory,
because its *consumers* live there — the mirror reads through it, the applier writes
through it, the journal logs through it — and the layering contract forbids `notion` from
importing upward anyway.

`MarkdownWorkspace` stores pages as `.md` files with a small YAML header and speaks
Notion's block JSON at the boundary. Block ids, which a file does not have, live in a
sidecar under `.palimpsest/` and are re-aligned against the file on every read: exact text
first, then same-type-nearest-position.

## Consequences

### What this buys

`palimpsest demo` exists at all — the whole product on a sample vault, no account, no key,
nothing of the visitor's writable.

The write path became testable offline. Ingest → classify → plan → apply → undo now runs
end to end in CI with no network, verified against a local Ollama on the day it landed.

An Obsidian vault is a first-class home for this, which is a different and larger audience
than Notion's, and a fully local configuration (markdown + Ollama + local embeddings)
never sends a byte anywhere.

### What this costs

**Two backends to keep honest.** The mitigation is that the markdown tests are mostly
*parity* tests: a restored block comes back at the end of the page, archiving an already
archived block fails with the word "archived", appending reports nested descendants. Each
is a real Notion quirk the inverse logic was written around, and a backend that quietly
did the nicer thing would let those bugs back into the Notion path with CI still green.
Writing those tests caught one immediately.

**Block identity is genuinely hard.** The alignment heuristic is good — it survives an
edit made in Obsidian, and it survives reordering — but it is a heuristic. A page rewritten
wholesale outside the tool will renumber, and the provenance pointing at those blocks is
lost. Notion does not have this problem.

**Fidelity has a ceiling.** Databases, synced blocks and Notion's more exotic embeds have
no markdown equivalent and degrade to readable text. A vault is a vault.

### What would overturn it

If keeping the two backends in step ever starts costing more than the demo and the offline
tests are worth — concretely, if a Notion bug ships because it was masked by a markdown
test — the markdown backend should shrink to demo-and-test-only rather than being sold as
a place to keep real notes.
