"""MQTT subscriber for load control commands from Home Assistant."""

from __future__ import annotations

import logging
from typing import Callable

from power_master.mqtt.topics import load_command_topic

logger = logging.getLogger(__name__)


class LoadCommandSubscriber:
    """Subscribes to MQTT load command topics and dispatches to callbacks."""

    def __init__(self, topic_prefix: str = "power_master") -> None:
        self._prefix = topic_prefix
        self._handlers: dict[str, Callable[[str], None]] = {}

    def register_load(self, load_id: str, handler: Callable[[str], None]) -> str:
        """Register a handler for a load's command topic.

        Args:
            load_id: Unique load identifier.
            handler: Callback receiving the payload string ("ON"/"OFF").

        Returns:
            The MQTT topic to subscribe to.
        """
        topic = load_command_topic(self._prefix, load_id)
        self._handlers[topic] = handler
        return topic

    @property
    def topics(self) -> list[str]:
        """All registered command topics."""
        return list(self._handlers.keys())

    async def handle_message(self, topic: str, payload: str) -> None:
        """Route an incoming MQTT message to the correct handler."""
        handler = self._handlers.get(topic)
        if handler:
            logger.debug("Load command received: %s → %s", topic, payload)
            handler(payload)
        else:
            logger.debug("Unhandled MQTT message: %s", topic)
