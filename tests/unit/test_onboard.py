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
