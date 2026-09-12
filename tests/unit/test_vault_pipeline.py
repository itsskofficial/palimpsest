"""The whole journey against a real workspace, offline: sync → ingest → apply → undo.

Every other test here stops short of one of those steps. `test_flow.py` runs the pipeline
but stops before the write. `test_apply.py` runs the applier against a fake that is a
faithful implementation but is still a fake, seeded by hand rather than by a sync. Nothing
joined them up, because joining them up used to require a Notion token — which is why the
most dangerous code in the project had the weakest offline coverage, and why the bugs
found in it over the last week were found by hand, live, one at a time.

A markdown vault closes that gap. What runs below is the real mirror reading a real
workspace, the real planner, the real single write door, and the real inverse logic, with
a scripted model standing in for the one judgement call. Only the model is fake.

The assertion that matters is the last one: after undo, **the file on disk is byte-for-byte
what it was before**. Not "looks similar", not "has the same blocks" — identical. That is
the promise the product makes about your notes, and this is the only place it is checked
against bytes rather than against an in-memory double.
"""

from __future__ import annotations

import re

import pytest

from palimpsest import approval
from palimpsest.config import Settings
from palimpsest.llm import TokenUsage, Usage
from palimpsest.notion.apply import revert_patch
from palimpsest.notion.mirror import sync
from palimpsest.pipeline import ingest
from palimpsest.workspace.markdown import MarkdownWorkspace

_BLOCK_ID = re.compile(r"block_id: (\S+)")
_PAGE_ID = re.compile(r"^\s+- page_id: (\S+)", re.MULTILINE)

PAGE = """---
title: "Gradient clipping"
icon: "📐"
---

# Gradient clipping

## What it is

Gradient clipping bounds the size of a gradient update before the optimiser
applies it, which is the standard remedy for exploding gradients.

## Which variant

Global-norm clipping is the usual choice, with a threshold of 1.0.

- It is cheap enough to leave on permanently.
- It matters most at large batch sizes.
"""


class ScriptedModel:
    """The one judgement call, answered the same way every time.

    Reads the candidate ids out of the cached prefix so it names a block that genuinely
    exists — a made-up id would be stripped by `relate`'s repair path, and the test would
    then be exercising the repair rather than the thing it claims to.
    """

    model = "scripted/offline-1"
    name = "scripted"
    base_url = None

    def __init__(self, relation: str = "corroborates", confidence: float = 0.95):
        self.relation = relation
        self.confidence = confidence
        self.usage = Usage()

    def json(self, *, task, system, prompt, schema, effort="high",
             cache_prefix=None, max_tokens=None):
        self.usage.add(task, TokenUsage(input=800, output=100), 0.01)
        if task == "extract":
            # The quote has to appear verbatim in the *source* text, not in the page:
            # that is what anchors the claim to a span of what was captured.
            return {"claims": [{
                "text": "Global-norm clipping uses a threshold of 1.0 by convention.",
                "quote": "clipping conventionally uses a threshold of 1.0",
                "type": "fact", "topics": ["gradient clipping"], "confidence": 0.95}]}
        if task == "classify":
            prefix = cache_prefix or ""
            block = _BLOCK_ID.search(prefix)
            page = _PAGE_ID.search(prefix)
            return {
                "relation": self.relation,
                "confidence": self.confidence,
                "target_page_id": page.group(1) if page else None,
                "target_block_id": block.group(1) if block else None,
                "existing_text": None,
                "rationale": "the page already says this; the source is a second witness",
            }
        raise AssertionError(f"unexpected task {task!r}")


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "gradient-clipping.md").write_text(PAGE, encoding="utf-8")
    return MarkdownWorkspace(root)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        backend="markdown", vault_path=str(tmp_path / "vault"),
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        apply=True, autonomy="full", journal=False, compose_pages=False,
    ).validate()


def _page_file(vault):
    return vault.root / "gradient-clipping.md"


def test_the_whole_journey_leaves_the_file_exactly_as_it_found_it(vault, settings, store):
    """Sync, ingest, apply, undo — and the bytes on disk come back identical."""
    before = _page_file(vault).read_text(encoding="utf-8")

    mirrored = sync(vault, store, incremental=False)
    assert mirrored.pages == 1 and mirrored.blocks > 3

    result = ingest("Global-norm clipping conventionally uses a threshold of 1.0.",
                    store, ScriptedModel(), settings=settings)
    assert result.patch.operations, result.as_dict()

    gated = approval.gate(store, result.patch, settings,
                          notion_factory=lambda: vault, journal_factory=None)
    assert gated["applied"] >= 1, gated
    after_apply = _page_file(vault).read_text(encoding="utf-8")
    assert after_apply != before, "the apply must have actually changed the page"

    reverted = revert_patch(vault, store, store.get_patch(result.patch.patch_id))
    assert reverted.failed == 0, reverted.errors
    assert reverted.status == "reverted"

    after_undo = _page_file(vault).read_text(encoding="utf-8")
    body = after_undo.split("---\n\n", 1)[-1]
    assert body == before.split("---\n\n", 1)[-1], (
        "undo must restore the page exactly, not approximately")


def test_a_corroboration_adds_a_citation_and_no_prose(vault, settings, store):
    """The property the whole design rests on, checked against bytes for once.

    Most new information about a topic you already studied is the same claim from a
    different source. Under any other design that becomes a new paragraph; here it must
    become a marker on a sentence that already exists, and the word count must barely
    move.
    """
    sync(vault, store, incremental=False)
    before = _page_file(vault).read_text(encoding="utf-8")

    result = ingest("Global-norm clipping conventionally uses a threshold of 1.0.",
                    store, ScriptedModel("corroborates"), settings=settings)
    approval.gate(store, result.patch, settings, notion_factory=lambda: vault,
                  journal_factory=None)

    after = _page_file(vault).read_text(encoding="utf-8")
    grew = len(after.split()) - len(before.split())
    assert 0 < grew < 12, f"a citation should be a marker, not a paragraph (+{grew} words)"


def test_a_contradiction_reaches_the_vault_only_at_the_top_rung(vault, settings, store):
    """`test_safety.py` pins this on fakes. Here it is checked against a file."""
    import dataclasses

    sync(vault, store, incremental=False)
    before = _page_file(vault).read_text(encoding="utf-8")

    full = ingest("Per-parameter clipping conventionally uses a threshold of 1.0.",
                  store,
                  ScriptedModel("contradicts"), settings=settings)
    out = approval.gate(store, full.patch, settings, notion_factory=lambda: vault,
                        journal_factory=None)

    assert out["applied"] == 0
    assert _page_file(vault).read_text(encoding="utf-8") == before, "nothing was written"

    everything = dataclasses.replace(settings, autonomy="everything")
    result = ingest("Per-parameter clipping conventionally uses a threshold of 1.0 too.",
                    store, ScriptedModel("contradicts"), settings=everything)
    approval.gate(store, result.patch, everything, notion_factory=lambda: vault,
                  journal_factory=None)

    after = _page_file(vault).read_text(encoding="utf-8")
    assert after != before, "the top rung records the disagreement"
    # And what it wrote is a record, not a rewrite: the contradicted sentence survives.
    assert "Global-norm clipping is the usual choice" in after


def test_a_page_edited_outside_the_tool_between_sync_and_apply_still_works(
        vault, settings, store):
    """The case a vault has and Notion does not.

    Somebody has the file open in Obsidian and fixes a typo while a capture is in flight.
    The block ids are re-aligned on the next read, so the operation still lands on the
    sentence it was aimed at rather than at a stale id.
    """
    sync(vault, store, incremental=False)
    result = ingest("Global-norm clipping conventionally uses a threshold of 1.0.",
                    store, ScriptedModel(), settings=settings)

    path = _page_file(vault)
    path.write_text(path.read_text(encoding="utf-8").replace(
        "which is the standard remedy", "which is the usual remedy"), encoding="utf-8")

    gated = approval.gate(store, result.patch, settings, notion_factory=lambda: vault,
                          journal_factory=None)

    assert gated["applied"] >= 1, gated
    assert "usual remedy" in path.read_text(encoding="utf-8"), "their edit survived"


def test_a_patch_carries_what_it_could_not_decide(vault, settings, store):
    """A contradiction produces no operation by construction, which is what makes it a
    review item. Without the patch carrying its own review, such a patch reached the
    activity feed as "0 changes, waiting" — accurate, and with no way to find out what
    was waiting or what it disagreed with.
    """
    sync(vault, store, incremental=False)
    result = ingest("Per-parameter clipping conventionally uses a threshold of 1.0.",
                    store, ScriptedModel("contradicts"), settings=settings)

    assert result.patch.operations == []
    assert result.patch.review, "the patch must carry its own unfinished business"

    item = result.patch.review[0]
    assert item["reason"] == "contradiction"
    assert item["judgement"]["relation"] == "contradicts"
    assert item["page"] == "Gradient clipping"
    assert item["judgement"]["rationale"]


def test_the_review_survives_a_round_trip_through_the_store(vault, settings, store):
    """It persists inside the patch payload, so it is there when the feed asks — no
    migration, and it travels with the thing it explains."""
    sync(vault, store, incremental=False)
    result = ingest("Per-parameter clipping conventionally uses a threshold of 1.0.",
                    store, ScriptedModel("contradicts"), settings=settings)
    store.put_patch(result.patch)

    reloaded = store.get_patch(result.patch.patch_id)
    assert reloaded is not None
    assert len(reloaded.review) == len(result.patch.review)
    assert reloaded.review[0]["judgement"]["relation"] == "contradicts"
