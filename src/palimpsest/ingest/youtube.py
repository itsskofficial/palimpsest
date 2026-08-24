"""YouTube: transcript with timestamps, so a footnote opens the video at the moment.

This is the adapter that best demonstrates why anchors matter. A claim you take from a
three-hour lecture is worthless six months later if the citation says "that lecture".
It is genuinely useful if it says `1:42:07` and the link jumps there.

Two paths, in order of fidelity:

1. **The timedtext endpoint.** YouTube serves caption tracks as JSON with per-cue
   start times. No key, no dependency, and the timings are exact.
2. **`youtube-transcript-api`, if installed.** More robust to YouTube's periodic
   reshuffling of the first path, and worth having if you ingest a lot of video.

When neither works the adapter says so plainly rather than returning a description of
the page — a transcript you did not get is not a source, and silently substituting one
would poison the base with claims nobody made.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from palimpsest.ingest import PASSAGE_CHARS, make_source, merge_cues
from palimpsest.types import Source

__all__ = ["PASSAGE_CHARS", "from_youtube", "video_id"]

log = logging.getLogger("palimpsest.ingest.youtube")

_ID = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,})")
USER_AGENT = "Mozilla/5.0 (compatible; palimpsest/0.1)"


def video_id(url: str) -> str:
    m = _ID.search(url)
    if not m:
        raise ValueError(f"not a YouTube URL: {url}")
    return m.group(1)


def _fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _cues_via_timedtext(vid: str) -> tuple[list[dict], str]:
    """Read the caption track straight off YouTube's timedtext endpoint."""
    page = _fetch(f"https://www.youtube.com/watch?v={vid}")
    title_m = re.search(r'<meta name="title" content="([^"]+)"', page)
    title = title_m.group(1) if title_m else f"YouTube {vid}"

    tracks = re.search(r'"captionTracks":(\[.*?\])', page)
    if not tracks:
        raise RuntimeError("no caption track advertised for this video")
    listing = json.loads(tracks.group(1).replace("\\u0026", "&"))
    if not listing:
        raise RuntimeError("caption track list is empty")

    # Prefer a manually written English track; fall back to whatever exists.
    chosen = next((t for t in listing
                   if t.get("languageCode", "").startswith("en")
                   and t.get("kind") != "asr"), None) or listing[0]
    base = chosen.get("baseUrl", "").replace("\\u0026", "&")
    if not base:
        raise RuntimeError("caption track has no URL")

    raw = _fetch(base + "&fmt=json3")
    events = json.loads(raw).get("events") or []
    cues: list[dict] = []
    for ev in events:
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if not text:
            continue
        cues.append({"start": (ev.get("tStartMs") or 0) / 1000.0, "text": text})
    if not cues:
        raise RuntimeError("caption track contained no text")
    return cues, title


def _cues_via_library(vid: str) -> tuple[list[dict], str]:  # pragma: no cover - optional
    from youtube_transcript_api import YouTubeTranscriptApi

    raw = YouTubeTranscriptApi.get_transcript(vid, languages=["en", "en-US", "en-GB"])
    return ([{"start": float(c["start"]), "text": c["text"]} for c in raw],
            f"YouTube {vid}")


def from_youtube(url: str) -> Source:
    """Fetch a transcript and normalise it into timestamped passages."""
    vid = video_id(url)
    canonical = f"https://www.youtube.com/watch?v={vid}"

    errors: list[str] = []
    cues: list[dict] = []
    title = f"YouTube {vid}"
    for attempt in (_cues_via_timedtext, _cues_via_library):
        try:
            cues, title = attempt(vid)
            break
        except Exception as e:
            errors.append(f"{attempt.__name__}: {e}")

    if not cues:
        raise RuntimeError(
            "could not get a transcript for this video.\n  " + "\n  ".join(errors) +
            "\nIf it has no captions, transcribe the audio first "
            "(Deepgram or Sarvam) and ingest the text."
        )

    # The deep link is the whole point: it opens the video at the second the claim was
    # made. Merging itself is shared with the pasted-transcript adapter, so a lecture
    # anchors the same way whether its captions were fetched or copied out of a panel.
    text, segments = merge_cues(
        cues, deep_link=lambda s: f"{canonical}&t={int(s)}s")

    return make_source("youtube", title, text, url=canonical, segments=segments,
                       video_id=vid, cues=len(cues),
                       duration_s=int(cues[-1]["start"]) if cues else None)
