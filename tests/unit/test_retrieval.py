"""Retrieval: the part that decides whether anything downstream is even about the
right page.

The classifier gets the blame when a claim lands somewhere silly, but the classifier can
only choose among the pages retrieval handed it. A page that was never retrieved cannot
be corroborated, so it becomes a new page instead — and the tool that exists to stop
notes fragmenting causes it. That failure is invisible from the outside: the patch looks
reasonable, the reasoning reads well, and it is wrong.

So these tests pin ranking behaviour rather than plumbing, including two properties that
were quietly broken and produced plausible-looking output the whole time.

Everything here runs offline. The dense path uses a scripted embedder, which is the only
way to test "finds the paraphrase" deterministically.
"""

from __future__ import annotations

import pytest

from palimpsest.retrieve import Index

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

PAGES = [
    {"page_id": "pg_attn", "title": "Attention", "role": "deep_dive"},
    {"page_id": "pg_opt", "title": "Optimisers", "role": "deep_dive"},
    {"page_id": "pg_misc", "title": "Reading list", "role": "index"},
]

BLOCKS = [
    {"block_id": "b_scale", "page_id": "pg_attn", "type": "paragraph", "position": 0,
     "text": "Scaled dot-product attention divides the logits by the square root of "
             "the key dimension to keep gradient variance stable."},
    {"block_id": "b_heads", "page_id": "pg_attn", "type": "paragraph", "position": 1,
     "text": "Multi-head attention runs several attention operations in parallel and "
             "concatenates their outputs before a final projection."},
    {"block_id": "b_adamw", "page_id": "pg_opt", "type": "paragraph", "position": 0,
     "text": "AdamW decouples weight decay from the gradient update, which is why it "
             "generalises better than Adam with L2 regularisation."},
    {"block_id": "b_cka", "page_id": "pg_misc", "type": "paragraph", "position": 0,
     "text": "The CKA/RSA stuff from last term is worth revisiting before the exam."},
]


@pytest.fixture()
def index(store):
    store.put_pages(PAGES)
    store.put_blocks(BLOCKS)
    return Index(store)


class ScriptedEmbedder:
    """Vectors by lookup, so "semantically close" is a fact of the test, not a hope.

    Two dimensions: attention-ness and representation-similarity-ness. Real embeddings
    are 1536-dimensional and noisy; the property under test — that a dense hit can
    surface a block sharing no vocabulary with the query — is the same at two dimensions
    and is actually checkable here.
    """

    model = "scripted-2d"

    def __init__(self, table: dict[str, list[float]], default=(0.0, 0.0)):
        self.table = table
        self.default = list(default)
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.table.get(t, self.default) for t in texts]


# ---------------------------------------------------------------------------
# lexical ranking
# ---------------------------------------------------------------------------


def test_the_right_page_wins_on_an_exact_topic(index):
    hits = index.pages_for("how does attention scale the logits")
    assert hits[0].page_id == "pg_attn"


def test_a_pages_best_block_is_the_one_that_counts(store):
    """The damping must be applied in score order.

    A page's score is a damped sum: its best block counts fully, its second half, and so
    on — so one strong match beats six weak ones. That only works if the blocks are
    sorted first. Iterating the raw score map applied the divisors in whatever order the
    postings produced, so a page's strongest block could be divided by five while a weak
    one was left whole. The ranking looked principled and was close to arbitrary, and
    nothing downstream could tell.

    Here pg_noise has many weak mentions and pg_sharp has one exact one.
    """
    store.put_pages([
        {"page_id": "pg_sharp", "title": "Sharp", "role": "deep_dive"},
        {"page_id": "pg_noise", "title": "Noise", "role": "deep_dive"},
    ])
    sharp = ("Gradient checkpointing trades recomputation for memory by discarding "
             "activations and recomputing them during the backward pass.")
    store.put_blocks(
        [{"block_id": "b_sharp", "page_id": "pg_sharp", "type": "paragraph",
          "position": 0, "text": sharp}]
        + [{"block_id": f"b_n{i}", "page_id": "pg_noise", "type": "paragraph",
            "position": i,
            "text": f"Memory is a recurring concern in training run {i}, discussed "
                    f"briefly across several unrelated experiments and notes."}
           for i in range(6)]
    )
    hits = Index(store).pages_for(sharp)
    assert hits[0].page_id == "pg_sharp"


def test_a_title_match_lifts_a_page_without_swamping_the_ranking(index):
    """The boost is a fraction of the top block score, not a fixed number.

    It used to be +3.0, calibrated against the BM25 magnitudes of a small corpus. Those
    magnitudes move with corpus size, so on a real workspace the constant either vanished
    or dominated. Scaling it keeps its meaning — a title match is worth about half a
    strong body match — at any size.
    """
    hits = {h.page_id: h.score for h in index.pages_for("optimisers")}
    assert hits["pg_opt"] > hits.get("pg_attn", 0.0)


def test_blocks_for_is_precise_where_pages_for_is_broad(index):
    blocks = index.blocks_for("weight decay decoupled from the gradient update", top=1)
    assert blocks[0].block_id == "b_adamw"


def test_excluded_pages_never_come_back(index):
    blocks = index.blocks_for("attention logits square root", exclude_pages=("pg_attn",))
    assert all(b.page_id != "pg_attn" for b in blocks)


def test_alternative_phrasings_take_the_best_score_not_the_sum(index):
    """Variants are rewordings, not extra evidence.

    Summing would rank a claim by how many paraphrases the expander happened to produce,
    which is a property of the expander and not of the notes.
    """
    plain = index.pages_for("adamw weight decay")
    with_variants = index.pages_for(
        "adamw weight decay",
        also=["decoupled weight decay", "adam with L2 regularisation"])

    assert with_variants[0].page_id == plain[0].page_id == "pg_opt"
    assert with_variants[0].score >= plain[0].score


# ---------------------------------------------------------------------------
# the dense path
# ---------------------------------------------------------------------------


def test_vectors_find_the_page_that_shares_no_words_with_the_query(store):
    """The whole reason to have embeddings.

    "representational similarity analysis" and "the CKA/RSA stuff" have no token in
    common, so BM25 cannot rank that note at all — not badly, at all. The claim is
    therefore filed as new and the notes fragment, which is the failure the product
    exists to prevent. A re-ranker cannot fix this either: there is nothing in the
    shortlist to re-rank.
    """
    store.put_pages(PAGES)
    store.put_blocks(BLOCKS)
    query = "representational similarity analysis between layers"

    lexical_only = Index(store).pages_for(query)
    assert "pg_misc" not in [h.page_id for h in lexical_only]

    embedder = ScriptedEmbedder({
        query: [0.0, 1.0],
        BLOCKS[3]["text"]: [0.0, 1.0],          # the CKA/RSA note
        BLOCKS[0]["text"]: [1.0, 0.0],
        BLOCKS[1]["text"]: [1.0, 0.0],
        BLOCKS[2]["text"]: [0.7, 0.0],
    })
    with_vectors = Index(store, embedder=embedder).pages_for(query)
    assert with_vectors[0].page_id == "pg_misc"


def test_a_broken_embedder_degrades_to_lexical_rather_than_failing(store):
    """BM25 is the floor, not an error state. An embedding provider that is down, out of
    quota or misconfigured must not fail an ingest."""
    store.put_pages(PAGES)
    store.put_blocks(BLOCKS)

    class Broken:
        model = "broken"

        def embed(self, texts):
            raise RuntimeError("402 payment required")

    hits = Index(store, embedder=Broken()).pages_for("attention logits square root")
    assert hits[0].page_id == "pg_attn"


def test_the_recall_path_embeds_every_block(store):
    """pages_for scores everything; blocks_for only re-ranks the shortlist.

    Different jobs: finding a page nothing lexically matched needs every vector, while
    sharpening an already-good shortlist does not and should not pay for it.
    """
    store.put_pages(PAGES)
    store.put_blocks(BLOCKS)
    embedder = ScriptedEmbedder({}, default=(0.5, 0.5))

    Index(store, embedder=embedder).pages_for("attention")
    embedded = {t for call in embedder.calls for t in call}
    assert all(b["text"] in embedded for b in BLOCKS)


def test_vectors_are_asked_for_by_block_so_a_cache_can_key_on_them(store):
    """A persisting embedder is handed block ids positionally before each call.

    Duck-typed on purpose: `retrieve` must not import the embedding layer, or the offline
    core stops being dependency-free and the sweeps stop running without a key.
    """
    store.put_pages(PAGES)
    store.put_blocks(BLOCKS)

    class Recording(ScriptedEmbedder):
        def __init__(self):
            super().__init__({}, default=(0.5, 0.5))
            self.declared: list[list[str]] = []

        def for_blocks(self, block_ids):
            self.declared.append(list(block_ids))
            return self

    embedder = Recording()
    Index(store, embedder=embedder).pages_for("attention")

    declared = {bid for call in embedder.declared for bid in call}
    assert declared == {b["block_id"] for b in BLOCKS}
