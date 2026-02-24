"""Log context enrichment utilities."""

from __future__ import annotations

import structlog


def bind_context(**kwargs: object) -> None:
    """Bind key-value pairs to the current logging context (thread/task-local)."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Clear the current logging context."""
    structlog.contextvars.clear_contextvars()


def unbind_context(*keys: str) -> None:
    """Remove specific keys from the logging context."""
    structlog.contextvars.unbind_contextvars(*keys)
