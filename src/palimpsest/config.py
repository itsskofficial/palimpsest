"""Every setting, resolved once from the environment, printable with secrets redacted.

Two rules the validation enforces, because each is a way this becomes unsafe:

1. **Nothing writes to Notion unless `PALIMPSEST_APPLY` says so.** The default posture
   is propose-only. A tool that edits your notes the first time you run it, before you
   have seen what it wants to do, has spent its one chance at your trust.
2. **Binding to anything other than localhost requires an API key.** The review UI has
   no auth by design when it is local. The moment it listens on `0.0.0.0` that is a
   hole, and the app refuses rather than discovering it later.

Autonomy is a *ladder*, not a switch: `PALIMPSEST_AUTONOMY` names the highest risk
tier that may apply without review (`none` → `low` → `medium`). There is no `high`
value — contradictions are never automatic, at any setting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = ["Settings", "config_path", "load", "load_env_file", "redact"]


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

#: Highest-risk relation tier that may be applied without a human, per level.
AUTONOMY_LEVELS = {"none": set(), "low": {"low"}, "medium": {"low", "medium"}}


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

    # -- notion ----------------------------------------------------------------
    notion_token: str | None = None
    #: Pinned deliberately. Notion's API is versioned by date and the 2025-09-03
    #: release split databases into data sources; letting this float would change
    #: response shapes under a running deployment.
    notion_version: str = "2026-03-11"
    notion_root_pages: tuple[str, ...] = ()

    # -- the model -------------------------------------------------------------
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5"
    extract_effort: str = "medium"
    classify_effort: str = "high"
    max_tokens: int = 16_000

    # -- ingestion -------------------------------------------------------------
    firecrawl_api_key: str | None = None
    openai_api_key: str | None = None
    embed_model: str = "text-embedding-3-small"
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
            notion_token=os.environ.get("NOTION_TOKEN") or None,
            notion_version=os.environ.get("NOTION_VERSION", "2026-03-11"),
            notion_root_pages=tuple(r.strip() for r in roots.split(",") if r.strip()),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            model=os.environ.get("PALIMPSEST_MODEL", "claude-opus-5"),
            extract_effort=os.environ.get("PALIMPSEST_EXTRACT_EFFORT", "medium"),
            classify_effort=os.environ.get("PALIMPSEST_CLASSIFY_EFFORT", "high"),
            max_tokens=_int("PALIMPSEST_MAX_TOKENS", 16_000),
            firecrawl_api_key=os.environ.get("FIRECRAWL_API_KEY") or None,
            openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
            embed_model=os.environ.get("PALIMPSEST_EMBED_MODEL", "text-embedding-3-small"),
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
    def has_model(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_notion(self) -> bool:
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
        if self.autonomy not in AUTONOMY_LEVELS:
            raise ValueError(
                f"PALIMPSEST_AUTONOMY={self.autonomy!r} is not valid. Use one of: "
                f"{', '.join(sorted(AUTONOMY_LEVELS))}.\n"
                "There is deliberately no 'high': contradictions are never applied "
                "automatically, at any setting."
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
            "model": self.model if self.has_model else f"{self.model} (no key)",
            "effort": {"extract": self.extract_effort, "classify": self.classify_effort},
            "firecrawl": "configured" if self.firecrawl_api_key else "off (stdlib fallback)",
            "transcribe": self.transcriber or "MISSING (audio cannot be ingested)",
            "telegram": (f"paired with {len(self.telegram_allowed_chats)} chat(s)"
                         if self.telegram_token else "off"),
            "journal": "on (Notion databases)" if self.journal else "off (SQLite only)",
            "embeddings": "openai" if self.openai_api_key else "lexical (built-in)",
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

    def problems(self) -> list[str]:
        """Deployment mistakes that are legal but probably wrong."""
        out: list[str] = []
        if not self.has_notion:
            out.append("NOTION_TOKEN is not set — nothing can be mirrored or applied")
        if not self.has_model:
            out.append("ANTHROPIC_API_KEY is not set — extraction and classification are off "
                       "(the mirror and the sweeps still work)")
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
            out.append(f"apply=on and autonomy={self.autonomy}: {self.autonomy}-risk relations "
                       "will be written to Notion without review (contradictions never are)")
        if self.artifact_url.startswith("file://") and self.environment != "local":
            out.append(f"archive goes to a local path but PALIMPSEST_ENV={self.environment}; "
                       "a container filesystem does not survive a redeploy — use s3:// or "
                       "supabase://, or your citations stop resolving")
        if not self.is_local_only and not self.api_key:
            out.append("binding non-locally with no PALIMPSEST_API_KEY")
        return out


def load(**overrides) -> Settings:
    return Settings.load(**overrides)
