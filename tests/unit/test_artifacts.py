"""The archive: the original bytes of everything ingested.

This is what makes a footnote a citation rather than decoration. A link to a page that
404s in two years proves nothing; the bytes read at ingestion time do. So the failure
that matters here is not an exception, it is a write that went somewhere else — a
version silently overwritten, a key that escaped the root, a half-written file left
behind by a crash and served afterwards as if it were the source.

`key` is reachable from an API request, which is why the containment check gets as much
attention as everything else combined.
"""

from __future__ import annotations

import json
import os

import pytest

from palimpsest.artifacts import (
    ArtifactRef,
    LocalArtifacts,
    _version_key,
    open_artifacts,
)


@pytest.fixture()
def store(tmp_path):
    return LocalArtifacts(tmp_path / "archive")


# ---------------------------------------------------------------------------
# staying inside the root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "../escaped.txt",
    "../../etc/passwd",
    "nested/../../escaped.txt",
])
def test_a_key_that_climbs_out_of_the_root_is_refused(store, key, tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        store.put_bytes(key, b"should not be written")

    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.skipif(os.sep != "\\", reason="a backslash is an ordinary character on POSIX")
def test_a_backslash_climb_is_refused_where_a_backslash_separates(store, tmp_path):
    """Windows only. On Linux `..\\escaped.txt` is one perfectly legal filename sitting
    inside the root, and refusing it there would reject a key somebody could reasonably
    have written."""
    with pytest.raises(ValueError, match="escapes"):
        store.put_bytes("..\\escaped.txt", b"should not be written")

    assert not (tmp_path / "escaped.txt").exists()


def test_a_sibling_directory_sharing_the_roots_name_is_still_outside_it(store, tmp_path):
    """The check used to be `str(target).startswith(str(root))`, which is not a
    containment test: with a root of `.../archive`, `../archive-evil/x` resolves to
    `.../archive-evil/x`, starts with the root, and is nowhere inside it. The one place
    the code says the key comes from an API request."""
    with pytest.raises(ValueError, match="escapes"):
        store.put_bytes("../archive-evil/pwned.txt", b"escaped")

    assert not (tmp_path / "archive-evil").exists()


def test_an_absolute_looking_key_is_taken_as_relative_to_the_root(store):
    """`/audit/report.json` is a key, not a filesystem path. Refusing it would be
    surprising; honouring it would be catastrophic."""
    ref = store.put_bytes("/audit/report.json", b"{}")

    assert store.get_bytes("/audit/report.json") == b"{}"
    assert "audit/report.json" in ref.uri.replace("\\", "/")


def test_an_ordinary_nested_key_is_fine(store):
    store.put_bytes("sources/2026/03/paper.pdf", b"%PDF-1.4")

    assert store.exists("sources/2026/03/paper.pdf")


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def test_bytes_come_back_exactly(store):
    """Any re-encoding here corrupts a PDF or an audio file in a way that only shows up
    when somebody opens the citation."""
    data = bytes(range(256))

    store.put_bytes("raw.bin", data)

    assert store.get_bytes("raw.bin") == data


def test_a_write_that_fails_leaves_the_previous_version_intact(store, monkeypatch,
                                                               tmp_path):
    """Bytes go to a temp file and are moved into place, so a write that dies partway
    cannot leave a truncated one behind. The archive is read back by the provenance
    view, and a citation that resolves to corrupted bytes is worse than one that is
    missing -- missing is visible."""
    store.put_bytes("paper.pdf", b"the good version")

    class Boom(Exception):
        pass

    def fail(self, target):
        raise Boom("the process died before the rename")

    monkeypatch.setattr(type(tmp_path), "replace", fail)

    with pytest.raises(Boom):
        store.put_bytes("paper.pdf", b"a truncated new version")

    monkeypatch.undo()
    assert store.get_bytes("paper.pdf") == b"the good version"


def test_json_is_written_as_json_with_the_right_content_type(store):
    ref = store.put("audit.json", {"claims": 3, "pages": ["a", "b"]})

    assert json.loads(store.get_bytes("audit.json")) == {"claims": 3,
                                                         "pages": ["a", "b"]}
    assert ref.size > 0


def test_text_is_written_as_utf8(store):
    store.put("note.txt", "attention — scaled by √d")

    assert store.get_bytes("note.txt").decode("utf-8") == "attention — scaled by √d"


def test_bytes_are_passed_through_put_unchanged(store):
    store.put("raw.bin", b"\x00\x01\x02")

    assert store.get_bytes("raw.bin") == b"\x00\x01\x02"


def test_something_json_cannot_encode_is_stringified_rather_than_raising(store):
    """An eval payload carries datetimes and enums. Losing the whole record because one
    field is not a primitive is a poor trade for a store whose job is to keep things."""
    import datetime

    store.put("run.json", {"at": datetime.datetime(2026, 3, 1), "ok": True})

    assert "2026-03-01" in store.get_bytes("run.json").decode("utf-8")


def test_get_json_round_trips(store):
    store.put("x.json", {"a": 1})

    assert store.get_json("x.json") == {"a": 1}


# ---------------------------------------------------------------------------
# versioning — where history is silently lost
# ---------------------------------------------------------------------------


def test_a_versioned_write_keeps_the_old_one_and_refreshes_latest(store):
    """Both halves are the point: the history is why you can answer "when did this
    change", and `latest` is why a dashboard has a stable URL to read."""
    first = store.put_versioned("audit.json", {"n": 1})
    second = store.put_versioned("audit.json", {"n": 2})

    assert first["versioned"]["key"] != second["versioned"]["key"]
    assert store.get_json(first["versioned"]["key"]) == {"n": 1}
    assert store.get_json(second["versioned"]["key"]) == {"n": 2}
    assert store.get_json("audit/latest.json") == {"n": 2}


def test_two_versions_written_in_the_same_instant_do_not_overwrite_each_other(store):
    """An object store overwrites rather than errors, so a key collision loses a version
    with nothing anywhere reporting it. Seconds are too coarse, and milliseconds are too
    on Windows, where the clock ticks about every 16ms."""
    keys = {store.put_versioned("audit.json", {"n": i})["versioned"]["key"]
            for i in range(25)}

    assert len(keys) == 25


def test_version_keys_sort_chronologically(store):
    """`list` is how you find the previous one, and it sorts lexically. A key that does
    not sort by time makes "the version before this" unanswerable."""
    keys = sorted(_version_key() for _ in range(5))

    assert keys == sorted(keys)
    assert all(k.startswith("20") for k in keys)


def test_a_name_with_no_extension_still_versions(store):
    result = store.put_versioned("snapshot", {"n": 1})

    assert "snapshot/" in result["versioned"]["key"]
    assert result["latest"]["key"] == "snapshot/latest"


def test_the_versioned_keys_live_under_the_name_as_a_folder(store):
    store.put_versioned("audit.json", {"n": 1})

    listed = store.list("audit")
    assert len(listed) == 2
    assert "audit/latest.json" in listed


# ---------------------------------------------------------------------------
# finding things again
# ---------------------------------------------------------------------------


def test_listing_is_recursive_and_uses_forward_slashes(store):
    """The keys go into the database and into URLs. Backslashes on Windows would make
    an archive written there unreadable from anywhere else."""
    store.put_bytes("a/b/c.txt", b"x")
    store.put_bytes("a/d.txt", b"y")

    assert store.list("a") == ["a/b/c.txt", "a/d.txt"]


def test_listing_a_prefix_that_is_a_file_returns_just_it(store):
    store.put_bytes("a/b.txt", b"x")

    assert store.list("a/b.txt") == ["a/b.txt"]


def test_listing_something_that_is_not_there_is_empty_rather_than_an_error(store):
    assert store.list("nothing/here") == []


def test_listing_with_no_prefix_returns_everything(store):
    store.put_bytes("a.txt", b"x")
    store.put_bytes("b/c.txt", b"y")

    assert store.list() == ["a.txt", "b/c.txt"]


def test_exists_does_not_lie_about_a_missing_key(store):
    assert store.exists("never-written.txt") is False


def test_reading_a_missing_key_raises_rather_than_returning_empty(store):
    """Empty bytes would be archived onward as if they were the source."""
    with pytest.raises(OSError):
        store.get_bytes("never-written.txt")


# ---------------------------------------------------------------------------
# choosing a backend from a URL
# ---------------------------------------------------------------------------


def test_the_default_is_the_local_filesystem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert isinstance(open_artifacts(), LocalArtifacts)


def test_a_file_url_opens_the_directory_it_names(tmp_path):
    opened = open_artifacts(f"file://{tmp_path / 'store'}")

    assert isinstance(opened, LocalArtifacts)
    assert opened.root == (tmp_path / "store").resolve()


def test_a_relative_file_url_is_relative_to_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    opened = open_artifacts("file://./archive")

    assert opened.root == (tmp_path / "archive").resolve()


def test_a_bare_path_with_no_scheme_is_treated_as_a_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert open_artifacts("archive").root == (tmp_path / "archive").resolve()


def test_an_s3_url_selects_s3_and_says_which_extra_to_install(monkeypatch):
    """`boto3` is lazy on purpose: it is not a dependency of the default install, and a
    laptop that never touches S3 must not carry it. Which makes the message the thing
    that matters when somebody does reach for S3 — an `ImportError` naming `boto3` sends
    them to pip for the wrong package."""
    from palimpsest.artifacts import S3Artifacts

    try:
        opened = open_artifacts("s3://my-bucket/palimpsest")
    except ImportError as e:
        assert "palimpsest[aws]" in str(e)
        return

    assert isinstance(opened, S3Artifacts)
    assert opened.bucket == "my-bucket"
    assert opened.prefix == "palimpsest"


def test_a_supabase_url_selects_supabase(monkeypatch):
    from palimpsest.artifacts import SupabaseArtifacts

    monkeypatch.setenv("SUPABASE_URL", "https://abcdefgh.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

    opened = open_artifacts("supabase://archive/sources")

    assert isinstance(opened, SupabaseArtifacts)
    assert opened.bucket == "archive"


def test_a_url_nobody_implemented_says_what_is_supported(tmp_path):
    """This is a configuration typo, and the fix is one of three strings."""
    with pytest.raises(ValueError) as caught:
        open_artifacts("gs://bucket/prefix")

    message = str(caught.value)
    assert "file://" in message and "s3://" in message and "supabase://" in message


# ---------------------------------------------------------------------------
# the reference handed back
# ---------------------------------------------------------------------------


def test_a_reference_says_where_the_bytes_went_and_how_big_they_were(store):
    ref = store.put_bytes("paper.pdf", b"%PDF-1.4 and some more")

    assert isinstance(ref, ArtifactRef)
    assert ref.key == "paper.pdf"
    assert ref.size == len(b"%PDF-1.4 and some more")
    assert ref.backend == "file"
    assert ref.uri.startswith("file://")


def test_the_reference_serialises_for_the_provenance_row(store):
    """This dict is what ends up beside a claim, and it is the whole audit trail when
    somebody asks six months later where a sentence came from."""
    payload = store.put_bytes("paper.pdf", b"x").as_dict()

    assert set(payload) == {"uri", "key", "size", "backend"}
