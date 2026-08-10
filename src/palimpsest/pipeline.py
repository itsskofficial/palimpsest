"""The whole loop, in one function.

    ingest(spec) -> normalise -> extract claims -> retrieve -> classify -> plan -> patch

`ingest()` never writes to Notion. It returns a `Patch` and a review queue; applying is
a separate, explicit act. That separation is the reason the tool is safe to run against
your own notes the first time, before you have any evidence it behaves.

Two things happen here that are easy to overlook and matter a lot:

**Re-ingesting a source is free.** Sources are keyed by content hash, so dropping the
same PDF twice costs one hash lookup rather than a second extraction bill. This is what
makes the "backfill every URL already in your Notion" first run affordable.

**The archive is written before anything else.** The original bytes go to the artifact
store on the way in, so a citation still resolves in two years when the page has
404'd — which is the difference between a knowledge base you trust and one whose
footnotes are decoration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from palimpsest.extract import extract
from palimpsest.ingest import resolve
from palimpsest.llm import Model
from palimpsest.plan import PlanResult, plan
from palimpsest.relate import classify
from palimpsest.retrieve import Index
from palimpsest.types import Claim, Patch, Source

__all__ = ["IngestResult", "ingest"]

log = logging.getLogger("palimpsest.pipeline")


@dataclass
class IngestResult:
    source: Source
    patch: Patch
    review: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    reused: bool = False
    stages: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "source": {"source_id": self.source.source_id, "kind": self.source.kind,
                       "title": self.source.title, "url": self.source.url,
                       "chars": len(self.source.text)},
            "reused": self.reused,
            "claims": len(self.claims),
            "patch": self.patch.as_dict(),
            "review": self.review,
            "skipped": self.skipped,
            "stages": self.stages,
            "seconds": round(self.seconds, 1),
            "usage": self.usage,
        }

    def summary(self) -> str:  # pragma: no cover - display only
        counts = self.patch.by_relation()
        parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing"
        return (
            f"{self.source.title}\n"
            f"  {len(self.claims)} claim(s) → {parts}\n"
            f"  {len(self.patch)} operation(s) proposed, "
            f"{len(self.review)} needing review\n"
            f"  {self.seconds:.1f}s"
            + (f", ~${self.usage.get('estimated_cost_usd', 0):.3f}" if self.usage else "")
        )


def ingest(spec: str, store, model: Model | None = None, *, settings=None,
           kind: str | None = None, index: Index | None = None,
           archive=None, reuse: bool = True, max_windows: int | None = None,
           ) -> IngestResult:
    """Run one source all the way to a proposed patch. Writes nothing to Notion."""
    started = time.perf_counter()
    stages: dict[str, Any] = {}

    # -- 1. normalise ------------------------------------------------------
    t0 = time.perf_counter()
    source = resolve(
        spec, kind=kind, model=model,
        firecrawl_key=getattr(settings, "firecrawl_api_key", None),
    )
    stages["ingest"] = {"seconds": round(time.perf_counter() - t0, 2),
                        "chars": len(source.text), "kind": source.kind,
                        "segments": len(source.meta.get("segments") or [])}

    # -- 2. dedupe by content hash ----------------------------------------
    existing = store.find_source_by_hash(source.content_hash) if reuse else None
    if existing is not None:
        claims = store.get_claims(existing.source_id)
        if claims:
            log.info("source already ingested (%s); reusing %d claim(s)",
                     existing.source_id, len(claims))
            source = existing
            reused = True
        else:
            source = existing
            reused = False
            claims = []
    else:
        reused = False
        claims = []

    # -- 3. archive the original ------------------------------------------
    if archive is not None and not reused:
        try:
            ref = archive.put(f"sources/{source.source_id}.json", source.as_dict())
            source.archive_key = ref.key
        except Exception as e:
            log.warning("could not archive source: %s", e)

    store.put_source(source)

    # -- 4. extract claims -------------------------------------------------
    if not claims:
        if model is None:
            raise RuntimeError(
                "extraction needs a model. Set ANTHROPIC_API_KEY.\n"
                "The mirror, retrieval, the sweeps and undo all work without one."
            )
        t0 = time.perf_counter()
        extraction = extract(source, model, max_windows=max_windows,
                             effort=getattr(settings, "extract_effort", "medium"))
        claims = extraction.claims
        store.put_claims(claims)
        stages["extract"] = {**extraction.as_dict(),
                             "seconds": round(time.perf_counter() - t0, 2)}
    else:
        stages["extract"] = {"claims": len(claims), "reused": True}

    if not claims:
        return IngestResult(
            source=source, patch=Patch(patch_id="", source_id=source.source_id),
            claims=[], reused=reused, stages=stages,
            seconds=time.perf_counter() - started,
            usage=model.usage.as_dict(model.model) if model else {},
        )

    # -- 5. retrieve + classify -------------------------------------------
    if index is None:
        t0 = time.perf_counter()
        index = Index(store)
        stages["index"] = {"seconds": round(time.perf_counter() - t0, 2),
                           "blocks": len(index), "pages": index.n_pages}

    if model is None:  # pragma: no cover - guarded above
        raise RuntimeError("classification needs a model")

    t0 = time.perf_counter()
    classified = classify(claims, source, index, model,
                          effort=getattr(settings, "classify_effort", "high"),
                          top_pages=getattr(settings, "max_candidates", 8))
    store.put_judgements(classified.judgements)
    stages["classify"] = {**classified.as_dict(),
                          "seconds": round(time.perf_counter() - t0, 2)}

    # -- 6. plan -----------------------------------------------------------
    planned: PlanResult = plan(
        classified.judgements, {c.claim_id: c for c in claims}, source, store,
        min_confidence=getattr(settings, "min_confidence", 0.75),
        footnotes=getattr(settings, "footnotes", True),
        default_parent=(getattr(settings, "notion_root_pages", ()) or (None,))[0],
    )
    store.put_patch(planned.patch)
    stages["plan"] = {"operations": len(planned.patch), "review": len(planned.review),
                      "skipped": len(planned.skipped)}

    return IngestResult(
        source=source, patch=planned.patch, review=planned.review,
        skipped=planned.skipped, claims=claims, reused=reused, stages=stages,
        seconds=time.perf_counter() - started,
        usage=model.usage.as_dict(model.model),
    )
