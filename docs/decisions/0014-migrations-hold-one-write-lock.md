# 14. Schema migrations hold one write lock from check to commit

**Status:** Accepted
**Date:** 2026-09-12

## Context

A store migrates itself when it opens: read `schema_migrations`, run whatever is missing,
record it. With one process that is fine.

The desktop app is not one process. On launch the web server opens the SQLite file, and so
does each queue worker, because SQLite connections must not be shared across threads
(ADR 6). On a fresh install all of them open an unmigrated file at the same moment. The
check and the work were separate statements, so every connection read an empty
`schema_migrations`, every one ran the same `ALTER TABLE`, and every one but the first
failed with `duplicate column name`. A worker builds its store outside any `try`, so the
visible symptom was a queue that accepted captures and processed none.

Two smaller faults sat underneath. `PRAGMA busy_timeout` was set after
`PRAGMA journal_mode=WAL`, which is the one statement it could not help, and SQLite answers
`SQLITE_BUSY` on a journal-mode change without consulting the busy handler at all.

## Decision

Migration runs inside `BEGIN IMMEDIATE`, which takes the write lock before the check and
holds it through the last statement. The loser of a race waits up to the busy timeout,
re-reads `schema_migrations`, and finds nothing left to do.

The script therefore cannot go through `executescript`, which commits before it runs and
would release the lock. It is split into statements with the standard library's
`sqlite3.complete_statement`, which understands trigger bodies and string literals.

`busy_timeout` is set first on every connection, and WAL is only requested when the file is
not already in WAL mode. WAL is a property of the file, so a connection that loses that race
can proceed.

Postgres already migrates inside a transaction and is unaffected.

## Rejected

**Treating "already exists" as success.** Catching `duplicate column` and moving on hides
the race rather than removing it, and a connection could record a migration as applied
while another is halfway through the same script.

**A lock file beside the database.** It needs cleanup after a crash, and a stale lock file
blocks every future launch. The database already has a lock designed for exactly this.

**Opening the store once and sharing it.** That is the arrangement ADR 6 exists to avoid.

## Consequences

Any number of processes can open a fresh database at once. The test for it starts six
threads behind a barrier and asserts that all six open, and that each sees the finished
schema.

A migration now holds the write lock for its whole duration, so a slow one blocks writers
until it finishes. Migrations here are small schema changes, so this is milliseconds.
