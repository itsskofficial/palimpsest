# 4. The offline core has zero dependencies

**Status:** Accepted
**Date:** 2026-09-09

## Context

The product claims that before you have given it any key, it already does something worth
having: `sync` pulls your workspace into a local mirror, and `sweep duplicates` lists the
accumulated damage of the last two years, ranked, with a suggested merge for each pair.

That claim decays quietly. Someone imports numpy into the retrieval index because it is
faster, or reaches for `anthropic` in a helper the planner calls, and the package still
installs and the tests still pass. Nobody notices until a user without a key finds that
the thing they were promised does not start.

## Decision

`dependencies = []`. Claude, FastAPI, psycopg2, pypdf, openpyxl, firecrawl and langfuse
are all extras. The mirror, the BM25 index, the planner, the reverse-patch algebra, the
sweeps and the SQLite store are standard library.

Two mechanisms keep it true. Contract 4, "The offline core stays dependency-free",
forbids `types`, `retrieve`, `plan`, `notion/blocks`, `store/sqlite`, `store/stats` and
`store/migrations` from importing `anthropic`, `openai`, `fastapi`, `psycopg2`, `numpy`,
`firecrawl` or `langfuse`. `llm` and `ingest.*` are deliberately absent from that source
list, because wrapping an optional dependency is their entire job.

The `offline` CI job is the runtime half. It installs the package with no extras, puts
two pages carrying the same sentence into an in-memory store, and asserts
`sweep.duplicates` finds exactly one pair. It then checks that no optional dependency
leaked into the default install. If that job ever needs an extra, the claim has stopped
being true and the build says so.

## Consequences

### What this buys

The enforcement mechanism for the day-one story, and for the retrieval decision that
rests on it (ADR 9). It also keeps the test suite offline and fast, keeps the wheel
small, and makes `pip install palimpsest-notion` a few seconds rather than a compile.

### What this costs

Real work is reimplemented. BM25 with bigrams, an IDF-weighted cosine, a token-bucket
rate limiter, a config loader, a durable job queue with a worker pool — several hundred
lines in pure Python, maintained here, battle-tested by nobody else.

Contributors pay attention tax. Adding an import to `plan.py` or `retrieve.py` can fail
CI for reasons unrelated to the change, and the fix is often a module split rather than a
one-line import. And `[all]` is the install most users actually want, so the property is
invisible to them — a guarantee about a configuration many people never run.

## Revisit if

A pure-Python implementation in the core becomes a correctness liability rather than a
maintenance cost, the retrieval index being the likely candidate. Adding a dependency
there means the `offline` job needs it, which means the day-one claim comes out of the
README in the same commit rather than being quietly left standing.
