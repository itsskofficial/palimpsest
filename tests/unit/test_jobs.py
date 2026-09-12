"""The capture queue, which every surface goes through.

The failure that matters for a capture tool is not slowness, it is losing something you
told it to remember: you believe it has the link, it does not, and you find out never.
Everything here is about that — the row is durable before `submit` returns, a handler
that raises costs its own job and nothing else, and a worker that dies takes the queue's
capacity with it silently, because nothing anywhere polls for "are there still workers".

The second half is `ingest_runner`, where the one auto-apply gate lives. Its job is to
fail *legibly*: a queue that accepts captures and fails them all with `AttributeError`
looks broken, where the same queue saying "no model is configured" is a setup step.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

from palimpsest.config import Settings
from palimpsest.jobs import JobQueue, ingest_runner, submit_spec
from palimpsest.store.base import open_store


@pytest.fixture()
def settings():
    """A configuration that names no provider -- the state a fresh install is in."""
    return Settings()


@pytest.fixture()
def db(tmp_path):
    """A file-backed store, because the queue hands every worker its own connection."""
    return f"sqlite:///{tmp_path / 'jobs.db'}"


@pytest.fixture()
def opened(db):
    """Stores opened by a test, closed afterwards."""
    stores = []

    def make():
        s = open_store(db)
        stores.append(s)
        return s

    yield make
    for s in stores:
        s.close()


def _drain(queue: JobQueue, store, job_id: str, timeout: float = 5.0) -> dict:
    """Wait for one job to reach a terminal state, or say what it was still doing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.get_job(job_id)
        if job and job["status"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job never finished; last status "
                         f"{(store.get_job(job_id) or {}).get('status')!r}")


# ---------------------------------------------------------------------------
# submitting: durable before it returns
# ---------------------------------------------------------------------------


def test_a_submitted_job_is_on_disk_before_submit_returns(db, opened):
    """The browser popup dies the moment you click away. If the row is written after
    the response, everything captured in that last instant is gone and nothing says so."""
    queue = JobQueue(store_factory=lambda: open_store(db), handlers={})

    job = queue.submit("https://example.com/post", source_kind="web", title="A post")

    # A *different* connection, so this is the disk and not an in-process cache.
    row = opened().get_job(job["job_id"])
    assert row is not None
    assert row["status"] == "queued"
    assert row["spec"] == "https://example.com/post"
    assert row["title"] == "A post"


def test_submitting_does_not_need_a_running_queue(db, opened):
    """Capture must work when no worker is up — the desktop app can be closed, and the
    extension posts to whatever is listening."""
    queue = JobQueue(store_factory=lambda: open_store(db), handlers={})

    job = queue.submit("something")

    assert opened().get_job(job["job_id"])["status"] == "queued"


def test_the_origin_is_kept_so_an_approval_can_be_routed_back(db, opened):
    """A telegram capture has to be answerable in the chat it came from. Without the
    origin the approval goes nowhere and the person who sent it is never asked."""
    queue = JobQueue(store_factory=lambda: open_store(db), handlers={})

    job = queue.submit("a link", origin="telegram:12345")

    assert opened().get_job(job["job_id"])["origin"] == "telegram:12345"


def test_submit_spec_writes_the_same_shape_without_owning_a_queue(opened):
    """The CLI queues into a store it already has. A different row shape here means the
    CLI's captures are invisible to the desktop app's workers."""
    store = opened()

    job = submit_spec(store, "a link", source_kind="web")

    row = store.get_job(job["job_id"])
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["source_kind"] == "web"


# ---------------------------------------------------------------------------
# opening the same file from several threads at once — first launch
# ---------------------------------------------------------------------------


def test_several_connections_can_open_one_fresh_database_at_the_same_moment(db):
    """The desktop app's first launch, exactly: the web process and the queue workers
    each open the one file at the same moment, on a database that has never been
    migrated. The check and the migration used to be separate statements, so they all
    read an empty `schema_migrations`, all ran the same `ALTER TABLE`, and every one but
    the first died with "duplicate column name". A worker builds its store outside any
    try, so what that looked like was a queue that accepted captures and ran none."""
    barrier = threading.Barrier(6)
    errors: list[Exception] = []
    stores: list = []
    lock = threading.Lock()

    def race():
        barrier.wait()
        try:
            store = open_store(db)
        except Exception as e:
            with lock:
                errors.append(e)
            return
        with lock:
            stores.append(store)

    threads = [threading.Thread(target=race) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    try:
        assert errors == [], f"{len(errors)} of 6 could not open the database: {errors}"
        assert len(stores) == 6
        # And the schema each of them got is the finished one, not a half-migrated file.
        for store in stores:
            store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x",
                              "cover": "https://example.com/c.png"}])
            assert store.get_page("pg_1")["cover"] == "https://example.com/c.png"
    finally:
        for store in stores:
            store.close()


# ---------------------------------------------------------------------------
# draining
# ---------------------------------------------------------------------------


def test_a_worker_runs_the_job_and_records_what_came_back(db, opened):
    done = threading.Event()

    def handler(job, store):
        done.set()
        return {"claims": 3, "patch_id": "pat_1"}

    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": handler}, workers=1).start()
    try:
        job = queue.submit("a link")
        row = _drain(queue, opened(), job["job_id"])
    finally:
        queue.stop()

    assert done.is_set()
    assert row["status"] == "done"
    assert row["result"]["claims"] == 3
    assert row["patch_id"] == "pat_1", "the review UI finds the patch through the job"


def test_a_job_is_claimed_exactly_once_even_with_several_workers(db, opened):
    """Both workers winning the same row ingests the source twice and proposes two
    patches for it — which looks like the tool hallucinating duplicates of your work."""
    seen: list[str] = []
    lock = threading.Lock()

    def handler(job, store):
        with lock:
            seen.append(job["job_id"])
        return {}

    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": handler}, workers=4).start()
    try:
        jobs = [queue.submit(f"link {i}") for i in range(12)]
        store = opened()
        for job in jobs:
            _drain(queue, store, job["job_id"])
    finally:
        queue.stop()

    assert len(seen) == len(set(seen)) == 12


def test_a_handler_that_raises_costs_its_own_job_and_nothing_else(db, opened):
    """One bad PDF in a drop of nine must not take the other eight with it."""
    def handler(job, store):
        if job["spec"] == "bad":
            raise ValueError("that is not a PDF")
        return {"ok": True}

    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": handler}, workers=1).start()
    try:
        bad = queue.submit("bad")
        good = queue.submit("good")
        store = opened()
        bad_row = _drain(queue, store, bad["job_id"])
        good_row = _drain(queue, store, good["job_id"])
    finally:
        queue.stop()

    assert bad_row["status"] == "failed"
    assert "that is not a PDF" in bad_row["error"]
    assert "ValueError" in bad_row["error"], "the type is most of the diagnosis"
    assert good_row["status"] == "done"


def test_a_failed_job_is_not_retried_on_its_own(db, opened):
    """Re-running a failed extraction costs real money, and a job that fails because the
    source is a 404 fails identically forever. Retrying is a human's decision."""
    attempts: list[int] = []

    def handler(job, store):
        attempts.append(1)
        raise RuntimeError("no")

    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": handler}, workers=1).start()
    try:
        job = queue.submit("a link")
        _drain(queue, opened(), job["job_id"])
        time.sleep(0.3)
    finally:
        queue.stop()

    assert len(attempts) == 1


def test_a_kind_with_no_handler_fails_with_a_message_naming_it(db, opened):
    """Otherwise the job sits at `running` forever and the feed shows a spinner that
    never resolves — the one state a user cannot tell from "still working"."""
    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": lambda j, s: {}}, workers=1).start()
    try:
        job = queue.submit("x", kind="summarise")
        row = _drain(queue, opened(), job["job_id"])
    finally:
        queue.stop()

    assert row["status"] == "failed"
    assert "summarise" in row["error"]


def test_a_store_error_while_finishing_does_not_kill_the_worker(db, opened):
    """The one failure that is permanent and silent. `finish_job` is outside the
    handler's try, so a transient "database is locked" propagated out of the worker
    loop and ended the thread — capacity dropped by one, nothing logged it as fatal,
    nothing restarts workers, and with two workers a second occurrence stalls the queue
    with jobs sitting at `queued` and no error anywhere."""
    calls = {"n": 0}

    class Flaky:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def finish_job(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return self._inner.finish_job(*a, **kw)

    queue = JobQueue(store_factory=lambda: Flaky(open_store(db)),
                     handlers={"ingest": lambda j, s: {}}, workers=1).start()
    try:
        first = queue.submit("one")
        time.sleep(0.3)
        second = queue.submit("two")
        row = _drain(queue, opened(), second["job_id"])
    finally:
        queue.stop()

    assert row["status"] == "done", "the worker survived the first job's store error"
    assert opened().get_job(first["job_id"]) is not None


def test_a_capture_is_picked_up_promptly_rather_than_on_the_next_poll(db, opened):
    """A worker that only wakes on its timer makes every capture feel like it went
    nowhere for half a second, which is the whole perceived speed of the product."""
    started = threading.Event()
    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": lambda j, s: started.set() or {}},
                     workers=1, poll_interval=30.0).start()
    try:
        queue.submit("a link")
        assert started.wait(3.0), "submit did not wake a sleeping worker"
    finally:
        queue.stop()


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_starting_twice_does_not_double_the_workers(db):
    queue = JobQueue(store_factory=lambda: open_store(db), handlers={}, workers=2)
    try:
        assert queue.start() is queue.start()
        assert len(queue._threads) == 2
    finally:
        queue.stop()


def test_stopping_lets_the_threads_go(db):
    queue = JobQueue(store_factory=lambda: open_store(db), handlers={},
                     workers=2).start()
    threads = list(queue._threads)

    queue.stop(timeout=5.0)

    assert all(not t.is_alive() for t in threads)
    assert queue._threads == []


def test_a_queue_can_be_started_again_after_being_stopped(db, opened):
    """The desktop app stops the queue when the window closes and starts it again when
    it reopens, in one process. `stop()` set a flag nothing cleared, so the second set
    of workers read it and exited at once: captures accepted, none ever run, and the
    queue looks identical from outside to one that is working."""
    ran = threading.Event()
    queue = JobQueue(store_factory=lambda: open_store(db),
                     handlers={"ingest": lambda j, s: ran.set() or {}}, workers=1)
    queue.start()
    queue.stop()

    queue.start()
    try:
        job = queue.submit("a link")
        row = _drain(queue, opened(), job["job_id"])
    finally:
        queue.stop()

    assert ran.is_set()
    assert row["status"] == "done"


def test_at_least_one_worker_runs_even_if_zero_were_asked_for(db):
    """A queue with no workers accepts captures and never processes any of them, which
    is indistinguishable from the tool being broken."""
    queue = JobQueue(store_factory=lambda: open_store(db), handlers={}, workers=0).start()
    try:
        assert len(queue._threads) == 1
    finally:
        queue.stop()


# ---------------------------------------------------------------------------
# the ingest runner
# ---------------------------------------------------------------------------


def test_an_empty_capture_says_so_rather_than_failing_deep_in_the_pipeline(settings):
    run = ingest_runner(settings, model_factory=lambda: None)

    with pytest.raises(ValueError, match="no spec"):
        run({"spec": "   "}, None)


def test_a_capture_with_no_model_configured_names_the_way_out(settings, store):
    """This is the most common first failure: the queue is up, a capture arrives, and
    there is no key. "no model is configured" is a setup step; anything else is a bug
    report."""
    assert not settings.has_model

    run = ingest_runner(settings, model_factory=lambda: None)

    with pytest.raises(RuntimeError) as caught:
        run({"spec": "https://example.com/p"}, store)

    message = str(caught.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "the mirror, the sweeps and undo all" in message.lower(), \
        "it has to say what still works, or this reads as a dead install"


# ---------------------------------------------------------------------------
# cleaning up what a capture surface dropped
# ---------------------------------------------------------------------------


def test_a_temp_upload_is_deleted_once_its_bytes_are_archived():
    """Nine PDFs dropped on the desktop app are nine copies in the temp directory. Left
    behind they accumulate for as long as the app is installed."""
    from palimpsest.jobs import _cleanup_temp

    folder = Path(tempfile.gettempdir()) / "palimpsest-uploads"
    folder.mkdir(parents=True, exist_ok=True)
    dropped = folder / "paper.pdf"
    dropped.write_bytes(b"%PDF-1.4")

    _cleanup_temp(str(dropped))

    assert not dropped.exists()


def test_a_file_the_user_pointed_at_is_never_touched(tmp_path):
    """`palimpsest ingest ~/notes/paper.pdf` ingests a file that is *theirs*. Deleting
    it because it has been archived is unrecoverable and nobody would expect it."""
    from palimpsest.jobs import _cleanup_temp

    theirs = tmp_path / "paper.pdf"
    theirs.write_bytes(b"%PDF-1.4")

    _cleanup_temp(str(theirs))

    assert theirs.exists()


def test_a_temp_file_that_is_not_ours_is_left_alone():
    """Being under the temp directory is not enough — other programs live there too."""
    from palimpsest.jobs import _cleanup_temp

    folder = Path(tempfile.gettempdir()) / "somebody-elses-cache"
    folder.mkdir(parents=True, exist_ok=True)
    theirs = folder / "paper.pdf"
    theirs.write_bytes(b"x")
    try:
        _cleanup_temp(str(theirs))
        assert theirs.exists()
    finally:
        theirs.unlink(missing_ok=True)


def test_cleanup_of_a_url_or_a_missing_path_is_a_no_op():
    """Most specs are URLs. Cleanup is never load-bearing and must never be the reason
    a successful ingest is reported as failed."""
    from palimpsest.jobs import _cleanup_temp

    _cleanup_temp("https://example.com/post")
    _cleanup_temp("")
    _cleanup_temp("\x00 not a path at all")


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_an_empty_patch_is_not_an_application(settings, store):
    """A source that produced nothing new is the common case once a base is mature, and
    it must read as "nothing to do", not as an applied change."""
    from palimpsest.jobs import _auto_apply

    assert _auto_apply(settings, store, None, None)["applied"] == 0
    assert "empty" in _auto_apply(settings, store, None, None)["reason"]
