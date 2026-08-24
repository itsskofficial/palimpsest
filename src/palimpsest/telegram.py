"""The bot: send it anything, it tells you what changed and why.

This is the surface that makes the whole thing feel like a thing rather than a pipeline.
You forward a link, a PDF, a voice note, a screenshot — from your phone, on a train —
and some minutes later a message comes back saying *what it did to your notes*: eight
citations added automatically, one rewrite that needs you, tap to approve.

Three decisions shape the implementation:

**Long polling, not webhooks.** A webhook needs a public HTTPS URL, which for a tool
that runs on your laptop means a tunnel, a certificate and a thing that breaks whenever
your IP changes. `getUpdates` with a 25-second timeout costs one idle connection and
works behind any NAT, on hotel wifi, with no infrastructure at all.

**An allowlist is mandatory, and the pairing flow is the only way in.** A bot token is a
bearer credential: anyone who learns your bot's username can message it, and without a
check that means anyone can write to your Notion. So an unrecognised chat gets told its
own id and nothing else happens. You add that id and restart. There is deliberately no
"first person to message wins" auto-pairing — that is a race anyone can win by finding
your bot before you do.

**The reply says what changed, not that something was received.** A capture tool that
answers "queued" and then goes quiet teaches you to distrust it. Every job reports back:
what it extracted, what it applied, what it held, and *why* — the classifier's own
reasoning, the same text that goes into the Notion journal.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Bot", "run"]

log = logging.getLogger("palimpsest.telegram")

API = "https://api.telegram.org"

#: Telegram refuses `getFile` above this. It is the bot API's limit, not ours, and the
#: error has to say so or it reads like palimpsest rejecting the file.
MAX_FILE_BYTES = 20 * 1024 * 1024

#: How long `getUpdates` is allowed to hold the connection open. Long polling means one
#: idle request rather than a request every second.
POLL_TIMEOUT = 25

HELP = """*palimpsest*

Two things, and you don't need commands for either:

📥 *Send me anything* and it goes into your knowledge base — a link, a PDF, a
spreadsheet, an image, a voice note, or a thought you type. I pull out the claims, work
out how each relates to what you already wrote, and propose small, cited edits.

💬 *Ask me anything* and I answer from your notes, with links to the pages — "what do I
know about X", "did I write this twice", "what's pending". Just talk to me.

Anything I'm unsure about, or anything that would change existing text, I send back with
*Approve* / *Reject* buttons. Nothing touches Notion without your tap unless you turn
that on.

Commands, if you want the fast path:
/pending — what's waiting for you
/sync — pull the latest from Notion
/organise — propose a tidier structure
/status — what's configured
/undo `<patch id>` — revert exactly"""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, params: dict | None = None,
          timeout: float = 40.0) -> Any:
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise TelegramError(f"{method} returned {e.code}: {detail}") from e
    if not payload.get("ok"):
        raise TelegramError(f"{method}: {payload.get('description')}")
    return payload.get("result")


def _md(text: str) -> str:
    """Escape the handful of characters Telegram's legacy Markdown chokes on."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------


@dataclass
class Bot:
    """A long-polling Telegram bot bound to one palimpsest instance."""

    token: str
    settings: Any
    store_factory: Any
    queue: Any
    allowed: frozenset[int] = frozenset()
    _offset: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _seen_jobs: set[str] = field(default_factory=set, repr=False)
    _me: dict = field(default_factory=dict, repr=False)

    # -- sending ---------------------------------------------------------------

    def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None,
             markdown: bool = True) -> dict | None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000],
                                  "disable_web_page_preview": True}
        if markdown:
            params["parse_mode"] = "Markdown"
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            return _call(self.token, "sendMessage", params)
        except TelegramError as e:
            # Markdown is the usual culprit — a stray underscore in a page title is
            # enough. Retrying as plain text is better than losing the message.
            log.warning("sendMessage failed (%s); retrying without markdown", e)
            try:
                return _call(self.token, "sendMessage",
                             {"chat_id": chat_id, "text": text[:4000]})
            except TelegramError:
                return None

    def edit(self, chat_id: int, message_id: int, text: str,
             buttons: list[list[dict]] | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id,
                                  "text": text[:4000], "parse_mode": "Markdown",
                                  "disable_web_page_preview": True}
        params["reply_markup"] = {"inline_keyboard": buttons or []}
        try:
            _call(self.token, "editMessageText", params)
        except TelegramError as e:
            log.debug("editMessageText failed: %s", e)

    def _say(self, chat_id: int, text: str,
             buttons: list[list[dict]] | None = None) -> None:
        """`send` that returns None, so a guard clause can `return self._say(...)`
        without handing back a value from a None-returning handler."""
        self.send(chat_id, text, buttons=buttons)

    # -- receiving -------------------------------------------------------------

    def _download(self, file_id: str, suggested: str | None) -> Path:
        info = _call(self.token, "getFile", {"file_id": file_id})
        size = int(info.get("file_size") or 0)
        if size > MAX_FILE_BYTES:
            raise TelegramError(
                f"Telegram will not serve files over {MAX_FILE_BYTES // (1024 * 1024)} MB "
                "to a bot. Drop this one on the desktop app instead.")
        remote = info["file_path"]
        name = Path(suggested or remote).name
        target = Path(tempfile.gettempdir()) / "palimpsest-telegram"
        target.mkdir(parents=True, exist_ok=True)
        out = target / f"{int(time.time())}_{name}"

        url = f"{API}/file/bot{self.token}/{remote}"
        with urllib.request.urlopen(url, timeout=180) as resp:
            out.write_bytes(resp.read())
        return out

    # -- the guard -------------------------------------------------------------

    def permitted(self, chat_id: int) -> bool:
        return chat_id in self.allowed

    def refuse(self, chat_id: int) -> None:
        """Tell an unknown chat its id, and nothing else.

        This is the pairing flow. It leaks only the id of the chat asking, which that
        chat already knows, and it means adding yourself is a copy-paste rather than a
        hunt through the API.
        """
        self.send(chat_id, (
            "This bot is not paired with you.\n\n"
            f"Your chat id is `{chat_id}`\n\n"
            "Add it to `TELEGRAM_ALLOWED_CHATS` and restart palimpsest."))
        log.warning("refused an unpaired chat: %s", chat_id)

    # -- handling messages -----------------------------------------------------

    def handle_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        if not self.permitted(chat_id):
            return self.refuse(chat_id)

        text = (message.get("text") or "").strip()
        caption = (message.get("caption") or "").strip()

        if text.startswith("/"):
            return self.handle_command(chat_id, text)

        # -- files, in the order Telegram prefers them ------------------------
        for key, name_key in (("document", "file_name"), ("audio", "file_name"),
                              ("voice", None), ("video", "file_name"),
                              ("video_note", None)):
            blob = message.get(key)
            if not blob:
                continue
            return self.capture_file(chat_id, blob, caption or None,
                                     blob.get(name_key) if name_key else None, key)

        if message.get("photo"):
            # Photos arrive as a ladder of sizes; the last is the largest.
            return self.capture_file(chat_id, message["photo"][-1], caption or None,
                                     "photo.jpg", "photo")

        # Free-form text goes to the agent, which decides whether it is something to
        # remember (→ capture) or something to answer (→ search and reply). A file has
        # no such ambiguity, which is why files bypass this and go straight to capture.
        if text:
            return self.converse(chat_id, text)

        self.send(chat_id, "I did not find anything in that message I can read.")

    def capture_file(self, chat_id: int, blob: dict, caption: str | None,
                     filename: str | None, kind: str) -> None:
        try:
            path = self._download(blob["file_id"], filename)
        except (TelegramError, urllib.error.URLError, OSError) as e:
            self._say(chat_id, f"Could not fetch that file.\n\n{_md(str(e))}")
            return

        # A voice note has no filename and therefore no suffix, so nothing downstream
        # would recognise it as audio. Give it one from the MIME type Telegram sent.
        if kind in ("voice", "video_note") or not Path(path).suffix:
            guessed = mimetypes.guess_extension(blob.get("mime_type") or "") or (
                ".ogg" if kind == "voice" else ".mp4")
            path = path.rename(path.with_suffix(guessed))

        job = self.queue.submit(str(path), title=caption or Path(path).stem,
                                origin=f"telegram:{chat_id}")
        self._seen_jobs.add(job["job_id"])
        self.send(chat_id, f"📥 `{_md(Path(path).name)}` — reading it now.")

    # -- conversation (the agent) ----------------------------------------------

    def converse(self, chat_id: int, text: str) -> None:
        """Run one agent turn for a text message, streaming progress, then reply.

        On its own thread so the poll loop keeps serving other chats. A fresh
        ToolContext per turn avoids sharing one SQLite connection across threads; the
        queue is shared so a capture the agent starts reaches the running workers.
        """
        def work() -> None:
            from palimpsest.agent import ToolContext
            from palimpsest.agent.loop import run_turn

            ctx = ToolContext(self.settings, queue=self.queue)
            try:
                if not self.settings.has_model:
                    self._say(chat_id, "`ANTHROPIC_API_KEY` is not set, so I can't "
                                       "answer or reason yet. I can still capture what "
                                       "you send.")
                    return
                session_id = self._session_id(ctx, chat_id)
                placeholder = self.send(chat_id, "_thinking…_")
                mid = (placeholder or {}).get("message_id")
                last_edit = {"t": 0.0}

                def on_step(note: str) -> None:
                    # Throttle: Telegram rate-limits edits, and a blur of them is worse
                    # than a steady one every second or so.
                    if mid and time.time() - last_edit["t"] > 1.1:
                        last_edit["t"] = time.time()
                        with contextlib.suppress(Exception):
                            self.edit(chat_id, mid, f"_{_md(note)}_")

                reply = run_turn(ctx, text, session_id=session_id,
                                 chat_id=str(chat_id), on_step=on_step)
                body = reply.text or "(no reply)"
                buttons = self._approval_buttons(reply.approvals)
                if mid:
                    self.edit(chat_id, mid, body, buttons=buttons)
                else:
                    self.send(chat_id, body, buttons=buttons)
            except Exception as e:
                log.exception("agent turn failed")
                self.send(chat_id, f"Something went wrong on my side. Nothing was "
                                   f"changed.\n\n{_md(str(e)[:300])}")
            finally:
                ctx.close()

        threading.Thread(target=work, daemon=True).start()

    def _session_id(self, ctx: Any, chat_id: int) -> str:
        from palimpsest.types import new_id

        existing = ctx.store.get_session_for_chat(str(chat_id))
        if existing:
            return existing["session_id"]
        sid = new_id("ses_")
        ctx.store.upsert_session({"session_id": sid, "chat_id": str(chat_id),
                                  "surface": "telegram", "started_at": time.time()})
        return sid

    # -- commands --------------------------------------------------------------

    def handle_command(self, chat_id: int, text: str) -> None:
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()

        if command in ("/start", "/help"):
            return self._say(chat_id, HELP)
        if command == "/status":
            return self.cmd_status(chat_id)
        if command == "/pending":
            return self.cmd_pending(chat_id)
        if command == "/sync":
            return self.cmd_sync(chat_id)
        if command == "/organise":
            return self.cmd_organise(chat_id)
        if command == "/undo":
            return self.cmd_undo(chat_id, argument)
        self.send(chat_id, f"I do not know `{_md(command)}`. /help lists what I do.")

    def cmd_status(self, chat_id: int) -> None:
        store = self.store_factory()
        try:
            stats = store.stats()
            problems = self.settings.problems()
        finally:
            store.close()

        lines = [
            "*Status*",
            f"mirror: {stats.get('pages', 0)} pages, {stats.get('blocks', 0)} blocks",
            f"queued: {stats.get('queued_jobs', 0)}",
            f"waiting for you: {stats.get('pending_patches', 0)}",
            f"mode: {'writing' if self.settings.apply else 'propose-only'}, "
            f"autonomy={self.settings.autonomy}",
            f"transcription: {self.settings.transcriber or 'not configured'}",
        ]
        if problems:
            lines += ["", "*Needs attention*"] + [f"• {_md(p)}" for p in problems]
        self.send(chat_id, "\n".join(lines))

    def cmd_sync(self, chat_id: int) -> None:
        if not self.settings.has_notion:
            return self._say(chat_id, "`NOTION_TOKEN` is not set.")
        self.send(chat_id, "Syncing…")

        def work() -> None:
            from palimpsest.notion import mirror
            from palimpsest.notion.client import NotionClient

            store = self.store_factory()
            try:
                result = mirror.sync(
                    NotionClient(self.settings.notion_token or "",
                                 version=self.settings.notion_version),
                    store, incremental=True,
                    roots=self.settings.notion_root_pages)
                d = result.as_dict()
                self.send(chat_id, f"Synced {d.get('pages', 0)} page(s), "
                                   f"{d.get('blocks', 0)} block(s).")
            except Exception as e:
                self.send(chat_id, f"Sync failed.\n\n{_md(str(e))}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True).start()

    def cmd_organise(self, chat_id: int) -> None:
        if not self.settings.has_model:
            return self._say(chat_id, "`ANTHROPIC_API_KEY` is not set.")
        self.send(chat_id, "Looking at the shape of the workspace…")

        def work() -> None:
            from palimpsest.llm import Model
            from palimpsest.notion.client import NotionClient
            from palimpsest.organise import organise

            store = self.store_factory()
            try:
                roots = self.settings.notion_root_pages
                result = organise(
                    store,
                    Model(self.settings.model, api_key=self.settings.anthropic_api_key,
                          max_tokens=self.settings.max_tokens),
                    root_page_id=roots[0] if roots else None,
                    min_confidence=self.settings.min_confidence)
                if not len(result.patch):
                    return self._say(chat_id, "Nothing worth moving. "
                                              f"{len(result.review)} for you to look at.")
                store.put_patch(result.patch)
                # Route the structural patch through the gate so it surfaces as an
                # approval, exactly like every other proposed change.
                from palimpsest import approval

                roots = self.settings.notion_root_pages
                out = approval.gate(
                    store, result.patch, self.settings, chat_id=str(chat_id),
                    notion_factory=(lambda: NotionClient(
                        self.settings.notion_token or "",
                        version=self.settings.notion_version))
                    if self.settings.has_notion else None,
                    summary=f"organise: {result.stats.get('pages_moved', 0)} move(s)")
                hubs = "\n".join(f"• {h.get('icon','')} {_md(h['name'])}"
                                 for h in result.hubs[:12])
                self.send(chat_id,
                          f"*Proposed shape*\n{hubs}\n\n"
                          f"{result.stats.get('pages_moved', 0)} page(s) to file, "
                          f"{len(result.review)} for you to decide.",
                          buttons=self._approval_buttons(
                              [out["approval_id"]] if out.get("approval_id") else []))
            except Exception as e:
                self.send(chat_id, f"Could not plan that.\n\n{_md(str(e))}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True).start()

    def cmd_pending(self, chat_id: int) -> None:
        store = self.store_factory()
        try:
            store.expire_approvals()
            rows = store.list_approvals(status="pending", limit=8)
        finally:
            store.close()
        if not rows:
            return self._say(chat_id, "Nothing waiting for you. ✨")
        self.send(chat_id, f"*{len(rows)} waiting for you:*")
        for row in rows:
            n = len(row.get("operation_ids") or [])
            self.send(chat_id, f"*{_md(row.get('summary') or 'a change')}*\n"
                               f"_{n} operation(s)_ · `{row['patch_id']}`",
                      buttons=self._approval_buttons([row["approval_id"]]))

    def cmd_undo(self, chat_id: int, patch_id: str) -> None:
        if not patch_id:
            return self._say(chat_id, "Which one? `/undo pch_…`")

        def work() -> None:
            from palimpsest.agent import ToolContext
            from palimpsest.notion.apply import revert_patch

            ctx = ToolContext(self.settings, queue=self.queue)
            try:
                patch = ctx.store.get_patch(patch_id)
                if patch is None:
                    return self._say(chat_id, f"`{patch_id}` is gone.")
                if not (self.settings.apply and self.settings.has_notion):
                    return self._say(chat_id, "Writes are off, so there's nothing "
                                              "applied to undo.")
                result = revert_patch(ctx.new_notion(), ctx.store, patch,
                                      reviewer=f"telegram:{chat_id}",
                                      journal=ctx.new_journal())
                self.send(chat_id, f"↩️ Reverted {result.applied} change(s).")
            except Exception as e:
                self.send(chat_id, f"Undo failed.\n\n{_md(str(e)[:300])}")
            finally:
                ctx.close()

        threading.Thread(target=work, daemon=True).start()

    # -- approvals -------------------------------------------------------------

    def _approval_buttons(self, approval_ids: list[str]) -> list[list[dict]] | None:
        """Approve / Reject for each held approval a turn produced."""
        if not approval_ids:
            return None
        rows = []
        for aid in approval_ids[:5]:
            rows.append([{"text": "✅ Approve", "callback_data": f"ap:{aid}"},
                         {"text": "❌ Reject", "callback_data": f"rj:{aid}"}])
        return rows

    def _resolve_approval(self, chat_id: int, approval_id: str, decision: str,
                          message_id: int | None) -> None:
        """Act on an Approve/Reject tap by driving the one gate. On its own thread —
        applying can take a few Notion round-trips."""
        def work() -> None:
            from palimpsest import approval
            from palimpsest.agent import ToolContext

            ctx = ToolContext(self.settings, queue=self.queue)
            try:
                result = approval.resolve(
                    ctx.store, approval_id, decision, by=f"telegram:{chat_id}",
                    notion_factory=ctx.new_notion if self.settings.has_notion else None,
                    journal_factory=ctx.new_journal if self.settings.has_notion else None)
                if decision == "rejected":
                    msg = "❌ Rejected. Nothing was written."
                elif result.get("ok"):
                    msg = f"✅ Applied {result.get('applied', 0)} change(s)."
                    errors = result.get("errors") or []
                    if errors:
                        msg += "\n" + "\n".join(f"• {_md(e)}" for e in errors[:3])
                else:
                    msg = f"Couldn't apply that: {_md(str(result.get('error')))}"
                if message_id:
                    self.edit(chat_id, message_id, msg)
                else:
                    self.send(chat_id, msg)
            except Exception as e:
                self.send(chat_id, f"That failed.\n\n{_md(str(e)[:300])}")
            finally:
                ctx.close()

        threading.Thread(target=work, daemon=True).start()

    def handle_callback(self, query: dict) -> None:
        chat_id = query["message"]["chat"]["id"]
        message_id = query["message"]["message_id"]
        data = query.get("data") or ""

        # Acknowledge first so the button stops spinning. Failing to acknowledge is
        # cosmetic; refusing to do the work because the acknowledgement failed is not.
        with contextlib.suppress(TelegramError):
            _call(self.token, "answerCallbackQuery", {"callback_query_id": query["id"]})

        if not self.permitted(chat_id):
            return self.refuse(chat_id)

        action, _, approval_id = data.partition(":")
        if action == "ap":
            self.edit(chat_id, message_id, "Applying…")
            self._resolve_approval(chat_id, approval_id, "approved", message_id)
        elif action == "rj":
            self._resolve_approval(chat_id, approval_id, "rejected", message_id)

    # -- reporting finished work ----------------------------------------------

    def report_finished(self) -> None:
        """Tell each chat what came of the things it sent.

        The queue is deliberately not given a callback into the bot: a worker thread
        that can block on a network call to Telegram is a worker thread that stops
        draining the queue. Polling the job table instead keeps the two independent.
        """
        store = self.store_factory()
        try:
            for job in store.list_jobs(limit=40):
                origin = job.get("origin") or ""
                if not origin.startswith("telegram:"):
                    continue
                if job["status"] not in ("done", "failed"):
                    continue
                if job["job_id"] not in self._seen_jobs:
                    continue
                self._seen_jobs.discard(job["job_id"])

                chat_id = int(origin.split(":", 1)[1])
                if job["status"] == "failed":
                    self.send(chat_id, f"⚠️ Could not read that.\n\n"
                                       f"{_md(str(job.get('error'))[:400])}")
                    continue
                self._report_one(chat_id, job)
        except Exception as e:  # pragma: no cover - reporting is never load-bearing
            log.warning("could not report finished jobs: %s", e)
        finally:
            store.close()

    def _report_one(self, chat_id: int, job: dict) -> None:
        result = job.get("result") or {}
        source = result.get("source") or {}
        applied = result.get("auto_applied") or {}
        patch = result.get("patch") or {}
        counts = patch.get("by_relation") or {}

        title = source.get("title") or job.get("title") or "that"
        lines = [f"*{_md(str(title)[:90])}*"]

        claims = result.get("claims", 0)
        if not claims:
            lines.append("Nothing worth keeping came out of it.")
            self._say(chat_id, "\n".join(lines))
            return

        breakdown = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        lines.append(f"{claims} claim(s)" + (f" → {breakdown}" if breakdown else ""))

        n_applied = applied.get("applied", 0)
        held = applied.get("held", 0)
        if n_applied:
            lines.append(f"✅ {n_applied} applied automatically")
        if applied.get("reason") and not n_applied:
            lines.append(f"_{_md(applied['reason'])}_")

        review = result.get("review") or []
        if review:
            lines.append(f"🔍 {len(review)} needs you:")
            for item in review[:3]:
                claim = (item.get("claim") or {}).get("text", "")
                reason = item.get("reason", "")
                lines.append(f"• _{_md(reason)}_ — {_md(claim[:120])}")

        if held:
            lines.append(f"🔍 {held} change(s) need your approval:")
        approval_id = applied.get("approval_id")
        self.send(chat_id, "\n".join(lines),
                  buttons=self._approval_buttons([approval_id]) if approval_id else None)

    # -- the loop --------------------------------------------------------------

    def poll_once(self) -> int:
        updates = _call(self.token, "getUpdates",
                        {"offset": self._offset, "timeout": POLL_TIMEOUT,
                         "allowed_updates": ["message", "callback_query"]},
                        timeout=POLL_TIMEOUT + 15)
        for update in updates or []:
            self._offset = max(self._offset, int(update["update_id"]) + 1)
            try:
                if "message" in update:
                    self.handle_message(update["message"])
                elif "callback_query" in update:
                    self.handle_callback(update["callback_query"])
            except Exception:
                log.exception("failed handling update %s", update.get("update_id"))
        return len(updates or [])

    def run(self) -> None:
        self._me = _call(self.token, "getMe") or {}
        log.info("telegram bot @%s is listening (%d paired chat(s))",
                 self._me.get("username", "?"), len(self.allowed))
        if not self.allowed:
            log.warning("TELEGRAM_ALLOWED_CHATS is empty — every chat will be refused "
                        "and told its id. Message the bot once, then add the id.")

        last_report = 0.0
        while not self._stop.is_set():
            try:
                self.poll_once()
            except TelegramError as e:
                log.error("telegram poll failed: %s", e)
                self._stop.wait(5.0)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log.warning("telegram unreachable (%s); retrying", e)
                self._stop.wait(5.0)

            if time.time() - last_report > 3.0:
                last_report = time.time()
                self.report_finished()

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------


def run(settings=None, queue=None) -> None:
    """Start the bot. Blocks until interrupted."""
    from palimpsest.artifacts import open_artifacts
    from palimpsest.config import Settings
    from palimpsest.jobs import JobQueue, ingest_runner
    from palimpsest.llm import Model
    from palimpsest.notion.client import NotionClient
    from palimpsest.store import open_store

    settings = settings or Settings.load()
    if not settings.telegram_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Message @BotFather on Telegram, send /newbot, and paste the token it "
            "gives you.")

    def new_store():
        return open_store(settings.database_url)

    if queue is None:
        queue = JobQueue(
            store_factory=new_store,
            handlers={"ingest": ingest_runner(
                settings,
                model_factory=lambda: Model(settings.model,
                                            api_key=settings.anthropic_api_key,
                                            max_tokens=settings.max_tokens),
                archive=open_artifacts(settings.artifact_url),
                notion_factory=(lambda: NotionClient(settings.notion_token or "",
                                                     version=settings.notion_version))
                if settings.has_notion else None)},
            workers=settings.workers,
        ).start()

    bot = Bot(token=settings.telegram_token, settings=settings,
              store_factory=new_store, queue=queue,
              allowed=frozenset(settings.telegram_allowed_chats))
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
