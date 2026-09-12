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
