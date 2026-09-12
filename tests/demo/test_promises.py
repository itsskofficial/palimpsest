"""Does the demo keep the promises it makes?

`palimpsest demo` puts four suggested captures in front of a visitor and tells each one
what to expect: this will argue with a page, this will sharpen one, this needs a page of
its own, this one has to choose between two. Those are claims about a live model's
behaviour on a fixed vault, and a demo that makes a promise the product does not keep is
worse than having no demo at all — it is the first thing a visitor checks and the only
thing they will remember.

The vault does not change. The model does. So this runs the real pipeline against a real
model and fails if any promise stops being true.

Marked `api` because it needs a key and costs money, so it is not part of the merge gate.
Run it before cutting a release, and any time the prompts, the vault, or the classifier's
instructions change:

    pytest tests/demo -m api -v
"""

from __future__ import annotations

import pytest

from palimpsest import demo, workspace
from palimpsest.llm import Model, available
from palimpsest.notion import mirror
from palimpsest.pipeline import ingest
from palimpsest.store import open_store

pytestmark = pytest.mark.api


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    """One demo vault per prompt, so nothing a prompt writes affects the next."""
    from palimpsest.config import Settings

    # `Settings.load()` rather than bare `available()`: the persisted config file is
    # where a real installation keeps its keys, and reading only `os.environ` would skip
    # this suite on exactly the machines that can run it.
    if not available(Settings.load()):
        pytest.skip("no model configured")
    return tmp_path_factory.mktemp("demo-promises")


def _run(home, prompt):
    vault = demo.install(home / prompt["label"].replace(" ", "-"), force=True)
    settings = demo.settings_for(vault)
    settings, _ = demo.ensure_model(settings)

    store = open_store(settings.database_url)
    try:
        mirror.sync(workspace.open(settings), store, incremental=False)
        result = ingest(prompt["text"], store, Model(settings=settings),
                        settings=settings)
        titles = {page["page_id"]: page["title"] for page in store.get_pages()}
        applied = [op.relation.value for op in (result.patch.operations
                                                if result.patch else [])]
        # A relation reaching `review` counts. `contradicts` in particular is *supposed*
        # to land there: it is the one verdict the system refuses to act on alone, so
        # requiring it in the applied list would be testing for a safety failure.
        reviewed = [(item.get("judgement") or {}).get("relation")
                    for item in (result.review or [])]
        # An operation targets a page *or* a block, so resolve through the block table
        # too -- looking a block id up in the page table silently yields nothing, which
        # reads as "it placed this nowhere" rather than "the test cannot see where".
        pages = set()
        for op in (result.patch.operations if result.patch else []):
            target = str(op.target)
            block = store.get_block(target)
            page_id = block["page_id"] if block else target
            pages.add(titles.get(page_id, ""))
        pages |= {item.get("page") or "" for item in (result.review or [])}
        return applied, reviewed, {p for p in pages if p}
    finally:
        store.close()


@pytest.mark.parametrize("prompt", demo.PROMPTS,
                         ids=[p["label"] for p in demo.PROMPTS])
def test_a_suggested_capture_does_what_it_says(prepared, prompt):
    applied, reviewed, pages = _run(prepared, prompt)
    seen = set(applied) | set(reviewed)

    for expected in prompt["expect"]:
        assert expected in seen, (
            f"{prompt['label']!r} promises {expected!r}.\n"
            f"  applied:  {applied}\n  review:   {reviewed}\n  pages:    {sorted(pages)}\n"
            f"Either the vault no longer supports that reading, or the classifier "
            f"changed its mind. Fix the promise or fix the cause — do not relax this.")

    if prompt.get("expect_page"):
        assert pages & set(prompt["expect_page"]), (
            f"{prompt['label']!r} promises it lands on one of "
            f"{prompt['expect_page']}, got {sorted(pages)}")


def test_a_contradiction_is_never_applied_even_here(prepared):
    """The safety property, checked against a live model rather than a fake one.

    `test_safety.py` pins this structurally and offline, which is what a merge gate
    needs. This is the other half: the demo runs at `autonomy=full`, a real model reads a
    real page and really does decide the claim contradicts it — and still nothing is
    written without a person.
    """
    prompt = demo.PROMPTS[0]
    applied, reviewed, _ = _run(prepared, prompt)

    assert "contradicts" in reviewed
    assert "contradicts" not in applied


def test_every_prompt_is_about_something_the_vault_actually_says():
    """Offline sanity: a promise can only hold if the vault supports it.

    Cheap to check and worth checking, because the failure it catches is silent — a
    prompt edited to say something the pages no longer mention still *looks* fine, and
    only fails later against a live model.
    """
    text = " ".join(path.read_text(encoding="utf-8").lower()
                    for path in demo.vault_source().glob("*.md"))
    for prompt in demo.PROMPTS:
        assert prompt["why"], prompt["label"]
        assert prompt["text"].strip(), prompt["label"]
        assert isinstance(prompt["expect"], tuple), (
            f"{prompt['label']}: expect must be a tuple of relation names, so that "
            f"'promises nothing in particular' is spelled `()` rather than implied")
        for page in prompt.get("expect_page", ()):
            assert page.lower() in text, f"{prompt['label']} names a page that is gone"
