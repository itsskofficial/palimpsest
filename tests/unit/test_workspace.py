"""The markdown backend, and the promise that it is interchangeable with Notion.

Two kinds of test here, and the second kind is the point.

The first kind checks the obvious: markdown parses to blocks, blocks render to markdown,
a round trip changes nothing. Worth pinning, unglamorous.

The second kind checks *parity* — that this backend behaves like Notion where the
pipeline depends on Notion's behaviour, including where that behaviour is awkward. A
restored block comes back at the end of the page. Archiving a block that is already
archived is an error containing the word "archived". `append_children` reports nested
descendants, not just the blocks you passed. Each of those is a real Notion quirk that
the applier's inverse logic was written around, and a backend that quietly did the
*nicer* thing would let a bug back into the Notion path with every test still green.
"""

from __future__ import annotations

import json

import pytest

from palimpsest.notion.client import NotionClient, NotionError
from palimpsest.workspace import check, describe
from palimpsest.workspace import open as open_workspace
from palimpsest.workspace.convert import md_to_rich, rich_to_md, to_blocks, to_markdown
from palimpsest.workspace.markdown import MarkdownWorkspace


@pytest.fixture()
def vault(tmp_path):
    return MarkdownWorkspace(tmp_path / "vault")


def _text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content}}]


def _para(content: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _text(content)}}


# ---------------------------------------------------------------------------
# the two backends are interchangeable
# ---------------------------------------------------------------------------


def test_both_backends_implement_the_whole_interface():
    """The contract that makes a second backend possible at all.

    Not a formal `isinstance` check: `NotionClient` predates the protocol and takes extra
    optional arguments on a couple of methods, so it is a structural superset rather than
    an exact match. What must hold is that every name the pipeline calls exists on both.
    """
    assert check(NotionClient("ntn_x")) == []
    assert check(MarkdownWorkspace("./nowhere-in-particular")) == []


def test_the_factory_returns_the_backend_the_settings_name(tmp_path):
    from palimpsest.config import Settings

    md = open_workspace(Settings(backend="markdown", vault_path=str(tmp_path / "v")))
    assert isinstance(md, MarkdownWorkspace)

    notion = open_workspace(Settings(backend="notion", notion_token="ntn_x"))
    assert isinstance(notion, NotionClient)

    assert "markdown vault" in describe(
        Settings(backend="markdown", vault_path=str(tmp_path)))


def test_a_markdown_vault_needs_somewhere_to_live():
    from palimpsest.config import Settings

    with pytest.raises(ValueError, match="PALIMPSEST_VAULT"):
        Settings(backend="markdown").validate()
    with pytest.raises(ValueError, match="not a backend"):
        Settings(backend="roam").validate()


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------


SAMPLE = """# Gradient clipping

Prose with **bold**, *italic*, `code`, ~~struck~~ and a [link](https://example.com).

- one
  - nested
  - also nested
- two

1. first
1. second

- [x] done
- [ ] not done

> [!note] A callout
> with a second line

> [!tip]- A folded toggle
> holding something

```python
x = 1
```

---

![a caption](https://example.com/i.png)"""


def test_markdown_survives_a_round_trip_unchanged():
    """Byte-for-byte, across every block type the converter claims to support.

    Stronger than "no data lost" on purpose. A converter that is merely lossless can
    still churn the file — reflowing lists, moving blank lines — and a vault that rewrites
    itself on every sync is unusable with git and miserable in Obsidian.
    """
    once = to_markdown(to_blocks(SAMPLE))
    assert once == SAMPLE
    assert to_markdown(to_blocks(once)) == once


@pytest.mark.parametrize("line", [
    "plain words",
    "**bold**", "*italic*", "`code`", "~~struck~~",
    "[label](https://example.com)",
    "mixed **bold** and *italic* and `code`",
])
def test_inline_formatting_survives(line):
    """Not cosmetic. `strike_payload` marks a superseded claim by setting
    `strikethrough`, so a backend that lost annotations would lose the difference between
    "this was true" and "this is true" — and undo would restore the wrong thing."""
    assert rich_to_md(md_to_rich(line)) == line


def test_structure_survives_not_just_text():
    blocks = to_blocks(SAMPLE)
    kinds = [b["type"] for b in blocks]
    assert kinds.count("heading_1") == 1
    assert "to_do" in kinds and "code" in kinds and "divider" in kinds
    assert "callout" in kinds and "toggle" in kinds and "image" in kinds

    bullets = [b for b in blocks if b["type"] == "bulleted_list_item"]
    assert len(bullets) == 2, "the two nested ones are children, not siblings"
    assert len(bullets[0]["bulleted_list_item"]["children"]) == 2, "nesting survived"

    todos = [b for b in blocks if b["type"] == "to_do"]
    assert [t["to_do"]["checked"] for t in todos] == [True, False]


def test_an_unknown_block_degrades_to_its_text_rather_than_vanishing():
    """A vault must never silently lose a page's content just because it cannot
    reproduce that block's chrome."""
    exotic = [{"type": "synced_block",
               "synced_block": {"rich_text": _text("borrowed prose")}}]
    assert "borrowed prose" in to_markdown(exotic)


# ---------------------------------------------------------------------------
# block identity
# ---------------------------------------------------------------------------


def test_block_ids_are_stable_across_reads(vault):
    page = vault.create_page("", "Stability", children=[_para("one"), _para("two")])
    first = [b["id"] for b in vault.block_children(page["id"])]
    second = [b["id"] for b in vault.block_children(page["id"])]
    assert first == second and all(first)


def test_block_ids_survive_an_edit_made_outside_this_tool(vault):
    """The case that makes an Obsidian vault usable rather than merely writable.

    Someone opens the vault in their editor and fixes a typo in the second paragraph.
    Nothing about that edit went through us. The ids still have to line up afterwards,
    or every footnote pointing at that block is orphaned and its provenance is lost.
    """
    page = vault.create_page("", "Edited elsewhere",
                             children=[_para("first"), _para("second"), _para("third")])
    before = [b["id"] for b in vault.block_children(page["id"])]

    path = vault._path_for(page["id"])
    path.write_text(path.read_text(encoding="utf-8").replace("second", "SECOND (typo fixed)"),
                    encoding="utf-8")

    after = list(vault.block_children(page["id"]))
    assert [b["id"] for b in after] == before, "an external edit must not renumber a page"
    assert "SECOND" in after[1]["paragraph"]["rich_text"][0]["text"]["content"]


def test_reordering_a_page_by_hand_keeps_every_id(vault):
    page = vault.create_page("", "Reordered",
                             children=[_para("alpha"), _para("beta")])
    original = {b["id"]: b["paragraph"]["rich_text"][0]["text"]["content"]
                for b in vault.block_children(page["id"])}

    path = vault._path_for(page["id"])
    body = path.read_text(encoding="utf-8")
    path.write_text(body.replace("alpha\n\nbeta", "beta\n\nalpha"), encoding="utf-8")

    after = {b["id"]: b["paragraph"]["rich_text"][0]["text"]["content"]
             for b in vault.block_children(page["id"])}
    assert after == original, "ids follow their text, not their position"


def test_a_file_written_by_hand_is_adopted(vault):
    """Drop a note into the folder and it becomes a page, rather than being ignored."""
    (vault.root / "dropped-in.md").write_text(
        "# Dropped in\n\nSome prose.\n", encoding="utf-8")

    pages = list(vault.search_pages())
    assert len(pages) == 1
    assert pages[0]["properties"]["title"]["title"][0]["text"]["content"] == "dropped in"
    # And it now carries an id, so the next read is stable.
    assert 'id: "mp_' in (vault.root / "dropped-in.md").read_text(encoding="utf-8")


def test_the_file_on_disk_stays_clean_markdown(vault):
    """No ids, no markers, nothing that would look like damage in Obsidian."""
    page = vault.create_page("", "Readable", children=to_blocks(SAMPLE))
    body = vault._path_for(page["id"]).read_text(encoding="utf-8")
    head, _, content = body.partition("---\n\n")
    assert "mb_" not in content and "<!--" not in content
    assert content.strip() == SAMPLE


# ---------------------------------------------------------------------------
# parity with Notion's awkward behaviours
# ---------------------------------------------------------------------------


def test_appending_reports_nested_descendants_too(vault):
    """What the real API returns, and the reason undo has to tolerate a cascade.

    If this returned only the top-level blocks, `created_block_ids` would be short, undo
    would leave the children behind, and the bug this parity exists to prevent would be
    invisible in every offline test.
    """
    page = vault.create_page("", "Nesting")
    toggle = {"type": "toggle", "toggle": {
        "rich_text": _text("Sources"),
        "children": [_para("one citation"), _para("another")]}}

    created = vault.append_children(page["id"], [toggle])["results"]

    assert len(created) == 3, "the toggle and both children"
    assert created[0]["type"] == "toggle"


def test_a_restored_block_comes_back_at_the_end_of_the_page(vault):
    """Notion does this, and `apply` rebuilds rewritten sections from a snapshot
    *because* it does. A backend that restored in place would let that bug back in."""
    page = vault.create_page("", "Order",
                             children=[_para("one"), _para("two"), _para("three")])
    blocks = list(vault.block_children(page["id"]))
    first = blocks[0]["id"]

    vault.archive_block(first)
    vault.restore_block(first)

    after = [b["paragraph"]["rich_text"][0]["text"]["content"]
             for b in vault.block_children(page["id"])]
    assert after == ["two", "three", "one"]


def test_archiving_an_already_archived_block_is_an_error_that_says_archived(vault):
    """Word-for-word parity matters here: `_archive_created` forgives this error by
    looking for "archived" in the message, and only this error."""
    page = vault.create_page("", "Twice", children=[_para("gone")])
    block = next(iter(vault.block_children(page["id"])))["id"]
    vault.archive_block(block)

    with pytest.raises(NotionError) as caught:
        vault.archive_block(block)
    assert "archived" in str(caught.value).lower()


def test_archiving_a_parent_takes_its_children(vault):
    page = vault.create_page("", "Cascade")
    toggle = {"type": "toggle", "toggle": {
        "rich_text": _text("Parent"), "children": [_para("child")]}}
    created = vault.append_children(page["id"], [toggle])["results"]

    vault.archive_block(created[0]["id"])

    assert list(vault.block_children(page["id"])) == []
    with pytest.raises(NotionError):
        vault.get_block(created[1]["id"])


def test_a_missing_page_raises_the_same_404_notion_would(vault):
    with pytest.raises(NotionError) as caught:
        vault.get_page("mp_nothing")
    assert caught.value.status == 404
    assert caught.value.is_not_found


# ---------------------------------------------------------------------------
# the rest of the write surface
# ---------------------------------------------------------------------------


def test_pages_can_be_created_renamed_iconed_covered_and_moved(vault):
    home = vault.create_page("", "Home")
    page = vault.create_page(home["id"], "Draft", children=[_para("body")], icon="📐")

    assert vault.get_page(page["id"])["icon"]["emoji"] == "📐"
    assert vault.get_page(page["id"])["parent"]["page_id"] == home["id"]

    vault.rename_page(page["id"], "Final")
    vault.set_page_icon(page["id"], "✅")
    vault.set_page_cover(page["id"], "https://example.com/c.png")
    elsewhere = vault.create_page("", "Elsewhere")
    vault.move_page(page["id"], elsewhere["id"])

    after = vault.get_page(page["id"])
    assert after["properties"]["title"]["title"][0]["text"]["content"] == "Final"
    assert after["icon"]["emoji"] == "✅"
    assert after["cover"]["external"]["url"] == "https://example.com/c.png"
    assert after["parent"]["page_id"] == elsewhere["id"]
    # The body is untouched by any of that.
    assert [b["type"] for b in vault.block_children(page["id"])] == ["paragraph"]


def test_updating_a_block_keeps_the_children_hanging_off_it(vault):
    """The applier only ever rewrites a block's text. Dropping its children would delete
    content silently — the worst kind of data loss, because nothing errors."""
    page = vault.create_page("", "Keep children")
    toggle = {"type": "toggle", "toggle": {
        "rich_text": _text("Heading"), "children": [_para("kept")]}}
    created = vault.append_children(page["id"], [toggle])["results"]

    vault.update_block(created[0]["id"],
                       {"toggle": {"rich_text": _text("Renamed")}})

    parent = vault.get_block(created[0]["id"])
    assert rich_to_md(parent["toggle"]["rich_text"]) == "Renamed"
    assert len(parent["toggle"]["children"]) == 1


def test_archiving_a_page_is_reversible(vault):
    page = vault.create_page("", "Temporary", children=[_para("content")])
    vault.archive_page(page["id"])

    assert list(vault.search_pages()) == []
    with pytest.raises(NotionError):
        vault.get_page(page["id"])

    vault.archive_page(page["id"], restore=True)
    assert [p["id"] for p in vault.search_pages()] == [page["id"]]
    assert len(list(vault.block_children(page["id"]))) == 1


def test_the_journal_becomes_a_markdown_table(vault):
    """A vault has no databases. The activity log is a table, which is what a database
    view is for a reader anyway — and it renders in Obsidian and on GitHub."""
    home = vault.create_page("", "Home")
    database = vault.create_database(home["id"], "Activity", properties={
        "Name": {"title": {}}, "When": {"date": {}}, "Kind": {"select": {}}})
    source = vault.data_source_id(database)
    assert source

    vault.create_row(source, {
        "Name": {"title": md_to_rich("Appended a claim")},
        "When": {"date": {"start": "2026-09-12"}},
        "Kind": {"select": {"name": "append_block"}}})

    body = vault._path_for(database["id"]).read_text(encoding="utf-8")
    assert "| Name | When | Kind |" in body
    assert "| Appended a claim | 2026-09-12 | append_block |" in body


def test_writes_from_several_threads_do_not_lose_each_other(vault):
    """Every write is a read-modify-write of a whole file. The queue worker and the web
    handlers share one workspace, so this is the shape of a real bug, not a hypothetical."""
    import concurrent.futures as cf

    page = vault.create_page("", "Concurrent")
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda i: vault.append_children(page["id"], [_para(f"line {i}")]),
            range(24)))

    assert len(list(vault.block_children(page["id"]))) == 24


def test_the_sidecar_never_leaks_into_the_vault(vault):
    """`.palimpsest/` holds the ids. It must not be mistaken for content, or a sync would
    try to ingest its own bookkeeping."""
    vault.create_page("", "Anything", children=[_para("x")])
    assert [p.name for p in vault._pages()] == ["anything.md"]
    assert (vault.root / ".palimpsest" / "pages.json").exists()
    assert json.loads((vault.root / ".palimpsest" / "pages.json").read_text())


# ---------------------------------------------------------------------------
# rendering that a markdown reader actually accepts
# ---------------------------------------------------------------------------


def test_emphasis_never_wraps_surrounding_whitespace():
    """`**bold **` is not bold in any renderer — it prints the asterisks.

    Notion hands out runs with trailing spaces constantly, because a sentence built from
    a bold lead-in and a plain tail is exactly that shape. Every contradiction callout
    the planner writes is that shape, so getting this wrong produced visible garbage on
    the page: `**Conflicts with the line above — **Recent work finds...*  (Note)*`.
    """
    runs = [
        {"type": "text", "text": {"content": "Conflicts with the line above — "},
         "annotations": {"bold": True}},
        {"type": "text", "text": {"content": "Per-parameter clipping is better."},
         "annotations": {}},
        {"type": "text", "text": {"content": " (Note)"},
         "annotations": {"italic": True}},
    ]
    rendered = rich_to_md(runs)

    assert rendered == ("**Conflicts with the line above —** Per-parameter clipping "
                        "is better. *(Note)*")
    # The property behind that string: parsing it back finds the same emphasis, which is
    # only true if no marker wrapped a space.
    reparsed = md_to_rich(rendered)
    assert [r["annotations"]["bold"] for r in reparsed] == [True, False, False]
    assert [r["annotations"]["italic"] for r in reparsed] == [False, False, True]
    assert rich_to_md(reparsed) == rendered


def test_a_run_that_is_only_whitespace_keeps_its_space_and_gains_no_markers():
    assert rich_to_md([{"type": "text", "text": {"content": "  "},
                        "annotations": {"bold": True}}]) == "  "


def test_a_callout_with_no_children_leaves_no_stray_quote_line():
    """`"".split()` is `[""]`, which used to append a bare `>` under every childless
    callout — a blank quote line that renders as an empty box."""
    rendered = to_markdown([{"type": "callout", "callout": {
        "rich_text": _text("Just the one line"),
        "icon": {"type": "emoji", "emoji": "🔖"}}}])

    assert rendered == "> [!info] Just the one line"
    assert not rendered.endswith(">")


def test_what_the_planner_writes_survives_the_round_trip():
    """The end-to-end property: a callout the contradiction planner produces goes to
    disk, comes back, and is still one callout saying the same thing."""
    original = "> [!warning] **Conflicts with the line above —** It is better. *(Note)*"
    blocks = to_blocks(original)

    assert len(blocks) == 1 and blocks[0]["type"] == "callout"
    assert to_markdown(blocks) == original


def test_soft_wrapped_prose_is_one_paragraph_not_one_per_line():
    """What anybody writing in an editor with a wrap column assumes, and what markdown
    says.

    Treating each line as its own block looked harmless and was not. A vault written by
    hand is full of soft-wrapped prose, so on first sync every sentence fragment became a
    separate block — separate id, separate retrieval candidate, separate thing for the
    classifier to hang a footnote on. And the round trip inserted a blank line at every
    wrap point, so the tool rewrote the file it had just read.
    """
    source = ("Gradient clipping bounds the size of a gradient update before the\n"
              "optimiser applies it. It is the standard remedy for exploding\n"
              "gradients in deep networks.")

    blocks = to_blocks(source)

    assert len(blocks) == 1 and blocks[0]["type"] == "paragraph"
    assert to_markdown(blocks) == source


def test_a_blank_line_still_starts_a_new_paragraph():
    blocks = to_blocks("First thought,\ncontinued.\n\nSecond thought.")

    assert [b["type"] for b in blocks] == ["paragraph", "paragraph"]
    assert rich_to_md(blocks[0]["paragraph"]["rich_text"]) == "First thought,\ncontinued."


def test_a_wrapped_line_that_starts_a_block_is_not_swallowed():
    """The join must stop at anything that is not more prose, or a heading directly
    under a paragraph disappears into it."""
    blocks = to_blocks("Some prose.\n## A heading\n- a bullet\n> a quote")

    assert [b["type"] for b in blocks] == [
        "paragraph", "heading_2", "bulleted_list_item", "quote"]


def test_a_callout_body_wraps_the_same_way():
    """The bug that surfaced this: a wrapped callout gained a blank quote line per wrap,
    so every sync rewrote the page."""
    source = ("> [!abstract] Vaswani et al., 2017\n"
              "> The paper that introduced the transformer. Replaces recurrence\n"
              "> entirely with self-attention.")

    blocks = to_blocks(source)

    assert len(blocks) == 1 and blocks[0]["type"] == "callout"
    assert len(blocks[0]["callout"]["children"]) == 1, "one paragraph, not two"
    assert to_markdown(blocks) == source
