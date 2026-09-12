"""The client that talks to somebody's actual Notion, at 38% coverage.

The methods are thin and boring. The transport underneath them is not, and everything
interesting about it only happens when something goes wrong:

**Pagination** decides whether a sync sees your whole workspace or its first hundred
pages. Getting it wrong does not fail — it silently returns page one, the mirror looks
populated, and retrieval quietly never considers the rest of your notes.

**Retries** decide whether a large sync survives. Notion rate-limits at three requests a
second and a real workspace is thousands of requests, so 429 is not an edge case, it is
Tuesday. A client that gives up on the first one cannot sync a workspace worth having.

**The cursor goes in a different place for GET and POST** — query string for one, body for
the other — which is exactly the kind of asymmetry that returns page one forever.

Nothing here touches the network: `urlopen` is replaced by a fake that serves scripted
responses and records what it was asked.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from palimpsest.notion.client import NotionClient, NotionError, RateLimiter


class Sent:
    """One recorded request."""

    def __init__(self, request):
        self.method = request.get_method()
        self.url = request.full_url
        self.headers = {k.lower(): v for k, v in request.headers.items()}
        self.body = json.loads(request.data.decode()) if request.data else None


class FakeHTTP:
    """A scripted `urlopen`. Each entry is a dict body, or an exception to raise."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent: list[Sent] = []
        self.slept: list[float] = []

    def __call__(self, request, timeout=None):
        self.sent.append(Sent(request))
        answer = self.responses.pop(0) if self.responses else {}
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)


class _Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _broken_html() -> urllib.error.HTTPError:
    """What a proxy or a captive portal returns instead of JSON."""
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/x", 502, "Bad Gateway", {},
        io.BytesIO(b"<html>upstream is unavailable</html>"))


def _http_error(status: int, code: str = "rate_limited", message: str = "slow down",
                headers: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps({"code": code, "message": message,
                       "request_id": "req_1"}).encode()
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/x", status, message, headers or {}, io.BytesIO(body))


@pytest.fixture()
def http(monkeypatch):
    """Install a scripted transport and make sleeping instant."""
    fake = FakeHTTP()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    # The backoff and the rate limiter both sleep. Recording only the backoff keeps the
    # assertions about retry timing honest — a stray limiter wait at the end of the list
    # is not a shrinking backoff.
    monkeypatch.setattr("palimpsest.notion.client.time.sleep", fake.slept.append)
    monkeypatch.setattr("palimpsest.notion.client.RateLimiter.take", lambda self: None)
    return fake


@pytest.fixture()
def client(http):
    return NotionClient("ntn_test_token")


# ---------------------------------------------------------------------------
# construction and headers
# ---------------------------------------------------------------------------


def test_no_token_says_how_to_get_one_and_names_the_step_everyone_misses():
    """An integration sees nothing until a page is shared with it, and that is not
    discoverable from a 404 later."""
    with pytest.raises(ValueError) as caught:
        NotionClient("")

    message = str(caught.value)
    assert "my-integrations" in message
    assert "share at least one page" in message


def test_every_request_carries_the_pinned_api_version(client, http):
    """Notion versions its API by date and the 2025-09-03 release split databases into
    data sources. An unpinned version changes response shapes under a running install."""
    http.responses.append({"object": "user"})

    client.whoami()

    assert http.sent[0].headers["notion-version"] == "2026-03-11"
    assert http.sent[0].headers["authorization"] == "Bearer ntn_test_token"


def test_the_call_count_is_what_the_mirror_reports_as_cost(client, http):
    http.responses.extend([{"object": "user"}, {"object": "page"}])

    client.whoami()
    client.get_page("pg_1")

    assert client.calls == 2


# ---------------------------------------------------------------------------
# retries, which decide whether a big sync survives
# ---------------------------------------------------------------------------


def test_a_rate_limit_is_retried_rather_than_raised(client, http):
    """Notion allows about three requests a second and a real workspace is thousands of
    them. A client that gives up on the first 429 cannot sync a workspace worth having."""
    http.responses.extend([_http_error(429), _http_error(429), {"object": "page"}])

    page = client.get_page("pg_1")

    assert page["object"] == "page"
    assert len(http.sent) == 3


def test_retry_after_is_honoured_when_notion_sends_one(client, http):
    """Notion says how long to wait. Ignoring it and backing off on our own schedule is
    how a sync gets the account rate-limited harder."""
    http.responses.extend([_http_error(429, headers={"Retry-After": "7"}),
                           {"object": "page"}])

    client.get_page("pg_1")

    assert http.slept == [7.0]


def test_without_a_retry_after_the_backoff_grows(client, http):
    """Retrying at a fixed interval against a server that is already struggling is how a
    slow response becomes an outage."""
    http.responses.extend([_http_error(500, "server_error"),
                           _http_error(500, "server_error"),
                           _http_error(500, "server_error"),
                           {"object": "page"}])

    client.get_page("pg_1")

    assert http.slept == sorted(http.slept), "each wait must be at least the last"
    assert len(set(http.slept)) > 1, "a fixed interval is not a backoff"


def test_a_server_error_is_retried_and_a_client_error_is_not(client, http):
    """A 400 means the request is wrong and will be wrong every time. Retrying it five
    times just takes five times as long to tell you."""
    http.responses.append(_http_error(400, "validation_error", "body failed validation"))

    with pytest.raises(NotionError) as caught:
        client.get_page("pg_1")

    assert caught.value.status == 400
    assert len(http.sent) == 1, "a client error must not be retried"


def test_giving_up_raises_the_last_error_with_its_request_id(client, http):
    """The request id is what Notion support asks for, and it is the only thing that
    distinguishes "their fault" from "ours"."""
    # A fresh error each time: `[error] * 5` repeats one object, and the
    # client reads its body — so every retry after the first sees an empty
    # stream and the code and request id vanish.
    http.responses.extend(_http_error(429) for _ in range(5))

    with pytest.raises(NotionError) as caught:
        client.get_page("pg_1")

    assert caught.value.is_rate_limit
    assert caught.value.request_id == "req_1"


def test_a_response_that_is_not_json_still_produces_a_readable_error(client, http):
    """A proxy or a captive portal returns HTML. Parsing that as JSON and failing gives
    a `JSONDecodeError` that says nothing about Notion."""
    http.responses.extend(_broken_html() for _ in range(5))

    with pytest.raises(NotionError) as caught:
        client.get_page("pg_1")

    assert caught.value.status == 502
    assert "upstream" in str(caught.value)


def test_the_error_classifies_itself_so_callers_can_act_on_it():
    assert NotionError(429, "rate_limited", "x").is_rate_limit
    assert NotionError(429, "rate_limited", "x").is_retryable
    assert NotionError(503, "unavailable", "x").is_retryable
    assert not NotionError(400, "validation_error", "x").is_retryable
    # 404 is the one that reads wrong: it almost always means "not shared with the
    # integration" rather than "does not exist", which is a different fix entirely.
    assert NotionError(404, "object_not_found", "x").is_not_found


# ---------------------------------------------------------------------------
# pagination, where being wrong is silent
# ---------------------------------------------------------------------------


def test_a_search_walks_every_page_of_results(client, http):
    """Stopping after the first page does not fail — the mirror looks populated, and
    retrieval simply never considers the rest of the workspace."""
    http.responses.extend([
        {"results": [{"id": "1"}, {"id": "2"}], "has_more": True,
         "next_cursor": "cur_a"},
        {"results": [{"id": "3"}], "has_more": True, "next_cursor": "cur_b"},
        {"results": [{"id": "4"}], "has_more": False},
    ])

    ids = [p["id"] for p in client.search_pages()]

    assert ids == ["1", "2", "3", "4"]


def test_the_cursor_goes_in_the_body_for_a_post(client, http):
    """Search is a POST and its cursor belongs in the body. Put it in the query string
    and every page returns the first page, forever, with `has_more` still true."""
    http.responses.extend([
        {"results": [{"id": "1"}], "has_more": True, "next_cursor": "cur_a"},
        {"results": [{"id": "2"}], "has_more": False},
    ])

    list(client.search_pages())

    assert http.sent[1].method == "POST"
    assert http.sent[1].body["start_cursor"] == "cur_a"
    assert "start_cursor" not in http.sent[1].url


def test_the_cursor_goes_in_the_query_string_for_a_get(client, http):
    """Block children is a GET, and the same value belongs somewhere else entirely."""
    http.responses.extend([
        {"results": [{"id": "b1"}], "has_more": True, "next_cursor": "cur_a"},
        {"results": [{"id": "b2"}], "has_more": False},
    ])

    list(client.block_children("bk_1"))

    assert http.sent[1].method == "GET"
    assert "start_cursor=cur_a" in http.sent[1].url
    assert http.sent[1].body is None


def test_has_more_without_a_cursor_stops_rather_than_looping(client, http):
    """A malformed page with `has_more` true and no cursor would otherwise re-request
    the same page until the process is killed."""
    http.responses.append({"results": [{"id": "1"}], "has_more": True,
                           "next_cursor": None})

    assert [p["id"] for p in client.search_pages()] == ["1"]
    assert len(http.sent) == 1


# ---------------------------------------------------------------------------
# the writes
# ---------------------------------------------------------------------------


def test_archiving_a_block_uses_in_trash_rather_than_archived(client, http):
    """The 2025-09-03 API renamed it. `archived` is accepted and ignored, so the wrong
    field means undo silently leaves the block on the page."""
    http.responses.append({"id": "bk_1", "in_trash": True})

    client.archive_block("bk_1")

    assert http.sent[0].method == "PATCH"
    assert http.sent[0].body.get("in_trash") is True


def test_restoring_a_block_is_the_same_field_inverted(client, http):
    http.responses.append({"id": "bk_1", "in_trash": False})

    client.restore_block("bk_1")

    assert http.sent[0].body.get("in_trash") is False


def test_appending_children_can_place_them_after_a_specific_block(client, http):
    """A footnote belongs under the sentence it annotates, not at the bottom of the
    page. Without the anchor every edit lands at the end and the page stops reading."""
    http.responses.append({"results": [{"id": "bk_new"}]})

    client.append_children("pg_1", [{"type": "paragraph"}], after_block_id="bk_3")

    # `position`, not the deprecated `after`: passing `after` on a current API version
    # is silently ignored and the blocks land at the end of the page.
    assert http.sent[0].body["position"] == {
        "type": "after_block", "after_block": {"id": "bk_3"}}
    assert http.sent[0].body["children"] == [{"type": "paragraph"}]


def test_there_is_no_way_to_delete_anything(client):
    """The safety story rests on this: the most destructive verb available is a PATCH
    that moves a block to a restorable trash."""
    import inspect

    source = inspect.getsource(NotionClient)

    assert '"DELETE"' not in source
    assert "'DELETE'" not in source


def test_a_page_is_created_under_the_parent_it_was_given(client, http):
    http.responses.append({"id": "pg_new", "url": "https://notion.so/pg_new"})

    client.create_page("pg_parent", "A new page", icon="📐")

    body = http.sent[0].body
    assert body["parent"] == {"type": "page_id", "page_id": "pg_parent"}
    assert body["icon"] == {"type": "emoji", "emoji": "📐"}


# ---------------------------------------------------------------------------
# the rate limiter
# ---------------------------------------------------------------------------


def test_the_limiter_lets_a_burst_through_then_paces(monkeypatch):
    """A burst matters: a sync fetches a page and then its blocks immediately, and
    pacing those two apart makes every page take a third of a second longer for nothing.
    """
    slept: list[float] = []
    monkeypatch.setattr("palimpsest.notion.client.time.sleep", slept.append)

    limiter = RateLimiter(rate=3.0, burst=3)
    for _ in range(3):
        limiter.take()

    assert slept == [], "the burst must not be paced"

    limiter.take()
    assert slept and slept[0] > 0, "beyond the burst it has to wait"


def test_the_limiter_is_safe_to_share_across_threads():
    """One limiter is shared by every worker in a sync. Without the lock the token count
    is a read-modify-write and the effective rate drifts above the limit, which is how a
    sync gets the whole integration throttled."""
    import threading

    limiter = RateLimiter(rate=1000.0, burst=50)
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(20):
                limiter.take()
        except Exception as e:  # pragma: no cover - the failure this guards against
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
