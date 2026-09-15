# Roadmap and known limitations

What is not built, roughly in order of value, and the rough edges in what is.

## Not built yet

**Measurement against your own notes.** The eval suite measures the classifier on a
committed, hand-labelled fixture. What it cannot yet do is measure per-relation precision
and recall against *your* accept/reject history. Until it can, the autonomy ladder is set
by hand. Contradiction recall should be weighted heavily when it arrives: a missed
contradiction corrupts the base, while a false one costs a minute of review.

**An autonomy ladder that learns.** The intended design is that a relation earns automatic
application once its measured precision on your history clears a bar. It depends on the
item above.

**The homework loop.** `palimpsest sweep questions` already produces the queue: open
questions and TODOs in your notes. Wiring it to web search on a schedule would bring answers
back as ordinary sources through the ordinary pipeline, with the same relations, review and
provenance.

**Backfilling links already in your notes.** The mirror stores every URL your pages link
to. Ingesting them is a loop over existing machinery, and a good first run.

**Real merges.** `duplicate` links the two pages today. Merging them properly means moving
content, reconciling wording and updating backlinks, and it needs a side-by-side review
view.

**A distilled classifier.** A small model trained on accept/reject history could handle the
easy majority and escalate only the ambiguous middle. It needs the measurement work first.

**Signed installers.** The Windows and macOS builds are unsigned, so both operating systems
warn on first launch.

## Known limitations

- **Extraction quality is the ceiling.** A claim whose quote cannot be found in the source
  is dropped and counted as `unanchored`. A high count means the model is paraphrasing
  rather than quoting.
- **Page roles are heuristic.** A page that matches no structural pattern is treated as
  `reference` and gets prose appended. The hook to ask the model exists and is not wired.
- **The classifier is single-shot.** No self-consistency or second opinion on
  medium-confidence judgements.
- **A first Notion sync is slow.** Notion allows about three requests a second, so a large
  workspace takes a while. Incremental syncs afterwards are fast.
- **`sweep stale` needs provenance**, so it reports nothing until sources have been
  ingested through palimpsest. It says so rather than returning an empty list.
- **Small local models classify noticeably worse.** The eval table in the README has the
  numbers. `palimpsest status` warns when a model with write access has not been measured.
- **The macOS and Linux installers are built on CI** and have not been exercised by hand on
  those platforms.
