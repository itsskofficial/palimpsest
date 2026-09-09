"""Local models: no key, no network beyond this machine, everything still works.

The provider-invariance work made "any model" true in the code. Running it against a
model nobody had used during development is what makes it true in fact, and it is where
the interesting failures are: a small model answers `80` when the schema says a number
between zero and one, a runtime implements `json_schema` but not `reasoning_effort`, an
embedding endpoint returns its vectors in whatever order it likes.

These tests need no runtime. They pin the *resolution* — that naming a local runtime
produces the right base URL, model and keyless client — and the parsing quirks that only
local models exposed. The live half (a real Ollama serving real vectors) is measured by
`palimpsest eval retrieval`, which is a command rather than a test because it needs a
machine with models on it.
"""

from __future__ import annotations

import pytest

from palimpsest import embed
from palimpsest.config import (
    LOCAL_RUNTIMES,
    Settings,
    describe_embedding,
    describe_setup,
)
from palimpsest.llm import resolve
from palimpsest.relate import _confidence

# ---------------------------------------------------------------------------
# a local runtime is chosen, never guessed
# ---------------------------------------------------------------------------


def test_naming_a_local_runtime_resolves_to_it_with_no_key():
    """The whole point: a working setup that costs nothing and sends nothing anywhere."""
    setup = describe_setup(Settings(model_provider="ollama"))

    assert setup is not None
    assert setup.provider == "ollama"
    assert setup.base_url == "http://127.0.0.1:11434/v1"
    assert setup.model == LOCAL_RUNTIMES["ollama"][1]


def test_a_local_runtime_needs_no_api_key_to_build_a_client():
    provider = resolve(Settings(model_provider="ollama"))

    assert provider.name == "ollama"
    assert provider._key in (None, "")


def test_an_explicit_model_beats_the_runtime_default():
    setup = describe_setup(Settings(model_provider="ollama", model="qwen3:8b"))
    assert setup is not None
    assert setup.model == "qwen3:8b"


def test_a_runtime_with_no_default_model_must_be_told_which_one():
    """LM Studio and llama.cpp serve whatever you happened to load, so there is no
    sensible default to invent. Returning None sends the caller to the "no model
    configured" message, which names the setting, rather than to a 404 from a model id
    nobody has."""
    assert describe_setup(Settings(model_provider="lmstudio")) is None
    assert describe_setup(
        Settings(model_provider="lmstudio", model="some-local-model")) is not None


def test_localhost_is_never_reached_for_without_being_asked():
    """A settings object that names no provider must not resolve to a local runtime.

    Silently pointing at localhost because something happened to be listening would be a
    surprising thing for a config layer to do, and it would make the meaning of an empty
    configuration depend on what else is running on the machine.
    """
    assert describe_setup(Settings()) is None
    assert describe_embedding(Settings()) is None


# ---------------------------------------------------------------------------
# embeddings from the same runtime
# ---------------------------------------------------------------------------


def test_choosing_a_local_runtime_for_chat_opts_into_its_embeddings_too():
    """The only configuration where vectors are free, so it should be the easy one to
    fall into rather than a second decision."""
    setup = describe_embedding(Settings(model_provider="ollama"))

    assert setup is not None
    assert setup.model == LOCAL_RUNTIMES["ollama"][2]
    assert setup.base_url == "http://127.0.0.1:11434/v1"


def test_a_local_embedder_is_built_and_wrapped_in_the_store_cache():
    store = None
    embedder = embed.resolve(Settings(model_provider="ollama"), store=store)
    assert embedder is not None
    assert embedder.model == "mxbai-embed-large"


def test_an_explicit_embedding_provider_overrides_the_chat_one():
    setup = describe_embedding(
        Settings(model_provider="anthropic", embed_provider="ollama"))
    assert setup is not None
    assert setup.provider == "ollama"


# ---------------------------------------------------------------------------
# what a small model actually returns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "want"), [
    (0.9, 0.9),
    (0.0, 0.0),
    (1, 1.0),
    (80, 0.8),
    (50, 0.5),
    (100, 1.0),
    (-3, 0.0),
    ("nonsense", 0.0),
    (None, 0.0),
    (float("nan"), 0.0),
    (float("inf"), 0.0),
])
def test_confidence_is_read_on_whatever_scale_the_model_answered_on(raw, want):
    """A local model asked for a number between zero and one answers `80`.

    Clamping that to the range gave 1.0, so "80% sure" — and worse, "50% sure" — was
    recorded as *certain* and sailed straight past the confidence floor, which is the
    last thing standing between a doubtful judgement and someone's notes. Nothing
    legitimate lands between 1 and 100 on a 0-1 scale, so reading those as percentages
    is unambiguous.
    """
    assert _confidence(raw) == pytest.approx(want)


def test_an_unreadable_confidence_routes_to_review_rather_than_to_notion():
    """0.0 is below every sane floor, so garbage becomes a review item. The failure
    direction matters more than the parsing."""
    assert _confidence({"not": "a number"}) == 0.0
    assert _confidence([1, 2, 3]) == 0.0


# ---------------------------------------------------------------------------
# an unmeasured model with write access is a thing worth saying out loud
# ---------------------------------------------------------------------------


def _record(store, model: str, *, passed: bool, f1: float, recall: float) -> None:
    from palimpsest.types import new_id

    store.put_eval_run({
        "run_id": new_id("run_"), "suite": "component", "model": model,
        "scores": {"weighted_f1": f1, "contradiction_recall": recall, "n": 20},
        "passed": passed,
    })


def test_a_model_with_write_access_and_no_measurement_is_flagged(store):
    """The risk the provider work created.

    A 7B model on a laptop and a frontier model are three lines of configuration apart
    and classify very differently — measured on the committed fixture, 0.65 weighted F1
    against 0.95, and contradiction recall 0.67 against 1.00. So "it may write to my
    notes on its own" means something different depending on an answer nobody is
    prompted for. The warning names the one command that answers it.
    """
    from palimpsest.config import autonomy_warnings

    settings = Settings(apply=True, autonomy="full", model_provider="ollama",
                        model="qwen2.5:7b")
    warnings = autonomy_warnings(store, settings)

    assert len(warnings) == 1
    assert "ollama/qwen2.5:7b" in warnings[0]
    assert "never been measured" in warnings[0]
    assert "palimpsest eval component" in warnings[0]


def test_a_measured_and_failing_model_says_what_it_scored(store):
    from palimpsest.config import autonomy_warnings

    _record(store, "ollama/qwen2.5:7b", passed=False, f1=0.42, recall=0.33)
    settings = Settings(apply=True, autonomy="full", model_provider="ollama",
                        model="qwen2.5:7b")

    (warning,) = autonomy_warnings(store, settings)
    assert "failed the classifier eval" in warning
    assert "0.42" in warning
    assert "0.33" in warning


def test_a_measured_and_passing_model_is_not_nagged_about(store):
    from palimpsest.config import autonomy_warnings

    _record(store, "anthropic/claude-sonnet-5", passed=True, f1=0.95, recall=1.0)
    settings = Settings(apply=True, autonomy="everything",
                        model_provider="anthropic", model="claude-sonnet-5")

    assert autonomy_warnings(store, settings) == []


def test_a_score_earned_by_one_model_is_never_inherited_by_another(store):
    """Exact match on the model string, deliberately.

    A fuzzy match would be a way for a small local model to wear a frontier model's
    number, which is precisely the confusion this warning exists to prevent.
    """
    from palimpsest.config import autonomy_warnings

    _record(store, "anthropic/claude-sonnet-5", passed=True, f1=0.95, recall=1.0)
    settings = Settings(apply=True, autonomy="full", model_provider="ollama",
                        model="qwen2.5:7b")

    (warning,) = autonomy_warnings(store, settings)
    assert "never been measured" in warning


def test_propose_only_is_never_warned_about(store):
    """Nothing reaches Notion without a person reading it, so an unmeasured model costs
    a review rather than an edit. Warning here would be noise on the safe default."""
    from palimpsest.config import autonomy_warnings

    assert autonomy_warnings(
        store, Settings(apply=False, autonomy="everything",
                        model_provider="ollama", model="qwen2.5:7b")) == []
    assert autonomy_warnings(
        store, Settings(apply=True, autonomy="none",
                        model_provider="ollama", model="qwen2.5:7b")) == []


def test_no_model_configured_produces_no_model_warning(store):
    """`problems()` already says there is no model. Saying it twice, differently, is
    how a checks list becomes something people stop reading."""
    from palimpsest.config import autonomy_warnings

    assert autonomy_warnings(store, Settings(apply=True, autonomy="full")) == []


def test_a_store_that_cannot_answer_does_not_break_status(store):
    """`palimpsest status` is what you run when things are already wrong. It must not be
    the thing that raises."""
    from palimpsest.config import model_quality

    class Broken:
        def last_eval_run(self, suite, model):
            raise RuntimeError("no such table")

    settings = Settings(apply=True, autonomy="full", model_provider="ollama")
    assert model_quality(Broken(), settings) is None
