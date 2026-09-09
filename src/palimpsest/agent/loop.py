"""The agent loop: a turn in, a reply out, tools in between.

A manual loop rather than the SDK's Tool Runner, and the choice is deliberate. Three
things this loop has to do do not fit a hands-off runner: it wraps every tool call in a
Langfuse span; it lets a tool *pause the whole turn* when an edit is held for approval;
and it mixes a provider's server-side `web_search` with local custom tools. Owning the
`while` gives all three cleanly, and the loop it replaces is about thirty lines.

Nothing here knows which vendor is serving the model. The turn is a `Reply`, the history
is a provider-owned `Conversation`, and web search is *offered*; a provider that does not
run one simply reports that it cannot and the model uses the local tools instead.

The shape is the standard tool loop, with the safety properties living in the tools and
the gate, not here:

    call the model  →  end_turn?  →  done, return the text
                    →  tool_use?  →  run each tool, append results, go again

Session continuity is by replay: prior turns are loaded from the store and the new turn
is appended, so "apply that one" resolves against what was just discussed. Growth is
bounded by a turn cap and, later, by compaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from palimpsest import trace
from palimpsest.agent.context import ToolContext, current_chat
from palimpsest.agent.prompts import build_system
from palimpsest.agent.registry import Tool, build_registry
from palimpsest.llm import ToolResult

log = logging.getLogger("palimpsest.agent.loop")

__all__ = ["AgentReply", "run_turn"]

#: A hard stop on tool round-trips in one turn. A well-behaved turn is two or three; ten
#: means the model is stuck, and looping forever is how an agent burns a credit balance.
MAX_STEPS = 10

#: How many prior messages to replay for continuity. Enough for "apply that one" to
#: resolve; short enough to stay cheap. Compaction can extend this later.
HISTORY_LIMIT = 24


@dataclass
class AgentReply:
    """What a turn produced: the text, plus structured hooks a surface can render."""

    text: str
    approvals: list[str] = field(default_factory=list)   # approval ids created this turn
    tool_calls: list[str] = field(default_factory=list)  # names, in order, for tracing
    steps: int = 0
    trace_id: str | None = None
    error: str | None = None


def _run_tool(tools: dict[str, Tool], name: str, args: dict) -> tuple[Any, bool]:
    """Execute one tool, returning (result, is_error). Never raises out."""
    tool = tools.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}"}, True
    with trace.span(name, kind="tool", input=args) as sp:
        try:
            result = tool.handler(**args)
            sp.update(output=result)
            return result, bool(isinstance(result, dict) and result.get("error"))
        except Exception as e:  # a tool crash becomes a result the model can recover from
            log.exception("tool %s failed", name)
            sp.update(level="ERROR", status_message=str(e)[:300])
            return {"error": f"{type(e).__name__}: {e}"}, True


def run_turn(ctx: ToolContext, user_text: str, *, session_id: str,
             chat_id: str | None = None, on_step=None) -> AgentReply:
    """Run one conversational turn to completion. `on_step(note)` streams progress."""
    registry = build_registry(ctx)
    tools_by_name = {t.name: t for t in registry}
    tool_specs = [t.spec() for t in registry]

    memories = ctx.store.get_memories(kind="preference", limit=30)
    system = build_system(ctx.settings, memories)

    # Replay prior turns for continuity, then add the new user message. The conversation
    # belongs to the provider: how an assistant turn and a set of tool results are
    # represented differs between vendors, and this loop should not know which is in use.
    history = ctx.store.get_messages(session_id, limit=HISTORY_LIMIT)
    conversation = ctx.model.conversation(
        [{"role": m["role"], "content": m["content"]} for m in history
         if m["role"] in ("user", "assistant")])
    conversation.user(user_text)
    ctx.store.add_message(session_id, "user", user_text)

    # Any capture the agent starts this turn should report back to this chat.
    current_chat.set(chat_id)

    reply = AgentReply(text="")

    with trace.span("agent-turn", kind="agent",
                    input={"message": user_text, "session": session_id},
                    metadata={"chat_id": chat_id}) as turn:
        reply.trace_id = trace_id = _current_trace_id()
        reply.trace_id = trace_id

        for step in range(MAX_STEPS):
            reply.steps = step + 1
            try:
                effort = "low" if step == 0 and _looks_trivial(user_text) else "high"
                turn_reply = ctx.model.converse(
                    system=system, conversation=conversation, tools=tool_specs,
                    effort=effort, web_search=True)
            except Exception as e:
                log.exception("model call failed")
                reply.error = f"{type(e).__name__}: {e}"
                reply.text = ("I hit an error reaching the model. Nothing was changed. "
                              "Try again in a moment.")
                turn.update(level="ERROR", status_message=reply.error)
                break

            if turn_reply.stop == "refusal":
                reply.text = "I can't help with that one. Nothing was changed."
                break

            # Give the model's own turn back before the next request, or it loses its
            # reasoning and the ids of the calls it is waiting on.
            conversation.assistant(turn_reply)

            if not turn_reply.tool_calls:
                reply.text = turn_reply.text.strip()
                # A server-side tool ran mid-flight and there is more to come.
                if turn_reply.stop == "pause":
                    continue
                break

            # Execute every requested custom tool; server tools already ran remotely.
            results: list[ToolResult] = []
            for call in turn_reply.tool_calls:
                reply.tool_calls.append(call.name)
                if on_step:
                    on_step(_step_note(call.name, call.arguments))
                result, is_error = _run_tool(tools_by_name, call.name, call.arguments)
                if isinstance(result, dict) and result.get("approval_id"):
                    reply.approvals.append(result["approval_id"])
                results.append(ToolResult(id=call.id, payload=result, is_error=is_error))

            conversation.results(results)
        else:
            # Loop fell through MAX_STEPS without an end_turn.
            reply.text = reply.text or ("I've done several steps but couldn't wrap this "
                                        "up cleanly. Here's where I got to — ask me to "
                                        "continue if you'd like.")

        turn.update(output={"text": reply.text[:2000], "steps": reply.steps,
                            "tools": reply.tool_calls, "approvals": reply.approvals})

    # Persist the assistant's final text for the next turn's continuity.
    if reply.text:
        ctx.store.add_message(session_id, "assistant", reply.text, trace_id=reply.trace_id)
    return reply


def _current_trace_id() -> str | None:
    try:
        from palimpsest import trace as _t

        if _t.enabled():
            return _t._client.get_current_trace_id()  # type: ignore[union-attr]
    except Exception:
        pass
    return None


_TRIVIAL_HINTS = ("thanks", "thank you", "ok", "okay", "yes", "no", "got it", "cool",
                  "hi", "hello", "hey")


def _looks_trivial(text: str) -> bool:
    """A short acknowledgement gets low effort — spending `high` on 'thanks' is waste."""
    t = text.strip().lower()
    return len(t) < 24 and any(t.startswith(h) for h in _TRIVIAL_HINTS)


def _step_note(name: str, args: dict) -> str:
    """A human phrase for a tool call, for the 'thinking…' line in Telegram."""
    phrases = {
        "search_notes": f"searching your notes for “{args.get('query', '')[:40]}”…",
        "read_page": "reading a page…",
        "get_provenance": "checking where that came from…",
        "get_patch": "looking at the proposed change…",
        "list_pending": "checking what's pending…",
        "capture_source": "capturing that…",
        "check_job": "checking on that…",
        "sync_mirror": "syncing from Notion…",
        "run_sweep": f"running the {args.get('kind', '')} sweep…",
        "propose_organisation": "working out a tidier structure…",
        "apply_patch": "applying the change…",
        "undo_patch": "undoing that…",
        "reject_patch": "discarding that proposal…",
        "remember": "noting that…",
        "recall": "recalling what you've told me…",
        "web_search": "searching the web…",
    }
    return phrases.get(name, f"{name}…")
