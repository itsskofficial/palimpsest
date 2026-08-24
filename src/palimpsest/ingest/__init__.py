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
from collections.abc import Callable
from pathlib import Path

from palimpsest.types import Anchor, Source, new_id

__all__ = ["anchor_for", "detect_kind", "merge_cues", "resolve", "timestamp"]

_YOUTUBE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,})")

#: Timestamped cues are merged into passages of about this many characters. A single
#: caption cue is too short for a claim to sit in, and a whole transcript is too coarse
#: to anchor — a citation of "somewhere in this three-hour lecture" is not a citation.
PASSAGE_CHARS = 900


def timestamp(seconds: float) -> str:
    """`1:42:07` for a long video, `4:12` for a short one."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def merge_cues(cues: list[dict], *, passage_chars: int = PASSAGE_CHARS,
               deep_link: Callable[[float], str | None] | None = None,
               ) -> tuple[str, list[dict]]:
    """Merge timestamped cues into readable passages, anchored per cue.

    Shared by every timestamped adapter — YouTube's fetched captions and a transcript
    pasted out of Udemy or Coursera — so that a claim taken from a lecture anchors the
    same way regardless of how the transcript was obtained. `deep_link` receives a time
    in seconds and returns a URL that opens the source there, or `None` when the
    platform has no such link.

    **Text is merged; anchors are not.** Caption cues are a few words long, which is too
    short for a claim to sit inside — so the text handed to the extractor is joined into
    passages of roughly `passage_chars`. But the *segments* stay one-per-cue, because
    they do a different job: they map a claim's character offset back to a moment. Emit
    them per passage instead and every claim in a nine-hundred-character span cites the
    timestamp of whatever was being said at the start of it, which for a dense lecture
    is a citation that is confidently off by a minute. Since a passage is built out of
    cues whose offsets are known anyway, per-cue anchoring is free.

    Returns the normalised text and the segments that index into it.
    """
    if not cues:
        return "", []

    parts: list[str] = []
    segments: list[dict] = []
    buffer: list[tuple[float, str]] = []
    cursor = 0

    def flush() -> None:
        nonlocal buffer, cursor
        if not buffer:
            return
        chunk = " ".join(text for _, text in buffer).strip() + "\n\n"
        # Walk the joined chunk to recover where each cue landed in the final text.
        offset = cursor
        for i, (start_s, text) in enumerate(buffer):
            body = text.strip()
            if not body:
                continue
            end = offset + len(body)
            segments.append({
                "start": offset, "end": end, "kind": "timestamp",
                "locator": timestamp(start_s),
                "url": deep_link(start_s) if deep_link else None,
            })
            # The single space `join` inserted, except after the final cue where the
            # paragraph break follows instead.
            offset = end + (1 if i < len(buffer) - 1 else 0)
        # The trailing "\n\n" belongs to the last cue, so a claim whose span runs to the
        # end of a passage still lands inside a segment rather than falling through to
        # the offset fallback.
        if segments:
            segments[-1]["end"] = cursor + len(chunk)

        parts.append(chunk)
        cursor += len(chunk)
        buffer = []

    for cue in cues:
        buffer.append((cue["start"], cue["text"]))
        if sum(len(text) for _, text in buffer) >= passage_chars:
            flush()
    flush()

    return "".join(parts).strip(), segments


def detect_kind(spec: str) -> str:
    """Work out which adapter a spec wants, from its shape alone."""
    lowered = spec.lower()
    if lowered.startswith("transcript:"):
        return "transcript"
    if lowered.startswith("text:"):
        return "text"
    if _YOUTUBE.search(spec):
        return "youtube"
    if lowered.startswith(("http://", "https://")):
        if lowered.split("?")[0].endswith(".pdf"):
            return "pdf"
        return "web"
    suffix = Path(spec).suffix.lower()
    if suffix in (".vtt", ".srt"):
        return "transcript"
    # Video containers land here too: the adapter sends the file to a speech-to-text
    # service, which reads the audio track and ignores the pictures.
    if suffix in (".mp3", ".m4a", ".wav", ".ogg", ".oga", ".flac", ".aac", ".wma",
                  ".opus", ".webm", ".mp4", ".mov", ".mkv", ".m4v", ".amr"):
        return "audio"
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
            firecrawl_key: str | None = None, title: str | None = None,
            url: str | None = None, settings=None) -> Source:
    """Normalise `spec` into a `Source`.

    `model` is only needed by adapters that cannot work without one (images). Passing
    it is optional everywhere else, and no adapter will silently spend tokens.

    `url` and `title` are what a caller knows but the text does not carry — the lecture
    a pasted transcript came from, for instance. Nothing infers them; a transcript with
    no URL still anchors to its timestamps, it just cannot deep-link.

    `settings` is consulted only by adapters with more configuration than one key —
    audio, which has three possible providers. Everything in it also has an environment
    fallback, so `resolve` stays usable on its own.
    """
    kind = kind or detect_kind(spec)

    if kind == "transcript":
        from palimpsest.ingest.transcript import from_transcript

        return from_transcript(spec, title=title, url=url)
    if kind == "audio":
        from palimpsest.ingest.audio import from_audio

        return from_audio(spec, title=title, url=url,
                          provider=getattr(settings, "transcribe_provider", None),
                          deepgram_key=getattr(settings, "deepgram_api_key", None),
                          groq_key=getattr(settings, "groq_api_key", None),
                          sarvam_key=getattr(settings, "sarvam_api_key", None))
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
