"""Connection strings, which are pure parsing and were nevertheless at zero coverage.

The stakes here are higher than "it is only string manipulation" suggests, because two of
the things this module decides fail *silently* when they are wrong.

**TLS.** psycopg2 defaults to `sslmode=prefer`, which falls back to an unencrypted
connection if the server allows one and says nothing about having done so. Against a
hosted database that is your notes crossing the internet in the clear, and the only
evidence is a packet capture nobody is taking.

**Pooler mode.** Supabase serves a session pooler and a transaction pooler from hostnames
that both contain "pooler", and they differ only by port. Transaction mode hands the
connection back after every statement, so prepared statements, `SET`, `LISTEN`, temp
tables and advisory locks all vanish — and code written against a direct connection
appears to work until it meets concurrency, then fails with `prepared statement "S_1"
already exists`, a message that says nothing about the cause.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

import pytest

from palimpsest.store.dsn import DIRECT_PORT, POOLER_PORT, is_local_host, normalise, parse


def _query(url: str) -> dict:
    return dict(parse_qsl(urlparse(url).query))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_a_plain_postgres_url_is_read_correctly():
    dsn = parse("postgresql://sk:secret@db.example.com:5432/palimpsest")

    assert dsn.host == "db.example.com"
    assert dsn.port == 5432
    assert dsn.database == "palimpsest"
    assert dsn.user == "sk"
    assert dsn.is_supabase is False
    assert dsn.is_pooler is False


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_both_spellings_of_the_scheme_are_accepted(scheme):
    """`postgres://` is what most tools emit and `postgresql://` is what the spec says.
    Accepting only one means a URL copied from a dashboard is rejected."""
    assert parse(f"{scheme}://u@h/d").host == "h"


def test_something_that_is_not_a_postgres_url_is_refused():
    with pytest.raises(ValueError, match="not a postgres URL"):
        parse("mysql://user@host/db")


def test_the_defaults_are_the_ones_postgres_itself_uses():
    dsn = parse("postgresql://host")

    assert dsn.port == DIRECT_PORT
    assert dsn.database == "postgres"
    assert dsn.user == "postgres"


def test_a_url_with_a_bare_slash_still_gets_a_database():
    """`postgresql://host/` is what you get from careless string building, and an empty
    database name is a connection error several seconds later rather than here."""
    assert parse("postgresql://host/").database == "postgres"


# ---------------------------------------------------------------------------
# which pooler, which is decided by the port
# ---------------------------------------------------------------------------


def test_the_transaction_pooler_is_identified_by_its_port():
    dsn = parse(
        f"postgresql://u@aws-0-ap-south-1.pooler.supabase.com:{POOLER_PORT}/postgres")

    assert dsn.is_pooler is True
    assert dsn.is_pgbouncer is True
    assert dsn.is_supabase is True


def test_the_session_pooler_is_not_a_transaction_pooler():
    """Both hostnames contain "pooler" and only the port separates them.

    Keying on the hostname would misclassify the session pooler, and `db migrate` would
    then refuse the very URL its own error message tells you to use — which is the kind
    of bug that makes somebody give up on a deployment.
    """
    dsn = parse(
        f"postgresql://u@aws-0-ap-south-1.pooler.supabase.com:{DIRECT_PORT}/postgres")

    assert dsn.is_pgbouncer is True, "it is still pgBouncer"
    assert dsn.is_pooler is False, "but it behaves like a session, not a transaction"


def test_a_direct_supabase_connection_is_neither():
    dsn = parse("postgresql://postgres@db.abcdefgh.supabase.co:5432/postgres")

    assert dsn.is_supabase is True
    assert dsn.is_pooler is False
    assert dsn.is_pgbouncer is False


# ---------------------------------------------------------------------------
# TLS, where the failure is silent
# ---------------------------------------------------------------------------


def test_a_hosted_database_gets_sslmode_require():
    """psycopg2 defaults to `prefer`, which silently accepts an unencrypted connection
    if the server offers one. Against a hosted database that is not acceptable."""
    assert _query(normalise("postgresql://u@db.example.com/p"))["sslmode"] == "require"


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "postgres", "db", "host.docker.internal",
    "pg.localhost",
])
def test_a_local_database_is_not_forced_onto_tls(host):
    """Most local Postgres installs ship without TLS at all, so requiring it here breaks
    development for a connection that cannot leave the machine anyway."""
    assert is_local_host(host)
    assert "sslmode" not in _query(normalise(f"postgresql://u@{host}/p"))


def test_an_explicit_sslmode_in_the_url_is_never_overridden():
    """Somebody who wrote `sslmode=verify-full` meant it, and somebody who wrote
    `disable` on a private network meant that too."""
    for mode in ("disable", "verify-full", "allow"):
        url = normalise(f"postgresql://u@db.example.com/p?sslmode={mode}")
        assert _query(url)["sslmode"] == mode


def test_the_environment_can_override_for_a_private_network(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_DB_SSLMODE", "disable")

    assert _query(normalise("postgresql://u@db.example.com/p"))["sslmode"] == "disable"


def test_the_override_does_not_beat_an_explicit_url(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_DB_SSLMODE", "disable")

    url = normalise("postgresql://u@db.example.com/p?sslmode=require")

    assert _query(url)["sslmode"] == "require", "the URL is more specific than the env"


def test_ssl_can_be_demanded_or_waived_by_the_caller():
    assert _query(normalise("postgresql://u@localhost/p",
                            require_ssl=True))["sslmode"] == "require"
    assert "sslmode" not in _query(normalise("postgresql://u@db.example.com/p",
                                             require_ssl=False))


# ---------------------------------------------------------------------------
# the parameters that stop a container hanging
# ---------------------------------------------------------------------------


def test_a_connect_timeout_is_always_set():
    """Without it a wrong host hangs the connection until the orchestrator's health
    check gives up, which turns a typo into a deployment that looks like an outage."""
    assert _query(normalise("postgresql://u@db.example.com/p"))["connect_timeout"] == "10"


def test_an_application_name_is_always_set():
    """So `pg_stat_activity` says which process is holding a connection. Debugging a
    pooler without it means guessing."""
    assert _query(normalise("postgresql://u@h/p"))["application_name"] == "palimpsest"


def test_a_caller_supplied_timeout_survives():
    url = normalise("postgresql://u@db.example.com/p?connect_timeout=2")

    assert _query(url)["connect_timeout"] == "2"


def test_normalising_is_idempotent():
    """It runs on every connection in some paths. Twice must equal once, or the query
    string grows a duplicate parameter each time."""
    once = normalise("postgresql://u@db.example.com/p")
    twice = normalise(once)

    assert _query(once) == _query(twice)
    assert twice.count("sslmode") == 1


def test_credentials_and_path_survive_normalisation():
    """The whole point is to add parameters, not to rewrite the connection."""
    url = normalise("postgresql://sk:p%40ss@db.example.com:6543/palimpsest")
    parsed = urlparse(url)

    assert parsed.username == "sk"
    # Still percent-encoded, and that is right: `urlparse` does not decode, and the
    # encoded form is what psycopg2 must receive. Decoding here would corrupt any
    # password containing `@`, `/` or `:` — which is most generated ones.
    assert parsed.password == "p%40ss"
    assert parsed.port == 6543
    assert parsed.path == "/palimpsest"


def test_a_non_postgres_url_is_returned_untouched():
    """`normalise` is called on whatever the setting holds, which may be SQLite."""
    assert normalise("sqlite:///notes.db") == "sqlite:///notes.db"


def test_a_password_with_reserved_characters_reaches_psycopg2_intact():
    """Generated passwords routinely contain `@`, `/` and `:`, which are exactly the
    characters that delimit a URL. Round-tripping through `urlparse`/`urlunparse` must
    not re-encode or decode them, or the connection fails with "password authentication
    failed" and nothing points at the URL handling."""
    from urllib.parse import unquote

    raw = "p@ss/w:rd"
    encoded = "p%40ss%2Fw%3Ard"
    url = normalise(f"postgresql://sk:{encoded}@db.example.com/p")

    assert f":{encoded}@" in url
    assert unquote(urlparse(url).password or "") == raw
