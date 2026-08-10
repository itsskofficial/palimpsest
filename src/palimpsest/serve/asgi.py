"""ASGI entry point: `uvicorn palimpsest.serve.asgi:app`.

Needed because `uvicorn --workers` and `gunicorn` require an import string rather than
a constructed app object — each worker process imports this module and builds its own.
Configuration comes entirely from the environment, which is what a container gives you.
"""

from __future__ import annotations

from palimpsest.config import Settings
from palimpsest.serve.app import AppState, create_app
from palimpsest.serve.middleware import configure_logging

__all__ = ["app", "settings", "state"]

settings = Settings.load()
configure_logging(settings.log_level, settings.log_json)
state = AppState(settings=settings)
app = create_app(state)
