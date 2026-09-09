"""The main path, offline: capture → queue → pipeline, and the mirror underneath it.

Three modules carry every capture the product ever receives, and a regression in any of
them is silent rather than loud. `pipeline.ingest` is the whole loop; `jobs` is the
durable queue every surface posts to; `notion.mirror` is the copy of Notion that
retrieval, undo and provenance are all defined against. When one of them breaks you do
not get an exception — you drop a PDF in and nothing appears, or the wrong thing does,
and you find out weeks later.

Everything here runs with no network and no key. The model is a fake that returns canned
payloads in the exact shapes `extract.py` and `relate.py` parse, and the Notion client is
a fake that returns page and block objects in the shape `notion/client.py` produces. That
is deliberate: these tests are about the orchestration — what gets stored, what gets
skipped, what gets re-fetched — and a real model would make them slow, expensive and
non-deterministic without testing any of it.

The fake classifier reads its answer out of the candidate block it was given, rather than
naming an id of its own. `relate.classify_one` repairs references the model invented, so
a fake that guessed would be silently corrected and the plan under test would be the
repair path rather than the real one.
"""

from __future__ import annotations

import re

import pytest

from palimpsest.config import Settings
from palimpsest.jobs import JobQueue, ingest_runner, submit_spec
from palimpsest.llm import TokenUsage, Usage
from palimpsest.notion.mirror import sync
from palimpsest.pipeline import ingest
from palimpsest.retrieve import Index
from tests.conftest import ADAMW, ATTENTION

# ---------------------------------------------------------------------------
# fakes: the model
# ---------------------------------------------------------------------------

#: The candidates `relate._render_candidates` puts in the cached prefix, so the fake can
#: answer with an id that genuinely exists rather than one the repair path would strip.
_BLOCK_ID = re.compile(r"block_id: (\S+)")
_PAGE_ID = re.compile(r"^\s+- page_id: (\S+)", re.MULTILINE)

#: Two claims whose `quote` appears verbatim in the fixture text, and whose `text` is a
#: tightened paraphrase — close enough to retrieve the right block, far enough that
#: `relate._shortcut` does not decide it without the model.
CLAIMS = [
    {"text": "Attention divides the logits by the square root of the key dimension.",
     "quote": "divides the logits by the square root of the key dimension",
     "type": "fact", "topics": ["attention"], "confidence": 0.95},
    {"text": "AdamW decouples weight decay from the gradient update.",
     "quote": "AdamW decouples weight decay from the gradient update",
     "type": "fact", "topics": ["optimisers"], "confidence": 0.9},
]


class FakeModel:
    """A `Model` in the only four things the pipeline asks of one.

    `json` dispatches on `task`, which is how `extract` and `relate` are distinguished
    everywhere else in the codebase, and it counts its calls per task so a test can
    assert that a stage was skipped rather than merely that its output was reused.
    """

    model = "fake/offline-1"
    name = "fake"
    base_url = None

    def __init__(self, claims=None, judgement=None):
        self.claims = CLAIMS if claims is None else claims
        self.judgement = judgement or {}
        self.calls: dict[str, int] = {}
        self.usage = Usage()

    def json(self, *, task, system, prompt, schema, effort="high",
             cache_prefix=None, max_tokens=None):
        self.calls[task] = self.calls.get(task, 0) + 1
        self.usage.add(task, TokenUsage(input=900, output=120), 0.01)
        if task == "extract":
            return {"claims": [dict(c) for c in self.claims]}
        if task == "classify":
            return self._classify(cache_prefix or "")
        raise AssertionError(f"the pipeline asked for an unexpected task: {task!r}")

    def _classify(self, prefix: str) -> dict:
        block = _BLOCK_ID.search(prefix)
        page = _PAGE_ID.search(prefix)
        return {
            "relation": "corroborates",
            "confidence": 0.9,
            "target_page_id": page.group(1) if page else None,
            "target_block_id": block.group(1) if block else None,
            "existing_text": None,
            "rationale": "the notes already state this; the source is a second witness",
            **self.judgement,
        }


class ExplodingModel:
    """A model that must never be reached. Being constructed at all is the failure."""

    def __init__(self):  # pragma: no cover - the point is that this never runs
        raise AssertionError("a model was built when none is configured")


# ---------------------------------------------------------------------------
# fakes: Notion
# ---------------------------------------------------------------------------


class RecordingNotion:
    """Records that it was built or written to. Never expected to be either."""

    def __init__(self):
        self.events: list[str] = []

    def built(self) -> RecordingNotion:
        self.events.append("constructed")
        return self

    def wrote(self, what: str) -> dict:
        self.events.append(what)
        return {}


def _page(page_id: str, title: str, *, last_edited: str, parent: str | None = None,
          icon: str | None = None) -> dict:
    """A page object in the shape `NotionClient.search_pages` yields."""
    return {
        "object": "page",
        "id": page_id,
        "parent": ({"type": "page_id", "page_id": parent} if parent
                   else {"type": "workspace", "workspace": True}),
        "properties": {"title": {"id": "title", "type": "title", "title": [
            {"type": "text", "text": {"content": title, "link": None},
             "plain_text": title}]}},
        "url": f"https://notion.so/{page_id}",
        "icon": {"type": "emoji", "emoji": icon} if icon else None,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": last_edited,
        "archived": False,
        "in_trash": False,
    }


def _block(block_id: str, text: str, *, kind: str = "paragraph",
           has_children: bool = False) -> dict:
    """A block object in the shape `NotionClient.block_children` yields."""
    return {
        "object": "block",
        "id": block_id,
        "type": kind,
        kind: {"rich_text": [{"type": "text", "text": {"content": text, "link": None},
                              "plain_text": text}]},
        "has_children": has_children,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-03-01T00:00:00.000Z",
        "archived": False,
        "in_trash": False,
    }


class FakeNotionAPI:
    """The three read members `mirror.sync` touches, plus a record of what it fetched.

    `fetched` is the load-bearing part: an incremental sync is only incremental if it
    does not ask for a page's children, and counting API calls alone would not say which
    page was spared.
    """

    def __init__(self, pages: list[dict], children: dict[str, list[dict]] | None = None):
        self.pages = list(pages)
        self.children = {k: list(v) for k, v in (children or {}).items()}
        self.calls = 0
        self.fetched: list[str] = []

    def search_pages(self, query: str = ""):
        self.calls += 1
        yield from self.pages

    def block_children(self, block_id: str):
        self.calls += 1
        self.fetched.append(block_id)
        yield from self.children.get(block_id, [])

    def edit(self, page_id: str, when: str) -> None:
        """Move a page's `last_edited_time`, as editing it in Notion would."""
        for page in self.pages:
            if page["id"] == page_id:
                page["last_edited_time"] = when


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------

SPEC = f"text:{ATTENTION}\n\n{ADAMW}"


def test_a_text_source_runs_end_to_end_and_stores_a_patch(mirror):
    model = FakeModel()
    result = ingest(SPEC, mirror, model, index=Index(mirror))

    assert len(result.claims) == 2
    assert all(c.anchor is not None for c in result.claims), "every claim must be anchored"
    assert len(result.patch) == 2

    # The patch is not just returned — it is durable, and it round-trips.
    stored = mirror.get_patch(result.patch.patch_id)
    assert stored is not None
    assert [op.kind.value for op in stored] == ["add_citation", "add_citation"]
    # Each claim was routed to the block it is actually about, not to whichever one
    # retrieval happened to rank first for both.
    assert [op.target for op in stored] == ["bk_att_1", "bk_opt_1"]
    assert stored.status == "proposed"

    # Stage timings are what `palimpsest ingest` prints and what a slow-source report is
    # built from; a stage silently missing from the dict is a stage that stopped running.
    assert set(result.stages) >= {"ingest", "extract", "classify", "plan"}
    classified = result.stages["classify"]
    assert classified["judgements"] == 2
    assert (classified["model_calls"], classified["shortcut"]) == (2, 0)
    assert result.stages["plan"]["operations"] == 2
    assert result.seconds > 0


def test_the_same_content_twice_is_deduplicated_by_hash(mirror):
    """Paying twice for the same PDF is both a bill and a duplicate patch.

    Sources are keyed by content hash precisely so that "backfill everything already in
    my Notion" is affordable to run more than once. If this regresses, the second run
    re-extracts every source and proposes a second patch for each — expensive, and then
    tedious to reject one by one.
    """
    model = FakeModel()
    first = ingest(SPEC, mirror, model, index=Index(mirror))
    second = ingest(SPEC, mirror, model, index=Index(mirror))

    assert first.reused is False
    assert second.reused is True
    assert second.source.source_id == first.source.source_id
    assert model.calls["extract"] == 1, "the second ingest must not re-extract"
    assert second.stages["extract"] == {"claims": 2, "reused": True}
    assert [c.claim_id for c in second.claims] == sorted(c.claim_id for c in first.claims)


def test_a_source_with_no_claims_produces_an_empty_patch(store):
    """An empty result is a valid answer — the extractor is told to prefer it over
    padding — so it must come back as an empty patch, not an exception halfway down."""
    model = FakeModel(claims=[])
    result = ingest("text:Cookie preferences. Subscribe to our newsletter.", store, model,
                    index=Index(store))

    assert result.claims == []
    assert len(result.patch) == 0
    assert store.list_patches() == [], "an empty patch is not worth a row"
    assert result.stages["extract"]["claims"] == 0


def test_ingest_writes_nothing_to_notion(mirror, monkeypatch):
    """The README's promise: `ingest` proposes, and applying is a separate, explicit act.

    That is the reason the tool is safe to point at your own notes before you have any
    evidence it behaves. A pipeline that quietly grew a write would break the promise
    without breaking a single other test in this file.
    """
    notion = RecordingNotion()
    monkeypatch.setattr("palimpsest.notion.client.NotionClient",
                        lambda *a, **k: notion.built())
    monkeypatch.setattr("palimpsest.notion.apply.apply_patch",
                        lambda *a, **k: notion.wrote("apply_patch"))

    result = ingest(SPEC, mirror, FakeModel(), index=Index(mirror))

    assert notion.events == [], "ingest must not touch Notion"
    assert result.patch.status == "proposed"
    assert mirror.page_history("pg_attention") == [], "nothing was applied to the ledger"


# ---------------------------------------------------------------------------
# the capture queue
# ---------------------------------------------------------------------------


def _queue(store, handler) -> JobQueue:
    """A queue bound to one store. Never started — the tests drive `_run` themselves so
    the assertions are deterministic rather than dependent on thread scheduling."""
    return JobQueue(store_factory=lambda: store, handlers={"ingest": handler}, workers=1)


def test_a_queued_job_is_claimed_exactly_once(store):
    """Two workers double-ingesting one capture is the failure the atomic claim prevents.

    The visible symptom is not an error — it is one source extracted twice, at twice the
    cost, with two patches proposed for it. So the second claim of a single-job queue
    must come back empty, never the same row again.
    """
    job = submit_spec(store, "text:something worth remembering")

    first = store.claim_job()
    second = store.claim_job()

    assert first is not None and first["job_id"] == job["job_id"]
    assert first["status"] == "running"
    assert second is None
    assert store.get_job(job["job_id"])["attempts"] == 1


def test_a_finished_job_records_its_status_and_patch_id(store):
    job = submit_spec(store, "text:a note")
    queue = _queue(store, lambda j, s: {"patch_id": "pch_written", "claims": 3})

    queue._run(store, store.claim_job())

    row = store.get_job(job["job_id"])
    assert row["status"] == "done"
    assert row["patch_id"] == "pch_written"
    assert row["result"]["claims"] == 3
    assert row["error"] is None


def test_a_failed_job_keeps_its_row_and_its_error(store):
    """Retrying is a decision for a human — re-running a failed extraction costs money —
    so a failure must leave a readable row behind rather than vanishing from the queue."""
    def boom(job, worker_store):
        raise ValueError("the transcript was empty")

    job = submit_spec(store, "text:a note")
    _queue(store, boom)._run(store, store.claim_job())

    row = store.get_job(job["job_id"])
    assert row["status"] == "failed"
    assert row["error"] == "ValueError: the transcript was empty"
    assert store.list_jobs("failed")[0]["job_id"] == job["job_id"]


def test_requeue_stale_jobs_returns_an_interrupted_capture_to_the_queue(store):
    """A capture that outlived the window that made it is the entire point of the queue.

    A browser popup closes the moment you click away and a laptop lid shuts mid-ingest.
    Without this, the job sits in `running` forever and the thing you asked to be
    remembered is silently lost — the one failure a capture tool must not have.
    """
    job = submit_spec(store, "text:captured just before the crash")
    store.claim_job()                                  # now 'running', then the process dies
    assert store.get_job(job["job_id"])["status"] == "running"

    assert store.requeue_stale_jobs() == 1
    assert store.get_job(job["job_id"])["status"] == "queued"
    assert store.claim_job()["job_id"] == job["job_id"], "it must be claimable again"


def test_the_ingest_runner_refuses_clearly_with_no_model(store):
    """Failing the job with a message naming the variable to set, rather than refusing to
    boot: a process with no key must still start a queue and still accept captures."""
    runner = ingest_runner(Settings(), model_factory=ExplodingModel)

    with pytest.raises(RuntimeError) as excinfo:
        runner({"job_id": "job_1", "kind": "ingest", "spec": "text:a note"}, store)

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "OPENAI_API_KEY" in message and "PALIMPSEST_MODEL_BASE_URL" in message


# ---------------------------------------------------------------------------
# the mirror
# ---------------------------------------------------------------------------

PAGE_A = "pgaaaa0000000000000000000000aaaa"
PAGE_B = "pgbbbb0000000000000000000000bbbb"


@pytest.fixture()
def workspace() -> FakeNotionAPI:
    """Two pages of flat prose — enough to sync, skip and re-sync."""
    return FakeNotionAPI(
        pages=[_page(PAGE_A, "Attention", last_edited="2026-03-01T00:00:00.000Z",
                     icon="🧠"),
               _page(PAGE_B, "Optimisers", last_edited="2026-03-02T00:00:00.000Z",
                     parent=PAGE_A)],
        children={PAGE_A: [_block("bkaa1", ATTENTION)],
                  PAGE_B: [_block("bkbb1", ADAMW)]},
    )


def test_a_sync_pulls_pages_and_blocks_into_the_store(store, workspace):
    result = sync(workspace, store, incremental=False)

    assert (result.pages, result.blocks) == (2, 2)

    titles = {p["page_id"]: p["title"] for p in store.get_pages()}
    assert titles == {PAGE_A: "Attention", PAGE_B: "Optimisers"}

    page_a = store.get_page(PAGE_A)
    assert page_a["icon"] == "🧠"
    assert page_a["url"] == f"https://notion.so/{PAGE_A}"
    assert store.get_page(PAGE_B)["parent_id"] == PAGE_A

    blocks = store.get_blocks(PAGE_A)
    assert [b["text"] for b in blocks] == [ATTENTION]
    assert blocks[0]["raw"]["type"] == "paragraph", "the raw block is what undo needs"


def test_a_resync_does_not_refetch_an_unchanged_page(store, workspace):
    """A full re-fetch of a real workspace is thousands of API calls at 2.5/second.

    Incremental sync is what makes a scheduled refresh cheap enough to run hourly, and
    the test for it cannot be "fewer calls" — it has to be that the children of an
    unchanged page were never asked for at all.
    """
    sync(workspace, store, incremental=True)
    workspace.fetched.clear()

    unchanged = sync(workspace, store, incremental=True)
    assert unchanged.skipped == 2
    assert unchanged.pages == 0
    assert workspace.fetched == [], "an unchanged page must cost no children call"

    workspace.edit(PAGE_B, "2026-04-09T00:00:00.000Z")
    moved = sync(workspace, store, incremental=True)
    assert (moved.pages, moved.skipped) == (1, 1)
    assert workspace.fetched == [PAGE_B]


def test_block_flattening_preserves_order_and_page_association(store):
    """Depth-first, position-numbered, every block owned by the page it was found under.

    Position is what the review UI and the applier both use to say *where* on a page an
    edit lands, and a nested block that forgets its root page is a citation pointing at
    the wrong note.
    """
    client = FakeNotionAPI(
        pages=[_page(PAGE_A, "Nested", last_edited="2026-03-01T00:00:00.000Z")],
        children={
            PAGE_A: [_block("bk1", "first paragraph"),
                     _block("bk2", "a toggle", kind="toggle", has_children=True),
                     _block("bk4", "last paragraph")],
            "bk2": [_block("bk3", "inside the toggle")],
        },
    )
    sync(client, store, incremental=False)

    blocks = store.get_blocks(PAGE_A)
    assert [b["text"] for b in blocks] == [
        "first paragraph", "- a toggle", "inside the toggle", "last paragraph",
    ]
    assert [b["position"] for b in blocks] == [0, 1, 2, 3]
    assert [b["depth"] for b in blocks] == [0, 0, 1, 0]
    assert {b["page_id"] for b in blocks} == {PAGE_A}
    assert store.get_block("bk3")["parent_id"] == "bk2"


def test_a_resync_does_not_wipe_a_page_profile(store, workspace):
    """Role, summary and topics are computed here and returned by no Notion endpoint.

    `put_pages` COALESCEs them for exactly this reason. Without that, every scheduled
    incremental sync would blank the profiles, the planner would treat every hub as a
    `reference` page, and index pages would quietly start growing paragraphs — a
    regression with no error message and a weeks-long delay before anyone notices.
    """
    sync(workspace, store, incremental=True)
    store.set_page_profile(PAGE_A, "hub", "the index of everything about attention",
                           ["attention", "transformers"])

    workspace.edit(PAGE_A, "2026-04-09T00:00:00.000Z")
    assert sync(workspace, store, incremental=True, profile=False).pages == 1

    page = store.get_page(PAGE_A)
    assert page["role"] == "hub"
    assert page["summary"] == "the index of everything about attention"
    assert page["topics"] == ["attention", "transformers"]
    assert page["last_edited"] == "2026-04-09T00:00:00.000Z", "the sync still happened"


# ---------------------------------------------------------------------------
# one document must not become several pages about the same thing
# ---------------------------------------------------------------------------


def test_two_claims_on_one_topic_create_one_page_not_two():
    """The fragmenting failure, caught inside a single source.

    Every claim is classified against the notes as they were *before* this source, which
    is correct — the index cannot hold pages that do not exist yet. The consequence is
    that two claims from one document about one topic both come back `new` with nothing
    to attach to, and each asks for its own page.

    Found live on the very first capture: a two-sentence note about attention produced
    two Notion pages, both titled "Attention". A tool whose entire purpose is to stop
    notes fragmenting cannot be the thing that fragments them.
    """
    from palimpsest.plan import plan
    from palimpsest.types import (
        Claim,
        ClaimType,
        Judgement,
        OpKind,
        Relation,
        Source,
        new_id,
    )

    source = Source(source_id=new_id("src_"), kind="text", title="note",
                    text="two sentences about one topic")
    claims, judgements = {}, []
    for text in ("Attention divides the logits by the square root of the key dimension.",
                 "Without that divisor the dot products grow with the dimension."):
        claim = Claim(claim_id=new_id("clm_"), text=text, type=ClaimType.FACT,
                      topics=("attention",))
        claims[claim.claim_id] = claim
        judgements.append(Judgement(claim_id=claim.claim_id, relation=Relation.NEW,
                                    confidence=0.95, target_page_id=None,
                                    rationale="nothing covers this", model="fake"))

    result = plan(judgements, claims, source, _NoStore(), default_parent="pg_root")

    creations = [op for op in result.patch.operations if op.kind is OpKind.CREATE_PAGE]
    assert len(creations) == 1, [op.payload.get("title") for op in creations]

    # Both claims survive the merge — folding must not lose the second one.
    body = str(creations[0].payload["children"])
    assert "square root of the key dimension" in body
    assert "dot products grow with the dimension" in body
    assert creations[0].payload["merged_claims"] == [judgements[1].claim_id]


def test_one_source_creates_at_most_one_new_page():
    """Two topics in one document are two sections, not two pages.

    This test used to assert the opposite, and the opposite was wrong. Folding page
    creations by *title* fixed the obvious duplication and left a subtler one: a single
    article about retrieval produced six pages — "Retrieval-Augmented Generation",
    "Retrieval", "Reranking", "Bi-Encoder" — because every claim carried a different
    topic string, so no two titles ever collided. Four of them were one sentence long.
    That is the same fragmentation the product exists to prevent, reached by a different
    route, and a reader cannot tell the difference.

    Claims that belong on pages you already have are untouched by this: they were never
    page creations. This only governs material that has nowhere to go, and the composer
    turns it into sections — a job it does better than the planner, because it sees the
    claims together.
    """
    from palimpsest.plan import plan
    from palimpsest.types import (
        Claim,
        ClaimType,
        Judgement,
        OpKind,
        Relation,
        Source,
        new_id,
    )

    source = Source(source_id=new_id("src_"), kind="text", title="One article", text="x")
    claims, judgements = {}, []
    for topic in ("attention", "softmax", "reranking", "chunking"):
        claim = Claim(claim_id=new_id("clm_"), text=f"A fact about {topic}.",
                      type=ClaimType.FACT, topics=(topic,))
        claims[claim.claim_id] = claim
        judgements.append(Judgement(claim_id=claim.claim_id, relation=Relation.NEW,
                                    confidence=0.95, rationale="new", model="fake"))

    result = plan(judgements, claims, source, _NoStore(), default_parent="pg_root")

    creations = [op for op in result.patch.operations if op.kind is OpKind.CREATE_PAGE]
    assert len(creations) == 1

    # Every claim survives the fold — folding must never be a way to lose one.
    folded = {creations[0].claim_id, *creations[0].payload["merged_claims"]}
    assert folded == set(claims)
    body = str(creations[0].payload["children"])
    for topic in ("attention", "softmax", "reranking", "chunking"):
        assert topic in body


class _NoStore:
    """The planner only reads page titles and roles; a real store is not needed here."""

    def get_page(self, page_id):
        return None

    def get_block(self, block_id):
        return None
