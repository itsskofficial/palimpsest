"""A knowledge base you can try in one command, with nothing of yours at risk.

    palimpsest demo

Copies the sample vault in `vault/` to a scratch directory, points the app at it, and
opens the review UI. No Notion account, no token, no API key, and nothing writable that
belongs to you. Delete the folder afterwards and no trace remains.

**Why this exists.** The product's claim is that it reads a new fact, works out how it
relates to what you already wrote, and proposes a reversible edit to the right page. That
claim is unverifiable from a README. Before this command the only way to check it was to
create a Notion integration, share a page with it, buy an API key, and then hand a
stranger's agent write access to your actual notes — which is a great deal to ask of
somebody deciding whether a project is worth ten minutes.

**What is real.** All of it. The demo is the ordinary pipeline running against the
markdown backend: the same mirror, the same retrieval, the same classifier, the same
planner, the same one write door, the same undo. Nothing is stubbed and no output is
faked. The only thing that changes is where the pages live.

**What it needs.** A model, like any other run. `palimpsest demo` will use whatever is
configured; failing that it looks for a local Ollama, which needs no key and no account.
If it finds neither it still starts, still shows the vault, and says plainly what to do
next rather than pretending to think.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

__all__ = ["PROMPTS", "install", "settings_for", "vault_source"]

log = logging.getLogger("palimpsest.demo")

#: Captures that show the classifier's vocabulary against this vault. The UI offers them
#: as one-click suggestions, because the interesting thing to watch is not *that*
#: something happens but *which* relation the system decides on — and a visitor who has
#: not read the notes cannot invent a claim that lands on `contradicts` on purpose.
#:
#: `expect` is what each one claims will happen, and `tests/demo/test_promises.py` runs
#: all four against a live model and fails if any stops being true. A demo that makes a
#: promise the product does not keep is worse than having no demo, and the only way to
#: know is to keep checking — the vault is fixed, but the model is not.
#:
#: Note what the fourth one does *not* claim. Its promise is about placement, not about
#: which relation gets chosen, because "this belongs on one of two pages and it has to
#: pick" is the honest description of that case. Pinning it to a relation name would be
#: asserting something neither the design nor the model guarantees.
PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "label": "A fact that argues with a page",
        "expect": ("contradicts",),
        "text": "Recent work finds that per-parameter gradient clipping consistently "
                "outperforms global-norm clipping on transformer language models, "
                "especially at large batch sizes.",
        "why": "The gradient clipping page says outright that per-parameter clipping "
               "is worse. Watch it record the disagreement next to that line instead "
               "of silently overwriting it — and note that a contradiction is the one "
               "thing it will not apply without you, at any autonomy setting.",
    },
    {
        "label": "A fact that sharpens a page",
        "expect": ("refines",),
        "text": "Spaced repetition works best when the first review happens within "
                "24 hours of learning; intervals after that can expand rapidly with "
                "little loss of retention.",
        "why": "The spaced repetition page says the first interval matters most and "
               "admits it never pinned down how soon. This supplies the number, so "
               "the edit should sharpen that sentence rather than add another one "
               "underneath it.",
    },
    {
        "label": "A fact that belongs somewhere new",
        "expect": ("new",),
        "text": "Mixture-of-experts models activate only a small subset of their "
                "parameters per token, which decouples parameter count from the "
                "compute cost of a forward pass.",
        "why": "Nothing here covers this. Watch it decide the vault needs something "
               "it does not have, and write it out properly — headings, prose, "
               "citation — rather than dumping a bullet into the nearest note.",
    },
    {
        "label": "A fact two pages both want",
        #: Deliberately no relation claimed. See the note above `PROMPTS`.
        "expect": (),
        "expect_page": ("Sleep and memory", "Spaced repetition"),
        "text": "Reviewing material shortly before sleep improves retention more than "
                "reviewing the same material in the morning.",
        "why": "This sits between the spaced repetition note and the sleep note, and "
               "it has to choose. Which page it lands on — and whether it leaves a "
               "link on the other — is the whole question of where a fact belongs.",
    },
)


def vault_source() -> Path:
    """The sample vault that ships inside the package."""
    return Path(__file__).parent / "vault"


def install(target: str | Path, *, force: bool = False) -> Path:
    """Copy the sample vault to `target`. Returns where it landed.

    Never overwrites an existing demo without `force`: somebody who ran the demo, typed
    a few things into it and came back the next day should find their session, not a
    reset one.
    """
    destination = Path(target).expanduser().resolve()
    marker = destination / ".palimpsest" / "demo.json"

    if destination.exists() and any(destination.glob("*.md")):
        if not force:
            log.info("reusing the existing demo vault at %s", destination)
            return destination
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(vault_source(), destination, dirs_exist_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"demo": true}', encoding="utf-8")
    return destination


def settings_for(vault: str | Path, *, database_url: str | None = None) -> Any:
    """Settings that point the whole app at the demo vault.

    Autonomy is `full`, which is the honest setting for a sandbox: everything the planner
    decides gets applied so a visitor sees the product work, except contradictions, which
    stop for a human even here. That exception is not a demo limitation — it is the
    product's central safety property, and watching it hold is more informative than
    watching it be bypassed.
    """
    from palimpsest.config import Settings

    vault = Path(vault).expanduser().resolve()
    settings = Settings.load()
    return _replace(
        settings,
        backend="markdown",
        vault_path=str(vault),
        database_url=database_url or f"sqlite:///{vault / '.palimpsest' / 'demo.db'}",
        artifact_url=f"file://{vault / '.palimpsest' / 'archive'}",
        apply=True,
        autonomy="full",
        # A demo has no second workspace to write a ledger into, and the Activity tab
        # reads the local store anyway.
        journal=False,
        # Hermetic, and this part is not a detail.
        #
        # `Settings.load()` reads the config file, which on a machine that has been
        # set up holds a real Telegram token and real tracing keys. Inheriting those
        # meant `palimpsest demo` started the user's actual bot -- so a message sent
        # to it from a phone would be answered by the demo, against the sample vault
        # -- and shipped every demo call to their Langfuse project. Neither is what
        # anybody means by "try it out".
        telegram_token=None,
        telegram_allowed_chats=(),
        # Notion is unreachable from here by construction, since the backend is a
        # vault; clearing the token as well means a demo cannot read one either.
        notion_token=None,
        notion_root_pages=(),
        environment="demo",
    )


def _replace(settings: Any, **changes: Any) -> Any:
    import dataclasses

    return dataclasses.replace(settings, **changes)


def ensure_model(settings: Any) -> tuple[Any, str]:
    """Make sure the demo has something to think with. Returns (settings, description).

    Order of preference: whatever the environment already describes, then a local Ollama,
    then nothing. "Nothing" is a supported outcome — the vault, retrieval, the duplicate
    sweep and undo all work without a model — so the demo starts either way and says
    which case it is in.
    """
    from palimpsest.config import LOCAL_RUNTIMES, describe_setup
    from palimpsest.llm import available

    if available(settings):
        setup = describe_setup(settings)
        return settings, f"{setup.provider}/{setup.model}" if setup else "a model"

    base_url, model, embed = LOCAL_RUNTIMES["ollama"]
    if _ollama_has(base_url, model):
        return _replace(settings, model_provider="ollama", model_base_url=base_url,
                        model=model, embed_provider="ollama", embed_model=embed,
                        embed_base_url=base_url), f"local Ollama ({model})"
    return settings, ""


def _ollama_has(base_url: str, model: str) -> bool:
    """Whether a local Ollama is up and has the model the demo would ask for."""
    import json
    import urllib.error
    import urllib.request

    tags = base_url.rsplit("/v1", 1)[0] + "/api/tags"
    try:
        with urllib.request.urlopen(tags, timeout=2) as response:
            names = {m.get("name", "") for m in json.load(response).get("models", [])}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    return any(name == model or name.startswith(model.split(":")[0]) for name in names)


def default_home() -> Path:
    """Where a demo lives when the caller does not say.

    Beside the user's config rather than in the system temp directory, so the session
    survives a reboot and can be found again — and deleted deliberately.
    """
    from palimpsest.config import config_path

    return Path(os.environ.get("PALIMPSEST_DEMO_HOME") or
                (Path(config_path()).parent / "demo-vault"))
