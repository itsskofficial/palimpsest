"""The eval maths, offline.

The classifier run itself needs a model, so it is a command, not a test. But the scoring
— precision, recall, F1, and the contradiction weighting that makes the whole thing
honest — is pure arithmetic, and getting it wrong would make every measurement lie. So
that is pinned here, against confusion matrices built by hand.
"""

from __future__ import annotations

from palimpsest.evals.component import (
    PASS_CONTRADICTION_RECALL,
    _metrics,
    _passed,
)


def test_a_perfect_classifier_scores_one():
    confusion = {"new": {"new": 5}, "corroborates": {"corroborates": 5}}
    m = _metrics(confusion)
    assert m["per_relation"]["new"]["f1"] == 1.0
    assert m["macro_f1"] == 1.0
    assert m["weighted_f1"] == 1.0


def test_precision_and_recall_are_computed_from_the_confusion_matrix():
    # 'refines' predicted as 'supersedes' twice; caught 3 of 5.
    confusion = {
        "refines": {"refines": 3, "supersedes": 2},
        "supersedes": {"supersedes": 4},
    }
    m = _metrics(confusion)
    refines = m["per_relation"]["refines"]
    assert refines["recall"] == 0.6                      # 3 of 5
    assert refines["precision"] == 1.0                   # nothing else predicted refines
    sup = m["per_relation"]["supersedes"]
    assert sup["recall"] == 1.0                          # 4 of 4
    assert round(sup["precision"], 2) == 0.67            # 4 of 6 predicted-supersedes


def test_a_missed_contradiction_dominates_the_headline():
    """Two runs with the same macro error: one misses contradictions, one misses an
    equal number of `new`. The contradiction miss must score worse."""
    # Run A: contradictions caught 1 of 5 (bad recall on the weighted relation).
    a = _metrics({
        "contradicts": {"contradicts": 1, "supersedes": 4},
        "new": {"new": 5},
    })
    # Run B: same shape, but the misses are on `new` instead.
    b = _metrics({
        "new": {"new": 1, "extends": 4},
        "contradicts": {"contradicts": 5},
    })
    assert a["weighted_f1"] < b["weighted_f1"]


def test_a_run_fails_when_contradiction_recall_is_below_the_bar():
    m = _metrics({
        "contradicts": {"contradicts": 1, "supersedes": 4},   # 0.2 recall
        "new": {"new": 10},
    })
    assert m["contradiction_recall"] < PASS_CONTRADICTION_RECALL
    assert _passed(m) is False


def test_a_strong_run_passes():
    m = _metrics({
        "contradicts": {"contradicts": 5},
        "new": {"new": 8, "extends": 1},
        "corroborates": {"corroborates": 9},
    })
    assert m["contradiction_recall"] == 1.0
    assert _passed(m) is True


def test_the_golden_example_round_trips_through_the_store(tmp_path):
    from palimpsest.evals.golden import GoldenExample, load, put
    from palimpsest.store import open_store

    store = open_store(f"sqlite:///{tmp_path / 'e.db'}")
    try:
        ex = GoldenExample(id="ex_1", claim_text="Attention scales by 1/sqrt(d_k).",
                           claim_type="fact", topics=("attention",),
                           source_title="A paper", source_kind="pdf",
                           expected_relation="corroborates")
        put(store, ex)
        loaded = load(store)
        assert len(loaded) == 1
        assert loaded[0].expected_relation == "corroborates"
        assert loaded[0].topics == ("attention",)
    finally:
        store.close()


def test_bootstrap_turns_approved_operations_into_labels(tmp_path):
    """An approved operation is a label. This is what makes the golden set grow for free
    as the user works."""
    from palimpsest.evals.golden import bootstrap_from_history, load
    from palimpsest.store import open_store
    from palimpsest.types import (
        Claim,
        ClaimType,
        Operation,
        OpKind,
        Patch,
        Relation,
        Source,
    )

    store = open_store(f"sqlite:///{tmp_path / 'b.db'}")
    try:
        source = Source(source_id="src_1", kind="web", title="Post", text="…")
        store.put_source(source)
        claim = Claim(claim_id="clm_1", text="A scales by 1/sqrt(d).",
                      type=ClaimType.FACT, source_id="src_1")
        store.put_claims([claim])
        patch = Patch(patch_id="pch_1", source_id="src_1", operations=[
            Operation(kind=OpKind.ADD_CITATION, target="bk1",
                      relation=Relation.CORROBORATES, claim_id="clm_1",
                      payload={"rationale": "already stated"})])
        store.put_patch(patch)
        store.put_approval({"approval_id": "apr_1", "patch_id": "pch_1",
                            "status": "approved", "operation_ids": ["op"]})

        added = bootstrap_from_history(store)
        assert added == 1
        examples = load(store)
        assert examples[0].expected_relation == "corroborates"
        assert examples[0].label_source == "approval"
    finally:
        store.close()
