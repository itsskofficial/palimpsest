"""Shared fixtures: a small, realistic mirror with the flaws the product exists to fix.

The fixture workspace is deliberately *bad* in specific, named ways — an exact duplicate
across two pages, a paraphrase of the same idea on a third, a stale price, an open
question, a hub page. Every test that claims a behaviour can then point at the flaw it
is supposed to catch, rather than at a corpus that happens to work.
"""

from __future__ import annotations

import pytest

from palimpsest.store import open_store
from palimpsest.types import Anchor, Claim, ClaimType, Source, new_id


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch):
    """Never let a developer's real `.env` or saved config leak into a test.

    `Settings.load()` reads a persisted config file so a set-up machine stays set up —
    which means, without this, a test on a machine that ran `palimpsest setup` would
    silently inherit a real Notion token and a real bot token, and a test asserting
    "nothing is configured" would instead make a live API call. Tests control their
    environment through `monkeypatch`; the config file is switched off for all of them.
    """
    monkeypatch.setattr("palimpsest.config.load_env_file", lambda *a, **k: 0)

ATTENTION = ("Scaled dot-product attention divides the logits by the square root of the "
             "key dimension, which keeps the gradient variance stable as the dimension "
             "grows.")
ATTENTION_PARAPHRASE = ("Attention scales the dot product by one over sqrt of the key "
                        "dimension so that gradients stay stable when the dimension is "
                        "large.")
ADAMW = ("AdamW decouples weight decay from the gradient update, which is the reason it "
         "generalises better than Adam with L2 regularisation.")
PRICE = ("Claude Opus 5 costs five dollars per million input tokens and twenty-five "
         "dollars per million output tokens on the first-party API.")
QUESTION = "Does the layer-9 result hold on Qwen2.5, or is it Llama-specific?"


@pytest.fixture()
def store():
    s = open_store("sqlite://:memory:")
    yield s
    s.close()


@pytest.fixture()
def mirror(store):
    """A four-page workspace containing the exact flaws the sweeps look for."""
    store.put_pages([
        {"page_id": "pg_attention", "title": "Attention", "role": "deep_dive",
         "last_edited": "2026-03-01T00:00:00Z", "url": "https://notion.so/attention"},
        {"page_id": "pg_transformers", "title": "Transformers", "role": "reference",
         "last_edited": "2026-03-02T00:00:00Z"},
        {"page_id": "pg_optim", "title": "Optimisers", "role": "reference",
         "last_edited": "2026-03-03T00:00:00Z"},
        {"page_id": "pg_index", "title": "ML index", "role": "hub",
         "last_edited": "2026-03-04T00:00:00Z"},
    ])
    store.put_blocks([
        # An exact duplicate across two pages — what `sweep duplicates` must find.
        {"block_id": "bk_att_1", "page_id": "pg_attention", "type": "paragraph",
         "text": ATTENTION, "position": 0},
        {"block_id": "bk_tr_1", "page_id": "pg_transformers", "type": "paragraph",
         "text": ATTENTION, "position": 0},
        # A paraphrase of the same idea on a third page.
        {"block_id": "bk_tr_2", "page_id": "pg_transformers", "type": "paragraph",
         "text": ATTENTION_PARAPHRASE, "position": 1},
        # Unrelated content, so retrieval has something to *not* match.
        {"block_id": "bk_opt_1", "page_id": "pg_optim", "type": "paragraph",
         "text": ADAMW, "position": 0},
        # A fact with a short half-life.
        {"block_id": "bk_opt_2", "page_id": "pg_optim", "type": "paragraph",
         "text": PRICE, "position": 1},
        # An open question — the input to the homework loop.
        {"block_id": "bk_att_2", "page_id": "pg_attention", "type": "paragraph",
         "text": QUESTION, "position": 1},
    ])
    store.put_links([("pg_index", "pg_attention", "bk_idx_1"),
                     ("pg_index", "pg_optim", "bk_idx_2")])
    return store


@pytest.fixture()
def source():
    return Source(source_id=new_id("src_"), kind="web",
                  title="A blog post about attention",
                  text=ATTENTION + " " + ADAMW,
                  url="https://example.com/attention",
                  meta={"segments": [{"start": 0, "end": 400, "kind": "section",
                                      "locator": "Introduction",
                                      "url": "https://example.com/attention"}]})


@pytest.fixture()
def claim(source):
    return Claim(claim_id=new_id("clm_"), text=ATTENTION, type=ClaimType.FACT,
                 topics=("attention", "transformers"), confidence=0.95,
                 anchor=Anchor("section", "Introduction", 0, 140,
                               "https://example.com/attention"),
                 source_id=source.source_id)
