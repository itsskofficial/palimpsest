# 9. BM25 over a vector database

**Status:** Accepted
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
