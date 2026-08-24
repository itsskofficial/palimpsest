# Capture surfaces

Three ways to get things into palimpsest without opening a terminal. All are thin: they
decide *what* you are capturing and hand it to the queue. Everything after that —
extraction, classification, planning, provenance, the Notion journal — is the same
pipeline the CLI uses.

```
telegram ──┐
extension ─┤
desktop ───┼──► queue ──► pipeline ──► patch ──► review ──► Notion ──► journal
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

## The desktop app

```bash
cd clients/desktop
npm install
npm start
```

**Press `Ctrl+Shift+Space` anywhere.** Paste a link, a transcript or a thought, drop
files onto the window, press Enter. The window disappears immediately.

On first run it finds your Python, builds a virtualenv under the app's data directory,
and installs palimpsest into it from this checkout — about a minute, once. It installs
with `-e`, so the app runs the code you edit rather than a frozen copy.

Some behaviour worth knowing:

- **It adopts a server you already have.** If `palimpsest serve` is already on the port,
  the app attaches to it instead of starting a second one against the same SQLite file —
  and does not kill it on quit, because it isn't the app's to kill.
- **Files are queued by path, not uploaded.** The server is on the same machine, so
  pushing a 60 MB PDF through an HTTP request to a process that could just open it would
  be silly.
- **Keys live in `palimpsest.env`** in the app's data directory, editable from
  Settings. If you already have a `.env` in this checkout, it is read as a fallback so
  you do not type them twice. Keys are read at server start, so changing them needs a
  quit and reopen.
- **Start with Windows** is a checkbox in the tray menu.

Package it with `npm run dist` (electron-builder, NSIS).

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
