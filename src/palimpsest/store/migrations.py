"""Schema, versioned, applied explicitly.

`CREATE TABLE IF NOT EXISTS` on connect is fine for one process against one file. It is
not fine once a deployment has two tasks starting together against a shared Postgres,
because "if not exists" races are silent and partial.

So the schema lives here as an ordered list of migrations and a `schema_migrations`
table records what has been applied. Postgres migrations run under an advisory lock, so
two containers booting together do not both try.

**Why the mirror exists at all.** Notion's API gives you no diff primitive, no
transactions, and no usable version history. Every guarantee this project makes —
exact undo, time travel, "which source produced this sentence" — comes from keeping our
own copy and our own ledger. The mirror is not a cache; it is the source of truth for
everything except the current live content.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BOOKKEEPING", "LOCK_KEY", "MIGRATIONS", "Migration", "postgres_sql", "sqlite_sql"]

#: Arbitrary but fixed key for `pg_advisory_lock`. Two palimpsest processes migrating
#: the same database must pick the same number, so it is a constant.
LOCK_KEY = 5_120_907


@dataclass(frozen=True)
class Migration:
    id: str
    description: str
    postgres: str
    sqlite: str


# --- 0001: the mirror, the pipeline and the ledger --------------------------

_INIT_PG = """
-- The Notion mirror -------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
    page_id       TEXT PRIMARY KEY,
    parent_id     TEXT,
    parent_kind   TEXT,
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT,
    icon          TEXT,
    role          TEXT,
    summary       TEXT,
    topics        JSONB NOT NULL DEFAULT '[]'::jsonb,
    archived      BOOLEAN NOT NULL DEFAULT FALSE,
    created_time  TEXT,
    last_edited   TEXT,
    synced_at     DOUBLE PRECISION NOT NULL,
    content_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_parent ON pages(parent_id);
CREATE INDEX IF NOT EXISTS idx_pages_edited ON pages(last_edited DESC);

CREATE TABLE IF NOT EXISTS blocks (
    block_id     TEXT PRIMARY KEY,
    page_id      TEXT NOT NULL,
    parent_id    TEXT,
    type         TEXT NOT NULL,
    text         TEXT NOT NULL DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0,
    depth        INTEGER NOT NULL DEFAULT 0,
    has_children BOOLEAN NOT NULL DEFAULT FALSE,
    archived     BOOLEAN NOT NULL DEFAULT FALSE,
    raw          JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_edited  TEXT,
    synced_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_page ON blocks(page_id, position);
CREATE INDEX IF NOT EXISTS idx_blocks_parent ON blocks(parent_id);

-- Backlinks: which page mentions which. Drives graph expansion in retrieval.
CREATE TABLE IF NOT EXISTS links (
    from_page TEXT NOT NULL,
    to_page   TEXT NOT NULL,
    block_id  TEXT,
    PRIMARY KEY (from_page, to_page, block_id)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_page);

-- The pipeline ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT,
    author       TEXT,
    published_at TEXT,
    content_hash TEXT NOT NULL,
    archive_key  TEXT,
    text         TEXT NOT NULL DEFAULT '',
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(content_hash);
CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(url);

CREATE TABLE IF NOT EXISTS claims (
    claim_id   TEXT PRIMARY KEY,
    source_id  TEXT NOT NULL,
    text       TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'fact',
    topics     JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    anchor     JSONB,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_id);

CREATE TABLE IF NOT EXISTS judgements (
    id              BIGSERIAL PRIMARY KEY,
    claim_id        TEXT NOT NULL,
    relation        TEXT NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    target_page_id  TEXT,
    target_block_id TEXT,
    rationale       TEXT,
    existing_text   TEXT,
    model           TEXT,
    created_at      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgements_claim ON judgements(claim_id);

-- The ledger: this is what makes undo and time travel real -----------------
CREATE TABLE IF NOT EXISTS patches (
    patch_id    TEXT PRIMARY KEY,
    source_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed',
    payload     JSONB NOT NULL,
    n_ops       INTEGER NOT NULL DEFAULT 0,
    reviewer    TEXT,
    notes       TEXT,
    created_at  DOUBLE PRECISION NOT NULL,
    reviewed_at DOUBLE PRECISION,
    applied_at  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_patches_status ON patches(status, created_at DESC);

-- One row per applied operation. The inverse is written BEFORE the operation
-- runs, which is what makes a half-applied patch fully reversible.
CREATE TABLE IF NOT EXISTS applied_ops (
    op_id      TEXT PRIMARY KEY,
    patch_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    target     TEXT NOT NULL,
    page_id    TEXT,
    payload    JSONB NOT NULL,
    inverse    JSONB,
    result     JSONB,
    claim_id   TEXT,
    relation   TEXT,
    applied_at DOUBLE PRECISION,
    reverted_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_ops_patch ON applied_ops(patch_id);
CREATE INDEX IF NOT EXISTS idx_ops_page ON applied_ops(page_id, applied_at DESC);

-- Provenance: which source, claim and anchor produced the text in a block.
CREATE TABLE IF NOT EXISTS provenance (
    id         BIGSERIAL PRIMARY KEY,
    block_id   TEXT NOT NULL,
    page_id    TEXT,
    source_id  TEXT NOT NULL,
    claim_id   TEXT,
    relation   TEXT,
    anchor     JSONB,
    op_id      TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prov_block ON provenance(block_id);
CREATE INDEX IF NOT EXISTS idx_prov_source ON provenance(source_id);

-- Generic result records: sweeps, digests, run logs.
CREATE TABLE IF NOT EXISTS records (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,
    label      TEXT,
    payload    JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_kind ON records(kind, created_at DESC);
"""

_INIT_SQLITE = """
CREATE TABLE IF NOT EXISTS pages (
    page_id       TEXT PRIMARY KEY,
    parent_id     TEXT,
    parent_kind   TEXT,
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT,
    icon          TEXT,
    role          TEXT,
    summary       TEXT,
    topics        TEXT NOT NULL DEFAULT '[]',
    archived      INTEGER NOT NULL DEFAULT 0,
    created_time  TEXT,
    last_edited   TEXT,
    synced_at     REAL NOT NULL,
    content_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_parent ON pages(parent_id);
CREATE INDEX IF NOT EXISTS idx_pages_edited ON pages(last_edited DESC);

CREATE TABLE IF NOT EXISTS blocks (
    block_id     TEXT PRIMARY KEY,
    page_id      TEXT NOT NULL,
    parent_id    TEXT,
    type         TEXT NOT NULL,
    text         TEXT NOT NULL DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0,
    depth        INTEGER NOT NULL DEFAULT 0,
    has_children INTEGER NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    raw          TEXT NOT NULL DEFAULT '{}',
    last_edited  TEXT,
    synced_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_page ON blocks(page_id, position);
CREATE INDEX IF NOT EXISTS idx_blocks_parent ON blocks(parent_id);

CREATE TABLE IF NOT EXISTS links (
    from_page TEXT NOT NULL,
    to_page   TEXT NOT NULL,
    block_id  TEXT,
    PRIMARY KEY (from_page, to_page, block_id)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_page);

CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT,
    author       TEXT,
    published_at TEXT,
    content_hash TEXT NOT NULL,
    archive_key  TEXT,
    text         TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{}',
    fetched_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(content_hash);
CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(url);

CREATE TABLE IF NOT EXISTS claims (
    claim_id   TEXT PRIMARY KEY,
    source_id  TEXT NOT NULL,
    text       TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'fact',
    topics     TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 1.0,
    anchor     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_id);

CREATE TABLE IF NOT EXISTS judgements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        TEXT NOT NULL,
    relation        TEXT NOT NULL,
    confidence      REAL NOT NULL,
    target_page_id  TEXT,
    target_block_id TEXT,
    rationale       TEXT,
    existing_text   TEXT,
    model           TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgements_claim ON judgements(claim_id);

CREATE TABLE IF NOT EXISTS patches (
    patch_id    TEXT PRIMARY KEY,
    source_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed',
    payload     TEXT NOT NULL,
    n_ops       INTEGER NOT NULL DEFAULT 0,
    reviewer    TEXT,
    notes       TEXT,
    created_at  REAL NOT NULL,
    reviewed_at REAL,
    applied_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_patches_status ON patches(status, created_at DESC);

CREATE TABLE IF NOT EXISTS applied_ops (
    op_id       TEXT PRIMARY KEY,
    patch_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    target      TEXT NOT NULL,
    page_id     TEXT,
    payload     TEXT NOT NULL,
    inverse     TEXT,
    result      TEXT,
    claim_id    TEXT,
    relation    TEXT,
    applied_at  REAL,
    reverted_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ops_patch ON applied_ops(patch_id);
CREATE INDEX IF NOT EXISTS idx_ops_page ON applied_ops(page_id, applied_at DESC);

CREATE TABLE IF NOT EXISTS provenance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id   TEXT NOT NULL,
    page_id    TEXT,
    source_id  TEXT NOT NULL,
    claim_id   TEXT,
    relation   TEXT,
    anchor     TEXT,
    op_id      TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prov_block ON provenance(block_id);
CREATE INDEX IF NOT EXISTS idx_prov_source ON provenance(source_id);

CREATE TABLE IF NOT EXISTS records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    label      TEXT,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_kind ON records(kind, created_at DESC);
"""

_BOOKKEEPING_PG = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    applied_at DOUBLE PRECISION NOT NULL
);
"""

_BOOKKEEPING_SQLITE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
);
"""

# --- 0002: lock the Data API out --------------------------------------------

_RLS_PG = """
-- These tables hold a mirror of your private notes. Supabase exposes `public`
-- through PostgREST, so a table here is potentially readable with the anon key that
-- ships in a browser. palimpsest never uses the Data API — it connects over SQL as
-- the owner, and the service role bypasses RLS.
--
-- So: enable RLS everywhere and define **no policies**. That denies `anon` and
-- `authenticated` completely while leaving the backend untouched.
ALTER TABLE pages       ENABLE ROW LEVEL SECURITY;
ALTER TABLE blocks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE links       ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources     ENABLE ROW LEVEL SECURITY;
ALTER TABLE claims      ENABLE ROW LEVEL SECURITY;
ALTER TABLE judgements  ENABLE ROW LEVEL SECURITY;
ALTER TABLE patches     ENABLE ROW LEVEL SECURITY;
ALTER TABLE applied_ops ENABLE ROW LEVEL SECURITY;
ALTER TABLE provenance  ENABLE ROW LEVEL SECURITY;
ALTER TABLE records     ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
  END IF;
END $$;
"""

_RLS_SQLITE = "-- SQLite has no row-level security and no Data API. Nothing to do."

# --- 0003: the capture queue -------------------------------------------------

#: Capture is asynchronous because ingestion is slow and the things that capture are
#: not. A browser popup closes the instant you click away; a desktop drop of nine PDFs
#: cannot hold a socket open for twenty minutes. So a capture writes a row here and
#: returns immediately, and a worker drains the queue.
#:
#: The queue is *durable* rather than in-memory for one reason: the failure it prevents
#: is losing something you asked it to remember. Kill the machine mid-ingest and the
#: job is still `queued` on the next start. An in-process deque loses it silently,
#: which is the worst possible behaviour for a capture tool — you believe it has the
#: link and it does not.
_JOBS_PG = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'ingest',
    spec        TEXT NOT NULL DEFAULT '',
    source_kind TEXT,
    title       TEXT,
    url         TEXT,
    origin      TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    patch_id    TEXT,
    result      JSONB,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  DOUBLE PRECISION NOT NULL,
    started_at  DOUBLE PRECISION,
    finished_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

_JOBS_SQLITE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'ingest',
    spec        TEXT NOT NULL DEFAULT '',
    source_kind TEXT,
    title       TEXT,
    url         TEXT,
    origin      TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    patch_id    TEXT,
    result      TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

_JOBS_RLS_PG = """
-- Same reasoning as 0002: a job row carries the text you captured.
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON jobs FROM anon, authenticated;
  END IF;
END $$;
"""


# --- 0004: the agent ---------------------------------------------------------

#: The agent's own state: conversations, procedural memory, and the approval queue that
#: sits between a proposed patch and Notion. Kept in the same store as the mirror and
#: the ledger, on purpose — one place to back up, one place to reason about, and the
#: approval row can reference a patch id without a second database in the loop.
_AGENT_PG = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id  TEXT PRIMARY KEY,
    chat_id     TEXT,
    surface     TEXT NOT NULL DEFAULT 'telegram',
    summary     TEXT NOT NULL DEFAULT '',
    token_count INTEGER NOT NULL DEFAULT 0,
    started_at  DOUBLE PRECISION NOT NULL,
    last_active DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON agent_sessions(chat_id, last_active DESC);

CREATE TABLE IF NOT EXISTS agent_messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    JSONB NOT NULL,
    trace_id   TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msgs_session ON agent_messages(session_id, id);

-- Procedural memory: what the agent has learned about how you want it to behave.
-- `key` is unique per kind so a preference is updated, not duplicated.
CREATE TABLE IF NOT EXISTS agent_memory (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'preference',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    source     TEXT,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON agent_memory(kind);

-- The gate. A held patch waits here for a human tap; nothing reaches Notion from the
-- agent without a row of this table resolving to 'approved'.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    patch_id      TEXT NOT NULL,
    session_id    TEXT,
    chat_id       TEXT,
    operation_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    kind          TEXT NOT NULL DEFAULT 'apply',
    status        TEXT NOT NULL DEFAULT 'pending',
    summary       TEXT,
    requested_at  DOUBLE PRECISION NOT NULL,
    resolved_at   DOUBLE PRECISION,
    resolved_by   TEXT,
    expires_at    DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, requested_at);

-- The eval ground truth and its run history.
CREATE TABLE IF NOT EXISTS eval_examples (
    id           TEXT PRIMARY KEY,
    suite        TEXT NOT NULL DEFAULT 'relation',
    input        JSONB NOT NULL,
    expected     JSONB NOT NULL,
    label_source TEXT NOT NULL DEFAULT 'human',
    labelled_by  TEXT,
    created_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_examples_suite ON eval_examples(suite);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id     TEXT PRIMARY KEY,
    suite      TEXT NOT NULL,
    model      TEXT,
    scores     JSONB NOT NULL,
    passed     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_suite ON eval_runs(suite, created_at DESC);
"""

_AGENT_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id  TEXT PRIMARY KEY,
    chat_id     TEXT,
    surface     TEXT NOT NULL DEFAULT 'telegram',
    summary     TEXT NOT NULL DEFAULT '',
    token_count INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    last_active REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON agent_sessions(chat_id, last_active DESC);

CREATE TABLE IF NOT EXISTS agent_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    trace_id   TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msgs_session ON agent_messages(session_id, id);

CREATE TABLE IF NOT EXISTS agent_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'preference',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source     TEXT,
    updated_at REAL NOT NULL,
    UNIQUE (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON agent_memory(kind);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    patch_id      TEXT NOT NULL,
    session_id    TEXT,
    chat_id       TEXT,
    operation_ids TEXT NOT NULL DEFAULT '[]',
    kind          TEXT NOT NULL DEFAULT 'apply',
    status        TEXT NOT NULL DEFAULT 'pending',
    summary       TEXT,
    requested_at  REAL NOT NULL,
    resolved_at   REAL,
    resolved_by   TEXT,
    expires_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, requested_at);

CREATE TABLE IF NOT EXISTS eval_examples (
    id           TEXT PRIMARY KEY,
    suite        TEXT NOT NULL DEFAULT 'relation',
    input        TEXT NOT NULL,
    expected     TEXT NOT NULL,
    label_source TEXT NOT NULL DEFAULT 'human',
    labelled_by  TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_examples_suite ON eval_examples(suite);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id     TEXT PRIMARY KEY,
    suite      TEXT NOT NULL,
    model      TEXT,
    scores     TEXT NOT NULL,
    passed     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_suite ON eval_runs(suite, created_at DESC);
"""

_AGENT_RLS_PG = """
ALTER TABLE agent_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory   ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals      ENABLE ROW LEVEL SECURITY;
ALTER TABLE eval_examples  ENABLE ROW LEVEL SECURITY;
ALTER TABLE eval_runs      ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON agent_sessions, agent_messages, agent_memory, approvals,
                  eval_examples, eval_runs FROM anon, authenticated;
  END IF;
END $$;
"""


MIGRATIONS: list[Migration] = [
    Migration(
        id="0001_init",
        description="mirror (pages/blocks/links), pipeline (sources/claims/judgements), "
                    "ledger (patches/applied_ops/provenance), records",
        postgres=_INIT_PG,
        sqlite=_INIT_SQLITE,
    ),
    Migration(
        id="0002_rls",
        description="deny the Supabase Data API; these tables mirror private notes",
        postgres=_RLS_PG,
        sqlite=_RLS_SQLITE,
    ),
    Migration(
        id="0003_jobs",
        description="durable capture queue, so a closed popup never loses a capture",
        postgres=_JOBS_PG + _JOBS_RLS_PG,
        sqlite=_JOBS_SQLITE,
    ),
    Migration(
        id="0004_agent",
        description="agent sessions, procedural memory, the approval gate, and evals",
        postgres=_AGENT_PG + _AGENT_RLS_PG,
        sqlite=_AGENT_SQLITE,
    ),
]

BOOKKEEPING = {"postgres": _BOOKKEEPING_PG, "sqlite": _BOOKKEEPING_SQLITE}

#: Tables truncated by `palimpsest db reset`, in FK-safe order.
ALL_TABLES = ("provenance", "applied_ops", "patches", "judgements", "claims",
              "sources", "links", "blocks", "pages", "records", "jobs",
              "agent_messages", "agent_sessions", "agent_memory", "approvals",
              "eval_examples", "eval_runs")


def postgres_sql(include_bookkeeping: bool = True) -> str:
    """The whole schema as one script, for pasting into the Supabase SQL editor."""
    parts = [
        "-- palimpsest schema. Generated from palimpsest/store/migrations.py.",
        "-- Paste into the Supabase SQL editor, or run `palimpsest db migrate`.",
        "-- Safe to run more than once.",
        "",
    ]
    if include_bookkeeping:
        parts.append(_BOOKKEEPING_PG)
    for m in MIGRATIONS:
        parts += [f"-- {m.id}: {m.description}", m.postgres]
        if include_bookkeeping:
            parts.append(
                "INSERT INTO schema_migrations (id, applied_at) "
                f"VALUES ('{m.id}', EXTRACT(EPOCH FROM now())) ON CONFLICT (id) DO NOTHING;"
            )
    return "\n".join(parts).strip() + "\n"


def sqlite_sql() -> str:
    return "\n".join([BOOKKEEPING["sqlite"], *[m.sqlite for m in MIGRATIONS]])
