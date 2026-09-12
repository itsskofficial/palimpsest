"""The four sweeps over the base you already have — the day-one product.

Three of them need no model and no key at all, which is the point: `palimpsest sweep
duplicates` says something true about your notes before you have given the thing a
credential or any permission to write. That makes them the first output anyone sees, and
a first output that is wrong is the one that decides whether the tool gets a second run.

The recurring failure across all four is the same shape, and it is not a crash: a sweep
that reports *confidently* about a base it did not actually read. A stale record served
after the problem was fixed, a pair judged by a hallucinated index, a whole batch lost to
one provider hiccup with nothing saying so — each reads as a clean report.
"""

from __future__ import annotations

import time

import pytest

from palimpsest import sweep
from palimpsest.llm import ModelError, TokenUsage, Usage
from palimpsest.types import Source


class FakeModel:
    """Answers each batch from a script, and records what it was shown."""

    model = "fake/sweep-1"
    name = "fake"
    base_url = None

    def __init__(self, answers=None):
        # One entry per call. A `ModelError` instance is raised rather than returned.
        self.answers = list(answers or [])
        self.prompts: list[str] = []
        self.usage = Usage()

    def json(self, *, task, system, prompt, schema, effort="high",
             cache_prefix=None, max_tokens=None):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if self.answers else {"conflicts": []}
        if isinstance(answer, Exception):
            raise answer
        self.usage.add(task, TokenUsage(input=400, output=100), 0.01)
        return answer


def _conflict(pair_id: int, confidence: float = 0.9, kind: str = "factual") -> dict:
    return {"pair_id": pair_id, "kind": kind, "confidence": confidence,
            "explanation": "they cannot both be true"}


def _last(store, kind: str) -> dict | None:
    records = store.get_records(kind=kind, limit=1)
    return records[0]["payload"] if records else None


# ---------------------------------------------------------------------------
# duplicates — the sweep that needs nothing
# ---------------------------------------------------------------------------


def test_a_duplicate_finding_names_both_sides_well_enough_to_act_on(mirror):
    """The output is a merge queue. A finding that does not say which page each copy is
    on is a report you have to go and re-derive by hand."""
    finding = sweep.duplicates(mirror).findings[0]

    for side in ("a", "b"):
        assert finding[side]["block_id"]
        assert finding[side]["page_id"]
        assert finding[side]["page_title"], "a page id alone is not something to act on"
        assert finding[side]["text"]
    assert finding["a"]["page_id"] != finding["b"]["page_id"]
    assert 0.0 < finding["similarity"] <= 1.0


def test_the_suggestion_names_the_two_pages_by_title(mirror):
    suggestion = sweep.duplicates(mirror).findings[0]["suggestion"]

    assert "Attention" in suggestion and "Transformers" in suggestion


def test_a_high_threshold_reports_only_the_copies(mirror):
    """The default is permissive on purpose — a reworded restatement scores ~0.3. Raising
    the bar has to actually raise it, or the knob is decoration."""
    loose = sweep.duplicates(mirror, threshold=0.2)
    strict = sweep.duplicates(mirror, threshold=0.95)

    assert len(strict) < len(loose)
    assert all(f["similarity"] >= 0.95 for f in strict.findings)


def test_the_result_is_capped_so_a_messy_base_does_not_return_thousands(mirror):
    assert len(sweep.duplicates(mirror, threshold=0.01, top=1)) <= 1


def test_the_duplicate_sweep_is_persisted_for_the_ui_to_read(mirror):
    """`/v1/records` serves the last sweep; the desktop app never re-runs it to render
    it. A sweep that is not written is a sweep the UI cannot show."""
    result = sweep.duplicates(mirror)

    payload = _last(mirror, "sweep_duplicates")
    assert payload is not None
    assert len(payload["findings"]) == len(result.findings)
    record = mirror.get_records(kind="sweep_duplicates", limit=1)[0]
    assert record["label"] == str(len(result.findings))


def test_an_empty_base_is_an_empty_sweep_rather_than_an_error(store):
    """Day one, before the first mirror. This is the very first command people run."""
    result = sweep.duplicates(store)

    assert result.findings == []
    assert result.scanned == 0


# ---------------------------------------------------------------------------
# contradictions — the only sweep that costs money
# ---------------------------------------------------------------------------


def test_a_conflict_the_model_reports_becomes_a_finding_with_both_passages(mirror):
    model = FakeModel([{"conflicts": [_conflict(0, 0.88, "temporal")]}])

    result = sweep.contradictions(mirror, model, min_similarity=0.1)

    assert len(result) == 1
    finding = result.findings[0]
    assert finding["kind"] == "temporal"
    assert finding["confidence"] == 0.88
    assert finding["explanation"]
    assert finding["a"]["page_id"] != finding["b"]["page_id"]


def test_exact_copies_are_left_to_the_duplicate_sweep(mirror):
    """Paying a model to be told that two identical paragraphs agree is the definition
    of a wasted call, and every pair it crowds out is one it could have judged."""
    model = FakeModel()

    sweep.contradictions(mirror, model, min_similarity=0.1)

    assert model.prompts, "something must still have been compared"
    for prompt in model.prompts:
        # The fixture's exact duplicate appears verbatim on two pages; a pair containing
        # it would show the same text twice inside one PAIR block.
        for chunk in prompt.split("PAIR ")[1:]:
            a = chunk.split("B —")[0]
            b = chunk.split("B —")[-1]
            assert a.strip()[-60:] != b.strip()[-60:], "an exact copy was sent as a pair"


def test_pairs_are_batched_rather_than_asked_one_at_a_time(mirror):
    """One call per pair is the obvious implementation and it is the expensive one —
    the pairing stage exists precisely to make the model bill linear."""
    model = FakeModel()

    sweep.contradictions(mirror, model, min_similarity=0.01, batch=8)

    assert len(model.prompts) == 1, "it all fit in one batch, so it should cost one call"
    assert model.prompts[0].count("PAIR ") >= 2


def test_a_smaller_batch_splits_into_more_calls(mirror):
    model = FakeModel()

    sweep.contradictions(mirror, model, min_similarity=0.01, batch=1)

    assert len(model.prompts) >= 2
    assert all(p.count("PAIR ") == 1 for p in model.prompts)


def test_a_pair_id_the_model_invented_is_dropped_rather_than_indexed(mirror):
    """`chunk[idx]` on an out-of-range id raises; on a negative one it silently wraps and
    attributes the conflict to the wrong two passages entirely."""
    model = FakeModel([{"conflicts": [_conflict(99), _conflict(-1), _conflict(0)]}])

    result = sweep.contradictions(mirror, model, min_similarity=0.1)

    assert len(result) == 1, "only the real pair survives"


def test_one_failed_batch_does_not_lose_the_others(mirror):
    """A rate limit two batches into a twenty-batch sweep must not throw away eighteen
    batches of paid-for work."""
    model = FakeModel([ModelError("429 slow down"),
                       {"conflicts": [_conflict(0)]}])

    result = sweep.contradictions(mirror, model, min_similarity=0.01, batch=1)

    assert len(result) == 1
    assert any("failed" in note for note in result.notes)
    assert any("429" in note for note in result.notes), "the note has to say what broke"


def test_findings_come_back_most_confident_first(mirror):
    """Nobody reads past the top of this list, so the order is the report."""
    model = FakeModel([{"conflicts": [_conflict(0, 0.3), _conflict(1, 0.95)]}])

    result = sweep.contradictions(mirror, model, min_similarity=0.01, batch=8)

    confidences = [f["confidence"] for f in result.findings]
    assert confidences == sorted(confidences, reverse=True)


def test_the_number_of_model_calls_is_reported(mirror):
    """This is the one sweep with a bill, and the summary line is where it is shown."""
    model = FakeModel()

    result = sweep.contradictions(mirror, model, min_similarity=0.01, batch=1)

    assert result.model_calls == len(model.prompts) > 0


def test_max_pairs_caps_the_bill_on_a_large_base(mirror):
    model = FakeModel()

    sweep.contradictions(mirror, model, min_similarity=0.01, max_pairs=1, batch=8)

    assert model.prompts[0].count("PAIR ") == 1


def test_a_base_with_nothing_related_says_so_instead_of_calling_the_model(mirror):
    model = FakeModel()

    result = sweep.contradictions(mirror, model, min_similarity=0.999)

    assert model.prompts == [], "no pairs means no reason to spend anything"
    assert any("no sufficiently related" in note for note in result.notes)


def test_a_clean_sweep_overwrites_the_record_rather_than_leaving_the_old_one(mirror):
    """The failure this exists to catch: find three contradictions, fix all three, sweep
    again, and the desktop app goes on showing three — because the clean run returned
    early without writing. A report that gets less true the more you act on it."""
    model = FakeModel([{"conflicts": [_conflict(0)]}])
    sweep.contradictions(mirror, model, min_similarity=0.01, batch=8)
    assert len(_last(mirror, "sweep_contradictions")["findings"]) == 1

    sweep.contradictions(mirror, FakeModel(), min_similarity=0.999)

    assert _last(mirror, "sweep_contradictions")["findings"] == []


def test_both_passages_are_shown_to_the_model_with_the_page_they_are_on(mirror):
    """The page titles are most of the signal for "same thing, different version"."""
    model = FakeModel()

    sweep.contradictions(mirror, model, min_similarity=0.01)

    assert "Attention" in model.prompts[0] or "Transformers" in model.prompts[0]
    assert "page" in model.prompts[0]


def test_the_instructions_push_against_false_positives():
    """A sweep that cries wolf gets ignored, and then the real contradiction is ignored
    with it."""
    assert "Be conservative" in sweep.CONTRADICTION_SYSTEM
    assert "false positive" in sweep.CONTRADICTION_SYSTEM


# ---------------------------------------------------------------------------
# staleness — arithmetic, not a model
# ---------------------------------------------------------------------------


@pytest.fixture()
def aged(mirror):
    """Give a block provenance, then backdate it by however many days the test wants."""

    def age(block_id: str, days: float) -> None:
        mirror.put_source(Source(source_id=f"src_{block_id}", kind="web",
                                 title="A post", url="https://example.com/p", text="x"))
        mirror.put_provenance([{"block_id": block_id, "page_id": "pg_optim",
                                "source_id": f"src_{block_id}"}])
        mirror.conn.execute("UPDATE provenance SET created_at=? WHERE block_id=?",
                            (time.time() - days * 86400.0, block_id))
        mirror.conn.commit()

    return age


def test_a_fast_moving_fact_goes_stale_long_before_a_slow_one(mirror, aged):
    """Age alone is not staleness. A note on linear algebra from 2019 is fine; a price
    from 2019 is fiction. Getting this backwards flags the wrong half of the base."""
    aged("bk_opt_2", 100)   # a price — 60-day half-life
    aged("bk_opt_1", 100)   # AdamW — the 730-day default

    flagged = {f["block_id"] for f in sweep.stale(mirror).findings}

    assert "bk_opt_2" in flagged
    assert "bk_opt_1" not in flagged


def test_the_finding_says_how_old_and_against_what_half_life(mirror, aged):
    """Stale on its own is not actionable. The two numbers are what let a reader
    disagree with the verdict."""
    aged("bk_opt_2", 180)

    finding = sweep.stale(mirror).findings[0]

    assert finding["half_life_days"] == 60
    assert 175 <= finding["age_days"] <= 185
    assert finding["staleness"] == pytest.approx(finding["age_days"] / 60, rel=0.01)


def test_the_stalest_claims_come_first(mirror, aged):
    aged("bk_opt_2", 600)
    aged("bk_att_1", 5000)

    staleness = [f["staleness"] for f in sweep.stale(mirror).findings]

    assert staleness == sorted(staleness, reverse=True)


def test_a_claim_still_inside_its_half_life_is_not_flagged(mirror, aged):
    aged("bk_opt_2", 10)

    assert sweep.stale(mirror).findings == []


def test_the_finding_carries_the_sources_so_you_can_go_and_check(mirror, aged):
    aged("bk_opt_2", 500)

    sources = sweep.stale(mirror).findings[0]["sources"]

    assert sources and sources[0]["url"] == "https://example.com/p"
    assert len(sources) <= 3, "a long provenance list would swamp the report"


def test_blocks_with_no_provenance_are_not_scanned_at_all(mirror, aged):
    """Staleness is measured from when a claim entered the base. A mirrored block that
    was never ingested has no such date, and dating it from now would mark the entire
    existing workspace fresh."""
    aged("bk_opt_2", 500)

    assert sweep.stale(mirror).scanned == 1


def test_a_base_with_no_provenance_explains_itself(mirror):
    """Otherwise the first run of this sweep is an empty report that looks like a clean
    bill of health."""
    result = sweep.stale(mirror)

    assert result.findings == []
    assert any("provenance" in note for note in result.notes)


def test_staleness_is_measured_against_a_clock_the_caller_can_set(mirror, aged):
    """`now` is what makes this testable at all, and what lets an eval replay a base as
    of a date."""
    aged("bk_opt_2", 100)

    assert sweep.stale(mirror, now=time.time() - 90 * 86400).findings == []


@pytest.mark.parametrize("text,expected", [
    ("Sonnet costs $3 per million input tokens", 60),
    ("The v2 endpoint deprecates the old SDK", 120),
    ("This model tops the leaderboard", 180),
    ("On the roadmap for the next beta", 90),
    ("Matrix multiplication is associative", 730),
])
def test_the_half_life_comes_from_what_the_claim_is_about(text, expected):
    assert sweep._half_life_days(text) == expected


# ---------------------------------------------------------------------------
# open questions — the homework queue
# ---------------------------------------------------------------------------


def test_a_question_shaped_block_becomes_a_finding(mirror):
    result = sweep.open_questions(mirror)

    assert any(f["text"].rstrip().endswith("?") for f in result.findings)
    assert all(f["page_title"] for f in result.findings)


@pytest.mark.parametrize("text", [
    "TODO: work out whether the residual stream is normalised here",
    "TBD — which optimiser did we settle on for the small runs",
    "Q: does this hold for grouped-query attention as well",
    "unclear whether the tokenizer is shared between the two models",
    "not sure this is still true after the v3 release of the library",
    "?? the numbers in this table do not add up to the total above",
])
def test_the_ways_people_actually_flag_uncertainty_are_all_recognised(mirror, text):
    """These are markers in someone's existing notes, not a syntax the product invented.
    Missing one means the homework loop never sees that question."""
    mirror.put_blocks([{"block_id": "bk_q", "page_id": "pg_optim", "type": "paragraph",
                        "text": text, "position": 9}])

    assert "bk_q" in {f["block_id"] for f in sweep.open_questions(mirror).findings}


def test_a_question_already_ticked_off_is_not_still_open(mirror):
    """A completed to-do is answered work. Queuing it as research would have the base do
    homework it has already done."""
    mirror.put_blocks([{"block_id": "bk_done", "page_id": "pg_optim", "type": "to_do",
                        "text": "[x] TODO: check whether this still holds",
                        "position": 9}])

    found = {f["block_id"] for f in sweep.open_questions(mirror).findings}
    assert "bk_done" not in found


def test_a_bare_question_mark_is_not_a_research_task(mirror):
    """Stray punctuation and one-word notes would flood the queue with nothing to do."""
    mirror.put_blocks([{"block_id": "bk_short", "page_id": "pg_optim",
                        "type": "paragraph", "text": "?", "position": 9}])

    found = {f["block_id"] for f in sweep.open_questions(mirror).findings}
    assert "bk_short" not in found


def test_ordinary_prose_is_not_a_question(mirror):
    found = {f["block_id"] for f in sweep.open_questions(mirror).findings}
    assert "bk_opt_1" not in found


def test_the_queue_is_capped(mirror):
    assert len(sweep.open_questions(mirror, top=1)) <= 1


def test_the_open_questions_sweep_is_persisted_too(mirror):
    result = sweep.open_questions(mirror)

    assert len(_last(mirror, "sweep_open_questions")["findings"]) == len(result.findings)


# ---------------------------------------------------------------------------
# the shape every sweep returns
# ---------------------------------------------------------------------------


def test_a_result_reports_its_own_size(mirror):
    result = sweep.duplicates(mirror)

    assert len(result) == len(result.findings)


def test_the_serialised_form_is_what_the_api_returns(mirror):
    payload = sweep.duplicates(mirror).as_dict()

    assert set(payload) == {"kind", "findings", "scanned", "seconds", "model_calls",
                            "notes"}
    assert payload["kind"] == "duplicates"
    assert isinstance(payload["seconds"], float)
