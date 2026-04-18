"""Async event bus for decoupled notification dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Subscriber = Callable[["Event"], Coroutine[Any, Any, None]]


class Tier(str, Enum):
    """Actionability tier — orthogonal to severity.

    INFORMATIONAL: system handled it autonomously; FYI only.
    ATTENTION:     user should know now, no action required.
    DECISION:      user input would change the outcome.
    """
    INFORMATIONAL = "informational"
    ATTENTION = "attention"
    DECISION = "decision"


@dataclass(frozen=True)
class Action:
    """Structured "what the system did / is doing" block attached to an event.

    `taken` is a list of human-readable strings describing concrete actions
    (or committed plan changes).  `observation` is for events where the
    system is NOT acting (e.g. inverter offline — framed honestly).
    """
    taken: list[str] = field(default_factory=list)
    reason: str = ""
    observation: str = ""
    expires_at: datetime | None = None

    def content_hash(self) -> str:
        """Stable fingerprint of Action content for update-detection."""
        payload = f"{sorted(self.taken)}|{self.reason}|{self.observation}|{self.expires_at}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "taken": list(self.taken),
            "reason": self.reason,
            "observation": self.observation,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class Event:
    """A notification event emitted by any system component."""

    name: str
    severity: str  # "info", "warning", "critical"
    title: str
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = field(default_factory=dict)
    # ── New fields: action + incident tracking ──
    tier: Tier = Tier.INFORMATIONAL
    action: Action | None = None
    # Identity of the ongoing incident this event belongs to (stable across
    # updates, distinct across separate incidents of the same event name).
    # e.g. "storm_plan_active:2026-04-17T18:00Z".
    incident_id: str | None = None
    # Correlates an open-state event with its later *_resolved pair.
    correlation_id: str | None = None


class EventBus:
    """Simple async publish/subscribe event bus.

    Subscribers receive all events; filtering is done in the notification manager.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    async def publish(self, event: Event) -> None:
        for subscriber in self._subscribers:
            try:
                await subscriber(event)
            except Exception:
                logger.exception("Event subscriber error for %s", event.name)
