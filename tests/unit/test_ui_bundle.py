"""The built UI that ships inside the package is complete and self-consistent.

`palimpsest serve` hands out a Next.js static export that lives in the repository under
`serve/static/`, so a `pip install` needs no Node toolchain. That convenience has one
failure mode, and it is silent: someone edits the UI, forgets to run the ship script, and
the package goes out with an `index.html` pointing at chunk filenames that no longer
exist. The server returns 200 for the page and 404 for its JavaScript, and the app is a
blank vellum rectangle with nothing in the logs to explain it.

So: parse the shipped HTML, and assert every asset it asks for is actually in the wheel.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "palimpsest" / "serve" / "static"

# src="/_next/…" and href="/_next/…", which is every script, stylesheet and font the
# export references by URL.
ASSET = re.compile(r'(?:src|href)="(/_next/[^"]+)"')


@pytest.fixture(scope="module")
def index() -> str:
    page = STATIC / "index.html"
    if not page.is_file():
        pytest.skip("no built UI in this checkout; run clients/ui/scripts/ship.mjs")
    return page.read_text(encoding="utf-8")


def test_the_page_is_the_palimpsest_ui(index: str) -> None:
    assert "<title>palimpsest" in index or "palimpsest" in index


def test_every_asset_the_page_references_exists(index: str) -> None:
    referenced = sorted(set(ASSET.findall(index)))
    assert referenced, "the export references no assets at all, which cannot be right"

    missing = [url for url in referenced if not (STATIC / url.lstrip("/")).is_file()]
    assert not missing, (
        "the shipped UI references files that are not in the package — the export and "
        f"the copy in serve/static have drifted apart: {missing}"
    )


def test_the_bundle_carries_its_own_styles(index: str) -> None:
    """A stylesheet, not just scripts. Losing the CSS is the one break that still
    renders a page, so it is the one worth naming separately."""
    assert any(url.endswith(".css") for url in ASSET.findall(index))
