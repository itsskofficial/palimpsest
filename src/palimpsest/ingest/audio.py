"""Recordings: lectures, meetings, voice memos — transcribed, with the clock kept.

An audio file is the one source type that cannot be read at all without a service, so
this is the only adapter that hard-fails when it has no key. That is deliberate. The
tempting alternative is to fall back to *something* — the filename, a description, an
empty string — and every one of those puts a claim nobody made into your notes. A
transcript you did not get is not a source.

**The timestamps are the point, not a bonus.** A ninety-minute meeting recording yields
a claim you will want to check in six months, and "somewhere in that recording" is not a
citation. Every provider here returns timed segments, which go through the same
`merge_cues` path as YouTube captions and pasted Udemy transcripts — so a claim from a
voice memo cites `14:22` exactly like one from a lecture.

Three providers, chosen for different reasons:

- **Deepgram** is the default. Best accuracy on long-form speech, no practical file-size
  ceiling, word-level timings, and it can tell speakers apart — which matters for
  meetings, where "who said this" is often the reason you recorded it.
- **Groq** runs Whisper large-v3 and is the cheapest way to start, since it needs no
  account you do not already have. It caps uploads at 25 MB, which is about an hour of
  compressed speech.
- **Sarvam** is for Indian languages and Hinglish, where Whisper and Deepgram both
  degrade badly on code-switching. It is the right call for a lecture that drifts
  between English and Hindi mid-sentence.

Selection is by key: whichever is configured, in that order, unless
`PALIMPSEST_TRANSCRIBE` names one. No provider is ever called without its own key.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from palimpsest.ingest import make_source, merge_cues
from palimpsest.types import Source

__all__ = ["PROVIDERS", "from_audio", "transcribe"]

log = logging.getLogger("palimpsest.ingest.audio")

PROVIDERS = ("deepgram", "groq", "sarvam")

#: Groq rejects anything larger. Deepgram does not care, and a 90-minute lecture is
#: comfortably over this, so the error has to name the fix rather than just the limit.
GROQ_MAX_BYTES = 25 * 1024 * 1024

_ENV_KEYS = {
    "deepgram": "DEEPGRAM_API_KEY",
    "groq": "GROQ_API_KEY",
    "sarvam": "SARVAM_API_KEY",
}

#: Sent on every request. `urllib` otherwise identifies itself as `Python-urllib/3.x`,
#: which Groq's CDN rejects outright with a Cloudflare 1010 — a 403 that looks exactly
#: like a bad API key and sends you off checking the wrong thing.
USER_AGENT = "palimpsest/0.1 (+https://github.com/itsskofficial/palimpsest)"


def _media_type(path: Path) -> str:
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "audio/mpeg"


def _multipart(fields: dict[str, str], filename: str, data: bytes,
               media_type: str, file_field: str = "file") -> tuple[bytes, str]:
    """Build a multipart body without pulling in `requests`.

    The rest of this project speaks HTTP over `urllib` for the same reason: the surface
    needed is small, and a dependency here would be one more thing between you and a
    working install.
    """
    boundary = f"----palimpsest{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n").encode()
    out += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n").encode()
    out += data
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{url.split('/')[2]} returned {e.code}: {detail}") from e


# ---------------------------------------------------------------------------
# providers — each returns [{"start": seconds, "text": str}]
# ---------------------------------------------------------------------------


def _deepgram(data: bytes, media_type: str, key: str, *, language: str | None,
              diarise: bool, timeout: float) -> list[dict]:
    params = ["model=nova-3", "smart_format=true", "punctuate=true", "paragraphs=true"]
    if diarise:
        params.append("diarize=true")
    if language:
        params.append(f"language={language}")
    payload = _post(f"https://api.deepgram.com/v1/listen?{'&'.join(params)}",
                    data, {"Authorization": f"Token {key}",
                           "Content-Type": media_type}, timeout)

    alt = ((payload.get("results") or {}).get("channels") or [{}])[0]
    alt = (alt.get("alternatives") or [{}])[0]

    cues: list[dict] = []
    # Sentences carry both the timing and a natural unit to cite. Words are the
    # fallback because a claim anchored to a single word is technically precise and
    # practically useless.
    for para in ((alt.get("paragraphs") or {}).get("paragraphs") or []):
        speaker = para.get("speaker")
        for i, sentence in enumerate(para.get("sentences") or []):
            text = (sentence.get("text") or "").strip()
            if not text:
                continue
            if diarise and speaker is not None and i == 0:
                text = f"Speaker {speaker}: {text}"
            cues.append({"start": float(sentence.get("start", 0.0)), "text": text})

    if not cues:
        for word in alt.get("words") or []:
            cues.append({"start": float(word.get("start", 0.0)),
                         "text": word.get("punctuated_word") or word.get("word", "")})
    return cues


def _groq(data: bytes, media_type: str, key: str, *, filename: str,
          language: str | None, timeout: float) -> list[dict]:
    if len(data) > GROQ_MAX_BYTES:
        raise RuntimeError(
            f"Groq accepts at most {GROQ_MAX_BYTES // (1024 * 1024)} MB and this file is "
            f"{len(data) // (1024 * 1024)} MB. Use Deepgram for long recordings "
            "(set DEEPGRAM_API_KEY), or re-encode the file to a lower bitrate.")

    fields = {"model": "whisper-large-v3", "response_format": "verbose_json",
              "timestamp_granularities[]": "segment"}
    if language:
        fields["language"] = language
    body, content_type = _multipart(fields, filename, data, media_type)
    payload = _post("https://api.groq.com/openai/v1/audio/transcriptions", body,
                    {"Authorization": f"Bearer {key}", "Content-Type": content_type},
                    timeout)

    return [{"start": float(s.get("start", 0.0)), "text": (s.get("text") or "").strip()}
            for s in payload.get("segments") or []
            if (s.get("text") or "").strip()]


def _sarvam(data: bytes, media_type: str, key: str, *, filename: str,
            language: str | None, timeout: float) -> list[dict]:
    fields = {"model": "saarika:v2.5",
              "language_code": language or "unknown",
              "with_timestamps": "true"}
    body, content_type = _multipart(fields, filename, data, media_type)
    payload = _post("https://api.sarvam.ai/speech-to-text", body,
                    {"api-subscription-key": key, "Content-Type": content_type}, timeout)

    stamps = payload.get("timestamps") or {}
    words, starts = stamps.get("words") or [], stamps.get("start_time_seconds") or []
    if words and len(words) == len(starts):
        return [{"start": float(s), "text": w} for w, s in zip(words, starts, strict=False)]

    # Sarvam does not always return timings. Say so rather than inventing them: the
    # caller degrades to paragraph anchors, which is weaker but honest.
    transcript = (payload.get("transcript") or "").strip()
    if not transcript:
        raise RuntimeError("sarvam returned no transcript")
    log.warning("sarvam returned no word timings; the transcript will not be timestamped")
    return [{"start": 0.0, "text": transcript}]


# ---------------------------------------------------------------------------


def _pick(provider: str | None, keys: dict[str, str | None]) -> tuple[str, str]:
    if provider:
        provider = provider.lower()
        if provider not in PROVIDERS:
            raise ValueError(f"unknown transcription provider {provider!r}; "
                             f"use one of {', '.join(PROVIDERS)}")
        key = keys.get(provider)
        if not key:
            raise RuntimeError(f"{provider} was requested but {_ENV_KEYS[provider]} is not set")
        return provider, key

    for name in PROVIDERS:
        if keys.get(name):
            return name, keys[name]  # type: ignore[return-value]

    raise RuntimeError(
        "transcribing audio needs a speech-to-text key, and none is set. Any one of:\n"
        f"  {_ENV_KEYS['deepgram']}  best for long recordings, speaker labels\n"
        f"  {_ENV_KEYS['groq']}      Whisper large-v3, cheapest to start, 25 MB cap\n"
        f"  {_ENV_KEYS['sarvam']}    Indian languages and Hinglish\n"
        "There is no offline fallback on purpose: a transcript you did not get is not "
        "a source, and inventing one would put claims nobody made into your notes.")


def transcribe(path: str | Path, *, provider: str | None = None,
               deepgram_key: str | None = None, groq_key: str | None = None,
               sarvam_key: str | None = None, language: str | None = None,
               diarise: bool = True, timeout: float = 600.0) -> tuple[list[dict], str]:
    """Transcribe a local audio file into timed cues. Returns `(cues, provider)`."""
    audio = Path(path)
    if not audio.exists():
        raise FileNotFoundError(f"no such audio file: {audio}")

    keys = {
        "deepgram": deepgram_key or os.environ.get("DEEPGRAM_API_KEY"),
        "groq": groq_key or os.environ.get("GROQ_API_KEY"),
        "sarvam": sarvam_key or os.environ.get("SARVAM_API_KEY"),
    }
    chosen, key = _pick(provider or os.environ.get("PALIMPSEST_TRANSCRIBE"), keys)

    data = audio.read_bytes()
    media_type = _media_type(audio)
    log.info("transcribing %s (%.1f MB) with %s", audio.name,
             len(data) / 1024 / 1024, chosen)

    if chosen == "deepgram":
        cues = _deepgram(data, media_type, key, language=language, diarise=diarise,
                         timeout=timeout)
    elif chosen == "groq":
        cues = _groq(data, media_type, key, filename=audio.name, language=language,
                     timeout=timeout)
    else:
        cues = _sarvam(data, media_type, key, filename=audio.name, language=language,
                       timeout=timeout)

    if not cues:
        raise RuntimeError(
            f"{chosen} returned an empty transcript for {audio.name}. If the recording "
            "is silent or in an unexpected language, say so with --language.")
    return cues, chosen


def from_audio(spec: str, *, title: str | None = None, url: str | None = None,
               provider: str | None = None, language: str | None = None,
               **keys) -> Source:
    """A local recording, transcribed and anchored to its own clock."""
    audio = Path(spec)
    cues, chosen = transcribe(audio, provider=provider, language=language, **keys)

    # A local file has no deep link, so the locator carries the timestamp and you seek
    # to it yourself. Fabricating a `?t=` for a path would be a citation that looks
    # actionable and is not.
    text, segments = merge_cues(cues)
    timestamped = len(segments) > 1 or cues[0]["start"] > 0

    return make_source("audio", title or audio.stem, text, url=url, segments=segments,
                       transcriber=chosen, timestamped=timestamped, cues=len(cues),
                       duration_s=int(cues[-1]["start"]), path=str(audio.resolve()),
                       bytes=audio.stat().st_size)
