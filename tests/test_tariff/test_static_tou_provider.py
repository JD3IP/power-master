"""Tests for StaticTariffProvider with TOU tariff DSL.

Covers:
- Non-contiguous midnight-crossing shoulder band (FOUR4FREE fixture)
- DST boundary handling with fixed local windows
- Midnight-crossing bands
- Transition-day 23h/25h edge cases
- fetch_historical determinism
- is_healthy validation
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from power_master.config.schema import (
    BandBase,
    BillingCycleConfig,
    CreditConfig,
    FeedInBand,
    FeedInTier,
    FreeWindowConfig,
    TariffProviderConfig,
    TariffPlanConfig,
    TariffVersion,
    VPPConfig,
)
from power_master.tariff.base import TariffSlot
from power_master.tariff.providers.static_tou import StaticTariffProvider


class TestStaticTOUBasics:
    """Basic provider initialization and slot generation."""

    def test_init_requires_type_tou(self) -> None:
        """Provider rejects non-tou config."""
        config = TariffProviderConfig(type="amber")
        with pytest.raises(ValueError, match="type='tou'"):
            StaticTariffProvider(config)

    def test_init_requires_timezone(self) -> None:
        """Provider requires timezone (validated by Pydantic)."""
        # Pydantic should catch this before provider init
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="timezone is REQUIRED"):
            TariffProviderConfig(
                type="tou",
                plan=TariffPlanConfig(
                    versions=[
                        TariffVersion(
                            valid_from=date(2026, 6, 1),
                            valid_until=None,
                            import_bands=[
                                BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
                            ],
                        )
                    ],
                    billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                    supply_charge_c_per_day=148.5,
                ),
            )

    def test_init_requires_plan(self) -> None:
        """Provider requires plan (validated by Pydantic)."""
        # Pydantic should catch this before provider init
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="plan is REQUIRED"):
            TariffProviderConfig(
                type="tou",
                timezone="Australia/Brisbane",
            )

    @pytest.mark.asyncio
    async def test_fetch_prices_generates_96_slots(self) -> None:
        """Fetch generates ~96 slots for 48-hour horizon at 30-min granularity."""
        provider = _make_simple_provider()
        schedule = await provider.fetch_prices()

        # 48 hours * 60 min / 30 min per slot = 96 slots
        assert len(schedule.slots) == 96
        assert schedule.provider == "static_tou"

        # Check slot granularity
        for i, slot in enumerate(schedule.slots):
            expected_duration = timedelta(minutes=30)
            actual_duration = slot.end - slot.start
            assert (
                actual_duration == expected_duration
            ), f"Slot {i} duration {actual_duration} != {expected_duration}"

    @pytest.mark.asyncio
    async def test_is_healthy_true_for_valid_config(self) -> None:
        """is_healthy returns True when config is valid."""
        provider = _make_simple_provider()
        healthy = await provider.is_healthy()
        assert healthy is True

    @pytest.mark.asyncio
    async def test_is_healthy_false_no_active_version(self) -> None:
        """is_healthy returns False when no version covers today."""
        # Create a provider with a version that ended yesterday
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2020, 1, 1),
                        valid_until=date(2020, 12, 31),
                        import_bands=[
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(
                    length_days=28, anchor_date=date(2020, 1, 1)
                ),
                supply_charge_c_per_day=148.5,
            ),
        )
        provider = StaticTariffProvider(config)
        healthy = await provider.is_healthy()
        assert healthy is False


class TestFOUR4FREEFixture:
    """FOUR4FREE-like fixture with non-contiguous midnight-crossing shoulder band."""

    @pytest.fixture
    def four4free_provider(self) -> StaticTariffProvider:
        """FOUR4FREE-like config:
        - Peak 16:00-22:59 @ 55.55c
        - Off-peak (free window) 10:00-13:59 @ 0c (50 kWh/day cap)
        - Shoulder (default/fallback) 14:00-15:59 + 23:00-23:59 + 00:00-09:59 @ 34.1c
        - FiT: 8c 16:00-22:59, else 0c
        """
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(
                                descriptor="peak",
                                windows=["16:00-22:59"],
                                rate_c_per_kwh=55.55,
                            ),
                            BandBase(
                                descriptor="shoulder",
                                windows=[
                                    "14:00-15:59",
                                    "23:00-23:59",
                                    "00:00-09:59",
                                ],
                                rate_c_per_kwh=34.1,
                            ),
                            BandBase(
                                descriptor="off-peak-balance",
                                windows=[],  # default band
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
                                over_cap_falls_back_to="off-peak-balance",
                            )
                        ],
                        feed_in_bands=[
                            FeedInBand(
                                name="evening",
                                windows=["16:00-22:59"],
                                rate_c_per_kwh=8.0,
                            ),
                            FeedInBand(
                                name="default",
                                windows=[],
                                rate_c_per_kwh=0.0,
                            ),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(
                    length_days=28, anchor_date=date(2026, 6, 1)
                ),
                supply_charge_c_per_day=148.5,
            ),
        )
        return StaticTariffProvider(config)

    @pytest.mark.asyncio
    async def test_peak_window_price(self, four4free_provider: StaticTariffProvider) -> None:
        """18:00 local time is in peak window (16:00-22:59); priced at 55.55c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 18:00 Brisbane = 08:00 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)

        # Find the 18:00 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 18 and slot_local.minute == 0:
                assert slot.import_price_cents == 55.55
                assert slot.descriptor == "peak"
                found = True
                break
        assert found, "No 18:00 local slot found in schedule"

    @pytest.mark.asyncio
    async def test_free_window_price(
        self, four4free_provider: StaticTariffProvider
    ) -> None:
        """11:00 local time is in free window (10:00-13:59); priced at 0c."""
        # Use fetch_historical with a known date to ensure coverage
        start = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 2, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)

        # Find a slot that falls at 11:00 local time
        tz = ZoneInfo("Australia/Brisbane")
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 11 and slot_local.minute == 0:
                assert slot.import_price_cents == 0.0
                assert slot.descriptor == "four4free"
                found = True
                break
        assert found, "No slot found at 11:00 local time in the schedule"

    @pytest.mark.asyncio
    async def test_shoulder_morning_segment(
        self, four4free_provider: StaticTariffProvider
    ) -> None:
        """03:00 local is in shoulder segment (00:00-09:59); priced at 34.1c."""
        start = datetime(2026, 6, 14, 16, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 14, 18, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)
        tz = ZoneInfo("Australia/Brisbane")

        # Find a slot at 03:00 local time
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 3 and slot_local.minute == 0:
                assert slot.import_price_cents == 34.1
                assert slot.descriptor == "shoulder"
                found = True
                break
        assert found, "No slot found at 03:00 local time in the schedule"

    @pytest.mark.asyncio
    async def test_shoulder_afternoon_segment(
        self, four4free_provider: StaticTariffProvider
    ) -> None:
        """14:30 local is in shoulder segment (14:00-15:59); priced at 34.1c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 14:30 Brisbane = 04:30 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 4, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 5, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)

        # Find the 14:30 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 14 and slot_local.minute == 30:
                assert slot.import_price_cents == 34.1
                assert slot.descriptor == "shoulder"
                found = True
                break
        assert found, "No 14:30 local slot found in schedule"

    @pytest.mark.asyncio
    async def test_shoulder_evening_segment(
        self, four4free_provider: StaticTariffProvider
    ) -> None:
        """23:30 local is in shoulder segment (23:00-23:59); priced at 34.1c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 23:30 Brisbane = 13:30 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)

        # Find the 23:30 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 23 and slot_local.minute == 30:
                assert slot.import_price_cents == 34.1
                assert slot.descriptor == "shoulder"
                found = True
                break
        assert found, "No 23:30 local slot found in schedule"

    @pytest.mark.asyncio
    async def test_fit_in_peak_window(
        self, four4free_provider: StaticTariffProvider
    ) -> None:
        """20:00 local is in peak window; FiT is 8c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 20:00 Brisbane = 10:00 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)

        # Find the 20:00 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 20 and slot_local.minute == 0:
                assert slot.export_price_cents == 8.0
                found = True
                break
        assert found, "No 20:00 local slot found in schedule"

    @pytest.mark.asyncio
    async def test_fit_outside_peak_window(
        self, four4free_provider: StaticTariffProvider
    ) -> None:
        """12:00 local is outside peak window; FiT is 0c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 12:00 Brisbane = 02:00 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 1, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
        schedule = await four4free_provider.fetch_historical(start, end)

        # Find the 12:00 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 12 and slot_local.minute == 0:
                assert slot.export_price_cents == 0.0
                found = True
                break
        assert found, "No 12:00 local slot found in schedule"


class TestDSTBoundaryHandling:
    """Test timezone/DST handling at boundary transitions."""

    @pytest.fixture
    def sydney_provider_with_dst(self) -> StaticTariffProvider:
        """Provider with Australia/Sydney timezone (has DST).

        Midday free window 10:00-14:00 local time.
        Tests that this window stays correct before and after DST transition.
        """
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Sydney",  # AEDT/AEST
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 1, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(
                                descriptor="daytime-free",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=0.0,
                            ),
                            BandBase(
                                descriptor="peak",
                                windows=["16:00-22:00"],
                                rate_c_per_kwh=50.0,
                            ),
                            BandBase(
                                descriptor="shoulder",
                                windows=[],  # default band
                                rate_c_per_kwh=30.0,
                            ),
                        ],
                        free_windows=[],
                        feed_in_bands=[
                            FeedInBand(
                                name="default",
                                windows=[],
                                rate_c_per_kwh=5.0,
                            ),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(
                    length_days=28, anchor_date=date(2026, 1, 1)
                ),
                supply_charge_c_per_day=200.0,
            ),
        )
        return StaticTariffProvider(config)

    @pytest.mark.asyncio
    async def test_dst_winter_side_before_transition(
        self, sydney_provider_with_dst: StaticTariffProvider
    ) -> None:
        """Before Oct 2026 spring-forward, verify 10:00 Sydney is in daytime-free.

        Check a date before DST (e.g., June) when Sydney is AEST (UTC+10).
        June 15, 2026, 10:00 AEST = June 15, 2026, 00:00 UTC
        """
        # Use fetch_historical to control exact dates
        tz = ZoneInfo("Australia/Sydney")
        start = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 2, 0, tzinfo=timezone.utc)

        schedule = await sydney_provider_with_dst.fetch_historical(start, end)
        assert len(schedule.slots) > 0

        # Check the slot at 10:00 Sydney time
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 10 and slot_local.minute == 0:
                assert slot.descriptor == "daytime-free"
                assert slot.import_price_cents == 0.0
                found = True
                break
        assert found, "No 10:00 slot found in winter period"

    @pytest.mark.asyncio
    async def test_dst_summer_side_after_transition(
        self, sydney_provider_with_dst: StaticTariffProvider
    ) -> None:
        """After Oct 2026 spring-forward, verify 10:00 Sydney is in daytime-free.

        Check a date after DST (e.g., December) when Sydney is AEDT (UTC+11).
        Dec 15, 2026, 10:00 AEDT = Dec 14, 2026, 23:00 UTC
        """
        tz = ZoneInfo("Australia/Sydney")
        start = datetime(2026, 12, 14, 22, 0, tzinfo=timezone.utc)
        end = datetime(2026, 12, 15, 0, 0, tzinfo=timezone.utc)

        schedule = await sydney_provider_with_dst.fetch_historical(start, end)
        assert len(schedule.slots) > 0

        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 10 and slot_local.minute == 0:
                assert slot.descriptor == "daytime-free"
                assert slot.import_price_cents == 0.0
                found = True
                break
        assert found, "No 10:00 slot found in summer period"

    @pytest.mark.asyncio
    async def test_dst_transition_day_morning(
        self, sydney_provider_with_dst: StaticTariffProvider
    ) -> None:
        """On DST transition day, check morning hours before the jump (AEST).

        October 4, 2026 is spring-forward in Australia (clocks go forward at 2am).
        Before 2am is AEST (UTC+10); after is AEDT (UTC+11).
        Check that 01:00 AEST is NOT in daytime-free window.
        """
        tz = ZoneInfo("Australia/Sydney")
        start = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)

        schedule = await sydney_provider_with_dst.fetch_historical(start, end)
        assert len(schedule.slots) > 0

        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 1 and slot_local.minute == 0:
                # 01:00 is outside [10:00-14:00], so NOT daytime-free
                assert slot.descriptor != "daytime-free"
                found = True
                break
        assert found, "No 01:00 slot found on transition day"

    @pytest.mark.asyncio
    async def test_dst_transition_day_after_jump(
        self, sydney_provider_with_dst: StaticTariffProvider
    ) -> None:
        """After spring-forward jump, check 10:00 AEDT is in daytime-free.

        October 4, 2026, 10:00 AEDT (after the 2am jump) = Oct 3, 23:00 UTC.
        """
        tz = ZoneInfo("Australia/Sydney")
        start = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)

        schedule = await sydney_provider_with_dst.fetch_historical(start, end)
        assert len(schedule.slots) > 0

        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 10 and slot_local.minute == 0:
                # After DST transition, 10:00 should still be in daytime-free
                assert slot.descriptor == "daytime-free"
                assert slot.import_price_cents == 0.0
                found = True
                break
        assert found, "No 10:00 AEDT slot found after transition"


class TestMidnightCrossingBands:
    """Test midnight-crossing window handling."""

    @pytest.fixture
    def midnight_crossing_provider(self) -> StaticTariffProvider:
        """Provider with a midnight-crossing off-peak band.

        Off-peak 22:00-07:00 (spans midnight).
        """
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(
                                descriptor="off-peak",
                                windows=["22:00-07:00"],
                                rate_c_per_kwh=15.0,
                            ),
                            BandBase(
                                descriptor="peak",
                                windows=[],  # default band
                                rate_c_per_kwh=45.0,
                            ),
                        ],
                        free_windows=[],
                        feed_in_bands=[
                            FeedInBand(
                                name="default",
                                windows=[],
                                rate_c_per_kwh=5.0,
                            ),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(
                    length_days=28, anchor_date=date(2026, 6, 1)
                ),
                supply_charge_c_per_day=148.5,
            ),
        )
        return StaticTariffProvider(config)

    @pytest.mark.asyncio
    async def test_midnight_crossing_before_midnight(
        self, midnight_crossing_provider: StaticTariffProvider
    ) -> None:
        """23:00 local is in off-peak (22:00-07:00); priced at 15c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 23:00 Brisbane = 13:00 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
        schedule = await midnight_crossing_provider.fetch_historical(start, end)

        # Find the 23:00 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 23 and slot_local.minute == 0:
                assert slot.import_price_cents == 15.0
                assert slot.descriptor == "off-peak"
                found = True
                break
        assert found, "No 23:00 local slot found in schedule"

    @pytest.mark.asyncio
    async def test_midnight_crossing_after_midnight(
        self, midnight_crossing_provider: StaticTariffProvider
    ) -> None:
        """04:00 local is in off-peak (22:00-07:00); priced at 15c."""
        start = datetime(2026, 6, 14, 18, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 14, 20, 0, tzinfo=timezone.utc)
        schedule = await midnight_crossing_provider.fetch_historical(start, end)
        tz = ZoneInfo("Australia/Brisbane")

        # Find a slot at 04:00 local time
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 4 and slot_local.minute == 0:
                assert slot.import_price_cents == 15.0
                assert slot.descriptor == "off-peak"
                found = True
                break
        assert found, "No slot found at 04:00 local time in the schedule"

    @pytest.mark.asyncio
    async def test_midnight_crossing_outside_window(
        self, midnight_crossing_provider: StaticTariffProvider
    ) -> None:
        """12:00 local is outside off-peak (22:00-07:00); falls back to peak 45c."""
        # Use fetch_historical to avoid flakiness from hardcoded date
        # 12:00 Brisbane = 02:00 UTC
        tz = ZoneInfo("Australia/Brisbane")
        start = datetime(2026, 6, 15, 1, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
        schedule = await midnight_crossing_provider.fetch_historical(start, end)

        # Find the 12:00 local slot
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 12 and slot_local.minute == 0:
                assert slot.import_price_cents == 45.0
                assert slot.descriptor == "peak"
                found = True
                break
        assert found, "No 12:00 local slot found in schedule"


class TestFetchHistoricalDeterminism:
    """Test that fetch_historical generates consistent results."""

    @pytest.mark.asyncio
    async def test_fetch_historical_deterministic(self) -> None:
        """Multiple calls to fetch_historical for same range produce identical slots."""
        provider = _make_simple_provider()

        start = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc)

        schedule1 = await provider.fetch_historical(start, end)
        schedule2 = await provider.fetch_historical(start, end)

        assert len(schedule1.slots) == len(schedule2.slots)
        for s1, s2 in zip(schedule1.slots, schedule2.slots):
            assert s1.start == s2.start
            assert s1.end == s2.end
            assert s1.import_price_cents == s2.import_price_cents
            assert s1.export_price_cents == s2.export_price_cents
            assert s1.descriptor == s2.descriptor

    @pytest.mark.asyncio
    async def test_fetch_historical_covers_range(self) -> None:
        """fetch_historical generates slots covering the entire [start, end) range."""
        provider = _make_simple_provider()

        start = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)

        schedule = await provider.fetch_historical(start, end)

        # 2 hours * 60 min / 30 min per slot = 4 slots
        assert len(schedule.slots) == 4
        assert schedule.slots[0].start == start
        assert schedule.slots[-1].end == end

    @pytest.mark.asyncio
    async def test_fetch_historical_with_version_boundary(self) -> None:
        """fetch_historical spans a version change boundary correctly."""
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=date(2026, 6, 10),
                        import_bands=[
                            BandBase(
                                descriptor="v1",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=25.0,
                            ),
                            BandBase(
                                descriptor="default",
                                windows=[],
                                rate_c_per_kwh=50.0,
                            ),
                        ],
                        feed_in_bands=[
                            FeedInBand(
                                name="default",
                                windows=[],
                                rate_c_per_kwh=5.0,
                            ),
                        ],
                    ),
                    TariffVersion(
                        valid_from=date(2026, 6, 11),
                        valid_until=None,
                        import_bands=[
                            BandBase(
                                descriptor="v2",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=30.0,
                            ),
                            BandBase(
                                descriptor="default",
                                windows=[],
                                rate_c_per_kwh=45.0,
                            ),
                        ],
                        feed_in_bands=[
                            FeedInBand(
                                name="default",
                                windows=[],
                                rate_c_per_kwh=6.0,
                            ),
                        ],
                    ),
                ],
                billing_cycle=BillingCycleConfig(
                    length_days=28, anchor_date=date(2026, 6, 1)
                ),
                supply_charge_c_per_day=148.5,
            ),
        )
        provider = StaticTariffProvider(config)

        # Query from v1 to v2
        # Jun 10, 12:00 Brisbane = Jun 10, 02:00 UTC (in v1 window @ 25c)
        # Jun 11, 12:00 Brisbane = Jun 11, 02:00 UTC (in v2 window @ 30c)
        start = datetime(2026, 6, 10, 2, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 11, 2, 0, tzinfo=timezone.utc)

        schedule = await provider.fetch_historical(start, end)

        # 24 hours = 48 slots
        assert len(schedule.slots) == 48

        # First slot should be v1
        assert schedule.slots[0].import_price_cents == 25.0

        # Scan for a slot on Jun 11 at 12:00 Brisbane (in v2 window @ 30c)
        tz = ZoneInfo("Australia/Brisbane")
        found_v2 = False
        debug_slots_11 = []
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.date() == date(2026, 6, 11):
                debug_slots_11.append((slot_local, slot.descriptor, slot.import_price_cents))
            if slot_local.date() == date(2026, 6, 11) and 11 <= slot_local.hour <= 13 and slot_local.minute == 0:
                assert slot.import_price_cents == 30.0, f"Slot at {slot_local} has price {slot.import_price_cents} not 30.0"
                assert slot.descriptor == "v2", f"Slot at {slot_local} has descriptor {slot.descriptor} not v2"
                found_v2 = True
                break

        # Debug output if not found
        if not found_v2 and debug_slots_11:
            import sys
            print(f"\nDebug: Found {len(debug_slots_11)} slots on Jun 11:", file=sys.stderr)
            for local_time, desc, price in debug_slots_11[:10]:
                print(f"  {local_time}: {desc} @ {price}c", file=sys.stderr)

        assert found_v2, f"No v2 slot found at 12:00 on Jun 11 (checked {len(schedule.slots)} total slots)"


class TestTimeWindowEdgeCases:
    """Test edge cases in window matching."""

    @pytest.mark.asyncio
    async def test_window_boundary_inclusive_start(self) -> None:
        """Window boundary is inclusive on start (HH:MM <= time < end)."""
        provider = _make_simple_provider()
        start = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 1, 0, tzinfo=timezone.utc)
        schedule = await provider.fetch_historical(start, end)
        tz = ZoneInfo("Australia/Brisbane")

        # Scan for a slot at exactly 10:00 Brisbane (free window start)
        found = False
        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 10 and slot_local.minute == 0:
                # Should be in the free window
                assert slot.descriptor == "free"
                found = True
                break
        assert found, "No 10:00 slot found in schedule"

    @pytest.mark.asyncio
    async def test_window_boundary_exclusive_end(self) -> None:
        """Window boundary is exclusive on end (start <= time < HH:MM)."""
        provider = _make_simple_provider()
        start = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc)
        schedule = await provider.fetch_historical(start, end)
        tz = ZoneInfo("Australia/Brisbane")

        # Find slots at 13:30 (before free window end) and 14:00+ (at/after end)
        found_before_end = False
        found_at_end = False

        for slot in schedule.slots:
            slot_local = slot.start.astimezone(tz)
            if slot_local.hour == 13 and slot_local.minute == 30:
                assert slot.descriptor == "free", "13:30 should be in free window"
                found_before_end = True
            elif slot_local.hour == 14 and slot_local.minute == 0:
                # Should NOT be in free window (exclusive end boundary)
                assert slot.descriptor != "free", "14:00 should NOT be in free window"
                found_at_end = True

        assert found_before_end, "No 13:30 slot found"
        assert found_at_end, "No 14:00 slot found"


# ============================================================================
# Helper fixture
# ============================================================================


def _make_simple_provider() -> StaticTariffProvider:
    """Create a simple test provider (FOUR4FREE-like).

    - Free window: 10:00-14:00 @ 0c
    - Peak: 16:00-22:59 @ 55c
    - Shoulder (default): else @ 34c
    - FiT: 8c 16:00-22:59, else 0c
    """
    config = TariffProviderConfig(
        type="tou",
        timezone="Australia/Brisbane",
        plan=TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(
                            descriptor="peak",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=55.0,
                        ),
                        BandBase(
                            descriptor="shoulder",
                            windows=[],  # default band
                            rate_c_per_kwh=34.0,
                        ),
                    ],
                    free_windows=[
                        FreeWindowConfig(
                            name="free",
                            windows=["10:00-14:00"],
                            rate_c_per_kwh=0.0,
                            cap_kwh_per_day=50.0,
                            applies_to_channel="general",
                            over_cap_falls_back_to="shoulder",
                        )
                    ],
                    feed_in_bands=[
                        FeedInBand(
                            name="evening",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=8.0,
                        ),
                        FeedInBand(
                            name="default",
                            windows=[],
                            rate_c_per_kwh=0.0,
                        ),
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(
                length_days=28, anchor_date=date(2026, 6, 1)
            ),
            supply_charge_c_per_day=148.5,
        ),
    )
    return StaticTariffProvider(config)
