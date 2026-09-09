# 9. BM25 over a vector database

**Status:** Accepted, extended 2026-09-09 (see the foot of this file)
**Date:** 2026-09-09

## Context

Retrieval feeds the classifier, and `retrieve.py` asks the mirror two separate questions,
because collapsing them is the most common way a system like this picks the wrong page.
*Does this already exist?* is block-level and needs precision — a near-miss produces a
false `duplicate`. *Where does this belong?* is page-level and needs recall.

The assumed answer is a vector database: embed the blocks, embed the query, retrieve by
cosine. Being the assumed answer is why it deserves an argued alternative.

## Decision

The index is BM25 over unigrams and bigrams, in pure Python, rebuilt from the store on
demand. Dense embeddings are optional and *blended* — `_dense` scores only the candidates
BM25 surfaced, and the two are combined 0.65/0.35. They never substitute.

Three reasons, in order of weight. **It keeps the offline core dependency-free**:
embeddings by default would make an API key mandatory for candidate generation and both
sweeps, which destroys the day-one property (ADR 4). That is not a performance argument;
it is the product. **BM25 is genuinely strong here**, because matching a claim against
notes compares text that already shares vocabulary. **The corpus is small** — thousands
of blocks, not millions. The index builds in milliseconds, which is also why it is not
persisted: a persisted index can go silently stale relative to the notes it describes.

## Consequences

### What this buys

Every sweep, and candidate generation itself, runs with no key and no network. Retrieval
is milliseconds against local SQLite, and there is no index to build, host, migrate or
keep in sync with the mirror.

### What this costs

BM25 cannot match what does not share words. You wrote "the CKA/RSA stuff" and the source
says "representational similarity analysis"; retrieval finds nothing, the claim is `new`
by construction, and you get a duplicate paragraph on a page that already covered it. That
failure is invisible — it looks like the system working.

The gap is widest in the *agent*, not the pipeline. Matching a claim compares source text
to note text, which share vocabulary. Asking "what do I know about attention scaling?"
does not.

Bigrams cost something too: in the duplicate sweep they had to be blended 0.7/0.3 against
unigrams rather than sharing a vector, because every bigram is rare and therefore
high-IDF, and letting them dominate the norm drags an obvious duplicate below any
sensible threshold. Tuning nobody else maintains.

## Revisit if

**This is the decision most likely to be revisited**, and the trigger is a number rather
than a hunch: retrieval recall on conversational agent queries, measured against the
golden set `palimpsest eval bootstrap` builds from your accept/reject history. If recall
there is the bottleneck, blend dense retrieval into the agent's `search_notes` path only
— where a key is already assumed — and leave the sweeps and candidate generation lexical.
Replacing BM25 outright would mean withdrawing the offline claim, which is a far larger
decision than a retrieval change.


---

## Extension — hybrid retrieval (2026-09-09)

The revisit condition fired, from the direction predicted: a note reading "the CKA/RSA
stuff" and a source saying "representational similarity analysis" share no token, so BM25
cannot rank that note at all — not badly, *at all*. The claim is filed as `new`, a second
page about the same topic appears, and the tool meant to stop fragmenting has caused it.

BM25 was not replaced. `Index` takes an optional embedder; with none — the default, and
the only state that needs no key — retrieval is exactly what this ADR describes, and the
sweeps still run offline. With one, the recall query scores *every* block by cosine and
blends it with the lexical score, because a note sharing no vocabulary with the claim
cannot be re-ranked into view: it has to be found in the first place.

No vector database. Vectors are float32 blobs in the existing SQLite table, keyed by
block, text hash and model, and cosine over a few thousand of them is milliseconds of
pure Python. The operational cost this ADR refused — a second datastore to run, back up
and keep consistent with the mirror — is still refused.

The eval measures the gap rather than asserting it: two fixture cases are unreachable
lexically, scoring 0% with no embedder and 100% with one.
