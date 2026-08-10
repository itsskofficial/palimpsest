"""Turn anything into a `Source`: normalised text plus anchors you can cite.

    from palimpsest.ingest import resolve
    source = resolve("https://arxiv.org/abs/2601.01828")
    source = resolve("lecture.pdf")
    source = resolve("https://youtu.be/dQw4w9WgXcQ")
    source = resolve("text:Some claim I want to record")

**Every adapter emits segments.** A segment is a span of the normalised text with a
citable locator: a page number, a video timestamp, a heading path, a spreadsheet cell.
The extractor maps each claim's character range back to the segment it fell in, which
is how a footnote ends up saying `14:22` and linking to that second of the video
instead of saying "source: that video".

This is the part every comparable tool skips, and skipping it is why their citations
are decoration. An anchor is the difference between a knowledge base you trust in two
years and one you stop opening.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from palimpsest.types import Anchor, Source, new_id

__all__ = ["anchor_for", "detect_kind", "resolve"]

_YOUTUBE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,})")


def detect_kind(spec: str) -> str:
    """Work out which adapter a spec wants, from its shape alone."""
    lowered = spec.lower()
    if lowered.startswith("text:"):
        return "text"
    if _YOUTUBE.search(spec):
        return "youtube"
    if lowered.startswith(("http://", "https://")):
        if lowered.split("?")[0].endswith(".pdf"):
            return "pdf"
        return "web"
    suffix = Path(spec).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return "image"
    if suffix in (".csv", ".tsv", ".xlsx", ".xls"):
        return "tabular"
    if suffix in (".md", ".markdown", ".txt", ".rst", ""):
        return "text"
    return "text"


def resolve(spec: str, *, kind: str | None = None, model=None,
            firecrawl_key: str | None = None, title: str | None = None) -> Source:
    """Normalise `spec` into a `Source`.

    `model` is only needed by adapters that cannot work without one (images). Passing
    it is optional everywhere else, and no adapter will silently spend tokens.
    """
    kind = kind or detect_kind(spec)

    if kind == "text":
        from palimpsest.ingest.files import from_text

        return from_text(spec, title=title)
    if kind == "web":
        from palimpsest.ingest.web import from_url

        return from_url(spec, firecrawl_key=firecrawl_key or os.environ.get("FIRECRAWL_API_KEY"))
    if kind == "youtube":
        from palimpsest.ingest.youtube import from_youtube

        return from_youtube(spec)
    if kind == "pdf":
        from palimpsest.ingest.files import from_pdf

        return from_pdf(spec)
    if kind == "image":
        from palimpsest.ingest.files import from_image

        return from_image(spec, model=model)
    if kind == "tabular":
        from palimpsest.ingest.files import from_tabular

        return from_tabular(spec)
    raise ValueError(f"no adapter for kind {kind!r}")


def anchor_for(source: Source, start: int, end: int) -> Anchor:
    """The citable anchor for a character range in a source's normalised text.

    Falls back to a plain offset when a source has no richer structure, so a claim is
    never left un-anchored — an un-anchored claim is one you cannot verify later, and
    the whole design treats that as a defect rather than a normal case.
    """
    segments = (source.meta or {}).get("segments") or []
    for seg in segments:
        if seg["start"] <= start < seg["end"]:
            return Anchor(
                kind=seg.get("kind", "offset"),
                locator=seg.get("locator", f"{start}-{end}"),
                start=start,
                end=end,
                url=seg.get("url") or source.url,
            )
    return Anchor(kind="offset", locator=f"chars {start}–{end}", start=start, end=end,
                  url=source.url)


def make_source(kind: str, title: str, text: str, *, url: str | None = None,
                segments: list[dict] | None = None, **meta) -> Source:
    """Shared constructor so every adapter produces the same shape."""
    return Source(
        source_id=new_id("src_"),
        kind=kind,
        title=title or (url or kind),
        text=text,
        url=url,
        meta={"segments": segments or [], **meta},
    )
