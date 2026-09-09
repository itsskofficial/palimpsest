"""The two doors a human says yes at: the review UI and the bot.

Everything that reaches your notes goes through `palimpsest.approval`, and the gate
itself is well covered (`test_safety.py`, `test_agent.py`). What was not covered is the
*doors* — the HTTP route and the Telegram callback that a person actually taps. That is
the gap this file closes, because a bug there is not an abstract one: a resolve route
that applies twice writes a change to Notion twice, a callback that trusts any chat id
hands a stranger's tap the same authority as yours, and a settings writer that accepts
any key at all is a remote-code-execution-shaped hole in a local app.

So the tests here drive the surfaces, not the gate: FastAPI's `TestClient` for the UI,
and a fake Telegram transport plus an inline thread runner for the bot. Nothing here
touches the network, needs a key, or writes to a real Notion — the fake Notion is the
assertion target, because "nothing was written" is only meaningful if something *could*
have been.
"""

from __future__ import annotations

import contextlib
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from palimpsest import approval
from palimpsest import telegram as tg
from palimpsest.agent import ToolContext
from palimpsest.config import Settings
from palimpsest.serve.app import AppState, create_app
from palimpsest.store import open_store
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

pytestmark = pytest.mark.serve

#: The gating settings used to *create* an approval. Independent of whatever the surface
#: under test is configured with: holding everything is the propose-only default, and it
#: is the state in which a human is asked anything at all.
HOLD_EVERYTHING = Settings(apply=False, autonomy="none")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeNotion:
    """A Notion that records rather than writes. Every assertion about consent being
    honoured — or not honoured — is an assertion about this object."""

    def __init__(self):
        self.updated: list[str] = []
        self.appended: list[str] = []

    @property
    def writes(self) -> list[str]:
        return self.updated + self.appended

    def update_block(self, block_id, payload):
        self.updated.append(block_id)
        return {"id": block_id}

    def append_children(self, parent_id, children, after_block_id=None):
        self.appended.append(parent_id)
        return {"results": [{"id": new_id("nb_"), "type": "paragraph"}
                            for _ in children]}


class FakeQueue:
    """Enough of a capture queue to prove nothing was ingested."""

    def __init__(self):
        self.submitted: list[str] = []

    def submit(self, spec, **kw):
        self.submitted.append(spec)
        return {"job_id": new_id("job_"), "status": "queued"}


class _InlineThreading:
    """`threading` with `Thread.start()` running the work here and now.

    The bot hands every slow job to a daemon thread, which is right in production and
    untestable in a test — an assertion would race the work. Substituting the module's
    `threading` name makes each handler synchronous without changing what it does.
    """

    class Thread:
        def __init__(self, target=None, name=None, daemon=None, args=(), kwargs=None):
            self._target = target
            self._args, self._kwargs = args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

        def join(self, timeout=None):
            return None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _seed(store) -> None:
    """One page, one block — the minimum a real operation can target."""
    store.put_pages([{"page_id": "pg_1", "title": "Attention", "role": "reference",
                      "url": "https://notion.so/pg1", "last_edited": "2026-01-01"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "position": 0,
                       "text": "Attention scales the logits by 1/sqrt(d_k)."}])


@contextlib.contextmanager
def _review_app(tmp_path, name: str, **overrides):
    settings = Settings(database_url=f"sqlite:///{tmp_path / name}",
                        artifact_url=f"file://{tmp_path / 'archive'}", **overrides)
    state = AppState(settings=settings)
    _seed(state.store)
    # Injected rather than patched: `AppState.notion` returns the cached client, so the
    # apply path inside the resolve route reaches this and nothing else.
    state._notion = FakeNotion()
    with TestClient(create_app(state)) as client:
        client.state = state
        client.notion = state._notion
        yield client


@pytest.fixture(autouse=True)
def _no_ambient_credentials(tmp_path, monkeypatch):
    """No developer's real token, and no writing to a developer's real config file."""
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PALIMPSEST_CONFIG", str(tmp_path / "config.env"))


@pytest.fixture()
def ui(tmp_path):
    """A local instance with nothing configured — what a first run looks like."""
    with _review_app(tmp_path, "ui.db") as client:
        yield client


@pytest.fixture()
def wired_ui(tmp_path):
    """A local instance that could write to Notion, so a refusal to write means
    something."""
    with _review_app(tmp_path, "wired.db", notion_token="ntn_test") as client:
        yield client


@pytest.fixture()
def remote_ui(tmp_path):
    """The same app bound to a network interface, where settings must be refused."""
    with _review_app(tmp_path, "remote.db", host="0.0.0.0") as client:
        yield client


def _citation(target: str = "bk_1") -> Operation:
    return Operation(kind=OpKind.ADD_CITATION, target=target,
                     relation=Relation.CORROBORATES,
                     payload={"label": "src", "rationale": "the source says so too"})


def _hold_one(client, *ops) -> str:
    """Put a patch through the gate with writes off, and return the approval waiting."""
    patch = Patch(patch_id=new_id("pch_"), source_id="src_1",
                  operations=list(ops) or [_citation()])
    client.state.store.put_patch(patch)
    out = approval.gate(client.state.store, patch, HOLD_EVERYTHING)
    return out["approval_id"]


# ---------------------------------------------------------------------------
# the UI door: what the instance says about itself
# ---------------------------------------------------------------------------


def test_setup_state_on_a_fresh_instance_reports_nothing_configured(ui):
    body = ui.get("/v1/setup/state").json()

    assert body["configured"] is False
    assert body["steps"] == {"model": False, "notion": False, "root": False,
                             "telegram_token": False, "telegram_paired": False}
    assert any("NOTION_TOKEN" in p for p in body["problems"])
    assert body["apply"] is False


# ---------------------------------------------------------------------------
# the UI door: the queue
# ---------------------------------------------------------------------------


def test_the_approvals_queue_is_empty_before_anything_is_held(ui):
    assert ui.get("/v1/approvals").json()["approvals"] == []


def test_the_approvals_queue_expands_the_operations_it_is_holding(ui):
    """The list is what the reviewer reads before tapping. An approval that arrives with
    an empty `operations` array is a consent request for an invisible change."""
    op = Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                   payload={"text": "A new sentence.", "rationale": "not stated yet"})
    approval_id = _hold_one(ui, op)

    rows = ui.get("/v1/approvals").json()["approvals"]
    assert [r["approval_id"] for r in rows] == [approval_id]
    (shown,) = rows[0]["operations"]
    assert shown["op_id"] == op.op_id
    assert shown["kind"] == "append_block"
    assert shown["relation"] == "new"
    assert shown["text"] == "A new sentence."
    assert shown["why"] == "not stated yet"
    assert shown["page"] == "Attention"          # resolved through the mirror, not an id


# ---------------------------------------------------------------------------
# the UI door: resolving
# ---------------------------------------------------------------------------


def test_approving_over_http_applies_the_held_operations_to_notion(wired_ui):
    approval_id = _hold_one(wired_ui)

    body = wired_ui.post(f"/v1/approvals/{approval_id}/resolve",
                         json={"decision": "approved", "by": "sk"}).json()

    assert body["ok"] is True
    assert body["status"] == "approved"
    assert body["applied"] == 1
    assert wired_ui.notion.updated == ["bk_1"]   # the write actually happened
    stored = wired_ui.state.store.get_approval(approval_id)
    assert stored["status"] == "approved"
    assert stored["resolved_by"] == "sk"


def test_rejecting_over_http_writes_nothing_to_notion(wired_ui):
    approval_id = _hold_one(wired_ui)

    body = wired_ui.post(f"/v1/approvals/{approval_id}/resolve",
                         json={"decision": "rejected", "by": "sk"}).json()

    assert body["status"] == "rejected"
    assert body["applied"] == 0
    assert wired_ui.notion.writes == []
    assert wired_ui.state.store.get_approval(approval_id)["status"] == "rejected"


def test_an_unknown_decision_is_refused_before_anything_is_looked_up(wired_ui):
    approval_id = _hold_one(wired_ui)

    r = wired_ui.post(f"/v1/approvals/{approval_id}/resolve", json={"decision": "yes"})

    assert r.status_code == 422
    assert "approved" in r.json()["detail"]
    assert wired_ui.state.store.get_approval(approval_id)["status"] == "pending"


def test_resolving_an_approval_that_does_not_exist_is_a_clear_4xx(ui):
    """Not a 500. A reviewer who taps a stale button deserves a sentence explaining
    that the approval is gone, and an operator deserves a log free of tracebacks that
    are not bugs."""
    r = ui.post("/v1/approvals/apr_nonexistent/resolve", json={"decision": "approved"})

    assert 400 <= r.status_code < 500
    assert "apr_nonexistent" in r.json()["detail"]


def test_resolving_an_already_resolved_approval_does_not_apply_it_twice(wired_ui):
    """The double-spend bug, in the shape this product takes it.

    A second tap — a double click, a retried request, a phone that replayed the button —
    must not run the operations again. Applying twice means the sentence gets two
    citations, or the block gets rewritten on top of a rewrite, and the undo record now
    describes only half of what happened. So the second call must refuse, and the fake
    Notion must have seen exactly one write.
    """
    approval_id = _hold_one(wired_ui)
    path = f"/v1/approvals/{approval_id}/resolve"

    first = wired_ui.post(path, json={"decision": "approved", "by": "sk"})
    second = wired_ui.post(path, json={"decision": "approved", "by": "sk"})

    assert first.status_code == 200
    assert 400 <= second.status_code < 500
    assert "already" in second.json()["detail"]
    assert wired_ui.notion.updated == ["bk_1"]   # once, not twice


def test_an_expired_approval_is_refused_and_says_so(wired_ui):
    """A day-old approval was written against a workspace that has since moved on."""
    approval_id = _hold_one(wired_ui)
    stale = {**wired_ui.state.store.get_approval(approval_id), "expires_at": 1.0}
    wired_ui.state.store.put_approval(stale)

    r = wired_ui.post(f"/v1/approvals/{approval_id}/resolve",
                      json={"decision": "approved", "by": "sk"})

    assert 400 <= r.status_code < 500
    assert "expired" in r.json()["detail"]
    assert wired_ui.notion.writes == []
    assert wired_ui.state.store.get_approval(approval_id)["status"] == "expired"


# ---------------------------------------------------------------------------
# the UI door: settings, the one genuinely dangerous route
# ---------------------------------------------------------------------------


def test_the_settings_write_is_refused_by_a_non_local_instance(remote_ui):
    """Credentials arrive in the clear on this route. Over loopback that is a desktop
    app talking to its own backend; over a socket bound to an interface it is a
    credential post, so the route must be gone rather than merely discouraged."""
    r = remote_ui.post("/v1/settings",
                       json={"values": {"NOTION_TOKEN": "ntn_stolen"}})

    assert r.status_code == 403
    assert "local" in r.json()["detail"]


def test_reading_settings_is_allowed_remotely_but_redacted(remote_ui, monkeypatch):
    """The read side stays open — it is how the UI shows what is configured — so it must
    never hand back a value that could be replayed."""
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret_value")

    body = remote_ui.get("/v1/settings").json()

    assert body["present"]["NOTION_TOKEN"] is True
    assert "supersecret" not in str(body["values"])


def test_the_settings_write_refuses_a_key_that_is_not_on_the_allowlist(ui):
    """An unbounded settings writer is a remote-code-execution-shaped hole.

    `WRITABLE` is an allowlist, not a filter of known-bad names: anything the UI can
    persist is a name this process will read back out of its own environment on the next
    load, and a browser that can set an arbitrary environment variable on the host can
    set the ones that decide how code is found and run. So an unrecognised key must
    reach the config file never — not sanitised, not warned about.
    """
    r = ui.post("/v1/settings",
                json={"values": {"LD_PRELOAD": "/tmp/evil.so",
                                 "PALIMPSEST_SOMETHING_NEW": "1"}})

    assert r.status_code == 422
    assert "nothing to save" in r.json()["detail"]


def test_the_settings_write_saves_the_allowlisted_key_and_drops_the_rest(ui, tmp_path,
                                                                        monkeypatch):
    """The allowlist filters per key rather than rejecting the whole payload, so a good
    key travelling beside a bad one must still be the only thing that lands."""
    # Registered with monkeypatch first so the route's own `os.environ.update` is undone
    # when the test ends.
    monkeypatch.setenv("PALIMPSEST_AUTONOMY", "none")
    monkeypatch.setenv("PALIMPSEST_APPLY", "0")

    body = ui.post("/v1/settings",
                   json={"values": {"PALIMPSEST_AUTONOMY": "low",
                                    "PALIMPSEST_TOTALLY_MADE_UP": "1"}}).json()

    assert body["saved"] == ["PALIMPSEST_AUTONOMY"]
    written = (tmp_path / "config.env").read_text(encoding="utf-8")
    assert "PALIMPSEST_AUTONOMY=low" in written
    assert "TOTALLY_MADE_UP" not in written


# ---------------------------------------------------------------------------
# the Telegram door
# ---------------------------------------------------------------------------


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    """A bot with no network and no threads.

    `_call` is the module's single transport seam — `send`, `edit` and the callback
    acknowledgement all go through it — so replacing it makes the whole surface offline
    and turns every reply into an assertable record.
    """
    sent: list[tuple[str, dict]] = []

    def fake_call(token, method, params=None, timeout=40.0):
        params = params or {}
        sent.append((method, params))
        if method == "sendMessage":
            return {"message_id": 100 + len(sent), "chat": {"id": params.get("chat_id")}}
        return {}

    monkeypatch.setattr(tg, "_call", fake_call)
    monkeypatch.setattr(tg, "threading", _InlineThreading)

    notion = FakeNotion()
    monkeypatch.setattr(ToolContext, "new_notion", lambda self: notion)
    monkeypatch.setattr(ToolContext, "new_journal", lambda self: None)

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'bot.db'}",
                        artifact_url=f"file://{tmp_path / 'archive'}",
                        notion_token="ntn_test")
    store = open_store(settings.database_url)
    _seed(store)

    made = tg.Bot(token="tok:secret", settings=settings,
                  store_factory=lambda: open_store(settings.database_url),
                  queue=FakeQueue(), allowed=frozenset({4242}))
    made.sent = sent
    made.notion = notion
    made.store = store
    yield made
    store.close()


def _replies(bot) -> list[str]:
    return [p.get("text", "") for method, p in bot.sent
            if method in ("sendMessage", "editMessageText")]


def _hold_for_the_bot(bot) -> str:
    patch = Patch(patch_id=new_id("pch_"), source_id="src_1",
                  operations=[_citation()])
    bot.store.put_patch(patch)
    return approval.gate(bot.store, patch, HOLD_EVERYTHING,
                         chat_id="4242")["approval_id"]


def test_an_unpaired_chat_is_refused_and_told_only_its_own_id(bot):
    """A bot token is a bearer credential: anyone who finds the bot's username can
    message it, so the allowlist is the only thing standing between a stranger and write
    access to someone's notes. The refusal leaks the asking chat's own id — which that
    chat already knows — and nothing else, because that id is what pairing needs.
    """
    bot.handle_message({"chat": {"id": 9999}, "text": "remember this for me"})

    assert [m for m, _ in bot.sent] == ["sendMessage"]
    (reply,) = _replies(bot)
    assert "not paired" in reply
    assert "9999" in reply
    assert bot.queue.submitted == []             # nothing was ingested


def test_an_unpaired_chat_cannot_resolve_an_approval_it_names(bot):
    """The callback carries an approval id, so a refused chat that was still routed
    through to the gate would be tapping someone else's Approve button."""
    approval_id = _hold_for_the_bot(bot)

    bot.handle_callback({"id": "q1", "data": f"ap:{approval_id}",
                         "message": {"chat": {"id": 9999}, "message_id": 7}})

    assert bot.store.get_approval(approval_id)["status"] == "pending"
    assert bot.notion.writes == []
    assert "not paired" in _replies(bot)[-1]


def test_a_paired_chat_is_served(bot):
    bot.handle_message({"chat": {"id": 4242}, "text": "/pending"})

    (reply,) = _replies(bot)
    assert "Nothing waiting" in reply
    assert "not paired" not in reply


def test_the_pending_command_lists_what_is_waiting_with_buttons(bot):
    approval_id = _hold_for_the_bot(bot)

    bot.handle_message({"chat": {"id": 4242}, "text": "/pending"})

    buttons = [p for m, p in bot.sent if p.get("reply_markup")]
    assert buttons, "an approval listed without Approve/Reject is unreachable"
    taps = buttons[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in taps] == [f"ap:{approval_id}",
                                                  f"rj:{approval_id}"]


def test_the_approve_button_resolves_the_named_approval_and_applies_it(bot):
    approval_id = _hold_for_the_bot(bot)

    bot.handle_callback({"id": "q1", "data": f"ap:{approval_id}",
                         "message": {"chat": {"id": 4242}, "message_id": 7}})

    assert bot.store.get_approval(approval_id)["status"] == "approved"
    assert bot.notion.updated == ["bk_1"]
    assert "Applied 1 change(s)" in _replies(bot)[-1]


def test_the_reject_button_writes_nothing(bot):
    approval_id = _hold_for_the_bot(bot)

    bot.handle_callback({"id": "q1", "data": f"rj:{approval_id}",
                         "message": {"chat": {"id": 4242}, "message_id": 7}})

    assert bot.store.get_approval(approval_id)["status"] == "rejected"
    assert bot.notion.writes == []
    assert "Rejected" in _replies(bot)[-1]


def test_the_button_only_resolves_the_approval_it_names(bot):
    """Two approvals, one tap. Resolving by position, or resolving everything pending,
    would be invisible in a one-approval test and catastrophic in real use."""
    first = _hold_for_the_bot(bot)
    second = _hold_for_the_bot(bot)

    bot.handle_callback({"id": "q1", "data": f"rj:{second}",
                         "message": {"chat": {"id": 4242}, "message_id": 7}})

    assert bot.store.get_approval(second)["status"] == "rejected"
    assert bot.store.get_approval(first)["status"] == "pending"


def test_a_callback_naming_an_unknown_approval_replies_instead_of_raising(bot):
    bot.handle_callback({"id": "q1", "data": "ap:apr_nonexistent",
                         "message": {"chat": {"id": 4242}, "message_id": 7}})

    assert "Couldn't apply that" in _replies(bot)[-1]
    assert bot.notion.writes == []


def test_a_callback_acknowledges_the_tap_before_doing_the_work(bot):
    """Telegram spins the button until the query is answered. Doing the work first means
    a several-second spinner and a user who taps again — which is the double-apply the
    gate then has to refuse."""
    approval_id = _hold_for_the_bot(bot)

    bot.handle_callback({"id": "q1", "data": f"ap:{approval_id}",
                         "message": {"chat": {"id": 4242}, "message_id": 7}})

    assert bot.sent[0][0] == "answerCallbackQuery"


def test_an_expired_approval_tapped_from_telegram_writes_nothing(bot):
    approval_id = _hold_for_the_bot(bot)
    bot.store.put_approval({**bot.store.get_approval(approval_id),
                            "expires_at": time.time() - 1})

    bot.handle_callback({"id": "q1", "data": f"ap:{approval_id}",
                         "message": {"chat": {"id": 4242}, "message_id": 7}})

    assert bot.notion.writes == []
    assert bot.store.get_approval(approval_id)["status"] == "expired"
    assert "expired" in _replies(bot)[-1]


def test_rejecting_something_that_cannot_be_rejected_is_an_error_not_a_success(ui):
    """A failed rejection used to return 200.

    The guard read `if not result["ok"] and decision == "approved"`, so only approvals
    reported their failures. Rejecting an approval that was missing, already resolved or
    expired answered 200 with `ok: false` — the UI removed the card, believed the
    proposal was discarded, and the proposal was still sitting there pending.
    """
    response = ui.post("/v1/approvals/apr_does_not_exist/resolve",
                       json={"decision": "rejected"})
    assert response.status_code == 409
