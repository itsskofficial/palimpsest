"""The extension, in a real Chrome, against a real server.

Everything else about the extension is checked without a browser: its scraping against DOM
fixtures, its packaging against the zip that ships. Both are worth having and neither
answers the question that actually matters — *does it install and work* — because the two
ways it fails in practice are invisible to both. Chrome can refuse the whole extension for
a manifest it dislikes, and the browser can refuse its requests for a CORS header the
server did not send. Neither is reachable from a unit test.

So this loads the packaged zip into Chrome, opens its popup, and drives the extension's
own `api.js` from inside the extension's origin — a genuine `chrome-extension://` Origin
header, the real fetch wrapper, the real error handling — then asks the server whether a
job actually arrived.

Marked `browser` and not part of the merge gate: it needs Chrome and a running server.

    palimpsest demo --port 8100 &
    pytest tests/e2e -m browser -v

One wrinkle worth recording, because it wasted an hour: **Chrome 137 removed
`--load-extension`**. The sanctioned route for automation is now the CDP command
`Extensions.loadUnpacked`, behind `--enable-unsafe-extension-debugging`, and it is a
*browser*-level command — asked on a page-level session it reports "Method not available",
which reads like the flag is missing rather than the session being the wrong one.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.browser

SERVER = os.environ.get("PALIMPSEST_TEST_SERVER", "http://127.0.0.1:8100")
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _chrome() -> str:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    pytest.skip("no Chrome on this machine")


def _server_up() -> bool:
    try:
        urllib.request.urlopen(f"{SERVER}/v1/status", timeout=3)
        return True
    except (urllib.error.URLError, OSError):
        return False


def _jobs() -> list[dict]:
    with urllib.request.urlopen(f"{SERVER}/v1/jobs?limit=20", timeout=10) as response:
        return json.load(response)["jobs"]


@pytest.fixture(scope="module")
def packed(tmp_path_factory):
    """The zip that ships, unpacked — not the working directory.

    Testing the source directory would pass while the released archive was missing a
    file, which is the single most likely way this breaks for somebody else.
    """
    import zipfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    subprocess.run(["node", str(root / "clients" / "extension" / "pack.mjs")],
                   cwd=root, check=True, capture_output=True)
    archive = sorted((root / "clients" / "extension" / "dist").glob("*.zip"))[-1]

    out = tmp_path_factory.mktemp("extension")
    zipfile.ZipFile(archive).extractall(out)
    return str(out)


@pytest.fixture(scope="module")
def extension(packed, tmp_path_factory):
    """A Chrome with the extension loaded. Yields (playwright context, extension id)."""
    playwright = pytest.importorskip("playwright.sync_api")
    if not _server_up():
        pytest.skip(f"no palimpsest server on {SERVER}; run `palimpsest demo` first")

    chrome = _chrome()
    profile = tmp_path_factory.mktemp("chrome-profile")
    process = subprocess.Popen(
        [chrome, f"--user-data-dir={profile}", "--remote-debugging-port=9223",
         "--enable-unsafe-extension-debugging", "--no-first-run",
         "--no-default-browser-check", "--disable-background-networking",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:9223/json/version", timeout=1)
            break
        except Exception:
            time.sleep(1)
    else:
        process.kill()
        pytest.skip("Chrome did not expose a debugging port")

    with playwright.sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        # Browser-level, not page-level. See the note at the top of this file.
        session = browser.new_browser_cdp_session()
        loaded = session.send("Extensions.loadUnpacked", {"path": packed})
        time.sleep(2)
        try:
            yield context, loaded["id"]
        finally:
            browser.close()
            process.kill()


@pytest.fixture()
def popup(extension):
    context, ext_id = extension
    page = context.new_page()
    page.goto(f"chrome-extension://{ext_id}/popup.html", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    yield page
    page.close()


def test_chrome_accepts_the_extension_we_actually_ship(extension):
    """Chrome refuses a whole extension for one bad manifest entry or missing file, and
    says so only in a UI nobody automating this will see."""
    _, ext_id = extension

    assert ext_id and len(ext_id) == 32


def test_the_popup_renders_rather_than_failing_to_load(popup):
    errors: list[str] = []
    popup.on("pageerror", lambda e: errors.append(str(e)))
    popup.wait_for_timeout(500)

    body = popup.inner_text("body")

    assert "palimpsest" in body.lower()
    assert "Capture this page" in body
    assert not errors, f"the popup raised: {errors[:3]}"


def test_the_popup_reaches_the_server_across_the_extension_origin(popup):
    """The request carries a `chrome-extension://<id>` Origin, and the browser will drop
    the response unless the server allows it. That allowance is a regex on the server,
    the extension id is generated at load time, and no unit test on either side can
    catch the two disagreeing."""
    out = popup.evaluate("""async () => {
      const api = await import('./api.js');
      try { return {ok: true, status: await api.status()}; }
      catch (e) { return {ok: false, error: String(e)}; }
    }""")

    assert out["ok"], out.get("error")
    assert out["status"]["version"]


def test_capturing_from_the_extension_queues_a_job_on_the_server(popup):
    """The whole point of the extension, end to end."""
    before = {j["job_id"] for j in _jobs()}

    out = popup.evaluate("""async () => {
      const api = await import('./api.js');
      try {
        const job = await api.capture({
          spec: 'https://example.com/an-article',
          title: 'An article about clipping',
          url: 'https://example.com/an-article',
        });
        return {ok: true, job};
      } catch (e) {
        return {ok: false, error: String(e)};
      }
    }""")

    assert out["ok"], out.get("error")
    assert out["job"]["status"] == "queued"
    # `origin` is how the server knows which surface asked, and it is what stops a
    # desktop capture pinging somebody's phone.
    assert out["job"]["origin"] == "extension"

    after = {j["job_id"] for j in _jobs()}
    assert out["job"]["job_id"] in after - before, "the server did not record the job"


def test_an_unreachable_server_produces_a_message_about_the_server(popup):
    """"Failed to fetch" is what a browser says and it means nothing to anybody. The
    common cause by far is that palimpsest is not running, so the error has to say so."""
    out = popup.evaluate("""async () => {
      const api = await import('./api.js');
      await api.saveSettings({server: 'http://127.0.0.1:9'});
      try {
        await api.status();
        return {ok: true};
      } catch (e) {
        return {ok: false, error: String(e)};
      } finally {
        await api.saveSettings({server: 'http://127.0.0.1:8100'});
      }
    }""")

    assert out["ok"] is False
    assert "Could not reach palimpsest" in out["error"]
    assert "palimpsest serve" in out["error"] or "desktop app" in out["error"]
