# 8. No MCP server

**Status:** Rejected
**Date:** 2026-09-09

## Context

An earlier draft of the agent architecture included an MCP server exposing palimpsest's
tools — search the mirror, read a page, propose a patch, apply one — so Claude Desktop or
Cursor could drive the knowledge base. The mirror image was also considered: consuming
Notion's own MCP server instead of the hand-written client. Both were cut, and the
reasoning is recorded because "we should expose this over MCP" recurs every few months
and looks free each time.

## Decision

Neither half is built. Palimpsest exposes its tool surface to one consumer — its own agent
loop — and reaches Notion through `notion/client.py` and `notion/apply.py`.

**There is no second client to serve.** The only process that would call an MCP server is
the one that already has the Python API in-process. A protocol earns its cost when it
spans a boundary; here there is no boundary, only a serialisation layer between a function
and its single caller.

**It would be a second write path around the approval gate.** ADR 3 puts every write
behind one door and enforces it with an import-linter contract, and that contract cannot
follow a network endpoint across the wire. The gate becomes a convention clients are
expected to respect rather than a property of the code — the exact failure the contract
exists to prevent, reintroduced through the front door.

**Consuming Notion's MCP fails for four reasons that are one reason.** `undo` is real
because the applier captured the exact `rich_text` it is about to overwrite, and a generic
`update_page` returns nothing of the kind. The operation vocabulary *is* the safety model,
and `update_page` is `rewrite_page` wearing a hat. Provenance and journal rows are written
inside the apply loop, per operation. And the client paces itself under Notion's roughly
three requests per second. For reads, where MCP would otherwise shine, Notion is never
queried at all.

## Consequences

### What this buys

The safety invariants stay properties of the code rather than of a protocol's clients. No
transport layer, no auth model for it, no versioned public tool schema, and no support
burden for what a third-party client does with `apply_patch`.

### What this costs

Palimpsest is unreachable from the tools some people already live in. Someone who thinks
in Cursor or Claude Desktop cannot ask their notes a question without opening a different
window, and the Telegram bot is the only answer we have for "not at my terminal". A real
loss of reach, accepted because the users who would benefit are not this project's users.

## Revisit if

The intended audience changes — if palimpsest is meant to be driven from someone else's
client. The precondition is design, not demand: the server would have to sit *behind*
`approval.gate`, exposing only propose-and-hold operations and never a path that applies.
If it can be written such that the import-linter contract still describes reality, the
objection is answered. The tool registry is structured so the wrapper stays cheap.
