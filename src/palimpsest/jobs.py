"""The capture queue: take it now, think about it later.

Ingestion is slow — fetching a transcript, extracting claims, classifying each one
against the mirror is tens of seconds to a few minutes. The things that *capture* are
fast and short-lived: a browser popup dies the moment you click away, and a desktop
drop of nine PDFs cannot hold a socket open while they are all processed.

So capture and ingestion are separated. `submit()` writes a durable row and returns an
id in milliseconds; workers drain the queue in the background. Every surface — the
extension, the desktop app, the CLI, the review UI — goes through this one door, which
is why they all behave the same way when something takes four minutes or fails.

**The queue is durable, not in-memory.** The failure mode that matters for a capture
tool is losing something you told it to remember: you believe it has the link, it does
not, and you find out never. A row in the database survives the process; a `deque` does
not. `requeue_stale_jobs()` on startup completes the guarantee by rescuing jobs that
were mid-flight when the machine went down.

**Auto-apply lives here, and only here.** `plan.py` has already done the hard part —
contradictions and anything below the confidence floor were routed to `review` and
never reached the patch. So the rule this module implements is the simple remainder:
apply what the autonomy setting permits, and leave the rest proposed for a human. It
still consults `settings.may_auto_apply`, so `PALIMPSEST_APPLY=0` remains an absolute
veto no matter what a caller asks for.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from palimpsest.types import new_id

__all__ = ["JobQueue", "Runner", "ingest_runner", "submit_spec"]

log = logging.getLogger("palimpsest.jobs")

#: A handler: given the job row and a store bound to this worker, do the work and
#: return whatever should be recorded as the result.
Runner = Callable[[dict, Any], dict]

#: How long a worker sleeps when the queue is empty. Short enough that a capture feels
#: immediate, long enough that an idle desktop app is not spinning a core.
POLL_INTERVAL = 0.4


@dataclass
class JobQueue:
    """A durable work queue with a small pool of worker threads.

    `store_factory` must return a *fresh* store per worker rather than a shared one.
    SQLite connections are not safe to use concurrently from several threads, and WAL
    mode is specifically designed for the one-connection-per-thread arrangement — so
    handing every worker its own is both the correct and the faster choice.
    """

    store_factory: Callable[[], Any]
    handlers: dict[str, Runner]
    workers: int = 2
    poll_interval: float = POLL_INTERVAL

    _threads: list[threading.Thread] = field(default_factory=list, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)
    _started: bool = False

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> JobQueue:
        if self._started:
            return self
        self._started = True
        for i in range(max(1, self.workers)):
            t = threading.Thread(target=self._loop, name=f"palimpsest-worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        log.info("capture queue started with %d worker(s)", len(self._threads))
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
        self._started = False

    # -- submitting ------------------------------------------------------------

    def submit(self, spec: str, *, kind: str = "ingest", source_kind: str | None = None,
               title: str | None = None, url: str | None = None,
               origin: str | None = None) -> dict:
        """Queue one piece of work and return its row immediately."""
        job = {
            "job_id": new_id("job_"),
            "kind": kind,
            "spec": spec,
            "source_kind": source_kind,
            "title": title,
            "url": url,
            "origin": origin,
            "status": "queued",
            "attempts": 0,
            "created_at": time.time(),
        }
        store = self.store_factory()
        try:
            store.put_job(job)
        finally:
            _close(store)
        self._wake.set()
        return job

    # -- the worker loop -------------------------------------------------------

    def _loop(self) -> None:
        store = self.store_factory()
        try:
            while not self._stop.is_set():
                try:
                    job = store.claim_job()
                except Exception:  # pragma: no cover - transient store error
                    log.exception("could not claim a job; backing off")
                    self._stop.wait(2.0)
                    continue

                if job is None:
                    # Wait to be nudged by submit(), but wake periodically anyway so a
                    # job queued by another process is not left sitting.
                    self._wake.wait(self.poll_interval)
                    self._wake.clear()
                    continue

                self._run(store, job)
        finally:
            _close(store)

    def _run(self, store, job: dict) -> None:
        job_id = job["job_id"]
        handler = self.handlers.get(job.get("kind", "ingest"))
        if handler is None:
            store.finish_job(job_id, "failed",
                             error=f"no handler for job kind {job.get('kind')!r}")
            return

        started = time.perf_counter()
        try:
            result = handler(job, store) or {}
            store.finish_job(job_id, "done", result=result,
                             patch_id=result.get("patch_id"))
            log.info("job %s (%s) done in %.1fs", job_id, job.get("kind"),
                     time.perf_counter() - started)
        except Exception as e:
            # A failed job keeps its row and its error. Retrying is a decision for a
            # human or a later run, not something to do automatically — re-running a
            # failed extraction costs real money.
            log.exception("job %s failed", job_id)
            store.finish_job(job_id, "failed", error=f"{type(e).__name__}: {e}")


def _close(store) -> None:
    with contextlib.suppress(Exception):  # pragma: no cover - defensive
        store.close()


# ---------------------------------------------------------------------------
# the ingest runner
# ---------------------------------------------------------------------------


def ingest_runner(settings, *, model_factory: Callable[[], Any],
                  archive=None, notion_factory: Callable[[], Any] | None = None,
                  on_change: Callable[[], None] | None = None) -> Runner:
    """Build the handler that runs a capture all the way through the pipeline.

    The factories are callables rather than objects because a worker thread should
    build its own, and because a process with no `ANTHROPIC_API_KEY` must still be able
    to start a queue — it simply fails the job with a clear message when one arrives,
    instead of refusing to boot.
    """

    def run(job: dict, store) -> dict:
        from palimpsest.pipeline import ingest as run_pipeline
        from palimpsest.retrieve import Index

        spec = job.get("spec") or ""
        if not spec.strip():
            raise ValueError("the job has no spec to ingest")
        if not settings.has_model:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; extraction needs a model. The mirror, "
                "the sweeps and undo all work without one.")

        result = run_pipeline(
            spec, store, model_factory(), settings=settings,
            kind=job.get("source_kind"), index=Index(store), archive=archive,
            title=job.get("title"), url=job.get("url"),
        )
        payload = result.as_dict()

        # A telegram capture carries its chat in the origin, so a held approval can be
        # routed back to the person who sent the source.
        origin = job.get("origin") or ""
        chat = origin.split(":", 1)[1] if origin.startswith("telegram:") else None
        applied = _auto_apply(settings, store, result.patch, notion_factory, chat_id=chat)
        payload["auto_applied"] = applied

        # One row per source, so "where did all this come from" is answerable in Notion
        # rather than only from the SQLite tables. Best-effort, like every journal write.
        if notion_factory is not None and getattr(settings, "journal", True):
            from palimpsest.notion.journal import Journal

            roots = getattr(settings, "notion_root_pages", ()) or ()
            Journal(notion_factory(), store, roots[0] if roots else None).record_source(
                result.source.as_dict(), claims=len(result.claims),
                changes=int(applied.get("applied", 0)))

        # The original bytes are now safe in the artifact store, so the temp file a
        # capture surface dropped can go. Only files under a palimpsest temp dir are
        # touched — never a path the user pointed at directly, which is theirs to keep.
        _cleanup_temp(spec)

        if on_change is not None:
            on_change()
        return payload

    return run


def _cleanup_temp(spec: str) -> None:
    """Delete a temp upload once its bytes are archived. Never raises, never touches a
    file outside palimpsest's own temp directories."""
    import tempfile
    from pathlib import Path

    try:
        path = Path(spec)
        tmp = Path(tempfile.gettempdir())
        markers = ("palimpsest-uploads", "palimpsest-telegram")
        if path.is_file() and any(m in path.parts for m in markers) and tmp in path.parents:
            path.unlink()
    except Exception:  # pragma: no cover - cleanup is never load-bearing
        pass


def _auto_apply(settings, store, patch, notion_factory, *,
                chat_id: str | None = None) -> dict:
    """Route a freshly-planned patch through the one gate.

    Capture and the agent now converge here: the gate applies what the autonomy setting
    permits and files the rest as an approval a human resolves with a tap. In the
    default propose-only posture that means every edit becomes an approval — which is
    exactly the "send something, get asked to approve it" experience.
    """
    if not patch or not len(patch):
        return {"applied": 0, "held": 0, "reason": "empty patch"}

    from palimpsest import approval
    from palimpsest.notion.journal import Journal

    roots = getattr(settings, "notion_root_pages", ()) or ()

    def journal_factory():
        return Journal(notion_factory(), store, roots[0] if roots else None,
                       enabled=getattr(settings, "journal", True))

    return approval.gate(
        store, patch, settings, chat_id=chat_id,
        notion_factory=notion_factory,
        journal_factory=journal_factory if notion_factory is not None else None)


def submit_spec(store, spec: str, **kw) -> dict:
    """Queue a job against a store without owning a queue — used by the CLI."""
    job = {"job_id": new_id("job_"), "kind": kw.pop("kind", "ingest"), "spec": spec,
           "status": "queued", "attempts": 0, "created_at": time.time(), **kw}
    store.put_job(job)
    return job
