"""Sweeps over the base you already have — before a single source is ingested.

These are what you run on day one. They read the mirror and tell you things about your
own notes that you cannot currently find out:

- **`duplicates`** — the same content on two different pages. This is the accumulated
  damage the product exists to stop, and finding it is the first thing it should do.
- **`contradictions`** — claims in your notes that disagree with *each other*. Not
  new-versus-old: old-versus-old. Your Notion almost certainly contains some right now
  and you do not know which. This is the demo that makes people's eyes widen.
- **`stale`** — pages whose cited sources have changed, or whose fast-moving facts have
  outlived their half-life.
- **`open_questions`** — what the base knows it does not know. This is the input to the
  "does its own homework" loop: unresolved questions become the next research queue.

`duplicates` needs no API key at all — it is BM25 over the mirror. That matters more
than it sounds: it means the product delivers something useful before you have given it
a model key, and before it has ever had permission to write.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from palimpsest.llm import Model, ModelError
from palimpsest.retrieve import Index

__all__ = ["SweepResult", "contradictions", "duplicates", "open_questions", "stale"]

log = logging.getLogger("palimpsest.sweep")

#: Half-life in days, by topic signal. A claim about model pricing goes stale fast; a
#: claim about linear algebra does not. Crude on purpose — the alternative is asking
#: the model to date every fact, which costs more than it is worth.
HALF_LIVES = (
    (re.compile(r"\b(pric|cost|\$|per (million|token)|rate limit|free tier)", re.I), 60),
    (re.compile(r"\b(api|endpoint|sdk|version|release|deprecat|changelog)", re.I), 120),
    (re.compile(r"\b(model|benchmark|sota|state of the art|leaderboard)", re.I), 180),
    (re.compile(r"\b(roadmap|plan|upcoming|beta|preview)", re.I), 90),
)
DEFAULT_HALF_LIFE = 730


@dataclass
class SweepResult:
    kind: str
    findings: list[dict] = field(default_factory=list)
    scanned: int = 0
    seconds: float = 0.0
    model_calls: int = 0
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.findings)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "findings": self.findings, "scanned": self.scanned,
                "seconds": round(self.seconds, 1), "model_calls": self.model_calls,
                "notes": self.notes}

    def summary(self) -> str:  # pragma: no cover - display only
        return (f"{self.kind}: {len(self.findings)} finding(s) over {self.scanned} "
                f"item(s) in {self.seconds:.1f}s"
                + (f", {self.model_calls} model call(s)" if self.model_calls else ""))


# ---------------------------------------------------------------------------
# duplicates — no key required
# ---------------------------------------------------------------------------


def duplicates(store, *, index: Index | None = None, threshold: float = 0.32,
               top: int = 50) -> SweepResult:
    """Blocks on different pages that say nearly the same thing.

    Runs on the lexical index alone, so this works with no model and no API key. The
    output is a merge queue: each finding names both blocks, both pages, and how
    similar they are, so you can decide which page should own the content.
    """
    started = time.perf_counter()
    index = index or Index(store)
    result = SweepResult(kind="duplicates", scanned=len(index))

    for left, right, ratio in index.near_duplicates(threshold=threshold)[:top]:
        result.findings.append({
            "similarity": round(ratio, 3),
            "a": {"block_id": left.block_id, "page_id": left.page_id,
                  "page_title": left.page_title, "text": left.text[:400]},
            "b": {"block_id": right.block_id, "page_id": right.page_id,
                  "page_title": right.page_title, "text": right.text[:400]},
            "suggestion": (f"keep one on “{left.page_title}” and link from "
                           f"“{right.page_title}”, or merge the pages"),
        })

    result.seconds = time.perf_counter() - started
    store.put_record("sweep_duplicates", result.as_dict(), label=str(len(result.findings)))
    return result


# ---------------------------------------------------------------------------
# contradictions — the one that surprises people
# ---------------------------------------------------------------------------

CONTRADICTION_SYSTEM = """\
You are auditing a personal knowledge base for internal contradictions — places where \
the notes disagree with THEMSELVES.

You will be shown pairs of related passages from different pages. For each pair, decide \
whether they genuinely conflict.

A genuine conflict means both statements cannot be true at once about the same thing at \
the same time.

NOT conflicts:
- Different levels of detail about the same fact.
- Statements about different systems, versions, or time periods (unless presented as \
timeless).
- One statement being an example or special case of the other.
- Different phrasings of the same idea.
- One passage being incomplete.

Be conservative. A false positive wastes the reader's time and teaches them to ignore \
this report. Report nothing rather than reaching."""

CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair_id": {"type": "integer"},
                    "kind": {"type": "string",
                             "enum": ["factual", "temporal", "scope", "terminology"]},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["pair_id", "kind", "explanation", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["conflicts"],
    "additionalProperties": False,
}


def contradictions(store, model: Model, *, index: Index | None = None,
                   max_pairs: int = 120, batch: int = 8,
                   min_similarity: float = 0.30) -> SweepResult:
    """Find claims in your notes that disagree with each other.

    Two stages, because asking the model about every pair of blocks would be
    quadratic and unaffordable. Retrieval first proposes *related* pairs — passages
    similar enough to be about the same thing — and the model only judges whether
    related means conflicting. That turns an O(n²) model bill into a linear one.
    """
    started = time.perf_counter()
    index = index or Index(store)
    result = SweepResult(kind="contradictions", scanned=len(index))

    # Stage 1: related-but-not-identical pairs. Identical pairs are duplicates, which
    # is a different sweep; the interesting zone is "clearly about the same thing,
    # phrased differently".
    pairs: list[tuple] = []
    seen: set[tuple[str, str]] = set()
    for left, right, ratio in index.near_duplicates(threshold=min_similarity, min_chars=80):
        if ratio > 0.92:
            continue
        a, b = sorted((left.block_id, right.block_id))
        key = (a, b)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((left, right, ratio))
        if len(pairs) >= max_pairs:
            break

    if not pairs:
        result.seconds = time.perf_counter() - started
        result.notes.append("no sufficiently related cross-page passages to compare")
        return result

    calls_before = model.usage.calls
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        rendered = []
        for i, (left, right, _) in enumerate(chunk):
            rendered.append(
                f"PAIR {i}\n"
                f"  A — page “{left.page_title}”:\n    {left.text[:700]}\n"
                f"  B — page “{right.page_title}”:\n    {right.text[:700]}"
            )
        try:
            payload = model.json(
                task="contradiction_sweep",
                system=CONTRADICTION_SYSTEM,
                prompt="\n\n".join(rendered) + "\n\nWhich pairs genuinely conflict?",
                schema=CONTRADICTION_SCHEMA,
                effort="high",
            )
        except ModelError as e:
            result.notes.append(f"batch at {start} failed: {e}")
            continue

        for conflict in payload.get("conflicts", []):
            idx = int(conflict.get("pair_id", -1))
            if not 0 <= idx < len(chunk):
                continue
            left, right, ratio = chunk[idx]
            result.findings.append({
                "kind": conflict.get("kind", "factual"),
                "confidence": float(conflict.get("confidence", 0.0)),
                "explanation": conflict.get("explanation", ""),
                "similarity": round(ratio, 3),
                "a": {"block_id": left.block_id, "page_id": left.page_id,
                      "page_title": left.page_title, "text": left.text[:500]},
                "b": {"block_id": right.block_id, "page_id": right.page_id,
                      "page_title": right.page_title, "text": right.text[:500]},
            })

    result.findings.sort(key=lambda f: f["confidence"], reverse=True)
    result.model_calls = model.usage.calls - calls_before
    result.seconds = time.perf_counter() - started
    store.put_record("sweep_contradictions", result.as_dict(),
                     label=str(len(result.findings)))
    return result


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------


def _half_life_days(text: str) -> int:
    for pattern, days in HALF_LIVES:
        if pattern.search(text):
            return days
    return DEFAULT_HALF_LIFE


def stale(store, *, now: float | None = None, top: int = 60) -> SweepResult:
    """Claims whose facts have probably moved on.

    Age alone is not staleness — a note on linear algebra written in 2019 is fine. What
    matters is age *relative to how fast that kind of fact changes*, so each claim gets
    a half-life from its topic and is flagged when it has outlived it.

    Needs no model and no key: it is arithmetic over the provenance ledger.
    """
    started = time.perf_counter()
    now = now or time.time()
    result = SweepResult(kind="stale")

    scanned = 0
    findings: list[dict] = []
    for page in store.get_pages():
        for block in store.get_blocks(page["page_id"]):
            provenance = store.provenance_for_block(block["block_id"])
            if not provenance:
                continue
            scanned += 1
            newest = max(p.get("created_at") or 0 for p in provenance)
            age_days = (now - newest) / 86400.0
            half_life = _half_life_days(block.get("text", ""))
            if age_days < half_life:
                continue
            ratio = age_days / half_life
            findings.append({
                "block_id": block["block_id"],
                "page_id": page["page_id"],
                "page_title": page.get("title", ""),
                "text": block.get("text", "")[:300],
                "age_days": round(age_days),
                "half_life_days": half_life,
                "staleness": round(ratio, 2),
                "sources": [{"title": p.get("source_title"), "url": p.get("source_url")}
                            for p in provenance][:3],
            })

    findings.sort(key=lambda f: f["staleness"], reverse=True)
    result.findings = findings[:top]
    result.scanned = scanned
    result.seconds = time.perf_counter() - started
    if not scanned:
        result.notes.append(
            "no blocks carry provenance yet — staleness is measured from when a claim "
            "entered the base, so this fills in as you ingest sources"
        )
    store.put_record("sweep_stale", result.as_dict(), label=str(len(result.findings)))
    return result


# ---------------------------------------------------------------------------
# open questions — the input to the homework loop
# ---------------------------------------------------------------------------

_QUESTION = re.compile(r"(\?\s*$)|^\s*(?:todo|tbd|q:|question:|unclear|not sure|\?\?)",
                       re.IGNORECASE | re.MULTILINE)


def open_questions(store, *, top: int = 80) -> SweepResult:
    """What the base knows it does not know.

    Question-shaped blocks, TODOs, and anything explicitly flagged uncertain. This is
    the queue the "base does its own homework" loop consumes: each entry becomes a
    research task, and answers come back as ordinary sources through the ordinary
    pipeline — with the same relations, the same review, the same provenance.

    No model needed: this is pattern matching over the mirror.
    """
    started = time.perf_counter()
    result = SweepResult(kind="open_questions")
    scanned = 0
    for page in store.get_pages():
        for block in store.get_blocks(page["page_id"]):
            text = (block.get("text") or "").strip()
            scanned += 1
            if len(text) < 12 or not _QUESTION.search(text):
                continue
            if block.get("type") == "to_do" and text.startswith("[x]"):
                continue
            result.findings.append({
                "block_id": block["block_id"],
                "page_id": page["page_id"],
                "page_title": page.get("title", ""),
                "text": text[:300],
                "type": block.get("type"),
            })
    result.scanned = scanned
    result.findings = result.findings[:top]
    result.seconds = time.perf_counter() - started
    store.put_record("sweep_open_questions", result.as_dict(),
                     label=str(len(result.findings)))
    return result
