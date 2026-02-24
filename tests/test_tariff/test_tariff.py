"""Tests for tariff base models, schedule utilities, and spike detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from power_master.tariff.base import TariffSchedule, TariffSlot
from power_master.tariff.providers.amber import AmberProvider
from power_master.tariff.schedule import (
    classify_slot,
    get_cheapest_slots,
    get_most_profitable_export_slots,
)
from power_master.tariff.spike import SpikeDetector, SpikeEvent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_slot(
    price: float,
    export_price: float = 5.0,
    offset_minutes: int = 0,
) -> TariffSlot:
    start = _now() + timedelta(minutes=offset_minutes)
    return TariffSlot(
        start=start,
        end=start + timedelta(minutes=30),
        import_price_cents=price,
        export_price_cents=export_price,
    )


class TestTariffSlot:
    def test_is_spike_false_by_default(self) -> None:
        slot = _make_slot(50.0)
        assert not slot.is_spike

    def test_is_spike_true_when_descriptor_set(self) -> None:
        slot = _make_slot(150.0)
        slot.descriptor = "spike"
        assert slot.is_spike


class TestAmberParsing:
    def test_feed_in_negative_normalized_to_positive_revenue(self) -> None:
        data = [
            {
                "channelType": "general",
                "startTime": "2026-02-24T08:00:00+00:00",
                "perKwh": 42.0,
                "descriptor": "spike",
            },
            {
                "channelType": "feedIn",
                "startTime": "2026-02-24T08:00:00+00:00",
                "perKwh": -18.5,
            },
        ]
        slots = AmberProvider._parse_prices(data)
        assert len(slots) == 1
        assert slots[0].import_price_cents == 42.0
        assert slots[0].export_price_cents == 18.5


class TestTariffSchedule:
    def test_get_slot_at_finds_current(self) -> None:
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() - timedelta(minutes=15),
                    end=_now() + timedelta(minutes=15),
                    import_price_cents=25.0,
                    export_price_cents=8.0,
                )
            ]
        )
        slot = schedule.get_slot_at(_now())
        assert slot is not None
        assert slot.import_price_cents == 25.0

    def test_get_slot_at_returns_none_outside_range(self) -> None:
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() + timedelta(hours=1),
                    end=_now() + timedelta(hours=2),
                    import_price_cents=25.0,
                    export_price_cents=8.0,
                )
            ]
        )
        assert schedule.get_slot_at(_now()) is None


class TestTariffScheduleTimezones:
    """Test get_slot_at() with different timezone inputs."""

    def test_aest_query_against_utc_slot(self) -> None:
        """AEST (+10) query should match a UTC slot covering the same instant."""
        from datetime import timezone as tz

        aest = tz(timedelta(hours=10))
        utc_start = datetime(2025, 6, 15, 4, 0, tzinfo=timezone.utc)  # 14:00 AEST
        utc_end = datetime(2025, 6, 15, 4, 30, tzinfo=timezone.utc)  # 14:30 AEST

        schedule = TariffSchedule(
            slots=[TariffSlot(start=utc_start, end=utc_end,
                              import_price_cents=25.0, export_price_cents=8.0)]
        )
        # Query with AEST time that falls in the same instant
        query = datetime(2025, 6, 15, 14, 15, tzinfo=aest)
        slot = schedule.get_slot_at(query)
        assert slot is not None
        assert slot.import_price_cents == 25.0

    def test_z_suffix_parsed_as_utc(self) -> None:
        """Amber times with Z suffix should parse correctly."""
        start_str = "2025-06-15T04:00:00Z"
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end = start + timedelta(minutes=30)

        schedule = TariffSchedule(
            slots=[TariffSlot(start=start, end=end,
                              import_price_cents=30.0, export_price_cents=10.0)]
        )
        query = datetime(2025, 6, 15, 4, 10, tzinfo=timezone.utc)
        assert schedule.get_slot_at(query) is not None

    def test_naive_query_treated_as_utc(self) -> None:
        """Naive datetime query should be treated as UTC."""
        utc_start = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        utc_end = datetime(2025, 6, 15, 10, 30, tzinfo=timezone.utc)

        schedule = TariffSchedule(
            slots=[TariffSlot(start=utc_start, end=utc_end,
                              import_price_cents=20.0, export_price_cents=5.0)]
        )
        naive_query = datetime(2025, 6, 15, 10, 15)  # No tzinfo
        slot = schedule.get_slot_at(naive_query)
        assert slot is not None

    def test_gap_returns_none(self) -> None:
        """Query in a gap between slots returns None."""
        start1 = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        end1 = datetime(2025, 6, 15, 10, 30, tzinfo=timezone.utc)
        start2 = datetime(2025, 6, 15, 11, 0, tzinfo=timezone.utc)
        end2 = datetime(2025, 6, 15, 11, 30, tzinfo=timezone.utc)

        schedule = TariffSchedule(
            slots=[
                TariffSlot(start=start1, end=end1, import_price_cents=20.0, export_price_cents=5.0),
                TariffSlot(start=start2, end=end2, import_price_cents=25.0, export_price_cents=8.0),
            ]
        )
        gap_query = datetime(2025, 6, 15, 10, 45, tzinfo=timezone.utc)
        assert schedule.get_slot_at(gap_query) is None


class TestClassifySlot:
    def test_spike(self) -> None:
        assert classify_slot(_make_slot(150.0)) == "spike"

    def test_negative(self) -> None:
        assert classify_slot(_make_slot(-5.0)) == "negative"

    def test_off_peak(self) -> None:
        assert classify_slot(_make_slot(5.0)) == "off-peak"

    def test_shoulder(self) -> None:
        assert classify_slot(_make_slot(20.0)) == "shoulder"

    def test_peak(self) -> None:
        assert classify_slot(_make_slot(35.0)) == "peak"

    def test_custom_threshold(self) -> None:
        assert classify_slot(_make_slot(60.0), spike_threshold_cents=50) == "spike"


class TestScheduleUtilities:
    def test_get_cheapest_slots(self) -> None:
        schedule = TariffSchedule(
            slots=[
                _make_slot(30.0, offset_minutes=0),
                _make_slot(5.0, offset_minutes=30),
                _make_slot(15.0, offset_minutes=60),
                _make_slot(3.0, offset_minutes=90),
            ]
        )
        cheapest = get_cheapest_slots(schedule, count=2)
        assert len(cheapest) == 2
        assert cheapest[0].import_price_cents == 3.0
        assert cheapest[1].import_price_cents == 5.0

    def test_get_most_profitable_export(self) -> None:
        schedule = TariffSchedule(
            slots=[
                _make_slot(30.0, export_price=5.0, offset_minutes=0),
                _make_slot(30.0, export_price=25.0, offset_minutes=30),
                _make_slot(30.0, export_price=15.0, offset_minutes=60),
            ]
        )
        profitable = get_most_profitable_export_slots(schedule, count=1)
        assert len(profitable) == 1
        assert profitable[0].export_price_cents == 25.0


class TestSpikeDetector:
    def test_no_spike_below_threshold(self) -> None:
        detector = SpikeDetector(spike_threshold_cents=100)
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() - timedelta(minutes=15),
                    end=_now() + timedelta(minutes=15),
                    import_price_cents=80.0,
                    export_price_cents=5.0,
                )
            ]
        )
        changed = detector.evaluate(schedule)
        assert not changed
        assert not detector.is_spike_active

    def test_spike_detected_above_threshold(self) -> None:
        detector = SpikeDetector(spike_threshold_cents=100)
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() - timedelta(minutes=15),
                    end=_now() + timedelta(minutes=15),
                    import_price_cents=150.0,
                    export_price_cents=5.0,
                )
            ]
        )
        changed = detector.evaluate(schedule)
        assert changed
        assert detector.is_spike_active
        assert detector.current_spike is not None
        assert detector.current_spike.peak_price_cents == 150.0

    def test_spike_ends_when_price_drops(self) -> None:
        detector = SpikeDetector(spike_threshold_cents=100)

        # Start spike
        high_schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() - timedelta(minutes=15),
                    end=_now() + timedelta(minutes=15),
                    import_price_cents=200.0,
                    export_price_cents=5.0,
                )
            ]
        )
        detector.evaluate(high_schedule)
        assert detector.is_spike_active

        # End spike
        low_schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() - timedelta(minutes=15),
                    end=_now() + timedelta(minutes=15),
                    import_price_cents=20.0,
                    export_price_cents=5.0,
                )
            ]
        )
        changed = detector.evaluate(low_schedule)
        assert changed
        assert not detector.is_spike_active

    def test_upcoming_spikes(self) -> None:
        detector = SpikeDetector(spike_threshold_cents=100)
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=_now() + timedelta(hours=1),
                    end=_now() + timedelta(hours=1, minutes=30),
                    import_price_cents=200.0,
                    export_price_cents=5.0,
                ),
                TariffSlot(
                    start=_now() + timedelta(hours=2),
                    end=_now() + timedelta(hours=2, minutes=30),
                    import_price_cents=50.0,
                    export_price_cents=5.0,
                ),
            ]
        )
        upcoming = detector.get_upcoming_spikes(schedule)
        assert len(upcoming) == 1
        assert upcoming[0].import_price_cents == 200.0

    def test_spike_event_financial_impact(self) -> None:
        event = SpikeEvent(
            started_at=_now(),
            revenue_cents=500.0,
            costs_avoided_cents=200.0,
        )
        assert event.financial_impact_cents == 700.0
