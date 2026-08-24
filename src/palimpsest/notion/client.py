"""A small, explicit Notion REST client.

Hand-rolled on `urllib` rather than the SDK, for three reasons that all turned out to
matter: the surface we need is eight endpoints; the SDK pins an httpx range that fights
with everything else in a deployment; and pagination, rate limiting and retry are the
parts you actually have to get right, which a wrapper does not do for you.

**The version is pinned, deliberately.** Notion's API is versioned by date and the
`2025-09-03` release renamed half the world: databases became containers holding *data
sources*, and `search` stopped accepting `value: "database"` in favour of
`value: "data_source"`. Letting `Notion-Version` float would change response shapes
under a running deployment. It is a constant here and a setting you can override.

**Rate limiting is not optional.** Notion allows roughly three requests per second
averaged, and a first full mirror of a real workspace is thousands of requests. Without
a limiter you get 429s within seconds; with a naive retry you get a thundering herd.
This client paces itself with a token bucket and honours `Retry-After`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

__all__ = ["NotionClient", "NotionError", "RateLimiter"]

log = logging.getLogger("palimpsest.notion")

API = "https://api.notion.com/v1"

#: Notion's documented ceiling is ~3 requests/second averaged. We pace slightly under
#: it: the goal is a sync that never trips a 429, not one that finishes 8% sooner.
DEFAULT_RPS = 2.5

#: The API caps a children fetch and a children append at 100 items each.
PAGE_SIZE = 100
MAX_APPEND = 100


class NotionError(RuntimeError):
    """An error from the Notion API, with the bits needed to act on it."""

    def __init__(self, status: int, code: str, message: str, request_id: str | None = None):
        super().__init__(f"notion {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_retryable(self) -> bool:
        return self.status == 429 or self.status >= 500

    @property
    def is_not_found(self) -> bool:
        """404 usually means "not shared with the integration", not "does not exist"."""
        return self.status == 404


class RateLimiter:
    """A thread-safe token bucket. Shared across worker threads during a sync."""

    def __init__(self, rate: float = DEFAULT_RPS, burst: int = 3):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                time.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


class NotionClient:
    """The eight endpoints palimpsest needs, paced and retried."""

    def __init__(self, token: str, version: str = "2026-03-11",
                 rate: float = DEFAULT_RPS, timeout: float = 60.0, max_retries: int = 5):
        if not token:
            raise ValueError(
                "no Notion token. Create an internal integration at "
                "https://www.notion.so/my-integrations, copy the secret, and set "
                "NOTION_TOKEN. Then share at least one page with the integration — "
                "an integration sees nothing until you do."
            )
        self.token = token
        self.version = version
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = RateLimiter(rate)
        self.calls = 0

    # -- transport -------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None) -> dict:
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items()
                                                 if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None

        last: NotionError | None = None
        for attempt in range(1, self.max_retries + 1):
            self.limiter.take()
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Notion-Version", self.version)
            req.add_header("Content-Type", "application/json")
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {}
                err = NotionError(e.code, payload.get("code", "unknown"),
                                  payload.get("message", raw[:300]),
                                  payload.get("request_id"))
                if not err.is_retryable or attempt == self.max_retries:
                    raise err from e
                # Honour Retry-After when Notion sends it; otherwise back off.
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after else min(30.0, 1.5 * (2 ** (attempt - 1)))
                log.warning("notion %s %s -> %s; retrying in %.1fs (attempt %d/%d)",
                            method, path, e.code, wait, attempt, self.max_retries)
                time.sleep(wait)
                last = err
            except urllib.error.URLError as e:  # pragma: no cover - network
                if attempt == self.max_retries:
                    raise NotionError(0, "network", str(e.reason)) from e
                time.sleep(min(30.0, 1.5 * (2 ** (attempt - 1))))
        raise last or NotionError(0, "unknown", "request failed")  # pragma: no cover

    def _paginate(self, method: str, path: str, body: dict | None = None,
                  params: dict | None = None) -> Iterator[dict]:
        """Walk `start_cursor` / `has_more` and yield every result.

        Cursor goes in the body for POST endpoints and the query string for GET ones —
        a difference that is easy to get wrong and silently returns only page one.
        """
        cursor: str | None = None
        while True:
            if method == "POST":
                payload = dict(body or {})
                payload["page_size"] = PAGE_SIZE
                if cursor:
                    payload["start_cursor"] = cursor
                page = self._request(method, path, payload, params)
            else:
                query = dict(params or {})
                query["page_size"] = PAGE_SIZE
                if cursor:
                    query["start_cursor"] = cursor
                page = self._request(method, path, None, query)
            yield from page.get("results", [])
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # -- reads -----------------------------------------------------------------

    def whoami(self) -> dict:
        """Identify the integration. The cheapest way to validate a token."""
        return self._request("GET", "users/me")

    def search_pages(self, query: str = "") -> Iterator[dict]:
        """Every page shared with the integration, newest edit first.

        The filter value is `"page"`. Under API 2025-09-03 and later the only other
        legal value is `"data_source"` — `"database"` was removed, and sending it is a
        400 that reads like a permissions problem.
        """
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"timestamp": "last_edited_time", "direction": "descending"},
        }
        if query:
            body["query"] = query
        yield from self._paginate("POST", "search", body)

    def search_data_sources(self, query: str = "") -> Iterator[dict]:
        """Data sources (what pre-2025-09-03 callers thought of as databases)."""
        body: dict[str, Any] = {"filter": {"property": "object", "value": "data_source"}}
        if query:
            body["query"] = query
        yield from self._paginate("POST", "search", body)

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"pages/{page_id}")

    def get_block(self, block_id: str) -> dict:
        return self._request("GET", f"blocks/{block_id}")

    def block_children(self, block_id: str) -> Iterator[dict]:
        yield from self._paginate("GET", f"blocks/{block_id}/children")

    # -- writes ----------------------------------------------------------------

    def append_children(self, parent_id: str, children: list[dict],
                        after_block_id: str | None = None) -> dict:
        """Append blocks, optionally after a specific sibling.

        `position` replaced the deprecated `after` parameter. Passing `after` on a
        current API version is silently ignored, so the blocks land at the end of the
        page instead of next to the sentence they annotate — a wrong-looking edit with
        no error to explain it.
        """
        if len(children) > MAX_APPEND:
            raise ValueError(f"Notion accepts at most {MAX_APPEND} children per call; "
                             f"got {len(children)}. Chunk before calling.")
        body: dict[str, Any] = {"children": children}
        if after_block_id:
            body["position"] = {"type": "after_block", "after_block": {"id": after_block_id}}
        return self._request("PATCH", f"blocks/{parent_id}/children", body)

    def update_block(self, block_id: str, payload: dict) -> dict:
        """Update a block in place. `payload` is `{<block type>: {...}}`."""
        return self._request("PATCH", f"blocks/{block_id}", payload)

    def archive_block(self, block_id: str) -> dict:
        """Move a block to the trash.

        Called `archive` here and never `delete`, because that is what it is: Notion
        keeps the block and it is restorable from the UI. palimpsest never issues the
        hard DELETE.
        """
        return self._request("PATCH", f"blocks/{block_id}", {"archived": True})

    def restore_block(self, block_id: str) -> dict:
        return self._request("PATCH", f"blocks/{block_id}", {"archived": False})

    def create_page(self, parent_page_id: str, title: str,
                    children: list[dict] | None = None,
                    icon: str | None = None) -> dict:
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
            },
        }
        if children:
            body["children"] = children[:MAX_APPEND]
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        return self._request("POST", "pages", body)

    def archive_page(self, page_id: str) -> dict:
        return self._request("PATCH", f"pages/{page_id}", {"archived": True})

    def update_page(self, page_id: str, payload: dict) -> dict:
        """Patch a page's own attributes — title, icon, cover.

        Note that `parent` is *not* accepted here. Notion documents plainly that "a
        page's parent cannot be changed" through this endpoint; moving is a separate
        call, below.
        """
        return self._request("PATCH", f"pages/{page_id}", payload)

    def rename_page(self, page_id: str, title: str) -> dict:
        """Set a page's title.

        The property is keyed `title` because these are page-parented pages. A row in a
        database keys its title by whatever that database called the column, which is
        why the organiser only ever renames pages whose parent is a page.
        """
        return self.update_page(page_id, {
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
            }
        })

    def set_page_icon(self, page_id: str, icon: str | None) -> dict:
        """Set or clear a page's emoji icon. `None` clears it, which is the inverse of
        setting one on a page that had none."""
        body = {"icon": {"type": "emoji", "emoji": icon} if icon else None}
        return self.update_page(page_id, body)

    # -- databases -------------------------------------------------------------
    #
    # The 2025-09-03 release split a database into a container plus one or more *data
    # sources*. Rows are created against a `data_source_id`, never the `database_id` —
    # sending the latter works today on a single-source database and breaks the moment
    # a second source is added, which is the kind of failure that surfaces months later.

    def create_database(self, parent_page_id: str, title: str,
                        properties: dict, icon: str | None = None,
                        description: str | None = None) -> dict:
        """Create a database. The property schema goes inside `initial_data_source`."""
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title[:2000]}}],
            "initial_data_source": {"properties": properties},
        }
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        if description:
            body["description"] = [{"type": "text", "text": {"content": description[:2000]}}]
        return self._request("POST", "databases", body)

    def get_database(self, database_id: str) -> dict:
        return self._request("GET", f"databases/{database_id}")

    def data_source_id(self, database: dict) -> str | None:
        """The first data source of a created or fetched database."""
        sources = database.get("data_sources") or []
        return (sources[0].get("id") or "").replace("-", "") if sources else None

    def create_row(self, data_source_id: str, properties: dict,
                   children: list[dict] | None = None,
                   icon: str | None = None) -> dict:
        """Add a row to a database."""
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if children:
            body["children"] = children[:MAX_APPEND]
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        return self._request("POST", "pages", body)

    def query_data_source(self, data_source_id: str, filter_: dict | None = None,
                          sorts: list[dict] | None = None) -> Iterator[dict]:
        body: dict[str, Any] = {}
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        yield from self._paginate("POST", f"data_sources/{data_source_id}/query", body)

    def move_page(self, page_id: str, parent_page_id: str) -> dict:
        """Move a page under a different parent page.

        This is a dedicated endpoint (`POST /v1/pages/{id}/move`), not a field on the
        page patch — Notion added it separately and the patch route still refuses a
        `parent`. Sending it the other way is a silent no-op on the parent, which looks
        like a move that did nothing.

        **It cannot move a page back to the workspace top level.** The body takes a
        `page_id` or a `data_source_id` and there is no workspace variant, so a page
        currently parented by the workspace can be filed *into* a hub but not restored
        by this API. `plan_move` refuses to emit such an operation for exactly that
        reason: an edit whose inverse does not exist is not one this project performs.
        """
        return self._request("POST", f"pages/{page_id}/move", {
            "parent": {"type": "page_id", "page_id": parent_page_id}
        })
