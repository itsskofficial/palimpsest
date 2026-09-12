"""Supabase configuration, at zero coverage, and it decides two things that fail badly.

**Which connection string.** Supabase hands you three, and the differences are not
cosmetic. The direct host is IPv6-only on the free tier, so a deployed container with
IPv4 simply cannot resolve it — the failure is a DNS error minutes into a deploy that
says nothing about IP versions. The transaction pooler drops session state between
statements, so migrations against it fail in ways that look like corruption.

**Whether secrets reach a log.** `as_dict` is what `palimpsest supabase status` prints
and what the setup wizard renders. A service-role key is a full-access credential — it
bypasses row-level security entirely — so the difference between redacting it and not is
the difference between a screenshot somebody can safely paste into an issue and one that
hands over their whole database.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import pytest

from palimpsest.store.supabase import SupabaseConfig, connection_url, detect


def _config(**overrides) -> SupabaseConfig:
    return SupabaseConfig(**{
        "api_url": "https://abcdefgh.supabase.co",
        "db_url": "postgresql://postgres.abcdefgh:hunter2@aws-0-ap-south-1."
                  "pooler.supabase.com:6543/postgres",
        "service_role_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret",
        "is_local": False,
        "project_ref": "abcdefgh",
        **overrides,
    })


# ---------------------------------------------------------------------------
# which of the three connection strings
# ---------------------------------------------------------------------------


def test_the_default_is_the_one_a_deployed_container_needs():
    """`service` rather than `direct`, because the direct host is IPv6-only on the free
    tier and a Fargate task with IPv4 cannot resolve it at all."""
    url = connection_url("abcdefgh", "pw")

    assert urlparse(url).port == 6543
    assert "pooler.supabase.com" in url


def test_the_session_pooler_is_offered_for_anything_session_scoped():
    """Migrations, `psql`, advisory locks. Run against the transaction pooler these fail
    in ways that look like database corruption rather than like the wrong URL."""
    url = connection_url("abcdefgh", "pw", purpose="session")

    assert urlparse(url).port == 5432
    assert "pooler.supabase.com" in url


def test_the_direct_host_is_available_when_the_network_has_ipv6():
    url = connection_url("abcdefgh", "pw", purpose="direct")

    assert urlparse(url).hostname == "db.abcdefgh.supabase.co"
    assert urlparse(url).port == 5432


def test_a_purpose_nobody_defined_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="service, session or direct"):
        connection_url("abcdefgh", "pw", purpose="readonly")


def test_the_region_is_part_of_the_pooler_host():
    """Pointing at the wrong region resolves and then refuses the credentials, which
    reads as a bad password."""
    url = connection_url("abcdefgh", "pw", region="eu-west-2")

    assert "aws-0-eu-west-2.pooler.supabase.com" in url


@pytest.mark.parametrize("password", [
    "p@ssword", "with/slash", "has?query", "has#hash", "colon:inside", "a@b/c?d#e",
])
def test_a_generated_password_survives_being_put_in_a_url(password):
    """Supabase-generated passwords routinely contain `@`, `/` and `?` — every one of
    which breaks a naively concatenated URL, and always as an authentication error that
    sends you looking at the password rather than at the string built from it."""
    url = connection_url("abcdefgh", password)
    parsed = urlparse(url)

    assert parsed.hostname == "aws-0-ap-south-1.pooler.supabase.com"
    assert parsed.port == 6543
    assert unquote(parsed.password or "") == password


def test_the_pooler_user_carries_the_project_reference():
    """`postgres.<ref>`, not `postgres`. Against a pooler the bare user authenticates as
    the wrong tenant and the error mentions neither."""
    url = connection_url("abcdefgh", "pw")

    assert urlparse(url).username == "postgres.abcdefgh"


# ---------------------------------------------------------------------------
# not leaking the key
# ---------------------------------------------------------------------------


def test_status_output_redacts_the_service_role_key_by_default():
    """It bypasses row-level security entirely. `palimpsest supabase status` is exactly
    the output somebody pastes into an issue."""
    shown = _config().as_dict()

    assert "secret" not in shown["service_role_key"]
    assert shown["service_role_key"] != _config().service_role_key


def test_status_output_redacts_the_password_in_the_database_url():
    shown = _config().as_dict()

    assert "hunter2" not in shown["db_url"]


def test_the_real_values_are_available_when_explicitly_asked_for():
    """`supabase env` has to emit something usable, so redaction is a default rather
    than a wall."""
    shown = _config().as_dict(reveal=True)

    assert shown["service_role_key"] == _config().service_role_key
    assert "hunter2" in shown["db_url"]


def test_a_config_with_no_key_does_not_render_the_word_none():
    shown = _config(service_role_key=None).as_dict()

    assert "None" not in str(shown["service_role_key"])


# ---------------------------------------------------------------------------
# the environment a process needs
# ---------------------------------------------------------------------------


def test_the_environment_names_the_variables_the_app_actually_reads():
    env = _config().env()

    assert env["PALIMPSEST_DATABASE_URL"] == _config().db_url
    assert env["SUPABASE_URL"] == _config().api_url
    assert env["SUPABASE_SERVICE_ROLE_KEY"] == _config().service_role_key


def test_without_a_key_the_variable_is_omitted_rather_than_set_empty():
    """An empty string is a value. Exported, it looks configured and fails on use."""
    env = _config(service_role_key=None).env()

    assert "SUPABASE_SERVICE_ROLE_KEY" not in env


def test_the_storage_url_is_the_api_url_plus_the_storage_path():
    assert _config().storage_url == "https://abcdefgh.supabase.co/storage/v1"


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ambient_supabase(monkeypatch):
    for name in ("SUPABASE_URL", "SUPABASE_DB_URL", "SUPABASE_PROJECT_REF",
                 "SUPABASE_DB_PASSWORD", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(name, raising=False)
    # Never probe a local stack from a test: on a developer's machine that is a real
    # socket connection whose answer changes what the test asserts.
    monkeypatch.setattr("palimpsest.store.supabase._local_is_running", lambda **kw: False)


def test_explicit_urls_win_over_everything_else(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://explicit.supabase.co")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@explicit/postgres")
    monkeypatch.setenv("SUPABASE_PROJECT_REF", "ignored")

    config = detect()

    assert config is not None
    assert config.api_url == "https://explicit.supabase.co"
    assert "explicit" in config.db_url


def test_a_project_reference_and_password_assemble_the_url_for_you(monkeypatch):
    monkeypatch.setenv("SUPABASE_PROJECT_REF", "abcdefgh")
    monkeypatch.setenv("SUPABASE_DB_PASSWORD", "p@ss/word")

    config = detect()

    assert config is not None
    assert config.project_ref == "abcdefgh"
    assert config.is_local is False
    # Assembled through `connection_url`, so the password encoding holds here too.
    assert unquote(urlparse(config.db_url).password or "") == "p@ss/word"


def test_nothing_configured_is_none_rather_than_an_exception():
    """Supabase is optional. Most installs are one SQLite file and never touch this."""
    assert detect() is None


def test_require_turns_the_absence_into_an_explanation():
    with pytest.raises(RuntimeError) as caught:
        detect(require=True)

    message = str(caught.value)
    assert "SUPABASE" in message
