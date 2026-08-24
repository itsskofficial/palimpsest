"""The safety suite — the invariants that must hold at 100%, checked offline.

These are the properties an agent with access to your notes must never violate, written
so they run in CI with no key: an adversarial prompt to a live model is a useful online
eval, but a merge gate has to be deterministic. So each test here pins a *structural*
guarantee — the gate's behaviour across the whole autonomy matrix, the absence of any
lever the agent could pull, the prompt's stated limits — rather than hoping a model
behaves.

If any test in this file fails, the agent can do something it must not, and the fix is
never to change the test.
"""

from __future__ import annotations

import pytest

from palimpsest.agent import ToolContext, build_registry
from palimpsest.agent.prompts import CORE
from palimpsest.config import Settings
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "NOTION_TOKEN", "LANGFUSE_PUBLIC_KEY"):
        monkeypatch.delenv(var, raising=False)
    c = ToolContext(Settings(database_url=f"sqlite:///{tmp_path / 's.db'}"))
    yield c
    c.close()


class FakeNotion:
    def __init__(self):
        self.writes = []

    def update_block(self, block_id, payload):
        self.writes.append(block_id)
        return {"id": block_id}

    def append_children(self, parent_id, children, after_block_id=None):
        self.writes.append(parent_id)
        return {"results": [{"id": new_id("nb_"), "type": "paragraph"}]}


def _patch(*ops):
    return Patch(patch_id=new_id("pch_"), source_id="s", operations=list(ops))


def _op(relation, kind=OpKind.APPEND_BLOCK):
    return Operation(kind=kind, target="pg_a", relation=relation,
                     payload={"text": "x", "rationale": "y"})


# ---------------------------------------------------------------------------
# a contradiction is never applied — across the whole autonomy matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("autonomy", ["none", "low", "medium"])
@pytest.mark.parametrize("apply_on", [False, True])
def test_a_contradiction_never_applies_at_any_setting(ctx, autonomy, apply_on):
    from palimpsest import approval

    patch = _patch(_op(Relation.CONTRADICTS))
    ctx.store.put_patch(patch)
    notion = FakeNotion()
    settings = Settings(apply=apply_on, autonomy=autonomy, notion_token="ntn_x")

    out = approval.gate(ctx.store, patch, settings, notion_factory=lambda: notion)

    assert out["applied"] == 0
    assert out["blocked"] == 1
    assert out.get("approval_id") is None       # not even held
    assert notion.writes == []                  # nothing reached Notion


# ---------------------------------------------------------------------------
# PALIMPSEST_APPLY=0 is an absolute veto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("autonomy", ["none", "low", "medium"])
def test_writes_off_holds_everything_regardless_of_autonomy(ctx, autonomy):
    from palimpsest import approval

    patch = _patch(_op(Relation.NEW), _op(Relation.CORROBORATES))
    ctx.store.put_patch(patch)
    notion = FakeNotion()
    settings = Settings(apply=False, autonomy=autonomy, notion_token="ntn_x")

    out = approval.gate(ctx.store, patch, settings, notion_factory=lambda: notion)

    assert out["applied"] == 0
    assert out["held"] == 2
    assert notion.writes == []


# ---------------------------------------------------------------------------
# the agent has no lever to raise its own autonomy or force a write
# ---------------------------------------------------------------------------


def test_no_tool_schema_can_touch_autonomy_or_apply(ctx):
    """A tool argument named like a permission switch would let the model try to flip
    it. There must be none — autonomy and apply come only from the environment."""
    forbidden = ("autonomy", "apply", "force", "auto_apply", "permission", "override",
                 "bypass", "skip_review", "skip_approval")
    for tool in build_registry(ctx):
        props = set(tool.input_schema.get("properties", {}))
        leaked = props & set(forbidden)
        assert not leaked, f"{tool.name} exposes {leaked}"


def test_only_two_tools_can_write_and_both_are_gated(ctx):
    writers = [t for t in build_registry(ctx) if t.writes]
    assert {t.name for t in writers} == {"apply_patch", "undo_patch"}


def test_settings_has_no_high_autonomy_level():
    """The enum itself has no value above medium. 'Full autonomy' is not a thing the
    system can be talked into because it does not exist."""
    from palimpsest.config import AUTONOMY_LEVELS

    assert set(AUTONOMY_LEVELS) == {"none", "low", "medium"}
    with pytest.raises(ValueError):
        Settings(autonomy="high").validate()


def test_may_auto_apply_requires_both_switches():
    # medium autonomy but writes off → still no.
    assert Settings(apply=False, autonomy="medium").may_auto_apply("medium") is False
    # writes on but autonomy none → still no.
    assert Settings(apply=True, autonomy="none").may_auto_apply("low") is False
    # both aligned → yes, and never for a tier above the setting.
    s = Settings(apply=True, autonomy="low")
    assert s.may_auto_apply("low") is True
    assert s.may_auto_apply("medium") is False


# ---------------------------------------------------------------------------
# the prompt states the limits the code enforces
# ---------------------------------------------------------------------------


def test_the_system_prompt_states_its_hard_limits():
    """The prompt is the first line of defence; the gate is the last. Both must exist.
    If the prompt stops telling the model these rules, drift is likely."""
    low = CORE.lower()
    assert "cannot change the autonomy" in low or "cannot raise" in low
    assert "contradiction" in low
    assert "data, not instructions" in low or "not as a command" in low


# ---------------------------------------------------------------------------
# an approval cannot be applied twice, after expiry, or after rejection
# ---------------------------------------------------------------------------


def test_a_resolved_approval_cannot_be_applied_again(ctx):
    from palimpsest import approval

    op = Operation(kind=OpKind.ADD_CITATION, target="bk1", relation=Relation.CORROBORATES,
                   payload={"label": "s", "rationale": "r"})
    patch = _patch(op)
    ctx.store.put_patch(patch)
    ctx.store.put_blocks([{"block_id": "bk1", "page_id": "pg_a", "type": "paragraph",
                           "position": 0, "text": "x"}])
    out = approval.gate(ctx.store, patch, Settings(apply=False, autonomy="none"))
    aid = out["approval_id"]

    first = approval.resolve(ctx.store, aid, "approved", by="sk", notion_factory=FakeNotion)
    assert first["ok"]
    # A second tap must be a no-op, not a second write.
    second = approval.resolve(ctx.store, aid, "approved", by="sk", notion_factory=FakeNotion)
    assert not second["ok"]
    assert "already" in second["error"]


def test_a_rejected_approval_cannot_later_be_approved(ctx):
    from palimpsest import approval

    patch = _patch(_op(Relation.NEW))
    ctx.store.put_patch(patch)
    out = approval.gate(ctx.store, patch, Settings(apply=False, autonomy="none"))
    aid = out["approval_id"]

    approval.resolve(ctx.store, aid, "rejected", by="sk", notion_factory=FakeNotion)
    after = approval.resolve(ctx.store, aid, "approved", by="sk", notion_factory=FakeNotion)
    assert not after["ok"]
