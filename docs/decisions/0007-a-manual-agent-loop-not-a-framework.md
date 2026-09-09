# 7. A manual agent loop, not a framework

**Status:** Accepted
**Date:** 2026-09-09

## Context

The agent is a conversational surface over an existing pipeline: send it a link and the
base updates, ask it a question and it answers from your notes with citations. Every edit
it proposes goes through `approval.gate`.

The obvious way to build the loop is off the shelf — the SDK's Tool Runner, LangChain, or
LangGraph. Each takes the `while` off our hands. The question is what else it takes with
it.

## Decision

`agent/loop.py` owns the loop. Standard shape: call the model, return on `end_turn`, run
each tool on `tool_use` and go again, with `MAX_STEPS` capped at ten because a
well-behaved turn is two or three and looping forever is how an agent burns a credit
balance. Continuity is by replay — prior turns loaded from the store up to
`HISTORY_LIMIT` — so "apply that one" resolves against what was just discussed.

Three requirements do not fit a hands-off runner cleanly: a Langfuse span per tool call,
correlated by patch, job and chat id; a tool that can pause the whole turn when an edit
is held for approval; and mixing the server-side `web_search` tool, which returns results
in the same response, with local custom tools.

Not LangChain: its provider abstraction hides the Claude-specific features the pipeline
depends on — structured outputs on every call, cache breakpoints on the candidate prefix,
per-task effort. Not LangGraph: its two main draws already exist here, since durable
execution is the `jobs` table (ADR 6) and the graph today is one node. Either would also
add a dependency that breaks the zero-dependency contract (ADR 4).

## Consequences

### What this buys

The loop it replaces is about thirty lines, and those thirty lines are the ones that
needed the control. Every model feature the pipeline relies on stays reachable, tracing is
per-call rather than per-turn, and `[agent]` needs exactly one package — langfuse —
because the loop, tools, gate and memory are standard library over the existing model
layer.

### What this costs

Everything a framework would have supplied is ours to write and keep correct:
message-history management, tool-result serialisation, per-call error handling, the step
cap, replay-based continuity. Compaction does not exist yet — history is bounded by a
fixed turn limit, which is the crude version of something a framework would have given.

The loop is Claude-shaped by construction. Swapping providers is not a config change
here; it is a rewrite of the tool-block handling.

## Revisit if

This goes multi-agent or grows real branching — a researcher sub-agent for the homework
loop, a graph with more than one node. At that point LangGraph's durable execution and
state machine start earning the dependency, and the tool registry is structured so the
tools themselves survive the change.
