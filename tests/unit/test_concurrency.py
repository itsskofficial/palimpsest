"""Claims are classified concurrently, so the things they share must tolerate it.

Classifying a long article one claim at a time took eleven minutes. The calls are
independent — each claim is judged against the same immutable index and nothing a claim
decides affects another — so they now run eight at a time. That change turned two
latent single-thread assumptions into real bugs, and both were invisible in the obvious
way: no exception, just a wrong answer, about one run in twenty.

The first was the SQLite connection. It is opened with `check_same_thread=False` so the
queue worker and the web handlers can share a store, and `conn.execute` hands back a
cursor bound to that connection — so a second thread executing while the first iterates
its results gets a cursor whose state moved underneath it. What that looked like: one
claim came back with empty retrieval, took the "nothing matched, so this is new"
shortcut without a model call, and produced no operation.

The second was arithmetic. `usage.calls += 1` from eight threads loses increments, and
those numbers are what gets printed as "what did this cost".
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from palimpsest.llm import TokenUsage, Usage
from palimpsest.retrieve import Index

WORKERS = 8
ROUNDS = 40


def test_the_store_survives_being_read_from_many_threads(store):
    """Reads that interleave must each return their own rows.

    The failure this prevents returns *wrong rows*, not an error, so the assertion has to
    be on the content rather than on the absence of an exception.
    """
    store.put_pages([{"page_id": f"pg_{i}", "title": f"Page {i}", "last_edited": "1"}
                     for i in range(12)])
    store.put_blocks([{"block_id": f"bk_{i}", "page_id": f"pg_{i}", "type": "paragraph",
                       "position": 0, "text": f"the text of block number {i} here"}
                      for i in range(12)])

    def read(i: int) -> tuple[str, str]:
        page = store.get_page(f"pg_{i}")
        blocks = store.get_blocks(f"pg_{i}")
        return page["title"], blocks[0]["block_id"]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for _ in range(ROUNDS):
            results = list(pool.map(read, range(12)))
            assert results == [(f"Page {i}", f"bk_{i}") for i in range(12)]


def test_retrieval_is_identical_whether_or_not_it_is_run_concurrently(store):
    """The index promises to be in-memory; this is what that promise is worth.

    `pages_for` used to query the store for backlinks in the middle of ranking, so two
    threads ranking at once could each get the other's rows. Backlinks are now loaded at
    build time and the query path touches nothing shared and mutable.
    """
    store.put_pages([{"page_id": f"pg_{i}", "title": f"Topic {i}", "last_edited": "1"}
                     for i in range(8)])
    store.put_blocks([
        {"block_id": f"bk_{i}", "page_id": f"pg_{i}", "type": "paragraph", "position": 0,
         "text": f"Topic {i} concerns gradient descent and attention scaling in detail."}
        for i in range(8)])
    store.put_links([(f"pg_{i}", f"pg_{(i + 1) % 8}", None) for i in range(8)])

    index = Index(store)
    query = "attention scaling and gradient descent"
    expected = [h.page_id for h in index.pages_for(query)]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        runs = list(pool.map(lambda _: [h.page_id for h in index.pages_for(query)],
                             range(WORKERS * 6)))

    assert all(run == expected for run in runs)


def test_the_index_does_not_touch_the_store_once_it_is_built(store):
    """A database round trip inside a ranking loop is both slow and, now, unsafe."""
    store.put_pages([{"page_id": "pg_1", "title": "One", "last_edited": "1"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "position": 0, "text": "attention scaling and gradient descent"}])
    index = Index(store)

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"the index reached for store.{name} during a query")

    index.store = Exploding()
    index.pages_for("attention scaling")
    index.blocks_for("attention scaling")


def test_usage_counts_every_call_when_several_threads_report_at_once():
    """`self.calls += 1` is a read-modify-write. Losing some of them makes the cost
    line, which is the one number people check, quietly too low."""
    usage = Usage()
    total = WORKERS * 200

    def record(_: int) -> None:
        usage.add("classify", TokenUsage(input=10, output=2), 0.001)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(record, range(total)))

    assert usage.calls == total
    assert usage.by_task["classify"] == total
    assert usage.input_tokens == total * 10


def test_classify_preserves_the_order_of_the_claims_it_was_given(store):
    """Concurrency must not make a patch depend on which call finished first — the
    evals compare two runs over the same source."""
    import random
    import time

    from palimpsest.relate import classify
    from palimpsest.types import Claim, ClaimType, Source, new_id

    claims = [Claim(claim_id=f"clm_{i:03d}", text=f"Fact number {i} about attention.",
                    type=ClaimType.FACT, topics=()) for i in range(24)]
    source = Source(source_id=new_id("src_"), kind="text", title="t", text="x")

    class SlowModel:
        model, name, base_url = "fake", "fake", None

        def __init__(self):
            self.usage = Usage()
            self._lock = threading.Lock()

        def json(self, *, task, system, prompt, schema, **kw):
            time.sleep(random.uniform(0, 0.01))
            self.usage.add(task, TokenUsage(input=1, output=1), 0.0)
            return {"relation": "new", "confidence": 0.9, "target_page_id": None,
                    "target_block_id": None, "existing_text": None, "rationale": "r"}

    store.put_pages([{"page_id": "pg_1", "title": "Attention", "last_edited": "1"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "position": 0, "text": "Attention scales by one over root d_k."}])

    result = classify(claims, source, Index(store), SlowModel(), workers=WORKERS)

    assert [j.claim_id for j in result.judgements] == [c.claim_id for c in claims]
