"""SQLite store — the default, and the one a personal knowledge base should use.

`sqlite3` is in the standard library, so this costs nothing to install, works on every
machine, and leaves you with a single file you can copy, back up, or delete. For a
mirror of a few thousand Notion blocks it is not a compromise: the whole retrieval
index is built from it in milliseconds, which is faster than one Notion API round trip.

Design notes worth knowing:

- **Blocks store both rendered text and the raw JSON.** The text is what retrieval and
  the classifier read; the raw block is what the applier needs to build an exact
  inverse. Keeping only one of them would make either search or undo impossible.
- **`put_*` are idempotent on their primary keys.** Re-syncing a page or re-ingesting a
  source overwrites rather than duplicating, so a crashed sync is safe to just re-run.
- **The ledger is append-only.** `applied_ops` rows are never updated except to stamp
  `reverted_at`. A history you can rewrite is not a history.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from palimpsest.store.migrations import ALL_TABLES, BOOKKEEPING, MIGRATIONS
from palimpsest.store.stats import StoreStats
from palimpsest.types import Claim, Judgement, Patch, Source

__all__ = ["SQLiteStore"]


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _uj(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, dict | list):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class SQLiteStore:
    """The mirror, the pipeline and the ledger in one file."""

    def __init__(self, path: str | Path = "palimpsest.db"):
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            # WAL lets the review UI read while a sync writes, which is the whole
            # point of having a store rather than a dict.
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Queue workers each hold their own connection, so two of them can want the
        # write lock at once. Without a busy timeout that is an immediate
        # "database is locked" rather than a wait of a few milliseconds.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # -- schema ----------------------------------------------------------------

    def migrate(self) -> list[str]:
        applied: list[str] = []
        self.conn.executescript(BOOKKEEPING["sqlite"])
        done = {r[0] for r in self.conn.execute("SELECT id FROM schema_migrations")}
        for m in MIGRATIONS:
            if m.id in done:
                continue
            self.conn.executescript(m.sqlite)
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?,?)",
                (m.id, time.time()),
            )
            applied.append(m.id)
        self.conn.commit()
        return applied

    def applied_migrations(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM schema_migrations ORDER BY id")]

    def ping(self) -> float:
        t0 = time.perf_counter()
        self.conn.execute("SELECT 1").fetchone()
        return time.perf_counter() - t0

    # -- the Notion mirror -----------------------------------------------------

    def put_pages(self, pages: list[dict]) -> int:
        now = time.time()
        rows = [
            (p["page_id"], p.get("parent_id"), p.get("parent_kind"), p.get("title", ""),
             p.get("url"), p.get("icon"), p.get("role"), p.get("summary"),
             _j(p.get("topics", [])), int(bool(p.get("archived"))),
             p.get("created_time"), p.get("last_edited"), now, p.get("content_hash"))
            for p in pages
        ]
        # COALESCE on role/summary/topics: a re-sync must not wipe a profile that
        # was computed by the model, since the Notion API never returns one.
        self.conn.executemany(
            "INSERT INTO pages (page_id, parent_id, parent_kind, title, url, icon, role, "
            "summary, topics, archived, created_time, last_edited, synced_at, content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(page_id) DO UPDATE SET "
            "parent_id=excluded.parent_id, parent_kind=excluded.parent_kind, "
            "title=excluded.title, url=excluded.url, icon=excluded.icon, "
            "role=COALESCE(excluded.role, pages.role), "
            "summary=COALESCE(excluded.summary, pages.summary), "
            "topics=CASE WHEN excluded.topics='[]' THEN pages.topics ELSE excluded.topics END, "
            "archived=excluded.archived, last_edited=excluded.last_edited, "
            "synced_at=excluded.synced_at, content_hash=excluded.content_hash",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def put_blocks(self, blocks: list[dict]) -> int:
        now = time.time()
        rows = [
            (b["block_id"], b["page_id"], b.get("parent_id"), b.get("type", "paragraph"),
             b.get("text", ""), int(b.get("position", 0)), int(b.get("depth", 0)),
             int(bool(b.get("has_children"))), int(bool(b.get("archived"))),
             _j(b.get("raw", {})), b.get("last_edited"), now)
            for b in blocks
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO blocks (block_id, page_id, parent_id, type, text, "
            "position, depth, has_children, archived, raw, last_edited, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def put_links(self, links: list[tuple[str, str, str | None]]) -> int:
        self.conn.executemany(
            "INSERT OR IGNORE INTO links (from_page, to_page, block_id) VALUES (?,?,?)",
            [(a, b, c or "") for a, b, c in links],
        )
        self.conn.commit()
        return len(links)

    @staticmethod
    def _page_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["topics"] = _uj(d.get("topics"), [])
        d["archived"] = bool(d.get("archived"))
        return d

    def get_page(self, page_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()
        return self._page_row(r) if r else None

    def get_pages(self, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM pages WHERE archived=0 ORDER BY last_edited DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._page_row(r) for r in self.conn.execute(sql)]

    @staticmethod
    def _block_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["raw"] = _uj(d.get("raw"), {})
        d["has_children"] = bool(d.get("has_children"))
        d["archived"] = bool(d.get("archived"))
        return d

    def get_blocks(self, page_id: str | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM blocks WHERE archived=0"
        params: list[Any] = []
        if page_id:
            sql += " AND page_id=?"
            params.append(page_id)
        sql += " ORDER BY page_id, position"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._block_row(r) for r in self.conn.execute(sql, params)]

    def get_block(self, block_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM blocks WHERE block_id=?", (block_id,)).fetchone()
        return self._block_row(r) if r else None

    def backlinks(self, page_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT from_page FROM links WHERE to_page=?", (page_id,))]

    def set_page_profile(self, page_id: str, role: str, summary: str,
                         topics: list[str]) -> None:
        self.conn.execute(
            "UPDATE pages SET role=?, summary=?, topics=? WHERE page_id=?",
            (role, summary, _j(topics), page_id),
        )
        self.conn.commit()

    def page_last_synced(self) -> dict[str, str]:
        """`{page_id: last_edited}` — what incremental sync compares against."""
        return {r[0]: (r[1] or "") for r in self.conn.execute(
            "SELECT page_id, last_edited FROM pages")}

    def drop_missing(self, seen_page_ids: set[str]) -> int:
        """Mark pages absent from a full sync as archived.

        Archived rather than deleted: a page you removed in Notion may still be the
        provenance target of ledger entries, and a dangling foreign key in the history
        is worse than a tombstone.
        """
        known = {r[0] for r in self.conn.execute("SELECT page_id FROM pages WHERE archived=0")}
        gone = known - seen_page_ids
        if gone:
            self.conn.executemany("UPDATE pages SET archived=1 WHERE page_id=?",
                                  [(p,) for p in gone])
            self.conn.commit()
        return len(gone)

    # -- the pipeline ----------------------------------------------------------

    def put_source(self, source: Source) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO sources (source_id, kind, title, url, author, "
            "published_at, content_hash, archive_key, text, meta, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source.source_id, source.kind, source.title, source.url, source.author,
             source.published_at, source.content_hash, source.archive_key, source.text,
             _j(source.meta), source.fetched_at),
        )
        self.conn.commit()
        return source.source_id

    def get_source(self, source_id: str) -> Source | None:
        r = self.conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["meta"] = _uj(d.get("meta"), {})
        return Source.from_dict(d)

    def find_source_by_hash(self, content_hash: str) -> Source | None:
        r = self.conn.execute("SELECT * FROM sources WHERE content_hash=? LIMIT 1",
                              (content_hash,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["meta"] = _uj(d.get("meta"), {})
        return Source.from_dict(d)

    def list_sources(self, limit: int = 50) -> list[dict]:
        return [
            {k: r[k] for k in ("source_id", "kind", "title", "url", "fetched_at")}
            for r in self.conn.execute(
                "SELECT source_id, kind, title, url, fetched_at FROM sources "
                "ORDER BY fetched_at DESC LIMIT ?", (limit,))
        ]

    def put_claims(self, claims: list[Claim]) -> int:
        now = time.time()
        self.conn.executemany(
            "INSERT OR REPLACE INTO claims (claim_id, source_id, text, type, topics, "
            "confidence, anchor, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(c.claim_id, c.source_id or "", c.text, c.type.value, _j(list(c.topics)),
              c.confidence, _j(c.anchor.as_dict()) if c.anchor else None, now)
             for c in claims],
        )
        self.conn.commit()
        return len(claims)

    def get_claims(self, source_id: str) -> list[Claim]:
        out = []
        for r in self.conn.execute("SELECT * FROM claims WHERE source_id=? ORDER BY claim_id",
                                   (source_id,)):
            d = dict(r)
            d["topics"] = _uj(d.get("topics"), [])
            d["anchor"] = _uj(d.get("anchor"), None)
            out.append(Claim.from_dict(d))
        return out

    def put_judgements(self, judgements: list[Judgement]) -> int:
        now = time.time()
        self.conn.executemany(
            "INSERT INTO judgements (claim_id, relation, confidence, target_page_id, "
            "target_block_id, rationale, existing_text, model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(j.claim_id, j.relation.value, j.confidence, j.target_page_id,
              j.target_block_id, j.rationale, j.existing_text, j.model, now)
             for j in judgements],
        )
        self.conn.commit()
        return len(judgements)

    # -- the ledger ------------------------------------------------------------

    def put_patch(self, patch: Patch) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO patches (patch_id, source_id, status, payload, n_ops, "
            "reviewer, notes, created_at, reviewed_at, applied_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (patch.patch_id, patch.source_id, patch.status, _j(patch.as_dict()),
             len(patch.operations), patch.reviewer, patch.notes, patch.created_at,
             patch.reviewed_at, None),
        )
        self.conn.commit()
        return patch.patch_id

    def get_patch(self, patch_id: str) -> Patch | None:
        r = self.conn.execute("SELECT payload FROM patches WHERE patch_id=?",
                              (patch_id,)).fetchone()
        return Patch.from_dict(_uj(r[0], {})) if r else None

    def list_patches(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT patch_id, source_id, status, n_ops, reviewer, notes, created_at, "
               "reviewed_at, applied_at FROM patches")
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def set_patch_status(self, patch_id: str, status: str, reviewer: str | None = None,
                         notes: str | None = None) -> None:
        now = time.time()
        patch = self.get_patch(patch_id)
        if patch is not None:
            patch.status = status
            if reviewer:
                patch.reviewer = reviewer
                patch.reviewed_at = now
            if notes:
                patch.notes = notes
            self.conn.execute("UPDATE patches SET payload=? WHERE patch_id=?",
                              (_j(patch.as_dict()), patch_id))
        self.conn.execute(
            "UPDATE patches SET status=?, reviewer=COALESCE(?, reviewer), "
            "notes=COALESCE(?, notes), reviewed_at=CASE WHEN ? IS NULL THEN reviewed_at "
            "ELSE ? END, applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END "
            "WHERE patch_id=?",
            (status, reviewer, notes, reviewer, now, status, now, patch_id),
        )
        self.conn.commit()

    def record_applied_op(self, patch_id: str, op: Any, page_id: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO applied_ops (op_id, patch_id, kind, target, page_id, "
            "payload, inverse, result, claim_id, relation, applied_at, reverted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (op.op_id, patch_id, op.kind.value, op.target, page_id, _j(op.payload),
             _j(op.inverse) if op.inverse else None, _j(op.result) if op.result else None,
             op.claim_id, op.relation.value if op.relation else None, op.applied_at),
        )
        self.conn.commit()

    def mark_op_reverted(self, op_id: str) -> None:
        self.conn.execute("UPDATE applied_ops SET reverted_at=? WHERE op_id=?",
                          (time.time(), op_id))
        self.conn.commit()

    def put_provenance(self, rows: list[dict]) -> int:
        now = time.time()
        self.conn.executemany(
            "INSERT INTO provenance (block_id, page_id, source_id, claim_id, relation, "
            "anchor, op_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(r["block_id"], r.get("page_id"), r["source_id"], r.get("claim_id"),
              r.get("relation"), _j(r.get("anchor")) if r.get("anchor") else None,
              r.get("op_id"), now) for r in rows],
        )
        self.conn.commit()
        return len(rows)

    def provenance_for_block(self, block_id: str) -> list[dict]:
        out = []
        for r in self.conn.execute(
            "SELECT p.*, s.title AS source_title, s.url AS source_url, s.kind AS source_kind "
            "FROM provenance p LEFT JOIN sources s ON s.source_id = p.source_id "
            "WHERE p.block_id=? ORDER BY p.created_at", (block_id,)
        ):
            d = dict(r)
            d["anchor"] = _uj(d.get("anchor"), None)
            out.append(d)
        return out

    def page_history(self, page_id: str, limit: int = 200) -> list[dict]:
        """Every applied operation that touched this page, newest first.

        This is the time-travel primitive: replay the inverses from now back to a
        timestamp and you have the page as it stood then. Notion cannot do this.
        """
        out = []
        for r in self.conn.execute(
            "SELECT * FROM applied_ops WHERE page_id=? AND applied_at IS NOT NULL "
            "ORDER BY applied_at DESC LIMIT ?", (page_id, limit)
        ):
            d = dict(r)
            d["payload"] = _uj(d.get("payload"), {})
            d["inverse"] = _uj(d.get("inverse"), None)
            d["result"] = _uj(d.get("result"), None)
            out.append(d)
        return out

    # -- records ---------------------------------------------------------------

    def put_record(self, kind: str, payload: dict, label: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO records (kind, label, payload, created_at) VALUES (?,?,?,?)",
            (kind, label, _j(payload), time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def get_records(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM records"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["payload"] = _uj(d.get("payload"), {})
            out.append(d)
        return out

    # -- the agent: sessions, messages, memory ---------------------------------

    def upsert_session(self, session: dict) -> str:
        now = time.time()
        self.conn.execute(
            "INSERT INTO agent_sessions (session_id, chat_id, surface, summary, "
            "token_count, started_at, last_active) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, "
            "token_count=excluded.token_count, last_active=excluded.last_active",
            (session["session_id"], session.get("chat_id"),
             session.get("surface", "telegram"), session.get("summary", ""),
             int(session.get("token_count", 0)), session.get("started_at", now), now),
        )
        self.conn.commit()
        return session["session_id"]

    def get_session_for_chat(self, chat_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM agent_sessions WHERE chat_id=? ORDER BY last_active DESC "
            "LIMIT 1", (str(chat_id),)).fetchone()
        return dict(r) if r else None

    def add_message(self, session_id: str, role: str, content: Any,
                    trace_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO agent_messages (session_id, role, content, trace_id, "
            "created_at) VALUES (?,?,?,?,?)",
            (session_id, role, _j(content), trace_id, time.time()))
        self.conn.commit()

    def get_messages(self, session_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content, trace_id, created_at FROM agent_messages "
            "WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        out = [{**dict(r), "content": _uj(r["content"], None)} for r in rows]
        out.reverse()  # oldest first, for replay into the model
        return out

    def put_memory(self, kind: str, key: str, value: str, *, confidence: float = 1.0,
                   source: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO agent_memory (kind, key, value, confidence, source, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
            "value=excluded.value, confidence=excluded.confidence, "
            "source=excluded.source, updated_at=excluded.updated_at",
            (kind, key, value, confidence, source, time.time()))
        self.conn.commit()

    def get_memories(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM agent_memory"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def delete_memory(self, kind: str, key: str) -> None:
        self.conn.execute("DELETE FROM agent_memory WHERE kind=? AND key=?", (kind, key))
        self.conn.commit()

    # -- the agent: the approval gate ------------------------------------------

    def put_approval(self, approval: dict) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO approvals (approval_id, patch_id, session_id, "
            "chat_id, operation_ids, kind, status, summary, requested_at, resolved_at, "
            "resolved_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval["approval_id"], approval["patch_id"], approval.get("session_id"),
             approval.get("chat_id"), _j(approval.get("operation_ids", [])),
             approval.get("kind", "apply"), approval.get("status", "pending"),
             approval.get("summary"), approval.get("requested_at", time.time()),
             approval.get("resolved_at"), approval.get("resolved_by"),
             approval.get("expires_at")))
        self.conn.commit()
        return approval["approval_id"]

    def get_approval(self, approval_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM approvals WHERE approval_id=?",
                              (approval_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["operation_ids"] = _uj(d.get("operation_ids"), [])
        return d

    def list_approvals(self, status: str | None = "pending",
                       limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY requested_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["operation_ids"] = _uj(d.get("operation_ids"), [])
            out.append(d)
        return out

    def resolve_approval(self, approval_id: str, status: str, by: str | None) -> None:
        self.conn.execute(
            "UPDATE approvals SET status=?, resolved_at=?, resolved_by=? "
            "WHERE approval_id=?", (status, time.time(), by, approval_id))
        self.conn.commit()

    def expire_approvals(self, now: float | None = None) -> int:
        now = now or time.time()
        cur = self.conn.execute(
            "UPDATE approvals SET status='expired' WHERE status='pending' "
            "AND expires_at IS NOT NULL AND expires_at < ?", (now,))
        self.conn.commit()
        return int(cur.rowcount or 0)

    # -- the agent: eval storage -----------------------------------------------

    def put_eval_example(self, example: dict) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO eval_examples (id, suite, input, expected, "
            "label_source, labelled_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (example["id"], example.get("suite", "relation"), _j(example["input"]),
             _j(example["expected"]), example.get("label_source", "human"),
             example.get("labelled_by"), example.get("created_at", time.time())))
        self.conn.commit()
        return example["id"]

    def get_eval_examples(self, suite: str | None = None,
                          limit: int = 1000) -> list[dict]:
        sql = "SELECT * FROM eval_examples"
        params: list[Any] = []
        if suite:
            sql += " WHERE suite=?"
            params.append(suite)
        sql += " ORDER BY created_at LIMIT ?"
        params.append(limit)
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["input"] = _uj(d.get("input"), {})
            d["expected"] = _uj(d.get("expected"), {})
            out.append(d)
        return out

    def put_eval_run(self, run: dict) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO eval_runs (run_id, suite, model, scores, passed, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (run["run_id"], run["suite"], run.get("model"), _j(run.get("scores", {})),
             int(bool(run.get("passed"))), run.get("created_at", time.time())))
        self.conn.commit()
        return run["run_id"]

    def get_eval_runs(self, suite: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM eval_runs"
        params: list[Any] = []
        if suite:
            sql += " WHERE suite=?"
            params.append(suite)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["scores"] = _uj(d.get("scores"), {})
            d["passed"] = bool(d.get("passed"))
            out.append(d)
        return out

    # -- the capture queue -----------------------------------------------------

    def put_job(self, job: dict) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, kind, spec, source_kind, title, url, "
            "origin, status, patch_id, result, error, attempts, created_at, started_at, "
            "finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["job_id"], job.get("kind", "ingest"), job.get("spec", ""),
             job.get("source_kind"), job.get("title"), job.get("url"),
             job.get("origin"), job.get("status", "queued"), job.get("patch_id"),
             _j(job["result"]) if job.get("result") is not None else None,
             job.get("error"), int(job.get("attempts", 0)),
             job.get("created_at", time.time()), job.get("started_at"),
             job.get("finished_at")),
        )
        self.conn.commit()
        return job["job_id"]

    def get_job(self, job_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["result"] = _uj(d.get("result"), None)
        return d

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["result"] = _uj(d.get("result"), None)
            out.append(d)
        return out

    def claim_job(self) -> dict | None:
        """Atomically take the oldest queued job, or return `None`.

        The `WHERE status='queued'` in the UPDATE is what makes this safe: two workers
        racing on the same row produce one `rowcount == 1` and one `rowcount == 0`, so
        exactly one of them runs the job. Selecting and then updating without that
        predicate would let both win, and the visible symptom is a source ingested
        twice with two patches proposed for it.
        """
        row = self.conn.execute(
            "SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = self.conn.execute(
            "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 "
            "WHERE job_id=? AND status='queued'",
            (time.time(), row[0]),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None  # another worker took it
        return self.get_job(row[0])

    def finish_job(self, job_id: str, status: str, *, result: dict | None = None,
                   error: str | None = None, patch_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, result=?, error=?, patch_id=COALESCE(?, patch_id), "
            "finished_at=? WHERE job_id=?",
            (status, _j(result) if result is not None else None, error, patch_id,
             time.time(), job_id),
        )
        self.conn.commit()

    def requeue_stale_jobs(self) -> int:
        """Put jobs that were `running` when the process died back on the queue.

        Called on startup. Without it, a crash during ingestion leaves a job stuck in
        `running` forever and the capture is silently lost — which is the one failure a
        capture tool must not have.
        """
        cur = self.conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL "
            "WHERE status='running' AND attempts < 3")
        self.conn.execute(
            "UPDATE jobs SET status='failed', error='abandoned after 3 attempts', "
            "finished_at=? WHERE status='running' AND attempts >= 3", (time.time(),))
        self.conn.commit()
        return int(cur.rowcount or 0)

    # -- lifecycle -------------------------------------------------------------

    def stats(self) -> StoreStats:
        out = StoreStats()
        for t in ALL_TABLES:
            out[t] = int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        out["pending_patches"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM patches WHERE status='proposed'").fetchone()[0])
        out["queued_jobs"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0])
        return out

    def truncate_all(self) -> None:
        for t in ALL_TABLES:
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
