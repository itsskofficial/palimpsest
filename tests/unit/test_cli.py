"""The command line, which had no tests at all.

That gap was backwards. `cli.py` is the largest module in the project and the first thing
a new user touches, and it was the only one at zero coverage while the deep pipeline sat
comfortably above eighty. A wizard that crashes on line three loses a user permanently; a
classifier that picks `extends` over `refines` does not.

It is also the module where a mistake is *easiest* to make and hardest to notice, because
most of it is glue that only executes when someone types the right words in the right
order. Adding `eval leaderboard` put an unterminated string literal into this file, and
nothing caught it — not the type checker, not the linter, not four hundred tests — until
the command was run by hand.

So most of what follows is coverage of the shape rather than the behaviour: every
subcommand parses, every one dispatches somewhere real, the offline ones run. The
behaviour they wrap is tested where it lives.
"""

from __future__ import annotations

import json

import pytest

from palimpsest import cli


@pytest.fixture()
def db(tmp_path):
    return f"sqlite:///{tmp_path / 'cli.db'}"


@pytest.fixture(autouse=True)
def _no_wizard(monkeypatch):
    """`serve` offers the setup wizard at a terminal. Tests are not a terminal, but say
    so explicitly rather than depending on how pytest captures stdin."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


def _subcommands():
    import argparse

    parser = cli.build_parser()
    actions = [a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction)]
    return actions[0].choices


def test_every_subcommand_dispatches_to_a_real_function():
    """A subparser with no `func` fails with `AttributeError: 'Namespace' object has no
    attribute 'func'` at runtime, which is an unhelpful way to learn about a typo."""
    for name, parser in _subcommands().items():
        func = parser.get_default("func")
        assert callable(func), f"{name} has no command function"
        assert func.__name__.startswith("cmd_"), f"{name} -> {func.__name__}"


def test_the_whole_parser_builds_and_every_help_renders():
    """Cheap, and it executes every `add_argument` in the file — which is where a broken
    string literal or a duplicated flag actually lives."""
    for name, parser in _subcommands().items():
        text = parser.format_help()
        # An alias shares its parser, so `prog` carries the canonical name, not the
        # alias — `organize` renders help whose prog says `organise`.
        assert text.startswith("usage: palimpsest "), name
        assert parser.get_default("func"), name


def test_version_is_the_package_version(capsys):
    from palimpsest._version import __version__

    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_is_an_error_rather_than_a_traceback():
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2


@pytest.mark.parametrize("argv", [
    ["sync", "--full"],
    ["ingest", "some text", "--kind", "text"],
    ["patches", "--status", "proposed"],
    ["patch", "pch_1"],
    ["apply", "pch_1", "--reviewer", "sk"],
    ["undo", "pch_1"],
    ["sweep", "duplicates"],
    ["organise"],
    ["eval", "retrieval"],
    ["eval", "leaderboard", "--models", "a/b"],
    ["agent", "what did I write about attention?"],
    ["serve", "--port", "9000"],
    ["demo", "--no-serve"],
    ["status"],
    ["history", "pg_1"],
    ["provenance", "bk_1"],
    ["db", "check"],
])
def test_representative_invocations_parse(argv):
    args = cli.build_parser().parse_args(argv)
    assert callable(args.func)


def test_eval_rejects_a_suite_that_does_not_exist():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["eval", "vibes"])


# ---------------------------------------------------------------------------
# commands that run without a network, a key, or a workspace
# ---------------------------------------------------------------------------


def test_status_describes_an_unconfigured_machine(db, capsys, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_DATABASE_URL", db)
    assert cli.main(["status"]) in (0, 1)

    out = capsys.readouterr().out
    assert "model" in out.lower()
    assert "notion" in out.lower()


def test_patches_on_an_empty_store_says_so(db, capsys):
    assert cli.main(["patches", "--db", db]) == 0
    assert "no patches" in capsys.readouterr().out


def test_patch_that_does_not_exist_exits_with_a_message(db):
    with pytest.raises(SystemExit) as caught:
        cli.main(["patch", "pch_nothing", "--db", db])
    assert "pch_nothing" in str(caught.value)


def test_db_check_runs_against_a_fresh_store(db, capsys):
    # `db` names its argument `--url`, not `--db`: it is the one command whose subject
    # *is* the database rather than one that merely uses it.
    assert cli.main(["db", "check", "--url", db]) == 0
    assert capsys.readouterr().out.strip()


def test_sweep_duplicates_needs_no_model(db, capsys):
    """The day-one property, exercised through the command rather than the library."""
    from palimpsest.store import open_store

    store = open_store(db)
    text = "Attention divides the logits by the square root of the key dimension."
    store.put_pages([{"page_id": "p1", "title": "A", "last_edited": "x"},
                     {"page_id": "p2", "title": "B", "last_edited": "y"}])
    store.put_blocks([
        {"block_id": "b1", "page_id": "p1", "type": "paragraph", "text": text,
         "position": 0},
        {"block_id": "b2", "page_id": "p2", "type": "paragraph", "text": text,
         "position": 0}])
    store.close()

    assert cli.main(["sweep", "duplicates", "--db", db]) == 0
    assert "1" in capsys.readouterr().out


def test_out_writes_the_result_as_json(db, tmp_path):
    target = tmp_path / "nested" / "result.json"

    cli.main(["patches", "--db", db])           # no --out: nothing written
    assert not target.exists()

    cli.main(["sweep", "duplicates", "--db", db, "--out", str(target)])
    assert json.loads(target.read_text(encoding="utf-8")) is not None


# ---------------------------------------------------------------------------
# the safety-shaped parts of the surface
# ---------------------------------------------------------------------------


def test_apply_demands_a_reviewer(db):
    """An applied change records who approved it. Without that the ledger says a machine
    did it, which is exactly the question a ledger exists to answer."""
    from palimpsest.store import open_store
    from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

    store = open_store(db)
    patch = Patch(patch_id=new_id("pch_"), source_id="s", operations=[
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", relation=Relation.NEW,
                  payload={"text": "x"})])
    store.put_patch(patch)
    store.close()

    with pytest.raises(SystemExit) as caught:
        cli.main(["apply", patch.patch_id, "--db", db])
    assert "reviewer" in str(caught.value)


def test_apply_refuses_a_patch_carrying_a_contradiction(db):
    """The command-line half of the invariant. `--reviewer` is supplied, so this is the
    contradiction being refused rather than a missing argument."""
    from palimpsest.store import open_store
    from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

    store = open_store(db)
    patch = Patch(patch_id=new_id("pch_"), source_id="s", operations=[
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1",
                  relation=Relation.CONTRADICTS, payload={"text": "x"})])
    store.put_patch(patch)
    store.close()

    with pytest.raises(SystemExit) as caught:
        cli.main(["apply", patch.patch_id, "--db", db, "--reviewer", "sk"])
    assert "contradiction" in str(caught.value).lower()


def test_ingest_is_spelled_differently_from_apply():
    """Reading and writing must not be one flag apart.

    The command surface follows the safety model rather than the module layout: you type
    a different *command* to change your notes than to look at them. An `--apply` flag on
    `ingest` would undo that, so its absence is asserted rather than assumed.
    """
    ingest = _subcommands()["ingest"]
    flags = {s for a in ingest._actions for s in a.option_strings}

    assert "--apply" not in flags
    assert "--reviewer" not in flags
    assert "--reviewer" in {s for a in _subcommands()["apply"]._actions
                            for s in a.option_strings}


def test_demo_sets_up_a_vault_without_serving(tmp_path, capsys):
    """`--no-serve` exists so this is testable at all, and so CI can check the vault
    installs and mirrors on a machine with no browser."""
    assert cli.main(["demo", "--dir", str(tmp_path / "v"), "--no-serve"]) == 0

    out = capsys.readouterr().out
    assert "demo vault" in out
    assert (tmp_path / "v" / "gradient-clipping.md").exists()
    assert "pages" in out


def test_demo_does_not_touch_the_configured_database(tmp_path, monkeypatch, capsys):
    """A demo keeps its own store inside the vault. Writing the sample pages into
    somebody's real mirror would be a mess to unpick and would corrupt their evals."""
    real = tmp_path / "real.db"
    monkeypatch.setenv("PALIMPSEST_DATABASE_URL", f"sqlite:///{real}")

    cli.main(["demo", "--dir", str(tmp_path / "v"), "--no-serve"])

    assert not real.exists()
    assert (tmp_path / "v" / ".palimpsest" / "demo.db").exists()


def test_demo_checks_for_the_web_server_before_doing_any_work(tmp_path, monkeypatch):
    """The first two lines of the README used to end in a traceback.

    The offline core has no dependencies by design, so `pip install palimpsest-notion`
    gets you no web server — and `demo` copied a vault, mirrored it, printed a cheerful
    summary, and *then* raised `ImportError`. Twenty seconds of apparent progress
    followed by a stack trace is the worst possible first thirty seconds, and the error
    named `palimpsest[serve]`, which is a different project on PyPI.
    """
    import builtins

    real = builtins.__import__

    def missing(name, *a, **k):
        if name in ("fastapi", "uvicorn"):
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", missing)

    with pytest.raises(SystemExit) as caught:
        cli.main(["demo", "--dir", str(tmp_path / "v")])

    message = str(caught.value)
    assert "palimpsest-notion[serve]" in message, "must name the real distribution"
    assert "--no-serve" in message, "must offer the way forward"
    assert not (tmp_path / "v").exists(), "it must fail before doing any work"


# ---------------------------------------------------------------------------
# the commands that run with nothing configured
#
# These are what somebody types before they have given the tool a key, and what a
# deployment runs to find out whether it is healthy. Each has to work, and each has to
# say something useful when the answer is "nothing here yet".
# ---------------------------------------------------------------------------


def _run(argv, db=None):
    """`cli.main`, with the database pointed somewhere disposable."""
    return cli.main([*argv, *(["--db", db] if db else [])])


def test_status_passes_a_healthy_install_and_still_says_what_it_will_do(db, capsys,
                                                                       monkeypatch):
    """`palimpsest status` is documented as a health check that exits non-zero when it
    finds a problem. The write-posture line fires whenever writes are on at all -- the
    configuration people arrive at on purpose -- so counting it meant a correct install
    failed its own health check forever."""
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("PALIMPSEST_NOTION_ROOTS", "abc123")
    monkeypatch.setenv("PALIMPSEST_APPLY", "1")
    monkeypatch.setenv("PALIMPSEST_AUTONOMY", "low")
    # Measured, and it passed. Write access to an unmeasured model is a real warning and
    # stays one -- this test is about the line that is not.
    from palimpsest.evals import report
    from palimpsest.store.base import open_store

    store = open_store(db)
    report.record(store, "component",
                  {"weighted_f1": 0.95, "contradiction_recall": 1.0, "n": 20,
                   "passed": True}, model="anthropic/claude-opus-5")
    store.close()

    code = _run(["status"], db)

    out = capsys.readouterr().out
    assert "apply=on and autonomy=low" in out, "it still has to be said"
    assert code == 0, out


def test_status_fails_when_something_is_actually_missing(db, capsys, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    code = _run(["status"], db)

    assert code == 1
    assert "NOTION_TOKEN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


def test_db_sql_prints_a_schema_for_either_dialect(capsys):
    assert cli.main(["db", "sql", "--dialect", "sqlite"]) == 0
    sqlite = capsys.readouterr().out
    assert cli.main(["db", "sql", "--dialect", "postgres"]) == 0
    postgres = capsys.readouterr().out

    assert "CREATE TABLE" in sqlite and "CREATE TABLE" in postgres
    assert "AUTOINCREMENT" in sqlite, "that is the SQLite spelling"
    assert "BIGSERIAL" in postgres, "and that is the Postgres one"


def test_db_sql_can_be_written_to_a_file(tmp_path):
    out = tmp_path / "schema.sql"

    assert cli.main(["db", "sql", "--dialect", "sqlite", "--out", str(out)]) == 0
    assert "CREATE TABLE" in out.read_text(encoding="utf-8")


def test_db_check_on_a_fresh_store_reports_no_pending_migrations(db, capsys):
    """Exit 2 means "reachable, but the schema is behind" -- a deploy has to tell that
    apart from "cannot connect", because only one of them is fixed by running migrate."""
    code = cli.main(["db", "check", "--url", db])

    out = capsys.readouterr().out
    assert code == 0
    assert "pending   none" in out
    assert "ping" in out


def test_db_check_on_an_unreachable_database_exits_one_rather_than_raising(capsys):
    code = cli.main(["db", "check", "--url",
                     "postgresql://nobody@127.0.0.1:1/does_not_exist"])

    assert code == 1
    assert "UNREACHABLE" in capsys.readouterr().out


def test_db_check_redacts_the_password_it_prints(capsys):
    """This output is the first thing somebody pastes into an issue."""
    cli.main(["db", "check", "--url",
              "postgresql://user:hunter2@127.0.0.1:1/palimpsest"])

    assert "hunter2" not in capsys.readouterr().out


def test_db_migrate_is_idempotent(db, capsys):
    assert cli.main(["db", "migrate", "--url", db]) == 0
    first = capsys.readouterr().out
    assert cli.main(["db", "migrate", "--url", db]) == 0
    second = capsys.readouterr().out

    assert "applied" in first
    assert "nothing to do" in second


def test_db_reset_refuses_without_saying_you_mean_it(db):
    """It drops the mirror and the whole patch ledger. A typo must not be enough."""
    with pytest.raises(SystemExit) as caught:
        cli.main(["db", "reset", "--url", db])

    assert "--yes" in str(caught.value)


def test_db_reset_with_yes_empties_the_store(db, capsys):
    from palimpsest.store.base import open_store

    store = open_store(db)
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.close()

    assert cli.main(["db", "reset", "--yes", "--url", db]) == 0
    assert "truncated" in capsys.readouterr().out

    store = open_store(db)
    assert store.get_pages() == []
    store.close()


# ---------------------------------------------------------------------------
# history and provenance — the "where did this come from" half
# ---------------------------------------------------------------------------


def test_history_on_a_page_that_is_not_mirrored_says_so(db):
    with pytest.raises(SystemExit, match="no page"):
        _run(["history", "pg_nope"], db)


def test_history_on_a_page_with_no_edits_says_that_rather_than_nothing(db, capsys):
    """An empty list and a page palimpsest has never touched look identical otherwise,
    and the difference is the whole question being asked."""
    from palimpsest.store.base import open_store

    store = open_store(db)
    store.put_pages([{"page_id": "pg_1", "title": "Gradient clipping",
                      "last_edited": "x"}])
    store.close()

    assert _run(["history", "pg_1"], db) == 0
    out = capsys.readouterr().out
    assert "Gradient clipping" in out
    assert "has not edited this page" in out


def test_provenance_for_a_block_nobody_wrote_says_so(db, capsys):
    assert _run(["provenance", "bk_nope"], db) == 0
    assert "no provenance" in capsys.readouterr().out


def test_provenance_names_the_source_and_where_in_it(db, capsys):
    """This is the audit trail: a sentence, six months later, and the question of where
    it came from."""
    from palimpsest.store.base import open_store
    from palimpsest.types import Source

    store = open_store(db)
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1", "type": "paragraph",
                       "text": "clipping is the default", "position": 0}])
    store.put_source(Source(source_id="src_1", kind="web", title="A post on clipping",
                            url="https://example.com/p", text="x"))
    store.put_provenance([{"block_id": "bk_1", "page_id": "pg_1", "source_id": "src_1",
                           "relation": "refines",
                           "anchor": {"locator": "Introduction",
                                      "url": "https://example.com/p#intro"}}])
    store.close()

    assert _run(["provenance", "bk_1"], db) == 0
    out = capsys.readouterr().out
    assert "A post on clipping" in out
    assert "refines" in out
    assert "Introduction" in out
    assert "https://example.com/p#intro" in out


# ---------------------------------------------------------------------------
# the commands that need a model, run without one
# ---------------------------------------------------------------------------


def test_ingest_without_a_model_says_which_keys_would_work(db, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
                 "PALIMPSEST_MODEL_BASE_URL", "PALIMPSEST_MODEL_PROVIDER",
                 "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as caught:
        _run(["ingest", "text:a thought"], db)

    assert "API_KEY" in str(caught.value)


def test_sweep_names_the_kinds_it_does_have(db, capsys):
    """Refused at the parser, so the message lists the four rather than just saying no."""
    with pytest.raises(SystemExit):
        _run(["sweep", "vibes"], db)

    err = capsys.readouterr().err
    for kind in ("duplicates", "contradictions", "stale", "questions"):
        assert kind in err


def test_sweep_stale_runs_with_no_key_at_all(db, capsys):
    """Three of the four sweeps are arithmetic over the mirror. They are the day-one
    output, before anybody has given the tool a credential."""
    assert _run(["sweep", "stale"], db) == 0
    assert "stale" in capsys.readouterr().out


def test_sweep_questions_runs_with_no_key_either(db, capsys):
    assert _run(["sweep", "questions"], db) == 0
    assert "open_questions" in capsys.readouterr().out


def test_undo_on_a_patch_that_does_not_exist_says_so(db):
    with pytest.raises(SystemExit):
        _run(["undo", "pch_nope"], db)
