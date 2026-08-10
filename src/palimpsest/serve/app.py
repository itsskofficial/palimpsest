"""The review app: drop something in, read the diff, accept or reject.

    palimpsest serve
    # -> http://127.0.0.1:8100

This exists because approving patches from a REPL is a chore nobody repeats. The
product's whole autonomy story — that you eventually stop reviewing the low-risk
relations because you have watched them be right — depends on the review being fast
enough that you actually do it. Four minutes on a phone, not a session at a desk.

Two endpoints carry the weight:

- `POST /v1/ingest` runs the pipeline and returns a patch. **It never writes to
  Notion.** Ingesting and applying are separate acts, on purpose.
- `POST /v1/patches/{id}/apply` is the only route that mutates your workspace, and it
  refuses to apply an operation derived from a contradiction no matter what is asked.

Local by design: binds `127.0.0.1`, no authentication unless you set a key. It is a
control surface for your own notes, and the config layer refuses to bind publicly
without one.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from palimpsest._version import __version__
from palimpsest.config import Settings

log = logging.getLogger("palimpsest.serve")

__all__ = ["AppState", "create_app"]


def _require_fastapi():
    try:
        from fastapi import FastAPI  # noqa: F401
    except ImportError as e:  # pragma: no cover - optional extra
        raise ImportError("pip install 'palimpsest[serve]'") from e


class AppState:
    """Everything the request handlers share.

    The retrieval index is held here and rebuilt on demand rather than per request:
    building it is milliseconds, but doing it inside every classification would make a
    twenty-claim source twenty index builds.
    """

    def __init__(self, settings: Settings | None = None, **kwargs):
        from palimpsest.artifacts import open_artifacts
        from palimpsest.store import open_store

        self.settings = settings or Settings.load(**kwargs)
        self.store = open_store(self.settings.database_url)
        self.artifacts = open_artifacts(self.settings.artifact_url)
        self.started_at = time.time()
        self._index = None
        self._index_blocks = -1
        self._model = None
        self._notion = None

    # -- lazily-built shared objects -------------------------------------------

    @property
    def index(self):
        from palimpsest.retrieve import Index

        count = self.store.stats().get("blocks", 0)
        if self._index is None or self._index_blocks != count:
            self._index = Index(self.store)
            self._index_blocks = count
        return self._index

    def refresh_index(self):
        self._index = None
        return self.index

    @property
    def model(self):
        from palimpsest.llm import Model

        if self._model is None:
            self._model = Model(self.settings.model,
                                api_key=self.settings.anthropic_api_key,
                                max_tokens=self.settings.max_tokens)
        return self._model

    @property
    def notion(self):
        from palimpsest.notion.client import NotionClient

        if self._notion is None:
            self._notion = NotionClient(self.settings.notion_token or "",
                                        version=self.settings.notion_version)
        return self._notion

    # -- health ----------------------------------------------------------------

    def health(self) -> dict:
        """Liveness. Deliberately touches nothing external — a health check that
        fails when Notion blips gets a healthy process killed."""
        return {"status": "ok", "version": __version__,
                "uptime_s": round(time.time() - self.started_at, 1),
                "environment": self.settings.environment}

    def readiness(self) -> tuple[bool, dict]:
        checks: dict = {}
        ok = True
        try:
            checks["database"] = {"ok": True,
                                  "ping_ms": round(self.store.ping() * 1000, 2)}
        except Exception as e:
            ok = False
            checks["database"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        checks["notion"] = {"configured": self.settings.has_notion}
        checks["model"] = {"configured": self.settings.has_model}
        checks["apply"] = {"enabled": self.settings.apply,
                           "autonomy": self.settings.autonomy}
        return ok, {"status": "ready" if ok else "not ready", "checks": checks}

    def close(self) -> None:
        self.store.close()


def create_app(state: AppState | None = None, **kwargs) -> Any:
    """Build the FastAPI app. `state` is injectable so tests can pass an in-memory DB."""
    _require_fastapi()
    import contextlib

    from fastapi import Body, FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

    from palimpsest.serve.middleware import Metrics, install

    st = state or AppState(**kwargs)
    metrics = Metrics()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        log.info("palimpsest %s starting in %s", __version__, st.settings.environment)
        yield
        with contextlib.suppress(Exception):
            st.close()

    app = FastAPI(title="palimpsest", version=__version__,
                  description="A self-maintaining knowledge base on top of Notion.",
                  lifespan=lifespan)
    app.state.pal = st
    app.state.metrics = metrics
    install(app, st.settings, metrics)
    static = Path(__file__).parent / "static"

    # -- meta ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return st.health()

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        ok, payload = st.readiness()
        return JSONResponse(status_code=200 if ok else 503, content=payload)

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        if not st.settings.metrics:
            raise HTTPException(404, "metrics are disabled (PALIMPSEST_METRICS=0)")
        stats = st.store.stats()
        extra = {f"{k}_total": v for k, v in stats.items() if isinstance(v, int)}
        return PlainTextResponse(metrics.render(extra),
                                 media_type="text/plain; version=0.0.4")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return (static / "index.html").read_text(encoding="utf-8")

    @app.get("/v1/status", tags=["meta"])
    def status():
        return {
            "version": __version__,
            "uptime_s": round(time.time() - st.started_at, 1),
            "config": st.settings.as_dict(),
            "problems": st.settings.problems(),
            "store": dict(st.store.stats()),
            "migrations": st.store.applied_migrations(),
            "index": {"blocks": len(st.index), "pages": st.index.n_pages},
        }

    # -- the mirror ------------------------------------------------------------

    @app.post("/v1/sync", tags=["mirror"])
    def sync(payload: dict = Body(default={})):
        """Pull Notion into the local mirror."""
        from palimpsest.notion import mirror

        if not st.settings.has_notion:
            raise HTTPException(400, "NOTION_TOKEN is not set")
        result = mirror.sync(
            st.notion, st.store,
            incremental=bool(payload.get("incremental", True)),
            roots=st.settings.notion_root_pages,
            limit=payload.get("limit"),
        )
        st.refresh_index()
        return result.as_dict()

    @app.get("/v1/pages", tags=["mirror"])
    def pages(limit: int = Query(200, le=2000)):
        return {"pages": [
            {k: p.get(k) for k in ("page_id", "title", "role", "url", "last_edited")}
            for p in st.store.get_pages(limit=limit)
        ]}

    @app.get("/v1/pages/{page_id}", tags=["mirror"])
    def page(page_id: str):
        page = st.store.get_page(page_id)
        if page is None:
            raise HTTPException(404, f"no page {page_id} in the mirror")
        blocks = st.store.get_blocks(page_id)
        return {"page": page,
                "blocks": [{k: b.get(k) for k in ("block_id", "type", "text", "position")}
                           for b in blocks],
                "history": st.store.page_history(page_id, limit=50)}

    @app.get("/v1/blocks/{block_id}/provenance", tags=["mirror"])
    def provenance(block_id: str):
        """Which source, claim and anchor produced the text in this block."""
        return {"block_id": block_id,
                "provenance": st.store.provenance_for_block(block_id)}

    # -- the pipeline ----------------------------------------------------------

    @app.post("/v1/ingest", tags=["pipeline"])
    def ingest(payload: dict = Body(...)):
        """Run a source through the pipeline. **Writes nothing to Notion.**"""
        from palimpsest.pipeline import ingest as run

        spec = (payload.get("spec") or payload.get("text") or "").strip()
        if not spec:
            raise HTTPException(422, "send {'spec': '<url | path | text:...>'}")
        if payload.get("text") and not payload.get("spec"):
            spec = f"text:{spec}"
        if not st.settings.has_model:
            raise HTTPException(400,
                                "ANTHROPIC_API_KEY is not set; extraction needs a model")
        try:
            result = run(spec, st.store, st.model, settings=st.settings,
                         kind=payload.get("kind"), index=st.index,
                         archive=st.artifacts,
                         reuse=bool(payload.get("reuse", True)))
        except (RuntimeError, ValueError, ImportError) as e:
            raise HTTPException(400, str(e)) from e
        return result.as_dict()

    @app.get("/v1/patches", tags=["pipeline"])
    def patches(status: str | None = None, limit: int = Query(50, le=500)):
        return {"patches": st.store.list_patches(status=status, limit=limit)}

    @app.get("/v1/patches/{patch_id}", tags=["pipeline"])
    def patch(patch_id: str):
        found = st.store.get_patch(patch_id)
        if found is None:
            raise HTTPException(404, f"no patch {patch_id}")
        return found.as_dict()

    @app.post("/v1/patches/{patch_id}/apply", tags=["pipeline"])
    def apply(patch_id: str, payload: dict = Body(default={})):
        """Apply a patch to Notion. The only route that mutates your workspace."""
        from palimpsest.notion.apply import apply_patch
        from palimpsest.types import Relation

        found = st.store.get_patch(patch_id)
        if found is None:
            raise HTTPException(404, f"no patch {patch_id}")

        # Safety checks run BEFORE the configuration check, deliberately. A caller who
        # tries to apply a contradiction should be told that, not handed
        # "NOTION_TOKEN is not set" — an error that misdiagnoses a refusal as a setup
        # problem invites them to "fix" it by adding a token.
        reviewer = (payload.get("reviewer") or "").strip()
        dry_run = bool(payload.get("dry_run", False))

        only = payload.get("operations")
        if only:
            keep = set(only)
            found.operations = [op for op in found.operations if op.op_id in keep]

        # A contradiction-derived operation cannot reach Notion through this route, no
        # matter what the caller asks for. The planner already refuses to emit one;
        # this is the second lock on the same door.
        blocked = [op for op in found.operations if op.relation is Relation.CONTRADICTS]
        if blocked:
            raise HTTPException(
                400, f"{len(blocked)} operation(s) derive from a contradiction and are "
                     "never applied automatically; resolve them on the page instead")

        if not dry_run and not reviewer:
            raise HTTPException(422, "an applied patch must record who approved it: "
                                     "send {'reviewer': '...'}")
        if not st.settings.has_notion:
            raise HTTPException(400, "NOTION_TOKEN is not set")

        result = apply_patch(st.notion, st.store, found, dry_run=dry_run,
                             reviewer=reviewer or None)
        if not dry_run:
            st.refresh_index()
        return result.as_dict()

    @app.post("/v1/patches/{patch_id}/reject", tags=["pipeline"])
    def reject(patch_id: str, payload: dict = Body(default={})):
        reviewer = (payload.get("reviewer") or "").strip()
        if not reviewer:
            raise HTTPException(422, "send {'reviewer': '...'}")
        st.store.set_patch_status(patch_id, "rejected", reviewer=reviewer,
                                  notes=payload.get("notes"))
        return {"patch_id": patch_id, "status": "rejected"}

    @app.post("/v1/patches/{patch_id}/undo", tags=["pipeline"])
    def undo(patch_id: str, payload: dict = Body(default={})):
        """Revert an applied patch, exactly."""
        from palimpsest.notion.apply import revert_patch

        found = st.store.get_patch(patch_id)
        if found is None:
            raise HTTPException(404, f"no patch {patch_id}")
        result = revert_patch(st.notion, st.store, found,
                              reviewer=payload.get("reviewer"))
        st.refresh_index()
        return result.as_dict()

    # -- the sweeps ------------------------------------------------------------

    @app.post("/v1/sweep/{kind}", tags=["sweeps"])
    def sweep(kind: str, payload: dict = Body(default={})):
        from palimpsest import sweep as sweeps

        if kind == "duplicates":
            return sweeps.duplicates(st.store, index=st.index,
                                     threshold=float(payload.get("threshold", 0.32))
                                     ).as_dict()
        if kind == "stale":
            return sweeps.stale(st.store).as_dict()
        if kind == "questions":
            return sweeps.open_questions(st.store).as_dict()
        if kind == "contradictions":
            if not st.settings.has_model:
                raise HTTPException(400, "the contradiction sweep needs a model")
            return sweeps.contradictions(
                st.store, st.model, index=st.index,
                max_pairs=int(payload.get("max_pairs", 120))).as_dict()
        raise HTTPException(404, f"unknown sweep {kind!r}; try duplicates, "
                                 "contradictions, stale or questions")

    @app.get("/v1/records", tags=["meta"])
    def records(kind: str | None = None, limit: int = Query(20, le=200)):
        return {"records": st.store.get_records(kind=kind, limit=limit)}

    @app.get("/v1/sources", tags=["meta"])
    def sources(limit: int = Query(50, le=500)):
        return {"sources": st.store.list_sources(limit=limit)}

    @app.exception_handler(ValueError)
    def value_error_handler(_request, exc: ValueError):  # pragma: no cover - defensive
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app
