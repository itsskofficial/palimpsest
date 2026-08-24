"""First-run setup, in the terminal, with nothing to edit by hand.

`palimpsest serve` on an unconfigured machine drops you here. It asks for each key,
*checks it on the spot*, helps you make the one Notion page the whole thing hangs on,
and pairs your Telegram account by watching for your first message — then writes it all
to a config file the app reads on every later start. The goal is that a stranger on a
fresh VM types `pip install palimpsest`, `palimpsest serve`, answers a few questions,
and is chatting to their notes minutes later without ever opening an editor.

Deliberately plain prompts, not a full-screen TUI. A wizard that works over a flaky SSH
session and a 80-column terminal is worth more here than one that looks impressive and
breaks when the window is the wrong size — and it keeps the project's no-dependencies
promise: this is `input()` and the clients we already have, nothing new.

Every step validates before it moves on, because the failure this prevents is the worst
kind: a setup that *looks* finished and then silently does nothing — a Notion token with
no page shared, a bot token typo, a chat id off by a digit. Each is caught here, with the
fix named, rather than discovered as silence three days later.
"""

from __future__ import annotations

import sys
import time

from palimpsest.config import config_path

__all__ = ["is_configured", "run"]

# ANSI, degrading to nothing when the terminal cannot render it. No dependency for this;
# a handful of escapes is not worth `rich`.
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _bold(t: str) -> str: return _c("1", t)
def _dim(t: str) -> str: return _c("2", t)
def _green(t: str) -> str: return _c("32", t)
def _red(t: str) -> str: return _c("31", t)
def _cyan(t: str) -> str: return _c("36", t)


def _ok(msg: str) -> None:
    print(f"  {_green('✓')} {msg}")


def _err(msg: str) -> None:
    print(f"  {_red('✗')} {msg}")


def _ask(prompt: str, *, secret: bool = False, default: str | None = None,
         allow_blank: bool = False) -> str:
    """Prompt until a non-blank answer (unless `allow_blank`). Blank takes `default`."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"  {_cyan('›')} {prompt}{suffix}: ").strip()
        except EOFError:
            # stdin closed (piped input ran out, or not a real terminal). Take the
            # default if there is one; otherwise treat it as a cancel rather than
            # spinning forever on a required prompt nobody can answer.
            if default is not None or allow_blank:
                return default or ""
            raise KeyboardInterrupt from None
        if not value and default is not None:
            return default
        if value or allow_blank:
            return value
        print(_dim("    (required)"))


def is_configured(settings) -> bool:
    """Whether the essentials for the bot to actually do something are present."""
    return bool(settings.has_notion and settings.has_model and settings.telegram_token
                and settings.telegram_allowed_chats)


# ---------------------------------------------------------------------------
# the steps
# ---------------------------------------------------------------------------


def _step_anthropic(values: dict) -> None:
    print(_bold("\n1. Claude — the brain"))
    print(_dim("   Extraction, classification, and the agent. Get a key at "
               "https://console.anthropic.com/settings/keys"))
    existing = values.get("ANTHROPIC_API_KEY")
    key = _ask("ANTHROPIC_API_KEY", default=existing if existing else None)
    if not key.startswith("sk-ant-"):
        _err("that doesn't look like an Anthropic key (they start with sk-ant-), but "
             "saving it anyway — fix it later with `palimpsest setup` if it fails")
    values["ANTHROPIC_API_KEY"] = key
    _ok("saved")


def _step_notion(values: dict) -> None:
    print(_bold("\n2. Notion — where your notes live"))
    print(_dim("   1) Go to https://www.notion.so/my-integrations → New integration\n"
               "   2) Give it Read + Update + Insert content capability\n"
               "   3) Copy the token (starts with ntn_)\n"
               "   4) IMPORTANT: open a Notion page → ⋯ → Connections → your integration.\n"
               "      An integration sees NOTHING until a page is shared with it."))

    from palimpsest.notion.client import NotionClient, NotionError

    while True:
        token = _ask("NOTION_TOKEN", default=values.get("NOTION_TOKEN"))
        try:
            client = NotionClient(token)
            who = client.whoami()
            _ok(f"connected as “{who.get('name') or 'your integration'}”")
            values["NOTION_TOKEN"] = token
            break
        except NotionError as e:
            _err(f"Notion rejected that token: {e}. Try again.")
        except Exception as e:
            _err(f"could not reach Notion: {e}. Check your connection and try again.")

    _choose_root(client, values)


def _choose_root(client, values: dict) -> None:
    """Pick or create the one page palimpsest builds under — the isolation boundary."""
    print(_dim("\n   palimpsest works inside ONE page and its children — your other "
               "pages stay untouched.\n   This is where new notes, hubs and the change "
               "log get created."))
    try:
        pages = _shared_pages(client)
    except Exception:
        pages = []

    if pages:
        print(_dim("   Pages shared with the integration:"))
        for i, (pid, title) in enumerate(pages[:10], 1):
            print(f"     {i}. {title}  {_dim(pid)}")
        print(_dim("   Enter a number to use one of those, or press Enter to create a "
                   "fresh “palimpsest” page."))
        choice = _ask("choice", allow_blank=True, default="")
        if choice.isdigit() and 1 <= int(choice) <= min(10, len(pages)):
            pid = pages[int(choice) - 1][0]
            values["PALIMPSEST_NOTION_ROOTS"] = pid
            _ok(f"using “{pages[int(choice) - 1][1]}”")
            return

    # Create a fresh top-level page, so it nests inside none of their existing notes.
    try:
        pid, url = _create_root(client, "palimpsest")
        values["PALIMPSEST_NOTION_ROOTS"] = pid
        _ok(f"created a new page: {url}")
    except Exception as e:
        _err(f"couldn't create a page automatically ({e}).")
        manual = _ask("paste a Notion page id to use as the root (or leave blank to skip)",
                      allow_blank=True)
        if manual:
            values["PALIMPSEST_NOTION_ROOTS"] = manual.replace("-", "")


def _step_telegram(values: dict) -> None:
    print(_bold("\n3. Telegram — how you talk to it"))
    print(_dim("   1) Open Telegram, message @BotFather, send /newbot, follow the prompts\n"
               "   2) It gives you a token like 123456:ABC-DEF...\n"
               "   3) Paste it here."))

    from palimpsest import telegram as tg

    while True:
        token = _ask("TELEGRAM_BOT_TOKEN", default=values.get("TELEGRAM_BOT_TOKEN"))
        try:
            me = tg._call(token, "getMe")
            username = me.get("username", "your bot")
            _ok(f"found your bot: @{username}")
            values["TELEGRAM_BOT_TOKEN"] = token
            break
        except Exception as e:
            _err(f"Telegram rejected that token: {e}. Try again.")

    _pair_chat(token, username, values)


def _pair_chat(token: str, username: str, values: dict) -> None:
    """Capture the user's chat id by watching for their first message — no hunting."""
    print(_dim(f"\n   Now open Telegram and send @{username} any message "
               f"(“hi” is fine).\n   This pairs your account — the bot ignores everyone "
               f"else."))
    print(_dim("   Waiting for your message… (Ctrl-C to skip and set it later)"))

    from palimpsest import telegram as tg

    offset = 0
    deadline = time.time() + 300
    try:
        # Clear any backlog first so an old message doesn't pair the wrong account.
        with _suppress():
            for u in tg._call(token, "getUpdates", {"timeout": 0}) or []:
                offset = max(offset, int(u["update_id"]) + 1)

        while time.time() < deadline:
            try:
                updates = tg._call(token, "getUpdates",
                                   {"offset": offset, "timeout": 20}, timeout=30) or []
            except Exception:
                time.sleep(2)
                continue
            for u in updates:
                offset = max(offset, int(u["update_id"]) + 1)
                chat = (u.get("message") or {}).get("chat") or {}
                if chat.get("type") == "private" and chat.get("id"):
                    name = chat.get("first_name") or chat.get("username") or "you"
                    values["TELEGRAM_ALLOWED_CHATS"] = str(chat["id"])
                    _ok(f"paired with {name} (chat id {chat['id']})")
                    return
        _err("timed out waiting for a message. Run `palimpsest setup` again to pair, "
             "or message the bot once and it will reply with your id.")
    except KeyboardInterrupt:
        print()
        _err("skipped. The bot will refuse every chat until you set TELEGRAM_ALLOWED_CHATS.")


def _step_optional(values: dict) -> None:
    print(_bold("\n4. Optional — press Enter to skip any of these"))
    print(_dim("   Voice notes need a speech-to-text key (any one):"))
    for key, label in (("GROQ_API_KEY", "Groq (free, fast, 25 MB limit)"),
                       ("DEEPGRAM_API_KEY", "Deepgram (long recordings, speaker labels)")):
        if values.get(key):
            continue
        v = _ask(f"{key} — {label}", allow_blank=True)
        if v:
            values[key] = v
    print(_dim("   Observability (see costs and traces at langfuse.com):"))
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        if values.get(key):
            continue
        v = _ask(key, allow_blank=True)
        if v:
            values[key] = v
    if values.get("LANGFUSE_PUBLIC_KEY") and "LANGFUSE_BASE_URL" not in values:
        values["LANGFUSE_BASE_URL"] = _ask(
            "LANGFUSE_BASE_URL", default="https://us.cloud.langfuse.com")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run(settings=None, *, force: bool = False) -> bool:
    """Run the wizard. Returns True if the essentials are configured at the end."""
    from palimpsest._console import install as install_console
    from palimpsest.config import Settings

    # Reconfigure stdout/stdin to UTF-8 with errors="replace" before printing any of the
    # symbols or curly quotes below — otherwise a legacy Windows console (cp1252) raises
    # on the first ✓. The CLI already does this in main(); doing it here too means the
    # wizard is safe whatever calls it.
    install_console()
    settings = settings or Settings.load()
    values = _existing_values(settings)

    print(_bold("\n  palimpsest setup"))
    print(_dim("  A few questions, checked as you go. Answers are saved to\n  "
               f"{config_path()}\n  and loaded automatically from then on."))

    if is_configured(settings) and not force:
        print(_green("\n  Looks like you're already set up.") +
              _dim(" Re-run with `palimpsest setup` to change anything.\n"))
        return True

    try:
        _step_anthropic(values)
        _step_notion(values)
        _step_telegram(values)
        _step_optional(values)
    except KeyboardInterrupt:
        print(_red("\n\n  Setup cancelled. Nothing was saved.\n"))
        return False

    _write(values)
    done = _summarise(values)
    return done


def _existing_values(settings) -> dict:
    """Seed the wizard with anything already configured, so re-running only fills gaps."""
    out = {}
    for key in ("ANTHROPIC_API_KEY", "NOTION_TOKEN", "TELEGRAM_BOT_TOKEN", "GROQ_API_KEY",
                "DEEPGRAM_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
                "LANGFUSE_BASE_URL"):
        import os
        if os.environ.get(key):
            out[key] = os.environ[key]
    if settings.notion_root_pages:
        out["PALIMPSEST_NOTION_ROOTS"] = settings.notion_root_pages[0]
    if settings.telegram_allowed_chats:
        out["TELEGRAM_ALLOWED_CHATS"] = ",".join(str(c) for c in
                                                  settings.telegram_allowed_chats)
    return out


def _write(values: dict) -> None:
    """Write the config file, preserving anything already there we did not ask about."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in values.items() if v})
    existing.setdefault("PALIMPSEST_APPLY", "0")
    existing.setdefault("PALIMPSEST_AUTONOMY", "none")

    body = ["# palimpsest configuration — written by `palimpsest setup`.",
            "# Real environment variables override anything here.", ""]
    body += [f"{k}={v}" for k, v in existing.items()]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    # Config holds bearer tokens; keep it to the owner where the OS allows it.
    with _suppress():
        path.chmod(0o600)
    _ok(f"saved to {path}")


def _summarise(values: dict) -> bool:
    from palimpsest.config import Settings

    # Re-load so we judge the *persisted* state, not the in-memory dict.
    settings = Settings.load()
    done = is_configured(settings)
    print(_bold("\n  You're set." if done else "\n  Almost there."))
    checks = [
        ("Claude", settings.has_model),
        ("Notion", settings.has_notion),
        ("a root page", bool(settings.notion_root_pages)),
        ("Telegram bot", bool(settings.telegram_token)),
        ("your account paired", bool(settings.telegram_allowed_chats)),
    ]
    for label, ok in checks:
        (_ok if ok else _err)(label)
    if done:
        print(_dim("\n  Starting the bot now. Message it on Telegram and say hi.\n"))
    else:
        print(_dim("\n  Fill the rest with `palimpsest setup`. The bot will start but "
                   "stay quiet until it's paired.\n"))
    return done


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _shared_pages(client, limit: int = 10) -> list[tuple[str, str]]:
    """(id, title) for pages the integration can see, newest first."""
    out = []
    for page in client.search_pages():
        title = ""
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        out.append((page.get("id", "").replace("-", ""), title or "Untitled"))
        if len(out) >= limit:
            break
    return out


def _create_root(client, title: str) -> tuple[str, str]:
    """Create a fresh top-level page and return (id, url). Nests inside nothing."""
    import json
    import urllib.request

    body = json.dumps({
        "parent": {"type": "workspace", "workspace": True},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        "icon": {"type": "emoji", "emoji": "🗂️"},
    }).encode()
    req = urllib.request.Request("https://api.notion.com/v1/pages", data=body,
                                 method="POST")
    req.add_header("Authorization", f"Bearer {client.token}")
    req.add_header("Notion-Version", client.version)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = json.loads(resp.read().decode("utf-8"))
    return page["id"].replace("-", ""), page.get("url", "")


class _suppress:
    """A tiny contextlib.suppress(Exception), inlined to keep imports minimal."""

    def __enter__(self): return self
    def __exit__(self, *exc): return True
