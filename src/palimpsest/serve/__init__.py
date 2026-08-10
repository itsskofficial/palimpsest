"""The local review app: drop something in, read the diff, accept or reject.

    palimpsest serve

Local by design — binds `127.0.0.1`, no authentication unless you set a key. It is a
control surface for your own notes, not a multi-tenant service, and `config.validate()`
refuses to bind publicly without `PALIMPSEST_API_KEY`.
"""

__all__ = ["AppState", "create_app", "run"]


def __getattr__(name: str):
    """`create_app` and `AppState` are lazy so fastapi stays an optional extra."""
    if name in ("create_app", "AppState"):
        import importlib

        return getattr(importlib.import_module("palimpsest.serve.app"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run(host: str | None = None, port: int | None = None, db: str | None = None,
        reload: bool = False) -> None:
    """Start the server. Called by `palimpsest serve`."""
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover - optional extra
        raise ImportError("pip install 'palimpsest[serve]'") from e

    from palimpsest.config import Settings
    from palimpsest.serve.app import AppState, create_app
    from palimpsest.serve.middleware import configure_logging

    settings = Settings.load(host=host, port=port, database_url=db)
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
