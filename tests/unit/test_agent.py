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

import json

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


# ---------------------------------------------------------------------------
# the read-only tools
# ---------------------------------------------------------------------------
#
# The agent's answers are only worth anything if they come from the mirror, so what these
# check is mostly *grounding*: that each tool returns what is actually in the store, and
# that a miss comes back as a result the model can reason about rather than an exception
# that ends the turn. A tool that raises takes the whole conversation down; a tool that
# returns `{"error": ...}` lets the model say "there is no such page" and carry on.


def _call(ctx, name: str, **arguments):
    tool = next(t for t in build_registry(ctx) if t.name == name)
    return tool.handler(**arguments)


def test_get_provenance_answers_which_source_wrote_a_sentence(ctx):
    ctx.store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    ctx.store.put_provenance([{"block_id": "bk_1", "claim_id": "clm_1",
                               "source_id": "src_1", "relation": "new",
                               "patch_id": "pch_1"}])

    out = _call(ctx, "get_provenance", block_id="bk_1")

    assert out["provenance"][0]["source_id"] == "src_1"
    assert out["note"] is None


def test_a_block_palimpsest_did_not_write_says_so_rather_than_looking_broken(ctx):
    """Most blocks in a real workspace were typed by a person. "No provenance" is the
    normal answer, and without the note the model reports it as a failure."""
    out = _call(ctx, "get_provenance", block_id="bk_typed_by_hand")

    assert out["provenance"] == []
    assert "not written by palimpsest" in out["note"]


def test_get_patch_summarises_without_dumping_the_inverses(ctx):
    """The inverses are large, and every token of them is paid for on a call that only
    needs to explain what a patch would do."""
    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                   payload={"text": "a claim", "rationale": "nothing covers this"})
    op.inverse = {"kind": "archive_blocks", "target": "pg_1",
                  "payload": {"block_ids": ["bk_x"] * 50}}
    patch = Patch(patch_id=new_id("pch_"), source_id="s", operations=[op])
    ctx.store.put_patch(patch)

    out = _call(ctx, "get_patch", patch_id=patch.patch_id)

    assert out["by_relation"] == {"new": 1}
    assert out["operations"][0]["why"] == "nothing covers this"
    assert "inverse" not in json.dumps(out), "inverses are noise on this call"


def test_get_patch_for_something_that_is_not_there_is_an_error_not_a_crash(ctx):
    assert "error" in _call(ctx, "get_patch", patch_id="pch_nothing")


def test_list_pending_shows_both_kinds_of_waiting(ctx):
    """An approval and a proposed patch are different states — one is a change held at
    the gate, the other was never gated at all — and the agent has to see both or it
    tells you nothing is waiting while something is."""
    ctx.store.put_approval({"approval_id": "apr_1", "patch_id": "pch_1",
                            "operation_ids": ["op_a"], "status": "pending",
                            "summary": "one change"})
    ctx.store.put_patch(Patch(patch_id="pch_2", source_id="s", operations=[
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                  payload={"text": "x"})]))

    out = _call(ctx, "list_pending")

    assert out["approvals"][0]["approval_id"] == "apr_1"
    assert out["approvals"][0]["operations"] == 1
    assert out["proposed_patches"][0]["patch_id"] == "pch_2"


def test_check_job_reports_a_finished_capture(ctx):
    ctx.store.put_job({"job_id": "job_1", "kind": "ingest", "spec": "x",
                       "status": "done",
                       "result": {"claims": 4, "patch": {"patch_id": "pch_9"},
                                  "auto_applied": {"applied": 3,
                                                   "approval_id": "apr_2"}}})

    out = _call(ctx, "check_job", job_id="job_1")

    assert out["status"] == "done"
    assert out["claims"] == 4
    assert out["patch_id"] == "pch_9"
    assert out["applied"] == 3
    assert out["approval_id"] == "apr_2"


def test_check_job_on_a_job_that_never_existed_is_an_error(ctx):
    assert "error" in _call(ctx, "check_job", job_id="job_nothing")


def test_capture_queues_rather_than_ingesting_inline(ctx):
    """Ingestion takes minutes and a conversational turn does not. Doing it inline would
    hold the turn open past every timeout between here and the user."""
    out = _call(ctx, "capture_source", spec="https://example.com/a")

    assert out["status"] == "queued"
    assert ctx.store.get_job(out["job_id"]) is not None
    assert "background" in out["note"]


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_remembering_a_fact_makes_it_recallable(ctx):
    _call(ctx, "remember", fact="prefers terse answers", key="tone")

    assert _call(ctx, "recall")["memories"][0]["value"] == "prefers terse answers"


def test_recall_filters_on_the_key_as_well_as_the_value(ctx):
    """The agent searches its own memory by whatever it half-remembers, which is as
    often the label as the content."""
    _call(ctx, "remember", fact="prefers terse answers", key="tone")
    _call(ctx, "remember", fact="works on transformers", key="topic")

    assert len(_call(ctx, "recall", query="tone")["memories"]) == 1
    assert len(_call(ctx, "recall", query="transformers")["memories"]) == 1
    assert len(_call(ctx, "recall", query="nothing at all")["memories"]) == 0


def test_remembering_the_same_key_twice_replaces_rather_than_accumulates(ctx):
    """Otherwise a preference that changed leaves both versions in memory and the agent
    acts on whichever it reads first."""
    _call(ctx, "remember", fact="terse", key="tone")
    _call(ctx, "remember", fact="detailed", key="tone")

    memories = _call(ctx, "recall", query="tone")["memories"]
    assert len(memories) == 1
    assert memories[0]["value"] == "detailed"


# ---------------------------------------------------------------------------
# the tools that need a workspace
# ---------------------------------------------------------------------------


def test_sync_without_a_workspace_is_an_error_rather_than_a_traceback(ctx):
    """Unconfigured is a normal state — the agent is usable before Notion is connected —
    so asking it to sync has to come back as something the model can explain."""
    out = _call(ctx, "sync_mirror")

    assert "error" in out


def test_a_sweep_runs_against_the_mirror_with_no_model(ctx):
    """The day-one property, reached through the agent: the duplicate sweep needs no key
    and no network, so it works on a machine that has only just been set up."""
    text = "Attention divides the logits by the square root of the key dimension."
    ctx.store.put_pages([{"page_id": p, "title": p, "last_edited": "x"}
                         for p in ("pg_a", "pg_b")])
    ctx.store.put_blocks([
        {"block_id": "bk_a", "page_id": "pg_a", "type": "paragraph", "text": text,
         "position": 0},
        {"block_id": "bk_b", "page_id": "pg_b", "type": "paragraph", "text": text,
         "position": 0}])
    ctx.refresh_index()

    out = _call(ctx, "run_sweep", kind="duplicates")

    assert out.get("findings") or out.get("count"), out


def test_an_unknown_sweep_names_the_ones_that_exist(ctx):
    out = _call(ctx, "run_sweep", kind="vibes")

    assert "error" in out
    assert "duplicates" in json.dumps(out)


def test_rejecting_a_patch_records_the_reason(ctx):
    """"Why did you not apply this" is a question the ledger has to answer, and the
    answer is worthless without the reason."""
    patch = Patch(patch_id=new_id("pch_"), source_id="s", operations=[
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                  payload={"text": "x"})])
    ctx.store.put_patch(patch)

    out = _call(ctx, "reject_patch", patch_id=patch.patch_id,
                reason="already covered on the optimisers page")

    assert out.get("status") == "rejected" or out.get("rejected")
    stored = ctx.store.list_patches(status="rejected")
    assert [p["patch_id"] for p in stored] == [patch.patch_id]


def test_undo_on_a_patch_that_was_never_applied_says_so(ctx):
    assert "error" in _call(ctx, "undo_patch", patch_id="pch_nothing")
