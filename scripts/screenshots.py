"""Capture the README screenshots from a running demo.

    palimpsest demo --dir /tmp/shots --reset --port 8140 &
    python scripts/screenshots.py --port 8140

Committed rather than done by hand because the screenshots are a claim about what the
product looks like, and a claim that can only be re-checked by a person with the right
window size and the right theme is one that quietly goes stale. This takes them at a
fixed viewport, from the real app, against the real sample vault.

It does not run in CI — it needs a browser and a model — but it is one command before a
release, which is the difference between screenshots that get updated and screenshots
that show a version from four months ago.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"

#: The one prompt worth showing, because what it produces is the thing nobody expects:
#: the system finding a real disagreement and then declining to settle it.
CLAIM = ("Recent work finds that per-parameter gradient clipping consistently "
         "outperforms global-norm clipping on transformer language models, "
         "especially at large batch sizes.")


def _shot(page, path) -> None:
    """Screenshot the content, not the empty page below it.

    A viewport-sized shot of a mostly-empty app is a picture of a margin. Clipping to
    what `main` actually occupies means the framing stays right whether the screen holds
    one activity entry or six.
    """
    box = (page.locator("main").bounding_box()
           or page.locator("body").bounding_box())
    pad = 28
    page.screenshot(path=str(path), clip={
        "x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
        "width": box["width"] + pad * 2, "height": box["height"] + pad * 2})
    print("wrote", path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8140)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=860)
    ap.add_argument("--wait", type=int, default=90,
                    help="seconds to wait for the capture to be classified")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium")
        return 2

    import urllib.request

    base = f"http://127.0.0.1:{args.port}"
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height},
                                device_scale_factor=2)
        # `networkidle` never fires: the app polls for jobs and approvals, which is the
        # correct behaviour for a live review surface and useless as a load signal.
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("text=Try one of these", timeout=30_000)
        page.wait_for_timeout(1200)

        # 1. The capture surface, with the suggestions open on the interesting one.
        page.get_by_role("button", name="A fact that argues with a page").click()
        page.wait_for_timeout(600)
        _shot(page, OUT / "capture.png")

        # 2. Feed the claim through the API rather than the button, so the wait below is
        #    for the pipeline rather than for an animation.
        urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/v1/jobs", method="POST",
                headers={"Content-Type": "application/json"},
                data=f'{{"text": {CLAIM!r}, "origin": "ui"}}'.replace("'", '"').encode()),
            timeout=30)

        deadline = time.time() + args.wait
        while time.time() < deadline:
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector("text=Activity", timeout=30_000)
            page.get_by_text("Activity", exact=True).click()
            page.wait_for_timeout(1200)
            if page.get_by_text("for you to decide").count():
                break
            page.wait_for_timeout(4000)
        else:
            print("the capture did not finish in time; is a model configured?")
            browser.close()
            return 1

        page.wait_for_timeout(800)
        _shot(page, OUT / "activity.png")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
