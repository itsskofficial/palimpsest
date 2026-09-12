# Changelog

Notable changes, newest first. Dates are release dates.

This project follows [semantic versioning](https://semver.org). Before `1.0.0` the minor
version moves for anything that changes behaviour, including defaults — and a default in a
tool that edits your notes is not a small thing, so those are called out individually.

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
- **[`docs/SAFETY.md`](docs/SAFETY.md)** — every limit the system places on itself, each
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
