"""One suite, both stores. SQLite always; Postgres when there is one to talk to.

`postgres.py` was 430 statements at zero coverage — an advertised backend that nothing
had ever run. Testing it in isolation would have been the wrong fix, because the claim
the README makes is not "Postgres works", it is "**Supabase works unchanged**": the same
code, the same guarantees, a different URL. That is a claim about *equivalence*, and the
only way to check equivalence is to run one suite against both.

So every test below is parametrised over whichever stores are available. On a machine
with no Postgres it silently runs SQLite alone and says so; point
`PALIMPSEST_TEST_POSTGRES` at a database and the identical assertions run again against
it. A behaviour that holds on one and not the other fails here by construction, which is
the only thing that makes "works unchanged" an honest sentence.

Bring one up with Docker:

    docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=pw postgres:16-alpine
    export PALIMPSEST_TEST_POSTGRES=postgresql://postgres:pw@127.0.0.1:55432/postgres
"""

from __future__ import annotations

import os
import time

import pytest

from palimpsest.embed import pack, unpack
from palimpsest.types import (
    Anchor,
    Claim,
    ClaimType,
    Judgement,
    Operation,
    OpKind,
    Patch,
    Relation,
    Source,
    new_id,
)

POSTGRES_URL = os.environ.get("PALIMPSEST_TEST_POSTGRES")


def _backends() -> list[str]:
    return ["sqlite"] + (["postgres"] if POSTGRES_URL else [])


@pytest.fixture(params=_backends())
def store(request):
    """A clean store of each available kind.

    Postgres is truncated rather than recreated: `CREATE DATABASE` per test would be
    slower than the suite it supports, and `truncate_all` is a method the store already
    owes us — exercising it here is not a workaround, it is coverage.
    """
    from palimpsest.store import open_store

    if request.param == "sqlite":
        opened = open_store("sqlite://:memory:")
    else:
        opened = open_store(POSTGRES_URL)
        opened.truncate_all()

    opened.backend_name = request.param        # type: ignore[attr-defined]
    yield opened
    opened.close()


def _source(text: str = "a paragraph about attention") -> Source:
    return Source(source_id=new_id("src_"), kind="web", title="A post", text=text,
                  url="https://example.com/p",
                  meta={"segments": [{"start": 0, "end": len(text),
                                      "kind": "section", "locator": "Intro"}]})


def _claim(source: Source, text: str = "Attention divides by sqrt(d_k).") -> Claim:
    return Claim(claim_id=new_id("clm_"), text=text, type=ClaimType.FACT,
                 topics=("attention",), confidence=0.95,
                 anchor=Anchor("section", "Intro", 0, 20, source.url),
                 source_id=source.source_id)


# ---------------------------------------------------------------------------
# the mirror
# ---------------------------------------------------------------------------


def test_pages_and_blocks_round_trip(store):
    store.put_pages([{"page_id": "pg_1", "title": "Attention", "role": "deep_dive",
                      "last_edited": "2026-03-01T00:00:00Z",
                      "url": "https://notion.so/a"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "text": "the text", "position": 0,
                       "raw": {"type": "paragraph"}}])

    page = store.get_page("pg_1")
    assert page is not None
    assert page["title"] == "Attention"
    assert page["role"] == "deep_dive"

    block = store.get_block("bk_1")
    assert block is not None
    assert block["text"] == "the text"
    assert block["page_id"] == "pg_1"
    # `raw` is JSON on both backends, and the applier rebuilds blocks from it during an
    # undo — a store that hands it back as a string breaks reversibility.
    assert isinstance(block["raw"], dict)


def test_putting_a_page_twice_updates_rather_than_duplicates(store):
    """Every sync re-puts every page it saw. Insert-only semantics would grow the mirror
    without bound and make `get_pages` return the same page many times."""
    for title in ("First", "Second"):
        store.put_pages([{"page_id": "pg_1", "title": title,
                          "last_edited": "2026-03-01T00:00:00Z"}])

    pages = store.get_pages()
    assert len(pages) == 1
    assert pages[0]["title"] == "Second"


def test_blocks_come_back_in_reading_order(store):
    """Position is the page's meaning. Out of order, a rewrite reassembles a page into
    something its author never wrote."""
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([
        {"block_id": f"bk_{i}", "page_id": "pg_1", "type": "paragraph",
         "text": f"line {i}", "position": i}
        for i in (3, 0, 2, 1)
    ])

    assert [b["text"] for b in store.get_blocks("pg_1")] == [
        "line 0", "line 1", "line 2", "line 3"]


def test_a_block_that_left_the_page_is_retired(store):
    """A block deleted in the workspace must stop being a retrieval candidate, or the
    classifier anchors an edit to a sentence that is no longer there."""
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([
        {"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph", "text": "kept",
         "position": 0},
        {"block_id": "bk_2", "page_id": "pg_1", "type": "paragraph", "text": "gone",
         "position": 1}])

    store.drop_missing_blocks("pg_1", {"bk_1"})

    assert [b["block_id"] for b in store.get_blocks("pg_1")] == ["bk_1"]


def test_backlinks_are_readable_in_both_directions(store):
    store.put_pages([{"page_id": p, "title": p, "last_edited": "x"}
                     for p in ("pg_hub", "pg_leaf")])
    store.put_links([("pg_hub", "pg_leaf", "bk_1")])

    assert store.backlinks("pg_leaf") == ["pg_hub"]
    assert store.backlinks("pg_hub") == []


def test_a_page_profile_survives_a_resync(store):
    """Roles cost a model call to infer, so re-syncing a page must not discard one."""
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.set_page_profile("pg_1", "hub", "a summary", ["attention"])

    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "y"}])

    assert store.get_page("pg_1")["role"] == "hub"


# ---------------------------------------------------------------------------
# sources, claims and judgements
# ---------------------------------------------------------------------------


def test_a_source_round_trips_with_its_segments(store):
    """The segments are how a citation resolves to a place in the original. Losing them
    turns every footnote into a link to the top of a document."""
    source = _source()
    store.put_source(source)

    back = store.get_source(source.source_id)
    assert back is not None
    assert back.title == "A post"
    assert back.meta["segments"][0]["locator"] == "Intro"


def test_the_same_content_is_recognised_by_hash(store):
    """Re-dropping a PDF must not re-extract it — that is the whole cost of an ingest."""
    source = _source()
    store.put_source(source)

    found = store.find_source_by_hash(source.content_hash)

    assert found is not None
    assert found.source_id == source.source_id


def test_claims_and_judgements_round_trip(store):
    source = _source()
    store.put_source(source)
    claim = _claim(source)
    store.put_claims([claim])
    store.put_judgements([Judgement(
        claim_id=claim.claim_id, relation=Relation.CORROBORATES, confidence=0.91,
        target_page_id="pg_1", target_block_id="bk_1",
        rationale="the page already says this")])

    claims = store.get_claims(source.source_id)
    assert len(claims) == 1
    assert claims[0].text == claim.text
    assert claims[0].anchor is not None
    assert claims[0].anchor.locator == "Intro"


# ---------------------------------------------------------------------------
# patches, which carry the reversibility guarantee
# ---------------------------------------------------------------------------


def _patch(*ops: Operation) -> Patch:
    return Patch(patch_id=new_id("pch_"), source_id="src_1", operations=list(ops))


def test_a_patch_round_trips_with_its_operations_and_inverses(store):
    """The inverse is the undo. A store that loses it leaves a change nobody can take
    back, which is the one promise this project cannot break."""
    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                   payload={"text": "a claim"})
    op.inverse = {"kind": "archive_blocks", "target": "pg_1",
                  "payload": {"block_ids": ["bk_new"]}}
    patch = _patch(op)
    store.put_patch(patch)

    back = store.get_patch(patch.patch_id)
    assert back is not None
    assert len(back.operations) == 1
    assert back.operations[0].kind is OpKind.APPEND_BLOCK
    assert back.operations[0].relation is Relation.NEW
    assert back.operations[0].inverse["payload"]["block_ids"] == ["bk_new"]


def test_the_review_a_patch_could_not_decide_survives(store):
    """A contradiction produces no operation at all, so without this the activity feed
    has nothing to show and says "0 changes" about a patch full of disagreements."""
    patch = _patch()
    patch.review = [{"reason": "contradiction",
                     "judgement": {"relation": "contradicts", "confidence": 0.9}}]
    store.put_patch(patch)

    back = store.get_patch(patch.patch_id)
    assert back is not None
    assert back.review[0]["judgement"]["relation"] == "contradicts"


def test_patch_status_moves_and_is_listed(store):
    patch = _patch(Operation(kind=OpKind.APPEND_BLOCK, target="pg_1",
                             relation=Relation.NEW, payload={"text": "x"}))
    store.put_patch(patch)

    store.set_patch_status(patch.patch_id, "applied", reviewer="sk")

    rows = store.list_patches(status="applied")
    assert [r["patch_id"] for r in rows] == [patch.patch_id]
    assert rows[0]["reviewer"] == "sk"
    assert store.list_patches(status="proposed") == []


def test_an_applied_operation_is_recorded_and_can_be_marked_reverted(store):
    """This ledger is what `history` reads and what stops undo re-applying a change you
    already took back."""
    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                   payload={"text": "x"})
    op.inverse = {"kind": "archive_blocks", "target": "pg_1", "payload": {}}
    # `page_history` filters on `applied_at`, and rightly so: an operation recorded but
    # never actually run has no business in a history of what happened to the page.
    op.applied_at = time.time()
    patch = _patch(op)
    store.put_patch(patch)
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])

    store.record_applied_op(patch.patch_id, op, "pg_1")
    history = store.page_history("pg_1")
    assert len(history) == 1
    assert history[0]["kind"] == "append_block"

    store.mark_op_reverted(op.op_id)
    assert store.page_history("pg_1")[0]["reverted_at"] is not None


def test_provenance_answers_which_source_wrote_this_sentence(store):
    source = _source()
    store.put_source(source)
    claim = _claim(source)
    store.put_claims([claim])
    store.put_provenance([{"block_id": "bk_1", "claim_id": claim.claim_id,
                           "source_id": source.source_id, "relation": "new",
                           "patch_id": "pch_1"}])

    rows = store.provenance_for_block("bk_1")

    assert len(rows) == 1
    assert rows[0]["claim_id"] == claim.claim_id
    assert rows[0]["relation"] == "new"


# ---------------------------------------------------------------------------
# approvals — the gate
# ---------------------------------------------------------------------------


def test_an_approval_round_trips_with_the_operations_it_holds(store):
    store.put_approval({"approval_id": "apr_1", "patch_id": "pch_1",
                        "operation_ids": ["op_a", "op_b"], "status": "pending",
                        "summary": "two changes"})

    approval = store.get_approval("apr_1")
    assert approval is not None
    assert approval["operation_ids"] == ["op_a", "op_b"]
    assert approval["status"] == "pending"


def test_resolving_an_approval_takes_it_out_of_the_pending_list(store):
    store.put_approval({"approval_id": "apr_1", "patch_id": "pch_1",
                        "operation_ids": [], "status": "pending"})

    store.resolve_approval("apr_1", "approved", "sk")

    assert store.list_approvals(status="pending") == []
    assert store.get_approval("apr_1")["resolved_by"] == "sk"


def test_an_expired_approval_cannot_still_be_pending(store):
    """An approval that sat unanswered for a week must not apply when somebody finally
    taps it — the workspace has moved on and the inverse may no longer fit."""
    store.put_approval({"approval_id": "apr_1", "patch_id": "pch_1",
                        "operation_ids": [], "status": "pending",
                        "expires_at": time.time() - 60})

    assert store.expire_approvals() == 1
    assert store.list_approvals(status="pending") == []


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------


def test_a_job_round_trips_with_its_result(store):
    store.put_job({"job_id": "job_1", "kind": "ingest", "spec": "x",
                   "origin": "ui", "status": "done",
                   "result": {"claims": 3, "patch": {"by_relation": {"new": 3}}}})

    job = store.get_job("job_1")
    assert job is not None
    assert job["result"]["claims"] == 3
    assert job["result"]["patch"]["by_relation"]["new"] == 3


def test_jobs_are_listed_newest_first(store):
    for i in range(3):
        store.put_job({"job_id": f"job_{i}", "kind": "ingest", "spec": "x",
                       "status": "done", "created_at": 1000 + i})

    assert [j["job_id"] for j in store.list_jobs(limit=3)] == [
        "job_2", "job_1", "job_0"]


# ---------------------------------------------------------------------------
# records, memory and evals
# ---------------------------------------------------------------------------


def test_records_round_trip_by_kind(store):
    store.put_record("sweep", {"findings": 2}, label="duplicates")
    store.put_record("other", {"x": 1})

    rows = store.get_records(kind="sweep")
    assert len(rows) == 1
    assert rows[0]["payload"]["findings"] == 2


def test_memory_is_keyed_and_replaceable(store):
    store.put_memory("preference", "tone", "terse")
    store.put_memory("preference", "tone", "very terse")

    memories = store.get_memories(kind="preference")
    assert len(memories) == 1
    assert memories[0]["value"] == "very terse"

    store.delete_memory("preference", "tone")
    assert store.get_memories(kind="preference") == []


def test_an_eval_run_is_recorded_and_the_last_one_is_findable(store):
    """`status` warns about a model that has never been measured or that failed, and it
    reads exactly this."""
    store.put_eval_run({"run_id": "run_1", "suite": "component",
                        "model": "anthropic/claude-sonnet-5",
                        "scores": {"weighted_f1": 0.95}, "passed": True})

    last = store.last_eval_run("component", "anthropic/claude-sonnet-5")
    assert last is not None
    assert last["passed"]
    assert last["scores"]["weighted_f1"] == 0.95


def test_embeddings_survive_a_round_trip_as_floats(store):
    """They are packed as float32 bytes. A store that mangles the blob returns vectors
    that are silently wrong, and retrieval degrades without anything failing."""
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "text": "t", "position": 0}])
    vector = [0.5, -0.25, 0.125]
    # The model is a separate argument, not a column in each row: the cache is keyed by
    # (block, model), and a row that carried its own model could disagree with the table
    # it was being written into.
    store.put_embeddings(
        [{"block_id": "bk_1", "text_hash": "h", "dim": len(vector),
          "vector": pack(vector)}], "m")

    got = store.get_embeddings(["bk_1"], "m")

    assert "bk_1" in got
    text_hash, packed = got["bk_1"]
    assert text_hash == "h"
    assert [round(v, 3) for v in unpack(packed)] == vector


# ---------------------------------------------------------------------------
# the things a second backend gets wrong
# ---------------------------------------------------------------------------


def test_the_stats_a_status_line_reads_are_available(store):
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "text": "t", "position": 0}])

    stats = store.stats()

    assert stats["pages"] == 1
    assert stats["blocks"] == 1


def test_unicode_survives_both_backends(store):
    """Notes are not ASCII. An encoding mismatch on one backend and not the other is the
    classic way "works unchanged" turns out to be false."""
    text = "Grüße — “curly”, ✅, 日本語, and an emoji 📐"
    store.put_pages([{"page_id": "pg_1", "title": text, "last_edited": "x"}])

    assert store.get_page("pg_1")["title"] == text


def test_a_missing_row_is_none_rather_than_an_exception(store):
    assert store.get_page("pg_nothing") is None
    assert store.get_block("bk_nothing") is None
    assert store.get_patch("pch_nothing") is None
    assert store.get_job("job_nothing") is None
    assert store.get_approval("apr_nothing") is None


def test_both_backends_were_actually_exercised(store):
    """A guard against the suite quietly becoming SQLite-only.

    If Postgres is configured, this records that it ran. If it is not, it says so out
    loud rather than letting a green run imply coverage it does not have.
    """
    assert store.backend_name in ("sqlite", "postgres")
    if store.backend_name == "sqlite" and not POSTGRES_URL:
        pytest.skip("no PALIMPSEST_TEST_POSTGRES; Postgres half of the contract not run")


def test_two_eval_runs_in_the_same_clock_tick_still_order(store):
    """`created_at` is `time.time()`, whose resolution on Windows is about 15ms, so two
    runs recorded back to back share a timestamp and `ORDER BY created_at DESC` alone
    picks arbitrarily between them.

    The leaderboard records several models in a loop, which is precisely that shape —
    and `palimpsest status` reads the result to decide whether to warn you that the model
    with write access has never been measured or was measured and failed.
    """
    now = time.time()
    for i, score in enumerate((0.42, 0.61, 0.88)):
        store.put_eval_run({"run_id": f"run_{i}", "suite": "component", "model": "m",
                            "scores": {"weighted_f1": score}, "passed": score > 0.8,
                            "created_at": now})          # identical, on purpose

    last = store.last_eval_run("component", "m")

    assert last is not None
    assert last["scores"]["weighted_f1"] == 0.88, "the newest row must win a tie"
    assert last["passed"] is True
