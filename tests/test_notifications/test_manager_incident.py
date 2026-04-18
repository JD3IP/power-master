"""Tests for the reworked NotificationManager — incident dedup + persistence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from power_master.config.schema import NotificationsConfig
from power_master.notifications.bus import Action, Event, EventBus, Tier
from power_master.notifications.manager import (
    MAX_UPDATES_PER_INCIDENT_HOUR,
    NotificationManager,
)


def _mk_event(name: str, action: Action | None = None, incident_id: str | None = None) -> Event:
    return Event(
        name=name,
        severity="warning",
        title=f"{name}-title",
        message=f"{name}-message",
        tier=Tier.ATTENTION,
        action=action,
        incident_id=incident_id,
        timestamp=datetime.now(timezone.utc),
    )


class TestPersistence:
    @pytest.mark.asyncio
    async def test_event_is_logged_even_if_rule_disabled(self, repo) -> None:
        cfg = NotificationsConfig(enabled=False)
        cfg.events.price_spike.enabled = False
        bus = EventBus()
        NotificationManager(cfg, bus, repo=repo)
        await bus.publish(_mk_event("price_spike", action=Action(taken=["x"])))
        # give the task a moment
        await asyncio.sleep(0)
        rows = await repo.get_notifications_since("2000-01-01T00:00:00+00:00")
        assert len(rows) == 1
        assert rows[0]["event_name"] == "price_spike"
        assert rows[0]["channels_sent"] == ""

    @pytest.mark.asyncio
    async def test_action_json_roundtrip(self, repo) -> None:
        import json
        cfg = NotificationsConfig(enabled=True)
        cfg.events.price_spike.enabled = True
        bus = EventBus()
        NotificationManager(cfg, bus, repo=repo)
        action = Action(taken=["do a", "do b"], reason="r1")
        await bus.publish(_mk_event("price_spike", action=action))
        await asyncio.sleep(0)
        rows = await repo.get_notifications_since("2000-01-01T00:00:00+00:00")
        assert len(rows) == 1
        parsed = json.loads(rows[0]["action_json"])
        assert parsed["taken"] == ["do a", "do b"]
        assert parsed["reason"] == "r1"


class TestIncidentDedup:
    @pytest.mark.asyncio
    async def test_unchanged_action_suppressed_during_cooldown(self, repo) -> None:
        cfg = NotificationsConfig(enabled=True)
        cfg.events.price_spike.enabled = True
        cfg.events.price_spike.cooldown_seconds = 300
        bus = EventBus()
        NotificationManager(cfg, bus, repo=repo)
        action = Action(taken=["x"])
        incident = "spike:1"
        await bus.publish(_mk_event("price_spike", action=action, incident_id=incident))
        await bus.publish(_mk_event("price_spike", action=action, incident_id=incident))
        await asyncio.sleep(0)
        rows = await repo.get_notifications_since("2000-01-01T00:00:00+00:00")
        # Second event is logged as "channels_sent empty" but only the first
        # actually dispatched.  Count emitted (channels_sent != "" is tricky
        # with zero channels configured); just assert only one row exists.
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_changed_action_refires_update(self, repo) -> None:
        cfg = NotificationsConfig(enabled=True)
        cfg.events.price_spike.enabled = True
        cfg.events.price_spike.cooldown_seconds = 300
        bus = EventBus()
        NotificationManager(cfg, bus, repo=repo)
        incident = "spike:1"
        await bus.publish(_mk_event("price_spike", action=Action(taken=["v1"]),
                                    incident_id=incident))
        await bus.publish(_mk_event("price_spike", action=Action(taken=["v2"]),
                                    incident_id=incident))
        await asyncio.sleep(0)
        rows = await repo.get_notifications_since("2000-01-01T00:00:00+00:00")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_update_rate_limited(self, repo) -> None:
        cfg = NotificationsConfig(enabled=True)
        cfg.events.price_spike.enabled = True
        cfg.events.price_spike.cooldown_seconds = 300
        bus = EventBus()
        NotificationManager(cfg, bus, repo=repo)
        incident = "spike:1"
        # Total emissions per incident within 1h are capped at MAX_UPDATES_PER_INCIDENT_HOUR
        # (first emission counts toward the cap).
        for i in range(MAX_UPDATES_PER_INCIDENT_HOUR + 3):
            await bus.publish(_mk_event(
                "price_spike", action=Action(taken=[f"v{i}"]),
                incident_id=incident,
            ))
        await asyncio.sleep(0)
        rows = await repo.get_notifications_since("2000-01-01T00:00:00+00:00")
        assert len(rows) == MAX_UPDATES_PER_INCIDENT_HOUR
