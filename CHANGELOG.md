# Changelog

Notable changes, newest first. Dates are release dates.

This project follows [semantic versioning](https://semver.org). Before `1.0.0` the minor
version moves for anything that changes behaviour, including defaults — and a default in a
tool that edits your notes is not a small thing, so those are called out individually.

---

## Unreleased

### Fixed

- **The v0.2.1 release was published with no installers attached.** The release job
  downloaded the build artifacts and then checked out the repository, which clears the
  workspace. The files were attached to v0.2.1 afterwards, and a release with nothing to
  attach now fails instead of publishing.

### Changed

- CI runs on Python 3.13 as well, and on Node 22 rather than Node 20, which reached end of
  life.
- `CODE_OF_CONDUCT.md`, adapted from the Contributor Covenant.

## 0.2.1 — 2026-09-15

Found by running the thing rather than reading it: every item below was caught by driving
the desktop app, the queue, the bot or a real vault end to end, and each has a test that
fails without its fix.

### Fixed — things that stopped you

- **Finishing setup returned you to the start of setup.** The wizard's Telegram step
  offers "Skip", but "configured" required a bot token, so skipping it sent you back to
  step one with no way out but to create a bot. Requiring a Notion token did the same to
  every vault install. Configured now means somewhere to write and something to think with.
- **First launch of the desktop app could fail with "duplicate column name".** The web
  process and the queue workers open one SQLite file at once; migrations checked and then
  applied as separate statements, so all of them ran the same `ALTER TABLE`. Migration now
  holds the write lock throughout. `busy_timeout` was also set after the WAL pragma — the
  one statement it could not help.
- **A queue started a second time ran nothing.** Closing and reopening the window gave
  you workers that read a stale stop flag and exited: captures accepted, none processed.
  And a transient store error could end a worker thread for good; nothing in the loop can
  now, except stopping it.
- **Undo left a footnote's reasoning behind.** The rationale was a newline inside the
  callout, which a vault reads back as a separate paragraph, so undoing a footnote removed
  the citation and orphaned the explanation. Every page is now byte-identical after undo.
- **The desktop app could not use a folder of markdown files.** The two settings were not
  writable from the app. They are, under Settings.
- **A credential could be added in Settings but never removed.** Emptying a field now
  clears it.

### Fixed — things that were quietly wrong

- **Every citation printed its locator twice** (`A post · document · document`).
- **Every capture from the browser extension lost its source URL.** A selection arrives as
  text plus the tab's address, and the address was dropped before it reached the citation.
- **The Ask panel showed raw Markdown and cited internal page ids.** Answers render now,
  and cite pages as links — and a link the model invents rather than copies is demoted to
  plain text, since a local model will sometimes fabricate a plausible URL.
- **The Telegram bot never reported captures started by typing.** Text goes to the agent,
  which queues the capture itself, and only captures the bot queued were reported.
- **`/sync` and `/organise` on Telegram only worked with Notion.**
- **Choosing an embedding provider and key in Settings did nothing.** Only a vendor's own
  environment variable was honoured, and Together and Mistral were sent OpenAI's model.
- **A sweep that found nothing left the previous sweep's findings on screen.**
- **Lists written in one clock tick came back in arbitrary order**, so the activity feed
  could reshuffle between polls of an unchanged table.
- **`palimpsest status` failed its own health check on any install with writes on.** The
  write-posture line is still printed; it no longer counts as a problem. `db check` also
  answers in one second rather than twenty when the database is down.
- **A content-filtered request was reported as an empty response.**
- **The per-file upload limit could never be reached**, because the whole-request cap was
  smaller than it.
- **Messages across the product named `NOTION_TOKEN` on a vault install.** Six places now
  name the setting the configured backend actually needs.

### Documentation

- Design, deployment, safety and agent documents now live under [`docs/`](docs/README.md),
  with an index. The roadmap and known limitations are in
  [`docs/roadmap.md`](docs/roadmap.md).
- Two new decision records: schema migrations hold one write lock
  ([ADR 14](docs/decisions/0014-migrations-hold-one-write-lock.md)), and links in an answer
  must point into the mirror
  ([ADR 15](docs/decisions/0015-links-in-an-answer-point-into-the-mirror.md)).
- `CONTRIBUTING.md`, `SECURITY.md` with private vulnerability reporting, and issue and pull
  request templates.
- The licence file carries the full Apache-2.0 text, and the desktop app no longer
  declares itself MIT.

### Security

- **The archive's path check was a string prefix.** `../archive-evil/x` passed it and
  wrote outside the archive root, in the one place the code says the key is untrusted.
- **Every patch and job id became a permanent metric series** on `/metrics`, which stays
  open when an API key is set — unbounded memory, and the ids exposed. Metrics now use
  route templates.

---

## 0.2.0 — 2026-09-12

The release that makes it possible to try this without giving anything write access to
your real notes.

### Added

- **`palimpsest demo`.** The whole product on a sample knowledge base of eight pages. No
  Notion account, no API key, nothing of yours writable. Nothing is stubbed or recorded:
  same mirror, same retrieval, same classifier, same planner, same single write door, same
  undo — the pages just live in a temporary folder. It uses whatever model is configured,
  falls back to a local Ollama, and if it finds neither it still opens and says so.
  ([ADR 13](docs/decisions/0013-the-demo-runs-the-real-pipeline.md))
- **A markdown backend.** A knowledge base can now be a folder of `.md` files — an
  Obsidian vault, a git repo, anything you can read with `cat`. Set
  `PALIMPSEST_BACKEND=markdown` and `PALIMPSEST_VAULT`. Same pipeline and same guarantees,
  because the workspace is now a seventeen-method interface with two implementations.
  Block ids, which a file does not have, live in a sidecar and are re-aligned on every
  read, so **editing a page in Obsidian does not break provenance or undo**.
  ([ADR 11](docs/decisions/0011-the-workspace-is-an-interface-not-notion.md))
- **`palimpsest eval leaderboard --models a,b,c`.** Scores several models against the same
  committed golden set and prints the table as markdown. Contradiction recall is the
  column to read.
- **[`docs/safety.md`](docs/safety.md)** — every limit the system places on itself, each
  one pointing at the test that enforces it. A test now fails if the document cites a test
  that no longer exists, or omits an autonomy level.
- Suggested captures in the demo UI, each stating what it will do — and
  `tests/demo/test_promises.py`, which runs all four against a live model and fails if any
  stops being true.
- `PALIMPSEST_TRACE=0` switches tracing off explicitly, for callers that cannot simply
  unset the keys.

### Changed

- **The confidence threshold now scales with what the edit would do** rather than being
  one flat number: **0.60** to add a block, **0.75** to rewrite a sentence, **0.90** to
  change what an existing sentence means. `PALIMPSEST_MIN_CONFIDENCE` still moves the whole
  scale. A well-calibrated model is *least* certain about the safest operation, so one bar
  rejected purely additive edits most often while passing strike-throughs on the same
  number. **This makes more low-risk edits apply automatically**; high-risk edits got
  stricter. ([ADR 12](docs/decisions/0012-confidence-scales-with-what-the-edit-would-do.md))
- Warnings name the backend actually in use. A vault no longer gets told "NOTION_TOKEN is
  not set — nothing can be mirrored or applied" immediately after mirroring eight pages.
- Tests marked `api` are exempt from the hermetic fixture that strips provider keys, since
  reaching a real provider is their whole purpose.

### Fixed

- **Applying part of a patch discarded the rest of it.** `gate` splits a patch into what
  may apply now and what waits; `apply_patch` persists the patch it is handed, and it was
  handed the slice. The held operations vanished from the record, and approving the
  approval reported success while writing nothing.
- **Undo could stick halfway.** Archiving a parent block takes its children with it, and
  Notion then refuses to touch the child. That refusal describes the state the inverse
  wanted; read as a failure, it left the patch `partial` and told a user whose page had
  been fully reverted that the undo had not worked.
- **A judgement could name a block on one page and a page on another**, anchoring an
  operation to a page it was never meant to touch. Silent, not an error.
- **Page roles were decided by substring match**, so "psychology", "biology", "blog" and
  "log-log axes" all read as evidence of a project log. A page's role decides whether an
  edit appends prose or adds a link, so this changed what got written. Whole words now,
  with ambiguous ones counted only in the title.
- **Soft-wrapped prose became one block per line** in a markdown vault, so every sentence
  fragment was a separate block with its own id and its own retrieval candidacy — and a
  round trip inserted a blank line at every wrap point.
- `[[wikilinks]]` count as links, so a vault's index pages are recognised as hubs.
- Emphasis no longer wraps trailing whitespace (`**bold **`, which no renderer treats as
  bold, and which every contradiction callout would have produced).
- A childless callout no longer leaves a stray `>` line.
- The activity ledger renders as an actual markdown table on a vault, rather than as
  pipe-delimited sentences separated by blank lines.

- **The first two lines of the README ended in a traceback.** The offline core has no
  dependencies by design, so `pip install palimpsest-notion` gets you no web server —
  and `palimpsest demo` copied a vault, mirrored it, printed a cheerful summary, and
  *then* raised `ImportError`. The error named `palimpsest[serve]`, which is a different
  project on PyPI. The demo now checks first and says what to install, and the README
  asks for `palimpsest-notion[serve]`.

- **Every absolute SQLite path was relative on Linux and macOS.** `open_store` stripped
  *all* leading slashes, so `sqlite:////var/lib/notes.db` opened `var/lib/notes.db` under
  the working directory. Windows was unaffected — its paths start `C:` — which is exactly
  why it survived: the bug only appeared on the platforms most people run this on.

### Security

- **`palimpsest demo` no longer inherits credentials from the config file of whoever runs
  it.** The first working version started the user's real Telegram bot — so a message sent
  from a phone would have been answered by the demo, against the sample vault — and
  shipped every demo call to their real Langfuse project. Neither announced itself.

---

## 0.1.0 — 2026-09-09

First release. A self-maintaining knowledge base on top of Notion: claim extraction,
seven-relation classification, hybrid retrieval, inverse-first application, exact undo, a
review app, a Telegram bot, a browser extension and a desktop build.
