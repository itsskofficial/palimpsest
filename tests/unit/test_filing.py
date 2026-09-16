"""Where a claim goes when nothing in the notes is about it.

The failure this file exists for was found by sending a real playlist — ten videos on
neural networks — to a knowledge base about attention, sleep and gradient clipping. Every
claim was judged `new`, correctly, and every one was appended to an unrelated page:
"Gradient clipping" collected fifty-two bullets about handwritten digits. The rationale on
each said, in words, that nothing in the notes covered the claim.

Three places each forced a page onto a claim that had none, and a fourth left a vault with
nowhere to create one:

1. The no-model shortcut used the top page-level search hit when no block matched.
2. The classifier prompt said `target_page_id` was required for `new`.
3. A repair step filled in the top hit whenever the model returned no page.
4. Page creation needed a Notion root page, which a markdown vault does not have.

A misfiled claim is worse than a new page, because it is found by nobody.
"""

from __future__ import annotations

from palimpsest.config import Settings
from palimpsest.plan import plan
from palimpsest.relate import SYSTEM, _shortcut, classify_one
from palimpsest.retrieve import Candidate, Index, PageHit
from palimpsest.types import Claim, ClaimType, Judgement, OpKind, Relation, Source, new_id


def _claim(text="Handwritten digits are rendered on a 28x28 pixel grid.") -> Claim:
    return Claim(claim_id=new_id("clm_"), text=text, type=ClaimType.FACT,
                 topics=("neural networks",), confidence=0.9)


def _source() -> Source:
    return Source(source_id=new_id("src_"), kind="youtube",
                  title="But what is a neural network?", text="x",
                  url="https://www.youtube.com/watch?v=aircAruvnKk")


def _unrelated_page() -> PageHit:
    return PageHit(page_id="pg_clipping", title="Gradient clipping", role="reference",
                   score=0.4)


# ---------------------------------------------------------------------------
# the classifier leaves a homeless claim homeless
# ---------------------------------------------------------------------------


def test_a_claim_that_matched_no_block_is_not_given_the_top_page_hit():
    judgement = _shortcut(_claim(), candidates=[], pages=[_unrelated_page()])

    assert judgement is not None
    assert judgement.relation is Relation.NEW
    assert judgement.target_page_id is None


def test_the_prompt_no_longer_requires_a_page_for_new():
    """The instruction the model followed when it filed MNIST under gradient clipping."""
    assert "`target_page_id` is required for new" not in SYSTEM
    assert "set it to null" in SYSTEM


class _Model:
    model = "fake/classify-1"
    name = "fake"

    def __init__(self, payload):
        self.payload = payload

    def json(self, **kw):
        return self.payload


class _Index:
    def __init__(self, candidates, pages):
        self._candidates, self._pages = candidates, pages

    def blocks_for(self, text, top=8):
        return self._candidates

    def pages_for(self, text, topics=(), top=5):
        return self._pages


def _weakly_related_block() -> Candidate:
    # One shared word is enough for BM25 to return it, and not enough to be related.
    return Candidate("bk_clip", "pg_clipping", "Gradient clipping",
                     "Clipping bounds the gradient norm before each update.", 0.2,
                     "paragraph")


def test_a_new_claim_the_model_gave_no_page_keeps_no_page():
    """The model answered "nothing here is about this". The repair used to overrule it."""
    judgement = classify_one(
        _claim(), _source(), _Index([_weakly_related_block()], [_unrelated_page()]),
        _Model({"relation": "new", "confidence": 0.9, "target_page_id": None,
                "target_block_id": None, "existing_text": None,
                "rationale": "No candidate covers MNIST input representation."}))

    assert judgement.relation is Relation.NEW
    assert judgement.target_page_id is None


def test_extends_still_gets_a_page_when_the_model_forgot_to_name_one():
    """`extends` means "belongs on another page", so a missing page is a slip to repair."""
    judgement = classify_one(
        _claim(), _source(), _Index([_weakly_related_block()], [_unrelated_page()]),
        _Model({"relation": "extends", "confidence": 0.9, "target_page_id": None,
                "target_block_id": None, "existing_text": None, "rationale": "r"}))

    assert judgement.target_page_id == "pg_clipping"


def test_a_new_claim_the_model_placed_on_a_real_page_stays_there():
    judgement = classify_one(
        _claim(), _source(), _Index([_weakly_related_block()], [_unrelated_page()]),
        _Model({"relation": "new", "confidence": 0.9, "target_page_id": "pg_clipping",
                "target_block_id": None, "existing_text": None, "rationale": "r"}))

    assert judgement.target_page_id == "pg_clipping"


# ---------------------------------------------------------------------------
# the planner creates one page for them
# ---------------------------------------------------------------------------


def _homeless(claim: Claim) -> Judgement:
    return Judgement(claim_id=claim.claim_id, relation=Relation.NEW, confidence=0.9,
                     target_page_id=None, rationale="nothing matched", model="m")


def test_homeless_claims_from_one_source_become_one_new_page(mirror):
    claims = [_claim(f"Fact {i} about neurons and activations.") for i in range(5)]

    result = plan([_homeless(c) for c in claims], {c.claim_id: c for c in claims},
                  _source(), mirror, default_parent="pg_root")

    creations = [op for op in result.patch.operations if op.kind is OpKind.CREATE_PAGE]
    assert len(creations) == 1
    assert not [op for op in result.patch.operations if op.kind is OpKind.APPEND_BLOCK], \
        "nothing was appended to a page that already existed"


def test_a_vault_has_somewhere_to_create_a_page():
    from palimpsest.pipeline import _default_parent
    from palimpsest.workspace.markdown import VAULT_ROOT

    assert _default_parent(Settings(backend="markdown", vault_path="/notes")) == VAULT_ROOT
    assert _default_parent(Settings(notion_root_pages=("pg_root",))) == "pg_root"
    assert _default_parent(Settings(backend="notion")) is None


def test_a_page_created_at_the_vault_root_is_a_top_level_page(tmp_path):
    from palimpsest.workspace.convert import to_blocks
    from palimpsest.workspace.markdown import VAULT_ROOT, MarkdownWorkspace

    vault = MarkdownWorkspace(tmp_path)
    created = vault.create_page(VAULT_ROOT, "Neural networks",
                                children=to_blocks("A neuron holds a number."))

    text = next(tmp_path.glob("neural-networks*.md")).read_text(encoding="utf-8")
    assert created["id"]
    assert VAULT_ROOT not in text, "the sentinel is not written into the page as a parent"


def test_a_homeless_claim_in_a_real_index_goes_to_a_new_page_end_to_end(mirror):
    """Retrieval over the conftest workspace, a claim about something it has never seen,
    and the whole classify-then-plan path with no model involved."""
    from palimpsest.relate import classify

    claim = _claim("Zebrafish regenerate cardiac tissue after cryoinjury.")
    from palimpsest.llm import Usage

    model = _Model({})
    model.usage = Usage()
    classified = classify([claim], _source(), Index(mirror), model, workers=1)
    assert model.usage.calls == 0, "nothing matched, so the shortcut decided it"

    result = plan(classified.judgements, {claim.claim_id: claim}, _source(), mirror,
                  default_parent="pg_root")

    kinds = [op.kind for op in result.patch.operations]
    assert kinds == [OpKind.CREATE_PAGE]
