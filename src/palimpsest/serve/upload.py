"""The file-drop route, in its own module for a boring but real reason.

`serve/app.py` uses `from __future__ import annotations`, which turns every annotation
into a string, and it imports FastAPI *inside* `create_app()` so that importing the
package without the `serve` extra still works. Those two facts combine badly for one
signature: FastAPI resolves `list[UploadFile]` against the function's module globals,
`UploadFile` is not there, and the route fails to build.

So this module skips the future import and lets its annotations be real objects at
runtime — which `requires-python >= 3.10` makes free, since `list[X]` and `X | None`
are both evaluatable there without it. It is imported only from inside `create_app()`,
after FastAPI is known to be installed, which keeps the optional-extra promise intact.
"""

import logging
from pathlib import Path

from fastapi import File, Form, UploadFile

from palimpsest.types import new_id

__all__ = ["register"]

log = logging.getLogger("palimpsest.serve.upload")

#: Refused before anything is read. The middleware caps the whole body too; this is the
#: per-file message, which is the one that tells you which file to leave out.
MAX_FILE_BYTES = 64 * 1024 * 1024


def register(app, st) -> None:
    """Attach `POST /v1/ingest/upload` to the app."""

    @app.post("/v1/ingest/upload", tags=["capture"], status_code=202)
    async def upload(
        files: list[UploadFile] = File(...),
        origin: str = Form(default="upload"),
        title: str | None = Form(default=None),
        url: str | None = Form(default=None),
    ):
        """Accept dropped files and queue one job each.

        The bytes are written to a temp file and the *path* is queued, rather than the
        content riding along in the job row. A 40 MB PDF in a database column is a
        database problem; on disk it is just a file the adapter already knows how to
        open, and the archive step still captures the original on the way through.
        """
        from fastapi import HTTPException

        st.uploads.mkdir(parents=True, exist_ok=True)
        queued = []
        for f in files:
            # `Path(...).name` and nothing else: a client is free to send
            # "../../.ssh/authorized_keys" as a filename, and joining that onto a
            # directory writes wherever it likes.
            name = Path(f.filename or "upload").name or "upload"
            data = await f.read()
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(
                    413, f"{name} is {len(data) // (1024 * 1024)} MB; the limit is "
                         f"{MAX_FILE_BYTES // (1024 * 1024)} MB per file")

            target = st.uploads / f"{new_id('up_')}_{name}"
            target.write_bytes(data)
            queued.append(st.queue.submit(
                str(target), title=title or Path(name).stem, url=url, origin=origin))
            log.info("queued upload %s (%d bytes)", name, len(data))

        return {"jobs": queued, "count": len(queued)}
