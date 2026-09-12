"""Recordings — the one source type that cannot be read without a service.

Two things here are load-bearing rather than incidental.

**There is no fallback, and there must not be one.** Every tempting alternative to a
real transcript — the filename, a description, an empty string — puts a claim nobody
made into somebody's notes. So the failure path is tested as carefully as the happy one:
when there is no key, the error has to name the three that would work and say why it
will not guess.

**The timestamps are the product.** A ninety-minute meeting yields a claim you will want
to check in six months, and "somewhere in that recording" is not a citation. Each
provider returns timings in its own shape, and getting one of those shapes wrong does
not raise — it silently produces a transcript anchored at 0:00, which reads as a
successful ingest and cites nothing.

Nothing here makes a network call: `_post` is the seam, and every provider goes through
it.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from palimpsest.ingest import audio


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """A developer's real keys must not decide which branch a test takes."""
    for name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "SARVAM_API_KEY",
                 "PALIMPSEST_TRANSCRIBE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def recording(tmp_path):
    path = tmp_path / "meeting.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 2048)
    return path


@pytest.fixture()
def posted(monkeypatch):
    """Capture every outbound request instead of making it."""
    calls: list[dict] = []
    replies: list = []

    def fake(url, body, headers, timeout):
        calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        return replies.pop(0) if replies else {}

    monkeypatch.setattr(audio, "_post", fake)
    fake.calls = calls       # type: ignore[attr-defined]
    fake.replies = replies   # type: ignore[attr-defined]
    return fake


# ---------------------------------------------------------------------------
# choosing a provider
# ---------------------------------------------------------------------------


def test_with_no_key_at_all_the_error_names_all_three_and_says_why_not_to_guess():
    """This is the most common failure for a voice note, and the message is the whole
    fix. "There is no offline fallback on purpose" is the part that stops somebody
    filing a bug asking for one."""
    with pytest.raises(RuntimeError) as caught:
        audio._pick(None, {"deepgram": None, "groq": None, "sarvam": None})

    message = str(caught.value)
    assert "DEEPGRAM_API_KEY" in message
    assert "GROQ_API_KEY" in message
    assert "SARVAM_API_KEY" in message
    assert "no offline fallback" in message
    assert "claims nobody made" in message


def test_the_default_order_prefers_deepgram():
    """Best accuracy on long-form speech, no file-size ceiling, speaker labels. The
    order is a recommendation, and it only means anything if it is followed."""
    chosen, key = audio._pick(None, {"deepgram": "d", "groq": "g", "sarvam": "s"})

    assert (chosen, key) == ("deepgram", "d")


def test_the_next_key_is_used_when_the_preferred_one_is_absent():
    assert audio._pick(None, {"deepgram": None, "groq": "g", "sarvam": "s"})[0] == "groq"
    assert audio._pick(None, {"deepgram": None, "groq": None,
                              "sarvam": "s"})[0] == "sarvam"


def test_naming_a_provider_overrides_the_order():
    """Sarvam is the right call for a Hinglish lecture even when Deepgram is configured,
    and the setting is how you say so."""
    assert audio._pick("sarvam", {"deepgram": "d", "sarvam": "s"})[0] == "sarvam"


def test_a_provider_nobody_implemented_is_named_along_with_the_ones_that_exist():
    with pytest.raises(ValueError) as caught:
        audio._pick("whisper", {"deepgram": "d"})

    message = str(caught.value)
    assert "whisper" in message
    for name in audio.PROVIDERS:
        assert name in message


def test_asking_for_a_provider_with_no_key_names_that_providers_variable():
    """Not the generic "no key is set" list: they asked for one thing, so tell them
    about that thing."""
    with pytest.raises(RuntimeError) as caught:
        audio._pick("groq", {"deepgram": "d", "groq": None})

    assert "GROQ_API_KEY" in str(caught.value)
    assert "DEEPGRAM" not in str(caught.value)


def test_the_environment_can_choose_the_provider(recording, posted, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_TRANSCRIBE", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_x")
    posted.replies.append({"segments": [{"start": 0.0, "text": "hello"}]})

    _, chosen = audio.transcribe(recording)

    assert chosen == "groq"
    assert "groq.com" in posted.calls[0]["url"]


# ---------------------------------------------------------------------------
# deepgram
# ---------------------------------------------------------------------------


def _deepgram_reply(paragraphs=None, words=None) -> dict:
    alt = {}
    if paragraphs is not None:
        alt["paragraphs"] = {"paragraphs": paragraphs}
    if words is not None:
        alt["words"] = words
    return {"results": {"channels": [{"alternatives": [alt]}]}}


def test_deepgram_cues_come_from_sentences_with_their_own_timings(posted):
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 0, "sentences": [
            {"start": 12.5, "text": "Clipping is on by default."},
            {"start": 18.0, "text": "It costs almost nothing."}]}]))

    cues = audio._deepgram(b"x", "audio/mpeg", "k", language=None, diarise=False,
                           timeout=1)

    assert cues == [{"start": 12.5, "text": "Clipping is on by default."},
                    {"start": 18.0, "text": "It costs almost nothing."}]


def test_a_speaker_is_named_once_per_paragraph_not_once_per_sentence(posted):
    """"Speaker 0:" in front of every sentence is unreadable, and the transcript is the
    thing claims get extracted from."""
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 1, "sentences": [{"start": 0.0, "text": "First."},
                                     {"start": 1.0, "text": "Second."}]}]))

    cues = audio._deepgram(b"x", "audio/mpeg", "k", language=None, diarise=True,
                           timeout=1)

    assert cues[0]["text"] == "Speaker 1: First."
    assert cues[1]["text"] == "Second."


def test_speaker_labels_are_left_off_when_they_were_not_asked_for(posted):
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 1, "sentences": [{"start": 0.0, "text": "First."}]}]))

    cues = audio._deepgram(b"x", "audio/mpeg", "k", language=None, diarise=False,
                           timeout=1)

    assert cues[0]["text"] == "First."


def test_diarisation_and_language_reach_the_request(posted):
    posted.replies.append(_deepgram_reply(paragraphs=[]))

    audio._deepgram(b"x", "audio/mpeg", "k", language="hi", diarise=True, timeout=1)

    url = posted.calls[0]["url"]
    assert "diarize=true" in url
    assert "language=hi" in url


def test_words_are_the_fallback_when_there_are_no_sentences(posted):
    """A short voice memo comes back with words and no paragraph structure. Returning
    nothing there would fail a recording that transcribed perfectly."""
    posted.replies.append(_deepgram_reply(paragraphs=[], words=[
        {"start": 0.0, "punctuated_word": "Hello,", "word": "hello"},
        {"start": 0.4, "word": "world"}]))

    cues = audio._deepgram(b"x", "audio/mpeg", "k", language=None, diarise=False,
                           timeout=1)

    assert [c["text"] for c in cues] == ["Hello,", "world"]


def test_an_empty_sentence_is_dropped_rather_than_anchored(posted):
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 0, "sentences": [{"start": 0.0, "text": "   "},
                                     {"start": 1.0, "text": "Real."}]}]))

    cues = audio._deepgram(b"x", "audio/mpeg", "k", language=None, diarise=False,
                           timeout=1)

    assert [c["text"] for c in cues] == ["Real."]


def test_a_reply_shaped_nothing_like_the_documentation_is_empty_not_a_crash(posted):
    posted.replies.append({"unexpected": True})

    assert audio._deepgram(b"x", "audio/mpeg", "k", language=None, diarise=False,
                           timeout=1) == []


# ---------------------------------------------------------------------------
# groq
# ---------------------------------------------------------------------------


def test_groq_refuses_a_long_recording_before_uploading_it(posted):
    """Twenty-five megabytes is about an hour of speech, and a lecture is longer. The
    error has to name Deepgram, or the answer looks like "audio does not work"."""
    too_big = b"\x00" * (audio.GROQ_MAX_BYTES + 1)

    with pytest.raises(RuntimeError) as caught:
        audio._groq(too_big, "audio/mpeg", "k", filename="lecture.mp3", language=None,
                    timeout=1)

    message = str(caught.value)
    assert "25 MB" in message
    assert "DEEPGRAM_API_KEY" in message
    assert posted.calls == [], "and it did not spend an upload finding out"


def test_groq_segments_become_cues(posted):
    posted.replies.append({"segments": [
        {"start": 0.0, "text": " Clipping is on by default."},
        {"start": 5.5, "text": "   "},
        {"start": 9.0, "text": "It costs almost nothing."}]})

    cues = audio._groq(b"x", "audio/mpeg", "k", filename="a.mp3", language=None,
                       timeout=1)

    assert cues == [{"start": 0.0, "text": "Clipping is on by default."},
                    {"start": 9.0, "text": "It costs almost nothing."}]


def test_groq_is_asked_for_segment_timings(posted):
    """Without `timestamp_granularities[]` the response has no `segments` at all, and
    every claim from an hour of audio anchors to 0:00."""
    posted.replies.append({"segments": []})

    audio._groq(b"x", "audio/mpeg", "k", filename="a.mp3", language="en", timeout=1)

    body = posted.calls[0]["body"].decode("utf-8", "replace")
    assert "timestamp_granularities[]" in body
    assert "segment" in body
    assert "whisper-large-v3" in body
    assert 'name="language"' in body and "en" in body


# ---------------------------------------------------------------------------
# sarvam
# ---------------------------------------------------------------------------


def test_sarvam_word_timings_become_cues(posted):
    posted.replies.append({"timestamps": {"words": ["gradient", "clipping"],
                                          "start_time_seconds": [1.0, 1.4]}})

    cues = audio._sarvam(b"x", "audio/mpeg", "k", filename="a.wav", language="hi-IN",
                         timeout=1)

    assert cues == [{"start": 1.0, "text": "gradient"},
                    {"start": 1.4, "text": "clipping"}]


def test_sarvam_without_timings_returns_one_untimed_cue_rather_than_inventing_them(
        posted, caplog):
    """Sarvam does not always return timings. Spacing them evenly would produce a
    citation that looks precise and points at the wrong minute."""
    import logging

    posted.replies.append({"timestamps": {"words": ["a", "b"],
                                          "start_time_seconds": [1.0]},
                           "transcript": "the whole thing, untimed"})

    with caplog.at_level(logging.WARNING, logger="palimpsest.ingest.audio"):
        cues = audio._sarvam(b"x", "audio/mpeg", "k", filename="a.wav", language=None,
                             timeout=1)

    assert cues == [{"start": 0.0, "text": "the whole thing, untimed"}]
    assert any("timings" in r.getMessage() for r in caplog.records), \
        "silently dropping the timestamps is the thing that must not happen quietly"


def test_sarvam_with_nothing_at_all_raises(posted):
    posted.replies.append({"timestamps": {}, "transcript": "  "})

    with pytest.raises(RuntimeError, match="no transcript"):
        audio._sarvam(b"x", "audio/mpeg", "k", filename="a.wav", language=None,
                      timeout=1)


def test_sarvam_is_told_which_language_when_one_is_named(posted):
    posted.replies.append({"transcript": "x", "timestamps": {}})

    audio._sarvam(b"x", "audio/mpeg", "k", filename="a.wav", language="hi-IN",
                  timeout=1)

    body = posted.calls[0]["body"].decode("utf-8", "replace")
    assert "hi-IN" in body


# ---------------------------------------------------------------------------
# the transport
# ---------------------------------------------------------------------------


def test_a_multipart_body_carries_the_fields_and_the_file():
    body, content_type = audio._multipart(
        {"model": "whisper-large-v3"}, "meeting.mp3", b"AUDIOBYTES", "audio/mpeg")

    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]
    assert boundary.encode() in body
    assert b'name="model"' in body and b"whisper-large-v3" in body
    assert b'filename="meeting.mp3"' in body
    assert b"AUDIOBYTES" in body
    assert body.rstrip().endswith(f"--{boundary}--".encode()), "the closing boundary"


def test_every_request_identifies_itself_as_palimpsest(monkeypatch):
    """`Python-urllib/3.x` is rejected outright by Groq's CDN with a Cloudflare 1010 — a
    403 that looks exactly like a bad API key and sends you checking the wrong thing."""
    seen = {}

    class Response:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return Response()

    monkeypatch.setattr(audio.urllib.request, "urlopen", urlopen)

    audio._post("https://api.groq.com/v1/x", b"", {}, 1)

    assert "palimpsest" in seen["ua"]


def test_an_http_error_names_the_service_and_what_it_said(monkeypatch):
    """"HTTP Error 401" alone does not say which of three providers refused, and the
    body is where the reason is."""
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.deepgram.com/v1/listen", 401, "Unauthorized", {},
            __import__("io").BytesIO(json.dumps({"err_msg": "bad credentials"}).encode()))

    monkeypatch.setattr(audio.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        audio._post("https://api.deepgram.com/v1/listen", b"", {}, 1)

    message = str(caught.value)
    assert "api.deepgram.com" in message
    assert "401" in message
    assert "bad credentials" in message


# ---------------------------------------------------------------------------
# end to end, without the network
# ---------------------------------------------------------------------------


def test_a_file_that_is_not_there_says_so_before_choosing_a_provider(tmp_path):
    with pytest.raises(FileNotFoundError):
        audio.transcribe(tmp_path / "nothing.mp3", deepgram_key="d")


def test_a_silent_recording_fails_with_a_hint_rather_than_an_empty_source(recording,
                                                                         posted):
    """An empty transcript that became a Source would put a titled, cited, contentless
    page into somebody's notes."""
    posted.replies.append(_deepgram_reply(paragraphs=[]))

    with pytest.raises(RuntimeError) as caught:
        audio.transcribe(recording, deepgram_key="d")

    message = str(caught.value)
    assert "empty transcript" in message
    assert "--language" in message, "which is the usual cause and the usual fix"


def test_a_recording_becomes_a_source_that_carries_its_own_clock(recording, posted):
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 0, "sentences": [
            {"start": 0.0, "text": "Gradient clipping bounds the update."},
            {"start": 862.0, "text": "We left it on for every run."}]}]))

    source = audio.from_audio(str(recording), deepgram_key="d")

    assert source.kind == "audio"
    assert source.title == "meeting"
    assert "Gradient clipping" in source.text
    assert source.meta["transcriber"] == "deepgram"
    assert source.meta["timestamped"] is True
    assert source.meta["duration_s"] == 862
    assert source.meta["cues"] == 2
    assert source.meta["bytes"] == recording.stat().st_size
    assert source.meta["segments"], "no segments means no citable timestamp"


def test_a_given_title_beats_the_filename(recording, posted):
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 0, "sentences": [{"start": 0.0, "text": "Something."}]}]))

    source = audio.from_audio(str(recording), title="Standup, Tuesday",
                              deepgram_key="d")

    assert source.title == "Standup, Tuesday"


def test_a_recording_with_one_cue_at_zero_is_not_claimed_to_be_timestamped(recording,
                                                                          posted):
    """A voice memo transcribed as a single untimed block has no clock to cite, and
    saying it does is how a citation stops meaning anything."""
    posted.replies.append(_deepgram_reply(paragraphs=[
        {"speaker": 0, "sentences": [{"start": 0.0, "text": "One short thought."}]}]))

    source = audio.from_audio(str(recording), deepgram_key="d")

    assert source.meta["timestamped"] is False
