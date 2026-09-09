"""Finding the right places in your notes — two different questions, two queries.

Collapsing "does this already exist?" and "where does this belong?" into one search is
the most common way a system like this picks the wrong page. They want different
things:

- **Does this already exist?** is a *block*-level question needing high precision. You
  are looking for one sentence that says nearly the same thing, and a near-miss is
  worse than nothing because it produces a false `DUPLICATE`.
- **Where does this belong?** is a *page*-level question needing recall. You want every
  page that plausibly covers the topic, because the classifier will sort them out.

So there are two retrievers, and the classifier is given both.

**The index is BM25 over the mirror, in pure Python.** Not a compromise: BM25 is
genuinely strong on the vocabulary-matching this needs, a personal knowledge base is
thousands of blocks rather than millions, and building the index from SQLite takes
milliseconds. It also means retrieval — and therefore the duplicate and contradiction
sweeps — work with no API key at all.

Dense embeddings are an optional *addition*, not a replacement: they catch the cases
BM25 structurally cannot, where you wrote "the CKA/RSA stuff" and the source says
"representational similarity analysis". When configured, the two scores are blended.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = ["Candidate", "Index", "PageHit"]

log = logging.getLogger("palimpsest.retrieve")

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-_.]*")

#: Words that carry no discriminating signal in personal notes. Deliberately short:
#: over-aggressive stopping hurts recall on technical vocabulary, where "the transformer"
#: and "transformer" should not be the same query.
STOP = frozenset(["a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "did", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "of", "on", "or", "our", "so", "than", "that", "the", "their", "then", "there", "these", "they", "this", "to", "was", "were", "what", "when", "where", "which", "while", "who", "why", "will", "with", "would", "you", "your"])

K1 = 1.5
B = 0.75

#: How the two signals are weighted once both are on a 0-1 scale. Lexical leads because
#: on technical notes it is usually right and always explainable; the dense term is there
#: to rescue the paraphrase, not to overrule an exact match.
LEXICAL_WEIGHT = 0.65
DENSE_WEIGHT = 0.35

#: Cosine below this counts as "unrelated". Modern embedding models put almost all pairs
#: of English text above 0.6, so scoring the raw cosine adds a near-constant to every
#: block and changes nothing; subtracting the floor is what turns it into a signal.
DENSE_FLOOR = 0.55

#: How many BM25 hits get re-ranked by vectors on the precision path.
DENSE_RERANK = 60

#: Page-level boosts, as a fraction of the best page's own block score rather than as
#: absolute numbers. A page whose *title* matches the claim is strong evidence — often
#: stronger than a body match — but "strong" only means anything relative to the rest of
#: the ranking, and BM25 magnitudes vary by an order of magnitude with corpus size.
TITLE_BOOST = 0.5
TOPIC_BOOST = 0.35

#: What a page inherits from a neighbour it is linked to or from.
BACKLINK_SHARE = 0.15


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, with hyphenated compounds also split.

    `dot-product` yields `dot-product`, `dot` and `product`. Without the split, one
    note writing "dot-product attention" and another writing "dot product attention"
    share almost no vocabulary — which is exactly the pair the duplicate sweep exists
    to catch, and exactly the pair a naive tokenizer misses.
    """
    out: list[str] = []
    for raw in _TOKEN.findall((text or "").lower()):
        if raw in STOP or len(raw) < 2:
            continue
        out.append(raw)
        if "-" in raw or "_" in raw:
            out.extend(p for p in re.split(r"[-_]", raw)
                       if len(p) > 1 and p not in STOP)
    return out


def _bigrams(tokens: list[str]) -> list[str]:
    """Adjacent pairs. `learning_rate` should not match a page about learning."""
    return [f"{a}~{b}" for a, b in itertools.pairwise(tokens)]


@dataclass
class Candidate:
    """One block that might be what a claim is about."""

    block_id: str
    page_id: str
    page_title: str
    text: str
    score: float
    block_type: str = "paragraph"

    def as_dict(self) -> dict:
        return {"block_id": self.block_id, "page_id": self.page_id,
                "page_title": self.page_title, "text": self.text,
                "score": round(self.score, 4), "block_type": self.block_type}


@dataclass
class PageHit:
    """One page that might be where a claim belongs."""

    page_id: str
    title: str
    role: str
    score: float
    url: str | None = None
    matched_blocks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"page_id": self.page_id, "title": self.title, "role": self.role,
                "score": round(self.score, 4), "url": self.url,
                "matched_blocks": self.matched_blocks[:5]}


class Index:
    """A BM25 index over the mirror, built in memory, optionally blended with vectors.

    Rebuilt from the store rather than persisted: for a personal knowledge base it
    takes milliseconds, and a persisted index is one more thing that can silently go
    stale relative to the notes it describes.
    """

    def __init__(self, store, *, embedder=None, min_chars: int = 25):
        self.store = store
        self.embedder = embedder
        self.min_chars = min_chars
        self._df: Counter[str] = Counter()
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._blocks: list[dict] = []
        self._lengths: list[int] = []
        self._avg_len = 1.0
        self._pages: dict[str, dict] = {}
        #: Backlinks, loaded once at build time rather than queried per lookup. The
        #: index describes itself as in-memory and was not quite: `pages_for` reached
        #: into the store for every seed page, which is a database round trip inside a
        #: ranking loop and — once claims were classified concurrently — a second thread
        #: using the connection while the first iterated its results.
        self._backlinks: dict[str, list[str]] = {}
        self._vectors: dict[str, list[float]] = {}
        self._terms: list[Counter[str]] = []
        self.build()

    # -- construction ----------------------------------------------------------

    def build(self) -> Index:
        self._df.clear()
        self._postings.clear()
        self._blocks = []
        self._lengths = []
        self._terms = []

        self._pages = {p["page_id"]: p for p in self.store.get_pages()}
        for block in self.store.get_blocks():
            text = (block.get("text") or "").strip()
            # Short blocks are headings, bullets like "- see below", and empty
            # separators. Indexing them floods every query with noise.
            if len(text) < self.min_chars:
                continue
            if block.get("page_id") not in self._pages:
                continue
            tokens = tokenize(text)
            if not tokens:
                continue
            terms = tokens + _bigrams(tokens)
            counts = Counter(terms)
            doc = len(self._blocks)
            self._blocks.append(block)
            self._lengths.append(len(terms))
            self._terms.append(counts)
            for term, count in counts.items():
                self._postings[term].append((doc, count))
                self._df[term] += 1

        self._backlinks = {pid: list(self.store.backlinks(pid)) for pid in self._pages}
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._lengths else 1.0
        log.debug("index built: %d block(s), %d term(s)", len(self._blocks), len(self._df))
        return self

    def __len__(self) -> int:
        return len(self._blocks)

    @property
    def n_pages(self) -> int:
        return len(self._pages)

    # -- scoring ---------------------------------------------------------------

    def _bm25(self, query: str) -> dict[int, float]:
        tokens = tokenize(query)
        if not tokens:
            return {}
        terms = tokens + _bigrams(tokens)
        n = len(self._blocks) or 1
        scores: dict[int, float] = defaultdict(float)
        for term, qcount in Counter(terms).items():
            postings = self._postings.get(term)
            if not postings:
                continue
            df = self._df[term]
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            # Bigrams are worth more than their parts: matching "learning~rate" is
            # much stronger evidence than matching "learning" and "rate" separately.
            weight = 1.6 if "~" in term else 1.0
            for doc, tf in postings:
                length = self._lengths[doc] or 1
                norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self._avg_len))
                scores[doc] += idf * norm * qcount * weight
        return scores

    def _dense(self, query: str, docs: list[int]) -> dict[int, float]:
        """Cosine similarity over embeddings, for the blocks named in `docs`.

        Never raises. An embedding provider that is down, out of quota or misconfigured
        must degrade to lexical-only retrieval rather than fail an ingest: BM25 alone is
        the product's floor, not an error state.
        """
        if not self.embedder or not docs:
            return {}
        try:
            qvec = self.embedder.embed([query])[0]
            missing = [d for d in docs if self._blocks[d]["block_id"] not in self._vectors]
            if missing:
                # An embedder that persists its vectors needs to know which block each
                # text belongs to. Duck-typed rather than imported: this module stays
                # free of every dependency, which is what lets the sweeps run with no
                # key and no network at all.
                embedder = self.embedder
                if hasattr(embedder, "for_blocks"):
                    embedder = embedder.for_blocks(
                        [self._blocks[d]["block_id"] for d in missing])
                vectors = embedder.embed([self._blocks[d]["text"] for d in missing])
                for d, vec in zip(missing, vectors, strict=False):
                    if vec:
                        self._vectors[self._blocks[d]["block_id"]] = vec
            qnorm = math.sqrt(sum(a * a for a in qvec)) or 1.0
            out: dict[int, float] = {}
            for d in docs:
                vec = self._vectors.get(self._blocks[d]["block_id"])
                if not vec:
                    continue
                dot = sum(a * b for a, b in zip(qvec, vec, strict=False))
                nb = math.sqrt(sum(b * b for b in vec)) or 1.0
                out[d] = dot / (qnorm * nb)
            return out
        except Exception as e:
            log.warning("dense retrieval unavailable (%s); lexical only", e)
            return {}

    def _lexical(self, queries: Sequence[str]) -> dict[int, float]:
        """BM25 over several phrasings of the same question, keeping the best score.

        The best rather than the sum: variants are *rewordings*, not extra evidence, so a
        claim that matches strongly under one phrasing should score as if that had been
        the query. Summing would instead reward whichever claim happened to be given the
        most variants, which is a property of the expander, not of the notes.
        """
        best: dict[int, float] = {}
        for query in queries:
            for doc, score in self._bm25(query).items():
                if score > best.get(doc, 0.0):
                    best[doc] = score
        return best

    def _blended(self, queries: Sequence[str], *, deep: bool) -> dict[int, float]:
        """Lexical and dense scores over one normalised scale.

        With no embedder this is BM25 and nothing else, which is the honest default: the
        product must work for someone with no embedding provider at all.

        With one, `deep` decides *how much* work the vectors do. Re-ranking what BM25
        already surfaced (`deep=False`) is cheap and sharpens precision. Scoring every
        block (`deep=True`) is what actually finds the page whose wording shares nothing
        with the claim — the case lexical search cannot reach by construction, and the
        reason "did I already write this?" fails on a paraphrase. It costs one cosine per
        block, which for a personal knowledge base is milliseconds once the vectors are
        cached.
        """
        lexical = self._lexical(queries)
        if not self.embedder or not self._blocks:
            return lexical

        if deep:
            docs = list(range(len(self._blocks)))
        else:
            docs = sorted(lexical, key=lambda d: lexical[d], reverse=True)[:DENSE_RERANK]
        dense = self._dense(queries[0], docs)
        if not dense:
            return lexical

        lex_max = max(lexical.values(), default=0.0) or 1.0
        blended: dict[int, float] = {
            doc: LEXICAL_WEIGHT * (score / lex_max) for doc, score in lexical.items()
        }
        for doc, score in dense.items():
            # Cosine over a modern embedding model sits around 0.7 even for unrelated
            # text, so the floor is subtracted before weighting: without it every block
            # in the workspace gets the same large constant and the ranking is unchanged
            # while looking like it was improved.
            lift = max(0.0, score - DENSE_FLOOR) / (1.0 - DENSE_FLOOR)
            blended[doc] = blended.get(doc, 0.0) + DENSE_WEIGHT * lift
        return blended

    # -- the two queries -------------------------------------------------------

    def blocks_for(self, claim_text: str, *, top: int = 12,
                   exclude_pages: tuple[str, ...] = (),
                   also: Sequence[str] = ()) -> list[Candidate]:
        """"Does this already exist?" — high-precision, block level.

        `also` carries alternative phrasings of the same claim. Precision work, so the
        vectors only re-rank what the lexical pass already found.
        """
        scores = self._blended([claim_text, *also], deep=False)
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: top * 3]

        out: list[Candidate] = []
        for doc, score in ranked:
            block = self._blocks[doc]
            if block["page_id"] in exclude_pages:
                continue
            page = self._pages.get(block["page_id"], {})
            out.append(Candidate(
                block_id=block["block_id"], page_id=block["page_id"],
                page_title=page.get("title", ""), text=block["text"],
                score=float(score), block_type=block.get("type", "paragraph"),
            ))
            if len(out) >= top:
                break
        return out

    def pages_for(self, claim_text: str, topics: tuple[str, ...] = (), *,
                  top: int = 6, expand: bool = True,
                  also: Sequence[str] = ()) -> list[PageHit]:
        """"Where does this belong?" — recall-oriented, page level, graph-expanded.

        Page score aggregates its blocks' scores with a damped sum: a page with one
        strong match should beat a page with six weak ones, which a plain sum gets
        backwards. Titles are matched separately, because a page called "Concept
        injection" is about concept injection even if no block repeats the phrase.

        This is the recall path, so when vectors are available they score *every* block
        rather than re-ranking the lexical shortlist. A page whose wording shares nothing
        with the claim cannot be re-ranked into view; it has to be found in the first
        place, and that is the whole reason to have embeddings at all.

        `also` carries alternative phrasings — see `_lexical`.
        """
        query = claim_text + (" " + " ".join(topics) if topics else "")
        scores = self._blended([query, *also], deep=True)

        per_page: dict[str, float] = defaultdict(float)
        matched: dict[str, list[str]] = defaultdict(list)
        # Sorted, because the damping below is defined in terms of a page's *best*
        # blocks. Iterating the score map in its own order applied the /1, /2, /3 …
        # divisors in whatever sequence the postings happened to produce, so a page's
        # strongest match could be divided by six while a weak one was left whole — the
        # ranking looked principled and was close to arbitrary.
        for doc, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
            block = self._blocks[doc]
            pid = block["page_id"]
            # Damped: the k-th best block on a page contributes score / (k + 1).
            rank = len(matched[pid])
            per_page[pid] += score / (rank + 1.0)
            matched[pid].append(block["block_id"])

        # Title and topic overlap are added on the same scale as the block scores, which
        # `_blended` normalises to roughly 0-1 per block. A fixed +3.0 was calibrated
        # against raw BM25 magnitudes and would swamp everything else here.
        boost = max(per_page.values(), default=1.0) or 1.0
        qtokens = set(tokenize(query))
        for pid, page in self._pages.items():
            title_tokens = set(tokenize(page.get("title", "")))
            overlap = qtokens & title_tokens
            if overlap:
                per_page[pid] += TITLE_BOOST * boost * len(overlap)
            topic_overlap = {t.lower() for t in (page.get("topics") or [])} & set(topics)
            if topic_overlap:
                per_page[pid] += TOPIC_BOOST * boost * len(topic_overlap)

        if expand and per_page:
            # A page that links to a strong hit is plausibly relevant even when its own
            # words do not match — that is what a knowledge graph is for.
            seeds = sorted(per_page.items(), key=lambda kv: kv[1], reverse=True)[:3]
            for pid, score in seeds:
                for neighbour in self._backlinks.get(pid, ()):
                    if neighbour in self._pages:
                        per_page[neighbour] += score * BACKLINK_SHARE

        ranked = sorted(per_page.items(), key=lambda kv: kv[1], reverse=True)[:top]
        return [
            PageHit(page_id=pid, title=self._pages[pid].get("title", ""),
                    role=self._pages[pid].get("role") or "reference",
                    score=float(score), url=self._pages[pid].get("url"),
                    matched_blocks=matched.get(pid, []))
            for pid, score in ranked if pid in self._pages
        ]

    # -- similarity ------------------------------------------------------------

    def _idf(self, term: str) -> float:
        """Smoothed IDF that is always positive.

        BM25's IDF is deliberately *negative-going* for terms in most documents,
        because for ranking a term everyone uses cannot discriminate. That is exactly
        wrong for measuring similarity, where shared vocabulary is the entire signal —
        so this variant weights rare terms more without ever punishing common ones.
        """
        n = len(self._blocks) or 1
        return math.log(1.0 + n / (self._df.get(term, 0) + 1))

    def _cosine(self, a: Counter[str], b: Counter[str], bigrams: bool) -> float:
        keys_a = [t for t in a if ("~" in t) == bigrams]
        keys_b = [t for t in b if ("~" in t) == bigrams]
        if not keys_a or not keys_b:
            return 0.0
        shared = set(keys_a) & set(keys_b)
        if not shared:
            return 0.0

        def weight(counts: Counter[str], term: str) -> float:
            return (1.0 + math.log(counts[term])) * self._idf(term)

        dot = sum(weight(a, t) * weight(b, t) for t in shared)
        na = math.sqrt(sum(weight(a, t) ** 2 for t in keys_a))
        nb = math.sqrt(sum(weight(b, t) ** 2 for t in keys_b))
        return dot / (na * nb) if na and nb else 0.0

    def similarity(self, i: int, j: int) -> float:
        """Cosine similarity between two indexed blocks, in [0, 1].

        Sublinear term frequency (`1 + log tf`) so a word repeated five times does not
        count five times, and IDF-weighted so shared rare terms matter more than shared
        filler.

        Unigrams and bigrams are scored **separately and blended 0.7/0.3**, rather than
        thrown into one vector. Two genuine paraphrases share most of their vocabulary
        and almost none of their word pairs — put bigrams in the same vector and they
        dominate the norm (every bigram is rare, so every bigram has high IDF) and drag
        an obvious duplicate below any sensible threshold. Bigrams still earn their
        place: they are what stops "learning rate" matching a page about learning.
        """
        a, b = self._terms[i], self._terms[j]
        if not a or not b:
            return 0.0
        return 0.7 * self._cosine(a, b, False) + 0.3 * self._cosine(a, b, True)

    # -- used by the sweeps ----------------------------------------------------

    def near_duplicates(self, *, threshold: float = 0.32,
                        min_chars: int = 60) -> list[tuple[Candidate, Candidate, float]]:
        """Pairs of blocks on *different* pages that say nearly the same thing.

        This is the sweep that cleans up damage already done: the same paragraph
        written twice, months apart, on two pages you had forgotten were related.
        Same-page pairs are excluded — repetition within one note is usually
        deliberate.

        Two stages, and the split matters. BM25 **shortlists** (cheap, and it is good
        at finding "these might be about the same thing"), then `similarity()` scores
        the shortlisted pairs symmetrically. Using the BM25 score itself as the
        similarity is the intuitive mistake here and it silently finds nothing, because
        IDF down-weights precisely the shared terms that make two blocks duplicates.

        The default threshold is deliberately permissive. On measured examples a
        copy-paste scores ~1.0, a genuine reworded restatement ~0.3, and unrelated text
        ~0.0 — so a "safe" 0.5 would report only the copies and miss exactly the case
        that motivates the sweep: the same idea written twice, months apart, in
        different words. Results are ranked, so a low bar costs a longer tail rather
        than a worse top.
        """
        pairs: list[tuple[Candidate, Candidate, float]] = []
        seen: set[tuple[str, str]] = set()
        by_id = {b["block_id"]: i for i, b in enumerate(self._blocks)}

        for doc, block in enumerate(self._blocks):
            text = block["text"]
            if len(text) < min_chars:
                continue
            page = self._pages.get(block["page_id"], {})
            left = Candidate(block["block_id"], block["page_id"], page.get("title", ""),
                             text, 1.0, block.get("type", "paragraph"))

            for hit in self.blocks_for(text, top=8):
                if hit.block_id == block["block_id"] or hit.page_id == block["page_id"]:
                    continue
                if len(hit.text) < min_chars:
                    continue
                first, second = sorted((left.block_id, hit.block_id))
                key = (first, second)
                if key in seen:
                    continue
                other = by_id.get(hit.block_id)
                if other is None:
                    continue
                score = self.similarity(doc, other)
                if score < threshold:
                    continue
                seen.add(key)
                hit.score = score
                pairs.append((left, hit, score))

        pairs.sort(key=lambda p: p[2], reverse=True)
        return pairs
