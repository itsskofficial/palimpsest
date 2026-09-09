# 1. The atom is a claim, not a document

**Status:** Accepted
**Date:** 2026-09-09

## Context

Everyone who builds this builds "embed the new text, find similar pages, ask a model to
rewrite them". That tool gets abandoned in a week, because rewriting a page is an
unbounded operation and you cannot review unbounded operations.

Framing it as "update this page or make a new one" makes it worse. That binary has no
vocabulary for the interesting cases, and one of its two answers — make a new page — is
always available and always locally defensible. Take it fifty times and the base is four
versions of the same topic, none authoritative. That pile is the symptom the project
exists to fix, so the design cannot contain the move that produces it.

## Decision

The unit that moves through the pipeline is a single atomic assertion with a source
anchor. `relate.py` asks how that claim relates to what exists and answers from a closed
set of seven — `new`, `corroborates`, `refines`, `supersedes`, `duplicate`, `extends`,
`contradicts` — and `plan.py` maps each to a specific typed edit.

The claim is the atom because relations are not properties of paragraphs. "Llama-3-8B
reproduces the effect at 20% at layer 9, matching Anthropic's figure" is three claims,
and they can stand in three different relations to what you already wrote. A pipeline
that treats that sentence as one unit must pick one relation and will be wrong about part
of it.

## Consequences

### What this buys

`corroborates` becomes expressible, and it is why the base stops bloating: most new
information about a topic you already studied is the same claim from a different source,
and here it produces one citation and no prose. `duplicate` becomes structurally
incapable of re-adding text. `contradicts` gets a name and a route instead of being
quietly filed as `new`. And disagreeing with a relation is a specific, comprehensible
act; "the model wants to rewrite this page" is not.

### What this costs

More model calls, and an extraction step that can fail — a claim whose verbatim quote
cannot be located in the source is discarded, so recall drops whenever the model
paraphrases while quoting. Extraction quality is now the ceiling on everything
downstream, and per-relation precision has not been measured against hand-labelled real
notes yet.

The classifier ladder pays down the call cost: retrieval finding nothing is `new` by
construction, high token overlap is `corroborates`, and the model sees only the ambiguous
middle, once per claim with all candidates in a cached prefix.

## Revisit if

Extraction proves the dominant error source once the golden set exists. The fix would be
a better extractor, not a coarser atom — but if a passage-level relation ever measured
better than a claim-level one, that number would be the argument.
