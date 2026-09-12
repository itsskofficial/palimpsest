"""Recording an eval run, and the leaderboard that compares models. Both at zero.

Between them these decide whether `palimpsest status` can warn you that the model you
have given write access to has never been measured, or was measured and failed. That
warning is the only thing standing between somebody and handing their notes to a model
that misses two contradictions in three — so the recording underneath it has to work, and
it has to survive tracing being broken, because a dashboard that is down must not lose
the measurement.
"""

from __future__ import annotations

import pytest

from palimpsest.evals import leaderboard, report


@pytest.fixture()
def metrics():
    return {
        "weighted_f1": 0.95,
        "contradiction_recall": 1.0,
        "macro_f1": 0.93,
        "n": 20,
        "passed": True,
        "per_relation": {
            "contradicts": {"f1": 1.0, "recall": 1.0, "precision": 1.0, "support": 3},
            "new": {"f1": 0.9, "recall": 0.9, "precision": 0.9, "support": 5},
        },
    }


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def test_a_run_is_persisted_and_findable_by_suite_and_model(store, metrics):
    """This is what `status` reads to decide whether to warn about an unmeasured
    model."""
    run_id = report.record(store, "component", metrics,
                           model="anthropic/claude-sonnet-5")

    assert run_id.startswith("run_")
    last = store.last_eval_run("component", "anthropic/claude-sonnet-5")
    assert last is not None
    assert last["passed"] is True
    assert last["scores"]["weighted_f1"] == 0.95
    assert last["scores"]["n"] == 20


def test_only_numbers_are_stored_as_scores(store, metrics):
    """`per_relation` is a nested dict and `passed` is a flag. Stored as scores they
    would either fail to serialise or show up in the history as nonsense columns."""
    report.record(store, "component", metrics, model="m")

    scores = store.last_eval_run("component", "m")["scores"]
    assert "per_relation" not in scores
    assert "passed" not in scores
    assert set(scores) == {"weighted_f1", "contradiction_recall", "macro_f1", "n"}


def test_a_failing_run_is_recorded_as_failing(store):
    """A failure that is not recorded is a model that looks unmeasured, and `status`
    then says "never measured" rather than "measured, and it failed" — which is a much
    weaker warning."""
    report.record(store, "component", {"weighted_f1": 0.42, "n": 20, "passed": False},
                  model="ollama/llama3.1:8b")

    last = store.last_eval_run("component", "ollama/llama3.1:8b")
    assert last["passed"] is False


def test_the_most_recent_run_for_a_model_is_the_one_returned(store):
    for score in (0.42, 0.61, 0.88):
        report.record(store, "component", {"weighted_f1": score, "n": 20,
                                           "passed": score > 0.8}, model="m")

    assert store.last_eval_run("component", "m")["scores"]["weighted_f1"] == 0.88


def test_two_models_do_not_overwrite_each_other(store, metrics):
    """The whole point of the leaderboard is that several models are measured against
    one set, and each keeps its own record."""
    report.record(store, "component", metrics, model="a")
    report.record(store, "component", {**metrics, "weighted_f1": 0.5}, model="b")

    assert store.last_eval_run("component", "a")["scores"]["weighted_f1"] == 0.95
    assert store.last_eval_run("component", "b")["scores"]["weighted_f1"] == 0.5


def test_tracing_being_broken_never_loses_the_measurement(store, metrics, monkeypatch):
    """The run is the thing that matters; the dashboard is a convenience. An exception
    from a telemetry backend must not take the number with it."""
    import palimpsest.trace as trace

    monkeypatch.setattr(trace, "enabled", lambda: True)
    monkeypatch.setattr(trace, "span", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("langfuse is down")))

    run_id = report.record(store, "component", metrics, model="m")

    assert store.last_eval_run("component", "m") is not None
    assert run_id


def test_scores_are_pushed_inside_a_span(store, metrics, monkeypatch):
    """A score with no trace to attach to is dropped, and Langfuse warns once per score
    — six identical warnings for one eval run, which reads like a broken install."""
    import contextlib

    import palimpsest.trace as trace

    pushed: list[str] = []
    inside: list[bool] = []

    @contextlib.contextmanager
    def span(name, **kw):
        inside.append(True)
        try:
            yield type("Span", (), {"update": lambda self, **k: None})()
        finally:
            inside.pop()

    monkeypatch.setattr(trace, "enabled", lambda: True)
    monkeypatch.setattr(trace, "span", span)
    monkeypatch.setattr(trace, "score",
                        lambda name, value: pushed.append(name) if inside else
                        pytest.fail(f"{name} was scored outside a span"))
    monkeypatch.setattr(trace, "flush", lambda: None)

    report.record(store, "component", metrics, model="m")

    assert "eval.component.weighted_f1" in pushed
    # Per-relation too, or a regression on one relation hides inside a steady average.
    assert "eval.component.f1.contradicts" in pushed


# ---------------------------------------------------------------------------
# the leaderboard
# ---------------------------------------------------------------------------


def test_models_are_ordered_best_first():
    # `render` is the half that can be tested without a model; `run` needs live
    # providers and is exercised by `palimpsest eval leaderboard`.
    table = leaderboard.render([
        {"model": "b", "weighted_f1": 0.57, "contradiction_recall": 0.67,
         "duplicate_recall": 0.0, "n": 20, "passed": False},
        {"model": "a", "weighted_f1": 0.95, "contradiction_recall": 1.0,
         "duplicate_recall": 1.0, "n": 20, "passed": True},
    ])

    assert "`b`" in table and "`a`" in table
    assert "PASS" in table and "fail" in table


def test_the_best_score_is_the_one_emphasised():
    table = leaderboard.render([
        {"model": "a", "weighted_f1": 0.95, "contradiction_recall": 1.0,
         "duplicate_recall": 1.0, "n": 20, "passed": True},
        {"model": "b", "weighted_f1": 0.57, "contradiction_recall": 0.67,
         "duplicate_recall": 0.0, "n": 20, "passed": False},
    ])

    assert "**0.95**" in table
    assert "**0.57**" not in table


def test_a_model_that_could_not_be_reached_gets_a_row_saying_so():
    """The common shape of this command is a mixed list where one local runtime is not
    up. Losing four good measurements to one missing container helps nobody."""
    table = leaderboard.render([
        {"model": "a", "weighted_f1": 0.95, "contradiction_recall": 1.0,
         "duplicate_recall": 1.0, "n": 20, "passed": True},
        {"model": "ollama/qwen3:8b", "error": "connection refused"},
    ])

    assert "connection refused" in table
    assert "**0.95**" in table, "the reachable model is still reported"


def test_the_table_says_which_column_to_read():
    """The headline F1 is the number people look at, and it is the wrong one: a model
    that misses two contradictions in three is not one to hand write access to, whatever
    its average."""
    table = leaderboard.render([
        {"model": "a", "weighted_f1": 0.9, "contradiction_recall": 0.67,
         "duplicate_recall": 1.0, "n": 20, "passed": False}])

    assert "Contradiction recall is the column to read" in table


def test_the_table_is_markdown_that_can_be_pasted_into_the_readme():
    table = leaderboard.render([
        {"model": "a", "weighted_f1": 0.9, "contradiction_recall": 1.0,
         "duplicate_recall": 1.0, "n": 20, "passed": True}])
    lines = table.splitlines()

    assert lines[0].startswith("|") and lines[0].endswith("|")
    assert set(lines[1].replace("|", "").replace(" ", "")) == {"-"}
    assert lines[2].count("|") == lines[0].count("|"), "ragged rows do not render"


def test_a_row_with_no_score_at_all_does_not_crash_the_table():
    """`weighted_f1` can be absent without an `error` — a suite that ran and produced
    nothing. Formatting `None` raises, and losing the whole table to one bad row is a
    poor trade."""
    table = leaderboard.render([
        {"model": "a", "weighted_f1": None, "contradiction_recall": None,
         "duplicate_recall": None, "n": 0, "passed": False}])

    assert "`a`" in table
