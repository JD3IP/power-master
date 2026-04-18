"""Tests for the notification system — event bus, manager, and channels."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from power_master.config.schema import (
    NotificationChannelsConfig,
    NotificationEventsConfig,
    NotificationsConfig,
    NtfyChannelConfig,
    TelegramChannelConfig,
    WebhookChannelConfig,
)
from power_master.notifications.bus import Event, EventBus
from power_master.notifications.manager import NotificationManager


# ── Event Bus ─────────────────────────────────────────


async def test_event_bus_delivers_to_subscriber():
    bus = EventBus()
    received = []

    async def handler(event: Event):
        received.append(event)

    bus.subscribe(handler)
    event = Event(name="test", severity="info", title="Test", message="Hello")
    await bus.publish(event)

    assert len(received) == 1
    assert received[0].name == "test"


async def test_event_bus_multiple_subscribers():
    bus = EventBus()
    count = {"a": 0, "b": 0}

    async def handler_a(event):
        count["a"] += 1

    async def handler_b(event):
        count["b"] += 1

    bus.subscribe(handler_a)
    bus.subscribe(handler_b)
    await bus.publish(Event(name="x", severity="info", title="X", message=""))

    assert count["a"] == 1
    assert count["b"] == 1


async def test_event_bus_subscriber_error_does_not_propagate():
    bus = EventBus()
    received = []

    async def bad_handler(event):
        raise RuntimeError("boom")

    async def good_handler(event):
        received.append(event)

    bus.subscribe(bad_handler)
    bus.subscribe(good_handler)

    await bus.publish(Event(name="x", severity="info", title="X", message=""))
    assert len(received) == 1  # good_handler still ran


# ── Notification Manager ──────────────────────────────


def _make_config(**overrides) -> NotificationsConfig:
    defaults = {
        "enabled": True,
        "channels": NotificationChannelsConfig(
            webhook=WebhookChannelConfig(enabled=True, url="https://example.com/hook"),
        ),
    }
    defaults.update(overrides)
    return NotificationsConfig(**defaults)


async def test_manager_dispatches_to_channels():
    config = _make_config()
    bus = EventBus()
    manager = NotificationManager(config, bus)

    with patch(
        "power_master.notifications.channels.webhook.WebhookChannel.send",
        new_callable=AsyncMock,
    ) as mock_send:
        await bus.publish(Event(
            name="price_spike", severity="critical",
            title="Spike", message="Price spiked",
        ))
        mock_send.assert_called_once()


async def test_manager_respects_disabled_notifications():
    config = _make_config(enabled=False)
    bus = EventBus()
    manager = NotificationManager(config, bus)

    with patch(
        "power_master.notifications.channels.webhook.WebhookChannel.send",
        new_callable=AsyncMock,
    ) as mock_send:
        await bus.publish(Event(
            name="price_spike", severity="critical",
            title="Spike", message="Price spiked",
        ))
        mock_send.assert_not_called()


async def test_manager_respects_event_disabled():
    events = NotificationEventsConfig(
        price_spike={"enabled": False, "severity": "critical", "cooldown_seconds": 300},
    )
    config = _make_config(events=events)
    bus = EventBus()
    manager = NotificationManager(config, bus)

    with patch(
        "power_master.notifications.channels.webhook.WebhookChannel.send",
        new_callable=AsyncMock,
    ) as mock_send:
        await bus.publish(Event(
            name="price_spike", severity="critical",
            title="Spike", message="Price spiked",
        ))
        mock_send.assert_not_called()


async def test_manager_enforces_cooldown():
    events = NotificationEventsConfig(
        price_spike={"enabled": True, "severity": "critical", "cooldown_seconds": 9999},
    )
    config = _make_config(events=events)
    bus = EventBus()
    manager = NotificationManager(config, bus)

    with patch(
        "power_master.notifications.channels.webhook.WebhookChannel.send",
        new_callable=AsyncMock,
    ) as mock_send:
        # First should send
        await bus.publish(Event(
            name="price_spike", severity="critical",
            title="Spike", message="Price spiked",
        ))
        assert mock_send.call_count == 1

        # Second should be suppressed by cooldown
        await bus.publish(Event(
            name="price_spike", severity="critical",
            title="Spike", message="Price spiked again",
        ))
        assert mock_send.call_count == 1  # still 1


async def test_manager_different_events_independent_cooldown():
    config = _make_config()
    bus = EventBus()
    manager = NotificationManager(config, bus)

    with patch(
        "power_master.notifications.channels.webhook.WebhookChannel.send",
        new_callable=AsyncMock,
    ) as mock_send:
        await bus.publish(Event(
            name="price_spike", severity="critical",
            title="Spike", message="msg",
        ))
        await bus.publish(Event(
            name="inverter_offline", severity="critical",
            title="Offline", message="msg",
        ))
        assert mock_send.call_count == 2  # different events, no shared cooldown


async def test_manager_send_test():
    config = _make_config()
    bus = EventBus()
    manager = NotificationManager(config, bus)

    with patch(
        "power_master.notifications.channels.webhook.WebhookChannel.send",
        new_callable=AsyncMock,
    ):
        result = await manager.send_test("webhook")
        assert result["status"] == "ok"
        assert result["channel"] == "webhook"


async def test_manager_send_test_unknown_channel():
    config = _make_config()
    bus = EventBus()
    manager = NotificationManager(config, bus)

    result = await manager.send_test("telegram")
    # Telegram not configured (no bot_token), should error
    assert result["status"] == "error"


async def test_manager_reload_rebuilds_channels():
    config = _make_config()
    bus = EventBus()
    manager = NotificationManager(config, bus)
    assert len(manager._channels) == 1  # webhook

    # Reload with ntfy added
    new_config = _make_config(
        channels=NotificationChannelsConfig(
            webhook=WebhookChannelConfig(enabled=True, url="https://example.com/hook"),
            ntfy=NtfyChannelConfig(enabled=True, topic="test-topic"),
        ),
    )
    manager.reload(new_config)
    assert len(manager._channels) == 2


# ── Channel format_message ─────────────────────────────


def test_channel_format_message_critical():
    from power_master.notifications.channels.base import NotificationChannel

    class DummyChannel(NotificationChannel):
        name = "dummy"

        async def send(self, event):
            pass

    ch = DummyChannel()
    event = Event(name="test", severity="critical", title="Fire", message="Everything is on fire")
    title, body = ch.format_message(event)
    assert "CRITICAL" in title
    assert "Fire" in title
    assert body == "Everything is on fire"


# ── Config schema ───────────────────────────────────────


def test_notifications_config_defaults():
    from power_master.config.schema import AppConfig
    cfg = AppConfig()
    assert cfg.notifications.enabled is False
    assert cfg.notifications.channels.telegram.enabled is False
    assert cfg.notifications.events.price_spike.severity == "critical"
    assert cfg.notifications.events.price_spike.cooldown_seconds == 300
