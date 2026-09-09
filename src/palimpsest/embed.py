"""Vectors, when you have somewhere to get them — and nothing breaks when you do not.

BM25 answers "did I write these words?" Embeddings answer "did I write this idea?", and
the gap between those two questions is exactly where a self-maintaining knowledge base
fails in a way you notice. You wrote *the CKA/RSA stuff*; the paper says *representational
similarity analysis*. Lexically those share nothing, so the claim is filed as `new`, a
seventh page about the same topic appears, and the thing that was supposed to stop your
notes fragmenting has caused it.

So this module exists, and it is **optional on purpose**. Retrieval without it is BM25,
which is genuinely good on technical notes and needs no key, no network and no provider.
`resolve()` returning `None` is an ordinary Tuesday, not a misconfiguration.

**Any OpenAI-compatible `/v1/embeddings` endpoint works** — OpenAI, a local Ollama, an
LM Studio server, Together, vLLM — for the same reason the chat layer takes any
compatible endpoint: a base URL, a key, a model name. Stdlib HTTP, no SDK.

**Vectors are cached in the store, not in the process.** Embedding is charged per token
and every process that builds an index would otherwise re-embed the same blocks: the CLI,
the queue worker, the server, each eval run. The cache is keyed by block *and* by the
hash of its text, so a block that was edited re-embeds rather than quietly serving the
vector of what it used to say — and by model, because two models' vectors are not
comparable and mixing them produces a similarity number that means nothing at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import urllib.error
import urllib.request
from typing import Any, Protocol

from palimpsest.config import EMBED_PROVIDERS, describe_embedding, model_getter

__all__ = ["CachedEmbedder", "Embedder", "OpenAIEmbedder", "available", "resolve"]

log = logging.getLogger("palimpsest.embed")

USER_AGENT = "palimpsest/0.1 (+https://github.com/itsskofficial/palimpsest)"

#: How many texts go in one request. Large enough to matter, small enough that a failure
#: does not throw away much work.
BATCH = 96


class Embedder(Protocol):
    """Anything that can turn texts into vectors."""

    model: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order."""
        ...


def text_hash(text: str) -> str:
    """What the cache is keyed on. Short: collisions here cost a stale vector, not data."""
    return hashlib.blake2b((text or "").encode("utf-8"), digest_size=12).hexdigest()


def pack(vector: list[float]) -> bytes:
    """Float32, little-endian. A quarter the size of JSON and exactly reversible."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class OpenAIEmbedder:
    """`POST /v1/embeddings` against anything that implements it."""

    def __init__(self, model: str, *, api_key: str | None,
                 base_url: str = "https://api.openai.com/v1", name: str = "openai",
                 timeout: float = 60.0, max_retries: int = 3) -> None:
        self.model = model
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._key = api_key

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            out.extend(self._batch(texts[start:start + BATCH]))
        return out

    def _batch(self, texts: list[str]) -> list[list[float]]:
        # An empty string is a 400 on most servers and a zero vector on the rest, so it
        # is replaced rather than sent. The caller filters short blocks anyway; this is
        # for the ones that are whitespace after normalisation.
        payload = json.dumps({
            "model": self.model,
            "input": [t if t.strip() else " " for t in texts],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"

        last: Exception | None = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(
                f"{self.base_url}/embeddings", data=payload, headers=headers,
                method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                if e.code < 500 and e.code not in (408, 429):
                    raise RuntimeError(f"{self.name} embeddings {e.code}: {detail}") from e
                last = RuntimeError(f"{self.name} embeddings {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError) as e:
                last = RuntimeError(f"could not reach {self.base_url}: {e}")
            import time as _time

            _time.sleep(min(2 ** attempt, 8))
        else:
            raise last or RuntimeError(f"{self.name}: embeddings request failed")

        # Sorted by index rather than trusted in order: the spec says the array may come
        # back in any order, and a silently permuted batch attaches every vector to the
        # wrong block — which looks like a bad embedding model, not like a bug.
        items = sorted(data.get("data") or [], key=lambda d: int(d.get("index", 0)))
        if len(items) != len(texts):
            raise RuntimeError(
                f"{self.name} returned {len(items)} vectors for {len(texts)} texts")
        return [list(item["embedding"]) for item in items]


class CachedEmbedder:
    """An embedder that asks the store first.

    Wraps any `Embedder`. Reads are keyed by block id and text hash; anything missing or
    stale goes to the real provider and is written back. Texts with no block id — a query,
    most importantly — go straight through, because caching a query would fill the table
    with rows nothing will ever look up again.
    """

    def __init__(self, inner: Embedder, store: Any) -> None:
        self.inner = inner
        self.store = store
        self.model = inner.model
        #: Set by the index before an `embed` call so this wrapper knows which block each
        #: text belongs to. Kept out of the `Embedder` signature so the protocol stays the
        #: one-method thing it should be.
        self.block_ids: list[str] = []
        self.hits = 0
        self.misses = 0

    def for_blocks(self, block_ids: list[str]) -> CachedEmbedder:
        """Declare the block ids of the next `embed` call, positionally."""
        self.block_ids = list(block_ids)
        return self

    def embed(self, texts: list[str]) -> list[list[float]]:
        ids = self.block_ids
        self.block_ids = []
        if not ids or len(ids) != len(texts):
            return self.inner.embed(texts)

        cached = self.store.get_embeddings(ids, self.model)
        wanted = [text_hash(t) for t in texts]
        out: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, (block_id, want) in enumerate(zip(ids, wanted, strict=True)):
            entry = cached.get(block_id)
            if entry and entry[0] == want:
                out[i] = unpack(entry[1])
                self.hits += 1
            else:
                missing.append(i)

        if missing:
            self.misses += len(missing)
            fresh = self.inner.embed([texts[i] for i in missing])
            rows = []
            for i, vector in zip(missing, fresh, strict=True):
                out[i] = vector
                rows.append({"block_id": ids[i], "text_hash": wanted[i],
                             "dim": len(vector), "vector": pack(vector)})
            self.store.put_embeddings(rows, self.model)

        return [v if v is not None else [] for v in out]


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def available(settings: Any = None) -> bool:
    """Whether vectors can be computed at all. False is a supported state."""
    return describe_embedding(settings) is not None


def resolve(settings: Any = None, *, store: Any = None) -> Embedder | None:
    """Build an embedder from configuration, or `None` if nothing can serve one.

    Returning `None` rather than raising is the whole posture of this module: the caller
    hands it to `Index(embedder=...)`, which reads `None` as "lexical only" and carries
    on. Nothing upstream branches on it.
    """
    setup = describe_embedding(settings)
    if setup is None:
        return None

    get = model_getter(settings)
    key = (get("PALIMPSEST_EMBED_API_KEY")
           or getattr(settings, "embed_api_key", None)
           or get(EMBED_PROVIDERS.get(setup.provider, ("PALIMPSEST_MODEL_API_KEY",))[0])
           or get("PALIMPSEST_MODEL_API_KEY"))
    inner = OpenAIEmbedder(setup.model, api_key=key or None,
                           base_url=setup.base_url or "https://api.openai.com/v1",
                           name=setup.provider)
    return CachedEmbedder(inner, store) if store is not None else inner
