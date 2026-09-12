"""The embedding cache, which is what makes dense retrieval affordable to keep on.

Embedding a workspace costs money once and nothing thereafter — *if* the cache works. If
it does not, the failure is silent in the worst way: every sync re-embeds every block,
retrieval keeps working perfectly, and the only symptom is a bill. So most of what
follows is about the cache being genuinely used and genuinely invalidated.

The other half is the packing. Vectors are stored as float32 bytes, a quarter the size of
JSON, and a mistake there does not raise — it returns numbers that are subtly wrong, and
retrieval quietly degrades with nothing anywhere reporting a problem.
"""

from __future__ import annotations

import math

import pytest

from palimpsest.embed import CachedEmbedder, available, pack, resolve, text_hash, unpack


class FakeInner:
    """A provider that counts what it was asked to embed."""

    model = "fake-embed-1"

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        # Deterministic and text-dependent, so a wrong pairing is visible.
        return [[float(len(t)), float(sum(map(ord, t[:4]))), 0.5] for t in texts]

    @property
    def embedded(self) -> int:
        return sum(len(c) for c in self.calls)


@pytest.fixture()
def inner():
    return FakeInner()


@pytest.fixture()
def cached(inner, store):
    store.put_pages([{"page_id": "pg_1", "title": "P", "last_edited": "x"}])
    store.put_blocks([{"block_id": f"bk_{i}", "page_id": "pg_1", "type": "paragraph",
                       "text": f"block {i}", "position": i} for i in range(4)])
    return CachedEmbedder(inner, store)


# ---------------------------------------------------------------------------
# packing
# ---------------------------------------------------------------------------


def test_a_vector_survives_being_packed_and_unpacked():
    vector = [0.5, -0.25, 0.125, 0.0]

    assert unpack(pack(vector)) == vector


def test_packing_is_float32_and_therefore_a_quarter_of_the_size():
    """The point of the format. JSON would be four times the rows for the same recall."""
    assert len(pack([0.0] * 100)) == 400


def test_a_realistic_vector_round_trips_within_float32_precision():
    """float32 is not float64 and small differences are expected. What must not happen
    is a value coming back as something else entirely — a wrong offset or endianness
    does not raise, it returns plausible nonsense."""
    vector = [math.sin(i / 7.0) for i in range(384)]

    back = unpack(pack(vector))

    assert len(back) == len(vector)
    assert all(abs(a - b) < 1e-6 for a, b in zip(vector, back, strict=True))


def test_an_empty_vector_round_trips_rather_than_raising():
    assert unpack(pack([])) == []


def test_the_text_hash_changes_when_the_text_does():
    """This is the invalidation. A hash that ignored an edit would serve the old block's
    vector forever, and retrieval would answer about text that is no longer there."""
    assert text_hash("the original text") != text_hash("the edited text")
    assert text_hash("same") == text_hash("same")
    assert text_hash("") == text_hash(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


def test_the_second_pass_over_unchanged_blocks_costs_nothing(cached, inner):
    """The whole reason the cache exists. Without it every sync re-embeds the entire
    workspace, retrieval still works, and the only symptom is the bill."""
    ids = ["bk_0", "bk_1", "bk_2"]
    texts = ["block 0", "block 1", "block 2"]

    first = cached.for_blocks(ids).embed(texts)
    second = cached.for_blocks(ids).embed(texts)

    assert first == second
    assert inner.embedded == 3, "the second pass must not reach the provider"
    assert cached.hits == 3
    assert cached.misses == 3


def test_only_the_blocks_that_changed_are_re_embedded(cached, inner):
    """A sync touches a handful of blocks out of thousands. Re-embedding all of them
    because one changed is the same cost as having no cache."""
    ids = ["bk_0", "bk_1", "bk_2"]
    cached.for_blocks(ids).embed(["block 0", "block 1", "block 2"])
    inner.calls.clear()

    cached.for_blocks(ids).embed(["block 0", "block 1 EDITED", "block 2"])

    assert inner.calls == [["block 1 EDITED"]], "only the edited block"


def test_an_edited_block_gets_the_new_vector_not_the_stale_one(cached, inner):
    """The dangerous half of invalidation: serving the old vector is not an error, it is
    an answer about text that is no longer on the page."""
    cached.for_blocks(["bk_0"]).embed(["short"])

    after = cached.for_blocks(["bk_0"]).embed(["a much longer piece of text"])

    assert after[0][0] == float(len("a much longer piece of text"))


def test_vectors_come_back_in_the_order_they_were_asked_for(cached, inner):
    """A partial cache hit splits the batch and reassembles it. Getting the reassembly
    wrong pairs every block with somebody else's vector — retrieval keeps working and
    returns confidently wrong pages."""
    ids = ["bk_0", "bk_1", "bk_2", "bk_3"]
    texts = ["aaaa", "bb", "cccccc", "d"]
    cached.for_blocks(["bk_1", "bk_3"]).embed(["bb", "d"])   # warm two of the four

    out = cached.for_blocks(ids).embed(texts)

    assert [v[0] for v in out] == [4.0, 2.0, 6.0, 1.0], "each vector with its own text"


def test_a_query_is_not_cached(cached, inner):
    """A query has no block id and will never be looked up again. Caching it fills the
    table with rows nothing reads."""
    first = cached.embed(["what did I write about attention?"])
    second = cached.embed(["what did I write about attention?"])

    assert first == second
    assert inner.embedded == 2, "queries go straight through"
    assert cached.hits == 0


def test_a_mismatched_id_list_is_passed_through_rather_than_mispaired(cached, inner):
    """Fewer ids than texts would otherwise zip short and write each vector under the
    wrong block — a cache that is confidently wrong forever."""
    out = cached.for_blocks(["bk_0"]).embed(["one", "two", "three"])

    assert len(out) == 3
    assert inner.calls == [["one", "two", "three"]]
    assert cached.hits == 0


def test_the_block_ids_are_consumed_by_one_call_only(cached, inner):
    """They are set positionally just before `embed`. Left set, the *next* call — very
    likely a query — would be cached against somebody else's block."""
    cached.for_blocks(["bk_0"]).embed(["block 0"])
    inner.calls.clear()

    cached.embed(["an unrelated query"])

    assert cached.hits == 0
    assert inner.calls == [["an unrelated query"]]


def test_the_cache_is_keyed_by_model_as_well_as_block(cached, inner, store):
    """Two models produce vectors of different dimension and meaning. Sharing a key
    would hand one model's vectors to the other, and cosine similarity on mixed spaces
    is noise that looks like a score."""
    cached.for_blocks(["bk_0"]).embed(["block 0"])

    other = CachedEmbedder(FakeInner(), store)
    other.model = "a-different-model"
    other.for_blocks(["bk_0"]).embed(["block 0"])

    assert other.hits == 0, "a different model must not read this model's cache"


def test_hits_and_misses_are_counted_so_the_cache_can_be_seen_to_work(cached):
    """A cache nobody can observe is a cache nobody notices has stopped working."""
    cached.for_blocks(["bk_0", "bk_1"]).embed(["block 0", "block 1"])
    cached.for_blocks(["bk_0", "bk_1"]).embed(["block 0", "block 1"])

    assert cached.misses == 2
    assert cached.hits == 2


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_no_embedding_provider_is_a_supported_state(monkeypatch):
    """Dense retrieval is optional: BM25 alone is the default and works with no key at
    all. Raising here would make a key mandatory for the whole pipeline."""
    from palimpsest.config import Settings

    settings = Settings(embed_provider=None, embed_api_key=None, openai_api_key=None)

    assert available(settings) is False
    assert resolve(settings) is None


def test_a_resolved_embedder_is_wrapped_in_the_cache_when_a_store_is_given(monkeypatch,
                                                                          store):
    """Resolving without the wrapper is how the cache silently stops being used: nothing
    fails, and every sync re-embeds the workspace."""
    from palimpsest.config import Settings

    settings = Settings(embed_provider="openai", embed_api_key="sk-x",
                        embed_model="text-embedding-3-small")

    embedder = resolve(settings, store=store)

    assert isinstance(embedder, CachedEmbedder)
    assert embedder.model == "text-embedding-3-small"


def test_without_a_store_the_provider_is_returned_unwrapped(monkeypatch):
    from palimpsest.config import Settings

    settings = Settings(embed_provider="openai", embed_api_key="sk-x")

    embedder = resolve(settings)

    assert embedder is not None
    assert not isinstance(embedder, CachedEmbedder)


def test_a_named_provider_and_an_embed_key_are_enough_on_their_own():
    """The exact pair the desktop settings screen offers. Honouring only the vendor's own
    variable meant filling both fields in the UI changed nothing, `status` still said
    "lexical only", and retrieval stayed on BM25 with nothing reporting why."""
    from palimpsest.config import Settings, describe_embedding

    setup = describe_embedding(Settings(embed_provider="together",
                                        embed_api_key="sk-x"))

    assert setup is not None
    assert setup.provider == "together"
    assert setup.model == "BAAI/bge-large-en-v1.5", "the provider's own default"
    assert "together" in setup.reason, "status prints this, so it has to say which"


def test_an_embed_key_with_no_provider_named_means_openai():
    """The default model and base URL are OpenAI's already, so the alternative to
    guessing here is ignoring a key somebody explicitly set."""
    from palimpsest.config import Settings, describe_embedding

    setup = describe_embedding(Settings(embed_api_key="sk-x"))

    assert setup is not None
    assert setup.provider == "openai"


def test_a_named_provider_with_no_key_anywhere_is_still_lexical_only():
    """Naming a provider is not having an account with it. Resolving here would build a
    client that 401s on every block of a sync — worse than the BM25 it replaced."""
    from palimpsest.config import Settings, describe_embedding

    assert describe_embedding(Settings(embed_provider="mistral")) is None


def test_a_local_runtime_still_wins_over_the_hosted_branch():
    """Ollama needs no key, and the hosted branch must not shadow the one configuration
    where embeddings are free."""
    from palimpsest.config import Settings, describe_embedding

    setup = describe_embedding(Settings(embed_provider="ollama"))

    assert setup is not None
    assert setup.provider == "ollama"
    assert setup.base_url == "http://127.0.0.1:11434/v1"


def test_each_provider_gets_its_own_default_model_rather_than_openai_s():
    """`embed_model` has a default rather than being empty, so taken literally every
    provider was asked for `text-embedding-3-small`. Only OpenAI serves it, and the
    failure is a 400 on every block of the first sync."""
    from palimpsest.config import Settings, describe_embedding

    for provider, expected in [("openai", "text-embedding-3-small"),
                               ("together", "BAAI/bge-large-en-v1.5"),
                               ("mistral", "mistral-embed")]:
        setup = describe_embedding(Settings(embed_provider=provider,
                                            embed_api_key="sk-x"))
        assert setup is not None and setup.model == expected, provider


def test_an_explicitly_chosen_model_is_never_overridden():
    """The sentinel must not swallow a real choice -- somebody who names a model gets it,
    including the OpenAI one on an OpenAI-compatible endpoint."""
    from palimpsest.config import Settings, describe_embedding

    setup = describe_embedding(Settings(embed_provider="together",
                                        embed_api_key="sk-x",
                                        embed_model="BAAI/bge-base-en-v1.5"))

    assert setup is not None
    assert setup.model == "BAAI/bge-base-en-v1.5"
