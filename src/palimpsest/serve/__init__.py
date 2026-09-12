"""The local review app: drop something in, read the diff, accept or reject.

    palimpsest serve

Local by design — binds `127.0.0.1`, no authentication unless you set a key. It is a
control surface for your own notes, not a multi-tenant service, and `config.validate()`
refuses to bind publicly without `PALIMPSEST_API_KEY`.
"""

from typing import Any

__all__ = ["AppState", "create_app", "run"]


def __getattr__(name: str):
    """`create_app` and `AppState` are lazy so fastapi stays an optional extra."""
    if name in ("create_app", "AppState"):
        import importlib

        return getattr(importlib.import_module("palimpsest.serve.app"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run(host: str | None = None, port: int | None = None, db: str | None = None,
        reload: bool = False, settings: Any = None) -> None:
    """Start the server. Called by `palimpsest serve`.

    `settings` lets a caller hand over a fully-built configuration instead of having one
    read from the environment — which is how `palimpsest demo` points the same server at
    a sample vault without writing the demo's choices into anybody's config file.
    """
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover - optional extra
        # Naming the distribution matters: the package is `palimpsest-notion`, and
        # `pip install palimpsest[serve]` installs somebody else's project.
        raise ImportError(
            "the app needs a web server, which is an optional extra:\n"
            '  pip install "palimpsest-notion[serve]"\n\n'
            "The offline core has no dependencies on purpose — the mirror, retrieval, "
            "the sweeps and undo all work without any — so the server is not installed "
            "unless you ask for it."
        ) from e

    from palimpsest.config import Settings
    from palimpsest.serve.app import AppState, create_app
    from palimpsest.serve.middleware import configure_logging

    if settings is None:
        settings = Settings.load(host=host, port=port, database_url=db)
    elif host or port:
        import dataclasses

        settings = dataclasses.replace(
            settings, host=host or settings.host, port=port or settings.port)
    configure_logging(settings.log_level, settings.log_json)
    state = AppState(settings=settings)

    stats = state.store.stats()
    print(f"\n  palimpsest  ->  http://{settings.host}:{settings.port}")
    print(f"  api docs    ->  http://{settings.host}:{settings.port}/docs")
    print(f"  mirror: {stats.get('pages', 0)} page(s), {stats.get('blocks', 0)} block(s)")
    mode = "APPLY ON" if settings.apply else "propose-only (nothing is written)"
    print(f"  mode:   {mode}, autonomy={settings.autonomy}")
    for problem in settings.problems():
        print(f"  !       {problem}")
    print()

    uvicorn.run(create_app(state), host=settings.host, port=settings.port,
                log_level="warning", reload=reload)
