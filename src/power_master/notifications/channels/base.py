"""Base class for notification channels."""

from __future__ import annotations

from abc import ABC, abstractmethod

from power_master.notifications.bus import Event


class NotificationChannel(ABC):
    """Abstract notification channel."""

    name: str = "base"

    @abstractmethod
    async def send(self, event: Event) -> None:
        """Send a notification for the given event."""

    def format_message(self, event: Event) -> tuple[str, str]:
        """Return (title, body) formatted for this channel."""
        severity_prefix = {
            "critical": "\u26a0\ufe0f CRITICAL",
            "warning": "\u26a0 WARNING",
            "info": "\u2139\ufe0f INFO",
        }
        prefix = severity_prefix.get(event.severity, "")
        title = f"{prefix}: {event.title}" if prefix else event.title
        return title, event.message
