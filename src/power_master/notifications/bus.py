"""Async event bus for decoupled notification dispatch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Subscriber = Callable[["Event"], Coroutine[Any, Any, None]]


@dataclass
class Event:
    """A notification event emitted by any system component."""

    name: str
    severity: str  # "info", "warning", "critical"
    title: str
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = field(default_factory=dict)


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
