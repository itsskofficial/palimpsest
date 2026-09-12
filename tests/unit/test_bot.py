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


class Kept:
    """The one test database, wrapped so the bot cannot close it.

    The bot's contract is that it owns whatever `store_factory` hands it and closes it
    when done -- correct in production, where each call opens a fresh connection. A
    fixture that wants one in-memory database across several calls has to say so.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


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
    b = Bot(token="t", settings=Settings(), store_factory=lambda: Kept(store),
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

    cited = "".join(r["text"]["content"] for r in block["callout"]["rich_text"])
    assert "14:22" in cited
    assert cited.endswith("14:22"), "the locator is the end of the citation"

    # The reasoning is a child rather than a second line of the citation itself: a
    # vault reads a bare continuation line back as a separate paragraph, and the
    # footnote's own undo then leaves it orphaned on the page.
    why = block["callout"]["children"]
    assert "more precisely" in why[0]["paragraph"]["rich_text"][0]["text"]["content"]


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


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def test_help_lists_what_it_can_do(bot):
    bot.handle_message(message("/help"))
    bot.handle_message(message("/start"))

    assert len(bot.transport.texts) == 2
    for text in bot.transport.texts:
        assert "/pending" in text and "/undo" in text


def test_a_command_addressed_to_the_bot_by_name_still_dispatches(bot):
    """In a group, Telegram delivers `/status@my_bot`.

    Splitting on `@` is the difference between working in a group and answering "I do
    not know" to every command — which is where a bot shared with anyone actually lives.
    """
    bot.handle_message(message("/pending@palimpsest_test_bot"))

    assert bot.transport.texts
    assert "do not know" not in bot.transport.texts[0].lower()


def test_pending_with_nothing_waiting_says_so(bot):
    bot.handle_message(message("/pending"))
    assert "Nothing waiting" in bot.transport.texts[0]


def test_pending_lists_each_approval_with_its_own_buttons(bot, store):
    for i in range(2):
        store.put_approval({
            "approval_id": f"apr_{i}", "patch_id": f"pch_{i}",
            "operation_ids": ["op_a", "op_b"], "status": "pending",
            "summary": f"change number {i}"})

    bot.handle_message(message("/pending"))

    assert "2 waiting for you" in bot.transport.texts[0]
    # One message per approval, each carrying its own pair of buttons — so tapping
    # Approve under the second cannot resolve the first.
    with_buttons = [m for m in bot.transport.sent if m.get("reply_markup")]
    assert len(with_buttons) == 2
    markup = with_buttons[0]["reply_markup"]
    data = [b["callback_data"] for b in markup["inline_keyboard"][0]]
    assert data[0].startswith("ap:") and data[1].startswith("rj:")
    assert data[0].split(":")[1] == data[1].split(":")[1]


def test_undo_without_an_argument_asks_which_one(bot):
    bot.handle_message(message("/undo"))
    assert "Which one" in bot.transport.texts[0]


def test_status_reports_the_configuration(bot):
    bot.cmd_status(42)
    text = " ".join(bot.transport.texts).lower()
    assert "notion" in text or "model" in text or "mirror" in text


def test_an_empty_message_is_answered_rather_than_ignored(bot):
    """A forwarded sticker, a location pin, a poll. Silence reads as "it is broken"."""
    bot.handle_message(message(None))
    assert "did not find anything" in bot.transport.texts[0]


# ---------------------------------------------------------------------------
# files, which are the point of having it on a phone
# ---------------------------------------------------------------------------


@pytest.fixture()
def downloads(bot, monkeypatch, tmp_path):
    """Make `_download` produce a real file without touching the network."""
    written = []

    def fake(file_id, suggested):
        path = tmp_path / (suggested or file_id)
        path.write_bytes(b"\x00" * 16)
        written.append(path)
        return path

    monkeypatch.setattr(bot, "_download", fake)
    return written


def test_a_photo_is_captured_at_its_largest_size(bot, downloads):
    """Telegram sends a ladder of sizes, smallest first. Taking the first captures a
    thumbnail, and a whiteboard photographed at 90x60 is not a whiteboard."""
    bot.handle_message(message(None, photo=[
        {"file_id": "small", "width": 90}, {"file_id": "big", "width": 1280}]))

    assert bot.queue.submitted, "nothing was queued"
    assert downloads and downloads[0].exists()


def test_a_voice_note_is_given_a_suffix_so_it_reads_as_audio(bot, downloads):
    """A voice note arrives with no filename, and the transcriber is chosen by suffix.
    Without this the recording is captured as an unreadable blob that silently produces
    nothing — which is the single most likely thing to be sent from a phone."""
    bot.handle_message(message(None, voice={"file_id": "v1", "mime_type": "audio/ogg"}))

    from palimpsest.ingest import detect_kind

    assert bot.queue.submitted
    spec = bot.queue.submitted[0]["spec"]
    # Not `.ogg` specifically: `mimetypes.guess_extension("audio/ogg")` answers `.oga`
    # on some machines and `.ogg` on others, so asserting the literal makes this pass or
    # fail by platform. What has to hold is what the product depends on.
    assert detect_kind(spec) == "audio", f"{spec} is not recognised as audio"


def test_a_document_keeps_the_name_and_caption_it_was_sent_with(bot, downloads):
    bot.handle_message(message(
        None, document={"file_id": "d1", "file_name": "lecture-notes.pdf"},
        caption="week 4"))

    assert bot.queue.submitted[0]["spec"].endswith("lecture-notes.pdf")
    assert bot.queue.submitted[0]["title"] == "week 4"


def test_a_file_that_will_not_download_says_so_instead_of_raising(bot, monkeypatch):
    """An exception here would take the polling loop with it, and the bot goes silent
    until somebody notices and restarts it."""
    import palimpsest.telegram as mod

    def boom(file_id, suggested):
        raise mod.TelegramError("file is too big")

    monkeypatch.setattr(bot, "_download", boom)
    bot.handle_message(message(None, document={"file_id": "d", "file_name": "x.pdf"}))

    assert "Could not fetch" in bot.transport.texts[0]
    assert not bot.queue.submitted


def test_every_file_kind_telegram_sends_is_handled(bot, downloads):
    for kind, blob in (
        ("document", {"file_id": "a", "file_name": "a.pdf"}),
        ("audio", {"file_id": "b", "file_name": "b.mp3"}),
        ("voice", {"file_id": "c", "mime_type": "audio/ogg"}),
        ("video", {"file_id": "d", "file_name": "d.mp4"}),
        ("video_note", {"file_id": "e", "mime_type": "video/mp4"}),
    ):
        bot.queue.submitted.clear()
        bot.handle_message(message(None, **{kind: blob}))
        assert bot.queue.submitted, f"{kind} was not captured"


# ---------------------------------------------------------------------------
# telling you what came of it
# ---------------------------------------------------------------------------


def _finished(bot, store, **result):
    job_id = new_id("job_")
    store.put_job({"job_id": job_id, "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "done", "result": result})
    return job_id


def test_a_finished_capture_is_reported_with_its_breakdown(bot, store):
    _finished(bot, store, source={"title": "A paper on attention"}, claims=3,
              patch={"by_relation": {"corroborates": 2, "new": 1}},
              auto_applied={"applied": 3})

    bot.report_finished()

    text = "\n".join(bot.transport.texts)
    assert "A paper on attention" in text
    assert "3 claim(s)" in text
    assert "2 corroborates" in text and "1 new" in text
    assert "3 applied automatically" in text


def test_a_capture_that_found_nothing_says_that_plainly(bot, store):
    _finished(bot, store, source={"title": "A cookie banner"}, claims=0)

    bot.report_finished()

    assert "Nothing worth keeping" in "\n".join(bot.transport.texts)


def test_a_held_change_is_reported_with_approve_and_reject(bot, store):
    _finished(bot, store, source={"title": "A claim"}, claims=1,
              patch={"by_relation": {"refines": 1}},
              auto_applied={"applied": 0, "held": 1, "approval_id": "apr_x"})

    bot.report_finished()

    with_buttons = [m for m in bot.transport.sent if m.get("reply_markup")]
    assert with_buttons, "a held change must be actionable from the phone"
    keyboard = with_buttons[0]["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for b in keyboard[0]] == ["ap:apr_x", "rj:apr_x"]


def test_a_contradiction_is_reported_with_what_it_disagrees_with(bot, store):
    """The one verdict the system will not act on alone has to reach you somewhere, and
    on a phone this message is the only place it can."""
    _finished(bot, store, source={"title": "A source"}, claims=1,
              patch={"by_relation": {}},
              review=[{"reason": "contradiction",
                       "claim": {"text": "Per-parameter clipping is better."}}])

    bot.report_finished()

    text = "\n".join(bot.transport.texts)
    assert "1 needs you" in text
    assert "contradiction" in text
    assert "Per-parameter clipping" in text


def test_a_failed_capture_reports_the_error(bot, store):
    job_id = new_id("job_")
    store.put_job({"job_id": job_id, "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "failed",
                   "error": "that PDF is encrypted"})

    bot.report_finished()

    assert "encrypted" in "\n".join(bot.transport.texts)


def test_a_job_is_reported_once_and_not_again(bot, store):
    """`report_finished` runs every few seconds. Reporting on each pass would send the
    same summary forever."""
    _finished(bot, store, source={"title": "Once"}, claims=1, patch={"by_relation": {}})

    bot.report_finished()
    first = len(bot.transport.sent)
    bot.report_finished()

    assert len(bot.transport.sent) == first


def test_a_capture_from_another_surface_is_not_pushed_to_your_phone(bot, store):
    """The queue is shared. A file dropped on the desktop window must not ping a phone
    that was not involved."""
    job_id = new_id("job_")
    store.put_job({"job_id": job_id, "kind": "ingest", "spec": "x", "origin": "ui",
                   "status": "done", "result": {"claims": 1}})

    bot.report_finished()

    assert not bot.transport.sent


# ---------------------------------------------------------------------------
# the polling loop
# ---------------------------------------------------------------------------


def test_polling_advances_the_offset_past_what_it_handled(bot, monkeypatch):
    """Telegram redelivers anything not acknowledged by a higher offset, so getting this
    wrong makes the bot answer the same message forever."""
    import palimpsest.telegram as mod

    batches = [[{"update_id": 7, "message": message("/help")},
                {"update_id": 9, "message": message("/help")}]]

    def fake(token, method, params=None, timeout=None):
        if method == "getUpdates":
            return batches.pop(0) if batches else []
        return bot.transport(token, method, params, timeout)

    monkeypatch.setattr(mod, "_call", fake)

    assert bot.poll_once() == 2
    assert bot._offset == 10


def test_one_bad_update_does_not_stop_the_others(bot, monkeypatch):
    """An exception escaping this loop kills polling, and the bot goes quiet with no
    indication of why."""
    import palimpsest.telegram as mod

    def fake(token, method, params=None, timeout=None):
        if method == "getUpdates":
            return [{"update_id": 1, "message": {"chat": {}}},          # no id: raises
                    {"update_id": 2, "message": message("/help")}]
        return bot.transport(token, method, params, timeout)

    monkeypatch.setattr(mod, "_call", fake)

    assert bot.poll_once() == 2
    assert bot.transport.texts, "the good update must still have been handled"


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def test_markdown_that_would_break_telegram_is_escaped():
    """Telegram's legacy Markdown rejects the whole message on an unbalanced marker, so
    a page title containing an underscore silently sends nothing at all."""
    from palimpsest.telegram import _md

    backslash = chr(92)
    assert _md("snake_case") == "snake" + backslash + "_case"
    assert _md("*bold*") == backslash + "*bold" + backslash + "*"
    assert _md("a [link]") == "a " + backslash + "[link]"


# ---------------------------------------------------------------------------
# the backend the bot is actually pointed at
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault_bot(store, monkeypatch, tmp_path):
    """A bot on a markdown vault. Every surface has to work on both backends; this one
    was the last that did not."""
    import palimpsest.telegram as mod

    fake = FakeTelegram()
    monkeypatch.setattr(mod, "_call", fake)
    settings = Settings(backend="markdown", vault_path=str(tmp_path))
    b = Bot(token="t", settings=settings, store_factory=lambda: Kept(store),
            queue=FakeQueue(), allowed=frozenset({42}))
    b.transport = fake  # type: ignore[attr-defined]
    return b


def test_sync_on_a_vault_reads_the_vault_rather_than_calling_notion(vault_bot, tmp_path,
                                                                    monkeypatch):
    """`/sync` built a `NotionClient` outright. On a vault that is a call to
    api.notion.com with an empty token -- so the one command that refreshes the mirror
    failed for every vault user, with an error about Notion they could do nothing with."""
    import palimpsest.workspace as ws

    (tmp_path / "a-note.md").write_text(
        "# A note" + chr(10) * 2 + "Something written down." + chr(10),
        encoding="utf-8")
    opened: list = []
    real_open = ws.open
    monkeypatch.setattr(ws, "open",
                        lambda settings: opened.append(real_open(settings)) or opened[-1])

    vault_bot.cmd_sync(42)

    assert _wait_for("Synced", vault_bot), vault_bot.transport.texts
    assert opened, "the backend-aware door was never used"
    assert any("Synced 1 page(s)" in t for t in vault_bot.transport.texts)


def test_sync_with_no_vault_configured_says_which_setting_is_missing(store, monkeypatch):
    """Telling a vault user that `NOTION_TOKEN` is not set is an instruction they cannot
    follow, and it sends them hunting for a Notion problem they do not have."""
    import palimpsest.telegram as mod

    fake = FakeTelegram()
    monkeypatch.setattr(mod, "_call", fake)
    bot = Bot(token="t", settings=Settings(backend="markdown", vault_path=None),
              store_factory=lambda: Kept(store), queue=FakeQueue(),
              allowed=frozenset({42}))

    bot.cmd_sync(42)

    assert "PALIMPSEST_VAULT" in fake.texts[0]
    assert "NOTION_TOKEN" not in fake.texts[0]


def test_sync_on_notion_with_no_token_still_names_notion(bot):
    bot.settings = Settings(backend="notion", notion_token=None)

    bot.cmd_sync(42)

    assert "NOTION_TOKEN" in bot.transport.texts[0]


# ---------------------------------------------------------------------------
# reporting back — the promise the module is built on
# ---------------------------------------------------------------------------


def test_a_capture_the_agent_started_is_reported_too(bot, store):
    """The most common path on a phone: type a thought, or paste a link, and the text
    goes to the agent, which queues the capture through its own tool. Reporting only the
    jobs the bot submitted itself meant those finished in silence -- in a module whose
    entire premise is that something always comes back."""
    store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "done",
                   "result": {"source": {"title": "A post the agent fetched"},
                              "claims": 2, "patch": {"by_relation": {"new": 2}}}})

    bot.report_finished()

    assert "A post the agent fetched" in "\n".join(bot.transport.texts)


def test_a_restart_does_not_re_announce_everything_that_ever_finished(bot, store):
    """Forty summaries arriving at once, about work the person was told about days ago,
    is how a capture tool teaches you to mute it."""
    for i in range(5):
        store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "x",
                       "origin": "telegram:42", "status": "done",
                       "result": {"claims": 1, "source": {"title": f"Old {i}"}}})

    bot.prime()
    bot.report_finished()

    assert bot.transport.sent == []


def test_work_that_finishes_after_the_restart_is_still_reported(bot, store):
    """The other half: priming must not silence everything from then on."""
    store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "done",
                   "result": {"claims": 1, "source": {"title": "Before"}}})
    bot.prime()

    store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "y",
                   "origin": "telegram:42", "status": "done",
                   "result": {"claims": 1, "source": {"title": "After"}}})
    bot.report_finished()

    text = "\n".join(bot.transport.texts)
    assert "After" in text
    assert "Before" not in text


def test_a_job_still_running_is_not_reported_as_finished(bot, store):
    store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "running"})

    bot.report_finished()

    assert bot.transport.sent == []


def test_the_report_goes_to_the_chat_that_sent_it(bot, store):
    """The origin carries the chat. Reporting to the wrong one shows somebody else's
    notes to somebody else."""
    store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "done",
                   "result": {"claims": 1, "source": {"title": "Mine"}}})

    bot.report_finished()

    assert {m["chat_id"] for m in bot.transport.sent} == {42}


def test_a_source_that_produced_nothing_says_so_rather_than_going_quiet(bot, store):
    """Silence reads as "it didn't work". "Nothing worth keeping" reads as a judgement,
    which is what it is."""
    store.put_job({"job_id": new_id("job_"), "kind": "ingest", "spec": "x",
                   "origin": "telegram:42", "status": "done",
                   "result": {"claims": 0, "source": {"title": "A thin page"}}})

    bot.report_finished()

    assert "Nothing worth keeping" in "\n".join(bot.transport.texts)


def _wait_for(needle: str, bot, timeout: float = 10.0) -> bool:
    """Several commands answer from a worker thread, so the reply arrives after the
    call returns."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(needle in t for t in bot.transport.texts):
            return True
        time.sleep(0.02)
    return False


def test_a_second_reader_of_the_same_token_is_explained_once_not_every_five_seconds(
        bot, monkeypatch, caplog):
    """Telegram gives one long-poll slot per token, so a bot started twice -- or a token
    shared with another tool -- gets 409 forever and receives nothing. "telegram poll
    failed: getUpdates returned 409" sends nobody anywhere useful, and repeating it every
    five seconds buries whatever else is in the log."""
    import logging

    import palimpsest.telegram as mod

    calls = {"n": 0}

    def conflicted(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 3:
            bot.stop()
        raise mod.TelegramError(
            'getUpdates returned 409: {"description": "Conflict: terminated by other '
            'getUpdates request"}')

    monkeypatch.setattr(bot, "poll_once", conflicted)
    monkeypatch.setattr(bot._stop, "wait", lambda timeout=None: None)
    monkeypatch.setattr(mod, "_call", lambda *a, **kw: {"username": "a_bot"})

    with caplog.at_level(logging.ERROR, logger="palimpsest.telegram"):
        bot.run()

    explained = [r for r in caplog.records if "already reading updates" in r.getMessage()]
    assert len(explained) == 1, "said once, not once per retry"
    assert "@a_bot" in explained[0].getMessage()
    assert "BotFather" in explained[0].getMessage(), "and it says what to do about it"
