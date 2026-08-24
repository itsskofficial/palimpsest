"""The agent: the gate, the tools, and the loop — all offline.

The live behaviour (grounding, refusals, continuity) was proven against a real model;
these tests pin the parts that must never regress and must run with no key: that the
gate holds writes correctly, that the writing tools are exactly the gated ones, and that
the loop drives a scripted model through a tool call to an answer.

The fake model is scripted — it returns whatever blocks the test queued — so the loop's
control flow is what is under test, not the model's judgement.
"""

from __future__ import annotations

import pytest

from palimpsest.agent import ToolContext, build_registry
from palimpsest.agent.loop import run_turn
from palimpsest.config import Settings
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _Block:
    """Mimics an Anthropic content block closely enough for the loop."""

    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input
        self.id = id


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None


class FakeModel:
    """Returns queued responses in order. Each item is (content, stop_reason)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def message(self, **_kwargs):
        self.calls += 1
        content, stop = self.script.pop(0)
        return _Response(content, stop)


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
    """A safety invariant: the set of tools that can change Notion is exactly the two we
    route through the gate. A new writing tool must be a deliberate change to this list."""
    writers = {t.name for t in build_registry(ctx) if t.writes}
    assert writers == {"apply_patch", "undo_patch"}


def test_fifteen_tools_and_every_schema_is_well_formed(ctx):
    reg = build_registry(ctx)
    assert len(reg) == 15
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
        ([_Block("tool_use", name="search_notes",
                 input={"query": "attention"}, id="tu1")], "tool_use"),
        ([_Block("text", text="Your notes say attention scales by 1/sqrt(d_k).")],
         "end_turn"),
    ])
    reply = run_turn(ctx, "what about attention?", session_id="ses1", chat_id="42")

    assert reply.tool_calls == ["search_notes"]
    assert "1/sqrt(d_k)" in reply.text
    assert reply.steps == 2


def test_the_loop_persists_the_turn_for_continuity(ctx):
    ctx._model = FakeModel([([_Block("text", text="hello")], "end_turn")])
    run_turn(ctx, "hi", session_id="ses1", chat_id="42")

    messages = ctx.store.get_messages("ses1")
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant"]
    assert messages[0]["content"] == "hi"


def test_the_loop_surfaces_an_approval_created_by_a_tool(ctx):
    patch = _patch(_cite())
    ctx.store.put_patch(patch)
    ctx._model = FakeModel([
        ([_Block("tool_use", name="apply_patch",
                 input={"patch_id": patch.patch_id}, id="tu1")], "tool_use"),
        ([_Block("text", text="I've queued that for your approval.")], "end_turn"),
    ])
    reply = run_turn(ctx, f"apply {patch.patch_id}", session_id="ses1", chat_id="42")

    assert len(reply.approvals) == 1
    assert ctx.store.get_approval(reply.approvals[0])["status"] == "pending"


def test_the_loop_recovers_from_a_tool_error(ctx):
    """A tool returning an error must become a result the model can react to, not an
    exception that kills the turn."""
    ctx._model = FakeModel([
        ([_Block("tool_use", name="read_page",
                 input={"page_id": "pg_missing"}, id="tu1")], "tool_use"),
        ([_Block("text", text="That page isn't in your notes.")], "end_turn"),
    ])
    reply = run_turn(ctx, "read pg_missing", session_id="ses1")
    assert reply.steps == 2
    assert "isn't in your notes" in reply.text


def test_the_loop_stops_at_the_step_cap(ctx):
    """A model that only ever calls tools must be stopped, not allowed to loop forever."""
    from palimpsest.agent import loop as loop_mod

    forever = [([_Block("tool_use", name="list_pending", input={}, id=f"t{i}")],
                "tool_use") for i in range(loop_mod.MAX_STEPS + 3)]
    ctx._model = FakeModel(forever)
    reply = run_turn(ctx, "loop", session_id="ses1")
    assert reply.steps == loop_mod.MAX_STEPS
    assert reply.text  # a graceful message, not empty
