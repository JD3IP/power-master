"""MQTT-based load control adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from power_master.config.schema import MQTTLoadEndpointConfig
from power_master.loads.base import LoadState, LoadStatus

logger = logging.getLogger(__name__)

# Type for an async MQTT publish function: (topic, payload, retain) -> None
MQTTPublishFn = Callable[[str, str, bool], Coroutine[Any, Any, None]]


class MQTTLoadAdapter:
    """Controls a load via MQTT command/state topics.

    Uses an externally-provided publish function (from the MQTT client module)
    so this adapter doesn't manage its own MQTT connection.
    """

    def __init__(
        self,
        config: MQTTLoadEndpointConfig,
        publish_fn: MQTTPublishFn,
    ) -> None:
        self._config = config
        self._publish = publish_fn
        self._last_known_state: LoadState = LoadState.UNKNOWN
        self._available = True

    @property
    def load_id(self) -> str:
        return f"mqtt_{self._config.name}"

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def power_w(self) -> int:
        return self._config.power_w

    @property
    def priority_class(self) -> int:
        return self._config.priority_class

    @property
    def command_topic(self) -> str:
        return self._config.command_topic

    @property
    def state_topic(self) -> str:
        return self._config.state_topic

    async def turn_on(self) -> bool:
        """Publish ON command to MQTT topic."""
        try:
            await self._publish(self._config.command_topic, "ON", False)
            self._last_known_state = LoadState.ON
            logger.info("MQTT load '%s' commanded ON via %s", self._config.name, self._config.command_topic)
            return True
        except Exception as e:
            logger.error("Failed to turn on MQTT load '%s': %s", self._config.name, e)
            self._available = False
            return False

    async def turn_off(self) -> bool:
        """Publish OFF command to MQTT topic."""
        try:
            await self._publish(self._config.command_topic, "OFF", False)
            self._last_known_state = LoadState.OFF
            logger.info("MQTT load '%s' commanded OFF via %s", self._config.name, self._config.command_topic)
            return True
        except Exception as e:
            logger.error("Failed to turn off MQTT load '%s': %s", self._config.name, e)
            self._available = False
            return False

    async def get_status(self) -> LoadStatus:
        """Return last known state (updated by state topic subscriber)."""
        return LoadStatus(
            load_id=self.load_id,
            name=self.name,
            state=self._last_known_state,
            power_w=self._config.power_w if self._last_known_state == LoadState.ON else 0,
            is_available=self._available,
        )

    async def is_available(self) -> bool:
        """MQTT loads are available if we can publish."""
        return self._available

    def handle_state_update(self, payload: str) -> None:
        """Called when a message is received on the state topic.

        Expected payloads: "ON", "OFF", "on", "off", "1", "0", "true", "false"
        """
        normalised = payload.strip().upper()
        if normalised in ("ON", "1", "TRUE"):
            self._last_known_state = LoadState.ON
        elif normalised in ("OFF", "0", "FALSE"):
            self._last_known_state = LoadState.OFF
        else:
            logger.warning("Unknown state payload for '%s': %s", self._config.name, payload)
            self._last_known_state = LoadState.UNKNOWN
        self._available = True
