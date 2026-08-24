"""Structure: transcripts that keep their timestamps, filing that can be undone.

Two features share this file because they share one property — both are new ways for
something to enter or move around the workspace, and both are only safe if the thing
they produce is exactly reversible or exactly citable.

The applier tests run against a fake Notion that really moves, renames and re-icons
pages rather than recording that it was asked to. If an inverse is wrong, `undo` fails
here the same way it would against a real workspace.
"""

from __future__ import annotations

import pytest

from palimpsest.ingest import resolve
from palimpsest.ingest.transcript import parse_cues
from palimpsest.notion.apply import apply_patch, revert_patch
from palimpsest.organise import organise
from palimpsest.types import Operation, OpKind, Patch, new_id

# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------

UDEMY = """Transcript
0:00
Welcome to the course.
0:15
Attention was introduced in 2017.
1:12:34
That concludes positional encoding."""

COURSERA = ("[0:05] Gradient descent converges linearly for strongly convex functions.\n"
            "[0:31] The learning rate must be below two over the Lipschitz constant.\n"
            "[1:15] We now turn to stochastic variants.")

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
Bayes rule follows from conditional probability.

2
00:01:20.500 --> 00:01:24.000
The prior encodes what you believed beforehand."""


@pytest.mark.parametrize(("body", "expected"), [
    (UDEMY, [0.0, 15.0, 4354.0]),
    (COURSERA, [5.0, 31.0, 75.0]),
    (VTT, [1.0, 80.5]),
])
def test_every_transcript_shape_recovers_its_timestamps(body, expected):
    assert [c["start"] for c in parse_cues(body)] == expected


def test_an_hour_stamp_is_not_read_as_minutes():
    """`1:12:34` is 72 minutes, not 1 minute 12. Reading it the other way puts every
    anchor after the one-hour mark in the wrong place, silently."""
    cues = parse_cues(UDEMY)
    assert cues[-1]["start"] == 1 * 3600 + 12 * 60 + 34


def test_a_pasted_transcript_anchors_to_time_not_offsets():
    source = resolve("transcript:" + COURSERA, title="Convex Optimisation",
                     url="https://coursera.org/lecture/opt/3")
    segments = source.meta["segments"]
    assert source.kind == "transcript"
    assert source.meta["timestamped"] is True
    assert all(s["kind"] == "timestamp" for s in segments)
    # The locator is what a citation shows; an offset here would be the bug.
    assert segments[0]["locator"] == "0:05"
    assert segments[0]["url"] == "https://coursera.org/lecture/opt/3"


def test_each_claim_anchors_to_its_own_cue_not_to_the_passage(store):
    """The text is merged into passages so a claim has room to sit in; the anchors are
    not, so a claim cites the moment it was actually said.

    Merging both is the tempting shortcut and it is wrong in a way that is invisible
    until you click a footnote: every claim in a 900-character span would cite whatever
    was being said at the start of it, which on a dense lecture is a minute out.
    """
    from palimpsest.ingest import anchor_for

    body = ("0:15\nThe attention mechanism was introduced in 2017.\n"
            "1:02\nScaled dot-product attention divides by the square root of d_k.\n"
            "2:40\nMulti-head attention uses eight heads in the original paper.")
    source = resolve("transcript:" + body, url="https://udemy.com/x/lecture/9")

    # One segment per cue, not one per passage — the whole thing is under 900 chars and
    # would otherwise collapse into a single anchor.
    assert len(source.meta["segments"]) == 3

    located = {}
    for probe in ("attention mechanism", "square root", "eight heads"):
        i = source.text.index(probe)
        located[probe] = anchor_for(source, i, i + len(probe)).locator

    assert located == {"attention mechanism": "0:15", "square root": "1:02",
                       "eight heads": "2:40"}


def test_a_claim_at_the_very_end_of_a_passage_still_anchors(store):
    """The trailing paragraph break belongs to the last cue. Without that, a claim
    running to the end of a passage falls through to the raw-offset fallback."""
    from palimpsest.ingest import anchor_for

    source = resolve("transcript:0:05\nFirst thing said.\n0:30\nThe very last words here.")
    end = len(source.text)
    assert anchor_for(source, end - 5, end).kind == "timestamp"


def test_a_transcript_without_timestamps_degrades_honestly():
    """Prose with no timestamps still ingests, but must not claim to be timestamped —
    a fabricated `12:00` is worse than an honest paragraph anchor."""
    source = resolve("transcript:Just some prose.\n\nA second paragraph here.")
    assert source.meta["timestamped"] is False
    assert all(s["kind"] != "timestamp" for s in source.meta["segments"])


def test_transcript_is_detected_from_the_prefix_and_from_file_suffixes():
    from palimpsest.ingest import detect_kind

    assert detect_kind("transcript:0:01 hello") == "transcript"
    assert detect_kind("lecture.vtt") == "transcript"
    assert detect_kind("lecture.srt") == "transcript"


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["memo.mp3", "call.m4a", "lecture.wav", "talk.opus",
                                  "meeting.mp4", "standup.mkv"])
def test_recordings_are_recognised_as_audio_not_as_text(name):
    """Before the adapter existed these fell through to the text reader, which happily
    read the bytes as prose. Nothing errored; the claims were just gibberish."""
    from palimpsest.ingest import detect_kind

    assert detect_kind(name) == "audio"


def test_audio_with_no_key_refuses_instead_of_inventing_a_transcript(tmp_path, monkeypatch):
    """The one adapter that cannot degrade. Every fallback available — the filename, a
    description, an empty string — would put a claim nobody made into the notes."""
    from palimpsest.ingest.audio import transcribe

    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "SARVAM_API_KEY", "PALIMPSEST_TRANSCRIBE"):
        monkeypatch.delenv(key, raising=False)
    recording = tmp_path / "memo.mp3"
    recording.write_bytes(b"\x00\x01")

    with pytest.raises(RuntimeError, match="needs a speech-to-text key"):
        transcribe(recording)


def test_the_provider_is_chosen_by_which_key_is_set(monkeypatch):
    from palimpsest.config import Settings

    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "SARVAM_API_KEY", "PALIMPSEST_TRANSCRIBE"):
        monkeypatch.delenv(key, raising=False)

    assert Settings().transcriber is None
    assert Settings(groq_api_key="g").transcriber == "groq"
    # Deepgram wins when both are present: no size ceiling and it labels speakers.
    assert Settings(groq_api_key="g", deepgram_api_key="d").transcriber == "deepgram"
    # An explicit choice overrides the order...
    assert Settings(groq_api_key="g", deepgram_api_key="d",
                    transcribe_provider="groq").transcriber == "groq"
    # ...but naming a provider whose key is missing is a problem, not a silent fallback.
    settings = Settings(groq_api_key="g", transcribe_provider="sarvam")
    assert settings.transcriber is None
    assert any("its key is not set" in p for p in settings.problems())


def test_an_unknown_provider_is_rejected_by_name(tmp_path, monkeypatch):
    from palimpsest.ingest.audio import transcribe

    monkeypatch.setenv("GROQ_API_KEY", "g")
    recording = tmp_path / "memo.mp3"
    recording.write_bytes(b"\x00")

    with pytest.raises(ValueError, match="unknown transcription provider"):
        transcribe(recording, provider="whisper.cpp")


def test_a_recording_anchors_each_cue_to_its_own_moment(tmp_path, monkeypatch):
    """The same contract as YouTube and pasted transcripts, through the same merge."""
    from palimpsest.ingest import anchor_for
    from palimpsest.ingest import audio as audio_mod

    recording = tmp_path / "lecture.wav"
    recording.write_bytes(b"\x00")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(audio_mod, "_groq", lambda *a, **k: [
        {"start": 0.0, "text": "Attention divides by the square root of d_k."},
        {"start": 62.0, "text": "Multi-head attention uses eight heads."},
        {"start": 4354.0, "text": "That concludes positional encoding."},
    ])

    source = audio_mod.from_audio(str(recording), title="Lecture 4")
    assert source.kind == "audio"
    assert source.meta["transcriber"] == "groq"
    assert source.meta["timestamped"] is True

    located = {}
    for probe in ("square root", "eight heads", "positional encoding"):
        i = source.text.index(probe)
        located[probe] = anchor_for(source, i, i + len(probe)).locator

    assert located == {"square root": "0:00", "eight heads": "1:02",
                       "positional encoding": "1:12:34"}


def test_groq_refuses_an_oversized_file_and_names_the_alternative(tmp_path, monkeypatch):
    """A 90-minute lecture is over Groq's cap. The error has to say what to do, not
    just what went wrong."""
    from palimpsest.ingest.audio import GROQ_MAX_BYTES, _groq

    with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY"):
        _groq(b"\x00" * (GROQ_MAX_BYTES + 1), "audio/mpeg", "k",
              filename="lecture.mp3", language=None, timeout=1.0)


# ---------------------------------------------------------------------------
# a Notion that really moves things
# ---------------------------------------------------------------------------


class FakeNotion:
    """Implements the structural half of the client, with real state."""

    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}

    def create_page(self, parent_page_id, title, children=None, icon=None):
        pid = new_id("np_")
        self.pages[pid] = {"id": pid, "title": title, "parent": parent_page_id,
                           "icon": icon}
        return {"id": pid, "url": f"https://notion.so/{pid}"}

    def move_page(self, page_id, parent_page_id):
        self.pages.setdefault(page_id, {"id": page_id})["parent"] = parent_page_id
        return self.pages[page_id]

    def rename_page(self, page_id, title):
        self.pages.setdefault(page_id, {"id": page_id})["title"] = title
        return self.pages[page_id]

    def set_page_icon(self, page_id, icon):
        self.pages.setdefault(page_id, {"id": page_id})["icon"] = icon
        return self.pages[page_id]

    def archive_page(self, page_id):
        self.pages.setdefault(page_id, {"id": page_id})["archived"] = True
        return self.pages[page_id]

    def append_children(self, parent_id, children, after_block_id=None):
        created = []
        for child in children:
            bid = new_id("nb_")
            self.blocks[bid] = {**child, "id": bid}
            created.append({"id": bid, "type": child.get("type", "paragraph")})
        return {"results": created}


@pytest.fixture()
def filed(store):
    """A small workspace with one of each situation the organiser has to distinguish.

    `pg_attn` is already correctly filed, `pg_misplaced` is under the wrong hub, and
    `pg_loose` sits at the workspace root — the case Notion's API cannot undo.
    """
    store.put_pages([
        {"page_id": "pg_root", "title": "Root", "parent_kind": "workspace"},
        {"page_id": "pg_hub", "title": "Machine Learning", "role": "hub",
         "parent_id": "pg_root", "parent_kind": "page_id", "icon": "🧠"},
        {"page_id": "pg_other", "title": "Cooking", "role": "hub",
         "parent_id": "pg_root", "parent_kind": "page_id", "icon": "🍳"},
        {"page_id": "pg_attn", "title": "notes 2", "parent_id": "pg_hub",
         "parent_kind": "page_id"},
        {"page_id": "pg_misplaced", "title": "Backpropagation", "parent_id": "pg_other",
         "parent_kind": "page_id"},
        {"page_id": "pg_loose", "title": "Loose page", "parent_kind": "workspace"},
    ])
    return store


# ---------------------------------------------------------------------------
# structural operations invert exactly
# ---------------------------------------------------------------------------


def test_a_move_reverts_to_the_previous_parent(filed):
    notion = FakeNotion()
    patch = Patch(patch_id=new_id("pch_"), source_id="organise", operations=[
        Operation(kind=OpKind.MOVE_PAGE, target="pg_attn", risk="medium",
                  payload={"parent_page_id": "pg_root"})])

    assert apply_patch(notion, filed, patch).status == "applied"
    assert notion.pages["pg_attn"]["parent"] == "pg_root"

    assert revert_patch(notion, filed, patch).status == "reverted"
    assert notion.pages["pg_attn"]["parent"] == "pg_hub"


def test_a_rename_reverts_to_the_previous_title(filed):
    notion = FakeNotion()
    patch = Patch(patch_id=new_id("pch_"), source_id="organise", operations=[
        Operation(kind=OpKind.RENAME_PAGE, target="pg_attn", risk="medium",
                  payload={"title": "Attention"})])

    apply_patch(notion, filed, patch)
    assert notion.pages["pg_attn"]["title"] == "Attention"

    revert_patch(notion, filed, patch)
    assert notion.pages["pg_attn"]["title"] == "notes 2"


def test_setting_an_icon_on_a_page_that_had_none_reverts_to_none(filed):
    """The inverse of "give it an icon" is "clear it", not "leave whatever is there".
    An absent key here would silently make the operation one-way."""
    notion = FakeNotion()
    patch = Patch(patch_id=new_id("pch_"), source_id="organise", operations=[
        Operation(kind=OpKind.SET_ICON, target="pg_attn", risk="low",
                  payload={"icon": "📌"})])

    apply_patch(notion, filed, patch)
    assert notion.pages["pg_attn"]["icon"] == "📌"

    revert_patch(notion, filed, patch)
    assert notion.pages["pg_attn"]["icon"] is None


def test_a_move_out_of_the_workspace_root_is_refused(filed):
    """Notion's move endpoint has no workspace destination, so a page moved off the
    root cannot be put back. An edit with no inverse must not run at all."""
    notion = FakeNotion()
    patch = Patch(patch_id=new_id("pch_"), source_id="organise", operations=[
        Operation(kind=OpKind.MOVE_PAGE, target="pg_loose", risk="medium",
                  payload={"parent_page_id": "pg_hub"})])

    result = apply_patch(notion, filed, patch)
    assert result.status == "partial"
    assert result.applied == 0
    assert "no inverse" in result.errors[0]
    assert "pg_loose" not in notion.pages  # nothing happened


def test_a_move_of_a_page_missing_from_the_mirror_is_refused(filed):
    notion = FakeNotion()
    patch = Patch(patch_id=new_id("pch_"), source_id="organise", operations=[
        Operation(kind=OpKind.MOVE_PAGE, target="pg_unknown", risk="medium",
                  payload={"parent_page_id": "pg_hub"})])

    result = apply_patch(notion, filed, patch)
    assert result.status == "partial"
    assert "sync" in result.errors[0]


def test_a_move_into_a_hub_created_by_the_same_patch_resolves_its_id(filed):
    """An organise patch creates a hub and files pages into it. The hub's Notion id
    does not exist when the patch is written, so the move carries a reference that the
    applier resolves from the create's response."""
    notion = FakeNotion()
    create = Operation(kind=OpKind.CREATE_PAGE, target="pg_root", risk="low",
                       payload={"title": "Optimisation", "icon": "📉"})
    move = Operation(kind=OpKind.MOVE_PAGE, target="pg_attn", risk="medium",
                     payload={"parent_page_id": {"from_op": create.op_id,
                                                 "key": "page_id"}})
    patch = Patch(patch_id=new_id("pch_"), source_id="organise",
                  operations=[create, move])

    assert apply_patch(notion, filed, patch).status == "applied"
    new_hub = create.result["page_id"]
    assert notion.pages["pg_attn"]["parent"] == new_hub
    assert notion.pages[new_hub]["title"] == "Optimisation"

    # And the whole thing still reverses: the page goes home, the hub is archived.
    assert revert_patch(notion, filed, patch).status == "reverted"
    assert notion.pages["pg_attn"]["parent"] == "pg_hub"


def test_an_unresolvable_reference_fails_rather_than_guessing(filed):
    notion = FakeNotion()
    move = Operation(kind=OpKind.MOVE_PAGE, target="pg_attn", risk="medium",
                     payload={"parent_page_id": {"from_op": "op_nonexistent",
                                                 "key": "page_id"}})
    patch = Patch(patch_id=new_id("pch_"), source_id="organise", operations=[move])

    result = apply_patch(notion, filed, patch)
    assert result.status == "partial"
    assert "has not produced an id" in result.errors[0]


# ---------------------------------------------------------------------------
# the autonomy ladder covers structural operations too
# ---------------------------------------------------------------------------


def test_an_operation_without_a_relation_defaults_to_medium_risk():
    """Defaulting to `low` would let anything unlabelled through automatically. The
    direction to be wrong in is the one that asks a human."""
    assert Operation(kind=OpKind.MOVE_PAGE, target="p").risk_tier == "medium"
    assert Operation(kind=OpKind.SET_ICON, target="p", risk="low").risk_tier == "low"


def test_structural_operations_are_auto_appliable_but_still_gated():
    from palimpsest.config import Settings

    op = Operation(kind=OpKind.MOVE_PAGE, target="p", risk="medium")
    assert op.auto_appliable is True

    off = Settings(apply=False, autonomy="medium")
    assert off.may_auto_apply(op.risk_tier) is False  # APPLY=0 is an absolute veto

    low = Settings(apply=True, autonomy="low")
    assert low.may_auto_apply(op.risk_tier) is False

    medium = Settings(apply=True, autonomy="medium")
    assert medium.may_auto_apply(op.risk_tier) is True


# ---------------------------------------------------------------------------
# the organiser
# ---------------------------------------------------------------------------


class StubModel:
    """Returns a canned taxonomy, so the planner's own logic is what is under test."""

    def __init__(self, proposal):
        self.proposal = proposal
        self.prompts: list[str] = []

    def json(self, *, task, system, prompt, schema, effort="high", cache_prefix=None,
             **kw):
        self.prompts.append(cache_prefix or "")
        return self.proposal


def test_a_low_confidence_placement_goes_to_review_not_to_the_patch(filed):
    model = StubModel({
        "hubs": [{"name": "Machine Learning", "icon": "🧠", "rationale": "exists",
                  "existing_page_id": "pg_hub"}],
        "assignments": [{"page_id": "pg_misplaced", "hub": "Machine Learning",
                         "confidence": 0.42, "rationale": "might fit",
                         "suggested_title": None}],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id="pg_root", min_confidence=0.75)

    assert len(result.patch) == 0
    assert result.review[0]["kind"] == "uncertain_placement"
    assert result.review[0]["confidence"] == 0.42


def test_a_confident_placement_becomes_a_move(filed):
    """The counterpart to the test above: same page, same hub, higher confidence."""
    model = StubModel({
        "hubs": [{"name": "Machine Learning", "icon": "🧠", "rationale": "exists",
                  "existing_page_id": "pg_hub"}],
        "assignments": [{"page_id": "pg_misplaced", "hub": "Machine Learning",
                         "confidence": 0.96, "rationale": "backprop is ML, not cooking",
                         "suggested_title": None}],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id="pg_root", min_confidence=0.75)

    assert result.review == []
    move = next(o for o in result.patch.operations if o.kind is OpKind.MOVE_PAGE)
    assert move.target == "pg_misplaced"
    assert move.payload["parent_page_id"] == "pg_hub"


def test_a_workspace_root_page_becomes_a_review_item_rather_than_a_one_way_move(filed):
    model = StubModel({
        "hubs": [{"name": "Machine Learning", "icon": "🧠", "rationale": "exists",
                  "existing_page_id": "pg_hub"}],
        "assignments": [{"page_id": "pg_loose", "hub": "Machine Learning",
                         "confidence": 0.99, "rationale": "clearly ML",
                         "suggested_title": None}],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id="pg_root")

    assert len(result.patch) == 0
    assert result.review[0]["kind"] == "one_way_move"
    assert "cannot move it back" in result.review[0]["detail"]


def test_a_page_already_in_the_right_hub_produces_no_operation(filed):
    model = StubModel({
        "hubs": [{"name": "Machine Learning", "icon": "🧠", "rationale": "exists",
                  "existing_page_id": "pg_hub"}],
        "assignments": [{"page_id": "pg_attn", "hub": "Machine Learning",
                         "confidence": 0.99, "rationale": "already here",
                         "suggested_title": None}],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id="pg_root")
    assert [op for op in result.patch.operations if op.kind is OpKind.MOVE_PAGE] == []


def test_a_new_hub_is_created_and_moves_reference_it(filed):
    model = StubModel({
        "hubs": [{"name": "Optimisation", "icon": "📉", "rationale": "new subject",
                  "existing_page_id": None}],
        "assignments": [{"page_id": "pg_attn", "hub": "Optimisation",
                         "confidence": 0.95, "rationale": "it is about optimisers",
                         "suggested_title": "Optimisers"}],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id="pg_root")

    kinds = [op.kind for op in result.patch.operations]
    assert OpKind.CREATE_PAGE in kinds
    assert OpKind.MOVE_PAGE in kinds
    assert OpKind.RENAME_PAGE in kinds  # "notes 2" is a genuinely unhelpful title

    create = next(o for o in result.patch.operations if o.kind is OpKind.CREATE_PAGE)
    move = next(o for o in result.patch.operations if o.kind is OpKind.MOVE_PAGE)
    assert move.payload["parent_page_id"] == {"from_op": create.op_id, "key": "page_id"}


def test_without_a_root_page_a_new_hub_is_a_review_item(filed):
    """There is nowhere to create it, and inventing a location is not the planner's
    decision to make."""
    model = StubModel({
        "hubs": [{"name": "Optimisation", "icon": "📉", "rationale": "new",
                  "existing_page_id": None}],
        "assignments": [],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id=None)
    assert result.review[0]["kind"] == "hub_needs_a_home"


def test_the_root_page_is_never_moved(filed):
    model = StubModel({
        "hubs": [{"name": "Machine Learning", "icon": "🧠", "rationale": "exists",
                  "existing_page_id": "pg_hub"}],
        "assignments": [{"page_id": "pg_root", "hub": "Machine Learning",
                         "confidence": 1.0, "rationale": "nonsense",
                         "suggested_title": None}],
        "leave_alone": [],
    })
    result = organise(filed, model, root_page_id="pg_root")
    assert len(result.patch) == 0


def test_organise_never_imports_the_write_path():
    """The import-linter contract says so; this is the runtime version, because a
    planner that can write is one bad merge away from writing."""
    import palimpsest.organise as mod

    assert "notion.apply" not in (mod.__doc__ or "").replace("`notion.apply`", "")
    source = __import__("inspect").getsource(mod)
    assert "notion.apply" not in source.split('"""', 2)[-1]
