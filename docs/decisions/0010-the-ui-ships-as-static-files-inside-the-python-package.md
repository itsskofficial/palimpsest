# 10. The UI ships as static files inside the Python package

**Status:** Accepted
**Date:** 2026-09-09

## Context

The window is the product. `clients/ui/` is a Next.js app served three ways from one
build: `palimpsest serve` on `127.0.0.1:8100`, the Electron shell that wraps that server,
and the onboarding wizard on first run.

That leaves the question of how a Python package delivers a TypeScript app. The
alternative to shipping the built output is building at install time — a `pip install`
that shells out to `npm ci && next build`. That makes a Python package depend on a
JavaScript toolchain to show its own front page, and it fails on any machine without Node.

## Decision

`clients/ui/scripts/ship.mjs` removes `src/palimpsest/serve/static/`, recreates it, and
copies the static export in. That directory is **committed**, so `pip install
palimpsest-notion` gets a built UI and nobody needs Node at runtime.

`serve/app.py` serves it in two pieces. `GET /` reads `static/index.html`, falling back to
a plain page pointing at `/docs` if the UI was never shipped. The hashed assets are
mounted separately at the exact prefix `/_next`, last, so the mount can never shadow an
API route — a narrow mount is safer than a catch-all at `/`.

## Consequences

### What this buys

`pip install` produces a working app on a machine with no Node and no build step. The
Electron installer bundles no toolchain either: on first run it builds a virtualenv and
installs `palimpsest-notion` into it, which keeps the download small and lets the engine
be upgraded without reinstalling the shell. There is no second implementation to drift.

### What this costs

A build artefact lives in version control. Every UI change produces a diff of hashed chunk
files no reviewer can read, and conflicts there are resolved by rebuilding, not reading.

It also invites one silent failure: someone edits the UI, runs `npm run build`, forgets
`ship.mjs`, and the package goes out with an `index.html` referencing chunk names that no
longer exist. The server returns 200 for the page and 404 for its JavaScript, and the app
is a blank rectangle with nothing in the logs to explain it.

`tests/unit/test_ui_bundle.py` guards exactly that: it parses the shipped HTML for every
`src`/`href` under `/_next/` and asserts each file is in the package, plus that a
stylesheet is among them — losing the CSS is the one break that still renders a page. The
`ui` CI job runs `ship.mjs` against a fresh build and then that test. Deliberately not a
byte-for-byte diff, because hashed filenames make that brittle across Node versions.

## Revisit if

The UI grows large enough that committed chunks make the repository unpleasant to clone or
the diffs unreviewable. The alternative is publishing the built UI as a separate artefact
the wheel fetches at build time, which keeps `pip install` Node-free while taking the
bundle out of git — at the cost of a release step that can fail independently of the code.
