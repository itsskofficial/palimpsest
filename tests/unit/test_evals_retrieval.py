"""The retrieval eval, run as a merge gate.

Most of this project's evals need a model, so they are commands you run when you want a
number. This one does not: retrieval is BM25 over a committed fixture, so it is a
deterministic, offline measurement of the part of the system everything else depends on.
That makes it the rare eval that can be a *test*.

Which matters, because retrieval regressions are the quietest kind. The classifier keeps
returning well-formed judgements, the patches keep reading sensibly, and the only symptom
is that claims start being filed as `new` against pages that already covered them —
noticed weeks later as "it seems to be making a lot of duplicate pages lately".

The thresholds ratchet. When a change improves the numbers, raise `PASS_RECALL` and
`PASS_MRR` to the new floor; that is the point of having them.
"""

from __future__ import annotations

from palimpsest.evals import retrieval
from palimpsest.evals.fixture import cases, seed


def test_retrieval_clears_its_bar_on_the_committed_workspace(store):
    metrics = retrieval.run(store)

    assert metrics["passed"], retrieval.summary(metrics)
    assert metrics[f"recall_at_{metrics['k']}"] >= retrieval.PASS_RECALL
    assert metrics["mrr"] >= retrieval.PASS_MRR


def test_the_thresholds_are_a_floor_the_current_numbers_clear_comfortably(store):
    """A bar set exactly at today's score fails on noise; one set far below it measures
    nothing. These are the numbers as measured, minus a little room."""
    metrics = retrieval.run(store)

    assert metrics[f"recall_at_{metrics['k']}"] == 1.0
    assert metrics["mrr"] == 1.0


def test_every_case_names_the_property_it_tests(store):
    """A case with no stated reason becomes noise the moment it fails: nobody can tell
    whether the ranking got worse or the case was always arbitrary."""
    retrieval_cases, relation_cases = cases()

    for case in retrieval_cases + relation_cases:  # type: ignore[operator]
        assert case.why and len(case.why) > 30, case.id


def test_the_fixture_covers_every_relation(store):
    """The classifier has seven answers. A golden set missing one cannot measure it, and
    the missing one is always the rare, expensive kind."""
    from palimpsest.types import Relation

    _, relation_cases = cases()
    covered = {case.expect for case in relation_cases}
    assert covered == {r.value for r in Relation}


def test_contradictions_are_represented_more_than_once(store):
    """Contradiction recall is weighted 3x in the headline score. A single example makes
    that headline a coin flip."""
    _, relation_cases = cases()
    contradictions = [c for c in relation_cases if c.expect == "contradicts"]
    assert len(contradictions) >= 3


def test_the_semantic_cases_are_genuinely_unreachable_without_vectors(store):
    """The cases marked `needs_vectors` must actually be unreachable lexically.

    One of them originally was not — the note and the query happened to share
    "similar", "networks" and "comparing", so BM25 found it and the case silently
    measured nothing. A semantic case that passes without an embedder is not evidence
    that embeddings work; it is evidence that the case is mislabelled.
    """
    metrics = retrieval.run(store)

    assert metrics["semantic_n"] >= 2
    assert metrics["semantic_recall"] == 0.0


def test_vectors_recover_the_cases_lexical_search_cannot_reach(store):
    """The other half of the claim: those cases are reachable *with* an embedder.

    Scripted vectors rather than a provider — the property under test is that the dense
    path is wired into the recall query at all, not that any particular model is good.
    """
    seed(store)
    retrieval_cases, _ = cases()
    semantic = [c for c in retrieval_cases if c.needs_vectors]

    # Every block of an expected page points the same way as its case's query; everything
    # else points elsewhere. Two dimensions is enough to express "related" and "not".
    wanted_pages = {p for c in semantic for p in c.pages}
    aligned = {c.query for c in semantic}
    aligned |= {b["text"] for b in _blocks_of(store, wanted_pages)}

    class Scripted:
        model = "scripted"

        def embed(self, texts):
            return [[1.0, 0.0] if t in aligned else [0.0, 1.0] for t in texts]

    metrics = retrieval.run(store, embedder=Scripted(), seed_store=False)
    assert metrics["semantic_recall"] == 1.0


def _blocks_of(store, page_ids):
    return [b for b in store.get_blocks() if b["page_id"] in page_ids]
