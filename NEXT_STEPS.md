# What I need from you, and what comes next

## What you need to provide

Two keys make this useful. Everything else is optional or has a working fallback.

### 1. `NOTION_TOKEN` — required

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New
   integration** → internal, pick your workspace.
2. Capabilities: **Read content**, **Update content**, **Insert content**. (Read alone
   is enough to run the mirror and every sweep — grant the write capabilities only when
   you want it to be able to apply patches.)
3. Copy the secret (`ntn_...`).
4. **Share pages with it.** Open a top-level page → `⋯` → **Connections** → your
   integration. Sharing a parent shares its children.

> This is the step everyone misses. An integration sees nothing until a page is shared
> with it, and the symptom is a sync that reports zero pages rather than an error.

```bash
export NOTION_TOKEN=ntn_...
```

**Optional but recommended:** `PALIMPSEST_NOTION_ROOTS=<page_id>,<page_id>` restricts
the mirror to specific pages and their descendants. The first id is also where a claim
that fits nowhere gets a new page. Without it, palimpsest mirrors everything shared with
the integration and will not create pages.

### 2. `ANTHROPIC_API_KEY` — required for extraction and classification

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Without it you still get: the mirror, retrieval, `sweep duplicates`, `sweep questions`,
`sweep stale`, the review app, and `undo`. You do not get claim extraction, relation
classification, or `sweep contradictions`.

You have $500 of Anthropic credit. A rough sense of the burn: a 3,000-word article is
one or two extraction calls plus one classification call per claim, with the candidate
set cached — call it a few cents. A full backfill of a large workspace is the expensive
run; do it once.

### 3. Optional

| Key | Buys you | Without it |
|---|---|---|
| `FIRECRAWL_API_KEY` | JavaScript rendering, boilerplate stripping, clean markdown | a stdlib HTML reader that handles static pages fine |
| `OPENAI_API_KEY` | dense retrieval blended with BM25 | BM25 + bigrams, which is what all the sweeps run on anyway |
| Supabase project | deployment, multi-process | SQLite, which is genuinely better for one user |

---

## Try it in this order

```bash
pip install -e ".[anthropic,serve]"

# 1. See the whole thing work with no keys and no network.
python scripts/demo.py

# 2. Mirror your workspace. Read-only.
export NOTION_TOKEN=ntn_...
palimpsest sync

# 3. The day-one value, still with no model key.
palimpsest sweep duplicates
palimpsest sweep questions

# 4. Now the model.
export ANTHROPIC_API_KEY=sk-ant-...
palimpsest sweep contradictions          # old-vs-old: your notes disagreeing with themselves
palimpsest ingest https://some-article-you-read.com

# 5. Read the patch. Nothing has been written yet.
palimpsest patch pch_...

# 6. Dry run, then apply.
palimpsest apply pch_... --dry-run
palimpsest apply pch_... --reviewer "$(whoami)"

# 7. If you hate it.
palimpsest undo pch_...

# 8. The UI, which is where you'll actually live.
palimpsest serve
```

Step 3 is the one to do first. It tells you something about your own notes that you
cannot currently find out, it costs nothing, and it cannot touch anything.

---

## Recording the demo video

The sequence that shows the idea in about ninety seconds:

1. `python scripts/demo.py` — the whole pipeline, offline, in one screen. Point at the
   `corroborates` line producing a citation and no prose.
2. `palimpsest sweep duplicates` against **your real Notion**. This is the moment: it is
   your own accumulated mess, listed, and it needed no API key.
3. `palimpsest serve` → paste a URL → watch the diff appear with relations colour-coded.
4. Accept it, open the Notion page, show the citation and the footnote.
5. `palimpsest undo` → refresh Notion → gone, exactly.

Step 5 is worth more than the rest. Everyone building in this space promises the
first four; nobody demonstrates the fifth.

---

## What is deliberately not built yet

**Measurement, first.** The single most important missing piece is per-relation
precision and recall against a hand-labelled set of *your* notes. Everything about the
autonomy ladder is speculative until that exists: today `PALIMPSEST_AUTONOMY` is set by
hand, and the design — that accept/reject history raises autonomy per relation once
measured precision clears a bar — needs numbers to be honest. Contradiction *recall*
should be weighted heavily, because a missed contradiction corrupts the base while a
false one merely wastes four minutes.

**Then, in rough order of value:**

- **The homework loop.** `sweep questions` already produces the queue: unresolved
  questions and TODOs. Wire it to Firecrawl + web search, run weekly, and answers come
  back as ordinary sources through the ordinary pipeline — same relations, same review,
  same provenance. This is the "always adapting" property in its strong form and most of
  the machinery exists.
- **Backfill every URL already in your Notion.** Years of pasted links nobody processed.
  The mirror already stores them; this is a loop over `links` plus the existing
  ingestion path, and it is the best first-run experience the product can have.
- **Real merges.** `duplicate` currently links the two pages. Actually merging — moving
  content, reconciling wording, updating backlinks — needs the review UI to show both
  pages side by side.
- **Capture surfaces.** An AgentMail inbox (`notes@…`), a Telegram bot, a bookmarklet.
  All thin wrappers over `POST /v1/ingest`; deliberately deferred because you said to
  decide later.
- **A distilled classifier.** Fine-tune a small model on your accept/reject history
  (Fireworks/Unsloth) to handle the easy majority, escalating only the ambiguous middle
  to Claude. This is where the cost curve bends, and it needs the measurement work first.
- **Audio.** Deepgram/Sarvam for lectures and voice memos, including Hinglish. The
  anchor model already handles timestamps; it is one adapter.
- **Obsidian adapter.** The store boundary is already "a knowledge store with a block
  tree", so this is roughly a week and doubles the addressable audience.

---

## Known rough edges

- **Page roles are heuristic.** The structural tells are strong, but a page that does
  not match any pattern falls back to `reference`, which gets prose appended. The hook
  to ask the model exists and is not wired.
- **Notion's rate limit makes a first sync slow.** Thousands of requests at ~2.5/second.
  Incremental sync afterwards is fast; the first one is a coffee.
- **Extraction quality is the ceiling on everything.** Claims whose quote cannot be
  located are dropped and counted — watch `unanchored` in `--out` JSON. A high number
  means the model is paraphrasing rather than quoting and the prompt needs work.
- **`sweep stale` needs provenance to exist**, so it reports nothing until you have
  ingested sources through palimpsest. It says so rather than silently returning empty.
- **The classifier is single-shot.** No self-consistency, no second opinion on
  medium-confidence judgements. Cheap to add; unmeasured, so not added.
