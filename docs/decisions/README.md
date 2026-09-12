# Architecture decision records

Decisions already made in this codebase, one per file, with what each one costs.
[DECISIONS.md](../../DECISIONS.md) is the wider narrative; these are the load-bearing
ones, recorded in the form.

1. [The atom is a claim, not a document](0001-the-atom-is-a-claim-not-a-document.md) — why relation classification rather than "embed and rewrite".
2. [Contradictions are never applied automatically](0002-contradictions-are-never-applied-automatically.md) — the safety invariant, with three locks and no setting.
3. [One write door](0003-one-write-door.md) — only `notion/apply.py` writes to Notion, enforced by an import-linter contract.
4. [The offline core has zero dependencies](0004-the-offline-core-has-zero-dependencies.md) — what `dependencies = []` buys, and what it costs to keep true.
5. [Every operation carries its own inverse](0005-every-operation-carries-its-own-inverse.md) — inverse-first application, and exact reversibility.
6. [A durable job queue, not synchronous ingest](0006-a-durable-job-queue-not-synchronous-ingest.md) — why every capture surface posts to `/v1/jobs`.
7. [A manual agent loop, not a framework](0007-a-manual-agent-loop-not-a-framework.md) — why not the Tool Runner, LangChain or LangGraph.
8. [No MCP server](0008-no-mcp-server.md) — **rejected**, and what would change the decision.
9. [BM25 over a vector database](0009-bm25-over-a-vector-database.md) — lexical retrieval first, and the number that would overturn it.
10. [The UI ships as static files inside the Python package](0010-the-ui-ships-as-static-files-inside-the-python-package.md) — why not build at install time.
11. [The workspace is an interface, not Notion](0011-the-workspace-is-an-interface-not-notion.md) — seventeen methods behind one write door, and what a second backend buys.
12. [The confidence bar scales with what the edit would do](0012-confidence-scales-with-what-the-edit-would-do.md) — why one threshold for seven relations was backwards.
13. [The demo runs the real pipeline](0013-the-demo-runs-the-real-pipeline.md) — not a recording, and its promises are tested against a live model.
