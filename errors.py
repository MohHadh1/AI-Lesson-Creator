"""Shared errors for AI Lesson Creator.

Every message that can reach the screen or a saved file passes through
``public_error_message`` so an API key cannot leak into the UI.
"""

from __future__ import annotations

import re

# Google API keys start with this prefix. Strip any that appear in an error.
_SECRET_PATTERN = re.compile(
    r"(AIza[0-9A-Za-z\-_]{10,}|ya29\.[0-9A-Za-z\-_]+)",
)
_KEY_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret)\b(\s*[=:]\s*)(\S+)",
)


class LessonCreatorError(Exception):
    """A problem the instructor can act on.

    Raise this with a short, plain sentence. Do not include API keys,
    request headers, or raw credential text.
    """


def public_error_message(exc: BaseException) -> str:
    """Turn an exception into a short message that is safe to display."""
    if isinstance(exc, LessonCreatorError):
        text = str(exc).strip()
    else:
        text = str(exc).strip() or exc.__class__.__name__

    text = _SECRET_PATTERN.sub("[hidden]", text)
    text = _KEY_ASSIGNMENT.sub(r"\1\2[hidden]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "Something went wrong. Try the step again."
    if len(text) > 400:
        text = text[:400].rstrip() + "..."
    return text
