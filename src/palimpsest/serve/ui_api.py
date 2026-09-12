"""The routes the desktop UI needs that the CLI never did.

Four groups, and each exists because a graphical surface asks a question the terminal
did not have to:

- **setup** — the wizard moves into the app, so the UI needs to *validate a key while
  you type it*, list the Notion pages you could build under, create one, and watch for
  your first Telegram message to pair. The terminal wizard could do these inline; a UI
  needs them as endpoints.
- **settings** — read the current config with secrets redacted, and write it back. This
  is the one genuinely dangerous route here, so it refuses unless the server is bound to
  localhost: writing credentials over a network socket is not a thing this should ever
  do, and a config layer that already refuses to bind publicly without a key should not
  then accept credential writes from one.
- **approvals** — the gate's queue, previously reachable only from Telegram. Same gate,
  same `approval.resolve`; the UI is just another surface tapping the same button.
- **events** — a server-sent stream so the UI can *animate work as it happens* rather
  than polling and jerking. It diffs the job and approval tables about once a second and
  emits what changed, which needs no new dependency and no message broker.

Like `upload.py`, this module skips `from __future__ import annotations` so FastAPI can
resolve its parameter types at runtime, and is imported only from inside `create_app()`.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Body, HTTPException, Query
from fastapi.responses import StreamingResponse

log = logging.getLogger("palimpsest.serve.ui")

__all__ = ["register"]

#: Keys the UI is allowed to write. An allowlist, not a filter — an open-ended settings
#: write from a browser is a way to set `PALIMPSEST_APPLY=1` on someone's behalf.
WRITABLE = {
    "ANTHROPIC_API_KEY", "NOTION_TOKEN", "PALIMPSEST_NOTION_ROOTS",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHATS",
    "GROQ_API_KEY", "DEEPGRAM_API_KEY", "SARVAM_API_KEY", "FIRECRAWL_API_KEY",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
    "PALIMPSEST_APPLY", "PALIMPSEST_AUTONOMY",
    # Choosing a provider is configuration, so the wizard and Settings can set it.
    "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "PALIMPSEST_MODEL", "PALIMPSEST_MODEL_PROVIDER", "PALIMPSEST_MODEL_BASE_URL",
    "PALIMPSEST_MODEL_API_KEY",
    "PALIMPSEST_EMBED_MODEL", "PALIMPSEST_EMBED_BASE_URL", "PALIMPSEST_EMBED_API_KEY",
    "PALIMPSEST_EMBED_PROVIDER",
}

#: How often the event stream looks for changes. Fast enough that a capture feels live,
#: slow enough that an idle app is not spinning a core.
POLL_S = 1.0


def register(app, st) -> None:
    """Attach every UI route to the app."""

    def _local_only_or_403() -> None:
        if not st.settings.is_local_only:
            raise HTTPException(
                403, "settings can only be changed from a local instance. This server is "
                     "bound to a network interface; set the config file on the host "
                     "instead.")

    # -- setup / onboarding ---------------------------------------------------

    @app.get("/v1/setup/state", tags=["setup"])
    def setup_state():
        """What is configured, what is missing, and whether onboarding should show."""
        from palimpsest.onboard import is_configured

        s = st.settings
        return {
            "configured": is_configured(s),
            "steps": {
                "model": bool(s.has_model),
                "notion": bool(s.has_notion),
                "root": bool(s.notion_root_pages),
                "telegram_token": bool(s.telegram_token),
                "telegram_paired": bool(s.telegram_allowed_chats),
            },
            "optional": {
                "transcription": s.transcriber,
                "tracing": bool(s.extras.get("langfuse")) or _has_langfuse(),
            },
            "problems": s.problems(),
            "apply": s.apply,
            "autonomy": s.autonomy,
        }

    @app.get("/v1/setup/local", tags=["setup"])
    def setup_local():
        """Which local model runtimes are actually running, and what they have loaded.

        Probing here rather than in `config.describe_setup`: resolution must stay a pure
        function of the settings, and quietly pointing at localhost because something
        answered would be a surprising thing for it to do. The wizard, on the other hand,
        genuinely wants to know — offering "use the models already on this machine" is
        only helpful if it can say which ones, and only honest if it checked.
        """
        import json as jsonlib
        import urllib.error
        import urllib.request

        from palimpsest.config import LOCAL_RUNTIMES

        found = []
        for name, (base, chat, embed_model) in LOCAL_RUNTIMES.items():
            try:
                request = urllib.request.Request(f"{base}/models")
                with urllib.request.urlopen(request, timeout=1.5) as response:
                    body = jsonlib.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                continue
            ids = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
            # Ollama reports embedding models alongside chat ones with no flag to tell
            # them apart, so the split is by name. Wrong guesses are recoverable — the
            # wizard shows both lists and the user picks.
            embeds = [i for i in ids if "embed" in i.lower()]
            found.append({
                "provider": name,
                "base_url": base,
                "models": [i for i in ids if i not in embeds],
                "embedding_models": embeds,
                "suggested_model": chat if chat in ids else next(
                    (i for i in ids if i not in embeds), ""),
                "suggested_embedding": embed_model if embed_model in ids else (
                    embeds[0] if embeds else ""),
            })
        return {"runtimes": found}

    @app.post("/v1/setup/validate", tags=["setup"])
    def setup_validate(payload: dict = Body(...)):
        """Check a key against the real service, so the UI can tick it green live."""
        provider = (payload.get("provider") or "").lower()
        token = (payload.get("token") or "").strip()
        if not token:
            return {"ok": False, "error": "no token given"}

        try:
            if provider == "notion":
                from palimpsest.notion.client import NotionClient

                who = NotionClient(token).whoami()
                return {"ok": True, "detail": who.get("name") or "your integration"}
            if provider == "telegram":
                from palimpsest import telegram as tg

                me = tg._call(token, "getMe")
                return {"ok": True, "detail": f"@{me.get('username')}",
                        "username": me.get("username")}
            if provider == "anthropic":
                # Deliberately a shape check, not a live call: validating by spending a
                # token on every keystroke would be rude, and a wrong key surfaces
                # immediately on the first real use with a clear error.
                ok = token.startswith("sk-ant-")
                return {"ok": ok,
                        "detail": "looks right" if ok else "Anthropic keys start with sk-ant-"}
            return {"ok": False, "error": f"unknown provider {provider!r}"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    @app.get("/v1/setup/notion/pages", tags=["setup"])
    def setup_notion_pages(token: str = Query(default="")):
        """Pages the integration can see — the candidates for a root."""
        from palimpsest.notion.client import NotionClient

        use = token.strip() or (st.settings.notion_token or "")
        if not use:
            raise HTTPException(400, "no Notion token")
        try:
            from palimpsest.onboard import _shared_pages

            pages = _shared_pages(NotionClient(use), limit=25)
            return {"pages": [{"page_id": pid, "title": title} for pid, title in pages]}
        except Exception as e:
            raise HTTPException(400, str(e)[:300]) from e

    @app.post("/v1/setup/notion/root", tags=["setup"])
    def setup_notion_root(payload: dict = Body(default={})):
        """Create a fresh top-level page to build under, so it nests inside nothing."""
        from palimpsest.notion.client import NotionClient
        from palimpsest.onboard import _create_root

        token = (payload.get("token") or st.settings.notion_token or "").strip()
        if not token:
            raise HTTPException(400, "no Notion token")
        try:
            pid, url = _create_root(NotionClient(token),
                                    payload.get("title") or "palimpsest")
            return {"page_id": pid, "url": url}
        except Exception as e:
            raise HTTPException(400, str(e)[:300]) from e

    @app.post("/v1/setup/telegram/pair", tags=["setup"])
    async def setup_telegram_pair(payload: dict = Body(default={})):
        """Wait for the user's first message and return the chat id it came from.

        Long-polls Telegram for up to ~50s. The UI calls this, tells the user to message
        the bot, and ticks green when it returns — the same trick as the terminal wizard,
        so nobody ever hunts for a chat id.
        """
        from palimpsest import telegram as tg

        token = (payload.get("token") or st.settings.telegram_token or "").strip()
        if not token:
            raise HTTPException(400, "no Telegram token")

        def _wait() -> dict:
            offset = 0
            try:  # drain the backlog so an old message cannot pair the wrong account
                for u in tg._call(token, "getUpdates", {"timeout": 0}) or []:
                    offset = max(offset, int(u["update_id"]) + 1)
            except Exception:
                pass
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    updates = tg._call(token, "getUpdates",
                                       {"offset": offset, "timeout": 15}, timeout=25) or []
                except Exception:
                    time.sleep(1)
                    continue
                for u in updates:
                    offset = max(offset, int(u["update_id"]) + 1)
                    chat = (u.get("message") or {}).get("chat") or {}
                    if chat.get("type") == "private" and chat.get("id"):
                        return {"paired": True, "chat_id": chat["id"],
                                "name": chat.get("first_name") or chat.get("username")}
            return {"paired": False}

        return await asyncio.to_thread(_wait)

    # -- settings -------------------------------------------------------------

    @app.get("/v1/settings", tags=["settings"])
    def get_settings():
        """Current configuration with every secret redacted."""
        import os

        from palimpsest.config import config_path, redact

        present = {k: bool(os.environ.get(k)) for k in sorted(WRITABLE)}
        values = {k: redact(os.environ.get(k, ""), k) for k in sorted(WRITABLE)}
        # These two are not secrets and the UI needs their real values to show state.
        for plain in ("PALIMPSEST_APPLY", "PALIMPSEST_AUTONOMY",
                      "PALIMPSEST_NOTION_ROOTS", "TELEGRAM_ALLOWED_CHATS"):
            values[plain] = os.environ.get(plain, "")
        return {"values": values, "present": present,
                "config_path": str(config_path()),
                "writable": sorted(WRITABLE)}

    @app.post("/v1/settings", tags=["settings"])
    def put_settings(payload: dict = Body(...)):
        """Persist settings to the config file and apply them to this process.

        Local-only. Keys arrive in the clear over the loopback interface, which is
        acceptable for a desktop app talking to its own backend and is not acceptable
        over a network — hence the guard.
        """
        _local_only_or_403()
        import os

        from palimpsest.config import Settings
        from palimpsest.onboard import _write

        updates = {k: str(v) for k, v in (payload.get("values") or {}).items()
                   if k in WRITABLE and v is not None and str(v) != ""}
        if not updates:
            raise HTTPException(422, "nothing to save")

        _write(updates)
        # Make them live now rather than at next restart, so the UI's next call sees
        # the new state. The queue and clients are rebuilt lazily from settings.
        os.environ.update(updates)
        st.settings = Settings.load()
        st._model = st._notion = st._journal = None
        return {"saved": sorted(updates), "restart_recommended": True}

    # -- approvals ------------------------------------------------------------

    @app.get("/v1/approvals", tags=["approvals"])
    def list_approvals(status: str = "pending", limit: int = Query(50, le=200)):
        """The gate's queue, with each patch's operations expanded for a diff view."""
        st.store.expire_approvals()
        out = []
        for row in st.store.list_approvals(status=status or None, limit=limit):
            patch = st.store.get_patch(row["patch_id"])
            ops = []
            if patch is not None:
                keep = set(row.get("operation_ids") or [])
                for op in patch.operations:
                    if keep and op.op_id not in keep:
                        continue
                    page = st.store.get_page(op.target) or {}
                    ops.append({
                        "op_id": op.op_id, "kind": op.kind.value,
                        "relation": op.relation.value if op.relation else None,
                        "risk": op.risk_tier,
                        "page": page.get("title") or op.target[:8],
                        "page_url": page.get("url"),
                        "text": (op.payload or {}).get("text")
                        or (op.payload or {}).get("title"),
                        "was": (op.payload or {}).get("was"),
                        "why": (op.payload or {}).get("rationale"),
                        "confidence": (op.payload or {}).get("confidence"),
                    })
            out.append({**row, "operations": ops,
                        "source": _source_of(st.store, patch)})
        return {"approvals": out}

    @app.post("/v1/approvals/{approval_id}/resolve", tags=["approvals"])
    def resolve_approval(approval_id: str, payload: dict = Body(default={})):
        """Approve or reject — the same gate the bot taps."""
        from palimpsest import approval

        decision = (payload.get("decision") or "approved").lower()
        if decision not in ("approved", "rejected"):
            raise HTTPException(422, "decision must be 'approved' or 'rejected'")

        result = approval.resolve(
            st.store, approval_id, decision,
            by=payload.get("by") or "ui",
            notion_factory=(lambda: st.notion) if st.settings.has_workspace else None,
            journal_factory=(lambda: st.journal) if st.settings.has_workspace else None)
        # Any failure is a failure, including a rejection. The `and decision ==
        # "approved"` this used to carry meant that rejecting an approval which was
        # missing, already resolved or expired returned 200 with `ok: false` — so the UI
        # rendered a successful rejection that had not happened, and the card stayed gone
        # while the proposal stayed pending.
        if not result.get("ok"):
            raise HTTPException(409, result.get("error") or f"could not {decision[:-1]}")
        st.refresh_index()
        return result

    # -- activity and undo ----------------------------------------------------

    @app.get("/v1/activity", tags=["activity"])
    def activity(limit: int = Query(default=60, ge=1, le=300)):
        """Everything that has happened, newest first, with what can still be undone.

        One list rather than three. A patch that applied automatically, a patch you
        approved, and a patch you rejected are the same event from the reader's side —
        "something was proposed and this is what became of it" — and splitting them by
        which door they came through makes the history unreadable exactly when you are
        trying to find the change that broke something.
        """
        rows: list[dict] = []
        for patch_row in st.store.list_patches(limit=limit):
            patch = st.store.get_patch(patch_row["patch_id"])
            if patch is None:
                continue
            source = _source_of(st.store, patch)
            applied = [op for op in patch.operations if op.applied_at]
            # Reversal is recorded in the ledger, not on the patch: `revert_patch`
            # stamps `applied_ops.reverted_at` and sets the patch's status, and the
            # in-memory operations it worked from are not written back. Reading the
            # operation objects reported every undone patch as still undoable, which is
            # a button that re-applies what you just took back.
            reverted = patch_row["status"] == "reverted"
            rows.append({
                "patch_id": patch.patch_id,
                "status": patch_row["status"],
                "at": patch_row.get("applied_at") or patch_row.get("created_at"),
                "reviewer": patch_row.get("reviewer"),
                "source": source,
                "operations": len(patch.operations),
                "applied": len(applied),
                # Undoable means: it reached Notion, and it has not already been taken
                # back. A rejected or still-pending patch has nothing to undo.
                "undoable": bool(applied) and not reverted,
                "reverted": reverted,
                "relations": sorted({op.relation.value for op in patch.operations
                                     if op.relation}),
                "pages": sorted({p for p in (_page_name(st.store, op) for op in
                                             patch.operations) if p}),
            })
        return {"activity": rows}

    @app.post("/v1/patches/{patch_id}/undo", tags=["activity"])
    def undo_patch(patch_id: str):
        """Take back an applied patch, exactly.

        Every operation carries its own inverse, computed before it ran, so this is a
        real reversal rather than a compensating guess. It is the reason the autonomy
        ladder can go as far as it does: the answer to "it did something I did not want"
        is one button, not a manual repair.
        """
        from palimpsest.notion.apply import revert_patch

        patch = st.store.get_patch(patch_id)
        if patch is None:
            raise HTTPException(404, f"no patch {patch_id}")
        if not st.settings.has_workspace:
            raise HTTPException(400, "NOTION_TOKEN is not set, so nothing can be undone")
        if not any(op.applied_at for op in patch.operations):
            raise HTTPException(409, "that patch was never applied, so there is nothing "
                                     "to undo")

        result = revert_patch(st.notion, st.store, patch, reviewer="ui",
                              journal=st.journal if st.settings.journal else None)
        st.refresh_index()
        payload = result.as_dict() if hasattr(result, "as_dict") else {}
        if result.failed:
            raise HTTPException(409, f"undo did not complete: {payload.get('errors')}")
        return {"ok": True, "patch_id": patch_id, **payload}

    # -- the agent ------------------------------------------------------------

    @app.post("/v1/agent", tags=["agent"])
    async def agent_turn(payload: dict = Body(...)):
        """One conversational turn. Same loop Telegram drives."""
        message = (payload.get("message") or "").strip()
        if not message:
            raise HTTPException(422, "no message")
        if not st.settings.has_model:
            raise HTTPException(400, "ANTHROPIC_API_KEY is not set")

        def _run() -> dict:
            from palimpsest.agent import ToolContext
            from palimpsest.agent.loop import run_turn

            ctx = ToolContext(st.settings, queue=st.queue)
            try:
                session = payload.get("session_id") or "ui"
                reply = run_turn(ctx, message, session_id=session, chat_id=None)
                return {"text": reply.text, "tools": reply.tool_calls,
                        "approvals": reply.approvals, "steps": reply.steps,
                        "trace_id": reply.trace_id, "error": reply.error}
            finally:
                ctx.close()

        return await asyncio.to_thread(_run)

    # -- the live stream ------------------------------------------------------

    @app.get("/v1/events", tags=["events"])
    async def events() -> StreamingResponse:
        """Server-sent events describing work as it happens.

        Polling the store and diffing is deliberately unglamorous: it needs no broker,
        no extra dependency and no callback wired into the worker threads — and a worker
        that can block on an HTTP write to a browser is a worker that stops draining the
        queue.
        """
        async def stream() -> AsyncIterator[str]:
            seen_jobs: dict[str, str] = {}
            seen_approvals: set[str] = set()
            yield _sse("hello", {"at": time.time()})
            while True:
                try:
                    for job in st.store.list_jobs(limit=25):
                        jid, status = job["job_id"], job["status"]
                        if seen_jobs.get(jid) != status:
                            seen_jobs[jid] = status
                            yield _sse("job", _job_event(job))
                    for row in st.store.list_approvals(status="pending", limit=25):
                        if row["approval_id"] not in seen_approvals:
                            seen_approvals.add(row["approval_id"])
                            yield _sse("approval", {
                                "approval_id": row["approval_id"],
                                "patch_id": row["patch_id"],
                                "summary": row.get("summary"),
                                "operations": len(row.get("operation_ids") or []),
                            })
                except Exception as e:  # a stream failure must not kill the app
                    log.debug("event poll failed: %s", e)
                await asyncio.sleep(POLL_S)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _job_event(job: dict) -> dict:
    result = job.get("result") or {}
    applied = result.get("auto_applied") or {}
    return {
        "job_id": job["job_id"], "status": job["status"],
        "title": job.get("title") or job.get("url") or (job.get("spec") or "")[:60],
        "kind": (result.get("source") or {}).get("kind"),
        "claims": result.get("claims"),
        "by_relation": (result.get("patch") or {}).get("by_relation"),
        "applied": applied.get("applied"),
        "held": applied.get("held"),
        "approval_id": applied.get("approval_id"),
        "error": job.get("error"),
    }


def _page_name(store, op) -> str | None:
    """A human name for whatever an operation touched, for the activity list."""
    title = (op.payload or {}).get("title")
    if title:
        return str(title)
    page_id = (op.result or {}).get("page_id")
    if not page_id:
        block = store.get_block(op.target)
        page_id = (block or {}).get("page_id") or op.target
    page = store.get_page(page_id) if page_id else None
    return (page or {}).get("title") or None


def _source_of(store, patch) -> dict:
    if patch is None or not patch.source_id:
        return {}
    try:
        source = store.get_source(patch.source_id)
        if source is None:
            return {}
        return {"title": source.title, "kind": source.kind, "url": source.url}
    except Exception:
        return {}


def _has_langfuse() -> bool:
    import os

    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))
