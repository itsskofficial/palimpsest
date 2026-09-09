"""The agent: the gate, the tools, and the loop — all offline.

The live behaviour (grounding, refusals, continuity) was proven against a real model;
these tests pin the parts that must never regress and must run with no key: that the
gate holds writes correctly, that the writing tools are exactly the gated ones, and that
the loop drives a scripted model through a tool call to an answer.

The fake model is scripted — it returns whatever `Reply` the test queued — so the loop's
control flow is what is under test, not the model's judgement. Since the loop was made
provider-invariant the fake no longer imitates any vendor's content blocks, which is the
point: if this fake ever has to grow a vendor shape again, an abstraction has leaked.
"""

from __future__ import annotations

import pytest

from palimpsest.agent import ToolContext, build_registry
from palimpsest.agent.loop import run_turn
from palimpsest.config import Settings
from palimpsest.llm import Reply, ToolCall
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def says(text: str) -> Reply:
    """A turn that just answers."""
    return Reply(text=text, stop="end")


def wants(name: str, arguments: dict | None = None, call_id: str = "tu1") -> Reply:
    """A turn that asks for one tool."""
    return Reply(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments or {})],
                 stop="tools")


class FakeConversation:
    """History as plain turns. A real provider keeps its own shape; nothing here needs to."""

    def __init__(self, history=None):
        self.turns = list(history or [])

    def user(self, text):
        self.turns.append({"role": "user", "content": text})

    def assistant(self, reply):
        self.turns.append({"role": "assistant", "content": reply.text,
                           "calls": [c.name for c in reply.tool_calls]})

    def results(self, results):
        self.turns.append({"role": "tool",
                           "content": [r.payload for r in results],
                           "errors": [r.is_error for r in results]})

    def wire(self):
        return self.turns


class FakeModel:
    """Returns queued `Reply` objects in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.conversations: list[FakeConversation] = []

    def conversation(self, history=None):
        convo = FakeConversation(history)
        self.conversations.append(convo)
        return convo

    def converse(self, **_kwargs):
        self.calls += 1
        return self.script.pop(0)


class FakeNotion:
    def __init__(self):
        self.blocks, self.pages = {}, {}

    def update_block(self, block_id, payload):
        self.blocks.setdefault(block_id, {}).update(payload)
        return {"id": block_id}

    def append_children(self, parent_id, children, after_block_id=None):
        return {"results": [{"id": new_id("nb_"), "type": "paragraph"} for _ in children]}


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'a.db'}")
    c = ToolContext(s)
    c.store.put_pages([{"page_id": "pg_a", "title": "Attention", "role": "deep_dive",
                        "url": "https://notion.so/a"}])
    c.store.put_blocks([{"block_id": "bk1", "page_id": "pg_a", "type": "paragraph",
                         "position": 0, "text": "Attention scales by 1/sqrt(d_k)."}])
    yield c
    c.close()


def _patch(*ops, source_id="src_x"):
    p = Patch(patch_id=new_id("pch_"), source_id=source_id, operations=list(ops))
    return p


def _cite(**kw):
    return Operation(kind=OpKind.ADD_CITATION, target="bk1", relation=Relation.CORROBORATES,
                     payload={"label": "src", "rationale": "already stated"}, **kw)


def _supersede():
    return Operation(kind=OpKind.STRIKE_BLOCK, target="bk1", relation=Relation.SUPERSEDES,
                     payload={"text": "old", "rationale": "newer source"})


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_propose_only_holds_everything_as_one_approval(ctx):
    from palimpsest import approval

    patch = _patch(_cite(), _cite())
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=False, autonomy="none"),
                        chat_id="42")

    assert out["applied"] == 0
    assert out["held"] == 2
    assert out["approval_id"]
    pending = ctx.store.list_approvals("pending")
    assert len(pending) == 1 and pending[0]["chat_id"] == "42"


def test_autonomy_applies_low_risk_and_holds_the_rest(ctx):
    from palimpsest import approval

    patch = _patch(_cite(), _supersede())          # low + medium
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=True, autonomy="low", notion_token="ntn_x"),
                        notion_factory=FakeNotion, journal_factory=None)

    assert out["applied"] == 1                       # the citation
    assert out["held"] == 1                          # the supersede waits
    assert out["approval_id"]


def test_a_contradiction_is_never_applied_or_even_held(ctx):
    from palimpsest import approval

    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_a", relation=Relation.CONTRADICTS,
                   payload={"text": "conflicting"})
    patch = _patch(op)
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=True, autonomy="medium"),
                        notion_factory=FakeNotion)

    assert out["applied"] == 0
    assert out["held"] == 0
    assert out["blocked"] == 1
    assert ctx.store.list_approvals("pending") == []


def test_approving_applies_the_held_operations(ctx):
    from palimpsest import approval

    patch = _patch(_cite())
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=False, autonomy="none"))
    approval_id = out["approval_id"]

    resolved = approval.resolve(ctx.store, approval_id, "approved", by="sk",
                                notion_factory=FakeNotion)
    assert resolved["ok"] and resolved["status"] == "approved"
    assert resolved["applied"] == 1
    assert ctx.store.get_approval(approval_id)["status"] == "approved"


def test_rejecting_writes_nothing(ctx):
    from palimpsest import approval

    patch = _patch(_cite())
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=False, autonomy="none"))

    notion = FakeNotion()
    resolved = approval.resolve(ctx.store, out["approval_id"], "rejected", by="sk",
                                notion_factory=lambda: notion)
    assert resolved["status"] == "rejected"
    assert notion.blocks == {}                       # nothing written

def test_an_expired_approval_will_not_apply(ctx):
    from palimpsest import approval

    patch = _patch(_cite())
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=False, autonomy="none"))
    # force it stale
    ctx.store.put_approval({**ctx.store.get_approval(out["approval_id"]),
                            "expires_at": 1.0})

    resolved = approval.resolve(ctx.store, out["approval_id"], "approved", by="sk",
                                notion_factory=FakeNotion)
    assert not resolved["ok"] and resolved["status"] == "expired"


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_exactly_the_gated_tools_can_write(ctx):
    """A safety invariant: the set of tools that can change Notion is a closed list, and
    every one of them routes through the approval gate.

    The list grew when the agent gained authorship — `rewrite_page` can replace a whole
    page — so the useful assertion is no longer the count. It is that adding a writing
    tool is a deliberate edit here, and that the tool cannot reach Notion except through
    the same door as the smallest citation.
    """
    import inspect

    from palimpsest.agent import registry as reg

    writers = {t.name for t in build_registry(ctx) if t.writes}
    assert writers == {"apply_patch", "undo_patch", "rewrite_page"}

    # `bind` wraps each handler in a closure, so the tool object no longer carries the
    # code. The handlers are module-level and named after their tool, which is what lets
    # this look at what they actually do rather than at a lambda.
    for name in writers:
        source = inspect.getsource(getattr(reg, f"_{name}"))
        assert "approval.gate" in source or "revert_patch" in source, (
            f"{name} writes but does not go through the gate")


def test_every_tool_schema_is_well_formed(ctx):
    reg = build_registry(ctx)
    assert len(reg) == 16
    for t in reg:
        assert t.input_schema["type"] == "object"
        assert "properties" in t.input_schema
        # every non-optional property is required, and required names exist
        for r in t.input_schema["required"]:
            assert r in t.input_schema["properties"]


def test_search_notes_grounds_in_the_mirror(ctx):
    tool = next(t for t in build_registry(ctx) if t.name == "search_notes")
    out = tool.handler(query="attention scaling")
    assert out["count"] >= 1
    assert out["results"][0]["page_id"] == "pg_a"
    assert out["results"][0]["url"] == "https://notion.so/a"


def test_read_page_missing_returns_an_error_not_an_exception(ctx):
    tool = next(t for t in build_registry(ctx) if t.name == "read_page")
    out = tool.handler(page_id="pg_nope")
    assert "error" in out


def test_apply_patch_tool_holds_in_propose_only(ctx):
    patch = _patch(_cite())
    ctx.store.put_patch(patch)
    tool = next(t for t in build_registry(ctx) if t.name == "apply_patch")
    out = tool.handler(patch_id=patch.patch_id)
    assert out["applied"] == 0
    assert out["approval_id"]
    assert "not applied yet" in out["note"].lower()


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def test_the_loop_runs_a_tool_then_answers(ctx, monkeypatch):
    """One tool round-trip, then an end_turn. The loop must execute the tool, feed the
    result back, and return the final text."""
    ctx._model = FakeModel([
        wants("search_notes", {"query": "attention"}),
        says("Your notes say attention scales by 1/sqrt(d_k)."),
    ])
    reply = run_turn(ctx, "what about attention?", session_id="ses1", chat_id="42")

    assert reply.tool_calls == ["search_notes"]
    assert "1/sqrt(d_k)" in reply.text
    assert reply.steps == 2


def test_the_loop_persists_the_turn_for_continuity(ctx):
    ctx._model = FakeModel([says("hello")])
    run_turn(ctx, "hi", session_id="ses1", chat_id="42")

    messages = ctx.store.get_messages("ses1")
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant"]
    assert messages[0]["content"] == "hi"


def test_the_loop_surfaces_an_approval_created_by_a_tool(ctx):
    patch = _patch(_cite())
    ctx.store.put_patch(patch)
    ctx._model = FakeModel([
        wants("apply_patch", {"patch_id": patch.patch_id}),
        says("I've queued that for your approval."),
    ])
    reply = run_turn(ctx, f"apply {patch.patch_id}", session_id="ses1", chat_id="42")

    assert len(reply.approvals) == 1
    assert ctx.store.get_approval(reply.approvals[0])["status"] == "pending"


def test_the_loop_recovers_from_a_tool_error(ctx):
    """A tool returning an error must become a result the model can react to, not an
    exception that kills the turn."""
    ctx._model = FakeModel([
        wants("read_page", {"page_id": "pg_missing"}),
        says("That page isn't in your notes."),
    ])
    reply = run_turn(ctx, "read pg_missing", session_id="ses1")
    assert reply.steps == 2
    assert "isn't in your notes" in reply.text


def test_the_loop_stops_at_the_step_cap(ctx):
    """A model that only ever calls tools must be stopped, not allowed to loop forever."""
    from palimpsest.agent import loop as loop_mod

    forever = [wants("list_pending", call_id=f"t{i}")
               for i in range(loop_mod.MAX_STEPS + 3)]
    ctx._model = FakeModel(forever)
    reply = run_turn(ctx, "loop", session_id="ses1")
    assert reply.steps == loop_mod.MAX_STEPS
    assert reply.text  # a graceful message, not empty


def test_the_loop_feeds_tool_results_back_before_asking_again(ctx):
    """The round trip, checked from the conversation's side.

    A loop that runs a tool and then asks the model again *without* handing back what the
    tool returned looks identical from the outside — same text, same step count — and is
    the bug that makes an agent repeat itself forever. So assert on the history.
    """
    ctx._model = FakeModel([
        wants("search_notes", {"query": "attention"}),
        says("Found it."),
    ])
    run_turn(ctx, "what about attention?", session_id="ses1")

    turns = ctx._model.conversations[0].turns
    roles = [t["role"] for t in turns]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # The tool's own output, not a placeholder.
    assert turns[2]["content"][0]["results"][0]["page_id"] == "pg_a"
    assert turns[2]["errors"] == [False]


def test_a_tool_error_is_marked_as_one_in_the_history(ctx):
    """The model has to be able to tell a failure from an empty result."""
    ctx._model = FakeModel([
        wants("read_page", {"page_id": "pg_missing"}),
        says("Not there."),
    ])
    run_turn(ctx, "read pg_missing", session_id="ses1")

    tool_turn = next(t for t in ctx._model.conversations[0].turns if t["role"] == "tool")
    assert tool_turn["errors"] == [True]
    assert "error" in tool_turn["content"][0]
