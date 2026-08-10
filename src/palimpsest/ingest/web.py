"""Web pages: Firecrawl when a key is present, a stdlib reader when it is not.

Firecrawl's v2 `/scrape` endpoint handles the things that make scraping tedious —
JavaScript rendering, proxies, boilerplate stripping — and returns clean markdown. It
is a credit worth spending here, and this is the natural place for it.

The fallback is not a token gesture. A pure-stdlib HTML-to-text pass covers static
pages, which is most of what a note-taker actually reads: documentation, arXiv
abstracts, blog posts. Having it means the product works before you have signed up for
anything, and it is why the test suite can exercise this path offline.

**Segments come from headings.** A markdown document is split at its headings and each
span is anchored to its heading path, so a claim extracted from under "## Results"
cites `Results` rather than a character offset. That is the difference between a
footnote a human can use and one they cannot.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from palimpsest.ingest import make_source
from palimpsest.types import Source

__all__ = ["from_url", "html_to_text", "segments_from_markdown"]

log = logging.getLogger("palimpsest.ingest.web")

FIRECRAWL_API = "https://api.firecrawl.dev/v2/scrape"

USER_AGENT = ("Mozilla/5.0 (compatible; palimpsest/0.1; "
              "+https://github.com/itsskofficial/palimpsest)")

#: Elements whose text is never content.
_SKIP = frozenset({"script", "style", "noscript", "svg", "head", "nav", "footer",
                   "header", "aside", "form", "button", "iframe"})

_BLOCK = frozenset({"p", "div", "section", "article", "li", "tr", "br", "h1", "h2",
                    "h3", "h4", "h5", "h6", "blockquote", "pre", "table"})


class _Reader(HTMLParser):
    """A deliberately small HTML-to-text pass. Keeps headings, drops chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self.parts.append("\n")
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                self.parts.append("\n" + "#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        # The title check comes first: `<title>` lives inside `<head>`, which is in
        # _SKIP, so testing the skip depth first means no page ever yields a title.
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")


def html_to_text(raw: str) -> tuple[str, str]:
    """`(title, text)` from an HTML document, using only the standard library."""
    reader = _Reader()
    try:
        reader.feed(raw)
    except Exception:  # pragma: no cover - malformed markup should not be fatal
        log.debug("html parse aborted early; using what we have")
    text = "".join(reader.parts)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return reader.title.strip(), text.strip()


def segments_from_markdown(text: str, url: str | None = None) -> list[dict]:
    """Split at headings, anchoring each span to its heading path.

    The path is cumulative (`Results › Ablations`), so a citation names where in the
    document a claim lives rather than how far into it — which survives the page being
    edited, and reads like something a person would write.
    """
    segments: list[dict] = []
    stack: list[str] = []
    cursor = 0
    heading = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

    matches = list(heading.finditer(text))
    if not matches:
        return [{"start": 0, "end": len(text), "kind": "section",
                 "locator": "document", "url": url}]

    for i, m in enumerate(matches):
        if m.start() > cursor:
            segments.append({
                "start": cursor, "end": m.start(), "kind": "section",
                "locator": " › ".join(stack) or "preamble", "url": url,
            })
        level = len(m.group(1))
        title = m.group(2).strip()
        stack = [*stack[: level - 1], title]
        cursor = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segments.append({
            "start": cursor, "end": end, "kind": "section",
            "locator": " › ".join(stack), "url": url,
        })
        cursor = end
    return segments


def _firecrawl(url: str, api_key: str, timeout: float = 90.0) -> dict:
    """POST to Firecrawl v2 `/scrape` and return `data`."""
    body = json.dumps({
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        # Two days of cache: re-ingesting a page you read this morning should not
        # spend another credit.
        "maxAge": 172_800_000,
        "blockAds": True,
    }).encode("utf-8")
    req = urllib.request.Request(FIRECRAWL_API, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(f"firecrawl: {str(payload)[:200]}")
    return payload.get("data") or {}


def _fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def from_url(url: str, firecrawl_key: str | None = None) -> Source:
    """Fetch a page and normalise it, preferring Firecrawl when configured."""
    if firecrawl_key:
        try:
            data = _firecrawl(url, firecrawl_key)
            markdown = data.get("markdown") or ""
            meta = data.get("metadata") or {}
            if markdown.strip():
                title = meta.get("title") or url
                if isinstance(title, list):
                    title = title[0] if title else url
                return make_source(
                    "web", str(title), markdown, url=url,
                    segments=segments_from_markdown(markdown, url),
                    extractor="firecrawl",
                    description=meta.get("description"),
                    status_code=meta.get("statusCode"),
                    links=(data.get("links") or [])[:200],
                )
            log.warning("firecrawl returned no markdown for %s; falling back", url)
        except (urllib.error.URLError, RuntimeError, ValueError) as e:
            # A Firecrawl outage or an exhausted credit balance must not stop you
            # capturing a static page.
            log.warning("firecrawl failed for %s (%s); using the stdlib reader", url, e)

    raw = _fetch(url)
    title, text = html_to_text(raw)
    return make_source("web", title or url, text, url=url,
                       segments=segments_from_markdown(text, url),
                       extractor="stdlib")
