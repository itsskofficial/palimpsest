"""Pasted transcripts, with their timestamps kept.

Some of the best material is behind a login. Udemy and Coursera will not let a fetcher
near a lecture, but they will happily show you a transcript panel you can select and
copy. This adapter is the route in for that copy — and, equally, for a `.vtt` or `.srt`
you exported from somewhere else.

**The point is that it does not become flat text.** A transcript pasted as `text:` is
still ingestible, but every claim drawn from it anchors to a character offset, which is
worthless six months later: "chars 12400–12600 of a three-hour lecture" tells you
nothing you can act on. This adapter recovers the timestamps that were sitting in the
paste all along, so a claim from that lecture cites `1:42:07` — the same anchor quality
the YouTube adapter gets from fetched captions, through the same `merge_cues` path.

Four shapes are understood, because the four are what you actually get when you copy:

    WEBVTT / SRT      00:00:15.000 --> 00:00:18.000
    inline            0:15  the model was trained on...
    bracketed         [0:15] the model was trained on...
    stacked           0:15
                      the model was trained on...

The last is Udemy's and Coursera's, and it is the one a naive line-based parser gets
wrong: the timestamp and the words it labels are on different lines, so a parser that
requires them together silently produces zero cues and the paste degrades to flat text
with no error to explain it.

When a paste genuinely carries no timestamps, it falls back to paragraph anchors rather
than pretending. `¶ 12` is a weaker citation than `1:42:07`, but it is an honest one,
and an un-anchored claim is what this project exists to keep out of your notes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from palimpsest.ingest import make_source, merge_cues
from palimpsest.types import Source

__all__ = ["from_transcript", "parse_cues"]

log = logging.getLogger("palimpsest.ingest.transcript")

#: `00:00:15.000 --> 00:00:18.000` (WebVTT) or `,000` (SRT). Both in one pattern
#: because the only difference is the decimal separator.
_ARROW = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]?\d{0,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]?\d{0,3})"
)

#: A leading timestamp, optionally bracketed, with whatever follows it on the line.
#: The `(?:\d{1,2}:)?` group is the hours, absent in most lectures and present in long
#: ones — `12:34` is twelve minutes, `1:12:34` is over an hour, and reading the first
#: as hours would put every anchor in the wrong place.
_LEADING = re.compile(
    r"^\s*[\[(]?\s*(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})(?:[.,]\d{1,3})?"
    r"\s*[\])]?\s*(?P<text>.*)$"
)

#: Noise lines in a copied transcript panel. Cheap to drop, and each one that survives
#: becomes a claim about the user interface rather than the lecture.
_NOISE = re.compile(
    r"^\s*(WEBVTT|NOTE\b|Kind:|Language:|\d+\s*$|Transcript|Search in video|"
    r"Auto-?scroll|Download transcript|Show more|Collapse|English\b.*\(auto)",
    re.IGNORECASE,
)


def _seconds(h: str | None, m: str, s: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def _clock(raw: str) -> float:
    """`00:01:15.500` or `01:15` into seconds."""
    stamp = raw.replace(",", ".").strip()
    head, _, frac = stamp.partition(".")
    parts = [int(p) for p in head.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    total = parts[0] * 3600 + parts[1] * 60 + parts[2]
    return total + (float(f"0.{frac}") if frac.isdigit() else 0.0)


def _cues_from_arrows(lines: list[str]) -> list[dict]:
    """WebVTT and SRT: a timing line, then the caption until the next blank line."""
    cues: list[dict] = []
    current: dict | None = None
    for line in lines:
        match = _ARROW.search(line)
        if match:
            if current and current["text"].strip():
                cues.append(current)
            current = {"start": _clock(match.group("start")), "text": ""}
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            if current["text"].strip():
                cues.append(current)
                current = None
            continue
        if _NOISE.match(stripped):
            continue
        # WebVTT allows inline markup like <v Speaker> and <00:00:01.000>.
        current["text"] += (" " if current["text"] else "") + re.sub(r"<[^>]+>", "", stripped)
    if current and current["text"].strip():
        cues.append(current)
    return cues


def _cues_from_lines(lines: list[str]) -> list[dict]:
    """Inline, bracketed and stacked timestamps.

    A timestamp with text after it opens a cue and fills it. A timestamp *alone* on its
    line opens a cue that the following lines fill — which is the Udemy and Coursera
    case, and the reason this cannot simply match timestamp-and-text per line.
    """
    cues: list[dict] = []
    current: dict | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = _LEADING.match(stripped)
        if match:
            if current and current["text"].strip():
                cues.append(current)
            current = {"start": _seconds(match.group("h"), match.group("m"),
                                        match.group("s")),
                       "text": match.group("text").strip()}
            continue
        if _NOISE.match(stripped):
            continue
        if current is None:
            # Prose before the first timestamp — usually a lecture title. Keep it as a
            # cue at zero so it is not silently dropped from the text.
            current = {"start": 0.0, "text": stripped}
            continue
        current["text"] += (" " if current["text"] else "") + stripped
    if current and current["text"].strip():
        cues.append(current)
    return cues


def parse_cues(body: str) -> list[dict]:
    """Timestamped cues from a transcript in any of the four supported shapes.

    Returns `[]` when the text carries no timestamps at all, which the caller treats as
    a fallback rather than an error — plenty of useful transcripts are just prose.
    """
    lines = body.splitlines()
    cues = _cues_from_arrows(lines) if _ARROW.search(body) else _cues_from_lines(lines)
    # A single cue covering everything is not an anchor, it is the absence of one.
    return cues if len(cues) > 1 else []


def from_transcript(spec: str, *, title: str | None = None,
                    url: str | None = None) -> Source:
    """A pasted transcript, a `transcript:` string, or a `.vtt` / `.srt` file.

    `url` is the lecture the transcript came from. It cannot be inferred — nothing in a
    copied transcript names its source — so a caller that knows it should pass it, and
    the browser extension does exactly that from the tab it captured.
    """
    body = spec[len("transcript:"):] if spec.lower().startswith("transcript:") else spec

    path = Path(body[:260]) if "\n" not in body[:260] else None
    if path is not None and path.suffix.lower() in (".vtt", ".srt"):
        try:
            if path.exists():
                body = path.read_text(encoding="utf-8", errors="replace")
                title = title or path.stem
        except OSError as e:  # pragma: no cover - unreadable path
            raise RuntimeError(f"could not read transcript {path}: {e}") from e

    body = body.strip()
    if not body:
        raise ValueError("the transcript is empty")

    cues = parse_cues(body)
    if not cues:
        # Honest degradation: paragraph anchors, and a note in the metadata saying so,
        # rather than a timestamp that was never in the source.
        from palimpsest.ingest.web import segments_from_markdown

        log.info("no timestamps found in transcript; falling back to paragraph anchors")
        return make_source("transcript", title or "Transcript", body, url=url,
                           segments=segments_from_markdown(body), timestamped=False)

    def deep_link(seconds: float) -> str | None:
        """Most gated players do not accept a time parameter, so the locator carries
        the timestamp and the link opens the lecture. Guessing a `?t=` that the
        platform ignores would produce a citation that looks precise and is not."""
        return url

    text, segments = merge_cues(cues, deep_link=deep_link)
    return make_source("transcript", title or "Transcript", text, url=url,
                       segments=segments, timestamped=True, cues=len(cues),
                       duration_s=int(cues[-1]["start"]))
