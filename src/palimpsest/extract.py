"""Turn a source into atomic claims, each anchored to where it came from.

Atomicity is the point. "Llama-3-8B reproduces the effect at 20% at layer 9, matching
Anthropic's figure" is three claims — a reproduction result, a layer, and a
correspondence — and they can have three different relationships to what you already
wrote. Any pipeline that treats that sentence as one unit has to pick a single relation
for it, and will pick the wrong one at least a third of the time.

Two properties are enforced rather than requested:

**Every claim carries a verbatim quote.** The model returns the exact span it drew the
claim from, and we locate that span in the text ourselves to compute the anchor. Asking
the model for character offsets directly does not work — models are poor at counting
characters, and a fabricated offset produces a citation pointing at the wrong sentence,
which is worse than none. Searching for a quote we can verify is cheap and correct.

**Claims that cannot be located are dropped, loudly.** If the quote is not in the
source, the claim is a paraphrase or an invention. It gets counted in
`ExtractionResult.unanchored` and discarded. A claim with no verifiable origin is
precisely the thing this project exists to keep out of your notes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from palimpsest.ingest import anchor_for
from palimpsest.llm import Model, ModelError
from palimpsest.types import Claim, ClaimType, Source, new_id

__all__ = ["ExtractionResult", "extract"]

log = logging.getLogger("palimpsest.extract")

#: Characters of source text per extraction call. Large enough that claims spanning a
#: few paragraphs are visible; small enough that a 3-hour transcript does not become
#: one enormous request whose recall collapses in the middle.
WINDOW = 12_000
OVERLAP = 800

SYSTEM = """\
You extract atomic factual claims from source material for a personal knowledge base.

A claim is ONE assertion that can stand alone and be judged true or false. Split \
compound sentences: "X is 20% at layer 9, matching Y" contains a measurement, a \
location, and a correspondence — three claims.

Rules:
- Extract only what the source actually asserts. Never infer, extrapolate or add \
context from your own knowledge.
- `quote` MUST be copied character-for-character from the source text. It is used to \
locate the claim for citation; a paraphrase makes the claim unusable and it will be \
discarded.
- Prefer specific over general. "The learning rate was 3e-4" beats "they tuned \
hyperparameters".
- Keep numbers, units, model names, dates and proper nouns exactly as written.
- Skip navigation, boilerplate, adverts, author bios and calls to action.
- Skip pure opinion unless it is a substantive position worth recording; if you keep \
it, mark it `opinion`.
- If the source poses an open question worth tracking, record it as `question`.
- `topics` are 1-4 short lowercase subject tags used to find related notes later — \
prefer the vocabulary the source itself uses.
- `confidence` reflects how clearly the source states it: 1.0 for an explicit \
statement, lower for something hedged or implied.

Return no claims at all rather than padding with weak ones. An empty result is a \
valid, useful answer."""

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quote": {"type": "string"},
                    "type": {"type": "string", "enum": [t.value for t in ClaimType]},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["text", "quote", "type", "topics", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


@dataclass
class ExtractionResult:
    claims: list[Claim] = field(default_factory=list)
    windows: int = 0
    proposed: int = 0
    unanchored: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.claims)

    def as_dict(self) -> dict:
        return {"claims": len(self.claims), "windows": self.windows,
                "proposed": self.proposed, "unanchored": self.unanchored,
                "duplicates": self.duplicates, "errors": self.errors}

    def summary(self) -> str:  # pragma: no cover - display only
        bits = [f"{len(self.claims)} claim(s) from {self.windows} window(s)"]
        if self.duplicates:
            bits.append(f"{self.duplicates} duplicate")
        if self.unanchored:
            bits.append(f"{self.unanchored} discarded (quote not found)")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        return ", ".join(bits)


def _windows(text: str, size: int = WINDOW, overlap: int = OVERLAP) -> list[tuple[int, str]]:
    """Split into overlapping windows, preferring paragraph boundaries.

    The overlap is what stops a claim that straddles a window boundary from being lost
    — the same span is seen twice and deduplicated afterwards, which is much cheaper
    than missing it.
    """
    if len(text) <= size:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            window = text[start:end]
            for sep in ("\n\n", "\n", ". "):
                cut = window.rfind(sep)
                if cut > size * 0.6:
                    end = start + cut + len(sep)
                    break
        out.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return out


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _locate(quote: str, text: str, offset: int) -> tuple[int, int] | None:
    """Find a quote in the source, tolerating whitespace differences.

    Exact match first, then a whitespace-insensitive scan. Models reflow line breaks
    when quoting from a PDF, which would defeat an exact-only match and throw away
    perfectly good claims.
    """
    if not quote:
        return None
    idx = text.find(quote)
    if idx >= 0:
        return offset + idx, offset + idx + len(quote)

    flexible = re.escape(quote.strip())
    flexible = re.sub(r"(\\\s)+", r"\\s+", flexible)
    flexible = re.sub(r"\\\s\+", r"\\s+", flexible)
    try:
        m = re.search(flexible, text, re.IGNORECASE)
    except re.error:  # pragma: no cover - pathological quote
        return None
    if m:
        return offset + m.start(), offset + m.end()
    return None


def extract(source: Source, model: Model, *, effort: str = "medium",
            max_windows: int | None = None) -> ExtractionResult:
    """Extract anchored claims from a source."""
    result = ExtractionResult()
    if not source.text.strip():
        return result

    windows = _windows(source.text)
    if max_windows:
        windows = windows[:max_windows]
    result.windows = len(windows)

    # The source's identity is stable across windows, so it goes in the cached prefix.
    prefix = (f"SOURCE\ntitle: {source.title}\nkind: {source.kind}\n"
              f"url: {source.url or '(local)'}\n")

    seen: set[str] = set()
    for offset, window in windows:
        try:
            payload = model.json(
                task="extract",
                system=SYSTEM,
                prompt=f"{prefix}\nSOURCE TEXT\n{window}\n\nExtract the atomic claims.",
                schema=SCHEMA,
                effort=effort,
            )
        except ModelError as e:
            result.errors.append(str(e))
            continue

        for raw in payload.get("claims", []):
            result.proposed += 1
            text = (raw.get("text") or "").strip()
            if not text:
                continue

            key = _normalise(text)
            if key in seen:
                # Expected: the overlap between windows shows some spans twice.
                result.duplicates += 1
                continue

            span = _locate((raw.get("quote") or "").strip(), window, offset)
            if span is None:
                # The quote is not in the source, so the claim is a paraphrase or an
                # invention. Dropping it is the whole point.
                result.unanchored += 1
                log.debug("dropping unanchored claim: %s", text[:80])
                continue

            seen.add(key)
            try:
                claim_type = ClaimType(raw.get("type", "fact"))
            except ValueError:
                claim_type = ClaimType.FACT

            result.claims.append(Claim(
                claim_id=new_id("clm_"),
                text=text,
                type=claim_type,
                topics=tuple(t.strip().lower() for t in (raw.get("topics") or [])[:4]
                             if t and t.strip()),
                confidence=max(0.0, min(1.0, float(raw.get("confidence", 1.0)))),
                anchor=anchor_for(source, span[0], span[1]),
                source_id=source.source_id,
            ))

    return result
