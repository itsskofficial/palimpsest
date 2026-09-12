"""The applier's contract: inverse-first, never delete, stop on failure, undo exactly.

These run against a fake Notion that implements the eight methods the real client
exposes. It is not a mock that records calls — appending really returns new block ids,
updating really replaces rich_text, archiving is really reversible. If the inverse logic
is wrong, `undo` fails here exactly as it would against a real workspace.
"""

from __future__ import annotations

import pytest

from palimpsest.notion.apply import apply_patch, revert_patch
from palimpsest.notion.client import NotionError
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id


class FakeNotion:
    def __init__(self, fail_on: int | None = None):
        self.blocks: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.calls = 0
        self.fail_on = fail_on

    def _maybe_fail(self):
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise NotionError(500, "server_error", "injected failure")

    def append_children(self, parent_id, children, after_block_id=None):
        self._maybe_fail()
        created = []
        for child in children:
            bid = new_id("nb_")
            self.blocks[bid] = {**child, "id": bid, "archived": False}
            created.append({"id": bid, "type": child.get("type", "paragraph")})
        return {"results": created}

    def update_block(self, block_id, payload):
        self._maybe_fail()
        block = self.blocks.setdefault(block_id, {"type": "paragraph", "id": block_id})
        if "archived" in payload:
            block["archived"] = payload["archived"]
            return block
        for key, value in payload.items():
            block[key] = value
            block["type"] = key
        return block

    def archive_block(self, block_id):
        return self.update_block(block_id, {"archived": True})

    def restore_block(self, block_id):
        return self.update_block(block_id, {"archived": False})

    def create_page(self, parent_page_id, title, children=None, icon=None):
        self._maybe_fail()
        pid = new_id("np_")
        self.pages[pid] = {"id": pid, "title": title}
        return {"id": pid, "url": f"https://notion.so/{pid}"}

    def archive_page(self, page_id):
        self._maybe_fail()
        self.pages.pop(page_id, None)
        return {"id": page_id, "archived": True}

    @property
    def live_blocks(self) -> int:
        return len([b for b in self.blocks.values() if not b.get("archived")])


@pytest.fixture()
def seeded(store):
    store.put_pages([{"page_id": "pg_1", "title": "Page", "role": "reference",
                      "last_edited": "2026-01-01"}])
    store.put_blocks([{
        "block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
        "text": "the original text", "position": 0,
        "raw": {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "the original text"},
             "annotations": {"bold": True}}]}},
    }])
    return store


def _patch(*ops: Operation) -> Patch:
    return Patch(patch_id=new_id("pch_"), source_id="src_1", operations=list(ops))


# ---------------------------------------------------------------------------


def test_every_block_creating_operation_gets_an_inverse(seeded):
    """The bug this test exists for: footnotes and links append blocks too.

    `append_block` was the only creator whose inverse was stamped from the response, so
    footnote and link operations applied fine and then silently could not be undone.
    """
    notion = FakeNotion()
    patch = _patch(
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1",
                  payload={"text": "new claim"}, relation=Relation.NEW),
        Operation(kind=OpKind.INSERT_FOOTNOTE, target="bk_1",
                  payload={"text": "refined from", "source_title": "src",
                           "parent_page_id": "pg_1"}, relation=Relation.REFINES),
        Operation(kind=OpKind.LINK_PAGES, target="pg_1",
                  payload={"label": "see also"}, relation=Relation.DUPLICATE),
    )

    apply_patch(notion, seeded, patch)
    for op in patch.operations:
        assert op.inverse is not None, f"{op.kind.value} has no inverse"
        assert op.inverse["kind"] == "archive_blocks"


def test_undo_restores_the_exact_previous_rich_text(seeded):
    """Not a re-rendered approximation — bold and links must survive a round trip."""
    notion = FakeNotion()
    patch = _patch(Operation(kind=OpKind.UPDATE_TEXT, target="bk_1",
                             payload={"block_type": "paragraph", "text": "replaced"},
                             relation=Relation.REFINES))

    apply_patch(notion, seeded, patch)
    assert notion.blocks["bk_1"]["paragraph"]["rich_text"][0]["text"]["content"] == "replaced"

    revert_patch(notion, seeded, patch)
    restored = notion.blocks["bk_1"]["paragraph"]["rich_text"][0]
    assert restored["text"]["content"] == "the original text"
    assert restored["annotations"]["bold"] is True, "formatting must survive undo"


def test_undo_archives_every_block_the_patch_created(seeded):
    notion = FakeNotion()
    patch = _patch(
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "a"},
                  relation=Relation.NEW),
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "b"},
                  relation=Relation.NEW),
    )
    apply_patch(notion, seeded, patch)
    assert notion.live_blocks == 2

    result = revert_patch(notion, seeded, patch)
    assert result.applied == 2
    assert notion.live_blocks == 0


def test_a_failure_stops_the_patch_and_leaves_it_partial(seeded):
    """Ploughing on would produce a page in a state nobody chose."""
    notion = FakeNotion(fail_on=2)
    patch = _patch(
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "first"},
                  relation=Relation.NEW),
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "second"},
                  relation=Relation.NEW),
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "third"},
                  relation=Relation.NEW),
    )
    result = apply_patch(notion, seeded, patch)

    assert result.applied == 1
    assert result.failed == 1
    assert result.status == "partial"
    assert notion.live_blocks == 1, "the third operation must not have run"


def test_a_partial_patch_undoes_exactly_the_part_that_landed(seeded):
    notion = FakeNotion(fail_on=2)
    patch = _patch(
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "first"},
                  relation=Relation.NEW),
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "second"},
                  relation=Relation.NEW),
    )
    apply_patch(notion, seeded, patch)
    notion.fail_on = None

    result = revert_patch(notion, seeded, patch)
    assert result.applied == 1
    assert result.skipped == 1
    assert notion.live_blocks == 0


def test_dry_run_builds_inverses_and_writes_nothing(seeded):
    notion = FakeNotion()
    patch = _patch(Operation(kind=OpKind.UPDATE_TEXT, target="bk_1",
                             payload={"text": "replaced"}, relation=Relation.REFINES))
    result = apply_patch(notion, seeded, patch, dry_run=True)

    assert result.applied == 1
    assert notion.calls == 0, "a dry run must not touch the API"
    assert patch.operations[0].inverse is not None
    assert not patch.operations[0].applied


def test_supersede_strikes_through_and_never_archives(seeded):
    notion = FakeNotion()
    patch = _patch(Operation(kind=OpKind.STRIKE_BLOCK, target="bk_1",
                             payload={"text": "the original text"},
                             relation=Relation.SUPERSEDES))
    apply_patch(notion, seeded, patch)

    block = notion.blocks["bk_1"]
    assert block.get("archived") is not True
    assert block["paragraph"]["rich_text"][0]["annotations"]["strikethrough"] is True


def test_provenance_is_recorded_for_created_blocks(seeded):
    notion = FakeNotion()
    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "a claim"},
                   claim_id="clm_1", relation=Relation.NEW)
    patch = _patch(op)
    apply_patch(notion, seeded, patch)

    created = op.result["created_block_ids"][0]
    rows = seeded.provenance_for_block(created)
    assert rows and rows[0]["claim_id"] == "clm_1"
    assert rows[0]["relation"] == "new"


def test_applied_operations_land_in_the_ledger(seeded):
    notion = FakeNotion()
    patch = _patch(Operation(kind=OpKind.APPEND_BLOCK, target="pg_1",
                             payload={"text": "a"}, relation=Relation.NEW))
    apply_patch(notion, seeded, patch, reviewer="tester")

    history = seeded.page_history("pg_1")
    assert len(history) == 1
    assert history[0]["kind"] == "append_block"
    assert history[0]["inverse"]["kind"] == "archive_blocks"


class CascadingNotion(FakeNotion):
    """A fake that archives like the real thing: parents take their children with them.

    Two behaviours the plain fake does not have, both of which the real API has.
    `append_children` reports the nested blocks it created as well as the top-level ones,
    so a footnote toggle and the line inside it both end up in `created_block_ids`. And
    archiving a block archives its subtree, after which Notion refuses to edit any block
    in it: "Can't edit block that is archived".
    """

    def __init__(self):
        super().__init__()
        self.children: dict[str, list[str]] = {}

    def append_children(self, parent_id, children, after_block_id=None):
        self._maybe_fail()
        created = []

        def add(child, parent):
            bid = new_id("nb_")
            self.blocks[bid] = {**child, "id": bid, "archived": False}
            self.children.setdefault(parent, []).append(bid)
            created.append({"id": bid, "type": child.get("type", "paragraph")})
            nested = (child.get(child.get("type", ""), {}) or {}).get("children") or []
            for grandchild in nested:
                add(grandchild, bid)

        for child in children:
            add(child, parent_id)
        return {"results": created}

    def archive_block(self, block_id):
        if self.blocks.get(block_id, {}).get("archived"):
            raise NotionError(400, "validation_error",
                              "Can't edit block that is archived. "
                              "You must unarchive the block before editing.")
        for child in self.children.get(block_id, []):
            self.blocks[child]["archived"] = True
        return self.update_block(block_id, {"archived": True})


def test_undo_finishes_when_archiving_a_parent_already_took_its_child(seeded):
    """The undo that got stuck halfway.

    The agent lays out a page, so the blocks it creates nest: a toggle with lines inside
    it, a bulleted list under a heading. Every id it created is recorded, parents and
    children alike, and the inverse archives them in order. Archiving the toggle archives
    its children with it, and the next call is then refused -- "Can't edit block that is
    archived".

    That refusal is the state the inverse was asking for, but it was raised as a failure.
    So the patch came back `partial`, the Activity row stayed "partly undone", and a user
    looking at a page whose rewrite had in fact been fully reverted was told the undo had
    not worked.

    Nothing here is retried or forced: an "already archived" error is read as success,
    and any other error still stops the undo -- which is the next test.
    """
    notion = CascadingNotion()
    toggle = {"type": "toggle", "toggle": {
        "rich_text": [{"type": "text", "text": {"content": "Sources"}}],
        "children": [{"type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "one citation"}}]}}]}}
    op = Operation(kind=OpKind.REWRITE_SECTION, target="pg_1",
                   payload={"children": [toggle], "replaced": ["bk_1"],
                            "anchor_block_id": None},
                   relation=Relation.REFINES)
    patch = _patch(op)
    apply_patch(notion, seeded, patch)

    created = op.result["created_block_ids"]
    assert len(created) > 1, "expected a toggle and the block inside it"
    assert notion.blocks[created[0]]["archived"] is False

    result = revert_patch(notion, seeded, patch)

    assert result.failed == 0, result.errors
    assert result.status == "reverted", result.errors
    for bid in created:
        assert notion.blocks[bid]["archived"] is True
    # The old content is back -- rebuilt from the snapshot under a new id rather than
    # un-trashed, because Notion returns a restored block to the end of the page.
    live = [b for b in notion.blocks.values() if not b.get("archived")]
    assert any("the original text" in str(b) for b in live)


def test_an_unrelated_failure_still_stops_an_undo(seeded):
    """The other half of the tolerance: only 'already archived' is forgiven."""
    notion = CascadingNotion()
    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_1",
                   payload={"text": "a claim"}, relation=Relation.NEW)
    patch = _patch(op)
    apply_patch(notion, seeded, patch)

    def boom(block_id):
        raise NotionError(500, "server_error", "upstream is having a day")

    notion.archive_block = boom
    result = revert_patch(notion, seeded, patch)

    assert result.failed == 1
    assert result.status != "reverted"
    assert any("upstream" in str(e) for e in result.errors)
