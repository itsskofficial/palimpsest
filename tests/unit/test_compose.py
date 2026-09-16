"""Laying out a page, which is the one operation that is not small.

`rewrite_section` replaces a run of blocks wholesale, so this is the most destructive
thing the system does — and the destruction is only acceptable because of two properties
checked here.

**Nothing is silently dropped.** The composer is handed claims and returns blocks, and a
claim that appears in neither is a fact that went into the pipeline and came out nowhere.
`ok` is false when that happens, and the planner refuses the operation — because a page
that is *mostly* right is far worse than one that failed, since nobody re-reads a page
that looks finished.

**A rewrite keeps what is already there.** The model is given the page as it stands and
told to fold the new claims in. Left out, the obvious behaviour is to write a fine page
containing only the new material, which reads as a good edit and is a deletion.
"""

from __future__ import annotations

import pytest

from palimpsest.compose import (
    ComposeResult,
    compose_operation,
    compose_page,
    page_text,
    rewrite_page,
)
from palimpsest.llm import TokenUsage, Usage
from palimpsest.types import Anchor, Claim, ClaimType, OpKind, Relation, Source, new_id


class FakeModel:
    """Returns a scripted layout and records the prompt it was given."""

    model = "fake/compose-1"
    name = "fake"
    base_url = None

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {
            "title": "Gradient clipping",
            "icon": "📐",
            "summary": "How clipping bounds an update.",
            "blocks": [
                {"type": "heading_2", "text": "What it is"},
                {"type": "paragraph", "text": "Global-norm clipping is the default.",
                 "claim_ids": ["clm_1"]},
            ],
        }
        self.prompts: list[str] = []
        self.usage = Usage()

    def json(self, *, task, system, prompt, schema, effort="high",
             cache_prefix=None, max_tokens=None):
        self.prompts.append(prompt)
        self.usage.add(task, TokenUsage(input=500, output=200), 0.01)
        return self.payload


def _source() -> Source:
    return Source(source_id=new_id("src_"), kind="web", title="A post on clipping",
                  text="Global-norm clipping is the default.",
                  url="https://example.com/p")


def _claims(*texts: str) -> list[Claim]:
    return [
        Claim(claim_id=f"clm_{i}", text=text, type=ClaimType.FACT,
              topics=("clipping",), confidence=0.9,
              anchor=Anchor("section", "Intro", 0, 10, None), source_id="src_1")
        for i, text in enumerate(texts, start=1)
    ]


# ---------------------------------------------------------------------------
# what comes back
# ---------------------------------------------------------------------------


def test_a_composition_produces_notion_blocks_and_a_title():
    result = compose_page(_claims("Global-norm clipping is the default."),
                          _source(), FakeModel())

    assert result.title == "Gradient clipping"
    assert result.icon == "📐"
    assert [b["type"] for b in result.children] == ["heading_2", "paragraph", "callout"],         "the layout, then the line naming the source"
    assert result.ok


def test_every_claim_is_offered_to_the_model_with_the_id_it_must_cite():
    """The model answers with `claim_ids`, and that is the only way to tell which facts
    actually made it onto the page. Without the ids in the prompt there is nothing to
    match against and coverage cannot be checked at all."""
    model = FakeModel()
    claims = _claims("first fact", "second fact")

    compose_page(claims, _source(), model)

    prompt = model.prompts[0]
    for claim in claims:
        assert claim.claim_id in prompt
        assert claim.text in prompt


def test_a_claim_the_layout_never_used_is_reported_as_missing():
    """The failure this exists to catch. A page that is mostly right is worse than one
    that failed, because nobody re-reads a page that looks finished."""
    result = compose_page(_claims("used", "dropped"), _source(), FakeModel())

    assert result.covered == {"clm_1"}
    assert result.missing == {"clm_2"}
    assert result.ok is False, "a dropped claim must not pass as a good composition"


def test_a_layout_citing_an_id_that_was_never_sent_does_not_count_as_coverage():
    """A hallucinated id would otherwise mark a real claim as covered and let a page
    through that is missing it."""
    result = compose_page(
        _claims("the only claim"), _source(),
        FakeModel({"title": "T", "blocks": [
            {"type": "paragraph", "text": "x", "claim_ids": ["clm_invented"]}]}))

    assert result.covered == set()
    assert result.missing == {"clm_1"}


def test_the_claim_ids_are_stripped_before_the_blocks_reach_notion():
    """`claim_ids` is bookkeeping between us and the model. Left on the block it is an
    unknown field, and Notion rejects the whole append."""
    result = compose_page(_claims("a fact"), _source(), FakeModel())

    for block in result.children:
        assert "claim_ids" not in str(block)


def test_a_layout_with_no_usable_blocks_is_not_ok_rather_than_an_empty_page():
    """`blocks_from_spec` refusing everything must not produce a page with a title and
    nothing in it — which applies cleanly and looks like the tool did its job."""
    result = compose_page(_claims("a fact"), _source(),
                          FakeModel({"title": "T", "blocks": [{"type": "nonsense"}]}))

    assert result.children == []
    assert result.ok is False


def test_junk_in_the_block_list_is_skipped_rather_than_raising():
    """A model that returns a string where a block belongs should cost that block, not
    the whole composition."""
    result = compose_page(
        _claims("a fact"), _source(),
        FakeModel({"title": "T", "blocks": [
            "not a block",
            {"type": "paragraph", "text": "a fact", "claim_ids": ["clm_1"]}]}))

    assert [b["type"] for b in result.children] == ["paragraph", "callout"]
    assert result.ok


def test_a_layout_with_no_title_falls_back_to_the_source_title():
    """An untitled page in Notion is findable only by scrolling."""
    result = compose_page(_claims("a fact"), _source(),
                          FakeModel({"blocks": [
                              {"type": "paragraph", "text": "a fact",
                               "claim_ids": ["clm_1"]}]}))

    assert result.title == "A post on clipping"
    assert result.icon, "a page with no icon is harder to spot in a sidebar"


def test_an_absurd_title_is_truncated_rather_than_sent():
    result = compose_page(_claims("a fact"), _source(),
                          FakeModel({"title": "x" * 500, "blocks": [
                              {"type": "paragraph", "text": "a fact",
                               "claim_ids": ["clm_1"]}]}))

    assert len(result.title) <= 120


# ---------------------------------------------------------------------------
# rewriting, where the page already has content
# ---------------------------------------------------------------------------


def test_a_rewrite_shows_the_model_the_page_as_it_stands():
    """Without it the obvious behaviour is to write a fine page containing only the new
    material — which reads as a good edit and is a deletion."""
    model = FakeModel()

    compose_page(_claims("a new fact"), _source(), model,
                 existing="## What it is\n\nThe page already said this.")

    prompt = model.prompts[0]
    assert "The page already said this." in prompt
    assert "do not throw this away" in prompt


def test_a_very_long_page_is_truncated_rather_than_refused():
    """A page can be longer than a context window. Sending nothing would make the
    rewrite destructive in exactly the way the `existing` argument exists to prevent."""
    model = FakeModel()

    compose_page(_claims("a fact"), _source(), model, existing="word " * 4000)

    assert len(model.prompts[0]) < 20_000


def test_a_rewrite_becomes_one_reversible_operation_carrying_what_it_replaces():
    """`replaced` is what the applier snapshots before it runs. Without it the undo has
    nothing to restore and the old blocks are gone."""
    result = ComposeResult("T", "📐", "s", [{"type": "paragraph"}], {"clm_1"}, set())

    op = rewrite_page("pg_1", ["bk_1", "bk_2"], "bk_0", result, claim_id="clm_1")

    assert op.kind is OpKind.REWRITE_SECTION
    assert op.target == "pg_1"
    assert op.payload["replaced"] == ["bk_1", "bk_2"]
    assert op.payload["anchor_block_id"] == "bk_0"
    assert op.relation is Relation.REFINES


def test_a_new_page_becomes_a_create_operation():
    result = ComposeResult("Gradient clipping", "📐", "s",
                           [{"type": "paragraph"}], {"clm_1"}, set())

    op = compose_operation("pg_parent", result, claim_id="clm_1")

    assert op.kind is OpKind.CREATE_PAGE
    assert op.target == "pg_parent"
    assert op.payload["title"] == "Gradient clipping"
    assert op.relation is Relation.NEW


# ---------------------------------------------------------------------------
# reading the page back out
# ---------------------------------------------------------------------------


def test_page_text_reads_the_mirror_in_order(store):
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([
        {"block_id": f"bk_{i}", "page_id": "pg_1", "type": "paragraph",
         "text": f"line {i}", "position": i} for i in (2, 0, 1)])

    assert page_text(store, "pg_1") == "line 0\nline 1\nline 2"


def test_page_text_skips_blocks_with_nothing_in_them():
    """Dividers and images have no text. Kept as blank lines they pad the prompt and
    make the page look sparser than it is."""

    class Store:
        def get_blocks(self, page_id, limit=None):
            return [{"text": "real"}, {"text": "   "}, {"text": None}, {"text": "also"}]

    assert page_text(Store(), "pg_1") == "real\nalso"


def test_page_text_on_a_page_with_no_blocks_is_empty_rather_than_an_error():
    class Store:
        def get_blocks(self, page_id, limit=None):
            return None

    assert page_text(Store(), "pg_1") == ""


@pytest.mark.parametrize("children", [[], [{"type": "paragraph"}]])
def test_ok_requires_both_blocks_and_full_coverage(children):
    with_missing = ComposeResult("T", "i", "s", children, set(), {"clm_1"})
    assert with_missing.ok is False

    complete = ComposeResult("T", "i", "s", children, {"clm_1"}, set())
    assert complete.ok is bool(children)


def test_the_summary_says_what_was_covered_and_what_was_not():
    """This is what the review card shows, and "3 claims, 1 missing" is the difference
    between approving and looking."""
    result = ComposeResult("T", "📐", "s", [{"type": "paragraph"}],
                           {"clm_1", "clm_2"}, {"clm_3"})

    summary = result.as_dict()

    assert summary["claims_covered"] == 2
    assert summary["claims_missing"] == ["clm_3"]
    assert summary["blocks"] == 1


# ---------------------------------------------------------------------------
# citations survive composition
# ---------------------------------------------------------------------------


def _anchored(claim_id: str, text: str, locator: str, url: str | None) -> Claim:
    return Claim(claim_id=claim_id, text=text, type=ClaimType.FACT, topics=("nn",),
                 confidence=0.9, anchor=Anchor("timestamp", locator, 0, 10, url),
                 source_id="src_1")


def _runs(block: dict) -> list[dict]:
    return block[block["type"]]["rich_text"]


def test_a_composed_block_cites_the_moments_its_claims_came_from():
    """The page for a lecture was written with no way back to the lecture: the claim ids
    were the only record of which moment each block came from, and they were stripped."""
    claims = [
        _anchored("clm_1", "A neuron holds a number.", "0:37",
                  "https://www.youtube.com/watch?v=v&t=37s"),
        _anchored("clm_2", "Activations lie between 0 and 1.", "2:05",
                  "https://www.youtube.com/watch?v=v&t=125s"),
    ]
    source = Source(source_id="src_1", kind="youtube", title="Chapter 1", text="x",
                    url="https://www.youtube.com/watch?v=v")

    result = compose_page(claims, source, FakeModel({"title": "Neurons", "blocks": [
        {"type": "paragraph", "text": "A neuron holds an activation between 0 and 1.",
         "claim_ids": ["clm_1", "clm_2"]}]}))

    runs = _runs(result.children[0])
    markers = [(r["text"]["content"].strip(), r["text"]["link"]["url"])
               for r in runs[1:]]
    assert markers == [("[0:37]", "https://www.youtube.com/watch?v=v&t=37s"),
                       ("[2:05]", "https://www.youtube.com/watch?v=v&t=125s")]
    assert all(r["annotations"]["color"] == "gray" for r in runs[1:])


def test_the_page_ends_with_a_linked_line_naming_the_source():
    result = compose_page(_claims("a fact"), _source(), FakeModel())

    closing = result.children[-1]
    assert closing["type"] == "callout"
    run = _runs(closing)[0]
    assert run["text"]["content"] == "Source: A post on clipping"
    assert run["text"]["link"]["url"] == "https://example.com/p"


def test_a_citation_with_nowhere_to_link_is_left_off():
    """A pasted note's locator is "document". Bracketed and unlinked, it is noise."""
    claims = [_anchored("clm_1", "a thought", "document", None)]
    source = Source(source_id="src_1", kind="text", title="Note", text="x", url=None)

    result = compose_page(claims, source, FakeModel({"title": "T", "blocks": [
        {"type": "paragraph", "text": "a thought", "claim_ids": ["clm_1"]}]}))

    assert len(_runs(result.children[0])) == 1
    assert [b["type"] for b in result.children] == ["paragraph"], "and no source line"


def test_a_block_folding_many_moments_carries_at_most_three_markers():
    claims = [_anchored(f"clm_{i}", f"fact {i}", f"{i}:00",
                        f"https://www.youtube.com/watch?v=v&t={i * 60}s") for i in range(8)]
    source = Source(source_id="src_1", kind="youtube", title="T", text="x",
                    url="https://www.youtube.com/watch?v=v")

    result = compose_page(claims, source, FakeModel({"title": "T", "blocks": [
        {"type": "paragraph", "text": "everything", "claim_ids": [c.claim_id for c in claims]}]}))

    assert len(_runs(result.children[0])) == 1 + 3


def test_citation_keys_from_the_model_are_ignored():
    """`_cite` and `_link` turn into links on the page, so they are ours to set. A model
    that emits them, at any depth, gets plain text."""
    result = compose_page(_claims("a fact"), _source(), FakeModel({"title": "T", "blocks": [
        {"type": "toggle", "text": "outer", "claim_ids": ["clm_1"],
         "_link": "https://evil.example",
         "children": [{"type": "paragraph", "text": "inner", "_link": "https://evil.example",
                       "_cite": [{"type": "text", "text": {"content": "x",
                                                          "link": {"url": "https://evil.example"}}}]}]}]}))

    assert "evil.example" not in str(result.children)
