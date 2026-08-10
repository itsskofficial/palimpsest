"""Make terminal output survive a legacy Windows code page.

Two independent problems, both of which crash a run rather than degrade it:

1. **Our own output.** Curly quotes and arrows in a summary line are unencodable on a
   `cp1252` console and raise `UnicodeEncodeError` mid-print, killing the command after
   it has already done the work. So this module ships an ASCII-safe symbol table and
   the CLI uses it.
2. **Your data.** A Notion page called "Über-notes" or a claim containing an em dash
   will hit the same wall no matter how careful our own strings are, because the text
   came from you. `install()` reconfigures the streams with `errors="replace"` so a
   character that cannot be rendered becomes `?` instead of an exception.

UTF-8 is attempted first, since modern Windows Terminal handles it and the output is
nicer; `errors="replace"` is the floor, not the goal.
"""

from __future__ import annotations

import contextlib
import sys

__all__ = ["ARROW", "BULLET", "LDQUO", "RDQUO", "install", "quote", "supports_unicode"]

_installed = False
_unicode_ok = True


def install() -> bool:
    """Reconfigure stdout/stderr so unencodable characters never raise.

    Returns whether the streams can carry non-ASCII. Idempotent.
    """
    global _installed, _unicode_ok
    if _installed:
        return _unicode_ok

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - a captured or exotic stream
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - console refuses UTF-8
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")

    encoding = getattr(sys.stdout, "encoding", "") or ""
    try:
        "→".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        _unicode_ok = False

    _installed = True
    return _unicode_ok


def supports_unicode() -> bool:
    return install()


# Symbols the CLI uses, resolved once. ASCII fallbacks are chosen to read naturally
# rather than to be clever: `->` is a better degradation of `→` than a dropped glyph.
def _sym(fancy: str, plain: str) -> str:
    return fancy if supports_unicode() else plain


ARROW = _sym("→", "->")
BULLET = _sym("·", "-")
LDQUO = _sym("“", '"')
RDQUO = _sym("”", '"')


def quote(text: str) -> str:
    return f"{LDQUO}{text}{RDQUO}"
