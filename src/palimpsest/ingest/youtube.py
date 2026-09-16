"""YouTube: transcript with timestamps, so a footnote opens the video at the moment.

This is the adapter that best demonstrates why anchors matter. A claim you take from a
three-hour lecture is worthless six months later if the citation says "that lecture".
It is genuinely useful if it says `1:42:07` and the link jumps there.

Three paths to a transcript, in order:

1. **yt-dlp, if installed** (the `youtube` extra). YouTube changes how captions are
   served often enough that a hand-written client stops working without warning — the
   plain timedtext endpoint began returning empty bodies to anything that is not a
   browser, which broke every YouTube capture silently. yt-dlp is maintained against
   exactly that churn, so it goes first.
2. **The timedtext endpoint.** No dependency, and still right when it works.
3. **`youtube-transcript-api`, if installed.**

When none works the adapter says so plainly rather than returning a description of the
page — a transcript you did not get is not a source, and silently substituting one would
poison the base with claims nobody made.

**Playlists** are expanded, not ingested. Each video becomes its own source with its own
title, URL and timestamps, because a citation that says "somewhere in a ten-video course"
is not a citation. `playlist_videos` lists them; the capture queue turns each into a job.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from palimpsest.ingest import PASSAGE_CHARS, make_source, merge_cues
from palimpsest.types import Source

__all__ = ["MAX_PLAYLIST_VIDEOS", "PASSAGE_CHARS", "from_youtube", "is_playlist",
           "playlist_id", "playlist_videos", "video_id"]

log = logging.getLogger("palimpsest.ingest.youtube")

_ID = re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/"
                 r"|youtube\.com/live/)([\w-]{6,})")
_PLAYLIST = re.compile(r"youtube\.com/playlist\?(?:.*&)?list=([\w-]+)")
USER_AGENT = "Mozilla/5.0 (compatible; palimpsest/0.2)"

#: A playlist larger than this is cut off, and the result says so. Every video costs an
#: extraction and a classification per claim, so a 900-video channel archive sent by
#: accident is a bill, not a capture.
MAX_PLAYLIST_VIDEOS = 200

#: What yt-dlp reports for an entry that exists in the list but cannot be watched.
_UNAVAILABLE = ("[Private video]", "[Deleted video]")


def video_id(url: str) -> str:
    m = _ID.search(url)
    if not m:
        raise ValueError(f"not a YouTube URL: {url}")
    return m.group(1)


def playlist_id(url: str) -> str | None:
    """The playlist a URL names, when it names one on its own.

    Only `youtube.com/playlist?list=…` counts. A `watch?v=…&list=…` link is what the Share
    button produces for one video that happens to be in a playlist, and expanding it would
    turn "save this video" into "ingest the whole course".
    """
    m = _PLAYLIST.search(url)
    return m.group(1) if m else None


def is_playlist(url: str) -> bool:
    return playlist_id(url) is not None


def _fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _cues_from_json3(raw: str) -> list[dict]:
    """YouTube's `json3` caption format, as timed cues."""
    events = json.loads(raw).get("events") or []
    cues: list[dict] = []
    for ev in events:
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
        if text:
            cues.append({"start": (ev.get("tStartMs") or 0) / 1000.0, "text": text})
    return cues


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------


def _ytdlp(**options: Any):
    import yt_dlp

    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                             **options})


def _pick_track(info: dict) -> dict | None:
    """The best caption track: written by a person, in the video's language, as json3.

    Automatic captions are the fallback rather than the default because they drop
    punctuation and mishear names, and a claim's `quote` has to match the text exactly.
    `-orig` is YouTube's name for the auto track in the language actually spoken, as
    opposed to one machine-translated from it.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    spoken = (info.get("language") or "en").split("-")[0]

    def english(tracks: dict) -> list[str]:
        return [k for k in tracks if k == "en" or k.startswith("en-")]

    preferences = [
        (manual, [k for k in manual if k.split("-")[0] == spoken]),
        (manual, english(manual)),
        (auto, [f"{spoken}-orig", spoken]),
        (auto, ["en-orig", "en"]),
        (manual, list(manual)),
    ]
    for tracks, languages in preferences:
        for language in languages:
            for fmt in tracks.get(language) or []:
                if fmt.get("ext") == "json3" and fmt.get("url"):
                    return fmt
    return None


def _cues_via_ytdlp(vid: str) -> tuple[list[dict], str, dict]:
    with _ytdlp() as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        track = _pick_track(info)
        if track is None:
            raise RuntimeError("the video has no captions in any language")
        with ydl.urlopen(track["url"]) as response:
            raw = response.read().decode("utf-8", "replace")
    cues = _cues_from_json3(raw)
    if not cues:
        raise RuntimeError("caption track contained no text")
    meta = {k: info.get(k) for k in ("channel", "upload_date", "duration", "language")
            if info.get(k) is not None}
    return cues, info.get("title") or f"YouTube {vid}", meta


# ---------------------------------------------------------------------------
# the fallbacks
# ---------------------------------------------------------------------------


def _cues_via_timedtext(vid: str) -> tuple[list[dict], str, dict]:
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
    if not raw.strip():
        # What YouTube now sends a client it does not recognise. Parsing it produced a
        # JSONDecodeError that read like a bug in this module.
        raise RuntimeError("YouTube returned an empty caption response")
    cues = _cues_from_json3(raw)
    if not cues:
        raise RuntimeError("caption track contained no text")
    return cues, title, {}


def _cues_via_library(vid: str) -> tuple[list[dict], str, dict]:  # pragma: no cover
    from youtube_transcript_api import YouTubeTranscriptApi

    raw = YouTubeTranscriptApi.get_transcript(vid, languages=["en", "en-US", "en-GB"])
    return ([{"start": float(c["start"]), "text": c["text"]} for c in raw],
            f"YouTube {vid}", {})


def from_youtube(url: str) -> Source:
    """Fetch a transcript and normalise it into timestamped passages."""
    if is_playlist(url):
        raise ValueError(
            "that is a playlist, and a playlist is several sources rather than one. Send "
            "it through the capture queue — the app, the bot and the extension all do — "
            "and each video is ingested on its own, with its own citations.")

    vid = video_id(url)
    canonical = f"https://www.youtube.com/watch?v={vid}"

    errors: list[str] = []
    cues: list[dict] = []
    title = f"YouTube {vid}"
    meta: dict = {}
    missing_ytdlp = False
    for attempt in (_cues_via_ytdlp, _cues_via_timedtext, _cues_via_library):
        try:
            cues, title, meta = attempt(vid)
            break
        except ImportError:
            errors.append(f"{attempt.__name__}: not installed")
            missing_ytdlp = missing_ytdlp or attempt is _cues_via_ytdlp
        except Exception as e:
            errors.append(f"{attempt.__name__}: {e}")

    if not cues:
        hint = ("\nIf it has no captions, transcribe the audio first (Deepgram or Sarvam) "
                "and ingest the text.")
        if missing_ytdlp:
            # Both can be true, and the extra is the likelier fix: without it the only
            # remaining path is the endpoint YouTube stopped answering.
            hint = ("\nInstall the YouTube extra, which tracks YouTube's changes: "
                    "pip install 'palimpsest-notion[youtube]'" + hint)
        raise RuntimeError("could not get a transcript for this video.\n  "
                           + "\n  ".join(errors) + hint)

    # The deep link is the whole point: it opens the video at the second the claim was
    # made. Merging itself is shared with the pasted-transcript adapter, so a lecture
    # anchors the same way whether its captions were fetched or copied out of a panel.
    text, segments = merge_cues(
        cues, deep_link=lambda s: f"{canonical}&t={int(s)}s")

    return make_source("youtube", title, text, url=canonical, segments=segments,
                       video_id=vid, cues=len(cues),
                       duration_s=int(meta.get("duration") or cues[-1]["start"]),
                       **{k: v for k, v in meta.items() if k != "duration"})


# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------


def playlist_videos(url: str, *, limit: int = MAX_PLAYLIST_VIDEOS) -> dict:
    """List the watchable videos in a playlist, in playlist order.

    Returns `{"title", "url", "videos": [{"id", "title", "url", "duration"}], "total",
    "skipped", "truncated"}`. Needs yt-dlp: YouTube's playlist pages are rendered from a
    data blob whose shape changes, and a parser for this month's shape is next month's
    silent empty list.
    """
    pid = playlist_id(url)
    if pid is None:
        raise ValueError(f"not a YouTube playlist URL: {url}")
    if pid.startswith("RD"):
        raise ValueError(
            "that is a YouTube Mix, which is generated for each viewer and never ends. "
            "Save the videos you want to a playlist and send that instead.")
    try:
        ydl_context = _ytdlp(extract_flat="in_playlist")
    except ImportError as e:
        raise RuntimeError(
            "expanding a playlist needs the YouTube extra: "
            "pip install 'palimpsest-notion[youtube]'") from e

    canonical = f"https://www.youtube.com/playlist?list={pid}"
    with ydl_context as ydl:
        info = ydl.extract_info(canonical, download=False)

    videos: list[dict] = []
    skipped = 0
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id") or entry.get("title") in _UNAVAILABLE:
            skipped += 1
            continue
        videos.append({"id": entry["id"],
                       "title": entry.get("title") or f"YouTube {entry['id']}",
                       "url": f"https://www.youtube.com/watch?v={entry['id']}",
                       "duration": entry.get("duration")})
    if not videos:
        raise RuntimeError("that playlist has no videos that can be watched")

    return {"title": info.get("title") or f"Playlist {pid}", "url": canonical,
            "videos": videos[:limit], "total": len(videos), "skipped": skipped,
            "truncated": len(videos) > limit}
