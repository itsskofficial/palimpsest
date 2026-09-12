"""Every setting, resolved once from the environment, printable with secrets redacted.

Two rules the validation enforces, because each is a way this becomes unsafe:

1. **Nothing writes to Notion unless `PALIMPSEST_APPLY` says so.** The default posture
   is propose-only. A tool that edits your notes the first time you run it, before you
   have seen what it wants to do, has spent its one chance at your trust.
2. **Binding to anything other than localhost requires an API key.** The review UI has
   no auth by design when it is local. The moment it listens on `0.0.0.0` that is a
   hole, and the app refuses rather than discovering it later.

Autonomy is a *ladder*, not a switch: `PALIMPSEST_AUTONOMY` names the highest risk tier
that may apply without review (`none` → `low` → `medium` → `full` → `everything`).
`full` stops short of contradictions. `everything` adds them, and what it applies is a
record of the disagreement rather than a resolution of it — the system never decides
which of two claims is true, at any setting.

The model is configuration too. `PALIMPSEST_MODEL_BASE_URL` points at anything that
speaks the OpenAI API; otherwise the provider is inferred from whichever key is present.
`model_env` is how those values reach `palimpsest.llm` without it reading `os.environ`
behind an explicit `Settings`'s back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "EMBED_PROVIDERS",
    "LOCAL_RUNTIMES",
    "MODEL_PROVIDERS",
    "ModelSetup",
    "Settings",
    "autonomy_warnings",
    "confidence_bar",
    "config_path",
    "describe_embedding",
    "describe_setup",
    "load",
    "load_env_file",
    "model_getter",
    "model_quality",
    "redact",
]


def config_path() -> Path:
    """Where the persisted config lives, so `serve` finds it wherever it is run from.

    A fixed per-user location, not the working directory — a service started by systemd
    or a container has no meaningful cwd, and a config that only loads when you happen to
    launch from the right folder is a config that mysteriously stops working. Honours
    `PALIMPSEST_CONFIG` for an explicit override, then the XDG / APPDATA convention.
    """
    override = os.environ.get("PALIMPSEST_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "palimpsest" / "config.env"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "palimpsest" / "config.env"


def load_env_file(path: Path | None = None) -> int:
    """Populate `os.environ` from the persisted config file. Returns how many keys set.

    A real environment variable always wins over the file — so a container passing
    `NOTION_TOKEN` in its own environment is never shadowed by a stale saved value. Also
    reads a `.env` in the current directory, which is the convenient thing during
    development. Never raises: a missing or malformed file just means nothing is loaded.
    """
    # An explicit config location (the arg, or PALIMPSEST_CONFIG) means "use exactly
    # this" — the ambient `./.env` is a dev convenience only for when nothing was named.
    # Reading both would let a stray `.env` in the working directory shadow a config the
    # operator pointed at on purpose.
    if path is not None:
        candidates = [path]
    elif os.environ.get("PALIMPSEST_CONFIG"):
        candidates = [config_path()]
    else:
        candidates = [config_path(), Path(".env")]

    loaded = 0
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:   # real env wins
                    os.environ[key] = value
                    loaded += 1
        except OSError:
            continue
    return loaded

_SECRET_HINTS = ("key", "secret", "password", "token", "dsn", "url", "credential")

#: The risk tiers a relation can carry. `high` is contradictions, and only
#: contradictions — see `types.Relation.risk`.
RISK_TIERS = frozenset({"low", "medium", "high"})

#: The tier that no level below `everything` may admit.
#:
#: A knowledge base that silently replaces a true claim with a false one is worse than no
#: automation at all, because you stop knowing which parts to trust. Every level up to
#: and including `full` therefore refuses this tier, and the levels are derived from this
#: constant rather than each remembering the rule for itself.
NEVER_AUTOMATIC = frozenset({"high"})


#: How sure the classifier has to be, scaled by what being wrong would cost.
#:
#: `PALIMPSEST_MIN_CONFIDENCE` used to be one flat bar for all seven relations,
#: which quietly meant the opposite of what it looked like. A well-calibrated model
#: reports lower confidence for "this needs a page of its own" than for "this agrees
#: with line 12", because the first is a judgement about a whole knowledge base and
#: the second is a comparison of two sentences. So the flat bar rejected the *safest*
#: operation -- one additive block, undone in a click -- most often, while letting a
#: strike-through through on the same number.
#:
#: These are multipliers on the setting, so one knob still moves the whole scale, and
#: raising it still tightens everything. At the default 0.75 the bars are 0.60 for a
#: purely additive edit, 0.75 for one that rewrites a sentence, and 0.90 for one that
#: changes what an existing sentence means.
CONFIDENCE_BY_RISK: dict[str, float] = {"low": 0.8, "medium": 1.0, "high": 1.2}


def confidence_bar(risk: str, base: float) -> float:
    """The bar an operation at this risk tier must clear."""
    return min(1.0, base * CONFIDENCE_BY_RISK.get(risk, 1.0))

#: Which risk tiers may be applied without a human, per level.
#:
#: `full` is defined by subtraction: "everything that is allowed to be automatic", so a
#: new tier would be included here and excluded from `NEVER_AUTOMATIC` deliberately
#: rather than by forgetting.
#:
#: `everything` is the operator saying, explicitly, that they would rather see a
#: contradiction recorded in the page and undo it than be asked about it. It is not the
#: same as the system deciding which of two claims is true — it never does that, at any
#: setting. What it applies is a *record of the disagreement*: both sides, both sources,
#: side by side, in one reversible operation. See `plan._contradiction_ops`.
#: The backends a knowledge base can live in. Both implement the same seventeen methods
#: and both go through the same write door, so nothing above `workspace` knows which is
#: in use -- see `palimpsest.workspace`.
BACKENDS = ("notion", "markdown")

AUTONOMY_LEVELS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "low": frozenset({"low"}),
    "medium": frozenset({"low", "medium"}),
    "full": RISK_TIERS - NEVER_AUTOMATIC,
    "everything": RISK_TIERS,
}


#: Hosts that serve the OpenAI API, and a sensible model for each. Order is the order
#: they are tried when nothing is set explicitly.
#:
#: This table lives in `config` rather than in `palimpsest.llm` for a structural reason:
#: the store layer reads settings, and if `config` imported the model layer then `store`
#: would transitively depend on it, which the layering contract forbids and which would
#: mean the offline core could not be loaded without the model code. Resolution *policy*
#: is configuration; the *clients* are the model layer's business.
MODEL_PROVIDERS: dict[str, tuple[str, str, str]] = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-5.1"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
                   "anthropic/claude-sonnet-5"),
    "together": ("TOGETHER_API_KEY", "https://api.together.xyz/v1",
                 "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-large-latest"),
    "xai": ("XAI_API_KEY", "https://api.x.ai/v1", "grok-4"),
}


#: Runtimes that serve the OpenAI API from your own machine, needing no key at all.
#:
#: Kept apart from `MODEL_PROVIDERS` because those are *discovered* by finding a key in
#: the environment, and a keyless provider cannot be discovered that way. Pointing at
#: localhost on a hunch would also be a surprising thing to do silently, so these are
#: reached only by naming one: `PALIMPSEST_MODEL_PROVIDER=ollama`.
#:
#: The tuple is (base URL, default chat model, default embedding model). Only Ollama gets
#: defaults, because it is the one with a fixed model naming scheme and a registry; the
#: others serve whatever you happened to load, so they require a model name.
LOCAL_RUNTIMES: dict[str, tuple[str, str, str]] = {
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:7b", "mxbai-embed-large"),
    "lmstudio": ("http://127.0.0.1:1234/v1", "", ""),
    "llamacpp": ("http://127.0.0.1:8080/v1", "", ""),
    "vllm": ("http://127.0.0.1:8000/v1", "", ""),
}


#: Hosts that serve OpenAI-shaped embeddings, and a sensible model for each. Groq is
#: deliberately absent: it serves no embeddings endpoint, so someone whose only key is a
#: Groq one gets lexical retrieval and is told why rather than a string of 404s.
EMBED_PROVIDERS: dict[str, tuple[str, str, str]] = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "text-embedding-3-small"),
    "together": ("TOGETHER_API_KEY", "https://api.together.xyz/v1",
                 "BAAI/bge-large-en-v1.5"),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-embed"),
}


@dataclass(frozen=True)
class ModelSetup:
    """Which provider a configuration resolves to, and why. Never makes a call."""

    provider: str
    model: str
    base_url: str | None
    reason: str


def model_getter(settings: Any):
    """Where model configuration is read from: a `Settings` if given, else the process.

    A `Settings` is *authoritative*, not merely preferred. It is built from the
    environment in the first place, so an explicitly constructed one — in a test, in an
    eval scoring two models against the same golden set, in a worker pinned to a cheaper
    provider — means "these values, and no others". Falling back to `os.environ` for a
    field it deliberately left empty is how a process ends up using a key nobody asked it
    to use, and how a suite ends up passing only on the machine whose shell exports the
    right variable.
    """
    if settings is None:
        return lambda key: os.environ.get(key) or ""
    overrides = getattr(settings, "model_env", None) or {}
    return lambda key: str(overrides.get(key) or "")


def describe_embedding(settings=None):
    """Which embedding endpoint this configuration names, or `None` for lexical only.

    Here rather than in `palimpsest.embed` for the same reason as `describe_setup`: the
    store layer reads settings, so `config` must not import anything above it. `status`
    also wants to print this line without building a client or making a call.
    """
    get = model_getter(settings)
    extra = {
        "PALIMPSEST_EMBED_MODEL": getattr(settings, "embed_model", "") or "",
        "PALIMPSEST_EMBED_BASE_URL": getattr(settings, "embed_base_url", "") or "",
        "PALIMPSEST_EMBED_API_KEY": getattr(settings, "embed_api_key", "") or "",
        "PALIMPSEST_EMBED_PROVIDER": getattr(settings, "embed_provider", "") or "",
    }

    def value(key: str) -> str:
        if settings is None:
            return os.environ.get(key) or ""
        return get(key) or extra.get(key, "")

    model = value("PALIMPSEST_EMBED_MODEL")
    base = value("PALIMPSEST_EMBED_BASE_URL")

    named = (value("PALIMPSEST_EMBED_PROVIDER")
             or value("PALIMPSEST_MODEL_PROVIDER")).strip().lower()
    if not base and named in LOCAL_RUNTIMES:
        # A local runtime already serving the chat model is the obvious place to ask for
        # vectors too, so choosing one for chat opts you into both — and this is the only
        # configuration where embeddings cost nothing, which is the whole reason to make
        # it easy to fall into.
        url, _, default_embed = LOCAL_RUNTIMES[named]
        chosen = model if model != "text-embedding-3-small" else default_embed
        return ModelSetup(named, chosen, url, f"local runtime {named}") if chosen else None

    if base:
        if not model:
            return None
        return ModelSetup("openai-compatible", model, base,
                          "PALIMPSEST_EMBED_BASE_URL is set")
    for name, (env, url, default) in EMBED_PROVIDERS.items():
        if value(env):
            return ModelSetup(name, model or default, url, f"{env} is set")
    return None


def describe_setup(settings: Any = None) -> ModelSetup | None:
    """What provider this configuration names, or `None` if it names none.

    `None` is a supported state, not an error: the mirror, retrieval, the duplicate sweep
    and undo all work with no model at all.
    """
    get = model_getter(settings)

    explicit = get("PALIMPSEST_MODEL_BASE_URL")
    if explicit:
        return ModelSetup("openai-compatible", get("PALIMPSEST_MODEL") or "", explicit,
                          "PALIMPSEST_MODEL_BASE_URL is set")

    named = (get("PALIMPSEST_MODEL_PROVIDER") or "").strip().lower()
    if named in LOCAL_RUNTIMES:
        url, default, _ = LOCAL_RUNTIMES[named]
        model = get("PALIMPSEST_MODEL") or default
        if not model:
            return None
        return ModelSetup(named, model, url, f"PALIMPSEST_MODEL_PROVIDER={named}")
    if named == "anthropic":
        return ModelSetup("anthropic", get("PALIMPSEST_MODEL") or "claude-opus-5", None,
                          "PALIMPSEST_MODEL_PROVIDER=anthropic")
    if named in MODEL_PROVIDERS:
        _, url, default = MODEL_PROVIDERS[named]
        return ModelSetup(named, get("PALIMPSEST_MODEL") or default, url,
                          f"PALIMPSEST_MODEL_PROVIDER={named}")

    if get("ANTHROPIC_API_KEY"):
        return ModelSetup("anthropic", get("PALIMPSEST_MODEL") or "claude-opus-5", None,
                          "ANTHROPIC_API_KEY is set")
    for name, (env, url, default) in MODEL_PROVIDERS.items():
        if get(env):
            return ModelSetup(name, get("PALIMPSEST_MODEL") or default, url,
                              f"{env} is set")
    return None


#: Autonomy levels that write something without a person reading it first. Below these,
#: an unmeasured model costs you a review; at or above them it costs you an edit.
WRITING_LEVELS = frozenset({"low", "medium", "full", "everything"})


def model_quality(store, settings) -> dict | None:
    """The last component-eval result for the model this configuration would use.

    Returns None when the model has never been measured, which is a different answer
    from "measured and bad" and is reported differently. Never raises: a store that
    cannot answer must not stop `status` from printing.
    """
    setup = describe_setup(settings)
    if setup is None:
        return None
    name = f"{setup.provider}/{setup.model}"
    try:
        return store.last_eval_run("component", name)
    except Exception:  # pragma: no cover - a status line is not worth an exception
        return None


def autonomy_warnings(store, settings) -> list[str]:
    """Problems that only exist because of *which model* is doing the deciding.

    The provider-invariance work made every model runnable. That is the feature, and it
    is also the risk: a 7B model on a laptop and Claude Opus are the same three lines of
    configuration apart, and they classify very differently. Measured on the committed
    fixture, the best local 8B model scores 0.65 weighted F1 with contradiction recall
    0.67, against 0.95 and 1.00 for a frontier model — so "it can write to my notes on
    its own" means something quite different depending on an answer nobody is prompted
    for.

    So the warning is not "local models are bad". It is: you have given write access to
    a model whose accuracy on *your* fixture is unknown or known to be below the bar, and
    here is the one command that answers it.
    """
    out: list[str] = []
    if not settings.apply or settings.autonomy not in WRITING_LEVELS:
        return out

    setup = describe_setup(settings)
    if setup is None:
        return out

    run = model_quality(store, settings)
    name = f"{setup.provider}/{setup.model}"
    if run is None:
        out.append(
            f"{name} has write access but has never been measured — run "
            f"`palimpsest eval component` to see how it classifies before trusting it")
        return out

    if not run.get("passed"):
        scores = run.get("scores") or {}
        headline = scores.get("weighted_f1")
        recall = scores.get("contradiction_recall")
        detail = []
        if headline is not None:
            detail.append(f"weighted F1 {headline:.2f}")
        if recall is not None:
            detail.append(f"contradiction recall {recall:.2f}")
        out.append(
            f"{name} has write access but failed the classifier eval"
            + (f" ({', '.join(detail)})" if detail else "")
            + " — consider a stronger model, or a lower autonomy level")
    return out


def redact(value: Any, key: str = "") -> Any:
    """Blank anything that looks like a credential, keeping enough to identify it."""
    if value is None or not isinstance(value, str) or not value:
        return value
    if not any(h in key.lower() for h in _SECRET_HINTS):
        return value
    if value.startswith(("sqlite:", "file:")):
        return value
    if "://" in value:
        try:
            parsed = urlparse(value)
            host = parsed.hostname or "?"
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://***@{host}{port}{parsed.path}"
        except ValueError:
            return "***"
    return value[:4] + "…" + value[-2:] if len(value) > 10 else "***"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _chat_ids(raw: str) -> tuple[int, ...]:
    """Parse `TELEGRAM_ALLOWED_CHATS`, ignoring anything that is not an id.

    Chat ids are negative for groups, so the minus sign is meaningful and must survive.
    """
    out: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise ValueError(
                f"TELEGRAM_ALLOWED_CHATS contains {part!r}, which is not a chat id. "
                "Message the bot once and it will reply with yours."
            ) from None
    return tuple(out)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


@dataclass
class Settings:
    """Resolved configuration for a palimpsest process."""

    # -- storage ---------------------------------------------------------------
    database_url: str = "sqlite:///palimpsest.db"
    artifact_url: str = "file://./archive"

    # -- the workspace ---------------------------------------------------------
    #: Which backend holds the knowledge base. `notion` is the product; `markdown` is a
    #: folder of `.md` files -- an Obsidian vault, a git repo, the demo. The choice is
    #: one line here because there is exactly one write door, so a second backend is a
    #: second implementation of seventeen methods rather than a fork of the pipeline.
    backend: str = "notion"
    #: Where the markdown vault lives. Ignored unless `backend` is `markdown`.
    vault_path: str | None = None

    # -- notion ----------------------------------------------------------------
    notion_token: str | None = None
    #: Pinned deliberately. Notion's API is versioned by date and the 2025-09-03
    #: release split databases into data sources; letting this float would change
    #: response shapes under a running deployment.
    notion_version: str = "2026-03-11"
    notion_root_pages: tuple[str, ...] = ()

    # -- the model -------------------------------------------------------------
    #: Empty means "whatever the provider's default is", which is how someone with only
    #: a Groq key avoids being handed a Claude model id they cannot call.
    model: str = ""
    #: Force a provider. Empty means resolve from whichever key is present.
    model_provider: str | None = None
    #: Any OpenAI-compatible endpoint, including a local one. Setting this wins over
    #: every key-sniffing rule, because it is the only unambiguous statement of intent.
    model_base_url: str | None = None
    #: A key for that endpoint, when it is not one of the hosts we know by name.
    model_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    #: Lay out a newly created page with the model instead of stacking bullets. On
    #: by default: the bullet stack is a fallback, not a preference.
    compose_pages: bool = True
    #: How many claims are classified at once. Lower it for a provider with a tight
    #: rate limit; 1 restores the old sequential behaviour.
    classify_workers: int = 8
    extract_effort: str = "medium"
    classify_effort: str = "high"
    max_tokens: int = 16_000

    # -- ingestion -------------------------------------------------------------
    firecrawl_api_key: str | None = None
    openai_api_key: str | None = None
    #: Vectors are optional. Left unset, retrieval is BM25 — good, keyless, and the
    #: floor the product is designed around rather than a degraded mode.
    embed_model: str = "text-embedding-3-small"
    embed_base_url: str | None = None
    embed_api_key: str | None = None
    embed_provider: str | None = None
    #: Speech to text. Whichever key is set is used, in this order, unless
    #: `PALIMPSEST_TRANSCRIBE` names one. There is no offline fallback on purpose: a
    #: transcript you did not get is not a source.
    deepgram_api_key: str | None = None
    groq_api_key: str | None = None
    sarvam_api_key: str | None = None
    transcribe_provider: str | None = None

    # -- behaviour -------------------------------------------------------------
    apply: bool = False
    autonomy: str = "none"
    #: Mirror the ledger into two Notion databases, so "why does this say that" is
    #: answerable from Notion rather than only from SQLite.
    journal: bool = True
    min_confidence: float = 0.75
    max_candidates: int = 8
    footnotes: bool = True

    # -- the bot ---------------------------------------------------------------
    telegram_token: str | None = None
    #: Chat ids allowed to talk to the bot. A bot token is a bearer credential — anyone
    #: who finds the bot can message it — so an empty allowlist refuses everyone and
    #: tells each caller its own id, which is the pairing flow.
    telegram_allowed_chats: tuple[int, ...] = ()

    # -- the server ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8100
    api_key: str | None = None
    allow_insecure: bool = False
    cors_origins: tuple[str, ...] = ()
    #: A browser extension's origin is `chrome-extension://<id>`, and the id is not
    #: known until the extension is loaded — so it cannot be listed ahead of time and
    #: has to be matched. Defaulted only for a local-only bind, where the origin is
    #: already reachable by anything on the machine; a public bind must say so
    #: explicitly rather than inherit a permissive default it did not choose.
    cors_origin_regex: str | None = None
    #: Worker threads draining the capture queue. Two is enough for a personal
    #: workspace: ingestion is dominated by waiting on the model, not by local CPU.
    workers: int = 2
    log_json: bool = False
    log_level: str = "info"
    metrics: bool = True

    # -- provenance ------------------------------------------------------------
    environment: str = "local"
    release: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, **overrides) -> Settings:
        # Persisted config fills any gap the real environment left, so a machine that
        # ran the setup wizard once is configured every time thereafter.
        load_env_file()
        origins = os.environ.get("PALIMPSEST_CORS_ORIGINS", "")
        roots = os.environ.get("PALIMPSEST_NOTION_ROOTS", "")
        settings = cls(
            database_url=os.environ.get("PALIMPSEST_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or "sqlite:///palimpsest.db",
            artifact_url=os.environ.get("PALIMPSEST_ARTIFACT_URL", "file://./archive"),
            backend=os.environ.get("PALIMPSEST_BACKEND", "notion").strip().lower(),
            vault_path=os.environ.get("PALIMPSEST_VAULT") or None,
            notion_token=os.environ.get("NOTION_TOKEN") or None,
            notion_version=os.environ.get("NOTION_VERSION", "2026-03-11"),
            notion_root_pages=tuple(r.strip() for r in roots.split(",") if r.strip()),
            model=os.environ.get("PALIMPSEST_MODEL", ""),
            model_provider=os.environ.get("PALIMPSEST_MODEL_PROVIDER") or None,
            model_base_url=os.environ.get("PALIMPSEST_MODEL_BASE_URL") or None,
            model_api_key=os.environ.get("PALIMPSEST_MODEL_API_KEY") or None,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            compose_pages=_bool("PALIMPSEST_COMPOSE_PAGES", True),
            classify_workers=_int("PALIMPSEST_CLASSIFY_WORKERS", 8),
            extract_effort=os.environ.get("PALIMPSEST_EXTRACT_EFFORT", "medium"),
            classify_effort=os.environ.get("PALIMPSEST_CLASSIFY_EFFORT", "high"),
            max_tokens=_int("PALIMPSEST_MAX_TOKENS", 16_000),
            firecrawl_api_key=os.environ.get("FIRECRAWL_API_KEY") or None,
            openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
            embed_model=os.environ.get("PALIMPSEST_EMBED_MODEL", "text-embedding-3-small"),
            embed_base_url=os.environ.get("PALIMPSEST_EMBED_BASE_URL") or None,
            embed_provider=os.environ.get("PALIMPSEST_EMBED_PROVIDER") or None,
            embed_api_key=os.environ.get("PALIMPSEST_EMBED_API_KEY") or None,
            deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY") or None,
            groq_api_key=os.environ.get("GROQ_API_KEY") or None,
            sarvam_api_key=os.environ.get("SARVAM_API_KEY") or None,
            transcribe_provider=os.environ.get("PALIMPSEST_TRANSCRIBE") or None,
            apply=_bool("PALIMPSEST_APPLY", False),
            autonomy=os.environ.get("PALIMPSEST_AUTONOMY", "none").lower(),
            journal=_bool("PALIMPSEST_JOURNAL", True),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_allowed_chats=_chat_ids(
                os.environ.get("TELEGRAM_ALLOWED_CHATS", "")),
            min_confidence=_float("PALIMPSEST_MIN_CONFIDENCE", 0.75),
            max_candidates=_int("PALIMPSEST_MAX_CANDIDATES", 8),
            footnotes=_bool("PALIMPSEST_FOOTNOTES", True),
            host=os.environ.get("PALIMPSEST_HOST", "127.0.0.1"),
            port=_int("PALIMPSEST_PORT", 8100),
            api_key=os.environ.get("PALIMPSEST_API_KEY") or None,
            allow_insecure=_bool("PALIMPSEST_ALLOW_INSECURE"),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            cors_origin_regex=os.environ.get("PALIMPSEST_CORS_ORIGIN_REGEX") or None,
            workers=_int("PALIMPSEST_WORKERS", 2),
            log_json=_bool("PALIMPSEST_LOG_JSON"),
            log_level=os.environ.get("PALIMPSEST_LOG_LEVEL", "info").lower(),
            metrics=_bool("PALIMPSEST_METRICS", True),
            environment=os.environ.get("PALIMPSEST_ENV", "local"),
            release=os.environ.get("PALIMPSEST_RELEASE") or None,
        )
        if overrides:
            settings = replace(settings, **{k: v for k, v in overrides.items() if v is not None})
        settings.validate()
        return settings

    # -- derived ---------------------------------------------------------------

    @property
    def is_local_only(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    @property
    def uses_postgres(self) -> bool:
        return self.database_url.startswith(("postgres://", "postgresql://"))

    @property
    def is_supabase(self) -> bool:
        return "supabase" in self.database_url

    @property
    def uses_pooler(self) -> bool:
        """Supabase's transaction pooler is port 6543 — session state is gone there."""
        if not self.uses_postgres:
            return False
        return urlparse(self.database_url).port == 6543

    @property
    def model_env(self) -> dict[str, str]:
        """The model layer's view of this configuration, by environment-variable name.

        `palimpsest.llm` resolves a provider from a handful of variables. Handing it this
        dict rather than letting it read `os.environ` directly is what makes an explicit
        `Settings(...)` — in a test, in an eval, in a second worker with a different
        model — actually take effect instead of being silently overruled by the ambient
        process environment.
        """
        pairs = {
            "PALIMPSEST_MODEL": self.model,
            "PALIMPSEST_MODEL_PROVIDER": self.model_provider,
            "PALIMPSEST_EMBED_PROVIDER": self.embed_provider,
            "PALIMPSEST_MODEL_BASE_URL": self.model_base_url,
            "PALIMPSEST_MODEL_API_KEY": self.model_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
            "GROQ_API_KEY": self.groq_api_key,
            "OPENROUTER_API_KEY": self.openrouter_api_key,
        }
        return {k: v for k, v in pairs.items() if v}

    @property
    def model_setup(self) -> ModelSetup | None:
        """Which provider this configuration resolves to, or `None`. Never calls out."""
        return describe_setup(self)

    @property
    def has_model(self) -> bool:
        """Whether this configuration names a model provider, from any vendor.

        Says nothing about whether the Anthropic SDK is installed — `llm.available()`
        answers that. Someone who set a key but skipped the extra should be told to
        install the extra, not told they have no key.
        """
        return describe_setup(self) is not None

    @property
    def has_notion(self) -> bool:
        """Whether Notion specifically is configured. For display and onboarding."""
        return bool(self.notion_token)

    @property
    def has_workspace(self) -> bool:
        """Whether there is a knowledge base to read and write at all.

        Distinct from `has_notion`, and the distinction matters: every site that gates a
        *write* asks this one, so pointing the app at a markdown vault does not silently
        disable applying. `has_notion` stays for the places that genuinely mean Notion —
        the setup wizard, the status line, the agent's description of its own tools.
        """
        if self.backend == "markdown":
            return bool(self.vault_path)
        return bool(self.notion_token)

    @property
    def transcriber(self) -> str | None:
        """Which speech-to-text provider a recording would go to, if any.

        Order is deliberate: Deepgram handles long files and labels speakers, Groq is
        the cheapest start but caps at 25 MB, Sarvam is the one that copes with
        Hinglish. An explicit `PALIMPSEST_TRANSCRIBE` overrides all of it.
        """
        available = {"deepgram": self.deepgram_api_key, "groq": self.groq_api_key,
                     "sarvam": self.sarvam_api_key}
        if self.transcribe_provider:
            chosen = self.transcribe_provider.lower()
            return chosen if available.get(chosen) else None
        return next((name for name, key in available.items() if key), None)

    def may_auto_apply(self, risk: str) -> bool:
        """Whether a relation of this risk tier may be applied without a human.

        Note that this consults `apply` as well: even at `autonomy=medium`, a process
        started without `PALIMPSEST_APPLY=1` writes nothing. Two independent switches,
        because the failure they prevent is unrecoverable.
        """
        if not self.apply:
            return False
        return risk in AUTONOMY_LEVELS.get(self.autonomy, set())

    # -- validation ------------------------------------------------------------

    def validate(self) -> Settings:
        if self.log_level not in ("critical", "error", "warning", "info", "debug", "trace"):
            raise ValueError(f"PALIMPSEST_LOG_LEVEL={self.log_level!r} is not a log level")
        if self.backend not in BACKENDS:
            raise ValueError(
                f"PALIMPSEST_BACKEND={self.backend!r} is not a backend. Use one of: "
                f"{', '.join(sorted(BACKENDS))}."
            )
        if self.backend == "markdown" and not self.vault_path:
            raise ValueError(
                "PALIMPSEST_BACKEND=markdown needs PALIMPSEST_VAULT to say which folder "
                "holds the vault."
            )
        if self.autonomy not in AUTONOMY_LEVELS:
            raise ValueError(
                f"PALIMPSEST_AUTONOMY={self.autonomy!r} is not valid. Use one of: "
                f"{', '.join(sorted(AUTONOMY_LEVELS))}.\n"
                "'full' stops before contradictions; 'everything' records them in "
                "place, with both sides, rather than resolving them."
            )
        if not self.database_url.startswith(("sqlite:", "postgres://", "postgresql://")):
            raise ValueError(
                f"PALIMPSEST_DATABASE_URL={redact(self.database_url, 'url')} is not a "
                "store URL; use sqlite:///path.db or postgresql://..."
            )
        if not self.artifact_url.startswith(("file://", "s3://", "supabase://")):
            raise ValueError(
                f"PALIMPSEST_ARTIFACT_URL={self.artifact_url!r} must start with "
                "file://, s3:// or supabase://"
            )
        if self.extract_effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError(f"PALIMPSEST_EXTRACT_EFFORT={self.extract_effort!r} is not an effort")
        if self.classify_effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError(f"PALIMPSEST_CLASSIFY_EFFORT={self.classify_effort!r} is not an effort")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("PALIMPSEST_MIN_CONFIDENCE must be between 0 and 1")
        if not self.is_local_only and not self.api_key and not self.allow_insecure:
            raise ValueError(
                f"refusing to bind {self.host} with no API key.\n"
                "The review UI is unauthenticated by design when it is local. Once it "
                "listens on a public interface that is a hole — it can read and edit "
                "your notes. So either:\n"
                "  set PALIMPSEST_API_KEY=<a long random string>   (recommended)\n"
                "  or set PALIMPSEST_ALLOW_INSECURE=1              (you have a reason)"
            )
        if self.api_key and len(self.api_key) < 16:
            raise ValueError(
                "PALIMPSEST_API_KEY is shorter than 16 characters. Generate one with "
                "`python -c \"import secrets;print(secrets.token_urlsafe(32))\"`."
            )
        return self

    # -- rendering -------------------------------------------------------------

    def as_dict(self, reveal: bool = False) -> dict:
        return {
            "environment": self.environment,
            "release": self.release,
            "database": self.database_url if reveal else redact(self.database_url, "url"),
            "database_kind": "postgres" if self.uses_postgres else "sqlite",
            "supabase": self.is_supabase,
            "pooler": self.uses_pooler,
            "archive": self.artifact_url if reveal else redact(self.artifact_url, "url"),
            "notion": "configured" if self.has_notion else "MISSING",
            "notion_version": self.notion_version,
            "notion_roots": list(self.notion_root_pages) or ["(whole workspace)"],
            "model": self._model_line(),
            "effort": {"extract": self.extract_effort, "classify": self.classify_effort},
            "firecrawl": "configured" if self.firecrawl_api_key else "off (stdlib fallback)",
            "transcribe": self.transcriber or "MISSING (audio cannot be ingested)",
            "telegram": (f"paired with {len(self.telegram_allowed_chats)} chat(s)"
                         if self.telegram_token else "off"),
            "journal": "on (Notion databases)" if self.journal else "off (SQLite only)",
            "embeddings": self._embed_line(),
            "apply": self.apply,
            "autonomy": self.autonomy,
            "min_confidence": self.min_confidence,
            "host": self.host,
            "port": self.port,
            "auth": "api-key" if self.api_key
            else ("insecure" if not self.is_local_only else "local-only"),
            "cors_origins": list(self.cors_origins),
        }

    def summary(self) -> str:  # pragma: no cover - display only
        d = self.as_dict()
        width = max(len(k) for k in d)
        return "\n".join(f"  {k:<{width}}  {v}" for k, v in d.items())

    def _model_line(self) -> str:
        """Which provider and model a call would actually use.

        `model` is empty by default now that each provider has its own — printing the
        raw field said nothing, and said it in a place people look precisely when
        something is not behaving as they expected.
        """
        setup = self.model_setup
        if setup is None:
            return "none configured (the mirror and the sweeps still work)"
        where = f" @ {setup.base_url}" if setup.base_url else ""
        return f"{setup.provider}/{setup.model}{where}"

    def _embed_line(self) -> str:
        setup = describe_embedding(self)
        if setup is None:
            return "lexical only (BM25; no embedding provider configured)"
        return f"{setup.provider}/{setup.model}"

    def problems(self) -> list[str]:
        """Deployment mistakes that are legal but probably wrong."""
        out: list[str] = []
        # These used to name Notion unconditionally, which on a markdown vault
        # produced "nothing can be mirrored or applied" immediately after it had
        # mirrored eight pages. A warning that is visibly false is worse than no
        # warning: it teaches the reader to skip the ones that are true.
        if self.backend == "markdown":
            if not self.vault_path:
                out.append("PALIMPSEST_VAULT is not set — there is nowhere to read "
                           "or write")
        elif not self.has_notion:
            out.append("NOTION_TOKEN is not set — nothing can be mirrored or applied")
        if not self.has_model:
            out.append("no model is configured — extraction and classification are off "
                       "(the mirror and the sweeps still work). Set ANTHROPIC_API_KEY, "
                       "OPENAI_API_KEY or GROQ_API_KEY, or PALIMPSEST_MODEL_BASE_URL "
                       "for any other OpenAI-compatible endpoint")
        if self.uses_postgres and self.uses_pooler:
            out.append("database URL is a transaction pooler (6543): correct for the service, "
                       "but run `palimpsest db migrate --url <direct 5432 URL>` for migrations")
        if self.telegram_token and not self.telegram_allowed_chats:
            out.append("TELEGRAM_BOT_TOKEN is set but TELEGRAM_ALLOWED_CHATS is empty — "
                       "the bot will refuse every chat and reply with its id, which is "
                       "how you pair it")
        if self.journal and not self.notion_root_pages:
            out.append("PALIMPSEST_JOURNAL is on but PALIMPSEST_NOTION_ROOTS is not set — "
                       "there is nowhere to create the Changes and Sources databases, so "
                       "the ledger stays in SQLite only")
        if self.transcribe_provider and not self.transcriber:
            out.append(f"PALIMPSEST_TRANSCRIBE={self.transcribe_provider} but its key is "
                       "not set — recordings will fail rather than fall back")
        if self.apply and self.autonomy != "none":
            tiers = ", ".join(sorted(AUTONOMY_LEVELS[self.autonomy]))
            tail = ("contradictions are recorded beside the line they argue with, never "
                    "resolved" if self.autonomy == "everything"
                    else "contradictions still wait for you")
            where = "your vault" if self.backend == "markdown" else "Notion"
            out.append(f"apply=on and autonomy={self.autonomy}: {tiers}-risk relations "
                       f"are written to {where} without review — {tail}")
        # `demo` is as local as `local`. The warning is about container filesystems
        # on a redeploy, which is not a thing that happens to somebody trying the
        # tool on their laptop -- and a false alarm in the first thirty seconds
        # teaches people to ignore the real ones.
        if (self.artifact_url.startswith("file://")
                and self.environment not in ("local", "demo")):
            out.append(f"archive goes to a local path but PALIMPSEST_ENV={self.environment}; "
                       "a container filesystem does not survive a redeploy — use s3:// or "
                       "supabase://, or your citations stop resolving")
        if not self.is_local_only and not self.api_key:
            out.append("binding non-locally with no PALIMPSEST_API_KEY")
        return out


def load(**overrides) -> Settings:
    return Settings.load(**overrides)
