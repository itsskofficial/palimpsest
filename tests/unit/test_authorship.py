"""Authorship: the agent may rewrite a page, and the page comes back exactly.

Every other operation in this system is small enough to read as a diff, and that was the
stated reason they were the only ones allowed. `REWRITE_SECTION` gives that up — it
replaces a run of blocks wholesale, because a knowledge base worth reading needs sections
restructured and no amount of sentence-level editing produces that.

What it does *not* give up is recoverability, and that distinction is the whole point of
this file. "Small" and "reversible" were being treated as one property; they are two. A
rewrite is reversible because the blocks it replaces are snapshotted before it runs, so
these tests pin the snapshot, the ordering, and the two ways a rewrite is refused.

The one live finding behind the design: Notion restores a trashed block **at the end of
the page**, not where it was. Undoing a rewrite by un-trashing would therefore hand back
the right paragraphs in the wrong order — silently, and only visibly on a page long
enough to notice. So the inverse re-creates from the snapshot instead, which costs new
block ids and keeps the reading order.
"""

from __future__ import annotations

import pytest

from palimpsest.notion import blocks as B
from palimpsest.notion.apply import apply_patch, revert_patch
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

# ---------------------------------------------------------------------------
# a Notion that behaves like the real one, including the ordering trap
# ---------------------------------------------------------------------------


class FakeNotion:
    """Enough of Notion to test ordering, which is the property under test.

    Faithful about the thing that matters: `archive_block` removes a block from the page
    and `restore_block` puts it *back at the end*, exactly as the live API does.
    """

    def __init__(self, page_id: str, blocks: list[dict]):
        self.pages: dict[str, list[dict]] = {page_id: list(blocks)}
        self.trash: dict[str, dict] = {}
        self.covers: dict[str, str | None] = {}

    def _page_of(self, block_id: str) -> str | None:
        for page, blocks in self.pages.items():
            if any(b["id"] == block_id for b in blocks):
                return page
        return None

    def append_children(self, parent_id, children, after_block_id=None):
        made = [{"id": new_id("nb_"), "type": c.get("type", "paragraph"), **c}
                for c in children]
        page = parent_id if parent_id in self.pages else self._page_of(parent_id)
        blocks = self.pages.setdefault(page or parent_id, [])
        if after_block_id:
            index = next((i for i, b in enumerate(blocks)
                          if b["id"] == after_block_id), len(blocks) - 1) + 1
        else:
            index = 0
        blocks[index:index] = made
        return {"results": made}

    def archive_block(self, block_id):
        for blocks in self.pages.values():
            for i, b in enumerate(blocks):
                if b["id"] == block_id:
                    self.trash[block_id] = blocks.pop(i)
                    return {"id": block_id}
        return {"id": block_id}

    def restore_block(self, block_id):
        block = self.trash.pop(block_id, None)
        if block is not None:
            # The trap: Notion puts it back at the END, not where it was.
            next(iter(self.pages.values())).append(block)
        return {"id": block_id}

    def update_block(self, block_id, payload):
        return {"id": block_id}

    def set_page_cover(self, page_id, url):
        self.covers[page_id] = url
        return {"id": page_id}

    def texts(self, page_id: str) -> list[str]:
        out = []
        for b in self.pages[page_id]:
            body = b.get(b.get("type", "paragraph")) or {}
            out.append(B.plain_text(body.get("rich_text")))
        return out


PAGE = "pg_1"
ORIGINAL = ["First sentence.", "Second sentence.", "Third sentence."]


@pytest.fixture()
def wired(store):
    """A page of three paragraphs, in the store and in the fake Notion."""
    store.put_pages([{"page_id": PAGE, "title": "Attention", "last_edited": "x"}])
    rows, live = [], []
    for i, text in enumerate(ORIGINAL):
        bid = f"bk_{i}"
        raw = {"id": bid, "type": "paragraph",
               "paragraph": {"rich_text": [{"type": "text", "text": {"content": text},
                                            "plain_text": text}]},
               "has_children": False, "created_time": "t", "archived": False}
        rows.append({"block_id": bid, "page_id": PAGE, "type": "paragraph",
                     "position": i, "text": text, "raw": raw})
        live.append(raw)
    store.put_blocks(rows)
    return FakeNotion(PAGE, live)


def _rewrite(children: list[dict], replaced: list[str]) -> Patch:
    op = Operation(
        kind=OpKind.REWRITE_SECTION, target=PAGE, relation=Relation.REFINES,
        payload={"children": children, "replaced": replaced, "anchor_block_id": None,
                 "title": "Attention", "rationale": "laid out properly"})
    return Patch(patch_id=new_id("pch_"), source_id="src_1", operations=[op])


NEW_BODY = [
    {"object": "block", "type": "heading_2",
     "heading_2": {"rich_text": B.rich_text("What it does")}},
    {"object": "block", "type": "paragraph",
     "paragraph": {"rich_text": B.rich_text("One paragraph saying all three things.")}},
]


# ---------------------------------------------------------------------------
# the operation
# ---------------------------------------------------------------------------


def test_a_rewrite_replaces_the_section_and_keeps_the_rest(wired, store):
    patch = _rewrite(NEW_BODY, ["bk_0", "bk_1"])

    result = apply_patch(wired, store, patch, reviewer="test")

    assert result.applied == 1
    assert wired.texts(PAGE) == ["What it does",
                                 "One paragraph saying all three things.",
                                 "Third sentence."]


def test_a_rewrite_snapshots_what_it_replaces(wired, store):
    """The snapshot is taken *before* the blocks are archived, because afterwards their
    content is no longer readable from anywhere."""
    patch = _rewrite(NEW_BODY, ["bk_0", "bk_1"])

    apply_patch(wired, store, patch, reviewer="test")

    inverse = patch.operations[0].inverse
    assert inverse is not None
    assert inverse["kind"] == "restore_section"
    restored = [B.plain_text((b.get(b["type"]) or {}).get("rich_text"))
                for b in inverse["payload"]["snapshot"]]
    assert restored == ["First sentence.", "Second sentence."]
    assert len(inverse["payload"]["created_block_ids"]) == len(NEW_BODY)


def test_undoing_a_rewrite_restores_the_page_in_order(wired, store):
    """The finding that shaped the design.

    Notion returns a restored block to the end of the page, so an undo built on
    un-trashing would give back the right paragraphs in the wrong order — a corruption
    nobody would attribute to the undo. Rebuilding from the snapshot is what keeps the
    reading order, and reading order is what a person notices.
    """
    patch = _rewrite(NEW_BODY, ["bk_0", "bk_1"])
    apply_patch(wired, store, patch, reviewer="test")
    assert wired.texts(PAGE) != ORIGINAL

    revert_patch(wired, store, patch, reviewer="test")

    assert wired.texts(PAGE) == ORIGINAL


def test_undoing_a_whole_page_rewrite_restores_every_block(wired, store):
    patch = _rewrite(NEW_BODY, ["bk_0", "bk_1", "bk_2"])
    apply_patch(wired, store, patch, reviewer="test")
    assert wired.texts(PAGE) == ["What it does",
                                 "One paragraph saying all three things."]

    revert_patch(wired, store, patch, reviewer="test")

    assert wired.texts(PAGE) == ORIGINAL


def test_a_rewrite_with_no_replacement_content_is_refused(wired, store):
    """An empty rewrite would archive the section and put nothing back — a page silently
    emptied by an operation that reported success."""
    patch = _rewrite([], ["bk_0", "bk_1"])

    result = apply_patch(wired, store, patch, reviewer="test")

    assert result.applied == 0
    assert result.failed == 1
    assert wired.texts(PAGE) == ORIGINAL


def test_the_new_content_lands_before_the_old_is_removed(wired, store):
    """Order of operations, not order of blocks.

    Archiving first would leave the page visibly empty for the duration of the call, and
    permanently empty if the append then failed. Appending first means the worst case is
    a page with both versions on it, which is recoverable by reading.
    """
    seen: list[str] = []
    original_append = wired.append_children
    original_archive = wired.archive_block
    wired.append_children = lambda *a, **k: (seen.append("append"),
                                             original_append(*a, **k))[1]
    wired.archive_block = lambda *a, **k: (seen.append("archive"),
                                           original_archive(*a, **k))[1]

    apply_patch(wired, store, _rewrite(NEW_BODY, ["bk_0"]), reviewer="test")

    assert seen[0] == "append"
    assert "archive" in seen


# ---------------------------------------------------------------------------
# the author vocabulary
# ---------------------------------------------------------------------------


def test_the_spec_builds_the_blocks_a_real_page_needs():
    built = B.blocks_from_spec([
        {"type": "heading_2", "text": "Why"},
        {"type": "paragraph", "text": "Because."},
        {"type": "callout", "text": "Careful", "icon": "⚠️"},
        {"type": "code", "text": "x = 1", "language": "python"},
        {"type": "divider"},
    ])
    assert [b["type"] for b in built] == [
        "heading_2", "paragraph", "callout", "code", "divider"]
    assert built[3]["code"]["language"] == "python"


def test_an_unknown_block_type_costs_that_block_and_not_the_page():
    """One unsupported block in a page of thirty should not fail the whole patch."""
    built = B.blocks_from_spec([
        {"type": "paragraph", "text": "kept"},
        {"type": "nonsense", "text": "dropped"},
        {"type": "paragraph", "text": "also kept"},
    ])
    assert len(built) == 2


def test_a_spec_that_builds_nothing_is_an_error_rather_than_an_empty_page():
    with pytest.raises(B.SpecError):
        B.blocks_from_spec([{"type": "nonsense"}])


def test_media_must_be_a_web_url():
    """A `file://` URL would either fail at Notion or ask it to fetch something off the
    machine this happens to be running on."""
    assert B.blocks_from_spec([{"type": "image", "url": "https://x/y.png"},
                               {"type": "paragraph", "text": "k"}])[0]["type"] == "image"
    built = B.blocks_from_spec([{"type": "image", "url": "file:///etc/passwd"},
                                {"type": "paragraph", "text": "k"}])
    assert [b["type"] for b in built] == ["paragraph"]


def test_a_code_block_with_an_invented_language_still_applies():
    """A wrong highlight is cosmetic; a rejected patch is not."""
    built = B.blocks_from_spec([{"type": "code", "text": "x", "language": "klingon"}])
    assert built[0]["code"]["language"] == "plain text"


def test_a_single_column_layout_is_rejected_before_notion_sees_it():
    """Notion requires at least two columns and says so at apply time, which would be
    halfway through a rewrite."""
    built = B.blocks_from_spec([
        {"type": "column_list", "children": [
            {"type": "column", "children": [{"type": "paragraph", "text": "only"}]}]},
        {"type": "paragraph", "text": "kept"},
    ])
    assert [b["type"] for b in built] == ["paragraph"]


def test_the_spec_cannot_expand_without_limit():
    """A model emitting a self-similar structure should cost a bounded number of blocks,
    not a page with thousands."""
    deep: dict = {"type": "toggle", "text": "t"}
    node = deep
    for _ in range(12):
        child: dict = {"type": "toggle", "text": "t"}
        node["children"] = [child]
        node = child

    built = B.blocks_from_spec([deep] * 400)
    assert len(built) <= B.MAX_BLOCKS


def test_strip_readonly_drops_the_fields_notion_will_not_accept_back():
    """A block's own response cannot be fed straight back to create it again.

    Notion *returns* `"icon": null` on a paragraph and then rejects that same field on
    create — "should be an object or `undefined`, instead was `null`". This is not
    hypothetical: it is what made the first live undo of a rewrite fail, leaving the page
    rewritten and the original four paragraphs in the trash. Explicit nulls go.
    """
    raw = {"id": "bk_1", "type": "paragraph", "created_time": "t",
           "last_edited_time": "t", "has_children": False, "archived": False,
           "parent": {"page_id": "p"},
           "paragraph": {"rich_text": B.rich_text("hello"), "color": "default",
                         "icon": None, "children": [{"type": "paragraph"}]}}

    rebuilt = B.strip_readonly(raw)

    assert rebuilt["object"] == "block"
    assert rebuilt["type"] == "paragraph"
    assert set(rebuilt["paragraph"]) == {"rich_text", "color"}
    assert B.plain_text(rebuilt["paragraph"]["rich_text"]) == "hello"
    # Server-owned fields never make it into the rebuilt block.
    assert not {"id", "created_time", "has_children", "parent"} & set(rebuilt)


def test_strip_readonly_drops_nested_nulls_too():
    """The null that broke it was one level down, inside the block body."""
    raw = {"type": "callout", "callout": {
        "rich_text": [{"type": "text", "text": {"content": "x", "link": None},
                       "href": None}],
        "icon": {"type": "emoji", "emoji": "💡"}, "color": None}}

    rebuilt = B.strip_readonly(raw)

    assert "color" not in rebuilt["callout"]
    assert "link" not in rebuilt["callout"]["rich_text"][0]["text"]
    assert "href" not in rebuilt["callout"]["rich_text"][0]
    assert rebuilt["callout"]["icon"]["emoji"] == "💡"


# ---------------------------------------------------------------------------
# the mirror after an undo
# ---------------------------------------------------------------------------


def test_undoing_refreshes_the_mirror_like_applying_does(wired, store, monkeypatch):
    """An undo changes the workspace as much as the thing it undid.

    `apply_patch` pulls the touched pages back into the mirror; `revert_patch` did not.
    So the blocks an undo removed stayed in the mirror as live retrieval candidates —
    the classifier would corroborate one, and the apply would fail with "Can't edit
    block that is archived", about a block the user deleted minutes ago and has no
    reason to connect to what they are doing now. Found live, twice, before it was
    traced to the missing half of a pair.
    """
    from palimpsest.notion import apply as apply_mod

    refreshed: list[str] = []
    monkeypatch.setattr(apply_mod, "_refresh_mirror",
                        lambda client, st, patch: refreshed.append(patch.patch_id))

    patch = _rewrite(NEW_BODY, ["bk_0", "bk_1"])
    apply_patch(wired, store, patch, reviewer="test")
    assert refreshed == [patch.patch_id]

    revert_patch(wired, store, patch, reviewer="test")
    assert refreshed == [patch.patch_id, patch.patch_id]


def test_the_refresh_still_finds_the_page_after_applied_at_is_cleared(wired, store):
    """`revert_patch` clears `applied_at`, so a refresh that filtered on it would be a
    no-op on precisely the path that needs it."""
    from palimpsest.notion.apply import _refresh_mirror

    patch = _rewrite(NEW_BODY, ["bk_0"])
    apply_patch(wired, store, patch, reviewer="test")
    revert_patch(wired, store, patch, reviewer="test")
    assert patch.operations[0].applied_at is None

    seen: list[list[str]] = []

    class Recording:
        def get_page(self, pid):
            return None

    import palimpsest.notion.mirror as mirror_mod

    original = mirror_mod.refresh_pages
    mirror_mod.refresh_pages = lambda client, st, ids, **kw: seen.append(list(ids))
    try:
        _refresh_mirror(wired, store, patch)
    finally:
        mirror_mod.refresh_pages = original

    assert seen and PAGE in seen[0]
