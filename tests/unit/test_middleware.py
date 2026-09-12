"""What has to be true before this listens on anything but localhost.

The auth tests are the point. `PALIMPSEST_API_KEY` is the only thing between a public
bind and someone's notes — readable *and* writable — so "a request without a token is
refused" is not a nicety, and neither is the constant-time comparison behind it.

The metrics tests look like housekeeping and are not. A label built from the URL means
every patch id becomes a permanent counter in a process that runs for weeks, and puts
those ids on an endpoint that stays open even when a key is set.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from palimpsest.config import Settings
from palimpsest.serve.app import AppState, create_app
from palimpsest.serve.middleware import Metrics, _route_of, _template

pytestmark = pytest.mark.serve

KEY = "k" * 40


def _client(tmp_path, **overrides):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'm.db'}",
                        artifact_url=f"file://{tmp_path / 'a'}", **overrides)
    return TestClient(create_app(AppState(settings=settings)))


@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    """A server bound to a network interface with a key set — the deployed shape."""
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    with _client(tmp_path, host="0.0.0.0", api_key=KEY) as c:
        yield c


@pytest.fixture()
def local(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    with _client(tmp_path) as c:
        yield c


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


def test_a_request_with_no_token_is_refused(guarded):
    response = guarded.get("/v1/status")

    assert response.status_code == 401
    assert "PALIMPSEST_API_KEY" in response.json()["detail"]
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_request_with_the_wrong_token_is_refused(guarded):
    response = guarded.get("/v1/status", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(guarded):
    """The reason the comparison is `compare_digest` and not `==`: a plain comparison
    returns as soon as two bytes differ, and the timing difference is enough to recover
    the key one character at a time."""
    response = guarded.get("/v1/status", headers={"Authorization": f"Bearer {KEY[:20]}"})

    assert response.status_code == 401


def test_a_bearer_token_gets_through(guarded):
    assert guarded.get("/v1/status",
                       headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_the_scheme_is_matched_case_insensitively(guarded):
    """Clients spell it `bearer`, `Bearer` and `BEARER`. Refusing two of the three is a
    401 nobody can explain."""
    assert guarded.get("/v1/status",
                       headers={"Authorization": f"bearer {KEY}"}).status_code == 200


def test_an_api_key_header_also_works(guarded):
    """The browser extension sends this one; `fetch` with an Authorization header trips
    a preflight that some pages' policies then block."""
    assert guarded.get("/v1/status", headers={"X-API-Key": KEY}).status_code == 200


def test_health_checks_are_never_asked_for_a_token(guarded):
    """A load balancer cannot present one. Requiring it means the task never becomes
    healthy and the deployment rolls back forever, with a perfectly good application
    inside it."""
    assert guarded.get("/healthz").status_code == 200
    assert guarded.get("/readyz").status_code in (200, 503)


def test_nothing_is_asked_for_a_token_when_no_key_is_set(local):
    """The local experience is unchanged: a desktop app talking to its own backend over
    loopback should not have to carry a credential."""
    assert local.get("/v1/status").status_code == 200


def test_an_unauthorised_request_never_reaches_the_route(guarded):
    """The check runs before `call_next`, so a refused write is refused before anything
    has a chance to touch the store."""
    response = guarded.post("/v1/jobs", json={"text": "a thought"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# request ids
# ---------------------------------------------------------------------------


def test_every_response_carries_a_request_id(local):
    assert local.get("/v1/status").headers["X-Request-ID"]


def test_a_supplied_request_id_is_kept_so_a_trace_joins_up(local):
    """When something in front of this already assigned an id, inventing a second one
    breaks the only link between the two logs."""
    response = local.get("/v1/status", headers={"X-Request-ID": "abc123"})

    assert response.headers["X-Request-ID"] == "abc123"


def test_a_refusal_carries_one_too(guarded):
    """Otherwise the one request you most want to find in a log is the one with no id."""
    assert guarded.get("/v1/status").headers["X-Request-ID"]


# ---------------------------------------------------------------------------
# body limits
# ---------------------------------------------------------------------------


def test_an_enormous_body_is_refused_by_its_header_not_by_reading_it(local):
    """`POST /v1/ingest` accepts a pasted document. Reading it to find out it is too big
    is how a 2 GB paste takes the process with it."""
    response = local.post("/v1/ingest", json={"text": "x"},
                          headers={"Content-Length": str(64 * 1024 * 1024)})

    assert response.status_code == 413
    assert "MB" in response.json()["detail"]


def test_an_ordinary_body_is_not_refused(local):
    assert local.post("/v1/ingest", json={"text": "a thought"}).status_code != 413


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_metrics_render_as_prometheus_text(local):
    local.get("/v1/status")

    body = local.get("/metrics").text

    assert "# TYPE palimpsest_requests_total counter" in body
    assert 'palimpsest_requests_total{method="GET",path="/v1/status",status="200"}' in body
    assert "palimpsest_uptime_seconds" in body


def test_an_id_in_the_url_does_not_become_its_own_metric_series(local):
    """`request.url.path` carries the ids, so `/v1/patches/pch_48075473/undo` used to be
    its own counter -- a new one for every patch and every job, forever, in a process
    that runs for weeks. Unbounded memory, and every id on an endpoint that stays open
    even when an API key is set."""
    for patch_id in ("pch_1111aaaa2222", "pch_3333bbbb4444", "pch_5555cccc6666"):
        local.get(f"/v1/patches/{patch_id}")

    body = local.get("/metrics").text

    assert "pch_1111aaaa2222" not in body
    assert "pch_3333bbbb4444" not in body
    assert 'path="/v1/patches/{patch_id}"' in body


def test_an_unmatched_url_does_not_become_a_series_either(local):
    """A scanner walking random URLs would otherwise create a counter per URL it tried,
    which is a memory leak anybody on the internet can trigger."""
    for i in range(5):
        local.get(f"/v1/nope/pch_0000dead{i:04d}")

    body = local.get("/metrics").text

    assert "pch_0000dead" not in body


def test_a_refused_request_is_counted_without_its_id(guarded):
    """Auth runs before routing, so there is no matched route to ask -- and one 401 per
    id would be the same leak through the other door."""
    guarded.get("/v1/patches/pch_9999ffff8888")

    body = guarded.get("/metrics").text

    assert "pch_9999ffff8888" not in body
    assert 'status="401"' in body


def test_latency_buckets_are_cumulative(local):
    """A histogram whose buckets are not cumulative gives Prometheus quantiles that are
    quietly wrong, and nothing about the output looks broken."""
    metrics = Metrics()
    metrics.observe("GET", "/v1/x", 200, 0.02)

    body = metrics.render()
    counts = {}
    for line in body.splitlines():
        if "_bucket{" in line:
            le = line.split('le="')[1].split('"')[0]
            counts[le] = int(line.rsplit(" ", 1)[1])

    assert counts["0.001"] == 0
    assert counts["0.05"] == 1
    assert counts["+Inf"] == 1
    ordered = [counts[str(b)] for b in (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)]
    assert ordered == sorted(ordered)


def test_server_errors_are_counted_separately():
    metrics = Metrics()

    metrics.observe("GET", "/v1/x", 500, 0.1)
    metrics.observe("GET", "/v1/x", 404, 0.1)

    assert metrics.errors == 1, "a 404 is the caller's problem, a 500 is ours"


def test_extra_gauges_are_rendered_and_non_numbers_are_skipped():
    """`render` is handed the scorer's numbers, which include strings. A string emitted
    as a gauge makes Prometheus reject the whole scrape, not just that line."""
    body = Metrics().render({"pages_mirrored": 42, "model": "claude-opus-5"})

    assert "palimpsest_pages_mirrored 42" in body
    assert "claude-opus-5" not in body


def test_metrics_stay_open_so_a_scraper_can_reach_them(guarded):
    assert guarded.get("/metrics").status_code == 200


# ---------------------------------------------------------------------------
# the templating underneath
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/v1/patches/pch_48075473fd0f4c12", "/v1/patches/{id}"),
    ("/v1/patches/pch_48075473fd0f4c12/undo", "/v1/patches/{id}/undo"),
    ("/v1/jobs/job_7545ccee8f9d49c1", "/v1/jobs/{id}"),
    ("/v1/approvals/apr_1234abcd5678/resolve", "/v1/approvals/{id}/resolve"),
    ("/v1/status", "/v1/status"),
    ("/", "/"),
    ("/v1/sweep/duplicates", "/v1/sweep/duplicates"),
])
def test_only_id_shaped_segments_are_replaced(path, expected):
    """Over-matching is its own bug: `/v1/sweep/duplicates` collapsing to `{id}` merges
    four different sweeps into one line and makes the metric useless."""
    assert _template(path) == expected


def test_the_matched_route_wins_over_the_guess():
    """The guess is a fallback for requests that never reached routing. When Starlette
    has told us the template, that is the truth."""
    class Request:
        scope = {"route": type("R", (), {"path": "/v1/pages/{page_id}"})()}

    assert _route_of(Request(), "/v1/pages/anything-at-all") == "/v1/pages/{page_id}"
