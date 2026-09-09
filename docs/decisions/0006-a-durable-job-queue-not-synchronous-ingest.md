# 6. A durable job queue, not synchronous ingest

**Status:** Accepted
**Date:** 2026-09-09

## Context

Ingestion is slow: fetching a transcript, extracting claims and classifying each against
the mirror is tens of seconds for an article and minutes for a lecture. The surfaces that
capture are the opposite. A browser popup is destroyed the moment you click away, which
cancels its request mid-extraction. The desktop `Ctrl+Shift+Space` box is designed to
vanish the instant you press Enter — that is the point of it — so it cannot hold a socket.

The failure that matters for a capture tool is losing something you told it to remember.
You believe it has the link, it does not, and you find out never. A synchronous request
has that failure built in.

## Decision

Every interactive surface — the UI drop zone, the extension, the desktop shortcut, the
bot, the CLI — posts to `POST /v1/jobs`, which writes a durable row and returns an id in
milliseconds. Workers drain the queue in the background. `POST /v1/ingest` stays
synchronous for scripts that want the patch back.

The queue is a table, not a `deque`: a row survives the process and an in-memory structure
does not, and "the process died" is the case the guarantee is for.
`requeue_stale_jobs()` on startup rescues jobs that were mid-flight when the machine went
down.

Auto-apply lives in the job handler and only there. `plan.py` has already routed
contradictions and anything below the confidence floor to `review`, so the remaining rule
is small — apply what the autonomy ladder permits, leave the rest proposed — and it still
calls `settings.may_auto_apply`, so writes-off remains an absolute veto.

## Consequences

### What this buys

A capture outlives the window that made it, which is what lets the shortcut box disappear
before the work starts. Every surface behaves the same way when something takes four
minutes or fails, because they all go through one door. Held operations are also split
rather than stranded whole: a source usually yields six obvious citations and one
`supersedes` worth a look, and holding the six hostage to the one is how a review queue
becomes something you stop opening.

### What this costs

Asynchrony everywhere. A capture surface cannot tell you what happened — it gets an id
and must poll, so the UI needs a live feed and the bot has to message you back minutes
later. "It worked" and "it produced this" are two separate moments in every client.

There is also a queue to operate: stuck jobs, a stale-requeue path that can re-run work
which had already partly succeeded, and a worker pool whose failures show up in logs
rather than in an HTTP response.

## Revisit if

A capture surface appears that genuinely needs the patch in its response and cannot poll.
`/v1/ingest` already serves that; making it the default again would require ingestion to
become fast enough that a popup outlives it.
