# Design decisions

What was chosen, what was rejected, and why. Written for the reader who wants to judge
the engineering rather than take the README's word for it.

---

## 1. The atom is a claim, not a document

**Decision.** The unit that moves through the pipeline is a single atomic assertion with
a source anchor, not a chunk of text or a page.

**Rejected: chunk-level processing.** "Llama-3-8B reproduces the effect at 20% at layer
9, matching Anthropic's figure" is three claims — a reproduction result, a layer, and a
correspondence — and they can have three *different* relationships to what you already
wrote. A pipeline that treats that sentence as one unit must pick one relation for it,
and will pick the wrong one for at least a third of the content.

**Cost.** More model calls, and an extraction step that can fail. Both are paid down by
§4 (the classifier ladder) and §3 (the anchoring contract).

---

## 2. Seven relations, not "update or create"

**Decision.** The classifier answers *how does this claim relate to what exists*, from a
closed set of seven. Each relation implies a different edit.

**Rejected: a binary decision.** "Should I update this page or make a new one" has no
vocabulary for the interesting cases, and the escape hatch — make a new page — is always
available and always defensible. That is precisely the failure being fixed: fifty
reasonable local decisions produce a pile.

Three of the seven earn their place individually:

- **`corroborates`** is why the base stops growing. Most new information about a topic
  you already studied *is* the same claim from a different source. It produces one
  citation and zero prose. Without this relation the system is an append tool with extra
  steps.
- **`duplicate`** is the direct fix for the reported symptom. Its edit is a merge or a
  link, and it is structurally incapable of adding the text again.
- **`contradicts`** exists so that disagreement has a name and a route. Without it, a
  conflicting claim is classified as `new` or `refines` and quietly corrupts the page.

**Consequence.** A reviewer can disagree with a *relation*, which is a specific,
comprehensible thing. "The model wants to rewrite this page" is not.

---

## 3. Claims must carry a verbatim quote, or they are discarded

**Decision.** The extractor returns the exact span it drew each claim from. We locate
that span in the source ourselves and compute the anchor from the offsets. A claim whose
quote cannot be found is counted and dropped.

**Rejected: asking the model for character offsets.** Models are poor at counting
characters, and a fabricated offset produces a citation pointing at the wrong sentence —
worse than no citation, because it is confidently wrong.

**Rejected: accepting unanchored claims.** A claim with no verifiable origin is exactly
what this project exists to keep out of your notes. Silence would make the failure
invisible; `ExtractionResult.unanchored` makes it a number you can watch.

**Consequence.** Recall drops slightly when the model paraphrases while quoting. The
locator tolerates reflowed whitespace (models reformat when quoting PDFs) but nothing
looser — a fuzzy match that succeeds on a paraphrase defeats the entire point.

---

## 4. A ladder: cheap checks first, the model on the ambiguous middle

**Decision.** Two deterministic shortcuts run before any model call — retrieval finding
nothing means `new` by construction; a candidate with ≥0.90 token overlap means
`corroborates`. Everything else goes to the model.

**Rejected: more heuristics.** It is tempting to add rules for the medium-confidence
cases. That is exactly how a system starts making confident wrong edits, because the
heuristics that are easy to write are the ones that are wrong in the interesting cases.
Two shortcuts, both chosen because getting them wrong is nearly impossible.

**Rejected: one model call per (claim × candidate).** Quadratic, and it removes the
model's ability to *compare* candidates — which is exactly what distinguishes
`duplicate` from `extends`. One call per claim, all candidates in the prompt, candidates
in the cached prefix.

---

## 5. Patches are typed operations with pre-computed inverses

**Decision.** An edit is an ordered list of small typed operations
(`add_citation`, `update_text`, `insert_footnote`, `strike_block`, …). Each carries its
inverse, and **the inverse is written before the operation runs**.

**Rejected: free-form page rewriting.** An operation you cannot render as a diff is one
you cannot review, and an unreviewable operation on your own notes is one you will
eventually stop trusting. There is deliberately no `rewrite_page` in the vocabulary.

**Why inverse-first.** Notion has no transactions. A patch can be interrupted between
operations five and six — process killed, network dropped, rate limit exhausted.
Computing undo *first* means a half-applied patch is still exactly reversible;
computing it after means a crash leaves changes you cannot take back. The ordering is
the entire difference between "a bad afternoon" and "a lost page".

**Consequence.** `Patch.reverse()` includes only operations that actually ran, in
reverse order, so a `partial` patch inverts to precisely the part that landed.

---

## 6. Nothing hard-deletes

**Decision.** `supersedes` strikes text through and leaves it visible. `archive_block`
uses Notion's restorable trash. There is no code path in this project that destroys
content.

**Rejected: deleting superseded text.** The palimpsest metaphor is not decoration — the
old layer staying readable is what lets you audit an edit six months later and see that
the price *was* right when you wrote it.

---

## 7. Contradictions are never automatic, and that is not a setting

**Decision.** `Relation.CONTRADICTS.auto_appliable` is `False` unconditionally. The
autonomy enum has no `high` value. The planner emits no operation for a contradiction.
The apply route rejects one if it somehow appears.

**Rejected: a confidence threshold for contradictions.** Precision on the other six
relations is *earned* by measurement against your accept/reject history. This one is a
policy, and policies do not have thresholds. A knowledge base that silently replaces a
true claim with a false one is strictly worse than no automation, because it destroys
your ability to trust the parts that are still right.

**Three locks on the same door** (enum, planner, apply route) is deliberate redundancy.
The cost is a few lines; the failure it prevents is unrecoverable.

---

## 8. Two independent switches for permission to write

**Decision.** `PALIMPSEST_APPLY` (off) and `PALIMPSEST_AUTONOMY` (`none`). `may_auto_apply`
requires both. In Terraform, `apply` is plain task-definition configuration rather than
a secret.

**Why two.** One switch means one accident. Someone raising autonomy to experiment
should not thereby grant write access, and someone enabling writes for a manual review
session should not thereby enable automation.

**Why plain config in Terraform.** Turning on writes should be a visible change in a
plan that a human reads, not a value edited inside a secret nobody looks at.

---

## 9. A local mirror, not a cache

**Decision.** The full Notion workspace — pages, blocks, backlinks, page roles — is
synced into SQLite/Postgres, and a ledger of every applied operation is kept beside it.

**Why it is unavoidable.** Notion's API has no diff primitive, no transactions, and no
usable version history. Exact undo, "which source produced this sentence", and time
travel are therefore properties of *our* ledger. There is no version of this product
that works without it.

**Consequence.** A first sync of a large workspace is thousands of requests at Notion's
~3/second limit — minutes, not seconds. Incremental sync on `last_edited_time` is what
makes it routine afterwards.

---

## 10. Page *roles* change what an edit looks like

**Decision.** Each page is profiled as hub / deep-dive / literature-note / scratchpad /
project-log / reference, and the planner uses it: a hub gets a **link**, a deep-dive gets
a paragraph.

**Why.** Notion does not know a page's role, and appending prose to an index page is
exactly how hubs slowly turn into essays nobody reads. The profile is heuristic and
cheap (structural tells are strong: a hub is mostly links and little prose), with the
model reserved for the unsure cases.

---

## 11. BM25 by default; embeddings are an addition, not a replacement

**Decision.** Retrieval is BM25 over unigrams and bigrams, in pure Python, rebuilt from
the store on demand. Dense embeddings are optional and *blended*, never substituted.

**Why not embeddings by default.** They would make an API key mandatory for the
duplicate sweep, the contradiction sweep and candidate generation — which would destroy
the property that the product does something useful on day one before you have given it
any key. BM25 is also genuinely strong on the vocabulary-matching this needs, and a
personal knowledge base is thousands of blocks, not millions.

**Rejected: persisting the index.** It builds in milliseconds and a persisted index is
one more thing that can go silently stale relative to the notes it describes.

---

## 12. BM25 ranks; a separate symmetric measure scores similarity

**Decision.** `near_duplicates` uses BM25 only to *shortlist*, then scores the shortlist
with an IDF-weighted cosine over unigrams and bigrams, blended 0.7/0.3.

**Why, concretely.** Using the BM25 score as the similarity is the intuitive
implementation and it silently finds nothing. BM25's IDF is *negative-going* for terms
that appear in most documents — correct for ranking, exactly backwards for duplicate
detection, where shared vocabulary is the whole signal. Measured on the fixture corpus:
an exact copy scores 1.0, a genuine paraphrase 0.31, unrelated text 0.0.

**Why the 0.7/0.3 blend.** Two paraphrases share most of their vocabulary and almost
none of their word pairs. Put bigrams in the same vector and they dominate the norm
(every bigram is rare, so every bigram has high IDF) and drag an obvious duplicate below
any sensible threshold. Bigrams still earn their place — they stop "learning rate"
matching a page about learning.

**Threshold 0.32, deliberately permissive.** A "safe" 0.5 reports only copy-pastes and
misses the case that motivates the sweep: the same idea written twice, months apart, in
different words. Results are ranked, so a low bar costs a longer tail, not a worse top.

---

## 13. Anchors are part of the ingestion contract

**Decision.** Every adapter emits *segments* — spans of normalised text with a citable
locator. PDFs anchor to a page, YouTube to a timestamp (and the footnote deep-links to
that second), spreadsheets to a cell range, web pages to a cumulative heading path.

**Why it is a contract and not a nicety.** A citation that says "source: that YouTube
video" is decoration. One that opens the video at 14:22 is why you will still trust the
base in two years. Making it an adapter obligation means a new adapter cannot quietly
ship without one.

**Paired with:** archiving the original bytes on the way in, so the citation resolves
after the source 404s.

---

## 14. The default install has no dependencies

**Decision.** `dependencies = []`. Claude, FastAPI, psycopg2, pypdf, openpyxl are all
extras.

**Why.** It is the enforcement mechanism for §11 and for the day-one story. The
`offline` CI job imports the package with no extras and runs a duplicate sweep; if that
job ever needs an install, the claim that the decision layer runs with no keys has
quietly stopped being true.

**Four import-linter contracts** back this up, including one that forbids every module
except `notion/apply.py` from importing the write path. `store/stats.py` exists solely
so the SQLite store does not acquire a static import path to psycopg2 through
`store/base.py` — the contract is true rather than exempted.

---

## 15. Claude specifics

- **Structured outputs** (`output_config.format`) on every call. Claim extraction and
  relation classification are both "produce this exact shape" problems, and parsing free
  text into them is a source of silent corruption.
- **Prompt caching on the stable prefix.** The candidate set is identical across every
  claim from one source; the claim itself is not. Candidates go in the cached system
  block, the claim in the user turn. Reversing that caches nothing.
- **Effort per job.** Extraction is mechanical (`medium`); relation classification is
  the judgment the product rests on (`high`).
- **Refusals handled, fallbacks on.** Claude Opus 5 can decline with HTTP 200 and
  `stop_reason: "refusal"`; code that reads `content[0]` breaks on that. Server-side
  fallbacks are opted into and degrade gracefully if the beta is unavailable.

---

## 16. Notion API version is pinned

**Decision.** `Notion-Version: 2026-03-11`, as a constant with a settings override.

**Why.** Notion versions by date, and the `2025-09-03` release split databases into
*data sources* — `search` stopped accepting `value: "database"` in favour of
`value: "data_source"`. A floating version changes response shapes under a running
deployment. The client also paces itself under Notion's ~3 req/s limit with a token
bucket and honours `Retry-After`, because the alternative is discovering the limit
during your first full sync.

---

## Known limitations

- **Extraction quality is the ceiling.** Bad claims poison everything downstream. There
  is no per-relation precision measurement yet against a hand-labelled set of real
  notes — that is the first thing in [NEXT_STEPS.md](NEXT_STEPS.md).
- **The autonomy ladder does not yet learn.** The design is that accept/reject history
  raises autonomy per relation once measured precision clears a bar. Today autonomy is
  set by hand.
- **Page roles are heuristic.** The model is not consulted even where the heuristic is
  unsure; that hook exists but is not wired.
- **The classifier is single-shot.** No self-consistency, no second opinion on
  medium-confidence judgements.
- **Merging is a link, not a merge.** `duplicate` links the pages; it does not move
  content and reconcile the two. Doing that properly needs the review UI to show both
  pages side by side.
