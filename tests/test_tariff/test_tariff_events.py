"""Tests for tariff event emission (cap, credit, export tier).

Covers:
- Free-window cap event emission (consumed, approaching, exhausted)
- Credit window events (on-track, at-risk, forfeited)
- Export tier events
- Event history and querying
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from power_master.tariff.events import TariffEvent, TariffEventEmitter


class TestTariffEventEmitter:
    """Test tariff event emitter."""

    def test_init(self) -> None:
        """Emitter initializes with default history size."""
        emitter = TariffEventEmitter(max_history=100)
        assert emitter.get_recent_events(10) == []

    def test_emit_free_window_cap_consumed(self) -> None:
        """Emit free-window cap consumed event."""
        emitter = TariffEventEmitter()
        emitter.emit_free_window_cap_consumed(
            cap_name="four4free",
            kwh_consumed=25.0,
            cap_kwh_per_day=50.0,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "free_window_cap_consumed"
        assert events[0].details["cap_name"] == "four4free"
        assert events[0].details["kwh_consumed"] == 25.0
        assert events[0].details["cap_kwh_per_day"] == 50.0
        assert events[0].details["percent_used"] == 50.0

    def test_emit_free_window_cap_approaching(self) -> None:
        """Emit cap approaching event."""
        emitter = TariffEventEmitter()
        emitter.emit_free_window_cap_approaching(
            cap_name="four4free",
            kwh_consumed=40.0,
            cap_kwh_per_day=50.0,
            threshold_pct=0.80,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "free_window_cap_approaching"
        assert events[0].details["percent_used"] == 80.0
        assert events[0].details["threshold_pct"] == 80.0

    def test_emit_free_window_cap_exhausted(self) -> None:
        """Emit cap exhausted event."""
        emitter = TariffEventEmitter()
        emitter.emit_free_window_cap_exhausted(
            cap_name="four4free",
            cap_kwh_per_day=50.0,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "free_window_cap_exhausted"
        assert events[0].details["cap_name"] == "four4free"
        assert events[0].details["cap_kwh_per_day"] == 50.0

    def test_emit_credit_window_on_track(self) -> None:
        """Emit credit on-track event."""
        emitter = TariffEventEmitter()
        emitter.emit_credit_window_on_track(
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            current_import_kwh=0.02,
            threshold_kwh=0.09,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "credit_window_on_track"
        assert events[0].details["status"] == "on_track"
        assert events[0].details["current_import_kwh"] == 0.02

    def test_emit_credit_window_at_risk(self) -> None:
        """Emit credit at-risk event."""
        emitter = TariffEventEmitter()
        emitter.emit_credit_window_at_risk(
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            current_import_kwh=0.08,
            threshold_kwh=0.09,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "credit_window_at_risk"
        assert events[0].details["status"] == "at_risk"

    def test_emit_credit_window_forfeited(self) -> None:
        """Emit credit forfeited event."""
        emitter = TariffEventEmitter()
        emitter.emit_credit_window_forfeited(
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            final_import_kwh=0.15,
            threshold_kwh=0.09,
            reward_dollars=1.0,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "credit_window_forfeited"
        assert events[0].details["status"] == "forfeited"
        assert events[0].details["forfeited_reward_dollars"] == 1.0

    def test_emit_export_tier_progress(self) -> None:
        """Emit export tier progress event."""
        emitter = TariffEventEmitter()
        emitter.emit_export_tier_progress(
            tier_name="evening-premium",
            current_export_kwh=5.0,
            tier_cap_kwh_per_day=15.0,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "export_tier_progress"
        assert events[0].details["tier_name"] == "evening-premium"
        assert events[0].details["percent_used"] == 33.3

    def test_emit_export_tier_exhausted(self) -> None:
        """Emit export tier exhausted event."""
        emitter = TariffEventEmitter()
        emitter.emit_export_tier_exhausted(
            tier_name="evening-premium",
            tier_cap_kwh_per_day=15.0,
        )

        events = emitter.get_recent_events(1)
        assert len(events) == 1
        assert events[0].event_type == "export_tier_exhausted"

    def test_get_recent_events(self) -> None:
        """Get recent events up to N."""
        emitter = TariffEventEmitter(max_history=100)

        # Emit 5 events
        for i in range(5):
            emitter.emit_free_window_cap_consumed(
                cap_name=f"cap-{i}",
                kwh_consumed=float(i),
                cap_kwh_per_day=50.0,
            )

        # Get last 3
        recent = emitter.get_recent_events(3)
        assert len(recent) == 3
        assert recent[-1].details["cap_name"] == "cap-4"

    def test_get_events_by_type(self) -> None:
        """Filter events by type."""
        emitter = TariffEventEmitter()

        emitter.emit_free_window_cap_consumed("cap1", 10.0, 50.0)
        emitter.emit_free_window_cap_approaching("cap1", 40.0, 50.0)
        emitter.emit_export_tier_progress("tier1", 5.0, 15.0)

        cap_events = emitter.get_events_by_type("free_window_cap_consumed")
        assert len(cap_events) == 1
        assert cap_events[0].details["cap_name"] == "cap1"

        approaching_events = emitter.get_events_by_type("free_window_cap_approaching")
        assert len(approaching_events) == 1

        export_events = emitter.get_events_by_type("export_tier_progress")
        assert len(export_events) == 1

    def test_history_max_size(self) -> None:
        """History is trimmed to max_history."""
        emitter = TariffEventEmitter(max_history=5)

        for i in range(10):
            emitter.emit_free_window_cap_consumed(
                cap_name=f"cap-{i}",
                kwh_consumed=float(i),
                cap_kwh_per_day=50.0,
            )

        # Should only have last 5
        all_events = emitter.get_recent_events(100)
        assert len(all_events) == 5
        # Oldest should be cap-5
        assert all_events[0].details["cap_name"] == "cap-5"
        # Newest should be cap-9
        assert all_events[-1].details["cap_name"] == "cap-9"

    def test_event_to_dict(self) -> None:
        """Event can be serialized to dict."""
        now = datetime.now(timezone.utc)
        event = TariffEvent(
            event_type="test_event",
            timestamp=now,
            details={"key": "value"},
        )
        d = event.to_dict()
        assert d["event_type"] == "test_event"
        assert d["timestamp"] == now.isoformat()
        assert d["details"]["key"] == "value"
