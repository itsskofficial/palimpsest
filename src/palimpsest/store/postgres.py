"""Postgres and Supabase — the same schema, for a deployment.

Supabase connection strings are ordinary Postgres URLs, so this works against Supabase
unchanged. What is *not* unchanged is the behaviour of the pooler Supabase puts in
front of it, and getting that wrong is the most common way a working local app falls
over the first time two requests arrive together. See `store/dsn.py` for the three
connection strings Supabase hands you and what separates them.

What this store does about it:

- **Normalises the URL** before psycopg2 sees it: `sslmode=require` on any non-local
  host (psycopg2 defaults to `prefer`, which silently accepts an unencrypted
  connection — unacceptable for a database holding a mirror of your private notes), a
  `connect_timeout` so a wrong host fails in seconds rather than hanging a container
  start, and an `application_name` so you can see which process holds a connection.
- **Uses a real connection pool.** FastAPI serves on a thread pool and a single shared
  psycopg2 connection is not thread-safe; sharing one corrupts the protocol and
  produces errors that look like data corruption.
- **Retries the initial connect** with backoff, so a container that starts before its
  database is reachable waits instead of crash-looping.
- **Refuses to migrate over a transaction pooler**, where the advisory lock that makes
  concurrent migrations safe would silently not be held.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from palimpsest.store import dsn as dsn_mod
from palimpsest.store.migrations import ALL_TABLES, BOOKKEEPING, LOCK_KEY, MIGRATIONS
from palimpsest.store.stats import StoreStats
from palimpsest.types import Claim, Judgement, Patch, Source

__all__ = ["PostgresStore"]

log = logging.getLogger("palimpsest.store")


def _plain(value: Any) -> Any:
    """Round-trip through JSON so psycopg2's Json adapter never sees a dataclass."""
    return json.loads(json.dumps(value, default=str))


class PostgresStore:
    """The SQLite store's schema over psycopg2."""

    def __init__(self, url: str, minconn: int = 1, maxconn: int = 10,
                 migrate: bool = True, retries: int = 5, retry_wait_s: float = 1.5):
        try:
            import psycopg2
            import psycopg2.extras
            import psycopg2.pool
        except ImportError as e:  # pragma: no cover - optional extra
            raise ImportError("pip install 'palimpsest[postgres]'") from e

        self._psycopg2 = psycopg2
        self._json = psycopg2.extras.Json
        self._dict_cursor = psycopg2.extras.RealDictCursor
        self.dsn = dsn_mod.parse(url)
        self.url = dsn_mod.normalise(url)

        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self._pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, self.url)
                break
            except psycopg2.Error as e:  # pragma: no cover - needs a real server
                last = e
                if attempt == retries:
                    raise ConnectionError(
                        f"could not reach {self.dsn.host}:{self.dsn.port} after "
                        f"{retries} attempts: {e}\n{self.dsn.describe()}"
                    ) from e
                wait = retry_wait_s * attempt
                log.warning("postgres connect attempt %d/%d failed (%s); retrying in %.1fs",
                            attempt, retries, e, wait)
                time.sleep(wait)
        else:  # pragma: no cover - unreachable
            raise ConnectionError(str(last))

        if migrate:
            self.migrate()

    # -- connections -----------------------------------------------------------

    @contextlib.contextmanager
    def _connection(self):
        conn = self._pool.getconn()
        try:
            conn.autocommit = True
            yield conn
        except Exception:
            with contextlib.suppress(self._psycopg2.Error):  # pragma: no cover
                conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    @contextlib.contextmanager
    def _cur(self, dict_rows: bool = True):
        with self._connection() as conn:
            cur = conn.cursor(cursor_factory=self._dict_cursor if dict_rows else None)
            try:
                yield cur
            finally:
                cur.close()

    # -- schema ----------------------------------------------------------------

    def migrate(self) -> list[str]:
        if self.dsn.is_pooler:
            raise RuntimeError(
                "refusing to migrate over a transaction pooler (port 6543).\n"
                "The advisory lock that makes concurrent migrations safe needs a session, "
                "and pgBouncer in transaction mode does not give you one.\n"
                "Use the direct / session connection string (port 5432) for migrations:\n"
                "  palimpsest db migrate --url 'postgresql://...@...:5432/postgres'\n"
                "then run the service against the pooler URL as normal."
            )
        applied: list[str] = []
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
            try:
                with conn.cursor() as cur:
                    cur.execute(BOOKKEEPING["postgres"])
                    cur.execute("SELECT id FROM schema_migrations")
                    done = {r[0] for r in cur.fetchall()}
                    for m in MIGRATIONS:
                        if m.id in done:
                            continue
                        log.info("applying migration %s (%s)", m.id, m.description)
                        cur.execute(m.postgres)
                        cur.execute(
                            "INSERT INTO schema_migrations (id, applied_at) VALUES (%s,%s) "
                            "ON CONFLICT (id) DO NOTHING", (m.id, time.time()))
                        applied.append(m.id)
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        return applied

    def applied_migrations(self) -> list[str]:
        with self._cur(dict_rows=False) as cur:
            cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
            if not cur.fetchone()[0]:
                return []
            cur.execute("SELECT id FROM schema_migrations ORDER BY id")
            return [r[0] for r in cur.fetchall()]

    def ping(self) -> float:
        t0 = time.perf_counter()
        with self._cur(dict_rows=False) as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return time.perf_counter() - t0

    # -- the Notion mirror -----------------------------------------------------

    def put_pages(self, pages: list[dict]) -> int:
        now = time.time()
        rows = [
            (p["page_id"], p.get("parent_id"), p.get("parent_kind"), p.get("title", ""),
             p.get("url"), p.get("icon"), p.get("role"), p.get("summary"),
             self._json(_plain(p.get("topics", []))), bool(p.get("archived")),
             p.get("created_time"), p.get("last_edited"), now, p.get("content_hash"))
            for p in pages
        ]
        with self._cur(dict_rows=False) as cur:
            cur.executemany(
                "INSERT INTO pages (page_id, parent_id, parent_kind, title, url, icon, role, "
                "summary, topics, archived, created_time, last_edited, synced_at, content_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (page_id) DO UPDATE SET parent_id=EXCLUDED.parent_id, "
                "parent_kind=EXCLUDED.parent_kind, title=EXCLUDED.title, url=EXCLUDED.url, "
                "icon=EXCLUDED.icon, role=COALESCE(EXCLUDED.role, pages.role), "
                "summary=COALESCE(EXCLUDED.summary, pages.summary), "
                "topics=CASE WHEN EXCLUDED.topics = '[]'::jsonb THEN pages.topics "
                "ELSE EXCLUDED.topics END, archived=EXCLUDED.archived, "
                "last_edited=EXCLUDED.last_edited, synced_at=EXCLUDED.synced_at, "
                "content_hash=EXCLUDED.content_hash",
                rows)
        return len(rows)

    def put_blocks(self, blocks: list[dict]) -> int:
        now = time.time()
        rows = [
            (b["block_id"], b["page_id"], b.get("parent_id"), b.get("type", "paragraph"),
             b.get("text", ""), int(b.get("position", 0)), int(b.get("depth", 0)),
             bool(b.get("has_children")), bool(b.get("archived")),
             self._json(_plain(b.get("raw", {}))), b.get("last_edited"), now)
            for b in blocks
        ]
        with self._cur(dict_rows=False) as cur:
            cur.executemany(
                "INSERT INTO blocks (block_id, page_id, parent_id, type, text, position, "
                "depth, has_children, archived, raw, last_edited, synced_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (block_id) DO UPDATE SET page_id=EXCLUDED.page_id, "
                "parent_id=EXCLUDED.parent_id, type=EXCLUDED.type, text=EXCLUDED.text, "
                "position=EXCLUDED.position, depth=EXCLUDED.depth, "
                "has_children=EXCLUDED.has_children, archived=EXCLUDED.archived, "
                "raw=EXCLUDED.raw, last_edited=EXCLUDED.last_edited, "
                "synced_at=EXCLUDED.synced_at",
                rows)
        return len(rows)

    def put_links(self, links: list[tuple[str, str, str | None]]) -> int:
        with self._cur(dict_rows=False) as cur:
            cur.executemany(
                "INSERT INTO links (from_page, to_page, block_id) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                [(a, b, c or "") for a, b, c in links])
        return len(links)

    def get_page(self, page_id: str) -> dict | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM pages WHERE page_id=%s", (page_id,))
            r = cur.fetchone()
        return dict(r) if r else None

    def get_pages(self, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM pages WHERE archived=false ORDER BY last_edited DESC"
        params: list[Any] = []
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_blocks(self, page_id: str | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM blocks WHERE archived=false"
        params: list[Any] = []
        if page_id:
            sql += " AND page_id=%s"
            params.append(page_id)
        sql += " ORDER BY page_id, position"
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_block(self, block_id: str) -> dict | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM blocks WHERE block_id=%s", (block_id,))
            r = cur.fetchone()
        return dict(r) if r else None

    def backlinks(self, page_id: str) -> list[str]:
        with self._cur(dict_rows=False) as cur:
            cur.execute("SELECT DISTINCT from_page FROM links WHERE to_page=%s", (page_id,))
            return [r[0] for r in cur.fetchall()]

    def set_page_profile(self, page_id: str, role: str, summary: str,
                         topics: list[str]) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute("UPDATE pages SET role=%s, summary=%s, topics=%s WHERE page_id=%s",
                        (role, summary, self._json(_plain(topics)), page_id))

    def page_last_synced(self) -> dict[str, str]:
        with self._cur(dict_rows=False) as cur:
            cur.execute("SELECT page_id, last_edited FROM pages")
            return {r[0]: (r[1] or "") for r in cur.fetchall()}

    def drop_missing(self, seen_page_ids: set[str]) -> int:
        with self._cur(dict_rows=False) as cur:
            cur.execute("SELECT page_id FROM pages WHERE archived=false")
            known = {r[0] for r in cur.fetchall()}
            gone = known - seen_page_ids
            if gone:
                cur.executemany("UPDATE pages SET archived=true WHERE page_id=%s",
                                [(p,) for p in gone])
        return len(gone)

    # -- the pipeline ----------------------------------------------------------

    def put_source(self, source: Source) -> str:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO sources (source_id, kind, title, url, author, published_at, "
                "content_hash, archive_key, text, meta, fetched_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (source_id) DO UPDATE SET text=EXCLUDED.text, "
                "title=EXCLUDED.title, meta=EXCLUDED.meta, archive_key=EXCLUDED.archive_key",
                (source.source_id, source.kind, source.title, source.url, source.author,
                 source.published_at, source.content_hash, source.archive_key, source.text,
                 self._json(_plain(source.meta)), source.fetched_at))
        return source.source_id

    def get_source(self, source_id: str) -> Source | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM sources WHERE source_id=%s", (source_id,))
            r = cur.fetchone()
        return Source.from_dict(dict(r)) if r else None

    def find_source_by_hash(self, content_hash: str) -> Source | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM sources WHERE content_hash=%s LIMIT 1", (content_hash,))
            r = cur.fetchone()
        return Source.from_dict(dict(r)) if r else None

    def list_sources(self, limit: int = 50) -> list[dict]:
        with self._cur() as cur:
            cur.execute("SELECT source_id, kind, title, url, fetched_at FROM sources "
                        "ORDER BY fetched_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def put_claims(self, claims: list[Claim]) -> int:
        now = time.time()
        with self._cur(dict_rows=False) as cur:
            cur.executemany(
                "INSERT INTO claims (claim_id, source_id, text, type, topics, confidence, "
                "anchor, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (claim_id) DO NOTHING",
                [(c.claim_id, c.source_id or "", c.text, c.type.value,
                  self._json(list(c.topics)), c.confidence,
                  self._json(c.anchor.as_dict()) if c.anchor else None, now)
                 for c in claims])
        return len(claims)

    def get_claims(self, source_id: str) -> list[Claim]:
        with self._cur() as cur:
            cur.execute("SELECT * FROM claims WHERE source_id=%s ORDER BY claim_id",
                        (source_id,))
            return [Claim.from_dict(dict(r)) for r in cur.fetchall()]

    def put_judgements(self, judgements: list[Judgement]) -> int:
        now = time.time()
        with self._cur(dict_rows=False) as cur:
            cur.executemany(
                "INSERT INTO judgements (claim_id, relation, confidence, target_page_id, "
                "target_block_id, rationale, existing_text, model, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(j.claim_id, j.relation.value, j.confidence, j.target_page_id,
                  j.target_block_id, j.rationale, j.existing_text, j.model, now)
                 for j in judgements])
        return len(judgements)

    # -- the ledger ------------------------------------------------------------

    def put_patch(self, patch: Patch) -> str:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO patches (patch_id, source_id, status, payload, n_ops, reviewer, "
                "notes, created_at, reviewed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (patch_id) DO UPDATE SET status=EXCLUDED.status, "
                "payload=EXCLUDED.payload, n_ops=EXCLUDED.n_ops, reviewer=EXCLUDED.reviewer, "
                "notes=EXCLUDED.notes",
                (patch.patch_id, patch.source_id, patch.status,
                 self._json(_plain(patch.as_dict())), len(patch.operations), patch.reviewer,
                 patch.notes, patch.created_at, patch.reviewed_at))
        return patch.patch_id

    def get_patch(self, patch_id: str) -> Patch | None:
        with self._cur() as cur:
            cur.execute("SELECT payload FROM patches WHERE patch_id=%s", (patch_id,))
            r = cur.fetchone()
        return Patch.from_dict(r["payload"]) if r else None

    def list_patches(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT patch_id, source_id, status, n_ops, reviewer, notes, created_at, "
               "reviewed_at, applied_at FROM patches")
        params: list[Any] = []
        if status:
            sql += " WHERE status=%s"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def set_patch_status(self, patch_id: str, status: str, reviewer: str | None = None,
                         notes: str | None = None) -> None:
        now = time.time()
        patch = self.get_patch(patch_id)
        payload = None
        if patch is not None:
            patch.status = status
            if reviewer:
                patch.reviewer = reviewer
                patch.reviewed_at = now
            if notes:
                patch.notes = notes
            payload = self._json(_plain(patch.as_dict()))
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "UPDATE patches SET status=%s, reviewer=COALESCE(%s, reviewer), "
                "notes=COALESCE(%s, notes), payload=COALESCE(%s, payload), "
                "reviewed_at=CASE WHEN %s IS NULL THEN reviewed_at ELSE %s END, "
                "applied_at=CASE WHEN %s='applied' THEN %s ELSE applied_at END "
                "WHERE patch_id=%s",
                (status, reviewer, notes, payload, reviewer, now, status, now, patch_id))

    def record_applied_op(self, patch_id: str, op: Any, page_id: str | None) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO applied_ops (op_id, patch_id, kind, target, page_id, payload, "
                "inverse, result, claim_id, relation, applied_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (op_id) DO UPDATE SET result=EXCLUDED.result, "
                "applied_at=EXCLUDED.applied_at",
                (op.op_id, patch_id, op.kind.value, op.target, page_id,
                 self._json(_plain(op.payload)),
                 self._json(_plain(op.inverse)) if op.inverse else None,
                 self._json(_plain(op.result)) if op.result else None,
                 op.claim_id, op.relation.value if op.relation else None, op.applied_at))

    def mark_op_reverted(self, op_id: str) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute("UPDATE applied_ops SET reverted_at=%s WHERE op_id=%s",
                        (time.time(), op_id))

    def put_provenance(self, rows: list[dict]) -> int:
        now = time.time()
        with self._cur(dict_rows=False) as cur:
            cur.executemany(
                "INSERT INTO provenance (block_id, page_id, source_id, claim_id, relation, "
                "anchor, op_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                [(r["block_id"], r.get("page_id"), r["source_id"], r.get("claim_id"),
                  r.get("relation"),
                  self._json(_plain(r.get("anchor"))) if r.get("anchor") else None,
                  r.get("op_id"), now) for r in rows])
        return len(rows)

    def provenance_for_block(self, block_id: str) -> list[dict]:
        with self._cur() as cur:
            cur.execute(
                "SELECT p.*, s.title AS source_title, s.url AS source_url, "
                "s.kind AS source_kind FROM provenance p "
                "LEFT JOIN sources s ON s.source_id = p.source_id "
                "WHERE p.block_id=%s ORDER BY p.created_at", (block_id,))
            return [dict(r) for r in cur.fetchall()]

    def page_history(self, page_id: str, limit: int = 200) -> list[dict]:
        with self._cur() as cur:
            cur.execute(
                "SELECT * FROM applied_ops WHERE page_id=%s AND applied_at IS NOT NULL "
                "ORDER BY applied_at DESC LIMIT %s", (page_id, limit))
            return [dict(r) for r in cur.fetchall()]

    # -- records ---------------------------------------------------------------

    def put_record(self, kind: str, payload: dict, label: str = "") -> int:
        with self._cur(dict_rows=False) as cur:
            cur.execute("INSERT INTO records (kind, label, payload, created_at) "
                        "VALUES (%s,%s,%s,%s) RETURNING id",
                        (kind, label, self._json(_plain(payload)), time.time()))
            return int(cur.fetchone()[0])

    def get_records(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM records"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=%s"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # -- the agent: sessions, messages, memory ---------------------------------

    def upsert_session(self, session: dict) -> str:
        now = time.time()
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO agent_sessions (session_id, chat_id, surface, summary, "
                "token_count, started_at, last_active) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, "
                "token_count=excluded.token_count, last_active=excluded.last_active",
                (session["session_id"], session.get("chat_id"),
                 session.get("surface", "telegram"), session.get("summary", ""),
                 int(session.get("token_count", 0)), session.get("started_at", now), now))
        return session["session_id"]

    def get_session_for_chat(self, chat_id: str) -> dict | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM agent_sessions WHERE chat_id=%s "
                        "ORDER BY last_active DESC LIMIT 1", (str(chat_id),))
            r = cur.fetchone()
            return dict(r) if r else None

    def add_message(self, session_id: str, role: str, content: Any,
                    trace_id: str | None = None) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute("INSERT INTO agent_messages (session_id, role, content, "
                        "trace_id, created_at) VALUES (%s,%s,%s,%s,%s)",
                        (session_id, role, self._json(_plain(content)), trace_id,
                         time.time()))

    def get_messages(self, session_id: str, limit: int = 100) -> list[dict]:
        with self._cur() as cur:
            cur.execute("SELECT role, content, trace_id, created_at FROM agent_messages "
                        "WHERE session_id=%s ORDER BY id DESC LIMIT %s",
                        (session_id, limit))
            out = [dict(r) for r in cur.fetchall()]
        out.reverse()
        return out

    def put_memory(self, kind: str, key: str, value: str, *, confidence: float = 1.0,
                   source: str | None = None) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO agent_memory (kind, key, value, confidence, source, "
                "updated_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(kind, key) DO UPDATE "
                "SET value=excluded.value, confidence=excluded.confidence, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (kind, key, value, confidence, source, time.time()))

    def get_memories(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM agent_memory"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=%s"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def delete_memory(self, kind: str, key: str) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute("DELETE FROM agent_memory WHERE kind=%s AND key=%s", (kind, key))

    # -- the agent: the approval gate ------------------------------------------

    def put_approval(self, approval: dict) -> str:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO approvals (approval_id, patch_id, session_id, chat_id, "
                "operation_ids, kind, status, summary, requested_at, resolved_at, "
                "resolved_by, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status, "
                "resolved_at=excluded.resolved_at, resolved_by=excluded.resolved_by",
                (approval["approval_id"], approval["patch_id"], approval.get("session_id"),
                 approval.get("chat_id"), self._json(_plain(approval.get("operation_ids", []))),
                 approval.get("kind", "apply"), approval.get("status", "pending"),
                 approval.get("summary"), approval.get("requested_at", time.time()),
                 approval.get("resolved_at"), approval.get("resolved_by"),
                 approval.get("expires_at")))
        return approval["approval_id"]

    def get_approval(self, approval_id: str) -> dict | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM approvals WHERE approval_id=%s", (approval_id,))
            r = cur.fetchone()
            return dict(r) if r else None

    def list_approvals(self, status: str | None = "pending",
                       limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if status:
            sql += " WHERE status=%s"
            params.append(status)
        sql += " ORDER BY requested_at DESC LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def resolve_approval(self, approval_id: str, status: str, by: str | None) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute("UPDATE approvals SET status=%s, resolved_at=%s, resolved_by=%s "
                        "WHERE approval_id=%s", (status, time.time(), by, approval_id))

    def expire_approvals(self, now: float | None = None) -> int:
        with self._cur(dict_rows=False) as cur:
            cur.execute("UPDATE approvals SET status='expired' WHERE status='pending' "
                        "AND expires_at IS NOT NULL AND expires_at < %s",
                        (now or time.time(),))
            return int(cur.rowcount or 0)

    # -- the agent: eval storage -----------------------------------------------

    def put_eval_example(self, example: dict) -> str:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO eval_examples (id, suite, input, expected, label_source, "
                "labelled_by, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET input=excluded.input, "
                "expected=excluded.expected",
                (example["id"], example.get("suite", "relation"),
                 self._json(_plain(example["input"])), self._json(_plain(example["expected"])),
                 example.get("label_source", "human"), example.get("labelled_by"),
                 example.get("created_at", time.time())))
        return example["id"]

    def get_eval_examples(self, suite: str | None = None,
                          limit: int = 1000) -> list[dict]:
        sql = "SELECT * FROM eval_examples"
        params: list[Any] = []
        if suite:
            sql += " WHERE suite=%s"
            params.append(suite)
        sql += " ORDER BY created_at LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def put_eval_run(self, run: dict) -> str:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO eval_runs (run_id, suite, model, scores, passed, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id) DO NOTHING",
                (run["run_id"], run["suite"], run.get("model"),
                 self._json(_plain(run.get("scores", {}))), bool(run.get("passed")),
                 run.get("created_at", time.time())))
        return run["run_id"]

    def get_eval_runs(self, suite: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM eval_runs"
        params: list[Any] = []
        if suite:
            sql += " WHERE suite=%s"
            params.append(suite)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # -- the capture queue -----------------------------------------------------

    def put_job(self, job: dict) -> str:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO jobs (job_id, kind, spec, source_kind, title, url, origin, "
                "status, patch_id, result, error, attempts, created_at, started_at, "
                "finished_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id) DO UPDATE SET status=excluded.status, "
                "result=excluded.result, error=excluded.error, patch_id=excluded.patch_id, "
                "finished_at=excluded.finished_at",
                (job["job_id"], job.get("kind", "ingest"), job.get("spec", ""),
                 job.get("source_kind"), job.get("title"), job.get("url"),
                 job.get("origin"), job.get("status", "queued"), job.get("patch_id"),
                 self._json(_plain(job["result"])) if job.get("result") is not None else None,
                 job.get("error"), int(job.get("attempts", 0)),
                 job.get("created_at", time.time()), job.get("started_at"),
                 job.get("finished_at")),
            )
        return job["job_id"]

    def get_job(self, job_id: str) -> dict | None:
        with self._cur() as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            sql += " WHERE status=%s"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def claim_job(self) -> dict | None:
        """Atomically take the oldest queued job.

        `FOR UPDATE SKIP LOCKED` is the Postgres way to do this: concurrent workers each
        take a different row instead of queueing behind the same one. It is also why
        this store can back more than one process, which the SQLite one cannot.
        """
        with self._cur() as cur:
            cur.execute(
                "UPDATE jobs SET status='running', started_at=%s, attempts=attempts+1 "
                "WHERE job_id = (SELECT job_id FROM jobs WHERE status='queued' "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
                (time.time(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def finish_job(self, job_id: str, status: str, *, result: dict | None = None,
                   error: str | None = None, patch_id: str | None = None) -> None:
        with self._cur(dict_rows=False) as cur:
            cur.execute(
                "UPDATE jobs SET status=%s, result=%s, error=%s, "
                "patch_id=COALESCE(%s, patch_id), finished_at=%s WHERE job_id=%s",
                (status, self._json(_plain(result)) if result is not None else None,
                 error, patch_id, time.time(), job_id),
            )

    def requeue_stale_jobs(self) -> int:
        """Put jobs that were `running` when the process died back on the queue."""
        with self._cur(dict_rows=False) as cur:
            cur.execute("UPDATE jobs SET status='queued', started_at=NULL "
                        "WHERE status='running' AND attempts < 3")
            n = cur.rowcount or 0
            cur.execute("UPDATE jobs SET status='failed', "
                        "error='abandoned after 3 attempts', finished_at=%s "
                        "WHERE status='running' AND attempts >= 3", (time.time(),))
        return int(n)

    # -- lifecycle -------------------------------------------------------------

    def stats(self) -> StoreStats:
        out = StoreStats()
        with self._cur(dict_rows=False) as cur:
            for t in ALL_TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                out[t] = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM patches WHERE status='proposed'")
            out["pending_patches"] = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')")
            out["queued_jobs"] = int(cur.fetchone()[0])
        return out

    def truncate_all(self) -> None:
        with self._cur(dict_rows=False) as cur:
            for t in ALL_TABLES:
                cur.execute(f"TRUNCATE TABLE {t} CASCADE")

    def close(self) -> None:
        self._pool.closeall()

    def __enter__(self) -> PostgresStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
