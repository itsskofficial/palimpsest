# palimpsest

**A self-maintaining knowledge base on top of Notion.** Give it anything — a URL, a
YouTube link, a PDF, a spreadsheet, a photographed whiteboard, or a sentence you typed —
and it works out how that information relates to what you have already written, then
proposes small, reversible, fully-cited edits to the right pages.

> *A palimpsest is a manuscript written over an earlier text, where the earlier writing
> is still visible underneath.* That is the product: your notes get rewritten as you
> learn, and every earlier layer stays readable.

```bash
pip install -e ".[anthropic,serve]"

export NOTION_TOKEN=ntn_...
export ANTHROPIC_API_KEY=sk-ant-...

palimpsest sync                       # mirror your Notion locally
palimpsest sweep duplicates           # what you already wrote twice (no model needed)
palimpsest ingest https://example.com/post
palimpsest apply pch_… --reviewer sk  # the only command that writes
palimpsest undo pch_…                 # exactly reversible
```

---

## The problem

You study something and write it up in Notion. Two weeks later you learn more about the
same topic, and now you have to remember which pages touch it, open each one, decide
whether it needs changing, perform the merge without contradicting what past-you wrote,
and remember where it came from.

The deciding step is the one that breaks. It is genuinely ambiguous, it is boring, and
there is always an escape hatch — *make a new page*. Take it fifty times and your
knowledge base is four versions of the same topic, none authoritative.

## The reframe

Everyone who builds this builds "embed the new text, find similar pages, ask a model to
rewrite them." That tool gets abandoned in a week, because rewriting a page is an
unbounded operation and **you cannot review unbounded operations**.

The atom here is not a document. It is a **claim**. And the question is not "update or
create" — it is *how does this claim relate to what I already wrote?*

| Relation | What happens to your notes | Risk |
|---|---|---|
| **`new`** | nothing covers this → append to the best-fitting page, or create one | low |
| **`corroborates`** | already known → **add a citation. No prose is added.** | low |
| **`refines`** | a sharper version of what you have → edit in place, footnote the old wording | medium |
| **`supersedes`** | same fact, newer source → strike the old, add the new, footnote both | medium |
| **`duplicate`** | you wrote this on *another* page → link or merge. Never re-add. | medium |
| **`extends`** | related, but belongs on a different page | medium |
| **`contradicts`** | the source disagrees with the page → **never auto-applied, at any setting** | high |

Three consequences fall out of that table, and they are the whole design:

**`corroborates` is why the base stops bloating.** Most "new information" about a topic
you already studied is not new — it is the same claim from a different source. Under any
other design that becomes a new paragraph. Here it becomes one small grey marker on a
sentence you already wrote, and *no prose is added at all*.

**`contradicts` is the safety property everything rests on.** A knowledge base that
silently replaces a true claim with a false one is strictly worse than no automation,
because you stop knowing which parts to trust. Contradictions are surfaced with both
sides and both sources, and you decide. There is deliberately no autonomy level that
changes this.

**Every edit is typed, small, and exactly reversible.** `add_citation`, `update_text`,
`insert_footnote`, `strike_block`, `merge_pages` — there is no `rewrite_page` in the
vocabulary, because an operation you cannot render as a diff is one you cannot review.

---

## What it does on day one, before it writes anything

`palimpsest sync` pulls your workspace into a local mirror. Then, with **no model and no
API key at all**:

```bash
palimpsest sweep duplicates    # the same content on two different pages
palimpsest sweep questions     # what your base knows it doesn't know
palimpsest sweep stale         # claims that have outlived their topic's half-life
```

`sweep duplicates` is the one to run first. It is the accumulated damage of the last two
years, listed, ranked by similarity, with a suggested merge for each pair. It reads the
mirror and changes nothing.

With a model key you also get `sweep contradictions` — not new-versus-old, but
**old-versus-old**: places where your notes disagree with *themselves*. Your Notion
almost certainly contains some right now, and you do not know which.

---

## Architecture

```
capture → normalise → extract claims → retrieve → classify relation
        → plan patch → review → apply → record provenance
                          ↑                    │
                          └──── learns from ───┘
```

| Layer | What it does | Notable |
|---|---|---|
| `ingest/` | URL, YouTube, PDF, image, CSV/Excel, raw text → normalised text **+ anchors** | Firecrawl when keyed, stdlib reader when not |
| `extract.py` | text → atomic claims, each with a verbatim quote | claims whose quote isn't in the source are **discarded** |
| `notion/mirror.py` | Notion → local mirror: pages, blocks, backlinks, page *roles* | incremental on `last_edited_time` |
| `retrieve.py` | BM25 + bigrams over the mirror; two separate queries | pure Python, no key needed |
| `relate.py` | the seven-relation classifier | cheap heuristics first, model on the ambiguous middle |
| `plan.py` | relations → typed operations with pre-computed inverses | pure function; fully testable offline |
| `notion/apply.py` | **the only module that writes to Notion** | enforced by an import-linter contract |
| `sweep.py` | duplicates, contradictions, staleness, open questions | three of four need no model |

### Anchors, and why citations don't rot

Every adapter emits *segments* — spans of normalised text with a citable locator. A PDF
anchors to `p. 14`. A YouTube transcript anchors to `1:42:07` **and the footnote links
to that second of the video**. A spreadsheet anchors to `Sheet1!A–D47`.

The extractor maps each claim's character span back to its segment, and the original
bytes are archived on the way in. So a footnote still resolves in two years when the
page 404s. This is the part comparable tools skip, and skipping it is why their
citations are decoration.

### The mirror is not a cache

Notion's API has no diff primitive, no transactions, and no usable version history. So
exact undo, "which source produced this sentence", and time travel are properties of
**our** ledger, not of Notion:

```bash
palimpsest history <page_id>        # every applied change, newest first
palimpsest provenance <block_id>    # source, claim, anchor, who approved it
palimpsest undo <patch_id>          # exact reverse, including partial patches
```

---

## Safety model

Two independent switches, because the failure they prevent is unrecoverable:

```bash
PALIMPSEST_APPLY=0          # default. Nothing is ever written.
PALIMPSEST_AUTONOMY=none    # none | low | medium.  There is no "high".
```

`autonomy` names the highest **risk tier** that may apply without review. `low` covers
`new` and `corroborates`. `medium` adds `refines`, `supersedes`, `duplicate`, `extends`.
Nothing covers `contradicts` — the enum has no value for it, the planner refuses to emit
an operation for it, and the apply route rejects one if it somehow appears. Three locks
on the same door.

Beyond that: the applier **writes each operation's inverse before running it** (Notion
has no transactions, so a patch interrupted at operation six must still be reversible),
it **never hard-deletes** (strike-through and Notion's restorable trash only), and it
**stops at the first failure** with status `partial` rather than ploughing on.

---

## Commands

| Command | What it does |
|---|---|
| `palimpsest sync [--full]` | pull Notion into the mirror |
| `palimpsest ingest <spec>` | run a source through the pipeline. **Writes nothing.** |
| `palimpsest patches` / `patch <id>` | list / show proposed patches |
| `palimpsest apply <id> --reviewer <you>` | the only command that changes your notes |
| `palimpsest undo <id>` | revert exactly |
| `palimpsest sweep <kind>` | `duplicates` · `contradictions` · `stale` · `questions` |
| `palimpsest serve` | the review app on `:8100` |
| `palimpsest history <page_id>` | every applied change to a page |
| `palimpsest provenance <block_id>` | which source produced this text |
| `palimpsest status` | config, mirror size, and anything that looks wrong |
| `palimpsest db check\|migrate\|sql\|reset` | schema management |
| `palimpsest supabase status\|init\|env\|url` | local or cloud Supabase |

## The review app

```bash
palimpsest serve      # http://127.0.0.1:8100
```

Four tabs: **Add** (paste anything, get a diff), **Review** (accept/reject per
operation), **Sweeps**, **Mirror**. Every applied change records who approved it. The
header always shows whether the instance is in propose-only mode.

---

## Configuration

Nothing is required to *look*; two keys are required to be useful. See
[`.env.example`](.env.example) for the full list.

| Variable | Needed for |
|---|---|
| `NOTION_TOKEN` | everything that touches Notion |
| `ANTHROPIC_API_KEY` | extraction, classification, contradiction sweep |
| `FIRECRAWL_API_KEY` | better web extraction (falls back to a stdlib reader) |
| `PALIMPSEST_DATABASE_URL` | defaults to `sqlite:///palimpsest.db` |
| `PALIMPSEST_ARTIFACT_URL` | `file://./archive`, `s3://…`, or `supabase://…` |
| `PALIMPSEST_APPLY` / `PALIMPSEST_AUTONOMY` | permission to write |

## Deploying

`docker compose -f deploy/docker-compose.yml up` for the local stack against Supabase, or
`deploy/terraform` for ECS Fargate + ALB + S3 + Secrets Manager in `ap-south-1`. See
[DEPLOYMENT.md](DEPLOYMENT.md).

## Design decisions

[DECISIONS.md](DECISIONS.md) records what was chosen and what was rejected — the claim
atom, the seven relations, why the inverse is written first, why BM25 rather than
embeddings by default, and why there is no `autonomy=high`.

## Licence

Apache-2.0.
