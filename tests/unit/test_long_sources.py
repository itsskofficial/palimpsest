"""Long sources: a 77-minute talk, and what it took to keep every claim from it.

Sent through the desktop app, the talk produced 183 claims and one new page — and the page
held 50 of them. Two failures, both silent:

1. **Notion takes at most 100 children on create**, and the client sent the first 100
   blocks and dropped the rest. Each claim was a bullet plus its citation, so 100 blocks
   was 50 claims. The job reported success.
2. **One composition call for 183 claims** came back unusable, so the page fell back to
   bullets in the first place. A page that long is written in parts now.

And one that was only slow: the eight extraction windows ran one after another.
"""

from __future__ import annotations

import threading
import time

import pytest

from palimpsest.compose import COMPOSE_GROUP, compose_sections
from palimpsest.llm import TokenUsage, Usage
from palimpsest.notion.client import MAX_APPEND, NotionClient
from palimpsest.types import Anchor, Claim, ClaimType, Source

# ---------------------------------------------------------------------------
# no block is ever dropped
# ---------------------------------------------------------------------------


class _Recording(NotionClient):
    """A client whose transport records requests instead of making them."""

    def __init__(self, fail_append_at: int | None = None):
        super().__init__("ntn_x")
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_append_at = fail_append_at

    def _request(self, method, path, body=None, params=None):
        self.requests.append((method, path, body))
        if path.endswith("/children") and self.fail_append_at is not None:
            appends = sum(1 for m, p, _ in self.requests if p.endswith("/children"))
            if appends == self.fail_append_at:
                raise RuntimeError("notion 502")
        return {"id": "pg_new", "object": "page"}


def _blocks(n: int) -> list[dict]:
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(i)}}]}}
            for i in range(n)]


def test_a_page_with_more_than_100_blocks_keeps_every_one():
    client = _Recording()

    client.create_page("pg_root", "System Design Interviews", _blocks(366))

    created = client.requests[0][2]
    appended = [body["children"] for _, path, body in client.requests[1:]]
    assert len(created["children"]) == MAX_APPEND
    assert [len(chunk) for chunk in appended] == [100, 100, 66]
    everything = created["children"] + [b for chunk in appended for b in chunk]
    assert [b["paragraph"]["rich_text"][0]["text"]["content"] for b in everything] == \
        [str(i) for i in range(366)], "all of them, in order"


def test_a_short_page_is_still_one_request():
    client = _Recording()

    client.create_page("pg_root", "Short", _blocks(12))

    assert len(client.requests) == 1


def test_a_page_whose_remainder_fails_to_append_is_taken_back():
    """Half a page is worse than none, and the applier only records an inverse for a
    create that returned — so a half-built page would be one nobody could undo."""
    client = _Recording(fail_append_at=2)

    with pytest.raises(RuntimeError, match="502"):
        client.create_page("pg_root", "Long", _blocks(250))

    method, path, body = client.requests[-1]
    assert (method, path) == ("PATCH", "pages/pg_new")
    assert body == {"in_trash": True}


def test_a_database_row_keeps_every_block_too():
    """The journal writes rows through the same door, with the same limit."""
    client = _Recording()

    client.create_row("ds_1", {"Name": {"title": []}}, _blocks(150))

    assert [len(b["children"]) for _, _, b in client.requests] == [100, 50]


# ---------------------------------------------------------------------------
# a long page is composed in parts
# ---------------------------------------------------------------------------


def _claims(n: int) -> list[Claim]:
    return [Claim(claim_id=f"clm_{i:03d}", text=f"Fact number {i}.", type=ClaimType.FACT,
                  topics=("system design",), confidence=0.9,
                  anchor=Anchor("timestamp", f"{i}:00", i * 100, i * 100 + 20,
                                f"https://www.youtube.com/watch?v=v&t={i * 60}s"),
                  source_id="src_1")
            for i in range(n)]


SOURCE = Source(source_id="src_1", kind="youtube", title="A long talk", text="x",
                url="https://www.youtube.com/watch?v=v")


class _Composer:
    """Lays out whatever claims it is shown, one paragraph per claim."""

    model = "fake/compose-1"
    name = "fake"

    def __init__(self, fail_parts=(), drop_in_parts=()):
        self.prompts: list[str] = []
        self.fail_parts = set(fail_parts)
        self.drop_in_parts = set(drop_in_parts)
        self.usage = Usage()

    def json(self, *, task, system, prompt, schema, effort="high", cache_prefix=None,
             max_tokens=None):
        import re

        self.prompts.append(prompt)
        self.usage.add(task, TokenUsage(input=10, output=10), 0.0)
        part = len(self.prompts)
        if part in self.fail_parts:
            raise RuntimeError("the response was cut off")
        ids = re.findall(r"\[(clm_\d+)\]", prompt)
        if part in self.drop_in_parts:
            ids = ids[:-1]
        return {"title": "System Design Interviews", "icon": "🏗️", "summary": "s",
                "blocks": [{"type": "heading_2", "text": f"Part {part}", "claim_ids": []}]
                + [{"type": "paragraph", "text": f"prose for {i}", "claim_ids": [i]}
                   for i in ids]}


def test_a_long_source_is_composed_in_ordered_parts_as_one_page():
    model = _Composer()

    result = compose_sections(_claims(183), SOURCE, model)

    assert len(model.prompts) == -(-183 // COMPOSE_GROUP)
    assert "PART 1 OF 5" in model.prompts[0]
    assert "PART 5 OF 5" in model.prompts[-1]
    assert result.ok and not result.missing
    assert result.sections == 5 and result.fallback_sections == 0
    assert result.title == "System Design Interviews"
    headings = [b for b in result.children if b["type"] == "heading_2"]
    assert len(headings) == 5, "one section per part"
    sources = [b for b in result.children if b["type"] == "callout"]
    assert len(sources) == 1, "the source line once, at the end, not once per part"
    assert result.children[-1]["type"] == "callout"


def test_the_parts_follow_the_order_of_the_source():
    """A talk builds on itself, so the page reads in the order it was said."""
    claims = list(reversed(_claims(90)))
    model = _Composer()

    compose_sections(claims, SOURCE, model)

    assert "[clm_000]" in model.prompts[0]
    assert "[clm_089]" in model.prompts[-1]


def test_a_part_that_fails_costs_only_its_own_sections():
    model = _Composer(fail_parts={2})

    result = compose_sections(_claims(120), SOURCE, model)

    assert result.ok, "no claim was lost"
    assert result.fallback_sections == 1
    bullets = [b for b in result.children if b["type"] == "bulleted_list_item"]
    assert len(bullets) == COMPOSE_GROUP, "only the failed part became bullets"
    marker = bullets[0]["bulleted_list_item"]["rich_text"][-1]["text"]
    assert marker["link"]["url"].startswith("https://www.youtube.com/watch?v=v&t="), \
        "and the bullets still cite their moment"


def test_a_part_that_drops_a_claim_falls_back_rather_than_losing_it():
    model = _Composer(drop_in_parts={1})

    result = compose_sections(_claims(80), SOURCE, model)

    assert result.ok
    assert result.fallback_sections == 1
    text = str(result.children)
    assert all(f"Fact number {i}." in text or f"prose for clm_{i:03d}" in text
               for i in range(80))


def test_a_short_source_is_still_one_call():
    model = _Composer()

    result = compose_sections(_claims(12), SOURCE, model)

    assert len(model.prompts) == 1
    assert "PART" not in model.prompts[0]
    assert result.ok


# ---------------------------------------------------------------------------
# extraction windows run at once
# ---------------------------------------------------------------------------


def test_extraction_windows_run_concurrently_and_merge_in_order():
    from palimpsest.extract import extract

    text = " ".join(f"Sentence {i} states a distinct fact about topic {i}." for i in range(2500))
    source = Source(source_id="src_2", kind="text", title="Long", text=text)
    in_flight, peak = [0], [0]
    lock = threading.Lock()

    class Model:
        model = "fake/extract-1"
        name = "fake"
        usage = Usage()

        def json(self, *, task, system, prompt, schema, effort="high",
                 cache_prefix=None, max_tokens=None):
            import re

            with lock:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
            time.sleep(0.05)
            with lock:
                in_flight[0] -= 1
            first = re.search(r"Sentence (\d+) states", prompt.split("SOURCE TEXT")[1])
            n = first.group(1)
            quote = f"Sentence {n} states a distinct fact about topic {n}."
            return {"claims": [{"text": quote, "quote": quote, "type": "fact",
                                "topics": ["t"], "confidence": 0.9}]}

    result = extract(source, Model())

    assert result.windows > 4
    assert peak[0] > 1, "windows were asked at the same time"
    starts = [c.anchor.start for c in result.claims]
    assert starts == sorted(starts), "and merged back in the order of the source"
