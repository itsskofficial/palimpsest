"""The bot and the Notion-side ledger.

Two things here are load-bearing rather than merely nice:

- **The allowlist.** A bot token is a bearer credential. Anyone who finds the bot can
  message it, and without the guard that means anyone can write to someone else's
  Notion. Every test that touches an unpaired chat is checking a security property.
- **The journal never breaking an edit.** It is a log. A log that can refuse your edits
  is worse than no log, so its failure path is tested as deliberately as its happy one.
"""

from __future__ import annotations

import pytest

from palimpsest.config import Settings
from palimpsest.notion.journal import Journal, _headline, _why
from palimpsest.telegram import Bot
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

# ---------------------------------------------------------------------------
# a Telegram that records instead of sending
# ---------------------------------------------------------------------------


class FakeTelegram:
    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answered: list[str] = []

    def __call__(self, token, method, params=None, timeout=None):
        params = params or {}
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": len(self.sent)}
        if method == "editMessageText":
            self.edits.append(params)
            return {}
        if method == "answerCallbackQuery":
            self.answered.append(params.get("callback_query_id", ""))
            return True
        if method == "getMe":
            return {"username": "palimpsest_test_bot"}
        if method == "getUpdates":
            return []
        return {}

    @property
    def texts(self) -> list[str]:
        return [m.get("text", "") for m in self.sent]


class FakeQueue:
    def __init__(self):
        self.submitted: list[dict] = []

    def submit(self, spec, **kw):
        job = {"job_id": new_id("job_"), "spec": spec, **kw}
        self.submitted.append(job)
        return job


@pytest.fixture()
def bot(store, monkeypatch):
    import palimpsest.telegram as mod

    fake = FakeTelegram()
    monkeypatch.setattr(mod, "_call", fake)
    b = Bot(token="t", settings=Settings(), store_factory=lambda: store,
            queue=FakeQueue(), allowed=frozenset({42}))
    b.transport = fake  # type: ignore[attr-defined]
    return b


def message(text=None, chat_id=42, **extra):
    return {"chat": {"id": chat_id}, "message_id": 1, "text": text, **extra}


# ---------------------------------------------------------------------------
# the allowlist
# ---------------------------------------------------------------------------


def test_an_unpaired_chat_is_refused_and_told_only_its_own_id(bot):
    bot.handle_message(message("https://example.com/post", chat_id=99))

    assert bot.queue.submitted == []  # nothing was captured
    reply = bot.transport.texts[0]
    assert "not paired" in reply
    # The id is the one thing it may leak, because that chat already knows it.
    assert "99" in reply


def test_an_unpaired_chat_cannot_resolve_an_approval_with_a_button(bot, monkeypatch):
    """The callback path needs its own guard. Checking only on inbound messages would
    leave the buttons open to anyone who ever received a forwarded one."""
    resolved = []
    monkeypatch.setattr(bot, "_resolve_approval", lambda *a, **k: resolved.append(a))

    bot.handle_callback({
        "id": "cb1", "data": "ap:apr_x",
        "message": {"chat": {"id": 99}, "message_id": 7},
    })

    assert resolved == []
    assert "not paired" in bot.transport.texts[0]


def test_a_paired_chat_reaches_the_agent(bot, monkeypatch):
    """A text message is a conversation now — it routes to the agent, which decides
    whether to capture it or answer it. The bot no longer guesses."""
    seen = []
    monkeypatch.setattr(bot, "converse", lambda chat_id, text: seen.append((chat_id, text)))
    bot.handle_message(message("what do I know about attention?"))
    assert seen == [(42, "what do I know about attention?")]


def test_group_chat_ids_are_negative_and_survive_parsing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "42, -1001234567890")
    assert Settings.load().telegram_allowed_chats == (42, -1001234567890)


def test_a_junk_chat_id_is_rejected_at_load_rather_than_ignored(monkeypatch):
    """Silently dropping an unparseable id would leave someone with a bot that refuses
    them and a config file that looks correct."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "42,@myusername")
    with pytest.raises(ValueError, match="not a chat id"):
        Settings.load()


def test_a_token_with_no_allowlist_is_reported_as_a_problem():
    problems = Settings(telegram_token="t").problems()
    assert any("TELEGRAM_ALLOWED_CHATS is empty" in p for p in problems)


# ---------------------------------------------------------------------------
# routing: text to the agent, files to capture, commands to handlers
# ---------------------------------------------------------------------------


def test_text_goes_to_the_agent_not_straight_to_capture(bot, monkeypatch):
    """The old bot guessed whether text was a note, a transcript or a link. That
    judgement now belongs to the agent, which can actually read the message."""
    seen = []
    monkeypatch.setattr(bot, "converse", lambda c, t: seen.append(t))
    bot.handle_message(message("Remember that AdamW decouples weight decay."))
    bot.handle_message(message("https://youtu.be/dQw4w9WgXcQ"))
    assert len(seen) == 2
    assert bot.queue.submitted == []  # text never bypasses the agent into the queue


def test_a_dropped_file_bypasses_the_agent_and_captures_directly(bot, monkeypatch):
    """A file has no ambiguity — there is nothing to decide — so it goes straight to
    the queue without spending an agent turn."""
    captured = []
    monkeypatch.setattr(bot, "capture_file",
                        lambda *a, **k: captured.append(a))
    bot.handle_message({"chat": {"id": 42}, "message_id": 1,
                        "document": {"file_id": "f1", "file_name": "paper.pdf"}})
    assert len(captured) == 1


def test_an_unknown_command_says_so_without_reaching_the_agent(bot, monkeypatch):
    monkeypatch.setattr(bot, "converse", lambda *a: pytest.fail("should not converse"))
    bot.handle_message(message("/frobnicate"))
    assert "do not know" in bot.transport.texts[0]


def test_approve_and_reject_buttons_drive_the_gate(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "_resolve_approval",
                        lambda chat, aid, decision, mid: calls.append((aid, decision)))
    bot.handle_callback({"id": "c1", "data": "ap:apr_1",
                         "message": {"chat": {"id": 42}, "message_id": 5}})
    bot.handle_callback({"id": "c2", "data": "rj:apr_2",
                         "message": {"chat": {"id": 42}, "message_id": 6}})
    assert calls == [("apr_1", "approved"), ("apr_2", "rejected")]


# ---------------------------------------------------------------------------
# the Notion journal
# ---------------------------------------------------------------------------


class FakeNotionDatabases:
    def __init__(self, fail: bool = False):
        self.databases: list[dict] = []
        self.rows: list[dict] = []
        self.fail = fail

    def create_database(self, parent_page_id, title, properties, icon=None,
                        description=None):
        if self.fail:
            raise RuntimeError("notion is down")
        db = {"id": new_id("db_"), "title": title, "properties": properties,
              "data_sources": [{"id": new_id("ds_"), "name": title}]}
        self.databases.append(db)
        return db

    def data_source_id(self, database):
        return (database.get("data_sources") or [{}])[0].get("id")

    def create_row(self, data_source_id, properties, children=None, icon=None):
        if self.fail:
            raise RuntimeError("notion is down")
        row = {"data_source_id": data_source_id, "properties": properties,
               "children": children}
        self.rows.append(row)
        return row


def _op(**kw):
    payload = kw.pop("payload", {})
    return Operation(kind=kw.pop("kind", OpKind.ADD_CITATION),
                     target=kw.pop("target", "bk_1"), payload=payload,
                     relation=kw.pop("relation", Relation.CORROBORATES), **kw)


def test_the_journal_creates_both_databases_once_and_caches_them(store):
    client = FakeNotionDatabases()
    journal = Journal(client, store, "pg_root")

    first = journal.data_sources()
    assert set(first) == {"changes", "sources"}
    assert len(client.databases) == 2

    # A second Journal against the same store must find them, not make more. Two pairs
    # of databases side by side cannot be merged afterwards.
    again = Journal(client, store, "pg_root").data_sources()
    assert again == first
    assert len(client.databases) == 2


def test_a_change_row_records_the_reasoning(store):
    client = FakeNotionDatabases()
    journal = Journal(client, store, "pg_root")
    store.put_pages([{"page_id": "pg_a", "title": "Attention",
                      "url": "https://notion.so/a"}])

    op = _op(payload={"rationale": "the page already states this, from a different source",
                      "confidence": 0.93, "label": "src",
                      "anchor": {"locator": "14:22"}})
    op.applied_at = 1.0
    journal.record_change(op, patch_id="pch_1", page=store.get_page("pg_a"),
                          source={"url": "https://youtu.be/x"}, reviewer="sk")

    props = client.rows[0]["properties"]
    assert props["Why"]["rich_text"][0]["text"]["content"].startswith("the page already")
    assert props["Relation"]["select"]["name"] == "corroborates"
    assert props["Confidence"]["number"] == 0.93
    assert props["Cites"]["rich_text"][0]["text"]["content"] == "14:22"
    assert props["Approved by"]["rich_text"][0]["text"]["content"] == "sk"
    assert props["Status"]["select"]["name"] == "Applied"
    assert props["Patch"]["rich_text"][0]["text"]["content"] == "pch_1"


def test_a_row_without_a_rationale_still_explains_itself(store):
    """The relation is an explanation of a kind. An empty `Why` column would be the
    one thing this database exists to avoid."""
    client = FakeNotionDatabases()
    Journal(client, store, "pg_root").record_change(
        _op(payload={}), patch_id="p", page=None, source=None, reviewer=None)

    why = client.rows[0]["properties"]["Why"]["rich_text"][0]["text"]["content"]
    assert why == Relation.CORROBORATES.describe()


def test_a_journal_failure_never_breaks_the_edit(store):
    """A log line is not worth refusing an edit over."""
    client = FakeNotionDatabases(fail=True)
    journal = Journal(client, store, "pg_root")

    journal.record_change(_op(payload={}), patch_id="p", page=None, source=None,
                          reviewer=None)  # must not raise
    journal.record_source({"title": "x", "kind": "web"}, claims=1, changes=1)
    assert client.rows == []


def test_the_journal_is_off_without_a_root_page(store):
    """There is nowhere to create the databases, and guessing a location for something
    this visible is not a decision to make silently."""
    client = FakeNotionDatabases()
    journal = Journal(client, store, None)

    assert journal.enabled is False
    journal.record_change(_op(payload={}), patch_id="p", page=None, source=None,
                          reviewer=None)
    assert client.databases == [] and client.rows == []


def test_a_revert_is_recorded_as_reverted(store):
    client = FakeNotionDatabases()
    Journal(client, store, "pg_root").record_change(
        _op(payload={}), patch_id="p", page=None, source=None, reviewer="sk",
        status="Reverted")
    assert client.rows[0]["properties"]["Status"]["select"]["name"] == "Reverted"


def test_structural_operations_explain_where_a_page_went(store):
    op = Operation(kind=OpKind.MOVE_PAGE, target="pg_a", risk="medium",
                   payload={"hub": "Machine Learning", "confidence": 0.96})
    assert "filed under Machine Learning" in _why(op, op.payload)
    assert _headline(op) == "Filed"

    renamed = Operation(kind=OpKind.RENAME_PAGE, target="pg_a",
                        payload={"title": "Attention", "was": "notes 2"})
    assert "previously" in _why(renamed, renamed.payload)
    assert _headline(renamed) == "Renamed: Attention"


def test_the_planner_stamps_reasoning_onto_every_operation(mirror, source, claim):
    """Without this the journal has nothing to write and a footnote has nothing to say —
    the rationale would live only in the judgements table, which nobody reads."""
    from palimpsest.plan import plan
    from palimpsest.types import Judgement

    judgement = Judgement(claim_id=claim.claim_id, relation=Relation.CORROBORATES,
                          confidence=0.91, target_page_id="pg_attention",
                          target_block_id="bk_att_1",
                          rationale="the page already says exactly this")
    result = plan([judgement], {claim.claim_id: claim}, source, mirror)

    assert len(result.patch) >= 1
    for op in result.patch.operations:
        assert op.payload["rationale"] == "the page already says exactly this"
        assert op.payload["confidence"] == 0.91


def test_the_footnote_on_the_page_carries_the_why(store):
    """`palimpsest provenance` exists, and nobody runs it. The answer has to be next to
    the sentence, in Notion, where the question is actually asked."""
    from palimpsest.notion.blocks import footnote_block

    block = footnote_block("old wording", "A lecture", "14:22",
                           "https://youtu.be/x?t=862",
                           why="a later source states the figure more precisely")
    rendered = "".join(r["text"]["content"]
                       for r in block["callout"]["rich_text"])
    assert "14:22" in rendered
    assert "more precisely" in rendered


def test_the_patch_apply_path_accepts_a_journal(store):
    """Wiring check: `apply_patch` must take the journal and use it, or every row above
    is tested in isolation and never written in practice."""
    from palimpsest.notion.apply import apply_patch

    class Notion:
        def update_block(self, block_id, payload):
            return {"id": block_id}

    store.put_pages([{"page_id": "pg_a", "title": "Attention"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_a", "type": "paragraph",
                       "text": "Attention scales by sqrt(d_k).", "position": 0}])

    client = FakeNotionDatabases()
    journal = Journal(client, store, "pg_root")
    patch = Patch(patch_id="pch_1", source_id="", operations=[
        _op(kind=OpKind.UPDATE_TEXT, target="bk_1",
            payload={"text": "Attention scales by one over sqrt(d_k).",
                     "rationale": "sharper wording"})])

    result = apply_patch(Notion(), store, patch, reviewer="sk", journal=journal)
    assert result.status == "applied"
    assert len(client.rows) == 1
    assert client.rows[0]["properties"]["Why"]["rich_text"][0]["text"]["content"] == \
        "sharper wording"
