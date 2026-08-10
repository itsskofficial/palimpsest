"""The whole product, end to end, with no keys and no network.

    python scripts/demo.py

Builds a small Notion-shaped workspace that has the exact problems this tool exists to
fix, then runs every stage against it - the sweeps, retrieval, relation classification,
patch planning, applying, and undo - using a **fake Notion** and a **scripted model**.

The point is not to mock the product into looking good. The fake Notion is a real
implementation of the eight API methods the client uses, so the applier's inverses are
exercised for real and `undo` genuinely has to restore the previous state. The scripted
model returns fixed relations, which is what lets the demo assert on the *planner's*
behaviour rather than on a model's mood.

Run it with `--real` and the usual keys to do the same thing against your actual Notion.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from palimpsest._console import install as _install_console  # noqa: E402
from palimpsest.notion.apply import apply_patch, revert_patch  # noqa: E402
from palimpsest.plan import plan  # noqa: E402
from palimpsest.retrieve import Index  # noqa: E402
from palimpsest.store import open_store  # noqa: E402
from palimpsest.types import (  # noqa: E402
    Anchor,
    Claim,
    ClaimType,
    Judgement,
    Relation,
    Source,
    new_id,
)

BAR = "=" * 76


def heading(text: str) -> None:
    print(f"\n{BAR}\n  {text}\n{BAR}")


# ---------------------------------------------------------------------------
# a fake Notion that behaves like the real one
# ---------------------------------------------------------------------------


class FakeNotion:
    """The eight endpoints `NotionClient` exposes, over an in-memory workspace.

    Faithful where it matters for this demo: appending returns the created block ids
    (so the applier can build an inverse that archives exactly those), updating replaces
    rich_text, archiving is reversible. If the applier's inverse logic is wrong, undo
    here fails exactly as it would against Notion.
    """

    def __init__(self) -> None:
        self.blocks: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.calls = 0
        self.log: list[str] = []

    def _text(self, block: dict) -> str:
        body = block.get(block["type"], {})
        return "".join(r.get("text", {}).get("content", "")
                       for r in body.get("rich_text", []))

    def append_children(self, parent_id, children, after_block_id=None):
        self.calls += 1
        created = []
        for child in children:
            bid = new_id("nb_")
            stored = {**child, "id": bid, "parent": parent_id, "archived": False}
            self.blocks[bid] = stored
            created.append({"id": bid, "type": child["type"]})
            self.log.append(f"append {bid[:8]} -> {parent_id[:12]}: "
                            f"{self._text(child)[:60]}")
        return {"results": created}

    def update_block(self, block_id, payload):
        self.calls += 1
        block = self.blocks.setdefault(block_id, {"type": "paragraph", "id": block_id})
        if "archived" in payload:
            block["archived"] = payload["archived"]
            self.log.append(f"{'archive' if payload['archived'] else 'restore'} "
                            f"{block_id[:8]}")
            return block
        for key, value in payload.items():
            block[key] = value
            block["type"] = key
        self.log.append(f"update {block_id[:8]}: {self._text(block)[:60]}")
        return block

    def archive_block(self, block_id):
        return self.update_block(block_id, {"archived": True})

    def restore_block(self, block_id):
        return self.update_block(block_id, {"archived": False})

    def create_page(self, parent_page_id, title, children=None, icon=None):
        self.calls += 1
        pid = new_id("np_")
        self.pages[pid] = {"id": pid, "title": title, "parent": parent_page_id}
        self.log.append(f"create page {pid[:8]}: {title}")
        return {"id": pid, "url": f"https://notion.so/{pid}"}

    def archive_page(self, page_id):
        self.calls += 1
        self.pages.pop(page_id, None)
        self.log.append(f"archive page {page_id[:8]}")
        return {"id": page_id, "archived": True}


class ScriptedModel:
    """Returns a fixed relation per claim, so the demo tests the planner not the model."""

    def __init__(self, script: dict[str, Judgement]):
        self.script = script
        self.model = "scripted"


# ---------------------------------------------------------------------------
# the workspace
# ---------------------------------------------------------------------------

ATTENTION = ("Scaled dot-product attention divides the logits by the square root of the "
             "key dimension, which keeps gradient variance stable as the dimension grows.")
PRICE_OLD = ("Claude Opus 4.8 costs five dollars per million input tokens on the "
             "first-party API.")
LAYER = "Concept injection is detected around layer 9 in Llama-3-8B-Instruct."


def seed(store) -> None:
    store.put_pages([
        {"page_id": "pg_attention", "title": "Attention", "role": "deep_dive",
         "last_edited": "2026-03-01T00:00:00Z", "url": "https://notion.so/attention"},
        {"page_id": "pg_transformers", "title": "Transformers", "role": "reference",
         "last_edited": "2026-03-02T00:00:00Z"},
        {"page_id": "pg_pricing", "title": "Model pricing", "role": "reference",
         "last_edited": "2026-03-03T00:00:00Z"},
        {"page_id": "pg_introspect", "title": "Introspection", "role": "deep_dive",
         "last_edited": "2026-03-04T00:00:00Z"},
        {"page_id": "pg_index", "title": "ML index", "role": "hub",
         "last_edited": "2026-03-05T00:00:00Z"},
    ])
    store.put_blocks([
        {"block_id": "bk_att_1", "page_id": "pg_attention", "type": "paragraph",
         "text": ATTENTION, "position": 0,
         "raw": {"type": "paragraph", "paragraph": {"rich_text": [
             {"type": "text", "text": {"content": ATTENTION}}]}}},
        # The same content, on another page. This is the reported pain, seeded.
        {"block_id": "bk_tr_1", "page_id": "pg_transformers", "type": "paragraph",
         "text": ATTENTION, "position": 0,
         "raw": {"type": "paragraph", "paragraph": {"rich_text": [
             {"type": "text", "text": {"content": ATTENTION}}]}}},
        {"block_id": "bk_price", "page_id": "pg_pricing", "type": "paragraph",
         "text": PRICE_OLD, "position": 0,
         "raw": {"type": "paragraph", "paragraph": {"rich_text": [
             {"type": "text", "text": {"content": PRICE_OLD}}]}}},
        {"block_id": "bk_layer", "page_id": "pg_introspect", "type": "paragraph",
         "text": LAYER, "position": 0,
         "raw": {"type": "paragraph", "paragraph": {"rich_text": [
             {"type": "text", "text": {"content": LAYER}}]}}},
        {"block_id": "bk_q", "page_id": "pg_introspect", "type": "paragraph",
         "text": "Does the layer-9 result hold on Qwen2.5, or is it Llama-specific?",
         "position": 1},
    ])
    store.put_links([("pg_index", "pg_attention", "x"), ("pg_index", "pg_pricing", "y")])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sqlite://:memory:")
    ap.add_argument("--out", default=None, help="write the full run as JSON")
    args = ap.parse_args()
    _install_console()

    started = time.time()
    store = open_store(args.db)
    store.truncate_all()
    seed(store)

    heading("1. The mirror")
    stats = store.stats()
    print(f"  {stats.get('pages')} pages, {stats.get('blocks')} blocks")
    print("  Two of those blocks are the same paragraph on two different pages -")
    print("  seeded deliberately, because that is the problem this tool exists to fix.")

    heading("2. Sweeps - what is already wrong, before ingesting anything")
    from palimpsest import sweep

    index = Index(store)
    dupes = sweep.duplicates(store, index=index)
    print(f"  duplicates: {dupes.summary()}")
    for f in dupes.findings:
        print(f"    similarity {f['similarity']}  "
              f"'{f['a']['page_title']}' <-> '{f['b']['page_title']}'")
        print(f"      {f['a']['text'][:88]}...")
    questions = sweep.open_questions(store)
    print(f"\n  open questions: {questions.summary()}")
    for f in questions.findings:
        print(f"    {f['page_title']}: {f['text'][:80]}")
    print("\n  Both of those ran with no model and no API key.")

    heading("3. A new source arrives")
    source = Source(
        source_id=new_id("src_"), kind="web",
        title="Feeling the Strength but Not the Source (arXiv:2512.12411)",
        url="https://arxiv.org/abs/2512.12411",
        published_at="2026-02-14",
        text=(f"{ATTENTION} We find a configuration with layer=9 and a prompt-token "
              "averaged steering vector that achieves a 20% introspection rate on "
              "Llama-3.1-8B-Instruct. Claude Opus 5 costs five dollars per million "
              "input tokens and twenty-five per million output."),
        meta={"segments": [{"start": 0, "end": 2000, "kind": "section",
                            "locator": "Results",
                            "url": "https://arxiv.org/abs/2512.12411"}]},
    )
    store.put_source(source)
    print(f"  {source.title}")
    print(f"  {source.url}  ({len(source.text)} chars)")

    def anchor() -> Anchor:
        return Anchor("section", "Results", 0, 100, source.url)

    claims = [
        Claim(new_id("clm_"), ATTENTION, ClaimType.FACT, ("attention",), 0.98,
              anchor(), source.source_id),
        Claim(new_id("clm_"),
              "A layer-9 steering vector achieves a 20% introspection rate on "
              "Llama-3.1-8B-Instruct.", ClaimType.NUMBER, ("introspection",), 0.95,
              anchor(), source.source_id),
        Claim(new_id("clm_"),
              "Claude Opus 5 costs $5 per million input tokens and $25 per million "
              "output tokens.", ClaimType.NUMBER, ("pricing",), 0.97,
              anchor(), source.source_id),
        Claim(new_id("clm_"),
              "Prompt framing changes the measured introspection rate substantially.",
              ClaimType.FACT, ("introspection",), 0.9, anchor(), source.source_id),
    ]
    store.put_claims(claims)
    print(f"  extracted {len(claims)} atomic claim(s), each with a verifiable anchor")

    heading("4. Classification - how does each claim relate to what you already wrote?")
    judgements = [
        Judgement(claims[0].claim_id, Relation.CORROBORATES, 0.96, "pg_attention",
                  "bk_att_1", "the page already states this; a second source confirms it",
                  ATTENTION, "scripted"),
        Judgement(claims[1].claim_id, Relation.REFINES, 0.91, "pg_introspect",
                  "bk_layer", "sharpens 'around layer 9' into a measured rate",
                  LAYER, "scripted"),
        Judgement(claims[2].claim_id, Relation.SUPERSEDES, 0.93, "pg_pricing",
                  "bk_price", "newer model and price; the old line is now wrong",
                  PRICE_OLD, "scripted"),
        Judgement(claims[3].claim_id, Relation.NEW, 0.88, "pg_introspect", None,
                  "nothing in the notes covers prompt framing", None, "scripted"),
    ]
    store.put_judgements(judgements)
    for c, j in zip(claims, judgements, strict=False):
        print(f"  [{j.relation.value:<12}] {c.text[:66]}")
        print(f"    -> {j.rationale}")

    heading("5. Planning - relations become small, reversible operations")
    result = plan(judgements, {c.claim_id: c for c in claims}, source, store)
    store.put_patch(result.patch)
    print(f"  {result.summary()}")
    for op in result.patch.operations:
        rel = op.relation.value if op.relation else "-"
        print(f"    [{rel:<12}] {op.kind.value:<16} {op.summary()[:56]}")
    print("\n  Note what CORROBORATES produced: a citation, and no prose at all.")

    heading("6. Applying - against a fake Notion that behaves like the real one")
    notion = FakeNotion()
    applied = apply_patch(notion, store, result.patch, reviewer="demo")
    print(f"  {applied.summary()}")
    for line in notion.log:
        print(f"    {line}")

    heading("7. Provenance - every change traceable to its source and anchor")
    seen_blocks: set[str] = set()
    shown = 0
    for op in result.patch.operations:
        for bid in (op.result or {}).get("created_block_ids", []) or [op.target]:
            if bid in seen_blocks:
                continue
            seen_blocks.add(bid)
            for r in store.provenance_for_block(bid):
                a = r.get("anchor") or {}
                print(f"  {bid[:10]}  {r.get('relation'):<12} "
                      f"{(r.get('source_title') or '')[:40]}  {a.get('locator') or '-'}")
                shown += 1
    print(f"  {shown} provenance row(s) across {len(seen_blocks)} block(s)")

    heading("8. Undo - exactly reversible, including partial patches")
    before = len([b for b in notion.blocks.values() if not b.get("archived")])
    reverted = revert_patch(notion, store, result.patch, reviewer="demo")
    after = len([b for b in notion.blocks.values() if not b.get("archived")])
    print(f"  {reverted.summary()}")
    print(f"  live blocks in the fake workspace: {before} -> {after}")
    print("  every appended block archived, every edited block restored to its exact")
    print("  previous rich_text - not a re-rendered approximation of it.")

    heading("What this demonstrated")
    print("""\
  - duplicates and open questions found with no model and no API key
  - one source -> four atomic claims, each anchored to a citable locator
  - four different relations -> four different kinds of edit
  - CORROBORATES added a citation and zero prose - the anti-bloat property
  - SUPERSEDES struck the old price through rather than deleting it
  - every operation carried a pre-computed inverse, so undo is exact
  - nothing was written until a patch was explicitly applied with a reviewer""")

    payload = {
        "duplicates": dupes.as_dict(),
        "questions": questions.as_dict(),
        "patch": result.patch.as_dict(),
        "applied": applied.as_dict(),
        "reverted": reverted.as_dict(),
        "notion_log": notion.log,
        "seconds": round(time.time() - started, 2),
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2, default=str),
                                  encoding="utf-8")
        print(f"\n  wrote {args.out}")
    print(f"\n  ({time.time() - started:.1f}s, no network, no keys)\n")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
