"""First-run setup: the config file it writes, and the auto-load that makes it stick.

The interactive prompts and the live API checks are exercised by hand; what is pinned
here is the machinery a stranger's whole experience rests on — that answers are written
to a stable place, read back automatically on the next start, and never shadow a real
environment variable a deployment sets on purpose.
"""

from __future__ import annotations

import pytest

from palimpsest import onboard
from palimpsest.config import Settings, config_path, load_env_file


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A temp config file and a clean environment, so nothing here touches the machine.

    The autouse hermetic fixture stubs `load_env_file`; this test suite is specifically
    about that function, so it restores the real one for these tests only.
    """
    import palimpsest.config as cfg

    # conftest's autouse fixture stubs this to a no-op; restore the real one, since this
    # file is specifically about it.
    monkeypatch.setattr(cfg, "load_env_file", _real_load)
    cfgfile = tmp_path / "config.env"
    monkeypatch.setenv("PALIMPSEST_CONFIG", str(cfgfile))
    for key in ("NOTION_TOKEN", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_ALLOWED_CHATS", "PALIMPSEST_NOTION_ROOTS"):
        monkeypatch.delenv(key, raising=False)
    return cfgfile


# The real function, captured before conftest stubs it.
_real_load = load_env_file


def test_config_path_is_stable_and_not_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("PALIMPSEST_CONFIG", str(tmp_path / "x.env"))
    assert config_path() == tmp_path / "x.env"
    # Without an override it is a per-user location, never './config.env'.
    monkeypatch.delenv("PALIMPSEST_CONFIG", raising=False)
    assert config_path().name == "config.env"
    assert config_path().is_absolute()


def test_the_wizard_writes_a_file_the_loader_reads_back(isolated, monkeypatch):
    onboard._write({
        "ANTHROPIC_API_KEY": "sk-ant-x", "NOTION_TOKEN": "ntn_x",
        "PALIMPSEST_NOTION_ROOTS": "root123", "TELEGRAM_BOT_TOKEN": "1:AAA",
        "TELEGRAM_ALLOWED_CHATS": "42",
    })
    assert isolated.is_file()

    # A fresh process (fresh environment) loads it automatically.
    for key in ("NOTION_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    _real_load()
    settings = Settings(
        notion_token=__import__("os").environ.get("NOTION_TOKEN"),
        anthropic_api_key=__import__("os").environ.get("ANTHROPIC_API_KEY"),
    )
    assert settings.has_notion and settings.has_model


def test_a_real_environment_variable_wins_over_the_file(isolated, monkeypatch):
    onboard._write({"NOTION_TOKEN": "ntn_from_file"})
    monkeypatch.setenv("NOTION_TOKEN", "ntn_from_env")
    _real_load()   # must not overwrite the real one
    assert __import__("os").environ["NOTION_TOKEN"] == "ntn_from_env"


def test_setup_writes_safe_defaults(isolated):
    """A first setup must leave writes off and autonomy none — the product's whole
    trust story is that it does not edit your notes before you have seen it work."""
    onboard._write({"NOTION_TOKEN": "ntn_x"})
    body = isolated.read_text()
    assert "PALIMPSEST_APPLY=0" in body
    assert "PALIMPSEST_AUTONOMY=none" in body


def test_rewriting_preserves_keys_not_asked_about(isolated):
    onboard._write({"NOTION_TOKEN": "ntn_x", "GROQ_API_KEY": "g_secret"})
    onboard._write({"NOTION_TOKEN": "ntn_y"})   # a later run that only changed Notion
    body = isolated.read_text()
    assert "ntn_y" in body
    assert "g_secret" in body                    # not clobbered


def test_is_configured_needs_all_four_essentials():
    base = dict(notion_token="n", anthropic_api_key="a", telegram_token="t",
                telegram_allowed_chats=(42,))
    assert onboard.is_configured(Settings(**base)) is True
    for drop in ("notion_token", "anthropic_api_key", "telegram_token"):
        partial = {**base, drop: None}
        assert onboard.is_configured(Settings(**partial)) is False
    assert onboard.is_configured(Settings(**{**base, "telegram_allowed_chats": ()})) is False


def test_a_malformed_config_file_is_ignored_not_fatal(isolated):
    isolated.write_text("this is not = valid\n\n# comment\nNOTION_TOKEN=ntn_ok\n")
    n = _real_load()
    assert __import__("os").environ.get("NOTION_TOKEN") == "ntn_ok"
    assert n >= 1


# ---------------------------------------------------------------------------
# the wizard, driven the way a person drives it
# ---------------------------------------------------------------------------
#
# This is the first thing anybody does and it was almost entirely untested, which is the
# wrong way round: a wizard that crashes on step three loses a user permanently, and
# every one of its failure paths happens on somebody else's machine where nobody is
# watching. The tests below drive it through `input`, so what is exercised is the thing
# a person actually meets rather than the functions underneath it.


class Answers(list):
    """A scripted stdin. `append` what the person types; `asked` is what they were asked.

    `input` raising EOFError when the script runs out is the point, not an accident: it
    is exactly what a real terminal does when stdin closes, and the wizard has a branch
    for it.
    """

    def __init__(self):
        super().__init__()
        self.asked: list[str] = []


@pytest.fixture()
def answers(monkeypatch):
    given = Answers()

    def fake_input(prompt=""):
        given.asked.append(prompt)
        if not given:
            raise EOFError
        return given.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("getpass.getpass", fake_input, raising=False)
    return given


class FakeNotion:
    """A Notion that answers, with a page already shared with the integration."""

    def __init__(self, token, **kw):
        self.token = token

    def whoami(self):
        return {"name": "my integration"}

    def search_pages(self, query=""):
        return iter([{"id": "pg_existing", "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Study"}]}}}])

    #: `_create_root` does not go through the client -- it builds a raw request, because
    #: creating a *workspace-level* page is not something `NotionClient` exposes. So the
    #: tests below stub that function rather than a method here.
    token = "ntn_fake"
    version = "2026-03-11"


def test_a_prompt_with_no_answer_left_cancels_rather_than_spinning(answers, monkeypatch):
    """`input` raises EOFError when stdin runs out — a piped setup, a non-interactive
    shell, a double Ctrl-D. A required prompt that loops on that hangs a terminal
    forever, which is worse than any error message."""
    from palimpsest.onboard import _ask

    with pytest.raises(KeyboardInterrupt):
        _ask("NOTION_TOKEN")


def test_a_prompt_with_a_default_takes_it_when_stdin_is_empty(answers):
    from palimpsest.onboard import _ask

    assert _ask("model", default="claude") == "claude"
    assert _ask("anything", allow_blank=True) == ""


def test_a_blank_answer_takes_the_default_rather_than_clearing_it(answers):
    """Re-running the wizard shows existing values as defaults. Pressing Enter must keep
    the key you already have, not wipe it."""
    from palimpsest.onboard import _ask

    answers.append("")
    assert _ask("NOTION_TOKEN", default="ntn_existing") == "ntn_existing"


def test_a_required_prompt_asks_again_until_it_gets_something(answers):
    from palimpsest.onboard import _ask

    answers.extend(["", "  ", "ntn_real"])
    assert _ask("NOTION_TOKEN") == "ntn_real"
    assert len(answers.asked) == 3


def test_the_notion_step_retries_a_rejected_token(answers, monkeypatch):
    """A mistyped token is the most likely thing to happen here, and the wizard has to
    survive it — an exception would drop somebody back to a shell having lost the three
    answers they already gave."""
    from palimpsest.notion.client import NotionError
    from palimpsest.onboard import _step_notion

    attempts = []

    class Fussy(FakeNotion):
        def whoami(self):
            attempts.append(self.token)
            if len(attempts) == 1:
                raise NotionError(401, "unauthorized", "API token is invalid")
            return {"name": "my integration"}

    monkeypatch.setattr("palimpsest.notion.client.NotionClient", Fussy)
    answers.extend(["ntn_wrong", "ntn_right", "1"])

    values: dict = {}
    _step_notion(values)

    assert attempts == ["ntn_wrong", "ntn_right"]
    assert values["NOTION_TOKEN"] == "ntn_right"


def test_the_notion_step_survives_the_network_being_down(answers, monkeypatch):
    from palimpsest.onboard import _step_notion

    attempts = []

    class Offline(FakeNotion):
        def whoami(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("getaddrinfo failed")
            return {"name": "my integration"}

    monkeypatch.setattr("palimpsest.notion.client.NotionClient", Offline)
    answers.extend(["ntn_a", "ntn_b", "1"])

    values: dict = {}
    _step_notion(values)

    assert values["NOTION_TOKEN"] == "ntn_b"


def test_choosing_an_existing_page_by_number_uses_that_page(answers):
    from palimpsest.onboard import _choose_root

    answers.append("1")
    values: dict = {}
    _choose_root(FakeNotion("t"), values)

    assert values["PALIMPSEST_NOTION_ROOTS"] == "pg_existing"


@pytest.fixture()
def creates_root(monkeypatch):
    monkeypatch.setattr("palimpsest.onboard._create_root",
                        lambda client, title: ("pg_created",
                                               "https://notion.so/pg_created"))


def test_pressing_enter_creates_a_fresh_root_page(answers, creates_root):
    """The default has to be "make me one". Asking somebody to pick a page before they
    know what the tool does to a page is asking them to guess."""
    from palimpsest.onboard import _choose_root

    answers.append("")
    values: dict = {}
    _choose_root(FakeNotion("t"), values)

    assert values["PALIMPSEST_NOTION_ROOTS"] == "pg_created"


def _refuses(client, title):
    raise RuntimeError("insert content capability is not enabled")


def test_a_root_that_cannot_be_created_falls_back_to_pasting_an_id(answers, monkeypatch):
    """Creating a top-level page needs a capability the integration may not have been
    given. Dead-ending there strands somebody two steps from finishing."""
    from palimpsest.onboard import _choose_root

    class CannotCreate(FakeNotion):
        def search_pages(self, query=""):
            return iter(())

    monkeypatch.setattr("palimpsest.onboard._create_root", _refuses)
    answers.append("8f46-ccc8-8e12")
    values: dict = {}
    _choose_root(CannotCreate("t"), values)

    assert values["PALIMPSEST_NOTION_ROOTS"] == "8f46ccc88e12", "dashes are stripped"


def test_an_out_of_range_choice_creates_a_page_instead_of_crashing(answers,
                                                                  creates_root):
    from palimpsest.onboard import _choose_root

    answers.append("99")
    values: dict = {}
    _choose_root(FakeNotion("t"), values)

    assert values["PALIMPSEST_NOTION_ROOTS"] == "pg_created"


# ---------------------------------------------------------------------------
# pairing a phone
# ---------------------------------------------------------------------------


def test_pairing_takes_the_chat_that_messages_after_the_backlog_is_cleared(monkeypatch):
    """The backlog matters. A bot token that has been sitting around has old messages
    against it, and pairing with the first one in the queue pairs whoever last poked the
    bot — which, for a token anybody found, is not you."""
    import palimpsest.telegram as tg
    from palimpsest.onboard import _pair_chat

    calls = []

    def fake(token, method, params=None, timeout=None):
        calls.append(params or {})
        if len(calls) == 1:                       # the backlog sweep
            return [{"update_id": 500,
                     "message": {"chat": {"id": 111, "type": "private"}}}]
        return [{"update_id": 501,
                 "message": {"chat": {"id": 222, "type": "private",
                                      "first_name": "Sarthak"}}}]

    monkeypatch.setattr(tg, "_call", fake)
    values: dict = {}
    _pair_chat("t", "bot", values)

    assert values["TELEGRAM_ALLOWED_CHATS"] == "222", "the old message must not pair"
    assert calls[1]["offset"] == 501


def test_pairing_ignores_a_group_and_waits_for_a_private_chat(monkeypatch):
    """A bot added to a group sees group messages. Pairing with one would allowlist the
    whole group, so everyone in it could write to your Notion."""
    import palimpsest.telegram as tg
    from palimpsest.onboard import _pair_chat

    batches = [
        [],
        [{"update_id": 2, "message": {"chat": {"id": -100999, "type": "supergroup"}}}],
        [{"update_id": 3, "message": {"chat": {"id": 42, "type": "private"}}}],
    ]

    def fake(token, method, params=None, timeout=None):
        return batches.pop(0) if batches else []

    monkeypatch.setattr(tg, "_call", fake)
    values: dict = {}
    _pair_chat("t", "bot", values)

    assert values["TELEGRAM_ALLOWED_CHATS"] == "42"


def test_pairing_can_be_skipped_without_taking_the_wizard_down(monkeypatch):
    import palimpsest.telegram as tg
    from palimpsest.onboard import _pair_chat

    def impatient(token, method, params=None, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(tg, "_call", impatient)
    values: dict = {}
    _pair_chat("t", "bot", values)          # must return, not raise

    assert "TELEGRAM_ALLOWED_CHATS" not in values


# ---------------------------------------------------------------------------
# what gets written
# ---------------------------------------------------------------------------


def test_the_config_file_is_not_world_readable(isolated):
    """It holds bearer tokens. On POSIX that is a mode; on Windows the call is a no-op
    and the assertion is skipped rather than pretended."""
    import os
    import stat

    from palimpsest.onboard import _write

    _write({"NOTION_TOKEN": "ntn_secret"})
    path = config_path()

    assert path.is_file()
    if os.name != "nt":
        assert not stat.S_IMODE(path.stat().st_mode) & 0o077


def test_writing_twice_does_not_duplicate_a_key(isolated):
    from palimpsest.onboard import _write

    _write({"NOTION_TOKEN": "first"})
    _write({"NOTION_TOKEN": "second"})

    body = config_path().read_text(encoding="utf-8")
    assert body.count("NOTION_TOKEN=") == 1
    assert "second" in body and "first" not in body


def test_a_blank_answer_never_erases_a_saved_key(isolated):
    """Re-running the wizard and pressing Enter through it must be safe."""
    from palimpsest.onboard import _write

    _write({"NOTION_TOKEN": "ntn_keep", "ANTHROPIC_API_KEY": "sk-ant-keep"})
    _write({"NOTION_TOKEN": "", "TELEGRAM_BOT_TOKEN": "8000:new"})

    body = config_path().read_text(encoding="utf-8")
    assert "ntn_keep" in body
    assert "sk-ant-keep" in body
    assert "8000:new" in body


def test_the_written_defaults_are_the_safe_ones(isolated):
    """A wizard that finishes with writing switched on is a wizard that can damage a
    workspace before anybody has seen it propose anything."""
    from palimpsest.onboard import _write

    _write({"NOTION_TOKEN": "t"})
    body = config_path().read_text(encoding="utf-8")

    assert "PALIMPSEST_APPLY=0" in body
    assert "PALIMPSEST_AUTONOMY=none" in body


def test_existing_values_seed_the_wizard_so_it_only_fills_gaps(monkeypatch):
    from palimpsest.config import Settings
    from palimpsest.onboard import _existing_values

    monkeypatch.setenv("NOTION_TOKEN", "ntn_already")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-already")
    settings = Settings(notion_root_pages=("pg_root",), telegram_allowed_chats=(7, 8))

    values = _existing_values(settings)

    assert values["NOTION_TOKEN"] == "ntn_already"
    assert values["PALIMPSEST_NOTION_ROOTS"] == "pg_root"
    assert values["TELEGRAM_ALLOWED_CHATS"] == "7,8"
