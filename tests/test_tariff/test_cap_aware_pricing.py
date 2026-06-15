"""Tests for cap-aware pricing in StaticTariffProvider.

Covers:
- Free-window slots priced at 0c while cap is available
- Free-window slots fall back to over_cap_falls_back_to rate when cap exhausted
- Cap tracker integration via wire_cap_tracker
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import aiosqlite

from power_master.accounting.free_window_cap import FreeWindowCapTracker
from power_master.config.schema import (
    BandBase,
    BillingCycleConfig,
    FreeWindowConfig,
    TariffProviderConfig,
    TariffPlanConfig,
    TariffVersion,
)
from power_master.db.repository import Repository
from power_master.tariff.providers.static_tou import StaticTariffProvider


def _make_four_for_free_provider_with_cap(cap_tracker=None) -> StaticTariffProvider:
    """Make a provider with FOUR4FREE plan (10:00-13:59 free, 50 kWh/day cap)."""
    config = TariffProviderConfig(
        type="tou",
        timezone="Australia/Brisbane",
        plan=TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        # Peak: 16:00-22:59 = 55.55c
                        BandBase(
                            descriptor="peak",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=55.55,
                        ),
                        # Shoulder: 14:00-15:59, 23:00-23:59, 00:00-09:59 = 34.1c
                        BandBase(
                            descriptor="shoulder",
                            windows=["14:00-15:59", "23:00-23:59", "00:00-09:59"],
                            rate_c_per_kwh=34.1,
                        ),
                        # Default (off-peak balance): 28.6c
                        BandBase(
                            descriptor="offpeak_balance",
                            windows=[],
                            rate_c_per_kwh=28.6,
                        ),
                    ],
                    free_windows=[
                        FreeWindowConfig(
                            name="four4free",
                            windows=["10:00-13:59"],
                            rate_c_per_kwh=0.0,
                            cap_kwh_per_day=50.0,
                            applies_to_channel="general",
                            over_cap_falls_back_to="offpeak_balance",
                        ),
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(
                length_days=28,
                anchor_date=date(2026, 6, 1),
            ),
            supply_charge_c_per_day=148.5,
        ),
    )
    provider = StaticTariffProvider(config)
    if cap_tracker:
        provider.wire_cap_tracker(cap_tracker)
    return provider


class TestCapAwarePricing:
    """Test cap-aware pricing integration."""

    @pytest.mark.asyncio
    async def test_pricing_at_free_rate_with_cap_available(self) -> None:
        """Free-window slots price at 0c when cap is available."""
        # Create tracker with cap available (no consumption yet)
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=None,
        )

        provider = _make_four_for_free_provider_with_cap(cap_tracker=tracker)

        schedule = await provider.fetch_prices()

        # Find any slot during free window (10:00-13:59)
        # Convert to local time and check hours
        free_window_slots = []
        for slot in schedule.slots:
            local_time = slot.start.astimezone(ZoneInfo("Australia/Brisbane"))
            if 10 <= local_time.hour < 14:
                free_window_slots.append(slot)

        assert len(free_window_slots) > 0, "No slots found during free window 10:00-13:59"

        # Check first free window slot
        slot = free_window_slots[0]
        assert slot.import_price_cents == 0.0  # Free rate
        assert slot.descriptor == "four4free"

    @pytest.mark.asyncio
    async def test_pricing_falls_back_when_cap_exhausted(self) -> None:
        """Free-window slots price at fallback rate when cap exhausted."""
        # Create tracker with cap fully consumed
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=None,
        )
        # Manually exhaust the cap
        await tracker.increment(50.0)

        assert tracker.is_cap_exhausted()

        provider = _make_four_for_free_provider_with_cap(cap_tracker=tracker)

        schedule = await provider.fetch_prices()

        # Find any slot during free window (10:00-13:59)
        free_window_slots = []
        for slot in schedule.slots:
            local_time = slot.start.astimezone(ZoneInfo("Australia/Brisbane"))
            if 10 <= local_time.hour < 14:
                free_window_slots.append(slot)

        assert len(free_window_slots) > 0, "No slots found during free window 10:00-13:59"

        # Check first free window slot
        slot = free_window_slots[0]
        # Should fall back to offpeak_balance rate (28.6c)
        assert slot.import_price_cents == 28.6
        assert slot.descriptor == "offpeak_balance"

    @pytest.mark.asyncio
    async def test_pricing_without_cap_tracker(self) -> None:
        """Free-window slots price at 0c when no cap tracker is wired."""
        provider = _make_four_for_free_provider_with_cap(cap_tracker=None)

        schedule = await provider.fetch_prices()

        # Find any slot during free window (10:00-13:59)
        free_window_slots = []
        for slot in schedule.slots:
            local_time = slot.start.astimezone(ZoneInfo("Australia/Brisbane"))
            if 10 <= local_time.hour < 14:
                free_window_slots.append(slot)

        assert len(free_window_slots) > 0, "No slots found during free window 10:00-13:59"

        # Check first free window slot
        slot = free_window_slots[0]
        # Always prices at free rate (0c) when no cap tracker
        assert slot.import_price_cents == 0.0
        assert slot.descriptor == "four4free"

    @pytest.mark.asyncio
    async def test_pricing_with_partial_cap_remaining(self) -> None:
        """Free-window slots price correctly when cap is partially consumed."""
        # Create tracker with partial consumption
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=None,
        )
        await tracker.increment(30.0)  # 30 out of 50 consumed
        assert tracker.get_remaining_cap() == 20.0

        provider = _make_four_for_free_provider_with_cap(cap_tracker=tracker)

        schedule = await provider.fetch_prices()

        # Find any slot during free window (10:00-13:59)
        free_window_slots = []
        for slot in schedule.slots:
            local_time = slot.start.astimezone(ZoneInfo("Australia/Brisbane"))
            if 10 <= local_time.hour < 14:
                free_window_slots.append(slot)

        assert len(free_window_slots) > 0, "No slots found during free window 10:00-13:59"

        # Check first free window slot
        slot = free_window_slots[0]
        # Should still price at 0c (cap available)
        assert slot.import_price_cents == 0.0
        assert slot.descriptor == "four4free"

    @pytest.mark.asyncio
    async def test_non_free_window_unaffected_by_cap(self) -> None:
        """Non-free-window slots always use band rate (cap doesn't affect them)."""
        # Create tracker with cap fully consumed
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=None,
        )
        await tracker.increment(50.0)

        provider = _make_four_for_free_provider_with_cap(cap_tracker=tracker)

        schedule = await provider.fetch_prices()

        # Find any slot during peak (16:00-22:59)
        peak_slots = []
        for slot in schedule.slots:
            local_time = slot.start.astimezone(ZoneInfo("Australia/Brisbane"))
            if 16 <= local_time.hour < 23:
                peak_slots.append(slot)

        assert len(peak_slots) > 0, "No slots found during peak 16:00-22:59"

        # Check first peak slot
        slot = peak_slots[0]
        # Peak rate (55.55c) regardless of cap state
        assert slot.import_price_cents == 55.55
        assert slot.descriptor == "peak"

    @pytest.mark.asyncio
    async def test_wire_cap_tracker_idempotent(self) -> None:
        """wire_cap_tracker can be called and wired."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=None,
        )
        provider = _make_four_for_free_provider_with_cap(cap_tracker=None)

        # Initially no tracker
        assert provider._cap_tracker is None

        # Wire it in
        provider.wire_cap_tracker(tracker)
        assert provider._cap_tracker is tracker
