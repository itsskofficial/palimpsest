"""The adapters behind "give it anything" — the headline claim, least covered code.

`youtube.py` was at zero, `files.py` at twenty percent. These are the modules a stranger
meets first, because the first thing anybody does with a tool like this is throw a PDF at
it, and a PDF that fails is a user who does not come back.

What is actually being checked, in every case, is **anchoring**. Turning a file into text
is the easy half and the half that visibly fails; the half that fails *silently* is the
map from a span of that text back to where it came from. A citation that says `p. 14` and
means page 3, or a YouTube link that opens at 0:00 instead of 41:12, is worse than no
citation at all — it looks authoritative and sends you to the wrong place, and nothing
about it reads as broken.

Nothing here touches the network. The YouTube adapter is driven against recorded
responses in the shapes the endpoints actually return.
"""

from __future__ import annotations

import json

import pytest

from palimpsest.ingest import anchor_for, detect_kind

#: A PDF is a byte format and its structure is newline-delimited.
NEWLINE = bytes([10])

# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("spec", "kind"), [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
    ("https://example.com/an-article", "web"),
    ("notes.pdf", "pdf"),
    ("data.csv", "tabular"),
    ("sheet.xlsx", "tabular"),
    ("photo.jpg", "image"),
    ("lecture.mp3", "audio"),
    ("recording.ogg", "audio"),
    ("voice.oga", "audio"),
    ("just some text I typed", "text"),
])
def test_each_kind_of_thing_is_routed_to_its_adapter(spec, kind):
    """The router decides which adapter runs, and getting it wrong is not subtle: a
    YouTube URL sent to the web scraper captures the page chrome and no transcript."""
    assert detect_kind(spec) == kind


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "http://youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
])
def test_the_video_id_is_found_in_every_url_shape_youtube_uses(url):
    from palimpsest.ingest.youtube import video_id

    assert video_id(url) == "dQw4w9WgXcQ"


def test_something_that_is_not_a_youtube_url_is_refused():
    from palimpsest.ingest.youtube import video_id

    with pytest.raises(ValueError, match="not a YouTube URL"):
        video_id("https://vimeo.com/12345")


def _watch_page(tracks: list[dict], title: str = "A lecture on attention") -> str:
    body = json.dumps(tracks).replace("&", "\\u0026")
    return (f'<meta name="title" content="{title}">'
            f'<script>{{"captionTracks":{body}}}</script>')


def _json3(cues: list[tuple[float, str]]) -> str:
    return json.dumps({"events": [
        {"tStartMs": int(start * 1000), "segs": [{"utf8": text}]}
        for start, text in cues
    ]})


@pytest.fixture()
def youtube(monkeypatch):
    """Serve recorded responses in place of the two endpoints the adapter calls."""
    import palimpsest.ingest.youtube as mod

    responses: dict[str, str] = {}

    def fetch(url, timeout=30.0):
        for fragment, body in responses.items():
            if fragment in url:
                return body
        raise RuntimeError(f"nothing recorded for {url}")

    monkeypatch.setattr(mod, "_fetch", fetch)

    # yt-dlp goes first when it is installed, and it makes its own requests. Left in place
    # it reached real YouTube from a unit test and returned whatever that video ID is
    # today, so these tests exercise the timedtext path with it switched off.
    def no_ytdlp(vid):
        raise ImportError("yt_dlp")

    monkeypatch.setattr(mod, "_cues_via_ytdlp", no_ytdlp)
    return responses


def test_a_transcript_becomes_passages_that_deep_link_to_the_second(youtube):
    """The deep link is the whole point of the adapter.

    A claim from 41 minutes in has to cite a URL that opens the video *there*. Anchor
    the text wrongly and the citation still looks right, still resolves, and drops the
    reader at the wrong moment — which is the failure mode this adapter exists to avoid.
    """
    from palimpsest.ingest.youtube import from_youtube

    youtube["watch?v="] = _watch_page(
        [{"languageCode": "en", "baseUrl": "https://yt/api/timedtext?v=x"}])
    youtube["timedtext"] = _json3([
        (0.0, "Attention divides the logits by the square root of d_k."),
        (62.0, "Multi-head attention uses eight heads."),
        (2472.0, "That concludes positional encoding."),
    ])

    source = from_youtube("https://youtu.be/dQw4w9WgXcQ")

    assert source.kind == "youtube"
    assert source.title == "A lecture on attention"
    assert source.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert source.meta["cues"] == 3

    index = source.text.index("eight heads")
    anchor = anchor_for(source, index, index + 11)
    assert anchor.locator == "1:02"
    assert anchor.url is not None and anchor.url.endswith("&t=62s")

    late = source.text.index("positional encoding")
    assert anchor_for(source, late, late + 5).locator == "41:12"


def test_a_written_english_track_beats_an_auto_generated_one(youtube):
    """Auto-captions mishear technical vocabulary constantly, and a knowledge base built
    from "attention is all you need" transcribed as "a tension" is worse than empty."""
    from palimpsest.ingest.youtube import from_youtube

    youtube["watch?v="] = _watch_page([
        {"languageCode": "en", "kind": "asr", "baseUrl": "https://yt/asr"},
        {"languageCode": "en", "baseUrl": "https://yt/written"},
    ])
    youtube["written"] = _json3([(0.0, "the written one")])
    youtube["asr"] = _json3([(0.0, "the auto one")])

    assert "the written one" in from_youtube("https://youtu.be/abcdefg").text


def test_with_only_auto_captions_it_uses_them_rather_than_refusing(youtube):
    from palimpsest.ingest.youtube import from_youtube

    youtube["watch?v="] = _watch_page(
        [{"languageCode": "en", "kind": "asr", "baseUrl": "https://yt/asr"}])
    youtube["asr"] = _json3([(0.0, "the auto one")])

    assert "the auto one" in from_youtube("https://youtu.be/abcdefg").text


def test_a_video_with_no_captions_says_what_to_do_instead(youtube):
    """The useful answer is not "failed" — it is "transcribe the audio and try that",
    which is a thing this tool can also do."""
    from palimpsest.ingest.youtube import from_youtube

    youtube["watch?v="] = '<meta name="title" content="Silent film">'

    with pytest.raises(RuntimeError) as caught:
        from_youtube("https://youtu.be/abcdefg")

    message = str(caught.value)
    assert "no caption track" in message
    assert "transcribe the audio" in message


def test_empty_caption_segments_are_dropped_rather_than_padding_the_text(youtube):
    """YouTube emits timing-only events with no text. Kept, they become empty passages
    that anchor claims to nothing."""
    from palimpsest.ingest.youtube import from_youtube

    youtube["watch?v="] = _watch_page([{"languageCode": "en", "baseUrl": "https://yt/t"}])
    youtube["/t"] = json.dumps({"events": [
        {"tStartMs": 0, "segs": [{"utf8": "real text"}]},
        {"tStartMs": 1000, "segs": [{"utf8": "  "}]},
        {"tStartMs": 2000},
    ]})

    source = from_youtube("https://youtu.be/abcdefg")

    assert source.meta["cues"] == 1


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------


def _pdf(tmp_path, pages: list[str]):
    """A real, minimal PDF written by hand, so the reader is genuinely exercised.

    Built from bytes rather than with a library because the page needs a `/Font` resource
    for its text to be *extractable*. A page with a content stream and no font renders
    nothing and extracts nothing, which silently turns this into a test of an empty
    document — which is exactly what the first version of this fixture did, and it showed
    up only as a skip.
    """
    pytest.importorskip("pypdf")

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    def ref(number: int) -> bytes:
        return str(number).encode() + b" 0 R"

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    # The page objects need their parent's id before it exists, so it is reserved: one
    # font, then two objects per page, then /Pages.
    pages_id = 1 + len(pages) * 2 + 1
    page_ids: list[int] = []

    for text in pages:
        # Parentheses delimit a PDF string; sidestep escaping rather than get it subtly
        # wrong, since the fixture's job is to be obviously correct.
        body = text.replace("(", "[").replace(")", "]").encode("latin-1", "replace")
        stream = b"BT /F1 24 Tf 72 700 Td (" + body + b") Tj ET"
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>" + NEWLINE
                      + b"stream" + NEWLINE + stream + NEWLINE + b"endstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent " + ref(pages_id)
            + b" /MediaBox [0 0 612 792]"
            + b" /Resources << /Font << /F1 " + ref(font) + b" >> >>"
            + b" /Contents " + ref(content) + b" >>"))

    kids = b" ".join(ref(i) for i in page_ids)
    pages_obj = add(b"<< /Type /Pages /Kids [" + kids + b"] /Count "
                    + str(len(page_ids)).encode() + b" >>")
    assert pages_obj == pages_id, "the reserved /Parent id must match"
    catalog = add(b"<< /Type /Catalog /Pages " + ref(pages_obj) + b" >>")

    out = bytearray(b"%PDF-1.4" + NEWLINE)
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj" + NEWLINE + body + NEWLINE + b"endobj" + NEWLINE

    start = len(out)
    out += b"xref" + NEWLINE + b"0 " + str(len(objects) + 1).encode() + NEWLINE
    out += b"0000000000 65535 f " + NEWLINE
    for offset in offsets:
        out += f"{offset:010d} 00000 n ".encode() + NEWLINE
    out += (b"trailer" + NEWLINE + b"<< /Size " + str(len(objects) + 1).encode()
            + b" /Root " + ref(catalog) + b" >>" + NEWLINE
            + b"startxref" + NEWLINE + str(start).encode() + NEWLINE + b"%%EOF" + NEWLINE)

    path = tmp_path / "notes.pdf"
    path.write_bytes(bytes(out))
    return path


def test_a_pdf_anchors_each_claim_to_the_page_it_came_from(tmp_path):
    """`p. 3` has to mean page 3.

    A citation that points at the wrong page is worse than none: it looks authoritative,
    it resolves, and it sends the reader somewhere the claim is not — so they conclude
    the note is wrong rather than the citation.
    """
    from palimpsest.ingest.files import from_pdf

    path = _pdf(tmp_path, ["alpha on the first page",
                           "beta on the second page",
                           "gamma on the third page"])

    source = from_pdf(str(path))

    assert source.kind == "pdf"
    assert source.meta["pages"] == 3
    for word, page in (("alpha", 1), ("beta", 2), ("gamma", 3)):
        assert word in source.text, f"{word} was not extracted from the PDF"
        i = source.text.index(word)
        assert anchor_for(source, i, i + len(word)).locator == f"p. {page}"


def test_a_pdf_that_is_not_a_pdf_fails_with_something_readable(tmp_path):
    pytest.importorskip("pypdf")
    from palimpsest.ingest.files import from_pdf

    path = tmp_path / "not-really.pdf"
    path.write_bytes(b"this is not a PDF at all")

    with pytest.raises(Exception) as caught:
        from_pdf(str(path))

    # pypdf's own error type, whatever wording it uses. What matters is that a bad file
    # surfaces as a readable exception rather than a crash or an empty source that looks
    # like a document with nothing in it.
    assert type(caught.value).__name__.startswith("Pdf")


# ---------------------------------------------------------------------------
# spreadsheets
# ---------------------------------------------------------------------------


def test_a_csv_is_rendered_as_a_table_with_its_header_on_every_chunk(tmp_path):
    """A spreadsheet is only meaningful with its header. Split into chunks without
    repeating it, the second chunk is a grid of numbers whose columns mean nothing."""
    from palimpsest.ingest.files import from_tabular

    path = tmp_path / "runs.csv"
    path.write_text(
        "model,f1,contradiction_recall\n"
        "claude-sonnet-5,0.95,1.00\n"
        "qwen3-8b,0.57,0.67\n",
        encoding="utf-8")

    source = from_tabular(str(path))

    assert source.kind == "tabular"
    assert source.meta["rows"] == 2
    assert "model" in source.text and "contradiction_recall" in source.text
    assert "claude-sonnet-5" in source.text and "qwen3-8b" in source.text


def test_a_tsv_is_split_on_tabs_rather_than_commas(tmp_path):
    """A TSV read as CSV becomes one column per row, and every fact in it is lost."""
    from palimpsest.ingest.files import from_tabular

    path = tmp_path / "runs.tsv"
    path.write_text("model\tf1\nclaude\t0.95\n", encoding="utf-8")

    source = from_tabular(str(path))

    assert source.meta["rows"] == 1
    assert "claude" in source.text and "0.95" in source.text


def test_a_csv_with_bad_bytes_is_read_rather_than_refused(tmp_path):
    """Exports from elsewhere are routinely not UTF-8. Refusing them means telling
    somebody their own data is invalid."""
    from palimpsest.ingest.files import from_tabular

    path = tmp_path / "latin.csv"
    path.write_bytes("name,note\nGr\xfc\xdfe,fine\n".encode("latin-1"))

    source = from_tabular(str(path))

    assert source.meta["rows"] == 1
    assert "fine" in source.text


def test_an_empty_csv_does_not_report_a_negative_row_count(tmp_path):
    """`len(rows) - 1` on an empty file is -1, which surfaces as "-1 rows" in the UI."""
    from palimpsest.ingest.files import from_tabular

    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    assert from_tabular(str(path)).meta["rows"] == 0


# ---------------------------------------------------------------------------
# plain text
# ---------------------------------------------------------------------------


def test_typed_text_becomes_a_source_with_one_segment():
    from palimpsest.ingest.files import from_text

    source = from_text("Global-norm clipping is the usual choice.", title="A thought")

    assert source.kind == "text"
    assert source.title == "A thought"
    assert source.text.startswith("Global-norm")
    anchor = anchor_for(source, 0, 12)
    assert anchor is not None


def test_a_text_file_on_disk_is_read_rather_than_treated_as_its_own_content(tmp_path):
    """Otherwise dropping `notes.md` captures the string "notes.md"."""
    from palimpsest.ingest.files import from_text

    path = tmp_path / "notes.md"
    path.write_text("# Heading\n\nSome prose about clipping.\n", encoding="utf-8")

    source = from_text(str(path))

    assert "Some prose about clipping" in source.text
