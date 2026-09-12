"""`palimpsest demo` — offline properties, chiefly that it cannot touch anything of yours.

The live behaviour is in `tests/demo/test_promises.py`, which needs a key. What is here
needs none, and most of it is about containment.

That emphasis is deliberate. A demo inherits the config file of whoever runs it, and on a
machine that has been set up that file holds a real Telegram token, real tracing keys and
a real Notion token. The first working version of this command started the user's actual
bot and shipped their trial run to their own Langfuse project. Neither is what anybody
means by "try it out", and neither announced itself.
"""

from __future__ import annotations

import dataclasses

import pytest

from palimpsest import demo
from palimpsest.config import Settings
from palimpsest.workspace.convert import to_blocks, to_markdown

# ---------------------------------------------------------------------------
# containment
# ---------------------------------------------------------------------------


@pytest.fixture()
def configured(monkeypatch, tmp_path):
    """A machine that has been fully set up, which is the dangerous case."""
    monkeypatch.setattr(
        "palimpsest.config.Settings.load",
        classmethod(lambda cls, **kw: cls(
            notion_token="ntn_real_token",
            notion_root_pages=("3c628ce9750c81619ad8c23d03fa3441",),
            telegram_token="8000:REAL-BOT-TOKEN",
            telegram_allowed_chats=(12345,),
            anthropic_api_key="sk-ant-real",
            database_url="sqlite:///somewhere/real.db",
            artifact_url="file://./real-archive",
            journal=True,
            **kw)))
    return tmp_path / "vault"


def test_a_demo_cannot_reach_your_telegram_bot(configured):
    settings = demo.settings_for(configured)

    assert settings.telegram_token is None
    assert settings.telegram_allowed_chats == ()


def test_a_demo_cannot_reach_your_notion(configured):
    settings = demo.settings_for(configured)

    assert settings.notion_token is None
    assert settings.notion_root_pages == ()
    assert settings.backend == "markdown"
    assert settings.has_workspace, "it can still write — to the vault"


def test_a_demo_keeps_its_database_and_archive_inside_the_vault(configured):
    settings = demo.settings_for(configured)

    assert str(configured) in settings.database_url
    assert str(configured) in settings.artifact_url
    assert "real.db" not in settings.database_url


def test_a_demo_keeps_the_model_it_was_given(configured):
    """Containment is about side effects, not about crippling it. The model is the one
    thing a demo genuinely needs from your configuration."""
    assert demo.settings_for(configured).anthropic_api_key == "sk-ant-real"


def test_a_demo_writes_no_journal(configured):
    """There is no second workspace to write a ledger into, and the Activity tab reads
    the local store regardless."""
    assert demo.settings_for(configured).journal is False


def test_the_demo_settings_are_valid(configured):
    demo.settings_for(configured).validate()


def test_a_demo_applies_but_never_resolves_a_contradiction(configured):
    """`full`, not `everything`. A sandbox should show the product working, and the one
    thing it must not show is the safety property being switched off."""
    settings = demo.settings_for(configured)

    assert settings.apply is True
    assert settings.autonomy == "full"
    assert settings.may_auto_apply("low") and settings.may_auto_apply("medium")
    assert settings.may_auto_apply("high") is False


# ---------------------------------------------------------------------------
# the vault
# ---------------------------------------------------------------------------


def test_installing_lays_down_the_sample_vault(tmp_path):
    vault = demo.install(tmp_path / "v")

    pages = sorted(p.name for p in vault.glob("*.md"))
    assert len(pages) >= 7
    assert "gradient-clipping.md" in pages
    assert (vault / ".palimpsest" / "demo.json").exists()


def test_installing_twice_keeps_what_you_typed_into_it(tmp_path):
    """Somebody who ran the demo, tried a few things and came back tomorrow should find
    their session rather than a reset one."""
    vault = demo.install(tmp_path / "v")
    (vault / "my-own-note.md").write_text("# Mine\n\nTyped by hand.\n", encoding="utf-8")

    demo.install(tmp_path / "v")
    assert (vault / "my-own-note.md").exists()

    demo.install(tmp_path / "v", force=True)
    assert not (vault / "my-own-note.md").exists(), "--reset means reset"


def test_every_sample_page_parses_and_round_trips():
    """The vault ships as source. If a page does not survive a round trip, the first
    thing the demo does is rewrite the file it just showed you."""
    for path in demo.vault_source().glob("*.md"):
        body = path.read_text(encoding="utf-8").split("---\n\n", 1)[-1]
        assert to_markdown(to_blocks(body)) == body.strip(), path.name


def test_the_suggestions_are_well_formed():
    for prompt in demo.PROMPTS:
        assert prompt["label"] and prompt["text"] and prompt["why"]
        assert isinstance(prompt["expect"], tuple)


# ---------------------------------------------------------------------------
# warnings that tell the truth about the backend in use
# ---------------------------------------------------------------------------


def test_a_vault_is_not_warned_about_a_missing_notion_token():
    """It said "nothing can be mirrored or applied" immediately after mirroring eight
    pages. A warning that is visibly false teaches the reader to skip the true ones."""
    problems = " ".join(Settings(backend="markdown", vault_path="/tmp/v").problems())

    assert "NOTION_TOKEN" not in problems


def test_a_vault_with_nowhere_to_live_is_warned():
    assert any("PALIMPSEST_VAULT" in p
               for p in Settings(backend="markdown").problems())


def test_the_write_posture_names_where_writes_actually_go():
    vault = Settings(backend="markdown", vault_path="/tmp/v", apply=True, autonomy="full")
    notion = Settings(backend="notion", notion_token="ntn_x", apply=True, autonomy="full")

    assert any("your vault" in p for p in vault.posture())
    assert any("to Notion" in p for p in notion.posture())


def test_the_write_posture_is_not_counted_as_a_problem():
    """`palimpsest status` is documented as a health check that exits non-zero when it
    finds a problem. This line fires whenever writes are on at all -- the configuration
    people arrive at on purpose -- so counting it meant a correctly set-up install
    failed its own health check forever, which is how a health check stops being read."""
    writing = Settings(notion_token="ntn_x", anthropic_api_key="sk-x",
                       apply=True, autonomy="full")

    assert writing.posture(), "it still has to be said"
    assert not any("autonomy=full" in p for p in writing.problems())


def test_propose_only_has_no_posture_to_declare():
    """Nothing is written, so there is nothing to warn anybody about."""
    assert Settings(notion_token="ntn_x", apply=False).posture() == []
    assert Settings(notion_token="ntn_x", apply=True, autonomy="none").posture() == []


def test_a_local_demo_is_not_warned_about_container_filesystems(configured):
    """`PALIMPSEST_ENV=demo` used to trip the "a container filesystem does not survive a
    redeploy" warning, which is not a thing that happens to somebody on their laptop."""
    settings = demo.settings_for(configured)

    assert not any("redeploy" in p for p in settings.problems())
    assert any("redeploy" in p for p in
               dataclasses.replace(settings, environment="production").problems())


# ---------------------------------------------------------------------------
# tracing
# ---------------------------------------------------------------------------


def test_tracing_has_an_off_switch(monkeypatch):
    """"Unset the keys" is not always available to the caller. The demo inherits a config
    file it does not own and must be able to say no."""
    import palimpsest.trace as trace

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-real")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-real")
    monkeypatch.setenv("PALIMPSEST_TRACE", "0")
    monkeypatch.setattr(trace, "_configured", False)
    monkeypatch.setattr(trace, "_client", None)

    assert trace.configure() is False
    assert trace._client is None


# ---------------------------------------------------------------------------
# the ledger renders as a table
# ---------------------------------------------------------------------------


def test_consecutive_table_rows_are_not_split_by_blank_lines():
    """A vault has no databases, so the activity ledger is a markdown table and every row
    arrives as its own paragraph block. Spaced out the way paragraphs normally are, those
    rows stop being a table and render as twelve pipe-delimited sentences."""
    rows = ["| Change | When |", "| --- | --- |", "| Added: a claim | today |"]
    blocks = [{"type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": row}}]}} for row in rows]

    assert to_markdown(blocks) == "\n".join(rows)


def test_ordinary_paragraphs_still_get_their_blank_line():
    blocks = [{"type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text}}]}}
        for text in ("First thought.", "Second thought.")]

    assert to_markdown(blocks) == "First thought.\n\nSecond thought."


def test_a_demo_never_shows_the_setup_wizard(configured):
    """The first screen of the zero-setup path was a setup form.

    `is_configured` requires a Notion token and a paired Telegram chat, and a demo
    deliberately has neither — so the UI decided it was a fresh install and opened on
    onboarding. Somebody who ran one command to avoid configuring anything was shown a
    form asking them to configure things.
    """
    from palimpsest.onboard import is_configured

    assert is_configured(demo.settings_for(configured))


def test_an_actually_unconfigured_machine_still_gets_the_wizard():
    """The other direction, so the fix above is not "never show the wizard"."""
    from palimpsest.onboard import is_configured

    assert not is_configured(Settings())
    assert not is_configured(Settings(notion_token="ntn_x"))       # nothing to think with
    assert not is_configured(Settings(anthropic_api_key="sk-x"))   # nowhere to write


def test_a_reset_that_cannot_delete_says_what_to_do(tmp_path, monkeypatch):
    """Windows will not unlink a file another process has open, and that process is
    usually a demo the user forgot they left running. A traceback thirty seconds into
    somebody's first look at the project is a bad trade for a case with an obvious
    instruction."""
    vault = demo.install(tmp_path / "v")

    def locked(*_a, **_k):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr("shutil.rmtree", locked)

    with pytest.raises(RuntimeError, match="--dir"):
        demo.install(vault, force=True)
