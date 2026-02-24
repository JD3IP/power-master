"""MQTT telemetry and status publisher."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from power_master.hardware.telemetry import Telemetry
from power_master.mqtt.topics import build_topics

logger = logging.getLogger(__name__)

# Type for async publish function: (topic, payload, retain) -> None
PublishFn = Callable[[str, str, bool], Coroutine[Any, Any, None]]


class MQTTPublisher:
    """Publishes telemetry and system state to MQTT topics."""

    def __init__(self, publish_fn: PublishFn, topic_prefix: str = "power_master") -> None:
        self._publish = publish_fn
        self._topics = build_topics(topic_prefix)

    async def publish_telemetry(self, telemetry: Telemetry) -> None:
        """Publish current telemetry readings."""
        await self._publish(self._topics["battery_soc"], f"{telemetry.soc_pct:.1f}", True)
        await self._publish(self._topics["battery_power"], str(telemetry.battery_power_w), False)
        await self._publish(self._topics["solar_power"], str(telemetry.solar_power_w), False)
        await self._publish(self._topics["grid_power"], str(telemetry.grid_power_w), False)
        await self._publish(self._topics["load_total"], str(telemetry.load_power_w), False)

    async def publish_status(self, online: bool = True) -> None:
        """Publish system online/offline status."""
        await self._publish(self._topics["status"], "online" if online else "offline", True)

    async def publish_mode(self, mode_name: str) -> None:
        """Publish current operating mode."""
        await self._publish(self._topics["mode_current"], mode_name, True)

    async def publish_tariff(self, import_cents: float, export_cents: float) -> None:
        """Publish current tariff rates."""
        await self._publish(self._topics["tariff_import"], f"{import_cents:.1f}", True)
        await self._publish(self._topics["tariff_export"], f"{export_cents:.1f}", True)

    async def publish_wacb(self, wacb_cents: float) -> None:
        """Publish battery WACB."""
        await self._publish(self._topics["battery_wacb"], f"{wacb_cents:.1f}", True)

    async def publish_storm(self, active: bool) -> None:
        """Publish storm reserve status."""
        await self._publish(self._topics["storm_active"], "true" if active else "false", True)

    async def publish_spike(self, active: bool) -> None:
        """Publish spike status."""
        await self._publish(self._topics["spike_active"], "true" if active else "false", True)

    async def publish_accounting(self, today_net_cents: int) -> None:
        """Publish today's net cost."""
        await self._publish(self._topics["accounting_today_net"], str(today_net_cents), False)
