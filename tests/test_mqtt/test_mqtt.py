"""Tests for MQTT topics, publisher, discovery, and subscriber."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from power_master.hardware.telemetry import Telemetry
from power_master.mqtt.discovery import build_discovery_configs, publish_discovery
from power_master.mqtt.publisher import MQTTPublisher
from power_master.mqtt.subscriber import LoadCommandSubscriber
from power_master.mqtt.topics import build_topics, load_command_topic, load_state_topic


# ── Topics Tests ──────────────────────────────────────────────


class TestTopics:
    def test_default_prefix(self) -> None:
        topics = build_topics()
        assert topics["battery_soc"] == "power_master/battery/soc"
        assert topics["status"] == "power_master/status"
        assert topics["mode_current"] == "power_master/mode/current"

    def test_custom_prefix(self) -> None:
        topics = build_topics("solar_system")
        assert topics["battery_soc"] == "solar_system/battery/soc"
        assert topics["grid_power"] == "solar_system/grid/power"

    def test_load_command_topic(self) -> None:
        topic = load_command_topic("power_master", "pool_pump")
        assert topic == "power_master/load/pool_pump/command"

    def test_load_state_topic(self) -> None:
        topic = load_state_topic("power_master", "hot_water")
        assert topic == "power_master/load/hot_water/state"

    def test_all_topics_present(self) -> None:
        topics = build_topics()
        expected = [
            "status", "battery_soc", "battery_power", "battery_wacb",
            "solar_power", "grid_power", "load_total", "tariff_import",
            "tariff_export", "mode_current", "storm_active",
            "accounting_today_net", "spike_active",
        ]
        for key in expected:
            assert key in topics


# ── Publisher Tests ────────────────────────────────────────────


class TestPublisher:
    @pytest.mark.asyncio
    async def test_publish_telemetry(self) -> None:
        publish_fn = AsyncMock()
        publisher = MQTTPublisher(publish_fn)

        telemetry = Telemetry(
            soc=0.72,
            battery_power_w=-1500,
            solar_power_w=4200,
            grid_power_w=-1100,
            load_power_w=2500,
        )
        await publisher.publish_telemetry(telemetry)

        # Should publish 5 telemetry values
        assert publish_fn.call_count == 5

        # Check SOC was published with retain
        calls = publish_fn.call_args_list
        soc_call = [c for c in calls if "battery/soc" in c[0][0]]
        assert len(soc_call) == 1
        assert soc_call[0][0][1] == "72.0"
        assert soc_call[0][0][2] is True  # Retained

    @pytest.mark.asyncio
    async def test_publish_status(self) -> None:
        publish_fn = AsyncMock()
        publisher = MQTTPublisher(publish_fn)

        await publisher.publish_status(online=True)

        publish_fn.assert_called_once()
        assert publish_fn.call_args[0][1] == "online"
        assert publish_fn.call_args[0][2] is True  # Retained

    @pytest.mark.asyncio
    async def test_publish_mode(self) -> None:
        publish_fn = AsyncMock()
        publisher = MQTTPublisher(publish_fn)

        await publisher.publish_mode("FORCE_CHARGE")

        publish_fn.assert_called_once()
        assert publish_fn.call_args[0][1] == "FORCE_CHARGE"

    @pytest.mark.asyncio
    async def test_publish_tariff(self) -> None:
        publish_fn = AsyncMock()
        publisher = MQTTPublisher(publish_fn)

        await publisher.publish_tariff(import_cents=22.5, export_cents=7.3)

        assert publish_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_publish_storm(self) -> None:
        publish_fn = AsyncMock()
        publisher = MQTTPublisher(publish_fn)

        await publisher.publish_storm(active=True)
        assert publish_fn.call_args[0][1] == "true"

    @pytest.mark.asyncio
    async def test_publish_spike(self) -> None:
        publish_fn = AsyncMock()
        publisher = MQTTPublisher(publish_fn)

        await publisher.publish_spike(active=False)
        assert publish_fn.call_args[0][1] == "false"


# ── Discovery Tests ────────────────────────────────────────────


class TestDiscovery:
    def test_build_configs(self) -> None:
        configs = build_discovery_configs()
        assert len(configs) == 12  # 12 entities defined

        # Check first config structure
        topic, payload = configs[0]
        assert topic.startswith("homeassistant/sensor/")
        config = json.loads(payload)
        assert "name" in config
        assert "unique_id" in config
        assert "state_topic" in config
        assert "device" in config

    def test_custom_prefix(self) -> None:
        configs = build_discovery_configs(topic_prefix="solar", ha_prefix="ha")
        topic, payload = configs[0]
        assert topic.startswith("ha/sensor/")
        config = json.loads(payload)
        assert "solar/" in config["state_topic"]

    def test_device_info(self) -> None:
        configs = build_discovery_configs()
        _, payload = configs[0]
        config = json.loads(payload)
        device = config["device"]
        assert device["identifiers"] == ["power_master"]
        assert device["name"] == "Power Master"

    @pytest.mark.asyncio
    async def test_publish_discovery(self) -> None:
        publish_fn = AsyncMock()
        count = await publish_discovery(publish_fn)
        assert count == 12
        assert publish_fn.call_count == 12
        # All should be retained
        for call in publish_fn.call_args_list:
            assert call[0][2] is True


# ── Subscriber Tests ───────────────────────────────────────────


class TestSubscriber:
    def test_register_load(self) -> None:
        subscriber = LoadCommandSubscriber()
        handler = lambda payload: None

        topic = subscriber.register_load("pool_pump", handler)
        assert topic == "power_master/load/pool_pump/command"
        assert topic in subscriber.topics

    @pytest.mark.asyncio
    async def test_handle_message_calls_handler(self) -> None:
        subscriber = LoadCommandSubscriber()
        received = []
        subscriber.register_load("pump", lambda payload: received.append(payload))

        await subscriber.handle_message("power_master/load/pump/command", "ON")
        assert received == ["ON"]

    @pytest.mark.asyncio
    async def test_handle_unregistered_topic(self) -> None:
        subscriber = LoadCommandSubscriber()
        # Should not raise
        await subscriber.handle_message("unknown/topic", "test")

    def test_multiple_loads(self) -> None:
        subscriber = LoadCommandSubscriber()
        subscriber.register_load("pump", lambda p: None)
        subscriber.register_load("heater", lambda p: None)
        assert len(subscriber.topics) == 2
