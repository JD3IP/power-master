"""Async MQTT client wrapper using aiomqtt."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from power_master.config.schema import MQTTConfig

logger = logging.getLogger(__name__)

# Type alias for message callback: (topic, payload) -> None
MessageCallback = Callable[[str, str], Coroutine[Any, Any, None]]


class MQTTClient:
    """Async MQTT client wrapping aiomqtt.

    Handles connection, reconnection, and provides publish/subscribe methods.
    """

    def __init__(self, config: MQTTConfig) -> None:
        self._config = config
        self._client: Any = None
        self._connected = False
        self._subscriptions: dict[str, MessageCallback] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the MQTT broker."""
        try:
            import aiomqtt

            self._client = aiomqtt.Client(
                hostname=self._config.broker_host,
                port=self._config.broker_port,
                username=self._config.username or None,
                password=self._config.password or None,
            )
            logger.info(
                "MQTT connecting to %s:%d",
                self._config.broker_host, self._config.broker_port,
            )
            self._connected = True
        except ImportError:
            logger.warning("aiomqtt not installed — MQTT disabled")
            self._connected = False
        except Exception as e:
            logger.error("MQTT connect failed: %s", e)
            self._connected = False

    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        self._connected = False
        self._client = None

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """Publish a message to a topic."""
        if not self._connected or self._client is None:
            return

        try:
            async with self._client as client:
                await client.publish(topic, payload, retain=retain)
        except Exception as e:
            logger.error("MQTT publish failed for %s: %s", topic, e)

    def subscribe(self, topic: str, callback: MessageCallback) -> None:
        """Register a subscription callback for a topic."""
        self._subscriptions[topic] = callback

    async def listen(self) -> None:
        """Start listening for subscribed messages (blocking)."""
        if not self._connected or self._client is None or not self._subscriptions:
            return

        try:
            async with self._client as client:
                for topic in self._subscriptions:
                    await client.subscribe(topic)

                async for message in client.messages:
                    topic = str(message.topic)
                    payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)

                    callback = self._subscriptions.get(topic)
                    if callback:
                        try:
                            await callback(topic, payload)
                        except Exception:
                            logger.exception("MQTT callback error for %s", topic)
        except Exception as e:
            logger.error("MQTT listener error: %s", e)
            self._connected = False
