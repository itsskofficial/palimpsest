# Clients

The interface, and the ways into it that do not involve a terminal.

`ui/` is the product's actual interface: a Next.js app exported as static files, shipped
inside the Python package, and served by `palimpsest serve`. It is not a client of the
API in the way the others are — it *is* the app, and the desktop shell and the onboarding
wizard are both just it, wearing different frames.

The rest are capture surfaces. All are thin: they decide *what* you are capturing and
hand it to the queue. Everything after that — extraction, classification, planning,
provenance, the Notion journal — is the same pipeline the CLI uses.

```
UI (drop) ─┐
telegram ──┤
extension ─┼──► queue ──► pipeline ──► patch ──► review ──► Notion ──► journal
shortcut ──┤
CLI ───────┘
```

The **Telegram bot** lives in the Python package rather than here, because it is not a
client of the HTTP API — it shares the process and the queue. `palimpsest serve` starts
it whenever `TELEGRAM_BOT_TOKEN` is set, so the desktop app gets it for free.

## Why they post to a queue

`POST /v1/ingest` runs the whole pipeline before it responds, which takes tens of
seconds to a few minutes. A browser popup is destroyed the moment you click away, and a
drop of nine PDFs cannot hold a socket open while they are all read.

So the clients use `POST /v1/jobs`, which writes a durable row and returns an id in
milliseconds. The work outlives the window that asked for it, and a job interrupted by a
crash is re-queued on the next start rather than silently lost — which is the one
failure a capture tool must not have.

---

## The UI

```bash
cd clients/ui
npm install
npm run dev          # against a `palimpsest serve` on :8100
npm run build && node scripts/ship.mjs
```

`ship.mjs` copies the export into `src/palimpsest/serve/static/`, which is **committed**.
That is deliberate: a `pip install` must not need Node, and the alternative — building
the UI at install time — would make a Python package depend on a JavaScript toolchain to
show its own front page. `tests/unit/test_ui_bundle.py` guards the failure that trade
invites, which is shipping an `index.html` whose chunks were never copied.

The look is a manuscript: vellum ground, sepia ink, **rubric red for the one thing that
needs you** and aged copper for what has safely landed. Those last two are semantic, not
decorative — a glance at the feed tells you whether anything is waiting without reading a
word. A proposal renders the new sentence over the struck-through line it replaces, which
is the product's name made literal.

Every colour is a token defined on bare `:root`, redefined under both
`prefers-color-scheme: dark` and `[data-theme="dark"]`, so the app follows the system and
still obeys an explicit choice.

## The desktop app

```bash
cd clients/desktop
npm install
npm start            # or `npm run dist` to build the installer
```

The main window loads the UI above from `127.0.0.1`. Nothing is reimplemented here — the
shell exists for the two things a browser tab cannot do: live in the tray, and answer
**`Ctrl+Shift+Space` anywhere**. That shortcut opens a small box over whatever you are
doing; paste a link or drop files, press Enter, and it is gone before the ingest starts.

On first run it finds your Python, builds a virtualenv under the app's data directory,
and installs palimpsest into it — about a minute, once. From a checkout it installs `-e`
so the app runs the code you edit; from the installer, where there is no checkout, it
installs `palimpsest-notion` from PyPI.

Some behaviour worth knowing:

- **It adopts a server you already have.** If `palimpsest serve` is already on the port,
  the app attaches to it instead of starting a second one against the same SQLite file —
  and does not kill it on quit, because it isn't the app's to kill.
- **Files are queued by path, not uploaded.** The server is on the same machine, so
  pushing a 60 MB PDF through an HTTP request to a process that could just open it would
  be silly.
- **It does not store your keys.** Configuration lives in the config file the Python
  service owns and is edited from the app's own Settings tab. An earlier version kept a
  private copy and passed it as environment variables, which outranked the config file
  and made the Settings screen appear to save and then change nothing; that file is
  migrated across on first start and then ignored.
- **Start with Windows** is a checkbox in the tray menu.

The installer is unsigned and bundles no Python, so it is small. On Windows without
developer mode, `npm run dist` needs `-c.win.signAndEditExecutable=false` — the signing
toolchain unpacks macOS symlinks Windows will not create unprivileged. `.github/workflows/release.yml`
builds all three platforms on a tag, where that privilege exists.

---

## The browser extension

```
chrome://extensions → Developer mode → Load unpacked → clients/extension
```

Then `Ctrl+Shift+K`, or the toolbar button, or right-click → *Capture*.

### What "capture" means per site

The rule is to send the **least processed thing that still carries anchors**:

| Where you are | What gets sent | Why |
|---|---|---|
| YouTube | the bare link | The server fetches the caption track itself and anchors each claim to a second of video. Scraping the page would produce a worse transcript than the server gets for free. |
| Udemy, Coursera, edX, O'Reilly, … | the transcript read off the page | The server cannot log in. Sent as `kind: "transcript"` with the lecture URL, so claims cite `1:42:07` rather than a character offset. |
| A text selection | the selection | You already decided what mattered. |
| Anything else | the URL | Firecrawl when configured, a stdlib reader when not. |

Sending a gated lecture's *URL* is the failure this table exists to prevent: it looks
like it worked and produces a source consisting of the login page.

### Finding a transcript on a page

The extension does almost no parsing. It locates the transcript region and returns its
raw `innerText`; the tested Python adapter turns that into timestamped cues. The fragile
half therefore lives where it can be verified, and the browser half stays trivial.

Finding the region is done by **timestamp density**, not by class name — locate the
elements whose own text begins with a timestamp, take their common ancestor, climb only
if that ancestor holds bare numbers with no speech beside them. Known selectors are
tried first as a fast path, but the heuristic is what survives a redesign.

If nothing is found it says so and tells you to open the transcript panel, rather than
capturing an empty source.

```bash
cd clients && npm install && npm test
```

Seven tests over DOM fixtures: a Udemy-shaped panel with stacked timestamps, a
Coursera-shaped one with no usable selectors at all, and a split-column layout where the
timestamps and the words are in different containers.

**These fixtures are modelled on the real sites, not captured from them.** If a capture
comes back empty on a site you use, the density heuristic is the thing to check first —
open the console and run the body of `scrapeTranscript` from `scrape.js`.

### Settings

The extension talks to `http://127.0.0.1:8100` by default and needs no API key for a
local server. Set one only if you set `PALIMPSEST_API_KEY`.

---

## Both at once

They are complementary and share the queue, so it does not matter which one you use:

- the **extension** knows what tab you are on, which is what makes gated transcripts and
  selections possible at all;
- the **desktop app** takes files and works when the browser is not the thing in front
  of you.
