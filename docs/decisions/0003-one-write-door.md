# 3. One write door

**Status:** Accepted
**Date:** 2026-09-09

## Context

The project makes four promises about every change to your notes: it is reversible, it is
provenanced, it never hard-deletes, and it stops at the first failure. Each is a property
of the code that performs the write.

The failure this prevents is not a malicious write path but a reasonable one — a
convenience helper in the organiser, a quick page creation in a job handler, a tool the
agent was given because it looked harmless. Such code keeps none of the promises and
nothing about it looks wrong at review time. You find out when `palimpsest undo` reports
nothing to revert.

## Decision

`notion/apply.py` is the only module that writes to Notion, and its docstring lists the
four rules in order of how badly it goes when they break. This is enforced rather than
intended: contract 2 in `pyproject.toml`, "Only notion.apply may perform Notion writes",
forbids `types`, `retrieve`, `relate`, `plan`, `extract`, `sweep` and `organise` from
importing `notion.apply`, `notion.journal` or `approval`.

Three names, one door. `journal` writes to Notion too. `approval` is the gate that lets
writes through, so a planner able to import it could route around the gate — a module
that *proposes* edits must not reach the thing that *permits* them. `lint-imports` runs
in both the `test` and `safety` CI jobs.

## Consequences

### What this buys

Each promise has one place to be true and one place to test. `apply_patch` builds every
inverse before running the operation, records each applied operation to the ledger as it
happens rather than batching at the end, and breaks on the first failure with status
`partial`. Because there is no second path, `palimpsest undo` reverts everything that
ever reached Notion. It constrains the agent for free: there is no `write_to_notion` tool
because there is nothing for one to call except through `approval.gate`.

### What this costs

The contract shapes the module graph in ways that are not otherwise natural. `organise`
plans structural change and would find performing it convenient; it may not, and emits a
patch like every other planner. `store/stats.py` exists solely so the SQLite store does
not acquire a static import path to psycopg2 through `store/base.py` — a module split to
keep a contract true rather than exempted.

A contract is also only as good as its source list. A new module that writes to Notion
and is not named in `source_modules` passes CI, and that list is extended by hand.

## Revisit if

A second genuinely distinct write surface appears — a Notion-side webhook consumer, say,
which cannot route through `apply_patch`. The answer would be to make it a caller of the
applier rather than to relax the contract; if that proves impossible, the four promises
need restating before the contract changes.
