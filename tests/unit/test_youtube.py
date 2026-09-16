"""YouTube through yt-dlp, and playlists expanded into one source per video.

Two failures shaped this file, and neither raised anything useful.

**Every YouTube capture broke at once.** YouTube began returning empty caption responses
to clients it does not recognise, so the hand-written timedtext path produced a
`JSONDecodeError` and nothing came back. yt-dlp is maintained against that churn and now
goes first; these tests pin which caption track it picks, because an automatic track in
the wrong language produces claims whose quotes cannot be located.

**A playlist link was scraped as a web page.** It did not match the YouTube pattern, fell
through to the web adapter, and yielded claims about the page's sidebar. A playlist is now
recognised and expanded into one job per video, each its own source with its own
timestamps — "somewhere in a ten-video course" is not a citation.

No test here reaches the network. A fake `yt_dlp` module stands in for the real one.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from palimpsest.ingest import detect_kind, resolve
from palimpsest.ingest import youtube as yt

# ---------------------------------------------------------------------------
# a yt-dlp that answers from a script
# ---------------------------------------------------------------------------


def _json3(*cues: tuple[float, str]) -> bytes:
    return json.dumps({"events": [{"tStartMs": int(s * 1000), "segs": [{"utf8": t}]}
                                  for s, t in cues]}).encode("utf-8")


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def ytdlp(monkeypatch):
    """Install a fake `yt_dlp` whose answers the test sets."""
    script: dict = {"info": {}, "captions": {}, "opened": [], "options": []}

    class YoutubeDL:
        def __init__(self, options):
            script["options"].append(options)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            assert download is False, "nothing is ever downloaded"
            for fragment, info in script["info"].items():
                if fragment in url:
                    return info
            raise RuntimeError(f"no info scripted for {url}")

        def urlopen(self, url):
            script["opened"].append(url)
            return _Response(script["captions"][url])

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = YoutubeDL
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return script


def _track(url: str, ext: str = "json3") -> list[dict]:
    return [{"ext": "vtt", "url": url + ".vtt"}, {"ext": ext, "url": url}]


# ---------------------------------------------------------------------------
# recognising the link
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi",
    "https://youtube.com/playlist?list=PLabc123&si=share-token",
    "https://m.youtube.com/playlist?feature=share&list=PLabc123",
])
def test_a_playlist_link_is_recognised_as_a_playlist(url):
    """It used to fall through to the web adapter and be scraped as a page."""
    assert detect_kind(url) == "youtube_playlist"
    assert yt.is_playlist(url)


def test_a_video_shared_from_inside_a_playlist_is_still_one_video():
    """The Share button on a video in a playlist adds `&list=`. Expanding that would turn
    "save this video" into "ingest the whole course"."""
    url = "https://www.youtube.com/watch?v=aircAruvnKk&list=PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi"

    assert detect_kind(url) == "youtube"
    assert not yt.is_playlist(url)
    assert yt.video_id(url) == "aircAruvnKk"


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=aircAruvnKk", "aircAruvnKk"),
    ("https://youtu.be/aircAruvnKk?si=x", "aircAruvnKk"),
    ("https://www.youtube.com/shorts/aircAruvnKk", "aircAruvnKk"),
    ("https://www.youtube.com/live/aircAruvnKk", "aircAruvnKk"),
    ("https://www.youtube.com/watch?feature=share&v=aircAruvnKk", "aircAruvnKk"),
])
def test_every_shape_of_video_link_finds_the_video(url, vid):
    assert detect_kind(url) == "youtube"
    assert yt.video_id(url) == vid


def test_resolving_a_playlist_directly_explains_where_to_send_it():
    """`resolve` makes one source, and a playlist is many. The synchronous API route and
    anything else that calls it gets a sentence rather than a web scrape."""
    with pytest.raises(ValueError, match="capture queue"):
        resolve("https://www.youtube.com/playlist?list=PLabc123")


# ---------------------------------------------------------------------------
# captions through yt-dlp
# ---------------------------------------------------------------------------


def test_a_video_becomes_a_source_with_timestamped_deep_links(ytdlp):
    ytdlp["info"]["watch?v=aircAruvnKk"] = {
        "title": "But what is a neural network?", "duration": 1120, "language": "en",
        "channel": "3Blue1Brown", "upload_date": "20171005",
        "subtitles": {"en": _track("https://cap/en")},
    }
    ytdlp["captions"]["https://cap/en"] = _json3(
        (0.0, "This is a three."), (62.0, "Each neuron holds a number."),
        (905.0, "That is the whole network."))

    source = yt.from_youtube("https://youtu.be/aircAruvnKk")

    assert source.kind == "youtube"
    assert source.title == "But what is a neural network?"
    assert source.url == "https://www.youtube.com/watch?v=aircAruvnKk"
    assert source.meta["duration_s"] == 1120, "the video's length, not its last caption"
    assert source.meta["channel"] == "3Blue1Brown"
    deep = [s["url"] for s in source.meta["segments"]]
    assert any(u.endswith("&t=905s") for u in deep), "a claim 15 minutes in links there"


def test_a_written_track_is_preferred_to_an_automatic_one(ytdlp):
    """Automatic captions drop punctuation and mishear names, and a claim's quote has to
    match the text exactly or the claim is discarded."""
    ytdlp["info"]["watch?v=vid00000001"] = {
        "title": "t", "language": "en",
        "subtitles": {"en": _track("https://cap/manual")},
        "automatic_captions": {"en": _track("https://cap/auto")},
    }
    ytdlp["captions"]["https://cap/manual"] = _json3((0.0, "Written by a person."))

    yt.from_youtube("https://www.youtube.com/watch?v=vid00000001")

    assert ytdlp["opened"] == ["https://cap/manual"]


def test_the_automatic_track_in_the_spoken_language_beats_a_translation(ytdlp):
    """`-orig` is the language actually spoken; plain `en` on a Hindi lecture is a machine
    translation of it, and its quotes match nothing anybody said."""
    ytdlp["info"]["watch?v=vid00000002"] = {
        "title": "t", "language": "hi",
        "automatic_captions": {"en": _track("https://cap/translated"),
                               "hi-orig": _track("https://cap/spoken")},
    }
    ytdlp["captions"]["https://cap/spoken"] = _json3((0.0, "नमस्ते"))

    yt.from_youtube("https://www.youtube.com/watch?v=vid00000002")

    assert ytdlp["opened"] == ["https://cap/spoken"]


def test_a_live_chat_replay_is_never_taken_for_captions(ytdlp):
    ytdlp["info"]["watch?v=vid00000003"] = {
        "title": "t", "language": "en",
        "subtitles": {"live_chat": _track("https://cap/chat")},
        "automatic_captions": {"en": _track("https://cap/auto")},
    }
    ytdlp["captions"]["https://cap/auto"] = _json3((0.0, "Spoken words."))

    yt.from_youtube("https://www.youtube.com/watch?v=vid00000003")

    assert ytdlp["opened"] == ["https://cap/auto"]


def test_a_video_with_no_captions_says_so_rather_than_inventing_a_source(ytdlp,
                                                                        monkeypatch):
    ytdlp["info"]["watch?v=vid00000004"] = {"title": "t", "subtitles": {},
                                           "automatic_captions": {}}
    monkeypatch.setattr(yt, "_cues_via_timedtext",
                        lambda vid: (_ for _ in ()).throw(RuntimeError("empty")))

    with pytest.raises(RuntimeError) as caught:
        yt.from_youtube("https://www.youtube.com/watch?v=vid00000004")

    message = str(caught.value)
    assert "no captions" in message
    assert "Deepgram" in message, "and says what to do about it"


def test_without_yt_dlp_the_error_names_the_extra(monkeypatch):
    """The empty-response failure looks like a bug in this module. When yt-dlp is what
    would have fixed it, the message has to say so."""
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    monkeypatch.setattr(yt, "_cues_via_timedtext",
                        lambda vid: (_ for _ in ()).throw(
                            RuntimeError("YouTube returned an empty caption response")))

    with pytest.raises(RuntimeError) as caught:
        yt.from_youtube("https://www.youtube.com/watch?v=vid00000005")

    assert "palimpsest-notion[youtube]" in str(caught.value)


def test_an_empty_caption_response_is_named_rather_than_a_json_error(monkeypatch):
    pages = {"watch?v=": '"captionTracks":[{"languageCode":"en","baseUrl":"https://tt"}]',
             "https://tt": ""}
    monkeypatch.setattr(yt, "_fetch",
                        lambda url, timeout=30.0: next(v for k, v in pages.items()
                                                       if k in url))

    with pytest.raises(RuntimeError, match="empty caption response"):
        yt._cues_via_timedtext("vid00000006")


# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------


def _playlist(*entries) -> dict:
    return {"title": "Neural networks", "entries": list(entries)}


def test_a_playlist_lists_its_videos_in_order(ytdlp):
    ytdlp["info"]["playlist?list=PLabc"] = _playlist(
        {"id": "aaaaaaaaaaa", "title": "Chapter 1", "duration": 1120},
        {"id": "bbbbbbbbbbb", "title": "Chapter 2", "duration": 1233})

    playlist = yt.playlist_videos("https://www.youtube.com/playlist?list=PLabc")

    assert playlist["title"] == "Neural networks"
    assert [v["title"] for v in playlist["videos"]] == ["Chapter 1", "Chapter 2"]
    assert playlist["videos"][0]["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert ytdlp["options"][0]["extract_flat"] == "in_playlist", \
        "listing a playlist must not fetch every video's full page"


def test_private_and_deleted_videos_are_skipped_and_counted(ytdlp):
    ytdlp["info"]["playlist?list=PLabc"] = _playlist(
        {"id": "aaaaaaaaaaa", "title": "Chapter 1"},
        {"id": "ccccccccccc", "title": "[Private video]"},
        {"id": "ddddddddddd", "title": "[Deleted video]"},
        None)

    playlist = yt.playlist_videos("https://www.youtube.com/playlist?list=PLabc")

    assert len(playlist["videos"]) == 1
    assert playlist["skipped"] == 3


def test_a_huge_playlist_is_cut_off_and_says_so(ytdlp):
    """Every video is an extraction and a classification per claim. A channel archive
    sent by accident is a bill, not a capture."""
    ytdlp["info"]["playlist?list=PLbig"] = _playlist(
        *({"id": f"v{i:010d}", "title": f"Video {i}"} for i in range(12)))

    playlist = yt.playlist_videos("https://www.youtube.com/playlist?list=PLbig", limit=5)

    assert len(playlist["videos"]) == 5
    assert playlist["total"] == 12
    assert playlist["truncated"] is True


def test_a_mix_is_refused_because_it_never_ends(ytdlp):
    with pytest.raises(ValueError, match="Mix"):
        yt.playlist_videos("https://www.youtube.com/playlist?list=RDaircAruvnKk")


def test_a_playlist_with_nothing_watchable_is_an_error(ytdlp):
    ytdlp["info"]["playlist?list=PLgone"] = _playlist({"id": "x", "title": "[Private video]"})

    with pytest.raises(RuntimeError, match="no videos"):
        yt.playlist_videos("https://www.youtube.com/playlist?list=PLgone")


def test_a_playlist_without_yt_dlp_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)

    with pytest.raises(RuntimeError, match=r"palimpsest-notion\[youtube\]"):
        yt.playlist_videos("https://www.youtube.com/playlist?list=PLabc")


# ---------------------------------------------------------------------------
# the queue expands a playlist into one job per video
# ---------------------------------------------------------------------------


def test_the_queue_turns_a_playlist_into_one_job_per_video(ytdlp, store):
    from palimpsest.config import Settings
    from palimpsest.jobs import ingest_runner

    ytdlp["info"]["playlist?list=PLabc"] = _playlist(
        {"id": "aaaaaaaaaaa", "title": "Chapter 1"},
        {"id": "bbbbbbbbbbb", "title": "Chapter 2"},
        {"id": "ccccccccccc", "title": "Chapter 3"})
    run = ingest_runner(Settings(anthropic_api_key="sk-ant-x"),
                        model_factory=lambda: pytest.fail("expanding needs no model call"))

    result = run({"job_id": "job_parent", "origin": "telegram:42",
                  "spec": "https://www.youtube.com/playlist?list=PLabc"}, store)

    assert result["queued"] == 3
    assert result["playlist"]["title"] == "Neural networks"
    children = [store.get_job(j) for j in result["jobs"]]
    assert [c["title"] for c in children] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert all(c["status"] == "queued" for c in children)
    assert all(c["origin"] == "telegram:42" for c in children), \
        "each video reports back to the chat the playlist came from"
    assert all(c["source_kind"] == "youtube" for c in children)
    assert children[0]["spec"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def test_the_videos_are_claimed_in_playlist_order(ytdlp, store):
    """A course is watched in order, and its later chapters refine its earlier ones."""
    from palimpsest.config import Settings
    from palimpsest.jobs import ingest_runner

    ytdlp["info"]["playlist?list=PLabc"] = _playlist(
        *({"id": f"v{i:010d}", "title": f"Chapter {i}"} for i in range(1, 6)))
    run = ingest_runner(Settings(anthropic_api_key="sk-ant-x"), model_factory=lambda: None)
    run({"job_id": "p", "spec": "https://www.youtube.com/playlist?list=PLabc"}, store)

    claimed = []
    while (job := store.claim_job()) is not None:
        claimed.append(job["title"])

    assert claimed == [f"Chapter {i}" for i in range(1, 6)]


def test_a_playlist_job_is_shown_as_a_playlist_in_the_feed():
    from palimpsest.serve.ui_api import _job_event

    event = _job_event({"job_id": "j", "status": "done", "title": "Neural networks",
                        "result": {"playlist": {"title": "Neural networks", "videos": 10,
                                                "total": 10, "skipped": 0,
                                                "truncated": False, "url": "u"},
                                   "queued": 10, "claims": 0}})

    assert event["playlist"]["videos"] == 10


def test_telegram_reports_a_playlist_as_queued_not_as_nothing_worth_keeping(store,
                                                                          monkeypatch):
    """A playlist job has no claims of its own, and the ordinary report for that is
    "Nothing worth keeping came out of it" — exactly wrong for ten queued videos."""
    import palimpsest.telegram as tg
    from palimpsest.config import Settings

    sent: list[str] = []
    monkeypatch.setattr(tg, "_call", lambda token, method, params=None, timeout=None:
                        sent.append((params or {}).get("text", "")) or {"message_id": 1})
    bot = tg.Bot(token="t", settings=Settings(), store_factory=lambda: store, queue=None,
                 allowed=frozenset({42}))

    bot._report_one(42, {"result": {"playlist": {"title": "Neural networks", "videos": 10,
                                                 "total": 10, "skipped": 1,
                                                 "truncated": False, "url": "u"},
                                    "queued": 10, "claims": 0}})

    assert "Queued 10 video(s)" in sent[0]
    assert "1 unavailable" in sent[0]
    assert "Nothing worth keeping" not in sent[0]
