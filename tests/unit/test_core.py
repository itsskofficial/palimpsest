"""The properties the README claims. Every test here maps to a sentence in it.

These run offline with no key, which is the point: the decision layer — relations,
patch planning, inverses, retrieval, the sweeps — is a pure function over the mirror,
and a change that breaks one of these breaks a promise made to the user.
"""

from __future__ import annotations

import pytest

from palimpsest.plan import plan
from palimpsest.retrieve import Index, tokenize
from palimpsest.types import Judgement, Operation, OpKind, Patch, Relation, new_id

# ---------------------------------------------------------------------------
# the relation taxonomy
# ---------------------------------------------------------------------------


def test_contradicts_is_never_auto_appliable():
    """The core safety claim: no setting makes a contradiction automatic."""
    assert Relation.CONTRADICTS.auto_appliable is False
    assert Relation.CONTRADICTS.risk == "high"
    for relation in Relation:
        if relation is not Relation.CONTRADICTS:
            assert relation.auto_appliable is True


def test_autonomy_has_no_level_that_covers_contradictions():
    """There is deliberately no `autonomy=high`; the enum cannot express it."""
    from palimpsest.config import AUTONOMY_LEVELS

    assert "high" not in AUTONOMY_LEVELS
    for allowed in AUTONOMY_LEVELS.values():
        assert Relation.CONTRADICTS.risk not in allowed


def test_apply_and_autonomy_are_independent_switches():
    from palimpsest.config import Settings

    s = Settings(apply=False, autonomy="medium")
    assert s.may_auto_apply("low") is False, "apply=off must veto every relation"
    s = Settings(apply=True, autonomy="low")
    assert s.may_auto_apply("low") is True
    assert s.may_auto_apply("medium") is False
    assert s.may_auto_apply("high") is False


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------


def test_corroborates_adds_a_citation_and_no_prose(mirror, claim, source):
    """The relation that stops the base bloating must not add text."""
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.CORROBORATES,
                          confidence=0.95, target_page_id="pg_attention",
                          target_block_id="bk_att_1")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror)

    kinds = [op.kind for op in result.patch.operations]
    assert kinds == [OpKind.ADD_CITATION]
    assert not any(k in (OpKind.APPEND_BLOCK, OpKind.CREATE_PAGE) for k in kinds)


def test_duplicate_links_rather_than_appending(mirror, claim, source):
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.DUPLICATE,
                          confidence=0.9, target_page_id="pg_transformers",
                          target_block_id="bk_tr_1")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror)
    assert [op.kind for op in result.patch.operations] == [OpKind.LINK_PAGES]


def test_contradiction_produces_no_operation_only_a_review_item(mirror, claim, source):
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.CONTRADICTS,
                          confidence=0.99, target_page_id="pg_attention",
                          target_block_id="bk_att_1",
                          existing_text="something incompatible")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror)

    assert len(result.patch.operations) == 0
    assert len(result.review) == 1
    assert result.review[0]["reason"] == "contradiction"


def test_low_confidence_routes_to_review_not_to_the_patch(mirror, claim, source):
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.NEW,
                          confidence=0.20, target_page_id="pg_attention")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror,
                  min_confidence=0.75)
    assert len(result.patch.operations) == 0
    assert result.review[0]["reason"] == "low_confidence"


def test_supersedes_strikes_rather_than_deleting(mirror, claim, source):
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.SUPERSEDES,
                          confidence=0.9, target_page_id="pg_optim",
                          target_block_id="bk_opt_2", existing_text="old price")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror)
    kinds = [op.kind for op in result.patch.operations]

    assert OpKind.STRIKE_BLOCK in kinds
    assert OpKind.ARCHIVE_BLOCK not in kinds, "superseding must never archive"
    assert OpKind.APPEND_BLOCK in kinds


def test_hub_pages_get_a_link_not_a_paragraph(mirror, claim, source):
    """Page role changes the edit: appending prose to an index page is how hubs rot."""
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.NEW,
                          confidence=0.9, target_page_id="pg_index")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror)
    assert [op.kind for op in result.patch.operations] == [OpKind.LINK_PAGES]


def test_refines_footnotes_the_previous_wording(mirror, claim, source):
    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.REFINES,
                          confidence=0.9, target_page_id="pg_optim",
                          target_block_id="bk_opt_1", existing_text="AdamW is better")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror, footnotes=True)
    kinds = [op.kind for op in result.patch.operations]
    assert OpKind.UPDATE_TEXT in kinds
    assert OpKind.INSERT_FOOTNOTE in kinds


# ---------------------------------------------------------------------------
# reversibility
# ---------------------------------------------------------------------------


def test_reverse_only_includes_operations_that_actually_ran():
    """A partially-applied patch must invert to exactly the part that landed."""
    applied = Operation(kind=OpKind.UPDATE_TEXT, target="bk_1",
                        payload={"text": "new"},
                        inverse={"kind": "update_text", "target": "bk_1",
                                 "payload": {"text": "old"}})
    applied.applied_at = 1.0
    never_ran = Operation(kind=OpKind.UPDATE_TEXT, target="bk_2",
                          payload={"text": "new"},
                          inverse={"kind": "update_text", "target": "bk_2",
                                   "payload": {"text": "old"}})

    patch = Patch(patch_id=new_id("pch_"), source_id="src",
                  operations=[applied, never_ran])
    reverse = patch.reverse()

    assert len(reverse.operations) == 1
    assert reverse.operations[0].target == "bk_1"


def test_reverse_applies_operations_in_opposite_order():
    ops = []
    for i in range(3):
        op = Operation(kind=OpKind.UPDATE_TEXT, target=f"bk_{i}", payload={},
                       inverse={"kind": "update_text", "target": f"bk_{i}",
                                "payload": {}})
        op.applied_at = float(i)
        ops.append(op)
    reverse = Patch(patch_id="p", source_id="s", operations=ops).reverse()
    assert [op.target for op in reverse.operations] == ["bk_2", "bk_1", "bk_0"]


def test_patch_round_trips_through_json():
    op = Operation(kind=OpKind.ADD_CITATION, target="bk_1",
                   payload={"label": "x"}, relation=Relation.CORROBORATES,
                   claim_id="clm_1")
    patch = Patch(patch_id="pch_1", source_id="src_1", operations=[op])
    restored = Patch.from_dict(patch.as_dict())

    assert restored.patch_id == patch.patch_id
    assert restored.operations[0].kind is OpKind.ADD_CITATION
    assert restored.operations[0].relation is Relation.CORROBORATES


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------


def test_tokenizer_splits_hyphenated_compounds():
    """`dot-product` and `dot product` must share vocabulary, or duplicates hide."""
    tokens = tokenize("Scaled dot-product attention")
    assert "dot-product" in tokens
    assert "dot" in tokens and "product" in tokens


def test_similarity_separates_copies_paraphrases_and_unrelated_text(mirror):
    index = Index(mirror)
    ids = {b["block_id"]: i for i, b in enumerate(index._blocks)}

    copy = index.similarity(ids["bk_att_1"], ids["bk_tr_1"])
    paraphrase = index.similarity(ids["bk_att_1"], ids["bk_tr_2"])
    unrelated = index.similarity(ids["bk_att_1"], ids["bk_opt_1"])

    assert copy > 0.95, "an exact copy must score ~1"
    assert paraphrase > unrelated, "a paraphrase must beat unrelated text"
    assert unrelated < 0.15


def test_block_and_page_retrieval_answer_different_questions(mirror):
    index = Index(mirror)
    blocks = index.blocks_for("attention divides by the square root of the key dimension")
    pages = index.pages_for("attention divides by the square root of the key dimension")

    assert blocks and blocks[0].block_id in ("bk_att_1", "bk_tr_1")
    assert pages and pages[0].page_id in ("pg_attention", "pg_transformers")


def test_retrieval_excludes_short_boilerplate_blocks(store):
    store.put_pages([{"page_id": "p", "title": "T", "last_edited": "x"}])
    store.put_blocks([{"block_id": "b1", "page_id": "p", "type": "paragraph",
                       "text": "see below", "position": 0}])
    assert len(Index(store)) == 0


# ---------------------------------------------------------------------------
# the sweeps
# ---------------------------------------------------------------------------


def test_duplicate_sweep_finds_the_cross_page_copy(mirror):
    from palimpsest import sweep

    result = sweep.duplicates(mirror)
    pairs = {frozenset((f["a"]["block_id"], f["b"]["block_id"])) for f in result.findings}
    assert frozenset(("bk_att_1", "bk_tr_1")) in pairs


def test_duplicate_sweep_ignores_same_page_repetition(mirror):
    """Repetition inside one note is usually deliberate."""
    from palimpsest import sweep

    for f in sweep.duplicates(mirror).findings:
        assert f["a"]["page_id"] != f["b"]["page_id"]


def test_duplicate_sweep_needs_no_model(mirror, monkeypatch):
    from palimpsest import sweep

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert len(sweep.duplicates(mirror)) >= 1


def test_open_questions_finds_the_question(mirror):
    from palimpsest import sweep

    texts = [f["text"] for f in sweep.open_questions(mirror).findings]
    assert any("Qwen2.5" in t for t in texts)


def test_stale_sweep_reports_nothing_without_provenance(mirror):
    """Staleness is measured from when a claim entered the base."""
    from palimpsest import sweep

    result = sweep.stale(mirror)
    assert len(result) == 0
    assert result.notes, "it should say why it found nothing"


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_unlocatable_quotes_are_discarded(source):
    """A claim whose quote is not in the source is a paraphrase or an invention."""
    from palimpsest.extract import _locate

    assert _locate("gradient variance stable", source.text, 0) is not None
    assert _locate("a sentence that is simply not present", source.text, 0) is None


def test_locate_tolerates_reflowed_whitespace():
    """Models reflow line breaks when quoting from a PDF."""
    from palimpsest.extract import _locate

    text = "the learning rate was\n   3e-4 with warmup"
    assert _locate("the learning rate was 3e-4", text, 0) is not None


def test_windows_overlap_so_boundary_claims_survive():
    from palimpsest.extract import _windows

    text = ("sentence. " * 4000)
    windows = _windows(text, size=5000, overlap=500)
    assert len(windows) > 1
    starts = [w[0] for w in windows]
    assert starts[1] < 5000, "the second window must start before the first one ended"


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec,expected", [
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/watch?v=abc123", "youtube"),
    ("https://example.com/post", "web"),
    ("https://example.com/paper.pdf", "pdf"),
    ("notes.md", "text"),
    ("data.csv", "tabular"),
    ("whiteboard.png", "image"),
    ("text:a thought", "text"),
])
def test_kind_detection(spec, expected):
    from palimpsest.ingest import detect_kind

    assert detect_kind(spec) == expected


def test_html_reader_drops_chrome_and_keeps_headings():
    from palimpsest.ingest.web import html_to_text

    title, text = html_to_text(
        "<html><head><title>T</title></head><body><nav>skip me</nav>"
        "<h2>Results</h2><p>The number was 20%.</p>"
        "<script>alert(1)</script></body></html>")
    assert title == "T"
    assert "skip me" not in text
    assert "alert" not in text
    assert "## Results" in text
    assert "20%" in text


def test_markdown_segments_carry_a_cumulative_heading_path():
    from palimpsest.ingest.web import segments_from_markdown

    segments = segments_from_markdown("# Paper\n\ntext\n\n## Results\n\nmore text")
    locators = [s["locator"] for s in segments]
    assert any(loc == "Paper › Results" for loc in locators)


def test_anchor_falls_back_to_an_offset(source):
    """A claim is never left un-anchored."""
    from palimpsest.ingest import anchor_for

    anchor = anchor_for(source, 100_000, 100_010)
    assert anchor.kind == "offset"
    assert anchor.start == 100_000


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


def test_source_reingestion_is_idempotent(store, source):
    store.put_source(source)
    store.put_source(source)
    assert store.find_source_by_hash(source.content_hash).source_id == source.source_id


def test_page_profile_survives_a_resync(mirror):
    """A re-sync must not wipe a role the model computed — Notion never returns one."""
    mirror.set_page_profile("pg_attention", "deep_dive", "a summary", ["attention"])
    mirror.put_pages([{"page_id": "pg_attention", "title": "Attention",
                       "last_edited": "2026-04-01T00:00:00Z", "topics": []}])
    page = mirror.get_page("pg_attention")
    assert page["role"] == "deep_dive"
    assert page["summary"] == "a summary"
    assert page["topics"] == ["attention"]


def test_drop_missing_archives_rather_than_deletes(mirror):
    gone = mirror.drop_missing({"pg_attention"})
    assert gone == 3
    assert mirror.get_page("pg_optim")["archived"] is True
    assert mirror.get_page("pg_optim") is not None, "the row must survive for the ledger"
