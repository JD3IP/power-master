"""Home Assistant MQTT auto-discovery."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine

from power_master.mqtt.topics import build_topics

logger = logging.getLogger(__name__)

PublishFn = Callable[[str, str, bool], Coroutine[Any, Any, None]]

# Discovery entities: (unique_id_suffix, name, state_topic_key, unit, device_class, icon)
_ENTITIES = [
    ("battery_soc", "Battery SOC", "battery_soc", "%", "battery", "mdi:battery"),
    ("battery_power", "Battery Power", "battery_power", "W", "power", "mdi:battery-charging"),
    ("solar_power", "Solar Power", "solar_power", "W", "power", "mdi:solar-power"),
    ("grid_power", "Grid Power", "grid_power", "W", "power", "mdi:transmission-tower"),
    ("load_total", "Load Total", "load_total", "W", "power", "mdi:flash"),
    ("tariff_import", "Import Tariff", "tariff_import", "c/kWh", None, "mdi:currency-usd"),
    ("tariff_export", "Export Tariff", "tariff_export", "c/kWh", None, "mdi:currency-usd"),
    ("battery_wacb", "Battery WACB", "battery_wacb", "c/kWh", None, "mdi:calculator"),
    ("mode_current", "Operating Mode", "mode_current", None, None, "mdi:cog"),
    ("storm_active", "Storm Reserve", "storm_active", None, None, "mdi:weather-lightning"),
    ("spike_active", "Price Spike", "spike_active", None, None, "mdi:alert"),
    ("accounting_today", "Today Net Cost", "accounting_today_net", "c", None, "mdi:cash"),
]


def build_discovery_configs(
    topic_prefix: str = "power_master",
    ha_prefix: str = "homeassistant",
) -> list[tuple[str, str]]:
    """Build HA discovery config messages.

    Returns:
        List of (discovery_topic, config_json) tuples.
    """
    topics = build_topics(topic_prefix)
    configs = []

    device_info = {
        "identifiers": ["power_master"],
        "name": "Power Master",
        "manufacturer": "Custom",
        "model": "Solar Optimiser",
        "sw_version": "1.0",
    }

    for uid_suffix, name, topic_key, unit, device_class, icon in _ENTITIES:
        unique_id = f"power_master_{uid_suffix}"
        discovery_topic = f"{ha_prefix}/sensor/{unique_id}/config"

        config: dict[str, Any] = {
            "name": name,
            "unique_id": unique_id,
            "state_topic": topics[topic_key],
            "device": device_info,
            "icon": icon,
        }
        if unit:
            config["unit_of_measurement"] = unit
        if device_class:
            config["device_class"] = device_class

        configs.append((discovery_topic, json.dumps(config)))

    return configs


async def publish_discovery(
    publish_fn: PublishFn,
    topic_prefix: str = "power_master",
    ha_prefix: str = "homeassistant",
) -> int:
    """Publish all HA discovery configs. Returns count published."""
    configs = build_discovery_configs(topic_prefix, ha_prefix)

    for topic, payload in configs:
        await publish_fn(topic, payload, True)

    logger.info("Published %d HA discovery configs", len(configs))
    return len(configs)
