"""The routes the desktop app is built on — the surface the user actually sees.

Two of these carry real weight.

**Settings writes credentials.** It is the one route here that can hand someone's Notion
token to a network, so it refuses unless the server is bound to loopback, and it accepts
only an allowlist of keys — an open-ended write from a browser is a way to set
`PALIMPSEST_APPLY=1` on somebody's behalf.

**Approvals and undo are the gate.** They are the same `approval.resolve` and
`revert_patch` the bot taps, so a difference in behaviour between the two surfaces is a
difference in what "approved" means, which is the one thing the product cannot be vague
about.

The rest is about what the UI is *told*. A route that returns 200 with a body meaning
"nothing happened" is worse than an error here, because the interface renders success.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from palimpsest.config import Settings
from palimpsest.serve.app import AppState, create_app
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

pytestmark = pytest.mark.serve


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """Point the config layer at a scratch file, never the developer's real one."""
    path = tmp_path / "config.env"
    monkeypatch.setenv("PALIMPSEST_CONFIG", str(path))
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch, config_file):
    for name in ("ANTHROPIC_API_KEY", "NOTION_TOKEN", "TELEGRAM_BOT_TOKEN",
                 "PALIMPSEST_NOTION_ROOTS", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'ui.db'}",
                        artifact_url=f"file://{tmp_path / 'archive'}")
    state = AppState(settings=settings)
    state.store.put_pages([{"page_id": "pg_1", "title": "Gradient clipping",
                            "role": "reference", "last_edited": "2026-01-01",
                            "url": "https://notion.so/pg_1"}])
    with TestClient(create_app(state)) as c:
        c.state = state
        yield c


def _patch(store, *, applied=False, review=None, relation=Relation.REFINES) -> Patch:
    op = Operation(kind=OpKind.UPDATE_TEXT, target="pg_1",
                   payload={"text": "the new wording", "was": "the old wording",
                            "rationale": "it says it more precisely", "confidence": 0.9},
                   relation=relation,
                   inverse={"kind": "update_text", "target": "pg_1",
                            "payload": {"text": "the old wording"}})
    if applied:
        op.applied_at = 1.0
    patch = Patch(patch_id=new_id("pch_"), source_id=new_id("src_"), operations=[op],
                  review=review or [])
    store.put_patch(patch)
    return patch


# ---------------------------------------------------------------------------
# setup: what the wizard asks
# ---------------------------------------------------------------------------


def test_setup_state_says_which_steps_are_done(client):
    body = client.get("/v1/setup/state").json()

    assert body["configured"] is False
    assert body["steps"] == {"model": False, "notion": False, "root": False,
                             "telegram_token": False, "telegram_paired": False}
    assert isinstance(body["problems"], list)


def test_validate_refuses_an_empty_token_without_calling_anything(client):
    body = client.post("/v1/setup/validate", json={"provider": "notion"}).json()

    assert body["ok"] is False
    assert "no token" in body["error"]


def test_validate_names_a_provider_it_does_not_know(client):
    body = client.post("/v1/setup/validate",
                       json={"provider": "hotmail", "token": "x"}).json()

    assert body["ok"] is False
    assert "hotmail" in body["error"]


def test_an_anthropic_key_is_checked_by_shape_not_by_spending_a_token(client):
    """Validating live on every keystroke would bill somebody to type."""
    good = client.post("/v1/setup/validate",
                       json={"provider": "anthropic", "token": "sk-ant-abc"}).json()
    bad = client.post("/v1/setup/validate",
                      json={"provider": "anthropic", "token": "hunter2"}).json()

    assert good["ok"] is True
    assert bad["ok"] is False
    assert "sk-ant-" in bad["detail"], "and it says what the right shape is"


def test_a_telegram_token_is_validated_against_telegram(client, monkeypatch):
    import palimpsest.telegram as tg

    monkeypatch.setattr(tg, "_call", lambda *a, **k: {"username": "a_bot"})

    body = client.post("/v1/setup/validate",
                       json={"provider": "telegram", "token": "123:abc"}).json()

    assert body["ok"] is True
    assert body["username"] == "a_bot"


def test_a_provider_that_refuses_the_token_reports_why_rather_than_500ing(client,
                                                                         monkeypatch):
    """The wizard shows this string next to the field. A stack trace is not that."""
    import palimpsest.telegram as tg

    def refuse(*a, **k):
        raise tg.TelegramError("getMe: Unauthorized")

    monkeypatch.setattr(tg, "_call", refuse)

    response = client.post("/v1/setup/validate",
                           json={"provider": "telegram", "token": "bad"})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "Unauthorized" in response.json()["error"]


def test_a_notion_token_that_can_see_nothing_is_flagged_rather_than_passed(client,
                                                                          monkeypatch):
    """Authenticating and being granted access are different things in Notion, and
    checking only the first is what leaves people staring at an empty page list at
    exactly the step the instructions call the one everyone misses."""
    import palimpsest.notion.client as nc
    import palimpsest.onboard as onboard

    monkeypatch.setattr(nc.NotionClient, "whoami", lambda self: {"name": "palimpsest"})
    monkeypatch.setattr(onboard, "_shared_pages", lambda client, limit=1: [])

    body = client.post("/v1/setup/validate",
                       json={"provider": "notion", "token": "ntn_x"}).json()

    assert body["ok"] is True, "the token itself is fine, and saying otherwise misleads"
    assert body["shared_pages"] == 0
    assert "Connections" in body["warning"], "it has to say where to click"


def test_a_notion_token_that_can_see_pages_carries_no_warning(client, monkeypatch):
    import palimpsest.notion.client as nc
    import palimpsest.onboard as onboard

    monkeypatch.setattr(nc.NotionClient, "whoami", lambda self: {"name": "palimpsest"})
    monkeypatch.setattr(onboard, "_shared_pages",
                        lambda client, limit=1: [("pg_a", "A page")])

    body = client.post("/v1/setup/validate",
                       json={"provider": "notion", "token": "ntn_x"}).json()

    assert body["warning"] is None
    assert body["shared_pages"] == 1


def test_listing_notion_pages_without_a_token_is_a_400_not_a_crash(client):
    assert client.get("/v1/setup/notion/pages").status_code == 400


def test_creating_a_root_without_a_token_is_a_400(client):
    assert client.post("/v1/setup/notion/root", json={}).status_code == 400


def test_pairing_without_a_token_is_a_400(client):
    assert client.post("/v1/setup/telegram/pair", json={}).status_code == 400


# ---------------------------------------------------------------------------
# settings: the one route that writes credentials
# ---------------------------------------------------------------------------


def test_settings_are_returned_redacted(client, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_verysecretvalue123")

    body = client.get("/v1/settings").json()

    assert "verysecret" not in json.dumps(body)
    assert body["present"]["NOTION_TOKEN"] is True


def test_the_settings_that_are_not_secrets_come_back_in_full(client, monkeypatch):
    """The UI renders the current posture from these. Redacting `PALIMPSEST_AUTONOMY`
    would leave the write-posture control unable to show what it is set to."""
    monkeypatch.setenv("PALIMPSEST_AUTONOMY", "medium")
    monkeypatch.setenv("PALIMPSEST_APPLY", "1")

    body = client.get("/v1/settings").json()

    assert body["values"]["PALIMPSEST_AUTONOMY"] == "medium"
    assert body["values"]["PALIMPSEST_APPLY"] == "1"


def test_saving_a_setting_persists_it_and_makes_it_live(client, config_file):
    response = client.post("/v1/settings",
                           json={"values": {"PALIMPSEST_AUTONOMY": "medium"}})

    assert response.status_code == 200
    assert response.json()["saved"] == ["PALIMPSEST_AUTONOMY"]
    assert "PALIMPSEST_AUTONOMY=medium" in config_file.read_text(encoding="utf-8")
    assert client.state.settings.autonomy == "medium", "and this process, not just disk"


def test_a_key_the_ui_may_not_write_is_ignored(client, config_file):
    """An open-ended settings write from a browser is a way to point somebody's database
    at your server, or turn their writes on."""
    response = client.post("/v1/settings", json={"values": {
        "PALIMPSEST_DATABASE_URL": "postgres://attacker/db",
        "PALIMPSEST_AUTONOMY": "low"}})

    assert response.json()["saved"] == ["PALIMPSEST_AUTONOMY"]
    assert "attacker" not in config_file.read_text(encoding="utf-8")


def test_clearing_a_credential_actually_clears_it(client, config_file, monkeypatch):
    """Emptying a field is what the Settings screen sends when you want a key gone. An
    empty value used to be filtered out as "nothing", so a token could be added from the
    UI and never removed -- and if it was the only field you had touched, the answer was
    "nothing to save", which reads like a broken button."""
    client.post("/v1/settings", json={"values": {"TELEGRAM_BOT_TOKEN": "123:abc"}})
    assert "TELEGRAM_BOT_TOKEN=123:abc" in config_file.read_text(encoding="utf-8")

    response = client.post("/v1/settings", json={"values": {"TELEGRAM_BOT_TOKEN": ""}})

    assert response.status_code == 200
    assert response.json()["cleared"] == ["TELEGRAM_BOT_TOKEN"]
    assert "TELEGRAM_BOT_TOKEN" not in config_file.read_text(encoding="utf-8")
    assert client.state.settings.telegram_token in (None, "")


def test_clearing_one_key_leaves_the_others_alone(client, config_file):
    client.post("/v1/settings", json={"values": {"TELEGRAM_BOT_TOKEN": "123:abc",
                                                 "PALIMPSEST_AUTONOMY": "low"}})

    client.post("/v1/settings", json={"values": {"TELEGRAM_BOT_TOKEN": ""}})

    body = config_file.read_text(encoding="utf-8")
    assert "PALIMPSEST_AUTONOMY=low" in body
    assert "TELEGRAM_BOT_TOKEN" not in body


def test_an_empty_payload_is_still_refused(client):
    assert client.post("/v1/settings", json={"values": {}}).status_code == 422


def test_settings_cannot_be_written_from_a_network_interface(tmp_path, monkeypatch,
                                                             config_file):
    """Keys arrive in the clear. Over loopback to a desktop app that is fine; over a
    network it is handing somebody's Notion token to whoever is listening."""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'net.db'}",
                        artifact_url=f"file://{tmp_path / 'a'}",
                        host="0.0.0.0", api_key="a" * 32)
    with TestClient(create_app(AppState(settings=settings))) as c:
        response = c.post("/v1/settings", json={"values": {"PALIMPSEST_AUTONOMY": "low"}},
                          headers={"Authorization": f"Bearer {'a' * 32}"})

    assert response.status_code == 403
    assert "local" in response.json()["detail"]


# ---------------------------------------------------------------------------
# approvals — the same gate the bot taps
# ---------------------------------------------------------------------------


def test_an_approval_carries_enough_to_render_a_diff(client):
    """The card shows what would change, on which page, and why. Missing any of those
    turns approving into a coin flip, which is the one thing the gate must not be."""
    patch = _patch(client.state.store)
    approval_id = client.state.store.put_approval({
        "approval_id": new_id("apr_"), "patch_id": patch.patch_id,
        "operation_ids": [patch.operations[0].op_id], "status": "pending",
        "summary": "one rewrite"})

    body = client.get("/v1/approvals").json()

    assert len(body["approvals"]) == 1
    card = body["approvals"][0]
    assert card["approval_id"] == approval_id
    op = card["operations"][0]
    assert op["page"] == "Gradient clipping"
    assert op["page_url"] == "https://notion.so/pg_1"
    assert op["text"] == "the new wording"
    assert op["was"] == "the old wording", "without the old text there is no diff"
    assert op["why"] == "it says it more precisely"
    assert op["relation"] == "refines"
    assert op["risk"]


def test_only_the_operations_the_approval_holds_are_shown(client):
    """A patch can be part-applied and part-held. Showing the applied half on the card
    asks for approval of something that already happened."""
    patch = _patch(client.state.store)
    extra = Operation(kind=OpKind.UPDATE_TEXT, target="pg_1", payload={"text": "other"})
    patch.operations.append(extra)
    client.state.store.put_patch(patch)
    client.state.store.put_approval({
        "approval_id": new_id("apr_"), "patch_id": patch.patch_id,
        "operation_ids": [patch.operations[0].op_id], "status": "pending"})

    card = client.get("/v1/approvals").json()["approvals"][0]

    assert [o["op_id"] for o in card["operations"]] == [patch.operations[0].op_id]


def test_a_decision_that_is_neither_approve_nor_reject_is_refused(client):
    assert client.post("/v1/approvals/apr_x/resolve",
                       json={"decision": "maybe"}).status_code == 422


def test_resolving_something_that_is_not_there_is_an_error_not_a_quiet_success(client):
    """Returning 200 with `ok: false` made the UI render a successful rejection that had
    not happened: the card vanished and the proposal stayed pending."""
    response = client.post("/v1/approvals/apr_missing/resolve",
                           json={"decision": "rejected"})

    assert response.status_code == 409


def test_rejecting_records_the_decision_and_writes_nothing(client):
    patch = _patch(client.state.store)
    approval_id = client.state.store.put_approval({
        "approval_id": new_id("apr_"), "patch_id": patch.patch_id,
        "operation_ids": [patch.operations[0].op_id], "status": "pending"})

    response = client.post(f"/v1/approvals/{approval_id}/resolve",
                           json={"decision": "rejected", "by": "ui"})

    assert response.status_code == 200
    assert client.state.store.get_approval(approval_id)["status"] == "rejected"
    assert client.get("/v1/approvals").json()["approvals"] == []


# ---------------------------------------------------------------------------
# activity — one list, because that is how it is read
# ---------------------------------------------------------------------------


def test_activity_reports_what_became_of_each_patch(client):
    _patch(client.state.store, applied=True)

    row = client.get("/v1/activity").json()["activity"][0]

    assert row["operations"] == 1
    assert row["applied"] == 1
    assert row["relations"] == ["refines"]
    assert row["pages"] == ["Gradient clipping"]


def test_a_patch_that_never_applied_is_not_offered_an_undo_button(client):
    """A button that does nothing is worse than no button: it teaches you that undo is
    unreliable, which is the one promise the autonomy ladder rests on."""
    _patch(client.state.store, applied=False)

    assert client.get("/v1/activity").json()["activity"][0]["undoable"] is False


def test_an_applied_patch_is_undoable(client):
    _patch(client.state.store, applied=True)

    assert client.get("/v1/activity").json()["activity"][0]["undoable"] is True


def test_an_already_reverted_patch_is_not_undoable_again(client):
    """Reversal is recorded on the patch row, not on the in-memory operations. Reading
    the operations reported every undone patch as still undoable -- a button that
    re-applies what you just took back."""
    patch = _patch(client.state.store, applied=True)
    client.state.store.set_patch_status(patch.patch_id, "reverted")

    row = client.get("/v1/activity").json()["activity"][0]

    assert row["reverted"] is True
    assert row["undoable"] is False


def test_a_patch_that_is_entirely_review_still_says_what_is_waiting(client):
    """The usual shape when a source contradicts a page: no operations at all. The feed
    showed "0 changes, waiting" and left the reader no way to find out what was waiting
    or what to do about it."""
    patch = Patch(patch_id=new_id("pch_"), source_id=new_id("src_"), operations=[],
                  review=[{"reason": "contradiction",
                           "judgement": {"relation": "contradicts", "confidence": 0.91,
                                         "rationale": "the page says the opposite"},
                           "claim": {"text": "clipping is no longer standard"},
                           "existing_text": "clipping is standard",
                           "page": "Gradient clipping"}])
    client.state.store.put_patch(patch)

    row = client.get("/v1/activity").json()["activity"][0]

    assert row["operations"] == 0
    assert len(row["review"]) == 1
    item = row["review"][0]
    assert item["relation"] == "contradicts"
    assert item["claim"] == "clipping is no longer standard"
    assert item["existing_text"] == "clipping is standard", "both sides, or it is a claim"
    assert item["rationale"]


def test_undoing_a_patch_that_does_not_exist_is_a_404(client):
    assert client.post("/v1/patches/pch_nope/undo").status_code == 404


def test_undoing_a_patch_that_never_applied_is_refused_with_a_reason(client):
    patch = _patch(client.state.store, applied=False)

    response = client.post(f"/v1/patches/{patch.patch_id}/undo")

    assert response.status_code in (400, 409)
    assert "undo" in response.json()["detail"] or "never applied" in response.json()["detail"]


def test_undo_on_a_vault_with_no_vault_configured_names_the_right_setting(tmp_path,
                                                                         monkeypatch):
    """Telling a vault user that `NOTION_TOKEN` is not set is an instruction they cannot
    follow, and sends them hunting for a Notion problem they do not have."""
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'v.db'}",
                        artifact_url=f"file://{tmp_path / 'a'}",
                        backend="markdown", vault_path=None)
    state = AppState(settings=settings)
    patch = _patch(state.store, applied=True)

    with TestClient(create_app(state)) as c:
        detail = c.post(f"/v1/patches/{patch.patch_id}/undo").json()["detail"]

    assert "PALIMPSEST_VAULT" in detail
    assert "NOTION_TOKEN" not in detail


# ---------------------------------------------------------------------------
# the agent turn
# ---------------------------------------------------------------------------


def test_an_empty_message_is_refused(client):
    assert client.post("/v1/agent", json={"message": "   "}).status_code == 422


def test_asking_with_no_model_configured_says_so(client):
    response = client.post("/v1/agent", json={"message": "what do I know about X"})

    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


# ---------------------------------------------------------------------------
# the demo
# ---------------------------------------------------------------------------


def test_an_ordinary_install_is_not_a_demo(client):
    body = client.get("/v1/demo").json()

    assert body["demo"] is False
    assert body["prompts"] == []


def test_a_demo_offers_prompts_that_reach_each_relation(tmp_path):
    """A visitor who has not read the sample notes cannot invent a claim that lands on
    `contradicts` on purpose, so without these the demo shows that something happens
    without showing the thing worth seeing."""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'd.db'}",
                        artifact_url=f"file://{tmp_path / 'a'}",
                        backend="markdown", vault_path=str(tmp_path),
                        environment="demo")
    with TestClient(create_app(AppState(settings=settings))) as c:
        body = c.get("/v1/demo").json()

    assert body["demo"] is True
    assert body["vault"] == str(tmp_path)
    assert len(body["prompts"]) >= 3
    for prompt in body["prompts"]:
        assert prompt["label"] and prompt["text"] and prompt["why"]


# ---------------------------------------------------------------------------
# the live stream
#
# The stream is infinite by design, so these test the frames rather than reading the
# socket: a test that iterates it never stops, and one that reads exactly N lines is a
# timing bet that will eventually be a flake in somebody's CI.
# ---------------------------------------------------------------------------


def test_the_stream_is_declared_so_that_nothing_in_the_way_buffers_it(client):
    """The route is called directly and its body left unread, on purpose: the generator
    is infinite, and a test client that opens it has nothing to wait for on the way out.

    The headers are the whole substance anyway. A proxy that buffers an event stream
    turns live updates into one batch delivered when the connection closes, which for an
    infinite stream is never -- the UI sits on a working backend showing nothing.
    """
    import asyncio

    route = next(r for r in client.app.routes
                 if getattr(r, "path", None) == "/v1/events")
    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(route.endpoint())
    finally:
        loop.close()

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_a_frame_is_shaped_the_way_eventsource_expects():
    """`addEventListener("job", ...)` only fires on a named event, and only a blank line
    ends a frame. Getting either wrong means the browser receives bytes and dispatches
    nothing -- with no error anywhere."""
    from palimpsest.serve.ui_api import _sse

    frame = _sse("job", {"job_id": "job_1"})

    assert frame.startswith("event: job\n")
    assert frame.endswith("\n\n"), "a frame is not delivered until the blank line"
    assert json.loads(frame.split("data: ", 1)[1]) == {"job_id": "job_1"}


def test_a_frame_survives_data_that_is_not_json_serialisable():
    """One unserialisable value would otherwise raise inside the generator and take the
    whole stream down -- and the UI would show a connection that simply stopped."""
    import datetime

    from palimpsest.serve.ui_api import _sse

    frame = _sse("job", {"at": datetime.datetime(2026, 1, 1)})

    assert "2026-01-01" in frame


def test_a_job_frame_carries_what_the_feed_renders():
    from palimpsest.serve.ui_api import _job_event

    event = _job_event({
        "job_id": "job_1", "status": "done", "title": "A paper on clipping",
        "result": {"claims": 3, "source": {"kind": "pdf"},
                   "patch": {"by_relation": {"corroborates": 2, "new": 1}},
                   "auto_applied": {"applied": 2, "held": 1,
                                    "approval_id": "apr_1"}}})

    assert event["status"] == "done"
    assert event["title"] == "A paper on clipping"
    assert event["kind"] == "pdf"
    assert event["claims"] == 3
    assert event["by_relation"] == {"corroborates": 2, "new": 1}
    assert event["applied"] == 2
    assert event["held"] == 1
    assert event["approval_id"] == "apr_1", "the feed puts an Approve button on this"


def test_a_job_with_no_title_falls_back_to_something_a_person_can_read():
    """An untitled row in the feed is a row you cannot match to the thing you sent."""
    from palimpsest.serve.ui_api import _job_event

    assert _job_event({"job_id": "j", "status": "queued",
                       "url": "https://example.com/p"})["title"] ==         "https://example.com/p"
    assert _job_event({"job_id": "j", "status": "queued",
                       "spec": "text:a thought I had"})["title"].startswith("text:")


def test_a_failed_job_frame_carries_the_error():
    from palimpsest.serve.ui_api import _job_event

    event = _job_event({"job_id": "j", "status": "failed",
                        "error": "that PDF is encrypted"})

    assert event["error"] == "that PDF is encrypted"
    assert event["claims"] is None
